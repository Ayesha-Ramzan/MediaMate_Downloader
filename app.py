import os
import uuid
import asyncio
import subprocess
import json
import platform
import shutil
import re
import logging
import mimetypes
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone

import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─────────────────────────────────────────────
#  LOGGING
#  Silent `except: pass` blocks hide real failures. This gives you
#  actual visibility into per-item failures (e.g. image_worker skips)
#  without crashing the batch job that contains them.
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mediamate")

# ─────────────────────────────────────────────
#  APP SETUP
# ─────────────────────────────────────────────
app = FastAPI(title="Video Downloader API", version="2.1.1")

# ── CORS ──────────────────────────────────────
# Add your Vercel URL(s) here. Wildcards are not
# allowed with credentials, so list them explicitly.
ALLOWED_ORIGINS = [
    "https://my-video-dow-app.vercel.app",   # ← replace with your actual Vercel URL
    "http://localhost:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)

# ─────────────────────────────────────────────
#  DIRECTORIES
#  On HF Spaces the home dir is /root or /home/user.
#  We use /tmp for safety — it's always writable.
# ─────────────────────────────────────────────
BASE_DIR      = Path("/tmp/Downloader")
DOWNLOADS_DIR = BASE_DIR / "Downloads"   # everything (video/audio/images/converted) lands here
UPLOADS_DIR   = BASE_DIR / "Uploads"     # temp storage for files the user uploads (not shown to user)

# Kept as aliases so nothing downstream needs to guess which constant to use —
# they all point at the same single folder now, no more splitting by type.
VIDEOS_DIR    = DOWNLOADS_DIR
MUSIC_DIR     = DOWNLOADS_DIR
IMAGES_DIR    = DOWNLOADS_DIR
CONVERTED_DIR = DOWNLOADS_DIR

for d in [DOWNLOADS_DIR, UPLOADS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────
#  IN-MEMORY JOB STORE
#  No persistence layer here — jobs live only as long as the process does,
#  by design (this is a single-instance local/HF-Spaces app, not a
#  multi-worker deployment). But unbounded growth within that lifetime is
#  still a real leak, so we sweep terminal (done/error/cancelled) jobs
#  that are older than JOB_TTL_SECONDS, and hard-cap total job count.
# ─────────────────────────────────────────────
jobs: dict = {}

JOB_TTL_SECONDS = 2 * 60 * 60   # drop finished jobs after 2 hours
MAX_JOBS        = 200           # hard ceiling regardless of age

TERMINAL_STATUSES = {"done", "error", "cancelled"}


def cleanup_old_jobs() -> None:
    """Evict stale terminal jobs. Called opportunistically on job creation
    rather than on a background timer — simplest option for an in-memory
    dict with no persistence, and cheap enough (O(n) over a capped n)."""
    now = datetime.now(timezone.utc)
    stale_ids = []

    for jid, job in jobs.items():
        if job.get("status") not in TERMINAL_STATUSES:
            continue
        created_raw = job.get("created_at")
        if not created_raw:
            continue
        try:
            created = datetime.fromisoformat(created_raw)
        except ValueError:
            continue
        if (now - created).total_seconds() > JOB_TTL_SECONDS:
            stale_ids.append(jid)

    for jid in stale_ids:
        jobs.pop(jid, None)

    if len(jobs) > MAX_JOBS:
        overflow = len(jobs) - MAX_JOBS
        oldest_terminal = sorted(
            (j for j in jobs.values() if j.get("status") in TERMINAL_STATUSES),
            key=lambda j: j.get("created_at", ""),
        )
        for j in oldest_terminal[:overflow]:
            jobs.pop(j["id"], None)


# ─────────────────────────────────────────────
#  SUPPORTED SOURCES (informational only — yt-dlp
#  auto-detects the right extractor from the URL,
#  so no special-casing is needed anywhere else in
#  the app. This list is just used for the friendly
#  "/api/info" response and docs.)
#  Includes: YouTube, TikTok, Instagram, Twitter/X,
#  Facebook, Vimeo, LinkedIn (public post videos only).
# ─────────────────────────────────────────────
SUPPORTED_SOURCES = [
    "youtube", "tiktok", "instagram", "twitter", "facebook", "vimeo", "linkedin",
]

# Substrings yt-dlp tends to surface in DownloadError messages when a video
# sits behind a login wall or a private/restricted feed. LinkedIn's extractor
# only supports public post videos, so private/login-gated LinkedIn videos
# will typically trip one of these.
LOGIN_REQUIRED_HINTS = [
    "login required",
    "requires login",
    "log in",
    "sign in",
    "private video",
    "private profile",
    "authentication",
    "cookies",
    "this post is not available",
    "not available on this app",
]


def is_login_required_error(error_message: str) -> bool:
    msg = (error_message or "").lower()
    return any(hint in msg for hint in LOGIN_REQUIRED_HINTS)


def friendly_extractor_error(url: str, error_message: str) -> str:
    """
    Turn a raw yt-dlp error into a clearer message for the user, with a
    LinkedIn-specific hint since its extractor only covers public posts.
    """
    if is_login_required_error(error_message):
        if "linkedin" in url.lower():
            return ("This LinkedIn video is private or requires login and "
                    "can't be downloaded.")
        return "This video is private or requires login and can't be downloaded."
    return f"Could not process this URL: {error_message[:300]}"


# ─────────────────────────────────────────────
#  PYDANTIC MODELS
# ─────────────────────────────────────────────
class DownloadRequest(BaseModel):
    urls: List[str]
    tab: str = "video"
    format: str = "mp4"
    quality: str = "best"
    subtitle_lang: str = "none"
    embed_subs: bool = False
    enhance: bool = False
    keep_original: bool = True
    audio_quality: str = "192"


class PlaylistInfoRequest(BaseModel):
    url: str


class PlaylistDownloadRequest(BaseModel):
    urls: List[str]
    playlist_name: str = ""
    format: str = "mp4"
    quality: str = "best"
    subtitle_lang: str = "none"
    skip_downloaded: bool = False


class ExtractImagesRequest(BaseModel):
    url: str


class ImageDownloadRequest(BaseModel):
    urls: List[str]
    format: str = "jpg"
    rename_prefix: str = "image"
    as_zip: bool = True


class VideoInfoRequest(BaseModel):
    url: str


# ─────────────────────────────────────────────
#  JOB HELPERS
# ─────────────────────────────────────────────
def create_job(type_: str = "video") -> str:
    cleanup_old_jobs()
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "type": type_,
        "status": "waiting",
        "progress": 0,
        "filename": "",
        "filepath": "",
        "speed": "",
        "eta": "",
        "filesize": "",
        "error": "",
        "message": "Starting...",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return job_id


def update_job(job_id: str, **kwargs):
    if job_id in jobs:
        jobs[job_id].update(kwargs)


# ─────────────────────────────────────────────
#  YT-DLP PROGRESS HOOK
# ─────────────────────────────────────────────
def make_progress_hook(job_id: str):
    def hook(d):
        if d["status"] == "downloading":
            total      = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            pct        = round((downloaded / total * 100), 1) if total else 0
            update_job(job_id,
                status="downloading",
                progress=pct,
                speed=d.get("_speed_str", ""),
                eta=d.get("_eta_str", ""),
                filesize=d.get("_total_bytes_str", ""),
                filename=Path(d.get("filename", "")).name,
            )
        elif d["status"] == "finished":
            update_job(job_id,
                status="processing",
                progress=95,
                filename=Path(d.get("filename", "")).name,
                filepath=str(d.get("filename", "")),
                speed="",
                eta="",
            )
    return hook


# ─────────────────────────────────────────────
#  BUILD YDL OPTIONS
# ─────────────────────────────────────────────
def build_ydl_opts(job_id: str, out_dir: Path, req) -> dict:
    fmt     = getattr(req, "format",        "mp4")
    quality = getattr(req, "quality",       "best")
    tab     = getattr(req, "tab",           "video")

    quality_map = {
        "4k":    "bestvideo[height<=2160]+bestaudio/best",
        "1080p": "bestvideo[height<=1080]+bestaudio/best",
        "720p":  "bestvideo[height<=720]+bestaudio/best",
        "480p":  "bestvideo[height<=480]+bestaudio/best",
        "360p":  "bestvideo[height<=360]+bestaudio/best",
    }
    format_str = quality_map.get(quality, "bestvideo+bestaudio/best")

    opts = {
        "outtmpl":     str(out_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [make_progress_hook(job_id)],
        "quiet":       True,
        "no_warnings": True,
        "noplaylist":  True,
        # Retries & timeouts — important for HF Spaces networking
        "retries":         5,
        "fragment_retries": 5,
        "socket_timeout":  30,
    }

    audio_formats = {"mp3", "aac", "flac", "wav", "ogg", "m4a", "opus"}

    if tab == "audio" or fmt in audio_formats:
        audio_quality = getattr(req, "audio_quality", "192")
        codec = fmt if fmt in audio_formats else "mp3"
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": audio_quality,
        }]
    else:
        opts["format"]               = format_str
        opts["merge_output_format"]  = fmt
        opts["postprocessors"]       = []

        sub_lang = getattr(req, "subtitle_lang", "none")
        if sub_lang != "none":
            opts["writesubtitles"]  = True
            opts["subtitleslangs"]  = [sub_lang]
            if getattr(req, "embed_subs", False):
                opts["postprocessors"].append({
                    "key": "FFmpegEmbedSubtitle",
                    "already_have_subtitle": False,
                })

    # NOTE: No special-casing is needed here for LinkedIn (or any other
    # source) — yt-dlp auto-detects the correct extractor from the URL.
    # LinkedIn's extractor only supports public post videos; private or
    # login-gated feed videos will raise a DownloadError, which the
    # download worker / video-info endpoint translate into a friendly
    # message below.

    return opts


# ─────────────────────────────────────────────
#  DOWNLOAD WORKER
# ─────────────────────────────────────────────
def download_worker(job_id: str, urls: List[str], out_dir: Path,
                    ydl_opts: dict, enhance: bool = False,
                    keep_original: bool = True):
    try:
        downloaded_files = []

        for url in urls:
            if jobs.get(job_id, {}).get("status") == "cancelled":
                return

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    filename = ydl.prepare_filename(info)
                    downloaded_files.append(filename)

        # ── Optional FFmpeg enhancement ──
        if enhance and downloaded_files:
            for filepath in downloaded_files:
                fp = Path(filepath)
                # yt-dlp sometimes changes the extension after muxing
                for ext in [fp.suffix, ".mp4", ".mkv", ".webm"]:
                    candidate = fp.with_suffix(ext)
                    if candidate.exists():
                        fp = candidate
                        break

                if not fp.exists():
                    continue

                update_job(job_id, status="enhancing", progress=96,
                           message="Enhancing video quality...")

                enhanced_path = fp.parent / (fp.stem + "_enhanced" + fp.suffix)
                cmd = [
                    "ffmpeg", "-y", "-i", str(fp),
                    "-vf", "unsharp=5:5:1.0:5:5:0.0,scale=iw:ih:flags=lanczos",
                    "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                    "-c:a", "copy",
                    str(enhanced_path),
                ]
                subprocess.run(cmd, capture_output=True)

                if not keep_original and enhanced_path.exists():
                    fp.unlink(missing_ok=True)

                filepath = str(enhanced_path) if enhanced_path.exists() else str(fp)
                update_job(job_id, filepath=filepath,
                           filename=Path(filepath).name)

        last_file = downloaded_files[-1] if downloaded_files else ""
        update_job(job_id,
            status="done",
            progress=100,
            filepath=str(last_file),
            filename=Path(last_file).name if last_file else "Done",
        )

    except yt_dlp.utils.DownloadError as e:
        raw = str(e)
        # First URL in the batch is used to give LinkedIn-specific wording
        # when relevant; falls back to a generic message otherwise.
        friendly = friendly_extractor_error(urls[0] if urls else "", raw)
        logger.warning(f"Download failed for job {job_id}: {raw}")
        update_job(job_id, status="error", error=friendly)
    except Exception as e:
        logger.exception(f"Unexpected error in download_worker for job {job_id}")
        update_job(job_id, status="error", error=str(e)[:300])


# ─────────────────────────────────────────────
#  ENHANCE WORKER
# ─────────────────────────────────────────────
def _enhance_worker(job_id: str, input_path: str, out_path: str,
                    level: str, resolution: str, output_format: str,
                    keep_original: bool):
    try:
        # ── Get video duration ──
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True,
        )
        try:
            total_duration = float(probe.stdout.strip())
        except ValueError:
            total_duration = 0

        update_job(job_id, status="enhancing", progress=2,
                   message="Building filter chain...")

        # ── Filter chain ──
        sharpen_map = {
            "light":  "unsharp=3:3:0.5:3:3:0.0",
            "medium": "unsharp=5:5:1.0:5:5:0.0",
            "strong": "unsharp=7:7:1.5:7:7:0.0",
        }
        denoise_map = {
            "light":  "hqdn3d=1:1:3:3",
            "medium": "hqdn3d=2:2:5:5",
            "strong": "hqdn3d=4:4:8:8",
        }
        sharpen = sharpen_map.get(level, sharpen_map["medium"])
        denoise = denoise_map.get(level, denoise_map["medium"])

        scale_map = {
            "720p":  "scale=1280:720:flags=lanczos:force_original_aspect_ratio=decrease,"
                     "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "1080p": "scale=1920:1080:flags=lanczos:force_original_aspect_ratio=decrease,"
                     "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "auto":  "scale='if(lt(iw,1280),iw*2,iw)':'if(lt(ih,720),ih*2,ih)':flags=lanczos",
        }
        filters = [denoise, sharpen]
        if resolution in scale_map:
            filters.append(scale_map[resolution])

        vf = ",".join(filters)

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264",
            "-crf", "17",
            "-preset", "slow",
            "-tune", "film",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            out_path,
        ]

        update_job(job_id, status="enhancing", progress=5,
                   message="FFmpeg processing started...")

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)

        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    ms  = int(line.split("=")[1])
                    cur = ms / 1_000_000.0
                    if total_duration > 0:
                        pct = min(95, round(cur / total_duration * 95, 1))
                        update_job(job_id,
                            status="enhancing",
                            progress=pct,
                            message=f"Enhancing... {pct:.0f}%",
                            speed=f"{cur:.1f}s processed",
                            eta=f"~{max(0, int(total_duration - cur))}s left",
                        )
                except (ValueError, IndexError):
                    pass

        proc.wait()

        if proc.returncode != 0:
            stderr_out = proc.stderr.read() if proc.stderr else ""
            logger.warning(f"FFmpeg enhance failed for job {job_id}: {stderr_out[-300:]}")
            update_job(job_id, status="error",
                       error=stderr_out[-300:] or "FFmpeg error")
            return

        if not keep_original:
            Path(input_path).unlink(missing_ok=True)

        out_file = Path(out_path)
        update_job(job_id,
            status="done",
            progress=100,
            message="Enhancement complete!",
            filename=out_file.name,
            filepath=str(out_file),
            speed="",
            eta="",
        )

    except Exception as e:
        logger.exception(f"Unexpected error in _enhance_worker for job {job_id}")
        update_job(job_id, status="error", error=str(e)[:300])


# ═════════════════════════════════════════════
#  API ENDPOINTS
# ═════════════════════════════════════════════

# ── Health / Info ─────────────────────────────
@app.get("/api/info")
def get_info():
    try:
        result = subprocess.run(["yt-dlp", "--version"],
                                capture_output=True, text=True, timeout=10)
        ytdlp_ver = result.stdout.strip()
    except Exception:
        ytdlp_ver = "unknown"

    return {
        "version":       "2.1.1",
        "ytdlp_version": ytdlp_ver,
        "platform":      platform.system(),
        "base_dir":      str(BASE_DIR),
        "supported_sources": SUPPORTED_SOURCES,
        "status":        "ok",
    }


# ── Video metadata (preview before download) ──
@app.post("/api/video-info")
def video_info(req: VideoInfoRequest):
    """
    Fetch metadata only (title, thumbnail, duration, uploader, id, extractor)
    for a preview card — this never downloads the actual video/audio file.
    Works for any source yt-dlp supports (YouTube, TikTok, Instagram,
    Twitter/X, Facebook, Vimeo, LinkedIn public post videos, etc.) since
    yt-dlp auto-detects the right extractor from the URL.

    `id` + `extractor` are returned so the frontend can decide whether it
    can build a real inline iframe player (currently: YouTube, Vimeo only —
    those are the only two sources with a stable public embed URL pattern
    that needs nothing but the video ID). Everything else falls back to a
    thumbnail-only preview on the frontend.
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL is required")

    ydl_opts = {
        "quiet":         True,
        "no_warnings":   True,
        "skip_download": True,   # belt-and-braces alongside download=False
        "extract_flat":  False,  # we need real metadata, not a flat listing
        "socket_timeout": 15,
        "noplaylist":    True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise HTTPException(400, "Could not extract any information from this URL.")

        return {
            "id":         info.get("id", ""),
            "title":      info.get("title", ""),
            "thumbnail":  info.get("thumbnail", ""),
            "duration":   info.get("duration", 0),
            "uploader":   info.get("uploader", ""),
            "view_count": info.get("view_count", 0),
            "extractor":  info.get("extractor_key") or info.get("extractor", "unknown"),
        }

    except yt_dlp.utils.DownloadError as e:
        detail = friendly_extractor_error(url, str(e))
        logger.warning(f"video_info failed for {url}: {e}")
        raise HTTPException(400, detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in video_info for {url}")
        raise HTTPException(400, f"Could not fetch video info: {str(e)[:300]}")


# ── Start download ────────────────────────────
@app.post("/api/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    if not req.urls:
        raise HTTPException(400, "At least one URL is required")

    job_id  = create_job(req.tab)
    audio_fmts = {"mp3", "aac", "flac", "wav", "ogg", "m4a", "opus"}
    out_dir = MUSIC_DIR if (req.tab == "audio" or req.format in audio_fmts) else VIDEOS_DIR

    update_job(job_id, message="Preparing download...", folder=str(out_dir))
    ydl_opts = build_ydl_opts(job_id, out_dir, req)

    background_tasks.add_task(
        download_worker, job_id, req.urls, out_dir,
        ydl_opts, req.enhance, req.keep_original,
    )
    return {"job_id": job_id, "folder": str(out_dir)}


# ── Serve completed file to browser ──────────
@app.get("/api/download-file/{job_id}")
def download_file(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "done":
        raise HTTPException(400, "Download not finished yet")

    filepath = job.get("filepath", "")
    if not filepath:
        raise HTTPException(404, "No file path recorded")

    file_path = Path(filepath)

    # yt-dlp sometimes changes extension during post-processing
    if not file_path.exists():
        for ext in [".mp4", ".mkv", ".webm", ".mp3", ".m4a"]:
            candidate = file_path.with_suffix(ext)
            if candidate.exists():
                file_path = candidate
                break

    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")
    if not file_path.is_file():
        raise HTTPException(400, "Path is a directory, not a file")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
    )


# ── Stream video inline ───────────────────────
@app.get("/api/stream/{job_id}")
def stream_video(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "done":
        raise HTTPException(400, "Not ready yet")

    filepath  = job.get("filepath", "")
    file_path = Path(filepath)

    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")

    mime, _ = mimetypes.guess_type(str(file_path))
    mime    = mime or "video/mp4"

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
    )


# ── Convert (upload or URL) ───────────────────
@app.post("/api/convert")
async def convert_video(
    background_tasks: BackgroundTasks,
    file:          Optional[UploadFile] = File(None),
    url:           str  = Form(""),
    output_format: str  = Form("mp3"),
    quality:       str  = Form("192"),
    rename:        str  = Form(""),
):
    job_id = create_job("convert")

    input_path: Optional[Path] = None
    if file and file.filename:
        input_path = UPLOADS_DIR / f"{job_id}_{file.filename}"
        with open(input_path, "wb") as f:
            f.write(await file.read())

    def convert_worker():
        try:
            if input_path and input_path.exists():
                stem     = rename.strip() or input_path.stem
                out_file = CONVERTED_DIR / f"{stem}.{output_format}"

                # Avoid overwriting
                counter = 1
                while out_file.exists():
                    out_file = CONVERTED_DIR / f"{stem}_{counter}.{output_format}"
                    counter += 1

                update_job(job_id, status="processing", progress=30,
                           message="Converting...")
                cmd = [
                    "ffmpeg", "-y", "-i", str(input_path),
                    "-b:a", f"{quality}k",
                    str(out_file),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    logger.warning(f"ffmpeg convert failed for job {job_id}: {result.stderr[-300:]}")
                    update_job(job_id, status="error",
                               error=result.stderr[-300:])
                    return

                update_job(job_id, status="done", progress=100,
                           filename=out_file.name, filepath=str(out_file))

            elif url.strip():
                # Real validated model instead of a hand-rolled duck-typed
                # stand-in — build_ydl_opts() reads attributes off `req` via
                # getattr(), so it works with any object shape, but a real
                # DownloadRequest gets Pydantic validation for free instead
                # of silently trusting whatever shape a throwaway class has.
                audio_req = DownloadRequest(
                    urls=[url],
                    format=output_format,
                    quality="best",
                    tab="audio",
                    audio_quality=quality,
                    subtitle_lang="none",
                )
                ydl_opts = build_ydl_opts(job_id, CONVERTED_DIR, audio_req)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                update_job(job_id, status="done", progress=100,
                           message="Conversion complete")

            else:
                update_job(job_id, status="error",
                           error="No file or URL provided")

        except yt_dlp.utils.DownloadError as e:
            friendly = friendly_extractor_error(url, str(e))
            logger.warning(f"Convert download failed for job {job_id}: {e}")
            update_job(job_id, status="error", error=friendly)
        except Exception as e:
            logger.exception(f"Unexpected error in convert_worker for job {job_id}")
            update_job(job_id, status="error", error=str(e)[:300])

    background_tasks.add_task(convert_worker)
    return {"job_id": job_id}


# ── Playlist info ─────────────────────────────
@app.post("/api/playlist-info")
def playlist_info(req: PlaylistInfoRequest):
    if not req.url.strip():
        raise HTTPException(400, "URL is required")
    try:
        opts = {
            "quiet":        True,
            "extract_flat": True,
            "socket_timeout": 20,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info    = ydl.extract_info(req.url, download=False)
            entries = info.get("entries", []) or []
            videos  = [
                {
                    "title":     e.get("title", "Untitled"),
                    "url":       e.get("url") or e.get("webpage_url", ""),
                    "duration":  e.get("duration"),
                    "thumbnail": e.get("thumbnail", ""),
                }
                for e in entries if e
            ]
            return {
                "title":    info.get("title", "Playlist"),
                "uploader": info.get("uploader", ""),
                "count":    len(videos),
                "videos":   videos,
            }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_extractor_error(req.url, str(e)))
    except Exception as e:
        logger.exception(f"Unexpected error in playlist_info for {req.url}")
        raise HTTPException(400, str(e))


# ── Playlist download ─────────────────────────
@app.post("/api/playlist-download")
def playlist_download(req: PlaylistDownloadRequest,
                       background_tasks: BackgroundTasks):
    job_id = create_job("playlist")

    # Real validated model instead of the previous hand-rolled `_Req` class —
    # same reasoning as convert_worker above.
    video_req = DownloadRequest(
        urls=req.urls,
        format=req.format,
        quality=req.quality,
        tab="video",
        subtitle_lang=req.subtitle_lang,
        embed_subs=False,
        audio_quality="192",
    )
    ydl_opts = build_ydl_opts(job_id, VIDEOS_DIR, video_req)
    ydl_opts["noplaylist"] = False

    background_tasks.add_task(
        download_worker, job_id, req.urls, VIDEOS_DIR, ydl_opts,
    )
    return {"job_id": job_id}


# ── Extract image URLs from a webpage ────────
@app.post("/api/extract-images")
def extract_images(req: ExtractImagesRequest):
    if not req.url.strip():
        raise HTTPException(400, "URL is required")
    try:
        from html.parser import HTMLParser

        class _ImageParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.images = []

            def handle_starttag(self, tag, attrs):
                if tag == "img":
                    attr_dict = dict(attrs)
                    src = (attr_dict.get("src") or
                           attr_dict.get("data-src") or
                           attr_dict.get("data-lazy-src") or "")
                    if src.startswith("http"):
                        self.images.append({
                            "url":    src,
                            "alt":    attr_dict.get("alt", ""),
                            "width":  attr_dict.get("width", ""),
                            "height": attr_dict.get("height", ""),
                        })

        req2 = urllib.request.Request(
            req.url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")

        parser = _ImageParser()
        parser.feed(html)
        imgs = parser.images[:100]
        return {"images": imgs, "count": len(imgs)}

    except Exception as e:
        logger.exception(f"Unexpected error in extract_images for {req.url}")
        raise HTTPException(400, str(e))


# ── Download images ───────────────────────────
@app.post("/api/download-images")
def download_images_api(req: ImageDownloadRequest,
                         background_tasks: BackgroundTasks):
    if not req.urls:
        raise HTTPException(400, "No URLs provided")

    job_id = create_job("images")

    def image_worker():
        try:
            downloaded = []
            total      = len(req.urls)

            for i, url in enumerate(req.urls):
                base_name = f"{req.rename_prefix}_{str(i + 1).zfill(3)}"
                dest      = IMAGES_DIR / f"{base_name}.{req.format}"

                counter = 1
                while dest.exists():
                    dest = IMAGES_DIR / f"{base_name}_{counter}.{req.format}"
                    counter += 1

                try:
                    r = urllib.request.Request(
                        url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(r, timeout=15) as resp:
                        dest.write_bytes(resp.read())
                    downloaded.append(str(dest))
                except Exception as e:
                    # Previously a bare `except: pass` — silently swallowed
                    # every per-image failure with zero way to debug a batch
                    # that came back short. Now at least logged, and the
                    # skip is still non-fatal to the rest of the batch.
                    logger.warning(f"Skipped image {url} in job {job_id}: {e}")

                pct = round((i + 1) / total * 90, 1)
                update_job(job_id, status="downloading", progress=pct,
                           message=f"Downloading {i + 1}/{total}")

            if req.as_zip and downloaded:
                zip_name = f"{req.rename_prefix}_images"
                zip_path = IMAGES_DIR / f"{zip_name}.zip"

                counter = 1
                while zip_path.exists():
                    zip_path = IMAGES_DIR / f"{zip_name}_{counter}.zip"
                    counter += 1

                with zipfile.ZipFile(zip_path, "w",
                                     compression=zipfile.ZIP_DEFLATED) as zf:
                    for f in downloaded:
                        zf.write(f, Path(f).name)

                update_job(job_id, status="done", progress=100,
                           filename=zip_path.name, filepath=str(zip_path))
            else:
                update_job(job_id, status="done", progress=100,
                           filename=f"{len(downloaded)} images",
                           filepath=str(IMAGES_DIR))

        except Exception as e:
            logger.exception(f"Unexpected error in image_worker for job {job_id}")
            update_job(job_id, status="error", error=str(e)[:300])

    background_tasks.add_task(image_worker)
    return {"job_id": job_id}


# ── SSE progress stream ───────────────────────
@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    async def event_gen():
        terminal = TERMINAL_STATUSES
        while True:
            job = jobs.get(job_id, {})
            yield f"data: {json.dumps(job)}\n\n"
            if job.get("status") in terminal:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":      "keep-alive",
        },
    )


# ── Cancel job ───────────────────────────────
@app.post("/api/cancel/{job_id}")
def cancel_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    update_job(job_id, status="cancelled")
    return {"ok": True}


# ── Open folder (local only, no-op on HF) ────
@app.get("/api/open-folder")
def open_folder(path: str = ""):
    target = str(path) if path and Path(path).exists() else str(BASE_DIR)
    if Path(target).is_file():
        target = str(Path(target).parent)
    try:
        if platform.system() == "Windows":
            os.startfile(target)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as e:
        logger.warning(f"Could not open folder {target}: {e}")
    return {"ok": True}


# ── Update yt-dlp ─────────────────────────────
@app.post("/api/update-ytdlp")
def update_ytdlp():
    try:
        result = subprocess.run(
            ["pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=120,
        )
        return {
            "message": "yt-dlp updated successfully!",
            "output":  result.stdout[-300:],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Video enhance (upload) ────────────────────
@app.post("/api/enhance")
async def enhance_video(
    background_tasks: BackgroundTasks,
    file:          UploadFile = File(...),
    level:         str = Form("medium"),
    resolution:    str = Form("auto"),
    output_format: str = Form("mp4"),
    keep_original: str = Form("1"),
):
    if not file.filename:
        raise HTTPException(400, "No file provided")

    job_id = create_job("enhance")
    suffix = Path(file.filename).suffix or ".mp4"
    input_path = UPLOADS_DIR / f"{job_id}_input{suffix}"
    input_path.write_bytes(await file.read())

    stem        = Path(file.filename).stem
    out_path    = VIDEOS_DIR / f"{stem}_enhanced.{output_format}"
    counter     = 1
    while out_path.exists():
        out_path = VIDEOS_DIR / f"{stem}_enhanced_{counter}.{output_format}"
        counter += 1

    update_job(job_id, message="Starting enhancement...",
               filename=out_path.name, filepath=str(out_path))

    background_tasks.add_task(
        _enhance_worker, job_id,
        str(input_path), str(out_path),
        level, resolution, output_format, keep_original == "1",
    )
    return {"job_id": job_id}


# ── List all jobs (debug) ─────────────────────
@app.get("/api/jobs")
def list_jobs():
    return {"jobs": list(jobs.values()), "count": len(jobs)}


# ── Delete job record ─────────────────────────
@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    del jobs[job_id]
    return {"ok": True}


# ─────────────────────────────────────────────
#  STATIC FRONTEND (optional — only if bundled)
# ─────────────────────────────────────────────
@app.get("/")
async def serve_frontend():
    return FileResponse(Path(__file__).parent / "frontend" / "index.html")


# ─────────────────────────────────────────────
#  ENTRYPOINT
#  Hugging Face Spaces requires port 7860.
#  Local dev uses 8000.
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)