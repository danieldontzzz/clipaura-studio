
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse
import sqlite3
import subprocess
import sys
import shutil

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
EXPORTS = DATA / "exports"

for folder in (DATA, UPLOADS, EXPORTS):
    folder.mkdir(parents=True, exist_ok=True)

DB = DATA / "clipaura.sqlite3"

app = FastAPI(title="ClipAura Studio", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                creator TEXT NOT NULL,
                vod_url TEXT NOT NULL,
                notes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready_to_submit',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                title TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                duration REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending_review',
                file_path TEXT DEFAULT '',
                export_path TEXT DEFAULT '',
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
        """)

        columns = [
            row["name"]
            for row in c.execute(
                "PRAGMA table_info(jobs)"
            ).fetchall()
        ]

        if "vod_path" not in columns:
            c.execute(
                "ALTER TABLE jobs ADD COLUMN vod_path TEXT DEFAULT ''"
            )


init_db()


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": "ClipAura Studio",
        "version": "2.1.0"
    }


@app.get("/api/jobs")
def list_jobs():
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ).fetchall()

    return [dict(row) for row in rows]


@app.post("/api/jobs")
def create_job(
    creator: str = Form(...),
    vod_url: str = Form(...),
    notes: str = Form("")
):
    parsed = urlparse(vod_url.strip())

    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise HTTPException(
            400,
            "Enter a valid VOD link beginning with http:// or https://"
        )

    if not creator.strip():
        raise HTTPException(400, "Enter the creator name")

    job_id = str(uuid4())

    with conn() as c:
        c.execute(
            """
            INSERT INTO jobs(id, creator, vod_url, notes)
            VALUES(?,?,?,?)
            """,
            (
                job_id,
                creator.strip(),
                vod_url.strip(),
                notes.strip()
            )
        )

    return {
        "id": job_id,
        "creator": creator.strip(),
        "vod_url": vod_url.strip(),
        "status": "ready_to_submit"
    }


@app.post("/api/jobs/{job_id}/submitted")
def mark_submitted(job_id: str):
    with conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status='submitted_to_clipsgg' WHERE id=?",
            (job_id,)
        )

        if not cur.rowcount:
            raise HTTPException(404, "Job not found")

    return {"ok": True}


@app.post("/api/jobs/{job_id}/download")
def download_vod(job_id: str):
    with conn() as c:
        job = c.execute(
            "SELECT * FROM jobs WHERE id=?",
            (job_id,)
        ).fetchone()

    if not job:
        raise HTTPException(404, "Job not found")

    parsed = urlparse(job["vod_url"])
    host = (parsed.hostname or "").lower()

    allowed_hosts = (
        host == "twitch.tv"
        or host.endswith(".twitch.tv")
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
        or host == "kick.com"
        or host.endswith(".kick.com")
    )

    if parsed.scheme != "https" or not allowed_hosts:
        raise HTTPException(
            400,
            "Use a supported HTTPS Twitch, YouTube or Kick VOD link"
        )

    # Return an existing successful download instead of downloading again.
    existing_path = job["vod_path"] or ""

    if existing_path and Path(existing_path).is_file():
        return {
            "ok": True,
            "status": "vod_downloaded",
            "filename": Path(existing_path).name
        }

    output_template = str(UPLOADS / f"{job_id}_vod.%(ext)s")

    # Give each download its own temporary directory.
    temp_dir = UPLOADS / f"{job_id}_temp"

    # Remove stale temporary files from a previous failed attempt.
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)

    temp_dir.mkdir(parents=True, exist_ok=True)

    # Remove incomplete output files from an earlier attempt.
    for old_file in UPLOADS.glob(f"{job_id}_vod.*"):
        if old_file.is_file():
            old_file.unlink(missing_ok=True)

    # Do not force "-f best". yt-dlp chooses the best available
    # Twitch formats and merges them using FFmpeg.
    command = [
        sys.executable,
        "-m",
        "yt_dlp",

        "--no-playlist",
        "--max-filesize", "4G",

        "--merge-output-format", "mp4",

        # Twitch fragment reliability settings.
        "--concurrent-fragments", "1",
        "--fragment-retries", "20",
        "--retries", "10",
        "--file-access-retries", "10",
        "--retry-sleep", "fragment:exp=1:20",

        # Avoid resuming broken partial fragment downloads.
        "--no-continue",

        # Keep temporary fragments separate from final output.
        "--paths", f"temp:{temp_dir}",

        "-o", output_template,
        job["vod_url"]
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3600
        )

    except subprocess.TimeoutExpired:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(
            504,
            "Twitch VOD download timed out after 60 minutes"
        )

    except FileNotFoundError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(
            503,
            "Python or yt-dlp could not be started"
        )

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "Unknown download error")

        shutil.rmtree(temp_dir, ignore_errors=True)

        raise HTTPException(
            500,
            "VOD download failed: " + error_text[-2000:]
        )

    downloaded = [
        path for path in UPLOADS.glob(f"{job_id}_vod.*")
        if path.is_file()
        and path.suffix.lower() in {
            ".mp4", ".mkv", ".webm", ".mov"
        }
    ]

    if not downloaded:
        shutil.rmtree(temp_dir, ignore_errors=True)

        raise HTTPException(
            500,
            "Download completed but no video file was found"
        )

    vod_path = str(downloaded[0])

    # Remove temporary fragments after a successful download.
    shutil.rmtree(temp_dir, ignore_errors=True)

    with conn() as c:
        c.execute(
            """
            UPDATE jobs
            SET vod_path=?, status='vod_downloaded'
            WHERE id=?
            """,
            (vod_path, job_id)
        )

    return {
        "ok": True,
        "status": "vod_downloaded",
        "filename": downloaded[0].name
    }


@app.post("/api/jobs/{job_id}/clips")
def add_clip(
    job_id: str,
    title: str = Form(""),
    source_url: str = Form(""),
    score: int = Form(0),
    duration: float = Form(0)
):
    with conn() as c:
        job = c.execute(
            "SELECT id FROM jobs WHERE id=?",
            (job_id,)
        ).fetchone()

        if not job:
            raise HTTPException(404, "Job not found")

        clip_id = str(uuid4())

        c.execute(
            """
            INSERT INTO clips(
                id, job_id, title, source_url, score, duration
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                clip_id,
                job_id,
                title,
                source_url,
                max(0, min(100, score)),
                max(0, duration)
            )
        )

    return {
        "id": clip_id,
        "status": "pending_review"
    }


@app.get("/api/clips")
def list_clips():
    with conn() as c:
        rows = c.execute("""
            SELECT clips.*, jobs.creator
            FROM clips
            JOIN jobs ON jobs.id=clips.job_id
            ORDER BY clips.score DESC, clips.rowid DESC
        """).fetchall()

    return [dict(row) for row in rows]


@app.post("/api/clips/{clip_id}/approve")
def approve_clip(clip_id: str):
    with conn() as c:
        cur = c.execute(
            "UPDATE clips SET status='approved' WHERE id=?",
            (clip_id,)
        )

        if not cur.rowcount:
            raise HTTPException(404, "Clip not found")

    return {"ok": True, "status": "approved"}


@app.post("/api/clips/{clip_id}/reject")
def reject_clip(clip_id: str):
    with conn() as c:
        cur = c.execute(
            "UPDATE clips SET status='rejected' WHERE id=?",
            (clip_id,)
        )

        if not cur.rowcount:
            raise HTTPException(404, "Clip not found")

    return {"ok": True, "status": "rejected"}


@app.post("/api/clips/{clip_id}/upload")
async def upload_clip(
    clip_id: str,
    video: UploadFile = File(...)
):
    extension = Path(video.filename or "clip.mp4").suffix.lower()

    if extension not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(
            400,
            "Upload MP4, MOV, MKV or WEBM"
        )

    with conn() as c:
        exists = c.execute(
            "SELECT id FROM clips WHERE id=?",
            (clip_id,)
        ).fetchone()

    if not exists:
        raise HTTPException(404, "Clip not found")

    path = UPLOADS / f"{clip_id}{extension}"
    path.write_bytes(await video.read())

    with conn() as c:
        c.execute(
            "UPDATE clips SET file_path=? WHERE id=?",
            (str(path), clip_id)
        )

    return {
        "ok": True,
        "filename": path.name
    }


@app.post("/api/clips/{clip_id}/render")
def render_clip(
    clip_id: str,
    start: float = Form(0),
    end: float = Form(18),
    layout: str = Form("full_frame")
):
    with conn() as c:
        clip = c.execute(
            "SELECT * FROM clips WHERE id=?",
            (clip_id,)
        ).fetchone()

    if not clip:
        raise HTTPException(404, "Clip not found")

    if clip["status"] != "approved":
        raise HTTPException(
            409,
            "Approve this clip before rendering"
        )

    if not clip["file_path"] or not Path(clip["file_path"]).exists():
        raise HTTPException(
            400,
            "Upload the clip file first"
        )

    if start < 0 or end <= start or end - start > 90:
        raise HTTPException(
            400,
            "Choose a valid range up to 90 seconds"
        )

    if layout not in {"full_frame", "stacked_source"}:
        raise HTTPException(400, "Unsupported layout")

    output = EXPORTS / f"{clip_id}_clipaura.mp4"

    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,fps=30,format=yuv420p"
    )

    command = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", clip["file_path"],
        "-t", str(end - start),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output)
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900
        )

    except FileNotFoundError:
        raise HTTPException(
            503,
            "FFmpeg is not installed on this server"
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Render timed out")

    if result.returncode != 0:
        raise HTTPException(
            500,
            "FFmpeg render failed: " + result.stderr[-1200:]
        )

    with conn() as c:
        c.execute(
            """
            UPDATE clips
            SET export_path=?, status='rendered'
            WHERE id=?
            """,
            (str(output), clip_id)
        )

    return {
        "ok": True,
        "download": f"/api/exports/{output.name}"
    }


@app.get("/api/exports/{filename}")
def download_export(filename: str):
    path = EXPORTS / Path(filename).name

    if not path.exists():
        raise HTTPException(404, "Export not found")

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=path.name
    )


app.mount(
    "/",
    StaticFiles(
        directory=Path(__file__).parent / "static",
        html=True
    ),
    name="static"
)
