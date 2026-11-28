
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse
import sqlite3
import os
import secrets
import math
import array
import subprocess
import sys
import shutil

import hashlib
import hmac
import time
import re

import httpx

from fastapi import Request, Body
from fastapi.responses import HTMLResponse, RedirectResponse

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
EXPORTS = DATA / "exports"
ASSETS = DATA / "assets"

for folder in (DATA, UPLOADS, EXPORTS, ASSETS):
    folder.mkdir(parents=True, exist_ok=True)

DB = DATA / "clipaura.sqlite3"

app = FastAPI(title="ClipAura Studio", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


# Private single-owner access gate. Configure both secrets in Railway before use.
SESSION_COOKIE = "clipaura_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60


def valid_session(token: str, secret: str) -> bool:
    try:
        timestamp_text, supplied_signature = token.split(".", 1)
        timestamp = int(timestamp_text)
        if timestamp > int(time.time()) or int(time.time()) - timestamp > SESSION_MAX_AGE:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            timestamp_text.encode("ascii"),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(supplied_signature, expected)
    except (ValueError, TypeError):
        return False


@app.middleware("http")
async def owner_access_gate(request: Request, call_next):
    path = request.url.path

    if path == "/api/health":
        return await call_next(request)

    # Public image inspection links are time-limited and HMAC-verified by the route.
    if path.startswith("/share/asset/"):
        return await call_next(request)

    if path == "/login":
        return await call_next(request)

    if path == "/logout":
        if request.method == "POST":
            return await call_next(request)
        return HTMLResponse("Method not allowed", status_code=405)

    owner_password = os.environ.get("CLIPAURA_OWNER_PASSWORD", "")
    session_secret = os.environ.get("CLIPAURA_SESSION_SECRET", "")

    if not owner_password or not session_secret:
        return HTMLResponse(
            "ClipAura is locked. Configure CLIPAURA_OWNER_PASSWORD and "
            "CLIPAURA_SESSION_SECRET in Railway Variables, then redeploy.",
            status_code=503
        )

    token = request.cookies.get(SESSION_COOKIE, "")
    if not valid_session(token, session_secret):
        if path.startswith("/api/"):
            return HTMLResponse("Authentication required", status_code=401)
        return RedirectResponse("/login", status_code=303)

    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    if not os.environ.get("CLIPAURA_OWNER_PASSWORD") or not os.environ.get("CLIPAURA_SESSION_SECRET"):
        return HTMLResponse(
            "Login is not configured yet. Set CLIPAURA_OWNER_PASSWORD and "
            "CLIPAURA_SESSION_SECRET in Railway Variables.",
            status_code=503
        )
    return HTMLResponse("""<!doctype html>
<html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClipAura Studio — Sign in</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d12;color:#f5f5f5;font:16px system-ui}
form{width:min(88vw,360px);padding:28px;background:#171b24;border:1px solid #303746;border-radius:18px}
h1{font-size:24px;margin:0 0 8px}p{color:#aab2c0}
input,button{box-sizing:border-box;width:100%;padding:14px;border-radius:10px;font:inherit}
input{background:#0d1119;border:1px solid #424b5c;color:white;margin:12px 0}
button{border:0;background:#8b5cf6;color:white;font-weight:700}
</style>
<form method="post" action="/login"><h1>ClipAura Studio</h1>
<p>Sign in to your private workspace.</p>
<input type="password" name="password" placeholder="Owner password" required autocomplete="current-password">
<button type="submit">Sign in</button></form></html>""")


@app.post("/login")
def login(password: str = Form(...)):
    owner_password = os.environ.get("CLIPAURA_OWNER_PASSWORD", "")
    session_secret = os.environ.get("CLIPAURA_SESSION_SECRET", "")
    if not owner_password or not session_secret:
        raise HTTPException(503, "Owner login is not configured")
    if not secrets.compare_digest(password, owner_password):
        raise HTTPException(401, "Incorrect password")
    timestamp_text = str(int(time.time()))
    signature = hmac.new(
        session_secret.encode("utf-8"),
        timestamp_text.encode("ascii"),
        hashlib.sha256
    ).hexdigest()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        f"{timestamp_text}.{signature}",
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/"
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=True,
        httponly=True, samesite="strict"
    )
    return response


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
            c.execute(
                "ALTER TABLE jobs ADD COLUMN vod_path TEXT DEFAULT ''"
            )

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
    return {
        "ok": True,
        "app": "ClipAura Studio",
        "version": "3.0.0"
    }


@app.get("/api/google/config")
def google_picker_config():
    # These are browser-side Picker settings. The API key is referrer-restricted
    # in Google Cloud; the OAuth client ID and project number are public identifiers.
    values = {
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        "api_key": os.environ.get("GOOGLE_API_KEY", "").strip(),
        "project_number": os.environ.get("GOOGLE_PROJECT_NUMBER", "").strip(),
    }
    if not all(values.values()):
        raise HTTPException(
            503,
            "Google Drive is not configured yet. Add GOOGLE_CLIENT_ID, GOOGLE_API_KEY and GOOGLE_PROJECT_NUMBER in Railway Variables."
        )
    return values


@app.post("/api/drive/import")
async def import_google_drive_video(
    payload: dict = Body(...),
    authorization: str = Header(default="", alias="Authorization")
):
    access_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    file_id = str(payload.get("file_id", "")).strip()

    if not access_token:
        raise HTTPException(401, "Google authorization is missing. Connect Google Drive and try again.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        raise HTTPException(400, "The selected Google Drive file ID is invalid.")

    headers = {"Authorization": f"Bearer {access_token}"}
    metadata_url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    timeout = httpx.Timeout(180.0, connect=25.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        metadata_response = await client.get(
            metadata_url,
            params={"fields": "id,name,mimeType,size"},
            headers=headers
        )
        if metadata_response.status_code == 401:
            raise HTTPException(401, "Google authorization expired. Choose the video again.")
        if metadata_response.status_code == 403:
            raise HTTPException(403, "Google Drive did not grant access to this file. Re-select it in the Google Picker.")
        if metadata_response.status_code == 404:
            raise HTTPException(404, "That Google Drive video could not be found.")
        if metadata_response.status_code != 200:
            raise HTTPException(502, "Google Drive could not provide the selected file details.")

        metadata = metadata_response.json()
        mime_type = str(metadata.get("mimeType", ""))
        if not mime_type.startswith("video/"):
            raise HTTPException(400, "Please select a video file from Google Drive.")

        original_name = str(metadata.get("name") or "drive-video")
        safe_stem = Path(original_name).stem[:100]
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"}:
            suffix = ".mp4"

        declared_size = int(metadata.get("size") or 0)
        max_bytes = 4 * 1024 * 1024 * 1024
        if declared_size > max_bytes:
            raise HTTPException(413, "This video is larger than ClipAura's 4 GB import limit.")

        job_id = str(uuid4())
        destination = UPLOADS / f"{job_id}_drive_{secrets.token_hex(4)}{suffix}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0

        try:
            async with client.stream(
                "GET",
                metadata_url,
                params={"alt": "media"},
                headers=headers
            ) as media_response:
                if media_response.status_code == 401:
                    raise HTTPException(401, "Google authorization expired. Choose the video again.")
                if media_response.status_code == 403:
                    raise HTTPException(403, "Google Drive refused access to the video. Re-select it in the Picker.")
                if media_response.status_code != 200:
                    raise HTTPException(502, "Google Drive could not download the selected video.")

                with open(temporary, "wb") as output:
                    async for chunk in media_response.aiter_bytes(chunk_size=1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise HTTPException(413, "This video exceeds ClipAura's 4 GB import limit.")
                        output.write(chunk)

            if total == 0:
                raise HTTPException(400, "Google Drive returned an empty video.")
            temporary.replace(destination)
            os.chmod(destination, 0o600)

        except Exception:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise

    creator = "Google Drive"
    notes = f"Imported from Google Drive: {original_name}"
    drive_url = f"https://drive.google.com/file/d/{file_id}/view"

    try:
        with conn() as c:
            c.execute(
                """
                INSERT INTO jobs(id, creator, vod_url, notes, status, vod_path)
                VALUES(?,?,?,?,?,?)
                """,
                (job_id, creator, drive_url, notes, "vod_downloaded", str(destination))
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise HTTPException(500, "The video downloaded, but ClipAura could not add it to the VOD Vault.")

    return {
        "ok": True,
        "job_id": job_id,
        "filename": original_name,
        "bytes": total,
        "status": "vod_downloaded"
    }


@app.post("/api/drive/import-image")
async def import_google_drive_image(
    payload: dict = Body(...),
    authorization: str = Header(default="", alias="Authorization")
):
    access_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    file_id = str(payload.get("file_id", "")).strip()

    if not access_token:
        raise HTTPException(401, "Google authorization is missing. Connect Google Drive and try again.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        raise HTTPException(400, "The selected Google Drive file ID is invalid.")

    headers = {"Authorization": f"Bearer {access_token}"}
    metadata_url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    timeout = httpx.Timeout(90.0, connect=25.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        metadata_response = await client.get(
            metadata_url,
            params={"fields": "id,name,mimeType,size"},
            headers=headers
        )
        if metadata_response.status_code == 401:
            raise HTTPException(401, "Google authorization expired. Choose the image again.")
        if metadata_response.status_code == 403:
            raise HTTPException(403, "Google Drive did not grant access to this image. Re-select it in the Google Picker.")
        if metadata_response.status_code != 200:
            raise HTTPException(502, "Google Drive could not provide the selected image details.")

        metadata = metadata_response.json()
        mime_type = str(metadata.get("mimeType", "")).lower()
        allowed = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp"
        }
        if mime_type not in allowed:
            raise HTTPException(400, "Choose a PNG, JPG or WEBP image.")
        original_name = str(metadata.get("name") or "drive-image")
        declared_size = int(metadata.get("size") or 0)
        max_bytes = 20 * 1024 * 1024
        if declared_size > max_bytes:
            raise HTTPException(413, "Images must be 20 MB or smaller.")

        asset_id = str(uuid4())
        suffix = allowed[mime_type]
        destination = ASSETS / f"{asset_id}{suffix}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0

        try:
            async with client.stream(
                "GET", metadata_url, params={"alt": "media"}, headers=headers
            ) as media_response:
                if media_response.status_code == 401:
                    raise HTTPException(401, "Google authorization expired. Choose the image again.")
                if media_response.status_code != 200:
                    raise HTTPException(502, "Google Drive could not download the selected image.")
                with open(temporary, "wb") as output:
                    async for chunk in media_response.aiter_bytes(chunk_size=256 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise HTTPException(413, "This image exceeds ClipAura's 20 MB limit.")
                        output.write(chunk)

            if total == 0:
                raise HTTPException(400, "Google Drive returned an empty image.")
            temporary.replace(destination)
            os.chmod(destination, 0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise

    return {
        "ok": True,
        "asset_id": asset_id,
        "filename": original_name,
        "bytes": total,
        "url": f"/api/assets/{asset_id}"
    }


@app.get("/api/assets")
def list_assets():
    items = []
    for path in sorted(ASSETS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".webp"}:
            items.append({
                "asset_id": path.stem,
                "filename": path.name,
                "url": f"/api/assets/{path.stem}"
            })
    return items


@app.post("/api/assets/{asset_id}/share")
def create_asset_inspection_link(asset_id: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", asset_id):
        raise HTTPException(400, "Invalid asset ID.")
    matches = [p for p in ASSETS.glob(f"{asset_id}.*") if p.suffix.lower() in {".png", ".jpg", ".webp"}]
    if not matches or not matches[0].is_file():
        raise HTTPException(404, "Asset not found.")
    secret = os.environ.get("CLIPAURA_SESSION_SECRET", "")
    if not secret:
        raise HTTPException(503, "Secure sharing is not configured.")
    expires = int(time.time()) + 10 * 60
    payload = f"{asset_id}.{expires}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return {
        "url": f"/share/asset/{asset_id}/{expires}.{signature}",
        "expires_in_seconds": 600
    }


@app.get("/share/asset/{asset_id}/{proof}")
def view_shared_asset(asset_id: str, proof: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", asset_id):
        raise HTTPException(404, "Shared asset not found.")
    try:
        expires_text, supplied_signature = proof.split(".", 1)
        expires = int(expires_text)
    except (ValueError, TypeError):
        raise HTTPException(404, "Shared asset link is invalid.")
    if expires < int(time.time()) or expires > int(time.time()) + 10 * 60 + 30:
        raise HTTPException(410, "This inspection link has expired. Create a new one in ClipAura.")
    secret = os.environ.get("CLIPAURA_SESSION_SECRET", "")
    payload = f"{asset_id}.{expires}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(supplied_signature, expected):
        raise HTTPException(403, "Shared asset link is invalid.")
    matches = [p for p in ASSETS.glob(f"{asset_id}.*") if p.suffix.lower() in {".png", ".jpg", ".webp"}]
    if not matches or not matches[0].is_file():
        raise HTTPException(404, "Shared asset not found.")
    media = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[matches[0].suffix.lower()]
    return FileResponse(matches[0], media_type=media, filename=matches[0].name, headers={"Cache-Control": "no-store"})


@app.get("/api/assets/{asset_id}")
def get_asset(asset_id: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", asset_id):
        raise HTTPException(400, "Invalid asset ID.")
    matches = [p for p in ASSETS.glob(f"{asset_id}.*") if p.suffix.lower() in {".png", ".jpg", ".webp"}]
    if not matches or not matches[0].is_file():
        raise HTTPException(404, "Asset not found.")
    media = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[matches[0].suffix.lower()]
    return FileResponse(matches[0], media_type=media, filename=matches[0].name)




@app.get("/api/storage")
def storage_inventory():
    items=[]; known=set()
    def add_item(kind,item_id,path,label,detail=""):
        try:
            resolved=Path(path).resolve()
            if not resolved.is_file(): return
            stat=resolved.stat()
            items.append({"kind":kind,"id":str(item_id),"filename":resolved.name,"label":label,"detail":detail,"bytes":stat.st_size,"modified":stat.st_mtime})
            known.add(str(resolved))
        except (OSError,RuntimeError): return
    with conn() as c:
        for row in c.execute("SELECT id,creator,vod_path FROM jobs").fetchall():
            if row["vod_path"]: add_item("vod",row["id"],row["vod_path"],Path(row["vod_path"]).name,row["creator"] or "VOD")
        for row in c.execute("SELECT clips.id,clips.title,clips.file_path,clips.export_path,jobs.creator FROM clips LEFT JOIN jobs ON jobs.id=clips.job_id").fetchall():
            if row["file_path"]: add_item("clip_upload",row["id"],row["file_path"],row["title"] or "Uploaded clip",row["creator"] or "")
            if row["export_path"]: add_item("export",row["id"],row["export_path"],row["title"] or "Rendered export",row["creator"] or "")
    for folder,kind,extensions in ((ASSETS,"asset",{".png",".jpg",".webp"}),(UPLOADS,"orphan_upload",{".mp4",".mkv",".webm",".mov"}),(EXPORTS,"orphan_export",{".mp4",".mkv",".webm",".mov"})):
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in extensions and str(path.resolve()) not in known: add_item(kind,path.name,path,path.name,"Unlinked file")
    items.sort(key=lambda x:x["modified"],reverse=True)
    return {"items":items,"total_bytes":sum(x["bytes"] for x in items),"file_count":len(items)}


@app.delete("/api/storage/{kind}/{item_id}")
def delete_storage_item(kind: str,item_id: str):
    folders={"vod":UPLOADS,"clip_upload":UPLOADS,"export":EXPORTS,"asset":ASSETS,"orphan_upload":UPLOADS,"orphan_export":EXPORTS}
    if kind not in folders: raise HTTPException(400,"Unsupported storage item type.")
    path=None
    with conn() as c:
        if kind=="vod":
            row=c.execute("SELECT vod_path FROM jobs WHERE id=?",(item_id,)).fetchone()
            if row and row["vod_path"]: path=Path(row["vod_path"])
        elif kind=="clip_upload":
            row=c.execute("SELECT file_path FROM clips WHERE id=?",(item_id,)).fetchone()
            if row and row["file_path"]: path=Path(row["file_path"])
        elif kind=="export":
            row=c.execute("SELECT export_path FROM clips WHERE id=?",(item_id,)).fetchone()
            if row and row["export_path"]: path=Path(row["export_path"])
        elif kind=="asset":
            if not re.fullmatch(r"[0-9a-fA-F-]{36}",item_id): raise HTTPException(400,"Invalid asset ID.")
            matches=[p for p in ASSETS.glob(f"{item_id}.*") if p.suffix.lower() in {".png",".jpg",".webp"}]
            path=matches[0] if matches else None
        else:
            if Path(item_id).name!=item_id or item_id in {"",".",".."}: raise HTTPException(400,"Invalid filename.")
            candidate=folders[kind]/item_id
            if candidate.suffix.lower() in {".mp4",".mkv",".webm",".mov"}: path=candidate
        if path is None: raise HTTPException(404,"Storage item not found.")
        try:
            resolved=path.resolve(strict=True); resolved.relative_to(folders[kind].resolve())
        except (OSError,ValueError,RuntimeError): raise HTTPException(400,"File is outside the managed storage area.")
        if not resolved.is_file(): raise HTTPException(404,"Storage file not found.")
        resolved.unlink()
        if kind=="vod": c.execute("UPDATE jobs SET vod_path='' WHERE id=?",(item_id,))
        elif kind=="clip_upload": c.execute("UPDATE clips SET file_path='' WHERE id=?",(item_id,))
        elif kind=="export": c.execute("UPDATE clips SET export_path='',status=CASE WHEN status='rendered' THEN 'approved' ELSE status END WHERE id=?",(item_id,))
    return {"ok":True,"deleted":path.name}


@app.post("/api/settings/youtube-cookies")
async def upload_youtube_cookies(
    cookies: UploadFile = File(...),
    token: str = Header(default="", alias="X-ClipAura-Admin-Token")
):
    expected_token = os.environ.get("CLIPAURA_ADMIN_TOKEN", "")

    if not expected_token:
        raise HTTPException(503, "Admin token is not configured")

    if not secrets.compare_digest(token, expected_token):
        raise HTTPException(401, "Invalid admin token")

    if Path(cookies.filename or "").suffix.lower() != ".txt":
        raise HTTPException(400, "Upload a Netscape cookies .txt file")

    content = await cookies.read(2 * 1024 * 1024 + 1)

    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(413, "Cookie file is too large")

    text_content = content.decode("utf-8", errors="replace")

    if not any(
        marker in text_content
        for marker in (
            "# Netscape HTTP Cookie File",
            "# HTTP Cookie File"
        )
    ):
        raise HTTPException(400, "Invalid Netscape cookie file")

    cookie_path = DATA / "youtube_cookies.txt"
    temporary_path = DATA / "youtube_cookies.tmp"

    temporary_path.write_bytes(content)
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(cookie_path)
    os.chmod(cookie_path, 0o600)

    return {
        "ok": True,
        "message": "YouTube cookies securely saved"
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
        raise HTTPException(
            400,
            "Use a supported HTTPS Twitch, YouTube or Kick VOD link"
        )

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

    is_youtube = (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
    )

    cookie_path = DATA / "youtube_cookies.txt"

    
    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--js-runtimes", "deno",
        "--max-filesize", "4G",
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "1",
        "--fragment-retries", "20",
        "--retries", "10",
        "--file-access-retries", "10",
        "--retry-sleep", "fragment:exp=1:20",
        "--no-continue",
        "--paths", f"temp:{temp_dir}",
        "-o", output_template
    ]

    command.append(job["vod_url"])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3600
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
        raise HTTPException(
            500,
            "Download finished but no video was found"
        )

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
        raise HTTPException(
            400,
            "Download the VOD before analysing it"
        )

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            vod_path
        ],
        capture_output=True,
        text=True,
        timeout=60
    )

    try:
        vod_duration = float(probe.stdout.strip())
    except Exception:
        raise HTTPException(500, "Could not read VOD duration")

    if probe.returncode != 0 or vod_duration <= 0:
        raise HTTPException(500, "Invalid VOD duration")

    command = [
        "ffmpeg", "-v", "error", "-i", vod_path,
        "-vn", "-ac", "1", "-ar", "8000",
        "-f", "s16le", "pipe:1"
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
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

        raise HTTPException(
            500,
            "Audio analysis could not finish"
        )

    if len(measurements) < 2:
        raise HTTPException(400, "Not enough audio to analyse")

    levels = [m["db"] for m in measurements]
    peaks = [m["peak_db"] for m in measurements]

    def normalise(value, low, high):
        if high <= low:
            return 0

        return max(
            0,
            min(100, (value - low) / (high - low) * 100)
        )

    ranked = []

    for item in measurements:
        energy = normalise(item["db"], min(levels), max(levels))
        peak = normalise(item["peak_db"], min(peaks), max(peaks))

        score = round(energy * 0.65 + peak * 0.35)

        ranked.append({
            "time": item["time"],
            "score": score
        })

    ranked.sort(key=lambda item: item["score"], reverse=True)

    selected = []
    min_gap = 25
    clip_length = 18

    for item in ranked:
        if item["score"] < 25:
            continue

        event_time = item["time"] + 2.5

        start = max(
            0,
            min(event_time - 6, vod_duration - clip_length)
        )

        end = min(vod_duration, start + clip_length)

        if end - start < 5:
            continue

        if any(
            abs(start - old["start"]) < min_gap
            for old in selected
        ):
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
                clip_id,
                job_id,
                title,
                source_url,
                item["score"],
                item["duration"],
                "pending_review",
                start_seconds
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
            clip_id,
            job_id,
            title,
            source_url,
            max(0, min(100, score)),
            max(0, duration)
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




def detect_facecam_region(video_path: str, seek_time: float, duration: float):
    """Detect a stable facecam region from face boxes across sampled video frames."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise HTTPException(503, "Facecam detection is unavailable until OpenCV and NumPy are installed.")

    cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        raise HTTPException(503, "Face detector could not be loaded.")

    samples = sorted(set([0.5, max(0.5, duration * 0.2), max(0.5, duration * 0.4),
                          max(0.5, duration * 0.6), max(0.5, duration * 0.8)]))
    frame_reports = []
    detections = []
    frames_read = 0

    for offset in samples:
        at = max(0, seek_time + min(offset, max(0, duration - 0.2)))
        report = {"offset_seconds": round(offset, 2), "source_seconds": round(at, 2),
                  "frame_read": False, "frame_size": None, "faces": []}
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(at), "-i", video_path,
                 "-frames:v", "1", "-vf", "scale=1280:-1", "-f", "image2pipe",
                 "-vcodec", "mjpeg", "-"],
                capture_output=True, timeout=30
            )
        except Exception as exc:
            report["read_error"] = str(exc)[:180]
            frame_reports.append(report)
            continue
        if proc.returncode != 0 or not proc.stdout:
            report["read_error"] = (proc.stderr or b"").decode("utf-8", "replace")[:180] or "FFmpeg returned no frame"
            frame_reports.append(report)
            continue

        frame_buffer = np.frombuffer(proc.stdout, dtype=np.uint8)
        frame = cv2.imdecode(frame_buffer, cv2.IMREAD_GRAYSCALE)
        if frame is None:
            report["read_error"] = "OpenCV could not decode the extracted frame"
            frame_reports.append(report)
            continue

        frames_read += 1
        height, width = frame.shape[:2]
        report["frame_read"] = True
        report["frame_size"] = {"width": int(width), "height": int(height)}
        enhanced = cv2.equalizeHist(frame)
        faces = detector.detectMultiScale(
            enhanced, scaleFactor=1.08, minNeighbors=4, minSize=(36, 36)
        )

        for x, y, w, h in faces:
            cx, cy = x + w / 2, y + h / 2
            if cx < width * 0.48 and cy < height * 0.48:
                corner = "top_left"
            elif cx > width * 0.52 and cy < height * 0.48:
                corner = "top_right"
            elif cx < width * 0.48 and cy > height * 0.52:
                corner = "bottom_left"
            elif cx > width * 0.52 and cy > height * 0.52:
                corner = "bottom_right"
            else:
                corner = "center"
            face = {
                "x": int(x), "y": int(y), "width": int(w), "height": int(h),
                "center_x": float(cx), "center_y": float(cy),
                "corner": corner, "frame_width": int(width), "frame_height": int(height)
            }
            report["faces"].append({
                "box": {"x": int(x), "y": int(y), "width": int(w), "height": int(h)},
                "center": {"x": round(float(cx), 1), "y": round(float(cy), 1)},
                "corner": corner
            })
            if corner != "center":
                detections.append(face)
        frame_reports.append(report)

    corner_counts = {k: sum(1 for d in detections if d["corner"] == k)
                     for k in ("top_left", "top_right", "bottom_left", "bottom_right")}
    ranked = sorted(corner_counts.items(), key=lambda item: item[1], reverse=True)
    best_corner, best_count = ranked[0] if ranked else ("", 0)
    selected = [d for d in detections if d["corner"] == best_corner]

    diagnostics = {
        "frames_requested": len(samples),
        "frames_read": frames_read,
        "face_detections": len(detections),
        "corner_votes": corner_counts,
        "samples": frame_reports
    }
    if frames_read == 0 or best_count < 2:
        return {
            "detected": False, "confidence": 0,
            "message": "No stable facecam was detected. Try a clip with a clearer, larger webcam view.",
            "diagnostics": diagnostics
        }

    # Use the median face box to reduce false detections and movement between frames.
    med = lambda key: float(np.median([d[key] for d in selected]))
    frame_width = selected[0]["frame_width"]
    frame_height = selected[0]["frame_height"]
    face_cx, face_cy = med("center_x"), med("center_y")
    face_w, face_h = med("width"), med("height")

    # Estimate a webcam window around the face. Keep it inside the detected corner.
    roi_w = min(0.48, max(0.18, (face_w * 3.2) / frame_width))
    roi_h = min(0.48, max(0.18, (face_h * 3.0) / frame_height))
    if best_corner.endswith("left"):
        roi_x = min(max(0.0, (face_cx / frame_width) - roi_w * 0.50), 0.48 - roi_w)
    else:
        roi_x = max(0.52, (face_cx / frame_width) - roi_w * 0.50)
        roi_x = min(roi_x, 1.0 - roi_w)
    if best_corner.startswith("top"):
        roi_y = min(max(0.0, (face_cy / frame_height) - roi_h * 0.50), 0.48 - roi_h)
    else:
        roi_y = max(0.52, (face_cy / frame_height) - roi_h * 0.50)
        roi_y = min(roi_y, 1.0 - roi_h)

    confidence = round(min(0.95, 0.45 + 0.45 * (best_count / max(1, len(detections))) +
                           0.10 * (best_count / max(1, frames_read))), 2)
    webcam_region = {
        "x": round(roi_x, 4), "y": round(roi_y, 4),
        "width": round(roi_w, 4), "height": round(roi_h, 4),
        "coordinate_space": "normalized_0_to_1",
        "estimated_from": "median_face_box"
    }
    diagnostics["selected_face_median"] = {
        "center_x": round(face_cx, 1), "center_y": round(face_cy, 1),
        "width": round(face_w, 1), "height": round(face_h, 1)
    }
    diagnostics["estimated_webcam_region"] = webcam_region

    return {
        "detected": True, "position": best_corner, "confidence": confidence,
        "samples_with_face": len(set(round(d["center_x"], 0) for d in selected)),
        "samples_checked": frames_read, "webcam_region": webcam_region,
        "diagnostics": diagnostics
    }


def facecam_stack_filter(position: str, webcam_region=None):
    # Prefer the estimated webcam rectangle; retain corner fallback for manual selection.
    if webcam_region:
        x = float(webcam_region["x"])
        y = float(webcam_region["y"])
        w = float(webcam_region["width"])
        h = float(webcam_region["height"])
        face_crop = f"crop=iw*{w:.5f}:ih*{h:.5f}:iw*{x:.5f}:ih*{y:.5f}"
    else:
        crops = {
            "top_left": "crop=iw*0.45:ih*0.45:0:0",
            "top_right": "crop=iw*0.45:ih*0.45:iw*0.55:0",
            "bottom_left": "crop=iw*0.45:ih*0.45:0:ih*0.55",
            "bottom_right": "crop=iw*0.45:ih*0.45:iw*0.55:ih*0.55",
        }
        face_crop = crops[position]
    return (
        f"[0:v]split=2[game][cam];"
        f"[game]scale=1080:1152:force_original_aspect_ratio=increase,crop=1080:1152,setsar=1[gameout];"
        f"[cam]{face_crop},scale=1080:768:force_original_aspect_ratio=increase,crop=1080:768,setsar=1[camout];"
        f"[camout][gameout]vstack=inputs=2,fps=30,format=yuv420p[v]"
    )

def detect_facecam(clip_id: str):
    try:
        with conn() as c:
            clip = c.execute("""SELECT clips.*, jobs.vod_path FROM clips JOIN jobs ON jobs.id=clips.job_id WHERE clips.id=?""", (clip_id,)).fetchone()
        if not clip:
            raise HTTPException(404, "Clip not found")
        if clip["file_path"] and Path(clip["file_path"]).is_file():
            path, seek = clip["file_path"], 0.0
            duration = float(clip["duration"] or 18)
        elif clip["vod_path"] and Path(clip["vod_path"]).is_file():
            path = clip["vod_path"]
            seek = float(clip["start_seconds"] or 0)
            duration = float(clip["duration"] or 18)
        else:
            raise HTTPException(400, "Original video is missing")
        return detect_facecam_region(path, seek, duration)
    except HTTPException:
        raise
    except Exception as exc:
        # Keep the traceback in Railway logs while returning the useful exception to the UI.
        import logging
        logging.exception("Facecam detection crashed for clip_id=%s", clip_id)
        detail = f"Facecam detection crashed: {type(exc).__name__}: {str(exc)[:300]}"
        raise HTTPException(500, detail)


@app.post("/api/clips/{clip_id}/render")
def render_clip(
    clip_id: str,
    start: float = Form(0),
    end: float = Form(18),
    layout: str = Form("full_frame"),
    asset_id: str = Form(""),
    facecam_position: str = Form("auto")
):
    with conn() as c:
        clip = c.execute("""
            SELECT clips.*, jobs.vod_path
            FROM clips
            JOIN jobs ON jobs.id = clips.job_id
            WHERE clips.id=?
        """, (clip_id,)).fetchone()

    if not clip:
        raise HTTPException(404, "Clip not found")

    if clip["status"] not in {"approved", "rendered"}:
        raise HTTPException(
            409,
            "Approve this clip before rendering"
        )

    if start < 0 or end <= start or end - start > 90:
        raise HTTPException(400, "Choose a valid range up to 90 seconds")

    if layout not in {"full_frame", "stacked_source", "facecam_stack"}:
        raise HTTPException(400, "Unsupported layout")

    vod_path = clip["vod_path"] or ""

    is_analysed_candidate = (
        (clip["title"] or "").startswith("Moment ")
    )

    if (
        is_analysed_candidate
        and vod_path
        and Path(vod_path).is_file()
    ):
        input_path = vod_path
        seek_time = float(clip["start_seconds"] or 0) + start

        candidate_duration = float(clip["duration"] or 0)

        if candidate_duration <= 0:
            raise HTTPException(400, "Candidate duration is invalid")

        if end > candidate_duration + 0.1:
            raise HTTPException(
                400,
                "End time exceeds this candidate's duration"
            )

        source_type = "original_vod"

    elif clip["file_path"] and Path(clip["file_path"]).is_file():
        input_path = clip["file_path"]
        seek_time = start
        source_type = "uploaded_clip"

    elif vod_path and Path(vod_path).is_file():
        input_path = vod_path
        seek_time = float(clip["start_seconds"] or 0) + start
        source_type = "original_vod"

        if end > float(clip["duration"] or 0) + 0.1:
            raise HTTPException(
                400,
                "End time exceeds this candidate's duration"
            )

    else:
        raise HTTPException(
            400,
            "The original VOD or uploaded video is missing"
        )

    detected_webcam_region = None
    if layout == "facecam_stack":
        if facecam_position == "auto":
            detection = detect_facecam_region(input_path, seek_time, end - start)
            if not detection.get("detected"):
                raise HTTPException(422, detection.get("message", "Facecam not detected."))
            facecam_position = detection["position"]
            detected_webcam_region = detection.get("webcam_region")
        if facecam_position not in {"top_left", "top_right", "bottom_left", "bottom_right"}:
            raise HTTPException(400, "Choose a valid facecam corner.")
    output = EXPORTS / f"{clip_id}_clipaura.mp4"

    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,fps=30,format=yuv420p"
    )

    # Keep fast input seeking for the multi-input overlay graph. For a
    # plain clip, seek after opening the source so FFmpeg decodes accurately
    # to the requested timestamp instead of relying on keyframe seeking.
    if asset_id:
        command = [
            "ffmpeg", "-y",
            "-filter_threads", "2",
            "-filter_complex_threads", "2",
            "-ss", str(seek_time),
            "-i", input_path,
        ]
    else:
        command = [
            "ffmpeg", "-y",
            "-filter_threads", "2",
            "-filter_complex_threads", "2",
            "-i", input_path,
            "-ss", str(seek_time),
        ]

    if layout == "facecam_stack" and asset_id:
        raise HTTPException(400, "Campaign overlay is temporarily unavailable with facecam-stack layout; render the stack first.")

    if asset_id:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", asset_id):
            raise HTTPException(400, "Invalid overlay asset.")
        asset_matches = [
            p for p in ASSETS.glob(f"{asset_id}.*")
            if p.suffix.lower() in {".png", ".jpg", ".webp"}
        ]
        if not asset_matches or not asset_matches[0].is_file():
            raise HTTPException(404, "Overlay image not found in your Assets Library.")

        overlay_path = asset_matches[0]
        command.extend([
            "-loop", "1", "-i", str(overlay_path),
            "-t", str(end - start),
            "-filter_complex",
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1,fps=30[base];"
            "[1:v]scale=iw*min(300/iw\\,180/ih):ih*min(300/iw\\,180/ih)"
            "[logo];[base][logo]overlay=(W-w)/2:H*0.68:shortest=1,"
            "format=yuv420p[v]",
            "-map", "[v]", "-map", "0:a?",
        ])
    elif layout == "facecam_stack":
        command.extend(["-t", str(end - start), "-filter_complex", facecam_stack_filter(facecam_position, detected_webcam_region), "-map", "[v]", "-map", "0:a?"])
    else:
        command.extend([
            "-t", str(end - start),
            "-vf", video_filter,
        ])

    command.extend([
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-threads", "2",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output)
    ])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900
        )

    except FileNotFoundError:
        raise HTTPException(503, "FFmpeg is not installed")

    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Render timed out")

    if result.returncode != 0:
        raise HTTPException(
            500,
            f"FFmpeg render failed (exit={result.returncode}, source={source_type}, seek={seek_time:.3f}s, duration={end-start:.3f}s, threads=2): " + result.stderr[-5000:]
        )

    with conn() as c:
        c.execute("""
            UPDATE clips
            SET export_path=?, status='rendered'
            WHERE id=?
        """, (str(output), clip_id))

    return {
        "ok": True,
        "source": source_type,
        "clip_id": clip_id,
        "source_start_seconds": seek_time,
        "duration_seconds": end - start,
        "overlay_asset_id": asset_id or None,
        "download": f"/api/exports/{output.name}"
    }


@app.get("/api/exports/{filename}")
def download_export(filename: str):
    path = EXPORTS / Path(filename).name

    if not path.is_file():
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
