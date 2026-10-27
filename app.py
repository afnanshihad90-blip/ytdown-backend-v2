import glob
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse, quote

import requests
import yt_dlp
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # allow requests from the InfinityFree domain

# ------------------------------------------------------------------ limits
MAX_DURATION = 40 * 60              # seconds (video)
MAX_MP3_DURATION = 25 * 60          # seconds (mp3 conversion is CPU heavy)
MAX_FILESIZE = 400 * 1024 * 1024    # bytes per stream
MAX_PROXY_BYTES = 150 * 1024 * 1024
MAX_JOBS_AT_ONCE = 2
JOB_TTL = 15 * 60                   # keep finished files this long

IMG_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'gif', 'avif'}
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# Optional YouTube cookies (Netscape format) from env var YT_COOKIES
COOKIE_PATH = '/tmp/yt_cookies.txt'
if os.environ.get('YT_COOKIES'):
    with open(COOKIE_PATH, 'w', encoding='utf-8') as _f:
        _f.write(os.environ['YT_COOKIES'])
HAS_COOKIES = os.path.exists(COOKIE_PATH)

# YouTube player-client sets tried in order when the default one gets blocked
CLIENT_SETS = [None, ['tv'], ['android_vr'], ['web_safari'], ['mweb']]
RETRY_HINTS = ('sign in', 'bot', 'confirm you', '403', 'requested format',
               'player response', 'not available on this app', 'po token')

JOBS = {}
JOBS_LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(MAX_JOBS_AT_ONCE)


# ------------------------------------------------------------------ helpers
def is_valid_url(url):
    return isinstance(url, str) and url.startswith(('http://', 'https://'))


def is_youtube(url):
    host = (urlparse(url).hostname or '').lower()
    return 'youtube.com' in host or 'youtu.be' in host


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', ' ', name or 'video').strip()
    return name[:90] or 'video'


def clean(msg):
    return re.sub(r'\x1b\[[0-9;]*m', '', str(msg))


def friendly(msg):
    m = clean(msg)
    low = m.lower()
    if 'unsupported url' in low:
        return 'This link is not supported.'
    if 'sign in to confirm' in low or re.search(r'\bbot\b', low):
        return ('YouTube blocked the server (bot check). '
                'Please try again in a few minutes or try another video.')
    if 'private' in low:
        return 'This content is private.'
    if 'login' in low or 'cookies' in low or 'age' in low and 'restrict' in low:
        return 'This post needs a login (private or age-restricted).'
    if 'unavailable' in low or 'not available' in low:
        return 'This content is unavailable.'
    return m[:220]


def fmt_duration(sec):
    if not sec:
        return ''
    mins, secs = divmod(int(sec), 60)
    hours, mins = divmod(mins, 60)
    return f'{hours}:{mins:02d}:{secs:02d}' if hours else f'{mins}:{secs:02d}'


def url_ext(u, default='jpg'):
    e = os.path.splitext(urlparse(u).path)[1].lstrip('.').lower()
    return e if e in IMG_EXTS else default


TIERS = [(7000, '8K'), (3600, '4K'), (2400, '2K'), (1800, '1080p'),
         (1200, '720p'), (800, '480p'), (600, '360p'), (400, '240p'), (0, '144p')]


def tier_label(w, h, fps):
    long_side = max(w, h) if (w and h) else (h or 0) * 1.78
    label = '144p'
    for limit, name in TIERS:
        if long_side >= limit:
            label = name
            break
    if fps and fps > 30 and label.endswith('p'):
        label += str(int(round(fps)))
    return label


def base_opts():
    o = {
        'quiet': True,
        'noprogress': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 20,
        'http_headers': {'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'},
    }
    if HAS_COOKIES:
        o['cookiefile'] = COOKIE_PATH
    return o


def client_sets_for(url):
    return CLIENT_SETS if is_youtube(url) else [None]


def extract_with_fallback(url):
    last = None
    for cs in client_sets_for(url):
        o = base_opts()
        o['skip_download'] = True
        if cs:
            o['extractor_args'] = {'youtube': {'player_client': cs}}
        try:
            with yt_dlp.YoutubeDL(o) as y:
                return y.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001
            last = e
            if not any(k in clean(e).lower() for k in RETRY_HINTS):
                break
    raise last


def size_of(f, duration):
    exact = f.get('filesize')
    if exact:
        return exact, False
    approx = f.get('filesize_approx')
    if approx:
        return approx, True
    tbr = f.get('tbr') or f.get('abr') or 0
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration), True
    return 0, True


# ------------------------------------------------------------------ gallery-dl fallback (photo posts)
def gallery_media(url):
    try:
        out = subprocess.run(
            [sys.executable, '-m', 'gallery_dl', '-g', url],
            capture_output=True, text=True, timeout=45)
    except Exception:  # noqa: BLE001
        return None
    urls = []
    for line in out.stdout.splitlines():
        line = line.strip().lstrip('|').strip()
        if line.startswith('http') and line not in urls:
            urls.append(line)
    if not urls:
        return None
    items = []
    for i, u in enumerate(urls[:30], 1):
        ext = os.path.splitext(urlparse(u).path)[1].lstrip('.').lower() or 'jpg'
        kind = 'image' if ext in IMG_EXTS else 'video'
        items.append({'label': f"{'Photo' if kind == 'image' else 'Video'} {i}",
                      'kind': kind, 'url': u, 'ext': ext,
                      'name': f'post-{i}.{ext}', 'thumb': u if kind == 'image' else '', 'size': 0})
    first_img = next((it['url'] for it in items if it['kind'] == 'image'), '')
    return {'success': True, 'title': 'Post media', 'thumbnail': first_img, 'duration': '',
            'platform': urlparse(url).hostname or '', 'videos': [], 'original': None,
            'audios': [], 'mp3': [], 'images': [], 'subtitles': [], 'items': items}


# ------------------------------------------------------------------ info
def build_response(info):
    duration = info.get('duration') or 0
    title = info.get('title') or 'Untitled'
    fname = safe_filename(title)
    raw = info.get('formats') or ([info] if info.get('url') and not info.get('entries') else [])
    fmts = [f for f in raw
            if f.get('url') and f.get('protocol') != 'mhtml'
            and f.get('format_note') != 'storyboard']

    vids, auds = {}, {}
    any_video = False
    for f in fmts:
        vc, ac = f.get('vcodec'), f.get('acodec')
        unknown = vc is None and ac is None
        is_audio_ext = (f.get('ext') or '').lower() in ('m4a', 'mp3', 'opus', 'ogg', 'aac', 'wav', 'flac')
        vc, ac = vc or 'none', ac or 'none'
        if unknown:
            vc, ac = ('none', 'unknown') if is_audio_ext else ('unknown', 'none')
        if vc != 'none':
            any_video = True
            h = f.get('height') or 0
            if h:
                cur = vids.get(h)
                if not cur or (f.get('tbr') or 0) > (cur.get('tbr') or 0):
                    vids[h] = f
        elif ac != 'none':
            abr = int(round(f.get('abr') or f.get('tbr') or 0))
            key = (f.get('ext'), abr)
            if key not in auds:
                auds[key] = f

    audio_list = sorted(auds.values(),
                        key=lambda f: -(f.get('abr') or f.get('tbr') or 0))
    best_audio_size = 0
    if audio_list:
        best_audio_size = size_of(audio_list[0], duration)[0]

    videos = []
    seen_labels = set()
    def long_side(h):
        w = vids[h].get('width') or 0
        return max(w, h)

    for h in sorted(vids, key=long_side, reverse=True):
        f = vids[h]
        label = tier_label(f.get('width'), h, f.get('fps'))
        if label in seen_labels:
            continue
        seen_labels.add(label)
        sz, approx = size_of(f, duration)
        has_audio = (f.get('acodec') or 'none') != 'none'
        if not has_audio:
            sz += best_audio_size
        videos.append({'label': label, 'height': h, 'ext': 'mp4',
                       'size': sz, 'approx': approx})

    original = None
    if videos:
        original = {'label': 'Original (best quality)', 'height': 0, 'ext': 'mp4',
                    'size': videos[0]['size'], 'approx': videos[0]['approx']}
    elif any_video:
        original = {'label': 'Original (best quality)', 'height': 0, 'ext': 'mp4',
                    'size': 0, 'approx': True}

    audios = []
    for f in audio_list:
        abr = int(round(f.get('abr') or f.get('tbr') or 0))
        ext = f.get('ext') or 'm4a'
        sz, approx = size_of(f, duration)
        audios.append({'label': f"{ext.upper()} {abr} kbps" if abr else ext.upper(),
                       'abr': abr, 'ext': ext, 'size': sz, 'approx': approx})

    # thumbnail
    images = []
    thumbs = [t for t in (info.get('thumbnails') or []) if t.get('url', '').startswith('http')]
    best_thumb = None
    if thumbs:
        best_thumb = max(thumbs, key=lambda t: ((t.get('width') or 0) * (t.get('height') or 0),
                                                t.get('preference') or 0))
    turl = (best_thumb or {}).get('url') or info.get('thumbnail') or ''
    if turl:
        ext = url_ext(turl)
        w, h = (best_thumb or {}).get('width'), (best_thumb or {}).get('height')
        images.append({'label': 'Thumbnail' + (f' ({w}×{h})' if w and h else ''),
                       'url': turl, 'ext': ext, 'name': f'{fname}.{ext}'})

    # subtitles
    subs = []
    manual = info.get('subtitles') or {}
    auto = info.get('automatic_captions') or {}
    for lang, lst in list(manual.items())[:12]:
        sub = _pick_sub(lst)
        if sub:
            subs.append({'label': lang, 'url': sub['url'], 'ext': sub.get('ext', 'vtt'),
                         'name': f"{fname}.{lang}.{sub.get('ext', 'vtt')}"})
    if not subs:
        for lang, lst in auto.items():
            if lang in ('en', 'bn') or lang.endswith('-orig'):
                sub = _pick_sub(lst)
                if sub:
                    subs.append({'label': f'{lang} (auto)', 'url': sub['url'],
                                 'ext': sub.get('ext', 'vtt'),
                                 'name': f"{fname}.{lang}.{sub.get('ext', 'vtt')}"})
            if len(subs) >= 3:
                break

    # carousel / multi-media posts
    items = []
    entries = [e for e in (info.get('entries') or []) if e]
    for i, e in enumerate(entries[:30], 1):
        flist = [f for f in (e.get('formats') or []) if f.get('url')]
        direct, ext = e.get('url'), e.get('ext')
        if flist:
            best = max(flist, key=lambda f: (f.get('height') or 0, f.get('tbr') or 0))
            direct, ext = best['url'], best.get('ext')
        if not direct:
            continue
        ext = (ext or url_ext(direct, 'mp4')).lower()
        kind = 'image' if ext in IMG_EXTS else 'video'
        items.append({'label': f"{'Photo' if kind == 'image' else 'Video'} {i}",
                      'kind': kind, 'url': direct, 'ext': ext,
                      'name': f'{fname}-{i}.{ext}', 'thumb': e.get('thumbnail') or '',
                      'size': e.get('filesize') or e.get('filesize_approx') or 0})

    return {
        'success': True,
        'title': title,
        'thumbnail': info.get('thumbnail') or turl,
        'duration': fmt_duration(duration),
        'platform': info.get('extractor_key') or info.get('extractor') or '',
        'videos': videos,
        'original': original,
        'audios': audios,
        'mp3': [320, 256, 192, 128] if (audios or any_video) else [],
        'images': images,
        'subtitles': subs,
        'items': items,
    }


def _pick_sub(lst):
    if not lst:
        return None
    for want in ('vtt', 'srt'):
        for s in lst:
            if s.get('ext') == want and s.get('url'):
                return s
    return next((s for s in lst if s.get('url')), None)


@app.route('/')
def home():
    return jsonify({'status': 'online', 'message': 'YTDown Backend is running',
                    'version': '4.0'})


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/info', methods=['POST', 'GET'])
def video_info():
    if request.method == 'GET':
        url = request.args.get('url', '').strip()
    else:
        data = request.get_json(silent=True) or {}
        url = (data.get('url') or request.form.get('url', '')).strip()

    if not url:
        return jsonify({'success': False, 'message': 'No URL provided'}), 400
    if not is_valid_url(url):
        return jsonify({'success': False, 'message': 'Invalid URL'}), 400

    try:
        info = extract_with_fallback(url)
        resp = build_response(info)
        has_anything = (resp['videos'] or resp['original'] or resp['audios']
                        or resp['items'] or resp['images'])
        if not has_anything:
            raise Exception('No downloadable media found')
        return jsonify(resp)
    except Exception as e:  # noqa: BLE001
        msg = clean(e)
        low = msg.lower()
        if ('unsupported url' in low or 'no video' in low or 'no media' in low
                or 'no downloadable' in low or 'not a video' in low):
            media = gallery_media(url)
            if media:
                return jsonify(media)
        return jsonify({'success': False, 'message': friendly(msg)}), 400


# ------------------------------------------------------------------ jobs (download + merge)
def purge_jobs():
    now = time.time()
    with JOBS_LOCK:
        for jid in list(JOBS):
            j = JOBS[jid]
            if j['status'] in ('ready', 'error') and now - j['updated'] > JOB_TTL:
                shutil.rmtree(j['workdir'], ignore_errors=True)
                del JOBS[jid]


def set_job(job, **kw):
    kw['updated'] = time.time()
    job.update(kw)


def run_job(job, p):
    try:
        url, kind = p['url'], p['kind']
        limit = MAX_MP3_DURATION if kind == 'mp3' else MAX_DURATION
        height, abr, aext = p['height'], p['abr'], p['aext']
        last_err = None

        for cs in client_sets_for(url):
            job['streams_done'] = 0
            job['streams'] = 2 if (kind == 'video') else 1
            set_job(job, status='working', phase='downloading', progress=1)

            def hook(d):
                if d.get('status') == 'downloading':
                    tot = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    frac = (d.get('downloaded_bytes') or 0) / tot if tot else 0
                    pr = (job['streams_done'] + min(frac, 1)) / job['streams'] * 92
                    set_job(job, progress=max(job['progress'], min(pr, 92)))
                elif d.get('status') == 'finished':
                    job['streams_done'] += 1

            def phook(d):
                if d.get('status') == 'started':
                    name = d.get('postprocessor', '')
                    phase = 'converting' if 'ExtractAudio' in name else 'merging'
                    set_job(job, phase=phase, progress=max(job['progress'], 94))

            o = base_opts()
            o.update({
                'outtmpl': os.path.join(job['workdir'], 'out.%(ext)s'),
                'max_filesize': MAX_FILESIZE,
                'match_filter': yt_dlp.utils.match_filter_func(f'duration <=? {limit}'),
                'concurrent_fragment_downloads': 3,
                'progress_hooks': [hook],
                'postprocessor_hooks': [phook],
            })
            if cs:
                o['extractor_args'] = {'youtube': {'player_client': cs}}

            if kind == 'video':
                o['format_sort'] = ['res', 'ext:mp4:m4a']
                o['merge_output_format'] = 'mp4'
                if height:
                    o['format'] = (f'bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b')
                else:
                    o['format'] = 'bv*+ba/b'
            elif kind == 'audio':
                parts = []
                if abr:
                    if aext:
                        parts.append(f'ba[ext={aext}][abr<={abr + 1}]')
                    parts.append(f'ba[abr<={abr + 1}]')
                parts += ['ba', 'b']
                o['format'] = '/'.join(parts)
            else:  # mp3
                o['format'] = 'ba/b'
                o['postprocessors'] = [{'key': 'FFmpegExtractAudio',
                                        'preferredcodec': 'mp3',
                                        'preferredquality': str(p['q'])}]

            try:
                with yt_dlp.YoutubeDL(o) as y:
                    info = y.extract_info(url, download=True)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                for f in glob.glob(os.path.join(job['workdir'], 'out.*')):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                if not any(k in clean(e).lower() for k in RETRY_HINTS):
                    break

        if last_err:
            raise last_err

        files = [f for f in glob.glob(os.path.join(job['workdir'], 'out.*'))
                 if not f.endswith(('.part', '.ytdl'))]
        if not files:
            raise Exception('This video is too long or too large for the free server '
                            f'(max {limit // 60} min / 400 MB).')
        path = max(files, key=os.path.getsize)
        ext = os.path.splitext(path)[1].lstrip('.') or 'mp4'
        set_job(job, status='ready', phase='ready', progress=100, path=path,
                size=os.path.getsize(path),
                filename=f"{safe_filename((info or {}).get('title'))}.{ext}")
    except Exception as e:  # noqa: BLE001
        set_job(job, status='error', message=friendly(e))
    finally:
        SLOTS.release()


@app.route('/api/jobs', methods=['POST'])
def create_job():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    kind = data.get('kind', 'video')
    if not is_valid_url(url):
        return jsonify({'success': False, 'message': 'Invalid URL'}), 400
    if kind not in ('video', 'audio', 'mp3'):
        return jsonify({'success': False, 'message': 'Invalid type'}), 400

    def num(key, lo, hi, default=0):
        try:
            return max(lo, min(int(data.get(key, default) or 0), hi))
        except (TypeError, ValueError):
            return default

    params = {
        'url': url, 'kind': kind,
        'height': num('height', 0, 8640),
        'abr': num('abr', 0, 1000),
        'q': num('q', 64, 320, 192),
        'aext': re.sub(r'[^a-z0-9]', '', str(data.get('aext', '')).lower())[:5],
    }

    purge_jobs()
    if not SLOTS.acquire(blocking=False):
        return jsonify({'success': False,
                        'message': 'Server is busy with other downloads. Try again in a minute.'}), 503

    jid = uuid.uuid4().hex[:16]
    job = {'id': jid, 'status': 'working', 'phase': 'starting', 'progress': 0,
           'message': '', 'workdir': tempfile.mkdtemp(prefix='ytd_'),
           'updated': time.time(), 'path': None, 'size': 0, 'filename': '',
           'streams': 1, 'streams_done': 0}
    with JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=run_job, args=(job, params), daemon=True).start()
    return jsonify({'success': True, 'id': jid})


@app.route('/api/jobs/<jid>')
def job_status(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({'success': False, 'status': 'error',
                        'message': 'Job expired. Please try again.'}), 404
    return jsonify({'success': True, 'status': job['status'], 'phase': job['phase'],
                    'progress': round(job['progress'], 1), 'message': job['message'],
                    'size': job['size'], 'filename': job['filename']})


@app.route('/api/file/<jid>')
def job_file(jid):
    job = JOBS.get(jid)
    if not job or job['status'] != 'ready' or not job['path'] or not os.path.exists(job['path']):
        return jsonify({'success': False, 'message': 'File expired. Please try again.'}), 404
    set_job(job)
    return send_file(job['path'], as_attachment=True, download_name=job['filename'])


# ------------------------------------------------------------------ proxy (photos, subtitles, direct media)
def is_public_host(host):
    try:
        for ai in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(ai[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def open_public(url, hops=4):
    for _ in range(hops):
        p = urlparse(url)
        if p.scheme not in ('http', 'https') or not p.hostname or not is_public_host(p.hostname):
            return None
        r = requests.get(url, stream=True, timeout=20, allow_redirects=False,
                         headers={'User-Agent': UA})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get('Location'):
            url = requests.compat.urljoin(url, r.headers['Location'])
            r.close()
            continue
        return r
    return None


@app.route('/api/proxy')
def proxy():
    u = request.args.get('u', '').strip()
    name = safe_filename(request.args.get('name', 'file'))
    if not is_valid_url(u):
        return jsonify({'success': False, 'message': 'Invalid URL'}), 400
    r = open_public(u)
    if r is None or r.status_code != 200:
        return jsonify({'success': False, 'message': 'Could not fetch the file.'}), 400

    ctype = r.headers.get('Content-Type', 'application/octet-stream').split(';')[0].strip()
    if not ctype.startswith(('image/', 'video/', 'audio/', 'text/', 'application/octet-stream',
                             'application/x-subrip', 'binary/')):
        r.close()
        return jsonify({'success': False, 'message': 'Unsupported file type.'}), 400
    length = r.headers.get('Content-Length')
    if length and length.isdigit() and int(length) > MAX_PROXY_BYTES:
        r.close()
        return jsonify({'success': False, 'message': 'File is too large for this server.'}), 413

    def gen():
        total = 0
        try:
            for chunk in r.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_PROXY_BYTES:
                    break
                yield chunk
        finally:
            r.close()

    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', '_', name) or 'file'
    headers = {
        'Content-Disposition': f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        'Content-Type': ctype,
        'Cache-Control': 'no-store',
    }
    if length and length.isdigit():
        headers['Content-Length'] = length
    return Response(stream_with_context(gen()), headers=headers)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
