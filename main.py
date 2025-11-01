import os, time, threading, tempfile, subprocess, json, requests
from flask import Flask, request, jsonify

# ====== CONFIG FROM ENV ======
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
USERNAME = os.environ["TIKTOK_USERNAME"]          # no @
ADMIN_KEY = os.environ.get("ADMIN_KEY", "secret") # for /check endpoint

# ====== CONSTANTS ======
LAST_FILE = "last_id.txt"          # remembers last seen video id
CHECK_EVERY_SEC = 60               # minute polling
CHECK_BATCH = 5                    # how many latest posts to inspect (backfill safety)

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
            json={"chat_id": CHAT_ID, "text": text},
            timeout=15
        )
    except Exception:
        pass

def tg_send_video(path, caption=""):
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"video": f},
                timeout=180
            )
    except Exception:
        # if Telegram refuses (e.g., >50MB), at least send the link-only caption
        raise

def download_video(url):
    """
    Download best mp4 with yt-dlp. Returns path or None.
    """
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

def latest_items_via_ytdlp(username, n=CHECK_BATCH):
    """
    Query TikTok directly (no RSS). Returns up to n newest (url, title), newest-first.
    Uses yt-dlp JSON dump (one JSON line per item) without downloading media.
    """
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
    except Exception as e:
        tg_send_text(f"⚠️ yt-dlp error: {e}")
        return []

def process_once(verbose=False):
    """
    One detection+send pass.
    - Looks at latest N posts
    - Sends any unseen, oldest->newest
    Returns a dict summary.
    """
    last = read_last()
    items = latest_items_via_ytdlp(USERNAME, n=CHECK_BATCH)

    info = {"last_known": last, "found": len(items), "will_send": 0, "sent": 0}
    if not items:
        if verbose: tg_send_text("ℹ️ No items returned this pass.")
        return info

    def vid_id(u): return u.rstrip("/").split("/")[-1] if u else ""

    # build list of unseen since 'last'
    unseen = []
    for link, title in items:
        v = vid_id(link)
        if last and v == last:
            break
        unseen.append((link, title, v))

    # first time ever: only send the newest one to avoid spam
    if not last and unseen:
        unseen = unseen[:1]

    info["will_send"] = len(unseen)

    # send oldest->newest so chat order is natural
    for link, title, v in reversed(unseen):
        caption = f"🎬 New video from @{USERNAME}\n{title}\nOriginal: {link}"
        path = download_video(link)
        if path:
            try:
                tg_send_video(path, caption=caption)
            except Exception:
                tg_send_text(caption)  # fallback to link if Telegram rejects (size etc.)
        else:
            tg_send_text(caption)
        write_last(v)
        info["sent"] += 1
        last = v

    if verbose:
        tg_send_text(f"ℹ️ Scan: found={info['found']}, will_send={info['will_send']}, sent={info['sent']}")
    return info

# ---------- background loop ----------
def worker():
    tg_send_text(f"👋 Bot online. Watching @{USERNAME} every {CHECK_EVERY_SEC}s.")
    while True:
        try:
            process_once(verbose=False)
        except Exception as e:
            tg_send_text(f"⚠️ worker error: {e}")
        time.sleep(CHECK_EVERY_SEC)

# ---------- web endpoints ----------
@app.get("/")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

# Force a scan now from your browser: /check?key=ADMIN_KEY
@app.get("/check")
def check_now():
    if request.args.get("key") != ADMIN_KEY:
        return "forbidden", 403
    info = process_once(verbose=True)
    return jsonify(info)

# ---------- main ----------
if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
