"""
smed backend — link-paste media extractor.

Wraps yt-dlp to (1) inspect a social media URL and list downloadable
formats, and (2) fetch a chosen format and stream it back to the client.

Supported out of the box (via yt-dlp extractors): YouTube, TikTok,
Instagram, X/Twitter. Anything else yt-dlp supports will generally also
work, but is unverified here.

IMPORTANT — read before deploying:
  - Respect each platform's Terms of Service. Most platforms prohibit
    third-party downloading of content you don't own or have rights to.
  - Do not use this to redistribute copyrighted material.
  - Private/login-gated content is out of scope for this prototype (no
    cookie/auth handling is wired up).
  - Platforms change frequently; keep yt-dlp updated (`pip install -U yt-dlp`).
"""

import os
import re
import tempfile
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="smed API")

# Allow the local static frontend (or any origin, for prototype purposes)
# to call this API. Tighten this before deploying publicly.
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

# Jobs live for the duration of the process in this prototype. Swap for
# redis/a DB if you need this to survive restarts or run multi-worker.
TMP_ROOT = Path(tempfile.gettempdir()) / "smed_jobs"
TMP_ROOT.mkdir(exist_ok=True)


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
    Returns a clean set of standard resolution presets instead of raw stream IDs.
    """
    # Standard clean options for every video
    options = [
        {"format_id": "best", "label": "Best Available Quality", "ext": "mp4"},
        {"format_id": "1080p", "label": "1080p Video", "ext": "mp4"},
        {"format_id": "720p", "label": "720p Video", "ext": "mp4"},
        {"format_id": "480p", "label": "480p Video", "ext": "mp4"},
        {"format_id": "audio_only", "label": "Audio Only (MP3)", "ext": "mp3"},
    ]
    return options


@app.post("/api/extract")
def extract(req: ExtractRequest):
    url = _validate_url(req.url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, f"Couldn't read that link: {e}")

    # Some Instagram/TikTok links resolve to a "playlist" of one entry.
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
    url: str = Query(...),
    format_id: str = Query("best"),
):
    url = _validate_url(url)

    job_id = uuid.uuid4().hex
    job_dir = TMP_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    outtmpl = str(job_dir / "%(title).80s.%(ext)s")

    # Map the clean UI choices to yt-dlp format selection strings
    if format_id == "1080p":
        fmt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    elif format_id == "720p":
        fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    elif format_id == "480p":
        fmt = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
    elif format_id == "audio_only":
        fmt = "bestaudio/best"
    else:  # "best"
        fmt = "bestvideo+bestaudio/best"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "format": fmt,
        "merge_output_format": "mp3" if format_id == "audio_only" else "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        downloaded_files = list(job_dir.glob("*"))
        if not downloaded_files:
            raise HTTPException(500, "Download finished but output file was not found")

        target = downloaded_files[0]
        ext = target.suffix.lstrip(".")
        media_type = "audio/mpeg" if ext == "mp3" else f"video/{ext}"

        return FileResponse(
            path=target,
            media_type=media_type,
            filename=target.name,
            headers={"Content-Disposition": f'attachment; filename="{target.name}"'}
        )

    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(422, f"yt-dlp failed to fetch media: {str(e)}")
