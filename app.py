from flask import Flask, request, jsonify, Response
import yt_dlp
import os
import base64
import io
import tempfile

app = Flask(__name__)

API_KEY = os.environ.get('API_KEY', 'your-secret-key')

def verify_api_key():
    provided_key = request.headers.get('X-API-Key')
    if not provided_key or provided_key != API_KEY:
        return False
    return True

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

@app.route('/download', methods=['GET'])
def download():
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'URL parameter required'}), 400
    
    try:
        # Create a temporary file to download to
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
            tmp_path = tmp_file.name
        
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': tmp_path,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')
            duration = info.get('duration', 0)
        
        # Read the downloaded file
        with open(tmp_path, 'rb') as f:
            file_bytes = f.read()
        
        # Clean up
        os.unlink(tmp_path)
        
        # Encode as base64
        file_base64 = base64.b64encode(file_bytes).decode('utf-8')
        
        return jsonify({
            'title': title,
            'duration': duration,
            'fileBase64': file_base64,
            'fileSize': len(file_bytes),
            'contentType': 'audio/mp4'
        })
        
    except Exception as e:
        # Clean up temp file if it exists
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
