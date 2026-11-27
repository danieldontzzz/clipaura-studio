from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse
import sqlite3
import math
import array
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

app = FastAPI(title="ClipAura Studio", version="3.0.0")

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

        job_columns = {
            row["name"] for row in
            c.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "vod_path" not in job_columns:
            c.execute("ALTER TABLE jobs ADD COLUMN vod_path TEXT DEFAULT ''")

        clip_columns = {
            row["name"] for row in
            c.execute("PRAGMA table_info(clips)").fetchall()
        }
        if "start_seconds" not in clip_columns:
            c.execute(
                "ALTER TABLE clips ADD COLUMN start_seconds REAL DEFAULT 0"
            )


init_db()


@app.get("/api/health")
def health():
    return {"ok": True, "app": "ClipAura Studio", "version": "3.0.0"}


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
        raise HTTPException(400, "Enter a valid VOD link")

    if not creator.strip():
        raise HTTPException(400, "Enter the creator name")

    job_id = str(uuid4())

    with conn() as c:
        c.execute("""
            INSERT INTO jobs(id, creator, vod_url, notes)
            VALUES(?,?,?,?)
        """, (job_id, creator.strip(), vod_url.strip(), notes.strip()))

    return {"id": job_id, "status": "ready_to_submit"}


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
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    if not job:
        raise HTTPException(404, "Job not found")

    parsed = urlparse(job["vod_url"])
    host = (parsed.hostname or "").lower()
    allowed = (
        host == "twitch.tv" or host.endswith(".twitch.tv")
        or host == "youtube.com" or host.endswith(".youtube.com")
        or host == "youtu.be"
        or host == "kick.com" or host.endswith(".kick.com")
    )

    if parsed.scheme != "https" or not allowed:
        raise HTTPException(400, "Use a supported HTTPS Twitch, YouTube or Kick VOD link")

    existing = job["vod_path"] or ""
    if existing and Path(existing).is_file():
        return {
            "ok": True,
            "status": "vod_downloaded",
            "filename": Path(existing).name
        }

    output_template = str(UPLOADS / f"{job_id}_vod.%(ext)s")
    temp_dir = UPLOADS / f"{job_id}_temp"

    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    for old in UPLOADS.glob(f"{job_id}_vod.*"):
        if old.is_file():
            old.unlink(missing_ok=True)

    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--max-filesize", "4G",
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "1",
        "--fragment-retries", "20",
        "--retries", "10",
        "--file-access-retries", "10",
        "--retry-sleep", "fragment:exp=1:20",
        "--no-continue",
        "--paths", f"temp:{temp_dir}",
        "-o", output_template,
        job["vod_url"]
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=3600
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(504, "VOD download timed out")
    except FileNotFoundError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(503, "Could not start yt-dlp")

    if result.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        error = (result.stderr or result.stdout or "Unknown error")[-2000:]
        raise HTTPException(500, "VOD download failed: " + error)

    downloaded = [
        p for p in UPLOADS.glob(f"{job_id}_vod.*")
        if p.is_file() and p.suffix.lower() in
        {".mp4", ".mkv", ".webm", ".mov"}
    ]

    shutil.rmtree(temp_dir, ignore_errors=True)

    if not downloaded:
        raise HTTPException(500, "Download finished but no video was found")

    vod_path = str(downloaded[0])

    with conn() as c:
        c.execute("""
            UPDATE jobs SET vod_path=?, status='vod_downloaded'
            WHERE id=?
        """, (vod_path, job_id))

    return {
        "ok": True,
        "status": "vod_downloaded",
        "filename": downloaded[0].name
    }


@app.post("/api/jobs/{job_id}/analyse")
def analyse_vod(job_id: str):
    with conn() as c:
        job = c.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    if not job:
        raise HTTPException(404, "Job not found")

    vod_path = job["vod_path"] or ""
    if not vod_path or not Path(vod_path).is_file():
        raise HTTPException(400, "Download the VOD before analysing it")

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            vod_path
        ],
        capture_output=True, text=True, timeout=60
    )

    try:
        vod_duration = float(probe.stdout.strip())
    except Exception:
        raise HTTPException(500, "Could not read VOD duration")

    if probe.returncode != 0 or vod_duration <= 0:
        raise HTTPException(500, "Invalid VOD duration")

    # Read audio in 5-second windows. stderr is discarded to avoid
    # a full stderr pipe blocking the audio process.
    command = [
        "ffmpeg", "-v", "error", "-i", vod_path,
        "-vn", "-ac", "1", "-ar", "8000",
        "-f", "s16le", "pipe:1"
    ]

    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )

    window_seconds = 5
    window_bytes = 8000 * window_seconds * 2
    measurements = []

    try:
        index = 0
        while True:
            raw = process.stdout.read(window_bytes)
            if not raw:
                break

            # Ignore a trailing incomplete audio sample.
            raw = raw[:len(raw) - (len(raw) % 2)]
            samples = array.array("h")
            samples.frombytes(raw)

            if not samples:
                break

            square_sum = sum(v * v for v in samples)
            rms = math.sqrt(square_sum / len(samples))
            peak = max(abs(v) for v in samples)

            db = 20 * math.log10(max(rms, 1) / 32768)
            peak_db = 20 * math.log10(max(peak, 1) / 32768)

            measurements.append({
                "time": index * window_seconds,
                "db": db,
                "peak_db": peak_db
            })
            index += 1

        return_code = process.wait(timeout=3600)
        if return_code != 0:
            raise HTTPException(500, "Audio analysis failed")
    except Exception as exc:
        process.kill()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(500, "Audio analysis could not finish")

    if len(measurements) < 2:
        raise HTTPException(400, "Not enough audio to analyse")

    levels = [m["db"] for m in measurements]
    peaks = [m["peak_db"] for m in measurements]

    def normalise(value, low, high):
        if high <= low:
            return 0
        return max(0, min(100, (value - low) / (high - low) * 100))

    ranked = []
    for item in measurements:
        energy = normalise(item["db"], min(levels), max(levels))
        peak = normalise(item["peak_db"], min(peaks), max(peaks))
        score = round(energy * 0.65 + peak * 0.35)
        ranked.append({"time": item["time"], "score": score})

    # Pick strong moments spread across the VOD. Each selected moment
    # gets its own stored start time and its own 18-second source section.
    ranked.sort(key=lambda item: item["score"], reverse=True)
    selected = []
    min_gap = 25
    clip_length = 18

    for item in ranked:
        if item["score"] < 25:
            continue

        event_time = item["time"] + 2.5
        start = max(0, min(event_time - 6, vod_duration - clip_length))
        end = min(vod_duration, start + clip_length)

        if end - start < 5:
            continue

        if any(abs(start - old["start"]) < min_gap for old in selected):
            continue

        selected.append({
            "time": event_time,
            "start": round(start, 2),
            "duration": round(end - start, 2),
            "score": item["score"]
        })

        if len(selected) >= 20:
            break

    if not selected:
        raise HTTPException(400, "No strong audio moments found")

    # Order candidates chronologically and replace only this job's
    # previous pending candidates. Approved/rejected clips are retained.
    selected.sort(key=lambda item: item["start"])

    created = []
    with conn() as c:
        c.execute("""
            DELETE FROM clips
            WHERE job_id=? AND status='pending_review'
        """, (job_id,))

        for number, item in enumerate(selected, start=1):
            clip_id = str(uuid4())
            start_seconds = item["start"]

            whole = int(start_seconds)
            timestamp = (
                f"{whole // 3600}h"
                f"{(whole % 3600) // 60}m"
                f"{whole % 60}s"
            )

            original_url = job["vod_url"]
            separator = "&" if "?" in original_url else "?"
            source_url = f"{original_url}{separator}t={timestamp}"

            title = f"Moment {number} — {timestamp}"

            c.execute("""
                INSERT INTO clips(
                    id, job_id, title, source_url, score,
                    duration, status, start_seconds
                )
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                clip_id, job_id, title, source_url,
                item["score"], item["duration"],
                "pending_review", start_seconds
            ))

            created.append({
                "id": clip_id,
                "title": title,
                "score": item["score"],
                "start_seconds": start_seconds,
                "duration": item["duration"]
            })

    return {
        "ok": True,
        "message": "VOD analysis complete",
        "candidates_created": len(created),
        "clips": created
    }


@app.post("/api/jobs/{job_id}/clips")
def add_clip(
    job_id: str,
    title: str = Form(""),
    source_url: str = Form(""),
    score: int = Form(0),
    duration: float = Form(18)
):
    with conn() as c:
        job = c.execute(
            "SELECT id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")

        clip_id = str(uuid4())
        c.execute("""
            INSERT INTO clips(
                id, job_id, title, source_url, score, duration
            ) VALUES(?,?,?,?,?,?)
        """, (
            clip_id, job_id, title, source_url,
            max(0, min(100, score)), max(0, duration)
        ))

    return {"id": clip_id, "status": "pending_review"}


@app.get("/api/clips")
def list_clips():
    with conn() as c:
        rows = c.execute("""
            SELECT clips.*, jobs.creator, jobs.vod_path
            FROM clips
            JOIN jobs ON jobs.id=clips.job_id
            ORDER BY clips.rowid DESC
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
async def upload_clip(clip_id: str, video: UploadFile = File(...)):
    extension = Path(video.filename or "clip.mp4").suffix.lower()
    if extension not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(400, "Upload MP4, MOV, MKV or WEBM")

    with conn() as c:
        exists = c.execute(
            "SELECT id FROM clips WHERE id=?", (clip_id,)
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
    return {"ok": True, "filename": path.name}


@app.post("/api/clips/{clip_id}/render")
def render_clip(
    clip_id: str,
    start: float = Form(0),
    end: float = Form(18),
    layout: str = Form("full_frame")
):
    with conn() as c:
        clip = c.execute("""
            SELECT clips.*, jobs.vod_path
            FROM clips JOIN jobs ON jobs.id=clips.job_id
            WHERE clips.id=?
        """, (clip_id,)).fetchone()

    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip["status"] != "approved":
        raise HTTPException(409, "Approve this clip before rendering")
    if start < 0 or end <= start or end - start > 90:
        raise HTTPException(400, "Choose a valid range up to 90 seconds")
    if layout not in {"full_frame", "stacked_source"}:
        raise HTTPException(400, "Unsupported layout")

    # Uploaded files remain supported. Otherwise cut directly from the VOD.
    if clip["file_path"] and Path(clip["file_path"]).is_file():
        input_path = clip["file_path"]
        seek_time = start
    else:
        input_path = clip["vod_path"] or ""
        if not input_path or not Path(input_path).is_file():
            raise HTTPException(400, "The original VOD file is missing")
        if end > float(clip["duration"]) + 0.1:
            raise HTTPException(400, "End time exceeds this candidate's duration")
        seek_time = float(clip["start_seconds"] or 0) + start

    output = EXPORTS / f"{clip_id}_clipaura.mp4"

    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,fps=30,format=yuv420p"
    )

    command = [
        "ffmpeg", "-y",
        "-ss", str(seek_time),
        "-i", input_path,
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
            command, capture_output=True, text=True, timeout=900
        )
    except FileNotFoundError:
        raise HTTPException(503, "FFmpeg is not installed")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Render timed out")

    if result.returncode != 0:
        raise HTTPException(500, "FFmpeg render failed: " + result.stderr[-1200:])

    with conn() as c:
        c.execute("""
            UPDATE clips SET export_path=?, status='rendered'
            WHERE id=?
        """, (str(output), clip_id))

    return {"ok": True, "download": f"/api/exports/{output.name}"}


@app.get("/api/exports/{filename}")
def download_export(filename: str):
    path = EXPORTS / Path(filename).name
    if not path.is_file():
        raise HTTPException(404, "Export not found")
    return FileResponse(
        path, media_type="video/mp4", filename=path.name
    )


app.mount(
    "/",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="static"
)
