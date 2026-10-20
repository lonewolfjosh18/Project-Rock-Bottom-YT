from flask import (
    Flask, request, jsonify, send_from_directory, Response, stream_with_context
)
from flask_cors import CORS
import os
import re
import time
import base64
import tempfile
import requests
import yt_dlp

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Optional: If your host IP gets challenged by YouTube, export Netscape-format
# cookies from a browser, run: base64 -w0 cookies.txt, and set YT_COOKIES_B64
YT_COOKIES_B64 = os.environ.get('YT_COOKIES_B64', '')
MAX_VIDEO_SECONDS = int(os.environ.get('MAX_VIDEO_SECONDS', '1800'))  # 30 mins

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

COOKIE_FILE = os.path.join(tempfile.gettempdir(), 'yt_cookies.txt')

YOUTUBE_PATTERNS = [
    r"(?:youtube\.com|youtube-nocookie\.com)/watch\?(?:[^#]*&)?v=([A-Za-z0-9_-]{11})",
    r"youtu\.be/([A-Za-z0-9_-]{11})",
    r"youtube\.com/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})",
    r"^([A-Za-z0-9_-]{11})$",  # Plain 11-char ID
]

CACHE_TTL = 600  # 10 minutes
_cache = {}


def cache_get(key):
    item = _cache.get(key)
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]
    _cache.pop(key, None)
    return None


def cache_set(key, data):
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = (time.time(), data)


def public(result):
    """Strip internal fields before returning to client."""
    return {k: v for k, v in result.items() if not k.startswith('_')}


# ---------------------------------------------------------------------------
# yt-dlp Setup
# ---------------------------------------------------------------------------
def write_cookie_file():
    if not YT_COOKIES_B64:
        return None

    try:
        raw = base64.b64decode(YT_COOKIES_B64).decode('utf-8', errors='ignore')
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('#') and not stripped.startswith('#HttpOnly_'):
                continue
            lines.append(stripped)

        if not lines:
            return None

        with open(COOKIE_FILE, 'w') as f:
            f.write('# Netscape HTTP Cookie File\n' + '\n'.join(lines) + '\n')
        return COOKIE_FILE
    except Exception:
        return None


def build_ydl_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'socket_timeout': 15,
        'retries': 2,
        'http_headers': {'User-Agent': USER_AGENT},
        # Request progressive MP4 (audio + video combined, no ffmpeg required)
        'format': 'best[ext=mp4][vcodec!=none][acodec!=none]/best[ext=mp4]/best',
        # Rotate mobile clients to reduce bot verification triggers
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web']
            }
        }
    }
    cookie_file = write_cookie_file()
    if cookie_file:
        opts['cookiefile'] = cookie_file
    return opts


def pick_video(info):
    """Extract direct stream URL and needed HTTP headers."""
    if info.get('url'):
        return info['url'], info.get('http_headers') or {}

    formats = info.get('formats') or []
    progressive = [
        f for f in formats
        if f.get('url') and f.get('vcodec') not in (None, 'none') and f.get('acodec') not in (None, 'none')
    ]
    candidates = progressive or [f for f in formats if f.get('url')]
    if not candidates:
        return None, {}

    mp4 = [f for f in candidates if f.get('ext') == 'mp4']
    chosen = (mp4 or candidates)[-1]
    return chosen['url'], chosen.get('http_headers') or info.get('http_headers') or {}


# ---------------------------------------------------------------------------
# Helpers & Errors
# ---------------------------------------------------------------------------
def extract_video_id(url):
    url = url.strip()
    for pattern in YOUTUBE_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def friendly_error(exc):
    text = str(exc)
    lowered = text.lower()

    if 'sign in to confirm' in lowered or 'not a bot' in lowered or 'confirm your age' in lowered:
        if not YT_COOKIES_B64:
            return "YouTube is blocking datacenter requests. YT_COOKIES_B64 environment variable is required.", 503
        return "YouTube rejected the server's cookies. They may have expired.", 503

    if 'private video' in lowered or 'private' in lowered:
        return "This video is private", 403

    if 'unavailable' in lowered or 'removed' in lowered or 'not found' in lowered or '404' in lowered:
        return "Video not found or unavailable", 404

    if 'live event' in lowered or 'premiere' in lowered or 'is live' in lowered:
        return "Live streams and upcoming premieres cannot be downloaded", 400

    if '429' in text or 'too many' in lowered or ('rate' in lowered and 'limit' in lowered):
        return "YouTube is rate limiting this server right now. Please try again shortly.", 429

    return text, 500


def get_video_info(video_id):
    cached = cache_get(video_id)
    if cached:
        return cached

    page_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        with yt_dlp.YoutubeDL(build_ydl_opts()) as ydl:
            info = ydl.extract_info(page_url, download=False)

        if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
            return {"success": False, "error": "Live streams and upcoming premieres cannot be downloaded", "status": 400}

        duration = info.get('duration')
        if duration and duration > MAX_VIDEO_SECONDS:
            minutes = MAX_VIDEO_SECONDS // 60
            return {"success": False, "error": f"This video exceeds the maximum limit ({minutes} minutes)", "status": 413}

        video_url, stream_headers = pick_video(info)
        if not video_url:
            return {"success": False, "error": "No progressive MP4 format available for this video", "status": 422}

        timestamp = info.get('timestamp')
        iso_time = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(timestamp)) if timestamp else None

        # Clean/sanitize filename title
        raw_title = info.get('title') or "youtube_video"
        safe_title = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()[:80] or "youtube_video"

        headers = dict(stream_headers) or {}
        headers.setdefault('User-Agent', USER_AGENT)

        data = {
            "success": True,
            "video_url": video_url,
            "caption": info.get('title') or "",
            "owner_username": info.get('channel') or info.get('uploader') or "YouTube Creator",
            "thumbnail_url": info.get('thumbnail') or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "likes": info.get('like_count') or 0,
            "comments": info.get('comment_count') or 0,
            "timestamp": iso_time,
            "shortcode": video_id,
            "platform": "youtube",
            "_title": safe_title,
            "_headers": headers,
        }

        cache_set(video_id, data)
        return data

    except Exception as e:
        message, status = friendly_error(e)
        return {"success": False, "error": message, "status": status}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route('/')
def home():
    return send_from_directory('.', 'index.html')


@app.route('/download')
def video_download_page():
    return send_from_directory('.', 'video-download.html')


@app.route('/api/info', methods=['GET'])
def get_info_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid or unsupported YouTube URL"}), 400

    result = get_video_info(video_id)
    if result.get("success"):
        return jsonify(public(result))
    return jsonify({"error": result.get("error")}), result.get("status", 404)


@app.route('/api/download', methods=['GET'])
def download_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid or unsupported YouTube URL"}), 400

    info = get_video_info(video_id)
    if not info.get("success"):
        return jsonify({"error": info.get("error")}), info.get("status", 500)

    try:
        upstream = requests.get(
            info["video_url"],
            headers=info.get("_headers") or {'User-Agent': USER_AGENT},
            stream=True,
            timeout=30,
        )

        if upstream.status_code not in (200, 206):
            upstream.close()
            return jsonify({"error": "Failed to fetch video stream from YouTube"}), 502

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        safe_title = info.get("_title", f"youtube_{video_id}")
        filename = f"{safe_title}_{video_id}.mp4"

        response_headers = {
            'Content-Type': 'video/mp4',
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'no-store',
        }
        if upstream.headers.get('Content-Length'):
            response_headers['Content-Length'] = upstream.headers['Content-Length']

        return Response(stream_with_context(generate()), headers=response_headers)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/thumbnail', methods=['GET'])
def proxy_thumbnail():
    thumbnail_url = request.args.get('url')
    if not thumbnail_url:
        return jsonify({"error": "Missing thumbnail URL"}), 400

    # Whitelist YouTube CDN hosts
    host_ok = re.match(
        r'^https://([A-Za-z0-9.-]+\.)?(ytimg\.com|ggpht\.com|youtube\.com)/',
        thumbnail_url,
    )
    if not host_ok:
        return jsonify({"error": "Invalid thumbnail URL"}), 400

    try:
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        }
        response = requests.get(thumbnail_url, headers=headers, timeout=15)
        if response.status_code == 200:
            return Response(
                response.content,
                mimetype=response.headers.get('content-type', 'image/jpeg'),
            )
        return jsonify({"error": "Failed to fetch thumbnail"}), response.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)