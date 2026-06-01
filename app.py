import os, uuid, asyncio, subprocess, json, platform, threading, shutil, re
from pathlib import Path
from typing import Optional
import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import queue, time
from datetime import datetime

app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR = Path.home() / "Downloads" / "Downloader"
BASE_DIR.mkdir(parents=True, exist_ok=True)

# ─── FIXED FOLDER STRUCTURE ───
# Videos  → BASE_DIR / Videos        (one folder, always reused)
# Music   → BASE_DIR / Music          (one folder, always reused)
# Images  → BASE_DIR / Images         (one folder, always reused)
# Convert → BASE_DIR / Converted      (one folder, always reused)
# Playlists → BASE_DIR / Videos       (now saving directly to Videos to avoid nested folders)

VIDEOS_DIR   = BASE_DIR / "Videos"
MUSIC_DIR    = BASE_DIR / "Music"
IMAGES_DIR   = BASE_DIR / "Images"
CONVERTED_DIR = BASE_DIR / "Converted"
PLAYLISTS_DIR = BASE_DIR / "Videos"   # Point to VIDEOS_DIR to avoid folders in folders

for d in [VIDEOS_DIR, MUSIC_DIR, IMAGES_DIR, CONVERTED_DIR]:
    d.mkdir(parents=True, exist_ok=True)

jobs = {}  # job_id -> dict

# ─── MODELS ───
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

# ─── HELPERS ───

def create_job(type_="video"):
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
    }
    return job_id

def update_job(job_id, **kwargs):
    if job_id in jobs:
        jobs[job_id].update(kwargs)

# ─── YT-DLP PROGRESS HOOK ───
def make_progress_hook(job_id):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0
            speed = d.get("_speed_str", "")
            eta = d.get("_eta_str", "")
            filesize = d.get("_total_bytes_str", "")
            filename = Path(d.get("filename", "")).name
            update_job(job_id,
                status="downloading",
                progress=round(pct, 1),
                speed=speed,
                eta=eta,
                filesize=filesize,
                filename=filename
            )
        elif d["status"] == "finished":
            filename = Path(d.get("filename", "")).name
            update_job(job_id,
                status="processing",
                progress=95,
                filename=filename,
                filepath=str(d.get("filename", "")),
                speed="",
                eta=""
            )
    return hook

# ─── BUILD YDL OPTS ───
def build_ydl_opts(job_id, out_dir, req):
    fmt = req.format if hasattr(req, "format") else "mp4"
    quality = req.quality if hasattr(req, "quality") else "best"
    tab = req.tab if hasattr(req, "tab") else "video"

    format_str = "bestvideo+bestaudio/best"
    if quality == "4k":
        format_str = "bestvideo[height<=2160]+bestaudio/best"
    elif quality == "1080p":
        format_str = "bestvideo[height<=1080]+bestaudio/best"
    elif quality == "720p":
        format_str = "bestvideo[height<=720]+bestaudio/best"
    elif quality == "480p":
        format_str = "bestvideo[height<=480]+bestaudio/best"
    elif quality == "360p":
        format_str = "bestvideo[height<=360]+bestaudio/best"

    opts = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [make_progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    if tab == "audio" or fmt in ["mp3","aac","flac","wav","ogg","m4a","opus"]:
        audio_quality = getattr(req, "audio_quality", "192")
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt if fmt in ["mp3","aac","flac","wav","ogg","m4a","opus"] else "mp3",
            "preferredquality": audio_quality,
        }]
    else:
        opts["format"] = format_str
        opts["merge_output_format"] = fmt
        opts["postprocessors"] = []

        if getattr(req, "subtitle_lang", "none") != "none":
            lang = req.subtitle_lang
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = [lang]
            if getattr(req, "embed_subs", False):
                opts["postprocessors"].append({
                    "key": "FFmpegEmbedSubtitle",
                    "already_have_subtitle": False,
                })

    return opts

# ─── DOWNLOAD WORKER ───
def download_worker(job_id, urls, out_dir, ydl_opts, enhance=False, keep_original=True):
    try:
        downloaded_files = []
        for url in urls:
            if jobs[job_id].get("status") == "cancelled":
                return
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    filename = ydl.prepare_filename(info)
                    downloaded_files.append(filename)

        if enhance and downloaded_files:
            for filepath in downloaded_files:
                fp = Path(filepath)
                for ext in [fp.suffix, ".mp4", ".mkv", ".webm"]:
                    candidate = fp.with_suffix(ext)
                    if candidate.exists():
                        fp = candidate
                        break
                if fp.exists():
                    update_job(job_id, status="enhancing", progress=96, message="Enhancing video quality...")
                    enhanced_path = fp.parent / (fp.stem + "_enhanced" + fp.suffix)
                    cmd = [
                        "ffmpeg", "-y", "-i", str(fp),
                        "-vf", "unsharp=5:5:1.0:5:5:0.0,scale=iw:ih:flags=lanczos",
                        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                        "-c:a", "copy",
                        str(enhanced_path)
                    ]
                    subprocess.run(cmd, capture_output=True)
                    if not keep_original and enhanced_path.exists():
                        fp.unlink(missing_ok=True)
                    filepath = str(enhanced_path) if enhanced_path.exists() else str(fp)
                    update_job(job_id, filepath=filepath, filename=Path(filepath).name)

        last_file = downloaded_files[-1] if downloaded_files else ""
        update_job(job_id,
            status="done",
            progress=100,
            filepath=str(last_file),
            filename=Path(last_file).name if last_file else "Done"
        )
    except Exception as e:
        update_job(job_id, status="error", error=str(e))

# ─── API ENDPOINTS ───

@app.get("/api/info")
def get_info():
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        ytdlp_ver = result.stdout.strip()
    except:
        ytdlp_ver = "unknown"
    return {
        "version": "1.0.0",
        "ytdlp_version": ytdlp_ver,
        "platform": platform.system(),
        "base_dir": str(BASE_DIR),
    }

@app.post("/api/video-info")
def video_info(req: VideoInfoRequest):
    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(req.url, download=False)
            return {"title": info.get("title",""), "duration": info.get("duration",0), "thumbnail": info.get("thumbnail","")}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = create_job(req.tab)

    # ── FIXED: route to the correct fixed folder based on type ──
    if req.tab == "audio" or req.format in ["mp3","aac","flac","wav","ogg","m4a","opus"]:
        out_dir = MUSIC_DIR          # Music → always goes here
    else:
        out_dir = VIDEOS_DIR         # Video → always goes here (one shared folder)

    update_job(job_id, message="Preparing download...", folder=str(out_dir))
    ydl_opts = build_ydl_opts(job_id, out_dir, req)
    background_tasks.add_task(download_worker, job_id, req.urls, out_dir, ydl_opts, req.enhance, req.keep_original)
    return {"job_id": job_id, "folder": str(out_dir)}

# ── Serve the downloaded file directly to the browser ──
@app.get("/api/download-file/{job_id}")
def download_file(job_id: str):
    """Stream the completed file to the browser (Save dialog)."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "done":
        raise HTTPException(400, "Download not finished yet")
    filepath = job.get("filepath", "")
    if not filepath or not Path(filepath).exists():
        raise HTTPException(404, "File not found on disk")
    file_path = Path(filepath)
    if not file_path.is_file():
        raise HTTPException(400, "Target is a directory and cannot be downloaded directly")
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )

@app.get("/api/stream/{job_id}")
def stream_video(job_id: str):
    """
    Stream the video file so the browser can play it inline.
    Supports HTTP Range requests so the browser seek bar works.
    """
    import mimetypes
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "done":
        raise HTTPException(400, "Not ready yet")
    filepath = job.get("filepath", "")
    if not filepath or not Path(filepath).exists():
        raise HTTPException(404, "File not found on disk")

    file_path = Path(filepath)
    mime, _ = mimetypes.guess_type(str(file_path))
    mime = mime or "video/mp4"

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
    )

@app.post("/api/convert")
async def convert_video(
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    url: str = Form(""),
    output_format: str = Form("mp3"),
    quality: str = Form("192"),
    rename: str = Form("")
):
    job_id = create_job("convert")
    out_dir = CONVERTED_DIR   # Fixed: always use Converted folder
    out_dir.mkdir(exist_ok=True)

    input_path = None
    if file and file.filename:
        input_path = out_dir / file.filename
        with open(input_path, "wb") as f:
            f.write(await file.read())

    def convert_worker():
        try:
            if input_path and input_path.exists():
                stem = rename.strip() or input_path.stem
                out_file = out_dir / f"{stem}.{output_format}"
                cmd = ["ffmpeg", "-y", "-i", str(input_path), "-b:a", f"{quality}k", str(out_file)]
                update_job(job_id, status="processing", progress=30, message="Converting...")
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    update_job(job_id, status="error", error=result.stderr[-200:])
                    return
                update_job(job_id, status="done", progress=100, filename=out_file.name, filepath=str(out_file))
            elif url:
                class Req:
                    format = output_format
                    quality = "best"
                    tab = "audio"
                    audio_quality = quality
                    subtitle_lang = "none"
                ydl_opts = build_ydl_opts(job_id, out_dir, Req())
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                update_job(job_id, status="done", progress=100, message="Conversion complete")
            else:
                update_job(job_id, status="error", error="No input provided")
        except Exception as e:
            update_job(job_id, status="error", error=str(e))

    background_tasks.add_task(convert_worker)
    return {"job_id": job_id}

@app.post("/api/playlist-info")
def playlist_info(req: PlaylistInfoRequest):
    try:
        opts = {
            "quiet": True,
            "extract_flat": True,
            "force_generic_extractor": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            entries = info.get("entries", [])
            videos = []
            for e in entries:
                if e:
                    videos.append({
                        "title": e.get("title","Untitled"),
                        "url": e.get("url") or e.get("webpage_url",""),
                        "duration": e.get("duration"),
                        "thumbnail": e.get("thumbnail",""),
                    })
            return {
                "title": info.get("title","Playlist"),
                "uploader": info.get("uploader",""),
                "count": len(videos),
                "videos": videos,
            }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/playlist-download")
def playlist_download(req: PlaylistDownloadRequest, background_tasks: BackgroundTasks):
    job_id = create_job("playlist")
    # Save playlist videos directly into the Videos folder to avoid nested folders
    out_dir = VIDEOS_DIR

    class FakeReq:
        format = req.format
        quality = req.quality
        tab = "video"
        subtitle_lang = req.subtitle_lang
        embed_subs = False
        audio_quality = "192"

    ydl_opts = build_ydl_opts(job_id, out_dir, FakeReq())
    ydl_opts["noplaylist"] = False  # Enable playlist downloading
    background_tasks.add_task(download_worker, job_id, req.urls, out_dir, ydl_opts)
    return {"job_id": job_id}

@app.post("/api/extract-images")
def extract_images(req: ExtractImagesRequest):
    try:
        import urllib.request
        from html.parser import HTMLParser

        class ImageParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.images = []
            def handle_starttag(self, tag, attrs):
                if tag == "img":
                    attr_dict = dict(attrs)
                    src = attr_dict.get("src","")
                    if src and src.startswith("http"):
                        self.images.append({
                            "url": src,
                            "alt": attr_dict.get("alt",""),
                            "width": attr_dict.get("width",""),
                            "height": attr_dict.get("height",""),
                        })

        req2 = urllib.request.Request(req.url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")

        parser = ImageParser()
        parser.feed(html)
        return {"images": parser.images[:100], "count": len(parser.images[:100])}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/download-images")
def download_images_api(req: ImageDownloadRequest, background_tasks: BackgroundTasks):
    job_id = create_job("images")
    out_dir = IMAGES_DIR   # Fixed: always use Images folder
    out_dir.mkdir(exist_ok=True)

    def image_worker():
        import urllib.request
        try:
            downloaded = []
            total = len(req.urls)
            for i, url in enumerate(req.urls):
                ext = req.format
                base_name = f"{req.rename_prefix}_{str(i+1).zfill(3)}"
                dest = out_dir / f"{base_name}.{ext}"
                # Prevent overwriting existing images
                counter = 1
                while dest.exists():
                    dest = out_dir / f"{base_name}_{counter}.{ext}"
                    counter += 1
                
                try:
                    r = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
                    with urllib.request.urlopen(r, timeout=10) as resp:
                        with open(dest, "wb") as f:
                            f.write(resp.read())
                    downloaded.append(str(dest))
                except:
                    pass
                pct = (i+1)/total*90
                update_job(job_id, status="downloading", progress=round(pct,1), message=f"Downloading {i+1}/{total}")

            if req.as_zip and downloaded:
                import zipfile
                zip_name = f"{req.rename_prefix}_images"
                zip_path = out_dir / f"{zip_name}.zip"
                # Prevent overwriting existing ZIPs
                counter = 1
                while zip_path.exists():
                    zip_path = out_dir / f"{zip_name}_{counter}.zip"
                    counter += 1
                    
                with zipfile.ZipFile(zip_path, "w") as zf:
                    for f in downloaded:
                        zf.write(f, Path(f).name)
                update_job(job_id, status="done", progress=100, filename=zip_path.name, filepath=str(zip_path))
            else:
                update_job(job_id, status="done", progress=100, filename=f"{len(downloaded)} images", filepath=str(out_dir))
        except Exception as e:
            update_job(job_id, status="error", error=str(e))

    background_tasks.add_task(image_worker)
    return {"job_id": job_id}

@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    async def event_gen():
        while True:
            job = jobs.get(job_id, {})
            yield f"data: {json.dumps(job)}\n\n"
            if job.get("status") in ["done", "error", "cancelled"]:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.post("/api/cancel/{job_id}")
def cancel_job(job_id: str):
    if job_id in jobs:
        update_job(job_id, status="cancelled")
        return {"ok": True}
    raise HTTPException(404, "Job not found")

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
    except:
        pass
    return {"ok": True}

@app.post("/api/update-ytdlp")
def update_ytdlp():
    try:
        result = subprocess.run(["pip", "install", "--upgrade", "yt-dlp"], capture_output=True, text=True, timeout=60)
        return {"message": "yt-dlp updated successfully!", "output": result.stdout[-200:]}
    except Exception as e:
        raise HTTPException(500, str(e))

# Serve frontend
frontend_path = Path(__file__).parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="static")