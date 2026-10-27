from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import re
import os

app = Flask(__name__)
CORS(app)  # Allow requests from InfinityFree domain

def is_valid_url(url):
    try:
        return url.startswith('http://') or url.startswith('https://')
    except:
        return False

def get_video_info(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'noplaylist': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        raise Exception(str(e))

def format_filesize(size):
    if not size:
        return ''
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

@app.route('/')
def home():
    return jsonify({
        'status': 'online',
        'message': 'YTDown Backend is running',
        'version': '1.0'
    })

@app.route('/api/info', methods=['POST', 'GET'])
def video_info():
    if request.method == 'GET':
        url = request.args.get('url', '').strip()
    else:
        data = request.get_json(silent=True) or {}
        url = data.get('url') or request.form.get('url', '')
        url = url.strip()

    if not url:
        return jsonify({'success': False, 'message': 'No URL provided'}), 400

    if not is_valid_url(url):
        return jsonify({'success': False, 'message': 'Invalid URL'}), 400

    try:
        info = get_video_info(url)

        title = info.get('title', 'Unknown Title')
        thumbnail = info.get('thumbnail', '')
        duration = info.get('duration')
        duration_str = ''
        if duration:
            mins, secs = divmod(int(duration), 60)
            hours, mins = divmod(mins, 60)
            if hours:
                duration_str = f"{hours}:{mins:02d}:{secs:02d}"
            else:
                duration_str = f"{mins}:{secs:02d}"

        formats = []
        seen = set()

        # Process available formats
        for f in info.get('formats', []):
            format_id = f.get('format_id', '')
            ext = f.get('ext', 'mp4')
            height = f.get('height')
            width = f.get('width')
            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')
            filesize = f.get('filesize') or f.get('filesize_approx')
            fps = f.get('fps')
            url_direct = f.get('url')

            if not url_direct:
                continue

            # Video formats (with video codec)
            if vcodec != 'none' and height:
                quality = f"{height}p"
                if fps and fps > 30:
                    quality += f"{int(fps)}"
                
                key = f"video_{quality}_{ext}"
                if key not in seen:
                    seen.add(key)
                    formats.append({
                        'quality': quality,
                        'ext': ext,
                        'type': 'video',
                        'url': url_direct,
                        'size': format_filesize(filesize) if filesize else '',
                        'format_id': format_id,
                        'has_audio': acodec != 'none'
                    })

            # Audio only
            elif acodec != 'none' and vcodec == 'none':
                abr = f.get('abr') or f.get('tbr') or 128
                quality = f"MP3 {int(abr)}kbps" if ext in ['m4a', 'webm', 'opus'] else f"Audio {int(abr)}kbps"
                key = f"audio_{int(abr)}_{ext}"
                if key not in seen:
                    seen.add(key)
                    formats.append({
                        'quality': quality,
                        'ext': 'mp3' if ext in ['m4a', 'webm', 'opus'] else ext,
                        'type': 'audio',
                        'url': url_direct,
                        'size': format_filesize(filesize) if filesize else '',
                        'format_id': format_id,
                        'has_audio': True
                    })

        # Sort: videos by height desc, then audio
        def sort_key(x):
            if x['type'] == 'video':
                try:
                    h = int(re.search(r'(\d+)', x['quality']).group(1))
                    return (0, -h)
                except:
                    return (0, 0)
            else:
                return (1, 0)

        formats = sorted(formats, key=sort_key)

        # Keep top quality options (limit to avoid huge response)
        video_formats = [f for f in formats if f['type'] == 'video'][:8]
        audio_formats = [f for f in formats if f['type'] == 'audio'][:4]
        final_formats = video_formats + audio_formats

        # If no formats found, try best
        if not final_formats:
            best_url = info.get('url')
            if best_url:
                final_formats.append({
                    'quality': 'Best',
                    'ext': info.get('ext', 'mp4'),
                    'type': 'video',
                    'url': best_url,
                    'size': '',
                    'has_audio': True
                })

        return jsonify({
            'success': True,
            'title': title,
            'thumbnail': thumbnail,
            'duration': duration_str,
            'formats': final_formats,
            'platform': info.get('extractor', 'unknown')
        })

    except Exception as e:
        error_msg = str(e)
        # Clean common yt-dlp errors
        if 'Unsupported URL' in error_msg:
            error_msg = 'This website is not supported'
        elif 'Private video' in error_msg:
            error_msg = 'This video is private'
        elif 'Video unavailable' in error_msg:
            error_msg = 'Video unavailable'
        
        return jsonify({
            'success': False,
            'message': error_msg
        }), 400


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
