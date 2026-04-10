"""
app.py — AUTOPILOT (single-file version for Railway)
HTML is embedded directly — no templates/ folder needed.
"""

import os
import time
import json
import logging
import threading
import requests
import queue
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
TIKTOK_SESSION_ID = os.environ.get("TIKTOK_SESSION_ID", "")
APP_SECRET        = os.environ.get("APP_SECRET", "")

OUTPUT_DIR = Path("/tmp/videos")
OUTPUT_DIR.mkdir(exist_ok=True)

jobs = {}
job_queues = {}

def new_job_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

def push_log(job_id, msg, level="info"):
    entry = {"msg": msg, "level": level, "t": datetime.now().strftime("%H:%M:%S")}
    if job_id in jobs:
        jobs[job_id]["logs"].append(entry)
    if job_id in job_queues:
        job_queues[job_id].put(entry)

def push_step(job_id, step):
    entry = {"step": step, "t": datetime.now().strftime("%H:%M:%S")}
    if job_id in jobs:
        jobs[job_id]["step"] = step
    if job_id in job_queues:
        job_queues[job_id].put(entry)


def _make_dummy_mp4() -> bytes:
    """Minimal valid MP4 - pure Python, no dependencies."""
    import struct
    def box(name, data=b""):
        return struct.pack(">I", len(data) + 8) + name.encode() + data
    def u32(n): return struct.pack(">I", n)
    def u16(n): return struct.pack(">H", n)
    ftyp = box("ftyp", b"isom" + u32(0x200) + b"isom" + b"iso2" + b"mp41")
    mvhd_data = (
        u32(0) + u32(0) + u32(0) +
        u32(1000) + u32(4000) +
        u32(0x00010000) + u16(0x0100) +
        b"\x00" * 10 +
        u32(0x00010000) + u32(0) + u32(0) +
        u32(0) + u32(0x00010000) + u32(0) +
        u32(0) + u32(0) + u32(0x40000000) +
        b"\x00" * 24 + u32(2)
    )
    moov = box("moov", box("mvhd", mvhd_data))
    return ftyp + box("mdat", b"\x00" * 4) + moov


def run_pipeline(job_id, prompt, caption, hashtags, product_id, dry_run, model, duration):
    try:
        jobs[job_id]["status"] = "running"

        push_step(job_id, 1)
        push_log(job_id, f"Submitting to Sora API ({model}, {duration}s)...", "step")

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        # Correct endpoint: POST /v1/videos  (not /v1/videos/generations)
        payload = {
            "model": model,
            "prompt": prompt,
            "size": "720x1280",  # valid portrait sizes: 720x1280 or 1024x1792
            "seconds": str(duration),  # must be string: "4", "8", or "12"
        }
        push_log(job_id, "POST /v1/videos ...", "info")
        resp = requests.post(
            "https://api.openai.com/v1/videos",
            headers=headers,
            json=payload,
            timeout=60,
        )

        if not resp.ok:
            push_log(job_id, f"Sora API error {resp.status_code}: {resp.text[:300]}", "error")
            raise RuntimeError(f"Sora API returned {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        push_log(job_id, f"Response keys: {list(data.keys())}", "info")

        job_sora_id = (
            data.get("id")
            or data.get("generation_id")
            or (data.get("data") or [{}])[0].get("id")
        )
        if not job_sora_id:
            raise RuntimeError(f"No job ID in response: {str(data)[:200]}")

        push_log(job_id, f"Job queued → id={job_sora_id}", "info")

        video_url = None
        for attempt in range(180):
            time.sleep(5)
            # Correct poll endpoint: GET /v1/videos/{id}
            poll = requests.get(
                f"https://api.openai.com/v1/videos/{job_sora_id}",
                headers=headers,
                timeout=30,
            )
            if not poll.ok:
                push_log(job_id, f"Poll error {poll.status_code}: {poll.text[:200]}", "error")
                raise RuntimeError(f"Poll failed: {poll.status_code}")

            sd = poll.json()
            status = sd.get("status", "unknown")
            if attempt % 6 == 0:
                push_log(job_id, f"Status: {status} ({attempt*5}s elapsed)", "info")

            if status in ("completed", "succeeded"):
                video_url = (
                    sd.get("url")
                    or sd.get("video_url")
                    or (sd.get("data") or [{}])[0].get("url")
                    or (sd.get("generations") or [{}])[0].get("url")
                )
                push_log(job_id, f"Completed! url={bool(video_url)}", "success")
                break
            elif status in ("failed", "cancelled", "error"):
                err_detail = sd.get("error") or sd.get("message") or str(sd)[:200]
                raise RuntimeError(f"Sora job {status}: {err_detail}")

        push_log(job_id, "Video generated ✓", "success")

        push_step(job_id, 2)
        push_log(job_id, "Downloading video...", "step")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        video_path = OUTPUT_DIR / f"sora_{timestamp}.mp4"

        if video_url:
            r = requests.get(video_url, stream=True, timeout=120)
            r.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in r.iter_content(8192): f.write(chunk)
        else:
            # Fallback: fetch content directly
            cr = requests.get(f"https://api.openai.com/v1/videos/{job_sora_id}/content", headers=headers, stream=True, timeout=120)
            cr.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in cr.iter_content(8192): f.write(chunk)

        jobs[job_id]["video_path"] = str(video_path)
        push_log(job_id, f"Saved → {video_path.name}", "success")

        if dry_run:
            push_step(job_id, 3)
            push_log(job_id, "Dry run — skipping upload", "accent")
            push_step(job_id, 4)
            push_log(job_id, "Done (dry run) ✓", "success")
        else:
            push_step(job_id, 3)
            push_log(job_id, "Starting TikTok upload...", "step")
            try:
                from tiktok_uploader.upload import TikTokUploader
            except ImportError:
                raise RuntimeError("tiktok-uploader not installed.")

            cookies_list = [{
                'name': 'sessionid',
                'value': TIKTOK_SESSION_ID,
                'domain': '.tiktok.com',
                'path': '/',
                'expiry': 2147483647,
            }]
            hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
            full_desc = f"{caption} {hashtag_str}".strip()
            uploader = TikTokUploader(cookies_list=cookies_list, browser="chrome", headless=True)
            video_kwargs = dict(description=full_desc)
            if product_id:
                video_kwargs["product_id"] = product_id
                push_log(job_id, f"Attaching product ID: {product_id}", "info")
            push_log(job_id, "Uploading via Playwright...", "info")
            push_log(job_id, "Note: check TikTok drafts if not visible publicly", "info")
            uploader.upload_video(str(video_path), **video_kwargs)
            push_step(job_id, 4)
            push_log(job_id, "Posted to TikTok ✓ — check your profile AND drafts", "success")

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


# ── HTML (embedded) ────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#080810">
<title>AUTOPILOT</title>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=JetBrains+Mono:ital,wght@0,300;0,400;1,300&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{--bg:#080810;--s1:#10101c;--s2:#16162a;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);--lime:#c8ff57;--cyan:#57ffc4;--red:#ff4d6a;--text:#f0f0f8;--muted:#555570;--muted2:#888899;--r:14px;--safe-bottom:env(safe-area-inset-bottom,0px)}
html{height:100%;background:var(--bg)}
body{min-height:100%;background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:14px;overscroll-behavior:none}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='300'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.75' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='300' height='300' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E");opacity:0.5}
header{position:sticky;top:0;z-index:100;padding:16px 20px 14px;padding-top:calc(16px + env(safe-area-inset-top,0px));background:rgba(8,8,16,0.88);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.logo{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:1rem;letter-spacing:0.2em;display:flex;align-items:center;gap:10px}
.logo-pip{width:7px;height:7px;border-radius:50%;background:var(--lime);box-shadow:0 0 8px var(--lime);animation:pip 2s ease-in-out infinite}
@keyframes pip{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.3;transform:scale(0.6)}}
.status-pill{display:flex;align-items:center;gap:6px;padding:5px 10px;border-radius:20px;border:1px solid var(--border);background:var(--s1);font-size:0.65rem;letter-spacing:0.1em;color:var(--muted2);text-transform:uppercase;transition:all 0.3s}
.status-pip{width:5px;height:5px;border-radius:50%;background:var(--muted);transition:all 0.3s}
.status-pill.running{border-color:rgba(200,255,87,0.3);color:var(--lime)}
.status-pill.running .status-pip{background:var(--lime);box-shadow:0 0 6px var(--lime);animation:pip 0.8s infinite}
.status-pill.done{border-color:rgba(87,255,196,0.3);color:var(--cyan)}
.status-pill.done .status-pip{background:var(--cyan);box-shadow:0 0 6px var(--cyan);animation:none}
.status-pill.error{border-color:rgba(255,77,106,0.3);color:var(--red)}
.status-pill.error .status-pip{background:var(--red);animation:none}
.scroll{padding:20px 16px;padding-bottom:calc(100px + var(--safe-bottom));display:flex;flex-direction:column;gap:14px;max-width:560px;margin:0 auto}
.card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);padding:18px;animation:fadeUp 0.4s ease both}
.card:nth-child(1){animation-delay:0.05s}.card:nth-child(2){animation-delay:0.1s}.card:nth-child(3){animation-delay:0.15s}.card:nth-child(4){animation-delay:0.2s}.card:nth-child(5){animation-delay:0.25s}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
.card-tag{font-size:0.6rem;letter-spacing:0.18em;text-transform:uppercase;color:var(--muted);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.card-tag::after{content:'';flex:1;height:1px;background:var(--border)}
.stepbar{display:flex;gap:6px;margin-bottom:14px}
.stepbar-item{flex:1;height:3px;border-radius:2px;background:var(--s2);transition:background 0.4s;position:relative;overflow:hidden}
.stepbar-item.active{background:var(--border2)}
.stepbar-item.active::after{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;background:linear-gradient(90deg,transparent,rgba(200,255,87,0.6),transparent);animation:shimmer 1.2s infinite}
@keyframes shimmer{to{left:100%}}
.stepbar-item.done{background:var(--lime)}
.step-labels{display:flex;gap:6px;margin-bottom:16px}
.step-label{flex:1;font-size:0.58rem;text-align:center;color:var(--muted);letter-spacing:0.06em;text-transform:uppercase;transition:color 0.3s}
.step-label.active{color:var(--lime)}.step-label.done{color:var(--cyan)}
.log{background:#05050d;border-radius:10px;padding:12px;height:160px;overflow-y:auto;font-size:0.72rem;line-height:1.75;color:var(--muted2);-webkit-overflow-scrolling:touch}
.log::-webkit-scrollbar{width:3px}.log::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.log-line{display:block}.log-line.step{color:var(--text)}.log-line.success,.log-line.done{color:var(--cyan)}.log-line.error{color:var(--red)}.log-line.accent{color:var(--lime)}.log-line.info{color:var(--muted2);font-style:italic}
.field{margin-bottom:14px}.field:last-child{margin-bottom:0}
.field-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:7px}
label{font-size:0.62rem;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted2)}
.counter{font-size:0.6rem;color:var(--muted);transition:color 0.2s}.counter.warn{color:#ffb347}.counter.over{color:var(--red)}
textarea,input[type=text],select{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:0.82rem;padding:12px 14px;outline:none;resize:none;-webkit-appearance:none;appearance:none;transition:border-color 0.2s,box-shadow 0.2s}
textarea{line-height:1.5}
textarea:focus,input:focus,select:focus{border-color:rgba(200,255,87,0.3);box-shadow:0 0 0 3px rgba(200,255,87,0.06)}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23555570' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}
select option{background:#16162a}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.chip-area{background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:8px;display:flex;flex-wrap:wrap;gap:6px;min-height:46px;cursor:text;transition:border-color 0.2s,box-shadow 0.2s}
.chip-area:focus-within{border-color:rgba(200,255,87,0.3);box-shadow:0 0 0 3px rgba(200,255,87,0.06)}
.chip{display:flex;align-items:center;gap:4px;background:rgba(200,255,87,0.08);border:1px solid rgba(200,255,87,0.2);border-radius:6px;padding:3px 8px;font-size:0.7rem;color:var(--lime);animation:chipIn 0.15s ease;white-space:nowrap}
@keyframes chipIn{from{opacity:0;transform:scale(0.8)}to{opacity:1;transform:scale(1)}}
.chip-x{background:none;border:none;color:var(--lime);opacity:0.5;font-size:0.65rem;cursor:pointer;padding:0;line-height:1;transition:opacity 0.15s}
.chip-x:hover{opacity:1}
.chip-input{background:none;border:none;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:0.8rem;outline:none;min-width:80px;flex:1;padding:3px 4px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;background:var(--s2);border-radius:10px;margin-bottom:10px}
.toggle-row:last-of-type{margin-bottom:14px}
.tl{font-size:0.8rem}.ts{font-size:0.65rem;color:var(--muted2);margin-top:2px}
.switch{position:relative;width:42px;height:24px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.sw-track{position:absolute;inset:0;background:var(--s1);border:1px solid var(--border2);border-radius:12px;cursor:pointer;transition:0.2s}
.sw-track::before{content:'';position:absolute;width:16px;height:16px;left:3px;top:3px;background:var(--muted);border-radius:50%;transition:0.2s}
.switch input:checked+.sw-track{background:rgba(200,255,87,0.12);border-color:rgba(200,255,87,0.35)}
.switch input:checked+.sw-track::before{transform:translateX(18px);background:var(--lime)}
.preset-list{display:flex;flex-direction:column;gap:8px}
.preset-item{display:flex;align-items:center;gap:10px;padding:11px 14px;background:var(--s2);border:1px solid var(--border);border-radius:10px;cursor:pointer;transition:border-color 0.2s}
.preset-item:active{background:rgba(200,255,87,0.05)}
.preset-item.loaded{border-color:rgba(200,255,87,0.35)}
.pi-icon{font-size:1rem;flex-shrink:0}.pi-body{flex:1;min-width:0}
.pi-name{font-size:0.8rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi-tags{font-size:0.62rem;color:var(--muted2);margin-top:2px}
.pi-del{background:none;border:none;color:var(--muted);font-size:0.8rem;cursor:pointer;padding:4px}
.add-preset{width:100%;padding:11px;background:none;border:1px dashed var(--border);border-radius:10px;color:var(--muted2);font-family:'JetBrains Mono',monospace;font-size:0.75rem;cursor:pointer;transition:border-color 0.2s,color 0.2s;display:flex;align-items:center;justify-content:center;gap:6px}
.add-preset:active{border-color:rgba(200,255,87,0.3);color:var(--lime)}
.run-bar{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;z-index:100;padding:12px 16px;padding-bottom:calc(12px + var(--safe-bottom));background:rgba(8,8,16,0.92);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-top:1px solid var(--border);max-width:560px}
.run-btn{width:100%;padding:16px;background:var(--lime);color:#080810;border:none;border-radius:12px;font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:0.9rem;letter-spacing:0.15em;text-transform:uppercase;cursor:pointer;position:relative;overflow:hidden;transition:transform 0.15s,box-shadow 0.2s,opacity 0.2s,background 0.3s;-webkit-tap-highlight-color:transparent}
.run-btn::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,0.2) 0%,transparent 60%);pointer-events:none}
.run-btn:active:not(:disabled){transform:scale(0.97)}
.run-btn:disabled{opacity:0.35;cursor:not-allowed}
.run-btn.running{background:var(--s2);color:var(--muted2);border:1px solid var(--border2)}
.run-btn.success-state{background:var(--cyan)}
.empty{font-size:0.72rem;color:var(--muted);padding:8px 4px;font-style:italic}
.toast{position:fixed;top:80px;left:50%;transform:translateX(-50%);background:var(--s2);border:1px solid var(--border2);border-radius:10px;padding:10px 18px;font-size:0.75rem;color:var(--text);white-space:nowrap;z-index:9998;animation:toastIn 0.25s ease,toastOut 0.25s ease 2s forwards;pointer-events:none}
@keyframes toastIn{from{opacity:0;transform:translateX(-50%) translateY(-8px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
@keyframes toastOut{to{opacity:0;transform:translateX(-50%) translateY(-8px)}}
</style>
</head>
<body>
<header>
  <div class="logo"><span class="logo-pip"></span> AUTOPILOT</div>
  <div class="status-pill" id="statusPill"><span class="status-pip"></span><span id="statusText">idle</span></div>
</header>
<div class="scroll">
  <div class="card">
    <div class="card-tag">Pipeline</div>
    <div class="stepbar">
      <div class="stepbar-item" id="sb1"></div>
      <div class="stepbar-item" id="sb2"></div>
      <div class="stepbar-item" id="sb3"></div>
      <div class="stepbar-item" id="sb4"></div>
    </div>
    <div class="step-labels">
      <div class="step-label" id="sl1">Generate</div>
      <div class="step-label" id="sl2">Download</div>
      <div class="step-label" id="sl3">Upload</div>
      <div class="step-label" id="sl4">Post</div>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
      <span style="font-size:0.6rem;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);">Log output</span>
      <button onclick="copyLog()" style="background:var(--s2);border:1px solid var(--border2);border-radius:6px;color:var(--muted2);font-family:'JetBrains Mono',monospace;font-size:0.6rem;padding:3px 8px;cursor:pointer;letter-spacing:0.06em;" id="copyLogBtn">copy</button>
    </div>
    <div class="log" id="log"><span class="log-line info">// waiting for run...</span></div>
  </div>
  <div class="card">
    <div class="card-tag">Sora Video Prompt</div>
    <div class="field">
      <div class="field-head"><label>Prompt</label><span class="counter" id="pc">0/500</span></div>
      <textarea id="prompt" rows="5" placeholder="Close-up macro of a sleek glass dropper bottle on marble, golden morning light, slow-motion droplet fall, cinematic beauty product style..."></textarea>
    </div>
    <div class="row2">
      <div class="field"><label>Model</label><select id="model"><option value="sora-2">sora-2 (fast)</option><option value="sora-2-pro">sora-2-pro</option></select></div>
      <div class="field"><label>Duration</label><select id="duration"><option value="4">4 sec</option><option value="8" selected>8 sec</option><option value="12">12 sec</option></select></div>
    </div>
  </div>
  <div class="card">
    <div class="card-tag">TikTok Post</div>
    <div class="field">
      <div class="field-head"><label>Caption</label><span class="counter" id="cc">0/150</span></div>
      <input type="text" id="caption" placeholder="The serum that changed my lash game 👁️✨">
    </div>
    <div class="field">
      <label>Hashtags <span style="color:var(--muted);font-size:0.6rem;text-transform:none">(space or enter to add)</span></label>
      <div class="chip-area" id="chipArea"><input class="chip-input" id="chipInput" placeholder="#lashserum" autocomplete="off" autocorrect="off"></div>
    </div>
    <div class="field">
      <label>Product ID <span style="color:var(--muted);font-size:0.6rem;text-transform:none">(optional)</span></label>
      <input type="text" id="productId" placeholder="7123456789012345678" inputmode="numeric">
    </div>
  </div>
  <div class="card">
    <div class="card-tag">Options</div>
    <div class="toggle-row">
      <div><div class="tl">Dry run</div><div class="ts">Generate video only, don't post</div></div>
      <label class="switch"><input type="checkbox" id="dryRun"><span class="sw-track"></span></label>
    </div>
    <div class="toggle-row">
      <div><div class="tl">Schedule daily</div><div class="ts">Auto-run every day at set time</div></div>
      <label class="switch"><input type="checkbox" id="scheduleOn" onchange="toggleSchedule()"><span class="sw-track"></span></label>
    </div>
    <div class="field" id="scheduleField" style="display:none;">
      <label>Post time (24hr)</label>
      <input type="text" id="scheduleTime" placeholder="09:00" maxlength="5">
    </div>
    <div style="margin-top:4px;">
      <div class="card-tag" style="margin-bottom:10px;">TikTok test video <span style="color:var(--muted);font-size:0.6rem;text-transform:none">(optional — uses dummy if empty)</span></div>
      <label for="testVideoFile" style="display:block;width:100%;padding:12px;background:var(--s2);border:1px dashed var(--border2);border-radius:10px;text-align:center;cursor:pointer;font-size:0.75rem;color:var(--muted2);transition:border-color 0.2s;" id="testVideoLabel">
        &#128247; Tap to choose a video file
      </label>
      <input type="file" id="testVideoFile" accept="video/*" style="display:none" onchange="handleVideoFile(this)">
    </div>
  </div>
  <div class="card">
    <div class="card-tag">Saved Presets</div>
    <div class="preset-list" id="presetList"></div>
    <button class="add-preset" onclick="savePreset()">+ Save current as preset</button>
  </div>
</div>
<div class="run-bar" style="display:flex;flex-direction:column;gap:8px;">
  <button class="run-btn" id="runBtn" onclick="runPipeline()">&#9654; Run Pipeline</button>
  <button class="run-btn" id="testBtn" onclick="testTikTok()" style="background:var(--s2);color:var(--muted2);border:1px solid var(--border2);font-size:0.75rem;padding:11px;">&#128248; Test TikTok Upload Only</button>
</div>
<script>
let hashtags=[],presets=[],currentJobId=null,isRunning=false,schedulerInterval=null;
document.addEventListener('DOMContentLoaded',async()=>{
  try {
    const cfg = await fetch('/api/check-config').then(r=>r.json());
    if (!cfg.TIKTOK_SESSION_ID.startsWith('set')) {
      setTimeout(()=>toast('⚠ TIKTOK_SESSION_ID not set in Railway Variables!'),500);
    }
  } catch(e) {}
  setupChipInput();
  document.getElementById('prompt').addEventListener('input',()=>counter('prompt','pc',500));
  document.getElementById('caption').addEventListener('input',()=>counter('caption','cc',150));
  await loadPresets();
});
function counter(id,cid,max){const l=document.getElementById(id).value.length;const e=document.getElementById(cid);e.textContent=`${l}/${max}`;e.className='counter'+(l>max?' over':l>max*0.8?' warn':'');}
function setupChipInput(){const area=document.getElementById('chipArea');const inp=document.getElementById('chipInput');area.addEventListener('click',()=>inp.focus());inp.addEventListener('keydown',e=>{if((e.key===' '||e.key==='Enter')&&inp.value.trim()){e.preventDefault();addChip(inp.value.trim());inp.value='';}else if(e.key==='Backspace'&&!inp.value&&hashtags.length){removeChip(hashtags.length-1);}});}
function addChip(tag){tag=tag.replace(/^#+/,'');if(!tag||hashtags.includes(tag))return;hashtags.push(tag);renderChips();}
function removeChip(i){hashtags.splice(i,1);renderChips();}
function renderChips(){const area=document.getElementById('chipArea');area.querySelectorAll('.chip').forEach(c=>c.remove());const inp=document.getElementById('chipInput');hashtags.forEach((tag,i)=>{const chip=document.createElement('div');chip.className='chip';chip.innerHTML=`#${tag}<button class="chip-x" onclick="removeChip(${i})">&#x2715;</button>`;area.insertBefore(chip,inp);});}
function toggleSchedule(){const on=document.getElementById('scheduleOn').checked;document.getElementById('scheduleField').style.display=on?'block':'none';if(!on&&schedulerInterval){clearInterval(schedulerInterval);schedulerInterval=null;}}
async function loadPresets(){try{const r=await fetch('/api/presets');presets=await r.json();}catch{presets=[];}renderPresets();}
function renderPresets(){const list=document.getElementById('presetList');list.innerHTML='';if(!presets.length){list.innerHTML='<div class="empty">No presets yet. Fill in the form and save.</div>';return;}presets.forEach((p,i)=>{const el=document.createElement('div');el.className='preset-item';el.innerHTML=`<span class="pi-icon">&#10022;</span><div class="pi-body"><div class="pi-name">${p.name||p.prompt.substring(0,40)}</div><div class="pi-tags">${p.hashtags?.length?'#'+p.hashtags.join(' #'):'no hashtags'}</div></div><button class="pi-del" onclick="deletePreset(event,${i})">&#x2715;</button>`;el.addEventListener('click',e=>{if(!e.target.closest('.pi-del'))loadPreset(i);});list.appendChild(el);});}
async function savePreset(){const prompt=document.getElementById('prompt').value.trim();if(!prompt){toast('Add a prompt first');return;}const preset={name:prompt.substring(0,40)+(prompt.length>40?'...':''),prompt,caption:document.getElementById('caption').value,hashtags:[...hashtags],productId:document.getElementById('productId').value,model:document.getElementById('model').value,duration:document.getElementById('duration').value};await fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(preset)});await loadPresets();toast('Preset saved ✓');}
function loadPreset(i){const p=presets[i];document.getElementById('prompt').value=p.prompt||'';document.getElementById('caption').value=p.caption||'';document.getElementById('productId').value=p.productId||'';document.getElementById('model').value=p.model||'sora-2';document.getElementById('duration').value=p.duration||'10';hashtags=[...(p.hashtags||[])];renderChips();counter('prompt','pc',500);counter('caption','cc',150);document.querySelectorAll('.preset-item').forEach((el,idx)=>el.classList.toggle('loaded',idx===i));toast('Preset loaded');}
async function deletePreset(e,i){e.stopPropagation();await fetch(`/api/presets/${i}`,{method:'DELETE'});await loadPresets();}
function setStep(n){for(let i=1;i<=4;i++){const b=document.getElementById(`sb${i}`);const l=document.getElementById(`sl${i}`);if(i<n){b.className='stepbar-item done';l.className='step-label done';}else if(i===n){b.className='stepbar-item active';l.className='step-label active';}else{b.className='stepbar-item';l.className='step-label';}}}
function resetSteps(){for(let i=1;i<=4;i++){document.getElementById(`sb${i}`).className='stepbar-item';document.getElementById(`sl${i}`).className='step-label';}}
function allDone(){for(let i=1;i<=4;i++){document.getElementById(`sb${i}`).className='stepbar-item done';document.getElementById(`sl${i}`).className='step-label done';}}
function addLog(msg,level='info'){const box=document.getElementById('log');const line=document.createElement('span');line.className=`log-line ${level}`;const t=new Date().toLocaleTimeString('en-US',{hour12:false});line.textContent=`[${t}] ${msg}`;box.appendChild(document.createElement('br'));box.appendChild(line);box.scrollTop=box.scrollHeight;}
function clearLog(){document.getElementById('log').innerHTML='';}
function setStatus(state,text){document.getElementById('statusPill').className='status-pill '+state;document.getElementById('statusText').textContent=text;}
async function runPipeline(){
  if(isRunning)return;
  const prompt=document.getElementById('prompt').value.trim();
  const caption=document.getElementById('caption').value.trim();
  if(!prompt){toast('Enter a Sora prompt first');return;}
  if(!caption){toast('Enter a TikTok caption first');return;}
  isRunning=true;clearLog();resetSteps();setStatus('running','running');
  const btn=document.getElementById('runBtn');btn.textContent='⏳ Running...';btn.classList.add('running');btn.disabled=true;
  const body={prompt,caption,hashtags:[...hashtags],product_id:document.getElementById('productId').value.trim(),dry_run:document.getElementById('dryRun').checked,model:document.getElementById('model').value,duration:parseInt(document.getElementById('duration').value)};
  try{
    const res=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await res.json();
    if(data.error){addLog(data.error,'error');setStatus('error','error');finishRun(false);return;}
    currentJobId=data.job_id;addLog(`Job started: ${currentJobId}`,'accent');
    const evtSource=new EventSource(`/api/stream/${currentJobId}`);
    evtSource.onmessage=(e)=>{
      const entry=JSON.parse(e.data);
      if(entry.ping)return;
      if(entry.step!==undefined){setStep(entry.step);return;}
      if(entry.done){evtSource.close();if(entry.error){setStatus('error','error');finishRun(false);}else{allDone();setStatus('done','done ✓');finishRun(true);setupScheduler();}return;}
      if(entry.msg)addLog(entry.msg,entry.level||'info');
    };
    evtSource.onerror=()=>{evtSource.close();addLog('Connection lost','error');setStatus('error','error');finishRun(false);};
  }catch(err){addLog(`Network error: ${err.message}`,'error');setStatus('error','error');finishRun(false);}
}
function finishRun(success){isRunning=false;const btn=document.getElementById('runBtn');btn.classList.remove('running');btn.disabled=false;if(success){btn.classList.add('success-state');btn.textContent='✓ Done — Run Again';setTimeout(()=>{btn.classList.remove('success-state');btn.textContent='▶ Run Pipeline';setStatus('idle','idle');},5000);}else{btn.textContent='▶ Run Pipeline';}}
function setupScheduler(){if(!document.getElementById('scheduleOn').checked)return;const timeStr=document.getElementById('scheduleTime').value||'09:00';const[hour,min]=timeStr.split(':').map(Number);if(schedulerInterval)clearInterval(schedulerInterval);schedulerInterval=setInterval(()=>{const now=new Date();if(now.getHours()===hour&&now.getMinutes()===min&&!isRunning){addLog(`Scheduled run triggered at ${timeStr}`,'accent');runPipeline();}},60*1000);toast(`Scheduler set for ${timeStr} daily`);}

async function testTikTok() {
  if (isRunning) return;
  const caption = document.getElementById('caption').value.trim() || 'Test post';
  isRunning = true;
  clearLog();
  resetSteps();
  setStatus('running', 'testing');
  const btn = document.getElementById('testBtn');
  btn.textContent = '⏳ Testing...';
  btn.disabled = true;
  document.getElementById('runBtn').disabled = true;
  addLog('TikTok-only test (skipping Sora)...', 'accent');
  try {
    const res = await fetch('/api/test-tiktok', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        caption,
        hashtags: [...hashtags],
        product_id: document.getElementById('productId').value.trim(),
        custom_video_path: window._uploadedTestVideoPath || '',
      }),
    });
    const data = await res.json();
    if (data.error) { addLog(data.error, 'error'); setStatus('error','error'); finishTest(false); return; }
    currentJobId = data.job_id;
    const evtSource = new EventSource(`/api/stream/${currentJobId}`);
    evtSource.onmessage = (e) => {
      const entry = JSON.parse(e.data);
      if (entry.ping) return;
      if (entry.step !== undefined) { setStep(entry.step); return; }
      if (entry.done) {
        evtSource.close();
        if (entry.error) { setStatus('error','error'); finishTest(false); }
        else { allDone(); setStatus('done','done ✓'); finishTest(true); }
        return;
      }
      if (entry.msg) addLog(entry.msg, entry.level || 'info');
    };
    evtSource.onerror = () => { evtSource.close(); addLog('Connection lost','error'); setStatus('error','error'); finishTest(false); };
  } catch(err) { addLog(`Error: ${err.message}`,'error'); setStatus('error','error'); finishTest(false); }
}

function finishTest(success) {
  isRunning = false;
  const btn = document.getElementById('testBtn');
  btn.disabled = false;
  btn.textContent = success ? '✓ TikTok Test Passed!' : '📷 Test TikTok Upload Only';
  document.getElementById('runBtn').disabled = false;
  if (success) setTimeout(() => { btn.textContent = '📷 Test TikTok Upload Only'; setStatus('idle','idle'); }, 5000);
}


async function handleVideoFile(input) {
  const file = input.files[0];
  if (!file) return;
  const label = document.getElementById('testVideoLabel');
  label.textContent = '⏳ Uploading ' + file.name + '...';
  label.style.color = 'var(--muted2)';
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/upload-test-video', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.ok) {
      window._uploadedTestVideoPath = data.path;
      label.textContent = '✓ ' + data.filename + ' ready to use';
      label.style.color = 'var(--cyan)';
      label.style.borderColor = 'rgba(87,255,196,0.4)';
    } else {
      label.textContent = '✗ ' + (data.error || 'Upload failed');
      label.style.color = 'var(--red)';
    }
  } catch(e) {
    label.textContent = '✗ ' + e.message;
    label.style.color = 'var(--red)';
  }
}
function copyLog() {
  const lines = [...document.getElementById('log').querySelectorAll('.log-line')]
    .map(el => el.textContent).join('\n');
  const btn = document.getElementById('copyLogBtn');
  function flash() {
    btn.textContent = 'copied!';
    btn.style.color = 'var(--cyan)';
    setTimeout(() => { btn.textContent = 'copy'; btn.style.color = ''; }, 2000);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(lines).then(flash).catch(() => fallbackCopy(lines, flash));
  } else { fallbackCopy(lines, flash); }
}
function fallbackCopy(text, cb) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;';
  document.body.appendChild(ta);
  ta.focus(); ta.select();
  try { document.execCommand('copy'); cb(); } catch(e) { alert('Copy failed — long press the log to copy manually'); }
  document.body.removeChild(ta);
}
function toast(msg){document.querySelectorAll('.toast').forEach(t=>t.remove());const el=document.createElement('div');el.className='toast';el.textContent=msg;document.body.appendChild(el);setTimeout(()=>el.remove(),2500);}
</script>
</body>
</html>"""


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML

@app.route("/api/run", methods=["POST"])
def api_run():
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
    if not prompt:   return jsonify({"error": "prompt is required"}), 400
    if not caption:  return jsonify({"error": "caption is required"}), 400
    if not OPENAI_API_KEY: return jsonify({"error": "OPENAI_API_KEY not set on server"}), 500
    job_id = new_job_id()
    jobs[job_id] = {"status": "starting", "logs": [], "step": 0, "video_path": None, "error": None}
    job_queues[job_id] = queue.Queue()
    threading.Thread(target=run_pipeline, args=(job_id, prompt, caption, hashtags, product_id, dry_run, model, duration), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    if job_id not in jobs:
        return jsonify({"error": "job not found"}), 404
    def generate():
        for entry in jobs[job_id].get("logs", []):
            yield f"data: {json.dumps(entry)}\n\n"
        q = job_queues.get(job_id)
        if not q: return
        while True:
            try:
                entry = q.get(timeout=30)
                yield f"data: {json.dumps(entry)}\n\n"
                if entry.get("done"): break
            except queue.Empty:
                yield 'data: {"ping":true}\n\n'
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/job/<job_id>")
def api_job(job_id):
    if job_id not in jobs: return jsonify({"error": "not found"}), 404
    j = jobs[job_id]
    return jsonify({"status": j["status"], "step": j.get("step", 0), "error": j.get("error"), "has_video": j.get("video_path") is not None})

@app.route("/api/presets", methods=["GET"])
def get_presets():
    f = Path("/tmp/presets.json")
    return jsonify(json.loads(f.read_text()) if f.exists() else [])

@app.route("/api/presets", methods=["POST"])
def save_preset():
    f = Path("/tmp/presets.json")
    presets = json.loads(f.read_text()) if f.exists() else []
    presets.append(request.get_json() or {})
    f.write_text(json.dumps(presets))
    return jsonify({"ok": True})

@app.route("/api/presets/<int:idx>", methods=["DELETE"])
def delete_preset(idx):
    f = Path("/tmp/presets.json")
    presets = json.loads(f.read_text()) if f.exists() else []
    if 0 <= idx < len(presets): presets.pop(idx)
    f.write_text(json.dumps(presets))
    return jsonify({"ok": True})



@app.route("/api/upload-test-video", methods=["POST"])
def upload_test_video():
    """Accept a user-uploaded video file for TikTok testing."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    save_path = OUTPUT_DIR / f"uploaded_test_{f.filename}"
    f.save(str(save_path))
    return jsonify({"ok": True, "path": str(save_path), "filename": f.filename})

@app.route("/api/test-tiktok", methods=["POST"])
def api_test_tiktok():
    """Test TikTok upload only — skips Sora, uses a tiny dummy video."""
    body = request.get_json() or {}
    caption    = body.get("caption", "Test post").strip()
    hashtags   = body.get("hashtags", [])
    product_id = body.get("product_id", "").strip() or None

    job_id = new_job_id()
    jobs[job_id] = {"status": "starting", "logs": [], "step": 0, "video_path": None, "error": None}
    job_queues[job_id] = queue.Queue()

    custom_video = body.get("custom_video_path", "").strip() or None

    def run_tiktok_test():
        try:
            jobs[job_id]["status"] = "running"
            push_step(job_id, 3)

            if custom_video and Path(custom_video).exists():
                video_path_to_use = Path(custom_video)
                push_log(job_id, f"Using uploaded video: {video_path_to_use.name}", "step")
            else:
                push_log(job_id, "TikTok test — creating dummy video...", "step")
                dummy_path = OUTPUT_DIR / f"dummy_{job_id}.mp4"
                dummy_path.write_bytes(_make_dummy_mp4())
                push_log(job_id, f"Dummy video created → {dummy_path.name}", "info")
                video_path_to_use = dummy_path

            jobs[job_id]["video_path"] = str(video_path_to_use)

            push_log(job_id, "Starting TikTok upload...", "step")
            try:
                from tiktok_uploader.upload import TikTokUploader
            except ImportError:
                raise RuntimeError("tiktok-uploader not installed.")

            cookies_list = [{
                'name': 'sessionid',
                'value': TIKTOK_SESSION_ID,
                'domain': '.tiktok.com',
                'path': '/',
                'expiry': 2147483647,
            }]
            hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
            full_desc = f"{caption} {hashtag_str}".strip()
            uploader = TikTokUploader(cookies_list=cookies_list, browser="chrome", headless=True)
            video_kwargs = dict(description=full_desc)
            if product_id:
                video_kwargs["product_id"] = product_id
            push_log(job_id, "Uploading via Playwright...", "info")
            uploader.upload_video(str(video_path_to_use), **video_kwargs)
            push_step(job_id, 4)
            push_log(job_id, "Posted to TikTok ✓", "success")
            jobs[job_id]["status"] = "done"
            push_log(job_id, "── TikTok test complete ──", "done")
            if job_id in job_queues:
                job_queues[job_id].put({"done": True})
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
            push_log(job_id, f"Error: {e}", "error")
            if job_id in job_queues:
                job_queues[job_id].put({"done": True, "error": str(e)})

    threading.Thread(target=run_tiktok_test, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/check-config")
def check_config():
    return jsonify({
        "OPENAI_API_KEY": "set" if OPENAI_API_KEY else "NOT SET",
        "TIKTOK_SESSION_ID": f"set ({len(TIKTOK_SESSION_ID)} chars)" if TIKTOK_SESSION_ID else "NOT SET",
    })

@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)