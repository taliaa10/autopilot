"""
app.py — AUTOPILOT backend
Flask server that runs the Sora → TikTok pipeline.
Deploy to Railway: set env vars OPENAI_API_KEY and TIKTOK_SESSION_ID.
"""

import os
import time
import json
import logging
import threading
import requests
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, render_template, Response
import queue

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config from env vars ───────────────────────────────────────────────────
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
TIKTOK_SESSION_ID = os.environ.get("TIKTOK_SESSION_ID", "")   # TikTok sessionid cookie
APP_SECRET       = os.environ.get("APP_SECRET", "")           # Optional: protect your app with a password

OUTPUT_DIR = Path("/tmp/videos")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── In-memory job store ────────────────────────────────────────────────────
# { job_id: { status, logs: [], video_path, error } }
jobs = {}
job_queues = {}   # SSE log streams per job

def new_job_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

# ── SSE helpers ────────────────────────────────────────────────────────────
def push_log(job_id, msg, level="info"):
    """Push a log line to the job's SSE queue and in-memory log."""
    entry = {"msg": msg, "level": level, "t": datetime.now().strftime("%H:%M:%S")}
    if job_id in jobs:
        jobs[job_id]["logs"].append(entry)
    if job_id in job_queues:
        job_queues[job_id].put(entry)

def push_step(job_id, step):
    """Signal a pipeline step change."""
    entry = {"step": step, "t": datetime.now().strftime("%H:%M:%S")}
    if job_id in jobs:
        jobs[job_id]["step"] = step
    if job_id in job_queues:
        job_queues[job_id].put(entry)

# ── Pipeline logic ─────────────────────────────────────────────────────────
def run_pipeline(job_id, prompt, caption, hashtags, product_id, dry_run, model, duration):
    try:
        jobs[job_id]["status"] = "running"

        # ── STEP 1: Generate video with Sora ──────────────────────────────
        push_step(job_id, 1)
        push_log(job_id, f"Submitting to Sora API ({model}, {duration}s)...", "step")

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        resp = requests.post(
            "https://api.openai.com/v1/videos/generations",
            headers=headers,
            json={
                "model": model,
                "prompt": prompt,
                "size": "1080x1920",
                "n": 1,
                "duration": int(duration),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        job_sora_id = data.get("id") or (data.get("data") or [{}])[0].get("id")
        push_log(job_id, f"Job queued → id={job_sora_id}", "info")

        # Poll until done
        video_url = None
        for attempt in range(180):
            time.sleep(5)
            poll = requests.get(
                f"https://api.openai.com/v1/videos/generations/{job_sora_id}",
                headers=headers, timeout=30
            )
            poll.raise_for_status()
            sd = poll.json()
            status = sd.get("status", "unknown")
            push_log(job_id, f"Status: {status} (poll {attempt+1})", "info")

            if status == "completed":
                video_url = (
                    sd.get("url") or sd.get("video_url")
                    or (sd.get("data") or [{}])[0].get("url")
                )
                break
            elif status in ("failed", "cancelled"):
                raise RuntimeError(f"Sora job failed with status: {status}")

        push_log(job_id, "Video generated ✓", "success")

        # ── STEP 2: Download ───────────────────────────────────────────────
        push_step(job_id, 2)
        push_log(job_id, "Downloading video...", "step")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        video_path = OUTPUT_DIR / f"sora_{timestamp}.mp4"

        if video_url:
            r = requests.get(video_url, stream=True, timeout=120)
            r.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        else:
            content_resp = requests.get(
                f"https://api.openai.com/v1/videos/generations/{job_sora_id}/content/video",
                headers=headers, stream=True, timeout=120
            )
            content_resp.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in content_resp.iter_content(8192):
                    f.write(chunk)

        jobs[job_id]["video_path"] = str(video_path)
        push_log(job_id, f"Saved → {video_path.name}", "success")

        # ── STEP 3 & 4: Upload to TikTok ──────────────────────────────────
        if dry_run:
            push_step(job_id, 3)
            push_log(job_id, "Dry run — skipping upload", "accent")
            push_step(job_id, 4)
            push_log(job_id, "Done (dry run) ✓", "success")
        else:
            push_step(job_id, 3)
            push_log(job_id, "Starting TikTok upload...", "step")

            try:
                from tiktok_uploader.upload import upload_video
            except ImportError:
                raise RuntimeError("tiktok-uploader not installed. Add it to requirements.txt.")

            # Write session cookie to temp file
            cookies_path = OUTPUT_DIR / "cookies.txt"
            cookies_path.write_text(
                f"# Netscape HTTP Cookie File\n"
                f".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\t{TIKTOK_SESSION_ID}\n"
            )

            hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
            full_desc = f"{caption} {hashtag_str}".strip()

            kwargs = dict(
                filename=str(video_path),
                description=full_desc,
                cookies=str(cookies_path),
            )
            if product_id:
                kwargs["product_id"] = product_id
                push_log(job_id, f"Attaching product ID: {product_id}", "info")

            push_log(job_id, "Uploading via Playwright...", "info")
            upload_video(**kwargs)

            push_step(job_id, 4)
            push_log(job_id, "Posted to TikTok ✓", "success")

        jobs[job_id]["status"] = "done"
        push_log(job_id, "── Pipeline complete ──", "done")
        if job_id in job_queues:
            job_queues[job_id].put({"done": True})

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        push_log(job_id, f"Error: {e}", "error")
        log.exception(f"Pipeline error for job {job_id}")
        if job_id in job_queues:
            job_queues[job_id].put({"done": True, "error": str(e)})


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/run", methods=["POST"])
def api_run():
    """Start a pipeline job. Returns job_id immediately."""
    if APP_SECRET and request.headers.get("X-App-Secret") != APP_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json() or {}
    prompt     = body.get("prompt", "").strip()
    caption    = body.get("caption", "").strip()
    hashtags   = body.get("hashtags", [])
    product_id = body.get("product_id", "").strip() or None
    dry_run    = body.get("dry_run", False)
    model      = body.get("model", "sora-2")
    duration   = body.get("duration", 10)

    if not prompt:
        return jsonify({"error": "prompt is required"}), 400
    if not caption:
        return jsonify({"error": "caption is required"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"error": "OPENAI_API_KEY env var not set on server"}), 500

    job_id = new_job_id()
    jobs[job_id] = {"status": "starting", "logs": [], "step": 0, "video_path": None, "error": None}
    job_queues[job_id] = queue.Queue()

    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id, prompt, caption, hashtags, product_id, dry_run, model, duration),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})

@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    """SSE endpoint — streams log lines for a job."""
    if job_id not in jobs:
        return jsonify({"error": "job not found"}), 404

    def generate():
        # First, replay existing logs
        for entry in jobs[job_id].get("logs", []):
            yield f"data: {json.dumps(entry)}\n\n"

        q = job_queues.get(job_id)
        if not q:
            return

        while True:
            try:
                entry = q.get(timeout=30)
                yield f"data: {json.dumps(entry)}\n\n"
                if entry.get("done"):
                    break
            except queue.Empty:
                yield "data: {\"ping\": true}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/job/<job_id>")
def api_job(job_id):
    """Poll job status."""
    if job_id not in jobs:
        return jsonify({"error": "not found"}), 404
    j = jobs[job_id]
    return jsonify({
        "status": j["status"],
        "step": j.get("step", 0),
        "error": j.get("error"),
        "has_video": j.get("video_path") is not None,
    })

@app.route("/api/presets", methods=["GET"])
def get_presets():
    presets_file = Path("/tmp/presets.json")
    if presets_file.exists():
        return jsonify(json.loads(presets_file.read_text()))
    return jsonify([])

@app.route("/api/presets", methods=["POST"])
def save_preset():
    body = request.get_json() or {}
    presets_file = Path("/tmp/presets.json")
    presets = json.loads(presets_file.read_text()) if presets_file.exists() else []
    presets.append(body)
    presets_file.write_text(json.dumps(presets))
    return jsonify({"ok": True, "count": len(presets)})

@app.route("/api/presets/<int:idx>", methods=["DELETE"])
def delete_preset(idx):
    presets_file = Path("/tmp/presets.json")
    presets = json.loads(presets_file.read_text()) if presets_file.exists() else []
    if 0 <= idx < len(presets):
        presets.pop(idx)
        presets_file.write_text(json.dumps(presets))
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
