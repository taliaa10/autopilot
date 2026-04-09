# AUTOPILOT — Sora × TikTok
> Generate AI videos with Sora and auto-post to TikTok. Run from your phone.

---

## Files in this project

```
app.py              ← Flask server (the brain)
templates/index.html ← Mobile web UI
requirements.txt    ← Python dependencies
Procfile            ← How Railway starts the server
railway.json        ← Railway config
```

---

## Deploy to Railway (step by step)

### Step 1 — Put files on GitHub
1. Go to github.com → sign up free if needed
2. Click **New repository** → name it `autopilot` → Create
3. Upload all these files (drag and drop works)

### Step 2 — Deploy on Railway
1. Go to **railway.app** → sign up with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Select your `autopilot` repo
4. Railway will detect it's a Python app and start building

### Step 3 — Set environment variables
In Railway dashboard → your project → **Variables** tab, add:

| Variable | Value |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key (from platform.openai.com) |
| `TIKTOK_SESSION_ID` | Your TikTok session cookie (see below) |
| `APP_SECRET` | Any password you want (optional, protects your app) |

### Step 4 — Get your TikTok session ID
This is the cookie that lets the uploader act as you.

1. On a computer, go to tiktok.com and log in
2. Install the Chrome extension **"Get cookies.txt LOCALLY"**
3. Click the extension → find the cookie named `sessionid`
4. Copy that value → paste it as `TIKTOK_SESSION_ID` in Railway

> ⚠️ This cookie expires every ~60 days. When uploads stop working, repeat step 4.

### Step 5 — Open on your phone
1. In Railway dashboard → your project → **Settings** → copy the public URL
2. Open that URL in Safari or Chrome on your phone
3. Tap "Add to Home Screen" to make it feel like an app

---

## Using the app

1. **Fill in your Sora prompt** — describe the video you want
2. **Set your caption + hashtags** — what will appear on TikTok
3. **Add your Product ID** — find this in TikTok Shop seller dashboard
4. **Tap Run** — the server generates the video and posts it
5. **Save as preset** — for prompts you reuse every time

### To post automatically every day
- Toggle **"Schedule daily"** → set your time (e.g. 09:00)
- Keep the browser tab open on your phone (or leave it running)

---

## Updating your TikTok session (every ~60 days)

1. Go to Railway → Variables
2. Update `TIKTOK_SESSION_ID` with your fresh cookie
3. Railway redeploys automatically

---

## Cost estimate

| Service | Cost |
|---|---|
| Railway | Free tier available (~$5/mo for always-on) |
| Sora (sora-2, 10s video) | ~$0.50–$1 per video |
| Sora (sora-2-pro, 10s video) | ~$5 per video |
