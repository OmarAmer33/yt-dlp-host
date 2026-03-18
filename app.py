import base64
import hashlib
import hmac
import os
import re
import tempfile
import threading
import time
import uuid
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
YTDLP_PROXY_URL = os.getenv("YTDLP_PROXY_URL", "").strip()
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
CLOUDINARY_CHUNK_SIZE = 95 * 1024 * 1024  # 95 MB per chunk

# Cookie expiry tracking — populated by ensure_cookie_file()
_cookie_expires_at: Optional[float] = None  # Unix timestamp of earliest YouTube cookie expiry
_cookie_expires_lock = threading.Lock()

# ---------------------------------------------------------------------------
# In-memory async job store
# Jobs are kept for JOB_TTL_SECONDS after completion then eligible for cleanup.
# A background thread prunes stale jobs every JOB_PRUNE_INTERVAL_SECONDS.
# ---------------------------------------------------------------------------
JOB_TTL_SECONDS = 3600            # 1 hour retention after completion
JOB_PRUNE_INTERVAL_SECONDS = 300  # prune every 5 minutes

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _new_job(youtube_url: str) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "youtube_url": youtube_url,
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "error": None,
            "error_code": None,
        }
    return job_id


def _update_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
            _jobs[job_id]["updated_at"] = time.time()


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _prune_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with _jobs_lock:
        stale = [
            jid for jid, job in _jobs.items()
            if job["status"] in ("completed", "failed") and job["updated_at"] < cutoff
        ]
        for jid in stale:
            del _jobs[jid]


def _prune_loop() -> None:
    while True:
        time.sleep(JOB_PRUNE_INTERVAL_SECONDS)
        try:
            _prune_jobs()
        except Exception:
            pass


threading.Thread(target=_prune_loop, daemon=True, name="job-pruner").start()


# ---------------------------------------------------------------------------
# Core error type
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def classify_ytdlp_error(message: str) -> Tuple[str, str, int]:
    text = (message or "").strip().lower()

    if any(
        signal in text
        for signal in [
            "sign in to confirm you're not a bot",
            "confirm you're not a bot",
            "not a bot",
            "sign in to confirm",
            "bot check",
            "anti-bot",
        ]
    ):
        return ("anti_bot", "YouTube blocked the request with a sign-in or anti-bot check.", 403)

    if "cookie" in text and any(
        signal in text for signal in ["expired", "stale", "invalid", "bad cookie", "cookie is no longer valid"]
    ):
        return ("cookies_stale", "Configured YouTube cookies appear stale, expired, or invalid.", 403)

    if "cookie" in text and any(
        signal in text for signal in ["missing", "not configured", "required", "use --cookies", "cookies-from-browser"]
    ):
        return ("cookies_missing", "YouTube cookies are required but missing or not configured.", 500)

    if any(signal in text for signal in ["private video", "video unavailable", "unavailable", "members-only"]):
        return ("video_unavailable", "The YouTube video is unavailable or restricted.", 404)

    if any(signal in text for signal in ["timed out", "timeout", "connection reset", "network is unreachable"]):
        return ("provider_timeout", "yt-dlp timed out while contacting YouTube.", 504)

    return ("provider_error", message or "yt-dlp failed unexpectedly.", 500)


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


def _parse_earliest_cookie_expiry(cookie_content: str) -> Optional[float]:
    """Parse Netscape cookie file and return the earliest expiry timestamp
    among YouTube-relevant cookies. Returns None if no expiry found."""
    earliest: Optional[float] = None
    for line in cookie_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0]
        if "youtube.com" not in domain:
            continue
        try:
            expiry = float(parts[4])
            if expiry > 0:
                if earliest is None or expiry < earliest:
                    earliest = expiry
        except (ValueError, IndexError):
            continue
    return earliest


def ensure_cookie_file() -> Path:
    global _cookie_expires_at

    encoded = os.getenv(COOKIE_ENV_VAR, "").strip()
    if not encoded:
        raise AppError("cookies_missing", f"YouTube cookies are missing. Set {COOKIE_ENV_VAR}.", 500)

    try:
        decoded = base64.b64decode(encoded).decode("utf-8", errors="strict")
    except Exception as exc:
        raise AppError("cookies_stale", f"Could not decode {COOKIE_ENV_VAR}: {exc}", 500) from exc

    content = decoded.strip()
    if not content:
        raise AppError("cookies_missing", f"{COOKIE_ENV_VAR} decoded to an empty cookie file.", 500)

    if (
        "# Netscape HTTP Cookie File" not in content
        and "\tyoutube.com\t" not in content
        and "\t.youtube.com\t" not in content
    ):
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

    # Parse and cache cookie expiry for health reporting
    with _cookie_expires_lock:
        _cookie_expires_at = _parse_earliest_cookie_expiry(decoded)

    return COOKIE_FILE_PATH


def build_ydl_opts(
    download: bool,
    output_template: Optional[str] = None,
    use_proxy: bool = False,
) -> Dict[str, Any]:
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

    if use_proxy and YTDLP_PROXY_URL:
        opts["proxy"] = YTDLP_PROXY_URL

    return opts


def sanitize_title(title: str) -> str:
    safe = re.sub(r"[^\w\-. ]+", "", title or "youtube_video").strip()
    return safe[:120] or "youtube_video"


def get_video_info(youtube_url: str) -> Dict[str, Any]:
    last_exc = None
    for use_proxy in (False, True):
        if use_proxy and not YTDLP_PROXY_URL:
            break
        try:
            with YoutubeDL(build_ydl_opts(download=False, use_proxy=use_proxy)) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
                if not info:
                    raise AppError("provider_error", "yt-dlp returned no video metadata.", 500)
                return info
        except AppError:
            raise
        except DownloadError as exc:
            code, message, status = classify_ytdlp_error(str(exc))
            if code == "anti_bot" and not use_proxy and YTDLP_PROXY_URL:
                last_exc = exc
                continue
            raise AppError(code, message, status) from exc
        except Exception as exc:
            code, message, status = classify_ytdlp_error(str(exc))
            if code == "anti_bot" and not use_proxy and YTDLP_PROXY_URL:
                last_exc = exc
                continue
            raise AppError(code, message, status) from exc

    code, message, status = classify_ytdlp_error(str(last_exc))
    raise AppError(code, message, status) from last_exc


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
    last_exc = None
    for use_proxy in (False, True):
        if use_proxy and not YTDLP_PROXY_URL:
            break
        try:
            with tempfile.TemporaryDirectory(prefix="yt_") as tmpdir:
                tmp_path = Path(tmpdir)
                outtmpl = str(tmp_path / "%(id)s.%(ext)s")

                with YoutubeDL(
                    build_ydl_opts(download=True, output_template=outtmpl, use_proxy=use_proxy)
                ) as ydl:
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

        except AppError as exc:
            if exc.code == "anti_bot" and not use_proxy and YTDLP_PROXY_URL:
                last_exc = exc
                continue
            raise
        except DownloadError as exc:
            code, message, status = classify_ytdlp_error(str(exc))
            if code == "anti_bot" and not use_proxy and YTDLP_PROXY_URL:
                last_exc = exc
                continue
            raise AppError(code, message, status) from exc
        except Exception as exc:
            code, message, status = classify_ytdlp_error(str(exc))
            if code == "anti_bot" and not use_proxy and YTDLP_PROXY_URL:
                last_exc = exc
                continue
            raise AppError(code, message, status) from exc

    if last_exc:
        if isinstance(last_exc, AppError):
            raise last_exc
        code, message, status = classify_ytdlp_error(str(last_exc))
        raise AppError(code, message, status) from last_exc
    raise AppError("provider_error", "yt-dlp failed without a classified exception.", 500)


# ---------------------------------------------------------------------------
# Cloudinary helpers
# ---------------------------------------------------------------------------

def require_cloudinary_config() -> Tuple[str, str, str, str]:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    folder = os.getenv("CLOUDINARY_FOLDER", DEFAULT_CLOUDINARY_FOLDER)
    if not cloud_name or not api_key or not api_secret:
        raise AppError("cloudinary_not_configured", "Cloudinary credentials are not configured.", 500)
    return cloud_name, api_key, api_secret, folder


def sign_cloudinary_params(params: Dict[str, Any], api_secret: str) -> str:
    serial = "&".join(
        f"{key}={value}"
        for key, value in sorted(params.items())
        if value is not None and value != ""
    )
    return hashlib.sha1(f"{serial}{api_secret}".encode("utf-8")).hexdigest()


def _build_cloudinary_result(data: Dict[str, Any], cloud_name: str) -> Dict[str, str]:
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


def _upload_single(
    file_path: Path, public_id_hint: str,
    cloud_name: str, api_key: str, api_secret: str, folder: str,
) -> Dict[str, str]:
    timestamp = int(time.time())
    public_id = f"{folder}/{public_id_hint}"
    sign_params = {"folder": folder, "public_id": public_id, "timestamp": timestamp}
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
    return _build_cloudinary_result(response.json(), cloud_name)


def _upload_chunked(
    file_path: Path, public_id_hint: str,
    cloud_name: str, api_key: str, api_secret: str, folder: str, file_size: int,
) -> Dict[str, str]:
    upload_id = uuid.uuid4().hex
    timestamp = int(time.time())
    public_id = f"{folder}/{public_id_hint}"
    sign_params = {"folder": folder, "public_id": public_id, "timestamp": timestamp}
    signature = sign_cloudinary_params(sign_params, api_secret)
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"

    offset = 0
    last_response_data = None
    with file_path.open("rb") as handle:
        while offset < file_size:
            chunk = handle.read(CLOUDINARY_CHUNK_SIZE)
            if not chunk:
                break
            end = offset + len(chunk) - 1
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
                files={"file": ("video.mp4", chunk, "video/mp4")},
                headers={
                    "X-Unique-Upload-Id": upload_id,
                    "Content-Range": f"bytes {offset}-{end}/{file_size}",
                },
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            if response.status_code not in (200, 201):
                raise AppError(
                    "cloudinary_upload_failed",
                    f"Cloudinary chunked upload failed at offset {offset}: {response.text[:500]}",
                    502,
                )
            offset += len(chunk)
            last_response_data = response.json()

    if not last_response_data:
        raise AppError("cloudinary_upload_failed", "Chunked upload produced no response data.", 502)
    return _build_cloudinary_result(last_response_data, cloud_name)


def upload_to_cloudinary(file_path: Path, public_id_hint: str) -> Dict[str, str]:
    cloud_name, api_key, api_secret, folder = require_cloudinary_config()
    file_size = file_path.stat().st_size
    if file_size <= CLOUDINARY_CHUNK_SIZE:
        return _upload_single(file_path, public_id_hint, cloud_name, api_key, api_secret, folder)
    return _upload_chunked(file_path, public_id_hint, cloud_name, api_key, api_secret, folder, file_size)


# ---------------------------------------------------------------------------
# Async job worker
# ---------------------------------------------------------------------------

def _run_job(job_id: str, youtube_url: str) -> None:
    """Runs in a background thread. Downloads video and uploads to Cloudinary,
    updating job state throughout."""
    downloaded_file = None
    try:
        _update_job(job_id, status="downloading")
        downloaded_file, info = download_video(youtube_url)

        title = info.get("title") or "YouTube Video"
        video_id = info.get("id") or extract_video_id(youtube_url) or f"yt_{int(time.time())}"
        public_id_hint = f"{video_id}-{sanitize_title(title)}"

        _update_job(job_id, status="uploading")
        uploaded = upload_to_cloudinary(downloaded_file, public_id_hint)

        _update_job(
            job_id,
            status="completed",
            result={
                "provider": "yt-dlp-cloudinary-direct",
                "title": title,
                "video_id": video_id,
                "cloudinary_public_id": uploaded["cloudinary_public_id"],
                "cloudinary_url": uploaded["cloudinary_url"],
                "reframed_url": uploaded["reframed_url"],
                "thumbnail_url": uploaded["thumbnail_url"],
                "format": DEFAULT_FORMAT,
                "merge_output_format": "mp4",
            },
        )

    except AppError as exc:
        _update_job(job_id, status="failed", error=exc.message, error_code=exc.code)
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc), error_code="internal_error")
    finally:
        if downloaded_file and downloaded_file.exists():
            try:
                downloaded_file.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(AppError)
def handle_app_error(error: AppError):
    return error.to_response()


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    return jsonify({
        "success": False,
        "code": "internal_error",
        "error": f"internal_error: {str(error)}",
        "message": str(error),
    }), 500


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    cookie_status = "ok"
    cookie_path = None
    try:
        cookie_path = str(ensure_cookie_file())
    except AppError as exc:
        cookie_status = exc.code

    with _cookie_expires_lock:
        expires_at = _cookie_expires_at

    now = time.time()
    cookie_days_remaining: Optional[float] = None
    cookie_expiry_warning = False
    if expires_at is not None:
        seconds_remaining = expires_at - now
        cookie_days_remaining = round(seconds_remaining / 86400, 1)
        cookie_expiry_warning = seconds_remaining < (7 * 86400)

    with _jobs_lock:
        active_jobs = sum(1 for j in _jobs.values() if j["status"] in ("pending", "downloading", "uploading"))
        total_jobs = len(_jobs)

    return jsonify({
        "success": True,
        "status": "ok",
        "service": "yt-dlp-uploader",
        "auth_configured": bool(get_service_api_key()),
        "cookie_env_present": bool(os.getenv(COOKIE_ENV_VAR, "").strip()),
        "cookie_status": cookie_status,
        "cookie_file": cookie_path,
        "cookie_expires_at": expires_at,
        "cookie_days_remaining": cookie_days_remaining,
        "cookie_expiry_warning": cookie_expiry_warning,
        "proxy_configured": bool(YTDLP_PROXY_URL),
        "cloudinary_configured": all([
            os.getenv("CLOUDINARY_CLOUD_NAME"),
            os.getenv("CLOUDINARY_API_KEY"),
            os.getenv("CLOUDINARY_API_SECRET"),
        ]),
        "format": DEFAULT_FORMAT,
        "merge_output_format": "mp4",
        "active_jobs": active_jobs,
        "total_jobs": total_jobs,
    })


@app.route("/get-url", methods=["GET", "POST"])
def get_url():
    require_bearer_auth()
    youtube_url = get_youtube_url_from_request()
    validate_youtube_url(youtube_url)
    info = get_video_info(youtube_url)
    video_url = get_best_direct_url(info)
    if not video_url:
        raise AppError("provider_error", "No usable direct video URL found.", 500)
    return jsonify({
        "success": True,
        "provider": "yt-dlp",
        "title": info.get("title") or "YouTube Video",
        "video_id": info.get("id") or extract_video_id(youtube_url),
        "video_url": video_url,
        "format": DEFAULT_FORMAT,
        "merge_output_format": "mp4",
    })


@app.post("/download-and-upload")
def download_and_upload():
    """Synchronous download + upload. Suitable for short videos only.
    For long videos use /start-job + /job-status/<job_id> instead."""
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
        return jsonify({
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
        })
    finally:
        if downloaded_file and downloaded_file.exists():
            try:
                downloaded_file.unlink()
            except Exception:
                pass


@app.post("/start-job")
def start_job():
    """Async entry point. Immediately returns a job_id.
    Poll GET /job-status/<job_id> for progress and results."""
    require_bearer_auth()
    youtube_url = get_youtube_url_from_request()
    validate_youtube_url(youtube_url)

    job_id = _new_job(youtube_url)
    t = threading.Thread(
        target=_run_job,
        args=(job_id, youtube_url),
        daemon=True,
        name=f"job-{job_id[:8]}",
    )
    t.start()

    return jsonify({
        "success": True,
        "job_id": job_id,
        "status": "pending",
    }), 202


@app.get("/job-status/<job_id>")
def job_status(job_id: str):
    """Poll this endpoint after /start-job.
    Returns status: pending | downloading | uploading | completed | failed.
    On completed, result fields are flattened to the top level.
    On failed, error and error_code describe the failure."""
    require_bearer_auth()
    job = _get_job(job_id)
    if not job:
        raise AppError("not_found", f"Job {job_id} not found.", 404)

    response: Dict[str, Any] = {
        "success": True,
        "job_id": job_id,
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }

    if job["status"] == "completed" and job["result"]:
        response.update(job["result"])
    elif job["status"] == "failed":
        response["error"] = job["error"]
        response["error_code"] = job["error_code"]

    return jsonify(response)


@app.post("/refresh-cookies")
def refresh_cookies():
    require_bearer_auth()
    cookie_file = ensure_cookie_file()
    line_count = len(cookie_file.read_text(encoding="utf-8").splitlines())

    with _cookie_expires_lock:
        expires_at = _cookie_expires_at

    now = time.time()
    days_remaining: Optional[float] = None
    if expires_at is not None:
        days_remaining = round((expires_at - now) / 86400, 1)

    return jsonify({
        "success": True,
        "message": "Cookie file refreshed from YTDLP_COOKIES_B64.",
        "cookie_file": str(cookie_file),
        "line_count": line_count,
        "cookie_env_var": COOKIE_ENV_VAR,
        "cookie_expires_at": expires_at,
        "cookie_days_remaining": days_remaining,
    })


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "service": "yt-dlp-uploader"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
