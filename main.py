import os, time, threading, tempfile, subprocess, json, requests
from flask import Flask, request, jsonify

# ========= CONFIG FROM ENV =========
BOT_TOKEN = os.environ["BOT_TOKEN"]            # Telegram bot token
CHAT_ID = os.environ["CHAT_ID"]                # Your Telegram chat ID
USERNAME = os.environ["TIKTOK_USERNAME"]       # TikTok username (no @)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "secret")

# ========= CONSTANTS =========
LAST_FILE = "/var/data/last_id.txt"   # IMPORTANT: persistent storage
os.makedirs("/var/data", exist_ok=True)

CHECK_EVERY_SEC = 60
CHECK_BATCH = 5

app = Flask(__name__)

# ---------- helpers ----------
def read_last():
    try:
        with open(LAST_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def write_last(v):
    with open(LAST_FILE, "w", encoding="utf-8") as f:
        f.write(v or "")

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

# ---------- detection ----------
def latest_items_via_ytdlp(username, n=CHECK_BATCH):
    url = f"https://www.tiktok.com/@{username}"
    cmd = ["python3", "-m", "yt_dlp", "-j", "--playlist-end", str(n), url]

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
        return []

# ---------- main logic ----------
def process_once():
    last = read_last()
    items = latest_items_via_ytdlp(USERNAME)

    def vid_id(u):
        if not u:
            return ""
        u = u.split("?", 1)[0]
        if "/video/" in u:
            return u.split("/video/")[-1].split("/")[0]
        return u.rstrip("/").split("/")[-1]

    unseen = []
    for link, title in items:
        v = vid_id(link)
        if not v:
            continue
        if last and v == last:
            break
        unseen.append((link, title, v))

    if not last and unseen:
        unseen = unseen[:1]

    for link, title, v in reversed(unseen):
        caption = f"🎬 New video from @{USERNAME}\n{title}\n{link}"
        path = download_video(link)
        if path:
            try:
                tg_send_video(path, caption)
            except Exception:
                tg_send_text(caption)
        else:
            tg_send_text(caption)
        write_last(v)

# ---------- background loop ----------
def worker():
    tg_send_text(f"👋 Bot online. Watching @{USERNAME}.")
    while True:
        try:
            process_once()
        except Exception as e:
            tg_send_text(f"⚠️ Error: {e}")
        time.sleep(CHECK_EVERY_SEC)

# ---------- web ----------
@app.get("/")
def health():
    return "ok"

@app.get("/check")
def check():
    if request.args.get("key") != ADMIN_KEY:
        return "forbidden", 403
    process_once()
    return jsonify({"status": "ok"})

# ---------- start ----------
if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
