import glob
import ipaddress
import os
import random
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

import adminlink

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

# curl_cffi (browser TLS fingerprint impersonation) is included in requirements.
# When present it lets yt-dlp impersonate a real Chrome, which is the most
# reliable way to avoid YouTube's "sign in to confirm you're not a bot" block
# on a datacenter IP (Render) -- no cookies or proxy needed.
try:
    import curl_cffi  # noqa: F401
    HAS_IMPERSONATE = True
except Exception:  # noqa: BLE001
    HAS_IMPERSONATE = False

# Optional YouTube cookies (Netscape format).
# Sources, first found wins: env YT_COOKIES, Render Secret File, ./cookies.txt
# NEVER commit a cookies file to git. Use a throw-away Google account.
COOKIE_PATH = '/tmp/yt_cookies.txt'
_here = os.path.dirname(os.path.abspath(__file__))
if os.environ.get('YT_COOKIES'):
    with open(COOKIE_PATH, 'w', encoding='utf-8') as _f:
        _f.write(os.environ['YT_COOKIES'])
else:
    for _src in ('/etc/secrets/cookies.txt', os.path.join(_here, 'cookies.txt')):
        if os.path.exists(_src):
            shutil.copyfile(_src, COOKIE_PATH)   # copy: yt-dlp may rewrite the file
            break
HAS_COOKIES = os.path.exists(COOKIE_PATH)

# YouTube player-client sets tried in order when the default one gets blocked.
# More clients + impersonation = far fewer false "bot" errors.
CLIENT_SETS = [None, ['web_safari'], ['tv'], ['android_vr'], ['android'],
               ['ios'], ['mweb'], ['web_embedded'], ['android_creator']]

# Optional proxies used ONLY for YouTube (datacenter IPs get bot-blocked):
#   YT_PROXY  = any proxy url you own, e.g. socks5://user:pass@host:port
#   WARP      = local Cloudflare WARP socks5 proxy started by start.sh
WARP_PROXY = 'socks5://127.0.0.1:40000'
def _parse_proxies():
    raw = (os.environ.get('YT_PROXIES') or '') + ',' + (os.environ.get('YT_PROXY') or '')
    out = []
    for p in re.split(r'[,\s]+', raw):
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


# YT_PROXIES = comma/newline separated list (http://user:pass@host:port, socks5://...)
# YT_PROXY   = a single proxy (still supported)
YT_PROXY_LIST = _parse_proxies()
YT_PROXY = YT_PROXY_LIST[0] if YT_PROXY_LIST else None
MAX_PROXY_TRIES = 3   # random proxies tried per request (each failure can cost ~20s)


def warp_ok():
    return os.path.exists('/tmp/warp_ok')


PROXY_META = {}          # proxy url -> (key id, key name) for the admin panel
_cookie_sig = None


def sync_admin():
    """Pull settings from the admin panel (cached ~45s). Safe if the panel is unreachable."""
    global HAS_COOKIES, _cookie_sig
    cfg = adminlink.config()
    ck = (cfg.get('cookies') or '').strip()
    if ck and ck != _cookie_sig:
        try:
            with open(COOKIE_PATH, 'w', encoding='utf-8') as f:
                f.write(ck + '\n')
            _cookie_sig = ck
            HAS_COOKIES = True
        except OSError:
            pass
    return cfg


def admin_proxies():
    out = []
    for k in (adminlink.config().get('keys') or []):
        if k.get('type') == 'proxy' and k.get('value'):
            PROXY_META[k['value']] = (k.get('id'), k.get('name'))
            out.append(k['value'])
    return out


def proxy_label(px):
    return PROXY_META.get(px) or ('env', 'proxy from Render env')


def youtube_proxies():
    out = []
    pool = list(dict.fromkeys(YT_PROXY_LIST + admin_proxies()))
    random.shuffle(pool)               # spread load, skip dead ones by chance
    out.extend(pool[:MAX_PROXY_TRIES])
    if warp_ok():
        out.append(WARP_PROXY)
    out.append(None)  # last resort: direct (Render IP)
    return out
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


def classify(msg):
    low = clean(msg).lower()
    if 'unsupported url' in low:
        return 'unsupported'
    if 'sign in to confirm' in low or re.search(r'\bbot\b', low):
        return 'youtube_bot_check'
    if 'private' in low:
        return 'private'
    if 'login' in low or 'cookies' in low or ('age' in low and 'restrict' in low):
        return 'login_required'
    if 'unavailable' in low or 'not available' in low or 'removed' in low:
        return 'unavailable'
    if 'too long' in low or 'too large' in low:
        return 'too_large'
    if 'timed out' in low or 'timeout' in low:
        return 'timeout'
    if 'quota' in low or 'payment' in low:
        return 'api_quota'
    return 'unknown'


GENERIC_MSG = 'We could not process this link right now. Please try again in a few minutes or try another link.'
USER_MESSAGES = {
    'unsupported': 'This link is not supported.',
    'private': 'This content is private.',
    'login_required': 'This post needs a login (private or age-restricted).',
    'unavailable': 'This content is unavailable.',
    'too_large': 'This video is too long or too large for this server.',
}


def friendly(msg):
    """Short, safe message for visitors. Technical details are sent to the admin panel instead."""
    return USER_MESSAGES.get(classify(msg), GENERIC_MSG)


def client_ip():
    xf = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return xf or request.remote_addr or ''


def short_url(u):
    return re.sub(r'[?#].*$', '', str(u))[:200]


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


def base_opts(proxy=None, impersonate=None):
    o = {
        'quiet': True,
        'noprogress': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 20,
        'http_headers': {'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'},
    }
    if HAS_IMPERSONATE and impersonate:
        o['impersonate'] = impersonate
    if HAS_COOKIES:
        o['cookiefile'] = COOKIE_PATH
    if proxy:
        o['proxy'] = proxy
    return o


def client_sets_for(url):
    return CLIENT_SETS if is_youtube(url) else [None]


def attempts_for(url):
    """(proxy, player_client, impersonate) triples to try, best first.

    Kept small on purpose: each failed attempt can take ~20s, so we don't
    multiply every client by every proxy. Order:
      1. direct + Chrome impersonation (default client, then tv/web_safari)
      2. user's YT_PROXY with default client (covers WARP too)
      3. direct, remaining player clients (last resort)
    """
    if not is_youtube(url):
        # try Chrome impersonation first, then plain (works even if curl_cffi is broken)
        return ([(None, None, 'chrome'), (None, None, None)]
                if HAS_IMPERSONATE else [(None, None, None)])
    out = []
    if HAS_IMPERSONATE:
        for cs in (None, ['tv'], ['web_safari']):
            out.append((None, cs, 'chrome'))
    for px in youtube_proxies():
        if px:
            out.append((px, None, None))
    for cs in CLIENT_SETS:
        out.append((None, cs, None))
    return out


def should_retry(err, imp):
    low = clean(err).lower()
    if any(k in low for k in ('unsupported url', 'private video', 'video unavailable')):
        return False
    # if impersonation was used, the failure may be caused by it -> try next attempt
    return bool(imp) or 'impersonat' in low or any(k in low for k in RETRY_HINTS)


def status_tag():
    return (f"[WARP {'on' if warp_ok() else 'off'}, "
            f"cookies {'on' if HAS_COOKIES else 'off'}, "
            f"proxies {len(YT_PROXY_LIST)}]")


def extract_with_fallback(url):
    last, n = None, 0
    for px, cs, imp in attempts_for(url):
        n += 1
        o = base_opts(px, impersonate=imp)
        o['skip_download'] = True
        if cs:
            o['extractor_args'] = {'youtube': {'player_client': cs}}
        try:
            with yt_dlp.YoutubeDL(o) as y:
                info = y.extract_info(url, download=False)
            if px and px != WARP_PROXY:
                kid, kname = proxy_label(px)
                adminlink.event(kind='key', provider='yt-dlp proxy', key_id=kid, key_name=kname,
                                ok=True, reason='ok')
            info['_via'] = ('proxy: ' + proxy_label(px)[1]) if px else ('direct' + (' +chrome' if imp else ''))
            return info
        except Exception as e:  # noqa: BLE001
            last = e
            if px and px != WARP_PROXY:
                kid, kname = proxy_label(px)
                adminlink.event(kind='key', provider='yt-dlp proxy', key_id=kid, key_name=kname,
                                ok=False, reason=classify(e), detail=clean(e)[:300])
            if not should_retry(e, imp):
                break
    raise Exception(f'[{n} attempts] {clean(last)}')


# ------------------------------------------------------------------ Apify (YouTube)
APIFY_ACTOR_DEFAULT = 'streamers~youtube-video-downloader'
APIFY_COOLDOWN = {}      # key id -> unix time until which the key is skipped
VIDEO_EXT = {'mp4', 'webm', 'mkv', 'mov', 'avi'}
AUDIO_EXT = {'mp3', 'm4a', 'wav', 'opus', 'ogg', 'aac', 'flac'}


def youtube_id(url):
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})', url)
    return m.group(1) if m else ''


def apify_keys():
    now = time.time()
    return [k for k in (adminlink.config().get('keys') or [])
            if k.get('type') == 'apify' and k.get('value')
            and APIFY_COOLDOWN.get(k.get('id'), 0) < now]


def _walk(o, path=''):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f'{path}.{k}')
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, f'{path}[{i}]')
    else:
        yield path, o


def apify_to_resp(items, url):
    """Turn Apify dataset items into our response. Unknown output shape -> look for file URLs."""
    if isinstance(items, dict):
        items = items.get('items') or [items]
    items = [i for i in (items or []) if isinstance(i, dict)]
    if not items:
        raise Exception('apify returned no items')
    first = items[0]
    title = next((str(first[k]) for k in ('title', 'videoTitle', 'name') if first.get(k)), '') or 'YouTube video'
    vid = youtube_id(url)
    thumb = next((first[k] for k in ('thumbnail', 'thumbnailUrl', 'thumb', 'image')
                  if isinstance(first.get(k), str) and first[k].startswith('http')), '')
    if not thumb and vid:
        thumb = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
    dur = first.get('duration')
    duration = fmt_duration(dur) if isinstance(dur, (int, float)) and dur > 0 else ''

    out, seen = [], set()
    fname = safe_filename(title)
    for it in items:
        for path, val in _walk(it):
            if not (isinstance(val, str) and val.startswith('http')) or val in seen:
                continue
            ext = os.path.splitext(urlparse(val).path)[1].lstrip('.').lower()
            looks_file = (ext in VIDEO_EXT or ext in AUDIO_EXT or 'key-value-stores' in val
                          or 'download' in path.lower())
            if not looks_file or ext in IMG_EXTS or 'thumb' in path.lower():
                continue
            seen.add(val)
            if not ext or ext not in (VIDEO_EXT | AUDIO_EXT):
                ext = 'mp3' if 'audio' in path.lower() or 'mp3' in path.lower() else 'mp4'
            kind = 'audio' if ext in AUDIO_EXT else 'video'
            n = len(out) + 1
            out.append({'label': f"{'Audio' if kind == 'audio' else 'Video'} ({ext.upper()})"
                                 + (f' #{n}' if n > 1 else ''),
                        'kind': 'video', 'url': val, 'ext': ext,
                        'name': f'{fname}.{ext}', 'thumb': thumb, 'size': 0})
    if not out:
        keys = ','.join(list(first.keys())[:15])
        raise Exception(f'apify returned no file link (output fields: {keys})')
    return {'success': True, 'title': title, 'thumbnail': thumb, 'duration': duration,
            'platform': 'Youtube', 'videos': [], 'original': None, 'audios': [], 'mp3': [],
            'images': [], 'subtitles': [], 'items': out}


def apify_extract(url, keys, actor):
    """Try each Apify key in the admin-defined order. Returns (response, key). Logs every try."""
    last = 'no apify key available'
    for k in keys:
        t0 = time.time()
        kid, kname = k.get('id'), k.get('name')
        try:
            r = requests.post(
                f'https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items',
                headers={'Authorization': 'Bearer ' + k['value'], 'Content-Type': 'application/json'},
                json={'videos': [{'url': url}]}, timeout=170)
            if r.status_code in (200, 201):
                resp = apify_to_resp(r.json(), url)
                adminlink.event(kind='key', provider='apify', key_id=kid, key_name=kname, ok=True,
                                reason='ok', ms=int((time.time() - t0) * 1000))
                return resp, k
            code = r.status_code
            reason = {401: 'key_invalid', 403: 'key_invalid', 402: 'api_quota',
                      429: 'rate_limited'}.get(code, f'apify_http_{code}')
            cool = {'key_invalid': 900, 'api_quota': 3600, 'rate_limited': 60}.get(reason, 0)
            if cool:
                APIFY_COOLDOWN[kid] = time.time() + cool
            last = f'apify HTTP {code}: {r.text[:250]}'
            adminlink.event(kind='key', provider='apify', key_id=kid, key_name=kname, ok=False,
                            reason=reason, detail=last, ms=int((time.time() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            last = f'apify error: {clean(e)[:250]}'
            adminlink.event(kind='key', provider='apify', key_id=kid, key_name=kname, ok=False,
                            reason=classify(e) if 'timed out' not in last else 'timeout',
                            detail=last, ms=int((time.time() - t0) * 1000))
    raise Exception(last)


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
                    'version': '4.8'})


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'version': '4.8', 'warp': warp_ok(),
                    'cookies': HAS_COOKIES, 'proxy': bool(YT_PROXY_LIST), 'proxies': len(YT_PROXY_LIST),
                    'impersonate': HAS_IMPERSONATE,
                    'ytdlp': getattr(yt_dlp.version, '__version__', '?'),
                    'admin': adminlink.status()})


def ytdlp_lookup(url):
    try:
        info = extract_with_fallback(url)
        via = info.pop('_via', 'direct')
        resp = build_response(info)
        has_anything = (resp['videos'] or resp['original'] or resp['audios']
                        or resp['items'] or resp['images'])
        if not has_anything:
            raise Exception('No downloadable media found')
        return resp, 'yt-dlp (' + via + ')'
    except Exception as e:  # noqa: BLE001
        low = clean(e).lower()
        if ('unsupported url' in low or 'no video' in low or 'no media' in low
                or 'no downloadable' in low or 'not a video' in low):
            media = gallery_media(url)
            if media:
                return media, 'gallery-dl'
        raise


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

    cfg = sync_admin()
    t0 = time.time()
    ip = adminlink.hash_ip(client_ip())
    host = (urlparse(url).hostname or '').replace('www.', '')
    yt = is_youtube(url)
    keys = apify_keys() if yt else []
    mode = (cfg.get('youtube_mode') or 'ytdlp_first') if keys else 'ytdlp_only'
    order = ['ytdlp']
    if yt:
        order = {'apify_first': ['apify', 'ytdlp'], 'ytdlp_first': ['ytdlp', 'apify'],
                 'apify_only': ['apify'], 'ytdlp_only': ['ytdlp']}.get(mode, ['ytdlp'])

    errors = []
    for prov in order:
        try:
            if prov == 'apify':
                resp, k = apify_extract(url, keys, cfg.get('apify_actor') or APIFY_ACTOR_DEFAULT)
                via = f"apify ({k.get('name')})"
            else:
                resp, via = ytdlp_lookup(url)
            adminlink.event(kind='info', ok=True, platform=host, provider=via, reason='ok',
                            url=short_url(url), ip=ip, ms=int((time.time() - t0) * 1000))
            return jsonify(resp)
        except Exception as e:  # noqa: BLE001
            errors.append(f'{prov}: {clean(e)}')

    detail = ' || '.join(errors) + ' ' + status_tag()
    msg = friendly(detail)
    adminlink.event(kind='info', ok=False, platform=host, provider=' > '.join(order),
                    reason=classify(detail), detail=detail, user_msg=msg,
                    url=short_url(url), ip=ip, ms=int((time.time() - t0) * 1000))
    return jsonify({'success': False, 'message': msg}), 400


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

        for px, cs, imp in attempts_for(url):
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

            o = base_opts(px, impersonate=imp)
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
                # Quality first: YouTube's best video is usually VP9/AV1 in webm.
                # Forcing mp4 made yt-dlp fall back to a tiny combined stream
                # (a 50MB "Original" downloaded as ~5MB). Let the container
                # follow the codec (mkv/webm) so the FULL quality is delivered.
                #
                # Resolution buttons keep the "1080p / 720p MP4" promise, but the
                # "Original (best quality)" button downloads the true best stream.
                o['format_sort'] = ['res', 'vcodec', 'tbr']
                if height:
                    o['format'] = (f'bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b')
                    o['merge_output_format'] = 'mp4'
                    o['format_sort'] = ['res', 'ext:mp4:m4a']
                else:
                    o['format'] = 'bv*+ba/b'
                    o['merge_output_format'] = 'mkv'
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
                if not should_retry(e, imp):
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
        adminlink.event(kind='download_done', ok=True, platform=(urlparse(p['url']).hostname or '').replace('www.', ''),
                        provider=f"job:{p['kind']}", reason='ok', url=short_url(p['url']),
                        ip=p.get('ip'), size=os.path.getsize(path))
    except Exception as e:  # noqa: BLE001
        set_job(job, status='error', message=friendly(e))
        adminlink.event(kind='download_done', ok=False, platform=(urlparse(p['url']).hostname or '').replace('www.', ''),
                        provider=f"job:{p['kind']}", reason=classify(e), detail=clean(e)[:400],
                        user_msg=friendly(e), url=short_url(p['url']), ip=p.get('ip'))
    finally:
        SLOTS.release()


@app.route('/api/jobs', methods=['POST'])
def create_job():
    sync_admin()
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
        'ip': adminlink.hash_ip(client_ip()),
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
    adminlink.event(kind='download', ok=True, platform=(urlparse(url).hostname or '').replace('www.', ''),
                    provider=f'job:{kind}', reason='started', url=short_url(url), ip=params['ip'])
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
    host = (urlparse(u).hostname or '')
    if (r is None or r.status_code in (401, 403)) and host.endswith('apify.com'):
        for k in apify_keys():
            try:
                r = requests.get(u, stream=True, timeout=30,
                                 headers={'Authorization': 'Bearer ' + k['value'], 'User-Agent': UA})
                if r.status_code == 200:
                    break
            except requests.RequestException:
                r = None
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
