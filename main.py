import os, time, threading, tempfile, subprocess, json, requests, re
from flask import Flask, request, jsonify
from urllib.parse import urlparse

# ========= CONFIG FROM ENV =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
USERNAME = os.environ["TIKTOK_USERNAME"]       # no @
ADMIN_KEY = os.environ.get("ADMIN_KEY", "secret")

# ========= CONSTANTS (FREE RENDER SAFE) =========
CHECK_EVERY_SEC = 60
CHECK_BATCH = 7                 # scan a few more than 5 for safety

# On free Render you DON'T have a persistent disk.
# /tmp is the best place to store "memory" while the service stays alive.
STATE_DIR = "/tmp/tiktok_bot_state"
os.makedirs(STATE_DIR, exist_ok=True)

SENT_FILE = os.path.join(STATE_DIR, "sent_ids.json")  # stores many IDs (prevents spam)
MAX_SENT_IDS = 400                                    # keep last 400 IDs

app = Flask(__name__)

# ---------- Telegram ----------
def tg_send_text(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15
        )
    except Exception:
        pass

def tg_send_video(path, caption=""):
    with open(path, "rb") as f:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
            data={"chat_id": CHAT_ID, "caption": caption},
            files={"video": f},
            timeout=180
        )

# ---------- State (many IDs, not just one) ----------
def load_sent_ids():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def save_sent_ids(ids_list):
    try:
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(ids_list[-MAX_SENT_IDS:], f)
    except Exception:
        pass

# ---------- Video ID extraction (important) ----------
def extract_video_id(url: str) -> str:
    if not url:
        return ""
    # remove query params so IDs match consistently
    base = url.split("?", 1)[0]

    # standard tiktok format: /video/1234567890
    m = re.search(r"/video/(\d+)", base)
    if m:
        return m.group(1)

    # fallback: last path segment
    path = urlparse(base).path.rstrip("/")
    last = path.split("/")[-1]
    return last if last and last != "@" else ""

# ---------- Download (yt-dlp) ----------
def download_video(url):
    tmpdir = tempfile.mkdtemp()
    out = os.path.join(tmpdir, "%(id)s.%(ext)s")
    cmd = ["python3", "-m", "yt_dlp", "-o", out, "-f", "best[ext=mp4]/best", url]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name in os.listdir(tmpdir):
            if name.lower().endswith((".mp4", ".webm", ".mkv")):
                return os.path.join(tmpdir, name)
    except Exception:
        return None
    return None

# ---------- Get latest items ----------
def latest_items_via_ytdlp(username, n=CHECK_BATCH):
    url = f"https://www.tiktok.com/@{username}"
    cmd = ["python3", "-m", "yt_dlp", "-j", "--playlist-end", str(n), "--user-agent", "Mozilla/5.0", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=80, check=True)
        items = []
        for line in p.stdout.splitlines():
            try:
                o = json.loads(line)
                vid_url = o.get("webpage_url") or o.get("url")
                title = o.get("title") or ""
                if vid_url:
                    items.append((vid_url, title))
            except json.JSONDecodeError:
                continue
        return items[:n]
    except Exception:
        return fallback_latest_items(username, n=n)

def fallback_latest_items(username, n=CHECK_BATCH):
    # best-effort fallback if yt-dlp listing fails
    try:
        r = requests.get(f"https://tiktokapi.dev/api/feed/{username}", timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        raw = data if isinstance(data, list) else data.get("data", [])
        items = []
        for it in raw:
            link = it.get("url") or it.get("share_url") or it.get("webpage_url") or it.get("link")
            title = it.get("title") or it.get("desc") or ""
            if link:
                items.append((link, title))
            if len(items) >= n:
                break
        return items
    except Exception:
        return []

# ---------- One scan ----------
def process_once(verbose=False):
    sent_ids = load_sent_ids()
    sent_set = set(sent_ids)

    items = latest_items_via_ytdlp(USERNAME, n=CHECK_BATCH)
    if not items:
        if verbose:
            tg_send_text("ℹ️ No items returned this pass.")
        return {"found": 0, "sent": 0}

    new_items = []
    for link, title in items:
        vid = extract_video_id(link)
        if not vid:
            continue
        if vid in sent_set:
            continue
        new_items.append((link, title, vid))

    # Safety: if bot "forgot" (fresh start) don't dump many videos at once
    # (prevents spam on restarts)
    if len(sent_ids) == 0 and len(new_items) > 1:
        new_items = new_items[:1]

    # Send oldest-first
    sent_count = 0
    for link, title, vid in reversed(new_items):
        caption = f"🎬 New video from @{USERNAME}\n{title}\nOriginal: {link}"
        path = download_video(link)
        if path:
            try:
                tg_send_video(path, caption=caption)
            except Exception:
                tg_send_text(caption)
        else:
            tg_send_text(caption)

        sent_ids.append(vid)
        sent_set.add(vid)
        sent_count += 1

    save_sent_ids(sent_ids)

    if verbose:
        tg_send_text(f"ℹ️ Scan: found={len(items)}, new={len(new_items)}, sent={sent_count}")

    return {"found": len(items), "new": len(new_items), "sent": sent_count}

# ---------- Background loop ----------
def worker():
    tg_send_text(f"👋 Bot online. Watching @{USERNAME} every {CHECK_EVERY_SEC}s.")
    while True:
        try:
            process_once(verbose=False)
        except Exception as e:
            # keep message short to avoid floods if something breaks
            tg_send_text(f"⚠️ Worker error: {type(e).__name__}")
        time.sleep(CHECK_EVERY_SEC)

# ---------- Web endpoints ----------
@app.get("/")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.get("/check")
def check_now():
    if request.args.get("key") != ADMIN_KEY:
        return "forbidden", 403
    info = process_once(verbose=True)
    return jsonify(info)

# ---------- Main ----------
if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
