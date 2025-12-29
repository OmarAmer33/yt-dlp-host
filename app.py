from flask import Flask, request, jsonify
import yt_dlp
import base64
import os
import re

app = Flask(__name__)

# Optional: Simple API key check
API_KEY = os.environ.get("API_KEY", "")

def check_api_key():
    if not API_KEY:
        return True  # No key configured = allow all
    key = request.headers.get("X-API-Key", "")
    return key == API_KEY

def extract_video_id(url):
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([^&\n?#]+)',
        r'youtube\.com\/shorts\/([^&\n?#]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

@app.route("/download", methods=["GET", "POST"])
def download():
    # Check API key
    if not check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    # Get YouTube URL from query param (GET) or JSON body (POST)
    if request.method == "GET":
        youtube_url = request.args.get("url")
    else:
        data = request.get_json(silent=True) or {}
        youtube_url = data.get("url") or data.get("youtubeUrl")

    if not youtube_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    try:
        # Configure yt-dlp to download audio only
        output_path = f"/tmp/{video_id}.%(ext)s"
        
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_path,
            "quiet": True,
            "no_warnings": True,
            "extractaudio": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
        }

        # Download and extract info
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            title = info.get("title", "Unknown")
            duration = info.get("duration")

        # Find the downloaded file (will be .mp3 after conversion)
        audio_file = f"/tmp/{video_id}.mp3"
        
        if not os.path.exists(audio_file):
            # Fallback: check for other extensions
            for ext in ["m4a", "webm", "opus", "ogg"]:
                alt_path = f"/tmp/{video_id}.{ext}"
                if os.path.exists(alt_path):
                    audio_file = alt_path
                    break

        if not os.path.exists(audio_file):
            return jsonify({"error": "Audio file not found after download"}), 500

        # Read file and encode as base64
        with open(audio_file, "rb") as f:
            file_bytes = f.read()
        
        file_base64 = base64.b64encode(file_bytes).decode("utf-8")
        file_size = len(file_bytes)

        # Determine content type
        ext = os.path.splitext(audio_file)[1].lower()
        content_types = {
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".webm": "audio/webm",
            ".opus": "audio/opus",
            ".ogg": "audio/ogg",
        }
        content_type = content_types.get(ext, "audio/mpeg")

        # Clean up temp file
        try:
            os.remove(audio_file)
        except:
            pass

        # Return the response your backend expects
        return jsonify({
            "title": title,
            "videoId": video_id,
            "duration": duration,
            "contentType": content_type,
            "fileBase64": file_base64,
            "fileSize": file_size
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
