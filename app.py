from flask import Flask, request, jsonify
import yt_dlp
import base64
import os

app = Flask(__name__)

@app.route('/download', methods=['POST'])
def download_video():
    data = request.get_json()
    video_id = data.get('videoId')
    
    if not video_id:
        return jsonify({'error': 'videoId is required'}), 400
    
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f'/tmp/{video_id}.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://youtube.com/watch?v={video_id}', download=True)
            title = info.get('title', 'Unknown')
            filename = ydl.prepare_filename(info)
        
        # Read and encode as base64
        with open(filename, 'rb') as f:
            file_data = base64.b64encode(f.read()).decode('utf-8')
        
        # Clean up temp file
        os.remove(filename)
        
        return jsonify({
            'title': title,
            'videoId': video_id,
            'fileBase64': file_data
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
