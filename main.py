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


def _pick_formats(info):
    """
    Returns a clean list of downloadable formats that works well with
    YouTube, TikTok, Instagram, and X (Twitter).
    """

    formats = info.get("formats", [])
    picked = []
    seen = set()

    for f in formats:
        ext = f.get("ext")

        # Skip formats without an extension
        if not ext:
            continue

        vcodec = f.get("vcodec")
        acodec = f.get("acodec")

        has_video = vcodec not in (None, "none")
        has_audio = acodec not in (None, "none")

        # ---------- VIDEO ----------
        if has_video:
            height = f.get("height") or 0

            label = (
                f"{height}p"
                if height
                else f.get("format_note")
                or f.get("resolution")
                or "Video"
            )

            key = ("video", label, ext)
            if key in seen:
                continue
            seen.add(key)

            picked.append({
                "id": f["format_id"],
                "type": "video",
                "label": label,
                "ext": ext,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "video_only": not has_audio,
            })

        # ---------- AUDIO ----------
        elif has_audio:
            abr = f.get("abr")

            label = (
                f"{int(abr)} kbps"
                if abr
                else "Audio"
            )

            key = ("audio", label, ext)
            if key in seen:
                continue
            seen.add(key)

            picked.append({
                "id": f["format_id"],
                "type": "audio",
                "label": label,
                "ext": ext,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            })

    # Highest quality first
    picked.sort(
        key=lambda x: (
            x["type"] != "video",
            -(int(x["label"].replace("p", "")) if x["type"] == "video" and x["label"].endswith("p") else 0),
        )
    )

    return picked


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

    # If the selected format is video-only, automatically merge with best audio.
    if format_id == "best":
        fmt = "bestvideo+bestaudio/best"
    else:
        fmt = f"{format_id}+bestaudio/{format_id}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "format": fmt,
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(422, f"Download failed: {e}")

    files = list(job_dir.glob("*"))

    if not files:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, "No file was produced.")

    file_path = files[0]

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
    )
