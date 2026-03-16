import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

COOKIE_ENV_VAR = "YTDLP_COOKIES_B64"
COOKIE_FILE_PATH = Path("/tmp/youtube-cookies.txt")
DEFAULT_FORMAT = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "youtube-imports")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass
class AppError(Exception):
    code: str
    message: str
    status_code: int = 400

    def to_response(self):
        return jsonify(
            {
                "success": False,
                "code": self.code,
                "error": f"{self.code}: {self.message}",
                "message": self.message,
            }
        ), self.status_code


def get_service_api_key() -> Optional[str]:
    return os.getenv("API_KEY") or os.getenv("YTDLP_API_KEY")


def require_bearer_auth() -> None:
    expected = get_service_api_key()
    if not expected:
        raise AppError(
            "service_not_configured",
            "Server auth is not configured. Set API_KEY or YTDLP_API_KEY.",
            500,
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise AppError("unauthorized", "Bearer authorization required.", 401)

    provided = auth_header.split(" ", 1)[1].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise AppError("unauthorized", "Invalid Bearer token.", 401)


def parse_json_body() -> Dict[str, Any]:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return {}


def get_youtube_url_from_request() -> str:
    payload = parse_json_body()
    youtube_url = (
        payload.get("youtube_url")
        or payload.get("youtubeUrl")
        or request.args.get("youtube_url")
        or request.args.get("youtubeUrl")
        or request.args.get("url")
    )
    if not youtube_url or not isinstance(youtube_url, str):
        raise AppError("bad_request", "youtube_url is required.", 400)
    youtube_url = youtube_url.strip()
    if not youtube_url:
        raise AppError("bad_request", "youtube_url is required.", 400)
    return youtube_url


def classify_ytdlp_error(message: str) -> Tuple[str, str, int]:
    text = (message or "").strip().lower()

    if any(
        signal in text
        for signal in [
            "sign in to confirm you're not a bot",
            "confirm you’re not a bot",
            "not a bot",
            "sign in to confirm",
            "bot check",
            "anti-bot",
        ]
    ):
        return (
            "anti_bot",
            "YouTube blocked the request with a sign-in or anti-bot check.",
            403,
        )

    if "cookie" in text and any(
        signal in text for signal in ["expired", "stale", "invalid", "bad cookie", "cookie is no longer valid"]
    ):
        return (
            "cookies_stale",
            "Configured YouTube cookies appear stale, expired, or invalid.",
            403,
        )

    if "cookie" in text and any(
        signal in text for signal in ["missing", "not configured", "required", "use --cookies", "cookies-from-browser"]
    ):
        return (
            "cookies_missing",
            "YouTube cookies are required but missing or not configured.",
            500,
        )

    if any(signal in text for signal in ["private video", "video unavailable", "unavailable", "members-only"]):
        return (
            "video_unavailable",
            "The YouTube video is unavailable or restricted.",
            404,
        )

    if any(signal in text for signal in ["timed out", "timeout", "connection reset", "network is unreachable"]):
        return (
            "provider_timeout",
            "yt-dlp timed out while contacting YouTube.",
            504,
        )

    return (
        "provider_error",
        message or "yt-dlp failed unexpectedly.",
        500,
    )


def validate_youtube_url(youtube_url: str) -> None:
    parsed = urlparse(youtube_url)
    if parsed.scheme not in {"http", "https"}:
        raise AppError("bad_request", "youtube_url must be an http(s) URL.", 400)
    host = (parsed.netloc or "").lower()
    if not any(domain in host for domain in ["youtube.com", "youtu.be"]):
        raise AppError("bad_request", "Only YouTube URLs are supported.", 400)


def extract_video_id(youtube_url: str) -> Optional[str]:
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([^&\n?#]+)",
        r"youtube\.com/shorts/([^&\n?#]+)",
        r"youtube\.com/live/([^&\n?#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, youtube_url)
        if match:
            return match.group(1)
    return None


def ensure_cookie_file() -> Path:
    encoded = os.getenv(COOKIE_ENV_VAR, "").strip()
    if not encoded:
        raise AppError(
            "cookies_missing",
            f"YouTube cookies are missing. Set {COOKIE_ENV_VAR}.",
            500,
        )

    try:
        decoded = base64.b64decode(encoded).decode("utf-8", errors="strict")
    except Exception as exc:
        raise AppError(
            "cookies_stale",
            f"Could not decode {COOKIE_ENV_VAR}: {exc}",
            500,
        ) from exc

    content = decoded.strip()
    if not content:
        raise AppError(
            "cookies_missing",
            f"{COOKIE_ENV_VAR} decoded to an empty cookie file.",
            500,
        )

    if "# Netscape HTTP Cookie File" not in content and "\tyoutube.com\t" not in content and "\t.youtube.com\t" not in content:
        raise AppError(
            "cookies_stale",
            "Decoded cookie file does not look like a valid Netscape cookie export for YouTube.",
            500,
        )

    COOKIE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE_PATH.write_text(decoded, encoding="utf-8")
    try:
        os.chmod(COOKIE_FILE_PATH, 0o600)
    except Exception:
        pass

    return COOKIE_FILE_PATH


def build_ydl_opts(download: bool, output_template: Optional[str] = None) -> Dict[str, Any]:
    cookie_file = ensure_cookie_file()
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cookiefile": str(cookie_file),
        "format": DEFAULT_FORMAT,
        "merge_output_format": "mp4",
        "socket_timeout": 60,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "http_headers": {"User-Agent": USER_AGENT},
        "concurrent_fragment_downloads": 1,
    }

    if download:
        opts["outtmpl"] = output_template
    else:
        opts["skip_download"] = True

    return opts


def sanitize_title(title: str) -> str:
    safe = re.sub(r"[^\w\-. ]+", "", title or "youtube_video").strip()
    return safe[:120] or "youtube_video"


def get_video_info(youtube_url: str) -> Dict[str, Any]:
    try:
        with YoutubeDL(build_ydl_opts(download=False)) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            if not info:
                raise AppError("provider_error", "yt-dlp returned no video metadata.", 500)
            return info
    except AppError:
        raise
    except DownloadError as exc:
        code, message, status = classify_ytdlp_error(str(exc))
        raise AppError(code, message, status) from exc
    except Exception as exc:
        code, message, status = classify_ytdlp_error(str(exc))
        raise AppError(code, message, status) from exc


def get_best_direct_url(info: Dict[str, Any]) -> Optional[str]:
    candidates = []

    direct_url = info.get("url")
    if isinstance(direct_url, str) and direct_url.startswith("http"):
        candidates.append(direct_url)

    for fmt in info.get("formats") or []:
        fmt_url = fmt.get("url")
        if isinstance(fmt_url, str) and fmt_url.startswith("http"):
            height = fmt.get("height") or 0
            ext = fmt.get("ext") or ""
            has_audio = fmt.get("acodec") not in (None, "none")
            score = (1 if has_audio else 0, 1 if ext == "mp4" else 0, height)
            candidates.append((score, fmt_url))

    scored = [c for c in candidates if isinstance(c, tuple)]
    if scored:
        scored.sort(reverse=True)
        return scored[0][1]

    plain = [c for c in candidates if isinstance(c, str)]
    return plain[0] if plain else None


def download_video(youtube_url: str) -> Tuple[Path, Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="yt_") as tmpdir:
        tmp_path = Path(tmpdir)
        outtmpl = str(tmp_path / "%(id)s.%(ext)s")
        info = None

        try:
            with YoutubeDL(build_ydl_opts(download=True, output_template=outtmpl)) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                if not info:
                    raise AppError("provider_error", "yt-dlp returned no download metadata.", 500)

                requested_downloads = info.get("requested_downloads") or []
                filepath = None

                for item in requested_downloads:
                    candidate = item.get("filepath") or item.get("_filename")
                    if candidate and Path(candidate).exists():
                        filepath = Path(candidate)
                        break

                if not filepath:
                    candidate = info.get("_filename")
                    if candidate and Path(candidate).exists():
                        filepath = Path(candidate)

                if not filepath:
                    video_id = info.get("id")
                    matches = list(tmp_path.glob(f"{video_id}.*")) if video_id else []
                    if matches:
                        mp4_matches = [p for p in matches if p.suffix.lower() == ".mp4"]
                        filepath = mp4_matches[0] if mp4_matches else matches[0]

                if not filepath or not filepath.exists():
                    raise AppError("provider_error", "Downloaded file could not be located.", 500)

                final_path = Path("/tmp") / filepath.name
                final_path.write_bytes(filepath.read_bytes())
                return final_path, info

        except AppError:
            raise
        except DownloadError as exc:
            code, message, status = classify_ytdlp_error(str(exc))
            raise AppError(code, message, status) from exc
        except Exception as exc:
            code, message, status = classify_ytdlp_error(str(exc))
            raise AppError(code, message, status) from exc


def require_cloudinary_config() -> Tuple[str, str, str, str]:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    folder = os.getenv("CLOUDINARY_FOLDER", DEFAULT_CLOUDINARY_FOLDER)

    if not cloud_name or not api_key or not api_secret:
        raise AppError(
            "cloudinary_not_configured",
            "Cloudinary credentials are not configured.",
            500,
        )

    return cloud_name, api_key, api_secret, folder


def sign_cloudinary_params(params: Dict[str, Any], api_secret: str) -> str:
    serial = "&".join(
        f"{key}={value}"
        for key, value in sorted(params.items())
        if value is not None and value != ""
    )
    return hashlib.sha1(f"{serial}{api_secret}".encode("utf-8")).hexdigest()


def upload_to_cloudinary(file_path: Path, public_id_hint: str) -> Dict[str, str]:
    cloud_name, api_key, api_secret, folder = require_cloudinary_config()
    timestamp = int(time.time())
    public_id = f"{folder}/{public_id_hint}"

    sign_params = {
        "folder": folder,
        "public_id": public_id,
        "timestamp": timestamp,
    }
    signature = sign_cloudinary_params(sign_params, api_secret)

    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"
    with file_path.open("rb") as handle:
        response = requests.post(
            url,
            data={
                "api_key": api_key,
                "timestamp": timestamp,
                "folder": folder,
                "public_id": public_id,
                "signature": signature,
                "resource_type": "video",
            },
            files={"file": handle},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )

    if response.status_code >= 400:
        raise AppError(
            "cloudinary_upload_failed",
            f"Cloudinary upload failed: {response.text[:500]}",
            502,
        )

    data = response.json()
    uploaded_public_id = data["public_id"]
    secure_url = data["secure_url"]

    reframed_url = (
        f"https://res.cloudinary.com/{cloud_name}/video/upload/"
        f"c_fill,ar_9:16,g_auto,w_1080,h_1920/q_auto:good,f_mp4/"
        f"{uploaded_public_id}.mp4"
    )
    thumbnail_url = (
        f"https://res.cloudinary.com/{cloud_name}/video/upload/"
        f"so_auto,f_jpg,q_auto,w_720/{uploaded_public_id}.jpg"
    )

    return {
        "cloudinary_public_id": uploaded_public_id,
        "cloudinary_url": secure_url,
        "reframed_url": reframed_url,
        "thumbnail_url": thumbnail_url,
    }


@app.errorhandler(AppError)
def handle_app_error(error: AppError):
    return error.to_response()


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    return jsonify(
        {
            "success": False,
            "code": "internal_error",
            "error": f"internal_error: {str(error)}",
            "message": str(error),
        }
    ), 500


@app.get("/health")
def health():
    cookie_status = "ok"
    cookie_path = None

    try:
        cookie_path = str(ensure_cookie_file())
    except AppError as exc:
        cookie_status = exc.code

    return jsonify(
        {
            "success": True,
            "status": "ok",
            "service": "yt-dlp-uploader",
            "auth_configured": bool(get_service_api_key()),
            "cookie_env_present": bool(os.getenv(COOKIE_ENV_VAR, "").strip()),
            "cookie_status": cookie_status,
            "cookie_file": cookie_path,
            "cloudinary_configured": all(
                [
                    os.getenv("CLOUDINARY_CLOUD_NAME"),
                    os.getenv("CLOUDINARY_API_KEY"),
                    os.getenv("CLOUDINARY_API_SECRET"),
                ]
            ),
            "format": DEFAULT_FORMAT,
            "merge_output_format": "mp4",
        }
    )


@app.route("/get-url", methods=["GET", "POST"])
def get_url():
    require_bearer_auth()
    youtube_url = get_youtube_url_from_request()
    validate_youtube_url(youtube_url)

    info = get_video_info(youtube_url)
    video_url = get_best_direct_url(info)
    if not video_url:
        raise AppError("provider_error", "No usable direct video URL found.", 500)

    return jsonify(
        {
            "success": True,
            "provider": "yt-dlp",
            "title": info.get("title") or "YouTube Video",
            "video_id": info.get("id") or extract_video_id(youtube_url),
            "video_url": video_url,
            "format": DEFAULT_FORMAT,
            "merge_output_format": "mp4",
        }
    )


@app.post("/download-and-upload")
def download_and_upload():
    require_bearer_auth()
    youtube_url = get_youtube_url_from_request()
    validate_youtube_url(youtube_url)

    downloaded_file = None
    try:
        downloaded_file, info = download_video(youtube_url)
        title = info.get("title") or "YouTube Video"
        video_id = info.get("id") or extract_video_id(youtube_url) or f"yt_{int(time.time())}"
        public_id_hint = f"{video_id}-{sanitize_title(title)}"

        uploaded = upload_to_cloudinary(downloaded_file, public_id_hint)

        return jsonify(
            {
                "success": True,
                "provider": "yt-dlp-cloudinary-direct",
                "title": title,
                "video_id": video_id,
                "cloudinary_public_id": uploaded["cloudinary_public_id"],
                "cloudinary_url": uploaded["cloudinary_url"],
                "reframed_url": uploaded["reframed_url"],
                "thumbnail_url": uploaded["thumbnail_url"],
                "format": DEFAULT_FORMAT,
                "merge_output_format": "mp4",
            }
        )
    finally:
        if downloaded_file and downloaded_file.exists():
            try:
                downloaded_file.unlink()
            except Exception:
                pass


@app.post("/refresh-cookies")
def refresh_cookies():
    require_bearer_auth()
    cookie_file = ensure_cookie_file()
    line_count = len(cookie_file.read_text(encoding="utf-8").splitlines())

    return jsonify(
        {
            "success": True,
            "message": "Cookie file refreshed from YTDLP_COOKIES_B64.",
            "cookie_file": str(cookie_file),
            "line_count": line_count,
            "cookie_env_var": COOKIE_ENV_VAR,
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
