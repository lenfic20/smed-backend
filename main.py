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
    """Collapse yt-dlp's raw format list into a small, user-facing menu."""
    formats = info.get("formats") or []
    out = []

    # Progressive (video+audio in one file) formats first — simplest for users.
    seen_labels = set()
    for f in formats:
        if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none"):
            height = f.get("height")
            label = f"{height}p" if height else f.get("format_note", f.get("format_id"))
            if label in seen_labels:
                continue
            seen_labels.add(label)
            out.append({
                "format_id": f["format_id"],
                "label": f"Video · {label} · {f.get('ext')}",
                "ext": f.get("ext"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "kind": "video",
            })

    # Best audio-only option, useful for "just the audio" downloads.
    audio_only = [
        f for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    if audio_only:
        best_audio = max(audio_only, key=lambda f: f.get("abr") or 0)
        out.append({
            "format_id": best_audio["format_id"],
            "label": f"Audio only · {best_audio.get('ext')}",
            "ext": best_audio.get("ext"),
            "filesize": best_audio.get("filesize") or best_audio.get("filesize_approx"),
            "kind": "audio",
        })

    # If yt-dlp didn't report per-format details (some Instagram/TikTok
    # posts), fall back to a single "best" option.
    if not out:
        out.append({
            "format_id": "best",
            "label": "Best available",
            "ext": info.get("ext", "mp4"),
            "filesize": None,
            "kind": "video",
        })

    return out


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
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "format": format_id if format_id != "best" else "best",
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
        background=None,  # left simple for prototype; see README re: cleanup
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}
