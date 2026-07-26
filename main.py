import os, time, threading, tempfile, subprocess, json, requests
from flask import Flask, request, jsonify

# ========= CONFIG =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
USERNAMES = [u.strip().lstrip("@") for u in os.environ["TIKTOK_USERNAMES"].split(",") if u.strip()]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "secret")

CHECK_EVERY_SEC = 60
CHECK_BATCH = 8
DELETE_CHECK_EVERY_SEC = 600   # how often to re-check old videos for deletion
DELETE_FAIL_THRESHOLD = 3      # must fail this many checks in a row before we call it deleted

STATE_DIR = "/tmp/tiktok_bot_state"
os.makedirs(STATE_DIR, exist_ok=True)

app = Flask(__name__)
STATE_LOCK = threading.Lock()

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
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"video": f},
                timeout=180
            )
    except Exception:
        pass

# ---------- Download ----------
def download_video(url):
    tmpdir = tempfile.mkdtemp()
    out = os.path.join(tmpdir, "%(id)s.%(ext)s")
    cmd = ["python3", "-m", "yt_dlp", "-o", out, "-f", "best[ext=mp4]/best", url]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        for name in os.listdir(tmpdir):
            if name.lower().endswith((".mp4", ".webm", ".mkv")):
                return os.path.join(tmpdir, name)
    except Exception:
        return None
    return None

# ---------- State ----------
# Each state entry: {"id": str, "url": str, "title": str, "deleted": bool, "fail_count": int}
def state_file(username):
    return os.path.join(STATE_DIR, f"{username}_sent.json")

def load_state(username):
    with STATE_LOCK:
        try:
            with open(state_file(username), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []

    if not data:
        return []

    # migrate older formats to the current shape
    migrated = []
    for it in data:
        if isinstance(it, str):
            # oldest format: just an id string
            migrated.append({"id": it, "url": "", "title": "", "deleted": False, "fail_count": 0})
        else:
            migrated.append({
                "id": it.get("id"),
                "url": it.get("url", ""),
                "title": it.get("title", ""),
                "deleted": it.get("deleted", False),
                "fail_count": it.get("fail_count", 0),
            })
    return migrated

def save_state(username, items):
    with STATE_LOCK:
        try:
            tmp = state_file(username) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items[-500:], f)
            os.replace(tmp, state_file(username))
        except Exception:
            pass

# ---------- Get videos ----------
def latest_items(username):
    url = f"https://www.tiktok.com/@{username}"
    cmd = ["python3", "-m", "yt_dlp", "-j", "--playlist-end", str(CHECK_BATCH), "--user-agent", "Mozilla/5.0", url]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=80, check=True)
        items = []
        for line in p.stdout.splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue

            vid_id = o.get("id")
            vid_url = o.get("webpage_url") or o.get("url")
            title = o.get("title") or ""

            if vid_id and vid_url:
                items.append({
                    "id": str(vid_id),
                    "url": vid_url,
                    "title": title
                })

        return items
    except Exception:
        return []

# ---------- Check if a single video still exists ----------
def video_still_exists(url):
    cmd = ["python3", "-m", "yt_dlp", "-j", "--user-agent", "Mozilla/5.0", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode == 0
    except Exception:
        # network hiccup / timeout - don't assume deleted
        return True

# ---------- Process account (new videos) ----------
def process_account(username):
    state = load_state(username)
    sent_set = {it["id"] for it in state}

    items = latest_items(username)
    new_items = [it for it in items if it["id"] not in sent_set]

    # Stop spam on first-ever run
    if len(state) == 0 and len(new_items) > 1:
        new_items = new_items[:1]

    for it in reversed(new_items):
        caption = f"🎬 New video from @{username}\n{it['title']}\n{it['url']}"
        path = download_video(it["url"])

        if path:
            tg_send_video(path, caption)
        else:
            tg_send_text(caption)

        state.append({
            "id": it["id"],
            "url": it["url"],
            "title": it["title"],
            "deleted": False,
            "fail_count": 0,
        })

    save_state(username, state)

# ---------- Check account (deletions) ----------
def check_deletions_for_account(username):
    state = load_state(username)
    changed = False

    for it in state:
        if it.get("deleted") or not it.get("url"):
            continue

        if video_still_exists(it["url"]):
            if it.get("fail_count", 0) != 0:
                it["fail_count"] = 0
                changed = True
            continue

        it["fail_count"] = it.get("fail_count", 0) + 1
        changed = True

        if it["fail_count"] >= DELETE_FAIL_THRESHOLD:
            it["deleted"] = True
            title = it.get("title", "")
            tg_send_text(f"🗑️ Video deleted by @{username}\n{title}\n{it['url']}")

    if changed:
        save_state(username, state)

# ---------- Worker ----------
def worker():
    tg_send_text(f"👋 Bot online. Watching: {', '.join('@'+u for u in USERNAMES)}")
    last_delete_check = 0
    while True:
        for username in USERNAMES:
            try:
                process_account(username)
            except Exception:
                tg_send_text(f"⚠️ Error on @{username}")

        if time.time() - last_delete_check >= DELETE_CHECK_EVERY_SEC:
            for username in USERNAMES:
                try:
                    check_deletions_for_account(username)
                except Exception:
                    tg_send_text(f"⚠️ Error checking deletions for @{username}")
            last_delete_check = time.time()

        time.sleep(CHECK_EVERY_SEC)

# ---------- Web ----------
@app.get("/")
def health():
    return "ok"

@app.get("/check")
def check_now():
    if request.args.get("key") != ADMIN_KEY:
        return "forbidden", 403
    for username in USERNAMES:
        process_account(username)
    return jsonify({"status": "checked"})

@app.get("/check_deletions")
def check_deletions_now():
    if request.args.get("key") != ADMIN_KEY:
        return "forbidden", 403
    for username in USERNAMES:
        check_deletions_for_account(username)
    return jsonify({"status": "checked_deletions"})

# ---------- Main ----------
if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
