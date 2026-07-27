"""
smed backend — link-paste media extractor.
"""

import os
import re
import tempfile
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="smed API")

# Allow CORS for static frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_HOST_PATTERNS = [
    r"(?:^|\.)youtube\.com$",
    r"^youtu\.be$",
    r"(?:^|\.)tiktok\.com$",
    r"(?:^|\.)instagram\.com$",
    r"(?:^|\.)x\.com$",
    r"(?:^|\.)twitter\.com$",
]

TMP_ROOT = Path(tempfile.gettempdir()) / "smed_jobs"
TMP_ROOT.mkdir(exist_ok=True)

# Common yt-dlp options to bypass YouTube bot/IP blocks on cloud servers (Render)
YDL_COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
        }
    },
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    },
}


class ExtractRequest(BaseModel):
    url: str


def _validate_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "That doesn't look like a valid URL.")
    host_match = re.search(r"^https?://(?:www\.)?([^/]+)", url)
    host = host_match.group(1) if host_match else ""
    if not any(re.search(p, host) for p in ALLOWED_HOST_PATTERNS):
        raise HTTPException(
            400,
            "Unsupported link. smed currently supports YouTube, TikTok, "
            "Instagram, and X (Twitter) links.",
        )
    return url


def _pick_formats(info: dict) -> list[dict]:
    """
    Returns clean, reliable format selections that work across all platforms.
    """
    return [
        {"format_id": "best", "label": "Best Quality Video", "ext": "mp4"},
        {"format_id": "worst", "label": "Fast Download (Low Quality)", "ext": "mp4"},
        {"format_id": "bestaudio", "label": "Audio Only", "ext": "m4a"},
    ]


def _cleanup_dir(path: Path):
    """Background task to remove temp files after response delivery."""
    shutil.rmtree(path, ignore_errors=True)


@app.post("/api/extract")
def extract(req: ExtractRequest):
    url = _validate_url(req.url)

    ydl_opts = {
        **YDL_COMMON_OPTS,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, f"Couldn't read that link: {e}")

    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]

    return {
        "title": info.get("title") or "Untitled",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "source_url": url,
        "formats": _pick_formats(info),
    }


@app.get("/api/download")
def download(
    background_tasks: BackgroundTasks,
    url: str = Query(...),
    format_id: str = Query("best"),
):
    url = _validate_url(url)

    job_id = uuid.uuid4().hex
    job_dir = TMP_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    outtmpl = str(job_dir / "%(title).80s.%(ext)s")

    # Select single-stream formats to avoid FFmpeg dependency
    if format_id == "bestaudio":
        fmt = "bestaudio/best"
    elif format_id == "worst":
        fmt = "worst/worstvideo"
    else:
        fmt = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio/best"

    ydl_opts = {
        **YDL_COMMON_OPTS,
        "outtmpl": outtmpl,
        "format": fmt,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        downloaded_files = list(job_dir.glob("*"))
        if not downloaded_files:
            raise HTTPException(500, "Download finished but output file was not found")

        target = downloaded_files[0]
        ext = target.suffix.lstrip(".")
        media_type = "audio/mp4" if ext in ["m4a", "aac"] else f"video/{ext}"

        # Schedule temp folder deletion after response is sent
        background_tasks.add_task(_cleanup_dir, job_dir)

        return FileResponse(
            path=target,
            media_type=media_type,
            filename=target.name,
            headers={"Content-Disposition": f'attachment; filename="{target.name}"'}
        )

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(422, f"Failed to fetch media: {str(e)}")
