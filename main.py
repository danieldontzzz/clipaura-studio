from pathlib import Path
from uuid import uuid4
import sqlite3, os, subprocess, json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
EXPORTS = DATA / "exports"
for p in (DATA, UPLOADS, EXPORTS):
    p.mkdir(parents=True, exist_ok=True)
DB = DATA / "clipaura.sqlite3"

app = FastAPI(title="ClipAura Studio", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, creator TEXT NOT NULL, vod_url TEXT NOT NULL,
            notes TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'ready_to_submit',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS clips (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, title TEXT DEFAULT '',
            source_url TEXT DEFAULT '', score INTEGER DEFAULT 0, duration REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending_review', file_path TEXT DEFAULT '',
            export_path TEXT DEFAULT '', FOREIGN KEY(job_id) REFERENCES jobs(id)
        )""")
init_db()

@app.get("/api/health")
def health():
    return {"ok": True, "app": "ClipAura Studio", "version": "1.0.0"}

@app.get("/api/jobs")
def list_jobs():
    with conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

@app.post("/api/jobs")
def create_job(creator: str = Form(...), vod_url: str = Form(...), notes: str = Form("")):
    if not vod_url.startswith(("https://", "http://")):
        raise HTTPException(400, "Enter a valid VOD URL beginning with http:// or https://")
    jid = str(uuid4())
    with conn() as c:
        c.execute("INSERT INTO jobs(id,creator,vod_url,notes) VALUES(?,?,?,?)",
                  (jid, creator.strip(), vod_url.strip(), notes.strip()))
    return {"id": jid, "creator": creator, "vod_url": vod_url, "status": "ready_to_submit"}

@app.post("/api/jobs/{job_id}/submitted")
def mark_submitted(job_id: str):
    with conn() as c:
        cur = c.execute("UPDATE jobs SET status='submitted_to_clipsgg' WHERE id=?", (job_id,))
        if not cur.rowcount: raise HTTPException(404, "Job not found")
    return {"ok": True}

@app.post("/api/jobs/{job_id}/clips")
def add_clip(job_id: str, title: str = Form(""), source_url: str = Form(""), score: int = Form(0),
             duration: float = Form(0)):
    with conn() as c:
        if not c.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "Job not found")
        cid = str(uuid4())
        c.execute("INSERT INTO clips(id,job_id,title,source_url,score,duration) VALUES(?,?,?,?,?,?)",
                  (cid, job_id, title, source_url, max(0,min(100,score)), max(0,duration)))
    return {"id": cid, "status": "pending_review"}

@app.get("/api/clips")
def list_clips():
    with conn() as c:
        rows = c.execute("""SELECT clips.*, jobs.creator FROM clips
                            JOIN jobs ON jobs.id=clips.job_id
                            ORDER BY clips.score DESC, clips.rowid DESC""").fetchall()
    return [dict(r) for r in rows]

@app.post("/api/clips/{clip_id}/approve")
def approve_clip(clip_id: str):
    with conn() as c:
        cur = c.execute("UPDATE clips SET status='approved' WHERE id=?", (clip_id,))
        if not cur.rowcount: raise HTTPException(404, "Clip not found")
    return {"ok": True, "status": "approved"}

@app.post("/api/clips/{clip_id}/reject")
def reject_clip(clip_id: str):
    with conn() as c:
        cur = c.execute("UPDATE clips SET status='rejected' WHERE id=?", (clip_id,))
        if not cur.rowcount: raise HTTPException(404, "Clip not found")
    return {"ok": True, "status": "rejected"}

@app.post("/api/clips/{clip_id}/upload")
async def upload_clip(clip_id: str, video: UploadFile = File(...)):
    ext = Path(video.filename or "clip.mp4").suffix.lower() or ".mp4"
    if ext not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(400, "Upload MP4, MOV, MKV or WEBM")
    path = UPLOADS / f"{clip_id}{ext}"
    path.write_bytes(await video.read())
    with conn() as c:
        cur = c.execute("UPDATE clips SET file_path=? WHERE id=?", (str(path), clip_id))
        if not cur.rowcount: raise HTTPException(404, "Clip not found")
    return {"ok": True, "filename": path.name}

@app.post("/api/clips/{clip_id}/render")
def render_clip(clip_id: str, start: float = Form(0), end: float = Form(18),
                layout: str = Form("full_frame")):
    with conn() as c:
        row = c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not row: raise HTTPException(404, "Clip not found")
    if row["status"] != "approved": raise HTTPException(409, "Approve this clip before rendering")
    if not row["file_path"] or not Path(row["file_path"]).exists():
        raise HTTPException(400, "Upload the clip file first")
    if end <= start or end - start > 90:
        raise HTTPException(400, "Choose a valid range up to 90 seconds")
    if layout not in {"full_frame", "stacked_source"}:
        raise HTTPException(400, "Unsupported layout")
    out = EXPORTS / f"{clip_id}_clipaura.mp4"
    # V1: full-frame fit for landscape/portrait sources. The stacked_source mode
    # preserves an already-stacked facecam/gameplay composition while fitting 9:16.
    vf = ("scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,setsar=1,fps=30,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", row["file_path"], "-t", str(end-start),
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        raise HTTPException(503, "FFmpeg is not installed on this server yet")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Render timed out")
    if result.returncode != 0:
        raise HTTPException(500, "FFmpeg render failed: " + result.stderr[-1200:])
    with conn() as c:
        c.execute("UPDATE clips SET export_path=?, status='rendered' WHERE id=?", (str(out), clip_id))
    return {"ok": True, "download": f"/api/exports/{out.name}"}

@app.get("/api/exports/{filename}")
def download_export(filename: str):
    path = EXPORTS / Path(filename).name
    if not path.exists(): raise HTTPException(404, "Export not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
