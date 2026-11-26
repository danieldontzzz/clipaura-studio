# ClipAura Studio — Version 1

A cloud-ready, approval-first clipping workflow for iPad Safari.

## Included in V1
- Responsive iPad/iPhone web dashboard
- Creator VOD queue (SQLite)
- Clips.gg assisted handoff (opens the service; user submits and returns)
- Candidate clip entry, score sorting, approval/rejection
- Video upload and FFmpeg 9:16 MP4 render
- CapCut export download list

## Important integration boundary
The public ClipsGG page currently advertises YouTube URL or file upload, AI scoring and clip previews. This project does **not** claim a public ClipsGG API or automate its private UI. V1 prepares and tracks the workflow while you submit through the official site and add the returned candidates. Do not store ClipsGG passwords or session cookies in this app.

## Run locally
Requires Python 3.11+ and FFmpeg installed.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000

## Cloud deployment
Build and deploy the included Dockerfile to a container host (for example Railway or another persistent container service). Attach persistent storage at `/srv/clipaura/data` so SQLite, uploads and exports survive restarts. Set HTTPS and restrict access before using real creator media.

The app has no authentication in this prototype. **Do not expose it publicly with real uploads until authentication is added.** Add an access gate (for example platform-level password protection) before production use.

## V1 editing behavior
The current renderer makes a 1080x1920 crop from one uploaded video, 30fps, H.264/AAC. It does not yet separate facecam and gameplay into independent 40/60 tracks, perform subject tracking, generate captions, or add sound effects. Those are planned V1.1 upgrades and need source layout decisions / separate media tracks.

## Suggested next upgrades
1. Add authentication and private object storage.
2. Add a dual-source renderer: facecam 40% top + gameplay 60% bottom.
3. Add subtitle transcription and word-level captions.
4. Add supported ClipsGG API integration if one becomes available.
5. Add creator RSS/API discovery and scheduled VOD checks.
