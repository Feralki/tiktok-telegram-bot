import os, time, threading, tempfile, subprocess, json, requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify

# ========= CONFIG =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
USERNAMES = [u.strip().lstrip("@") for u in os.environ["TIKTOK_USERNAMES"].split(",") if u.strip()]
# Optional: comma-separated subset of USERNAMES to check more often than the rest.
PRIORITY_USERNAMES = set(
    u.strip().lstrip("@") for u in os.environ.get("PRIORITY_USERNAMES", "").split(",") if u.strip()
)
NORMAL_ACCOUNT_CYCLE_SKIP = 4  # non-priority accounts checked every ~80s (20s x 4)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "secret")

CHECK_EVERY_SEC = 20
CHECK_BATCH = 8
DELETE_CHECK_EVERY_SEC = 600     # how often to re-check old videos for deletion
DELETE_FAIL_THRESHOLD = 3        # fast path: consecutive "confirmed removed" messages
UNKNOWN_FAIL_THRESHOLD = 30      # slow path: consecutive unexplained failures (~5 hrs) before we assume deleted anyway
DELETE_CHECK_DELAY_SEC = 3       # pause between each individual video check

STATE_DIR = "/tmp/tiktok_bot_state"
os.makedirs(STATE_DIR, exist_ok=True)

app = Flask(__name__)
STATE_LOCK = threading.Lock()

# Accounts are checked in parallel so one slow account never delays the others.
ACCOUNT_EXECUTOR = ThreadPoolExecutor(max_workers=max(len(USERNAMES), 1))
# Downloads/uploads happen in the background - detection never waits on them.
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# ---------- Rate-limit warning (cooldown so we don't spam Telegram) ----------
RATE_LIMIT_WARNING_COOLDOWN_SEC = 600  # only send this warning once per 10 min max
RATE_LIMIT_PHRASES = [
    "429",
    "rate limit",
    "too many requests",
    "captcha",
    "403",
    "forbidden",
    "blocked",
]
_last_rate_limit_warning = {"ts": 0}
_rate_limit_lock = threading.Lock()

def maybe_warn_rate_limited(context, stderr_text):
    err = (stderr_text or "").lower()
    if not any(phrase in err for phrase in RATE_LIMIT_PHRASES):
        return
    with _rate_limit_lock:
        now = time.time()
        if now - _last_rate_limit_warning["ts"] < RATE_LIMIT_WARNING_COOLDOWN_SEC:
            return
        _last_rate_limit_warning["ts"] = now
    tg_send_text(
        f"⚠️ Looks like TikTok may be rate-limiting/blocking the bot ({context}). "
        f"New videos and deletion checks may be delayed until this clears."
    )

# ---------- Account-issue warning (separate from rate-limiting - this one usually needs YOU to check) ----------
ACCOUNT_ISSUE_PHRASES = [
    "account is either private or has embedding disabled",
    "unable to extract secondary user id",
    "user not found",
    "unable to find user",
    "this account is private",
    "doesn't exist",
]
ACCOUNT_ISSUE_WARNING_COOLDOWN_SEC = 3600  # once per hour per account, not every cycle
_account_issue_warnings = {}
_account_issue_lock = threading.Lock()

def maybe_warn_account_issue(username, stderr_text):
    err = (stderr_text or "").lower()
    if not any(phrase in err for phrase in ACCOUNT_ISSUE_PHRASES):
        return
    with _account_issue_lock:
        now = time.time()
        last = _account_issue_warnings.get(username, 0)
        if now - last < ACCOUNT_ISSUE_WARNING_COOLDOWN_SEC:
            return
        _account_issue_warnings[username] = now
    tg_send_text(
        f"🚨 @{username} keeps failing in a way that looks permanent (private account, "
        f"wrong username, or banned) - not just a temporary block. Worth checking this "
        f"account manually, since retrying alone probably won't fix it."
    )

# ---------- Telegram ----------
def tg_send_text(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        print(f"tg_send_text failed: {e}")

TG_SEND_VIDEO_ATTEMPTS = 3
TG_SEND_VIDEO_RETRY_DELAY_SEC = 5

def _try_send_video_once(path, caption):
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"video": f},
                timeout=180
            )
        if r.status_code != 200:
            print(f"tg_send_video non-200 response: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        print(f"tg_send_video failed: {e}")
        return False

def tg_send_video(path, caption=""):
    # Retry the upload itself a few times - a lot of "failures" here are just
    # transient network hiccups, not the video being unsendable.
    for attempt in range(1, TG_SEND_VIDEO_ATTEMPTS + 1):
        if _try_send_video_once(path, caption):
            return True
        if attempt < TG_SEND_VIDEO_ATTEMPTS:
            time.sleep(TG_SEND_VIDEO_RETRY_DELAY_SEC)
    return False

# ---------- Download ----------
# We have a ~3 min time budget, so it's worth retrying properly rather than
# giving up fast - this is the main defense against ever sending just a link.
DOWNLOAD_FORMATS = [
    "best[ext=mp4]/best",
    "best",
    "worst[ext=mp4]/worst",
]
DOWNLOAD_ATTEMPTS_PER_FORMAT = 2
DOWNLOAD_TIMEOUT_SEC = 20
DOWNLOAD_RETRY_DELAY_SEC = 3

def _try_download_once(url, fmt, timeout):
    tmpdir = tempfile.mkdtemp()
    out = os.path.join(tmpdir, "%(id)s.%(ext)s")
    cmd = ["python3", "-m", "yt_dlp", "-o", out, "-f", fmt, "--user-agent", "Mozilla/5.0", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            print(f"yt-dlp download failed for {url} with format '{fmt}' (exit {result.returncode}):\n{result.stderr}")
            maybe_warn_rate_limited(f"downloading {url}", result.stderr)
            return None
        for name in os.listdir(tmpdir):
            if name.lower().endswith((".mp4", ".webm", ".mkv")):
                return os.path.join(tmpdir, name)
        print(f"yt-dlp reported success but no video file found for {url} with format '{fmt}'")
        return None
    except subprocess.TimeoutExpired:
        print(f"download_video timed out for {url} with format '{fmt}'")
        return None
    except Exception as e:
        print(f"download_video exception for {url} with format '{fmt}': {e}")
        return None

def download_video(url):
    for fmt in DOWNLOAD_FORMATS:
        for attempt in range(1, DOWNLOAD_ATTEMPTS_PER_FORMAT + 1):
            path = _try_download_once(url, fmt, timeout=DOWNLOAD_TIMEOUT_SEC)
            if path:
                return path
            if attempt < DOWNLOAD_ATTEMPTS_PER_FORMAT:
                time.sleep(DOWNLOAD_RETRY_DELAY_SEC)
    return None

# ---------- State ----------
# Each state entry: {"id": str, "url": str, "title": str, "deleted": bool, "fail_count": int, "unknown_count": int}
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
            migrated.append({"id": it, "url": "", "title": "", "deleted": False, "fail_count": 0, "unknown_count": 0})
        else:
            migrated.append({
                "id": it.get("id"),
                "url": it.get("url", ""),
                "title": it.get("title", ""),
                "deleted": it.get("deleted", False),
                "fail_count": it.get("fail_count", 0),
                "unknown_count": it.get("unknown_count", 0),
            })
    return migrated

def save_state(username, items):
    with STATE_LOCK:
        try:
            tmp = state_file(username) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items[-500:], f)
            os.replace(tmp, state_file(username))
        except Exception as e:
            print(f"save_state failed for {username}: {e}")

# ---------- Get videos ----------
def latest_items(username):
    url = f"https://www.tiktok.com/@{username}"
    cmd = ["python3", "-m", "yt_dlp", "-j", "--playlist-end", str(CHECK_BATCH), "--user-agent", "Mozilla/5.0", url]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            print(f"latest_items failed for @{username} (exit {p.returncode}):\n{p.stderr}")
            maybe_warn_rate_limited(f"fetching @{username}'s videos", p.stderr)
            maybe_warn_account_issue(username, p.stderr)
            return []

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
    except Exception as e:
        print(f"latest_items exception for @{username}: {e}")
        return []

# ---------- Check if a single video still exists ----------
# Returns one of: "exists", "deleted", "unknown"
# "unknown" covers rate-limits/blocks/network errors - never counts as deleted.
REMOVED_PHRASES = [
    "video currently unavailable",
    "this post is unavailable",
    "content isn't available",
    "content unavailable",
    "video not available",
    "removed by the creator",
    "user's account is private",  # only if you don't already skip private accounts
]

def video_still_exists(url):
    cmd = ["python3", "-m", "yt_dlp", "-j", "--user-agent", "Mozilla/5.0", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"video_still_exists exception for {url}: {e}")
        return "unknown"  # timeout / crash - don't assume deleted

    if p.returncode == 0:
        return "exists"

    err = (p.stderr or "").lower()

    if any(phrase in err for phrase in REMOVED_PHRASES):
        return "deleted"

    # anything else (403, captcha, rate limit, empty response, unknown error)
    # is NOT proof the video is gone - just that we got blocked this time
    print(f"video_still_exists unknown result for {url}:\n{p.stderr}")
    maybe_warn_rate_limited(f"checking if {url} still exists", p.stderr)
    return "unknown"

# ---------- Process account (new videos) ----------
def handle_new_video(username, item):
    # Runs in the background - a slow download/upload here never delays
    # detection of the next video, on this account or any other.
    caption = f"🎬 New video from @{username}\n{item['title']}\n{item['url']}"
    path = download_video(item["url"])

    sent_ok = False
    if path:
        sent_ok = tg_send_video(path, caption)

    if not sent_ok:
        tg_send_text(caption)

def process_account(username):
    state = load_state(username)
    sent_set = {it["id"] for it in state}

    items = latest_items(username)
    new_items = [it for it in items if it["id"] not in sent_set]

    # Stop spam on first-ever run
    if len(state) == 0 and len(new_items) > 1:
        new_items = new_items[:1]

    if not new_items:
        return

    # Record every new video's ID immediately (so nothing gets re-detected
    # or double-sent), THEN hand the slow part (download/upload) to the
    # background pool and move on right away.
    for it in reversed(new_items):
        state.append({
            "id": it["id"],
            "url": it["url"],
            "title": it["title"],
            "deleted": False,
            "fail_count": 0,
            "unknown_count": 0,
        })
    save_state(username, state)

    for it in reversed(new_items):
        DOWNLOAD_EXECUTOR.submit(handle_new_video, username, it)

# ---------- Check account (deletions) ----------
def check_deletions_for_account(username):
    state = load_state(username)
    changed = False

    # check EVERY video not yet marked deleted, every cycle, forever -
    # same idea as how new-video polling never stops watching an account.
    # Newest first just so fresh uploads get confirmed sooner in the logs.
    candidates = [it for it in state if not it.get("deleted") and it.get("url")]
    to_check = list(reversed(candidates))

    for i, it in enumerate(to_check):
        if i > 0:
            time.sleep(DELETE_CHECK_DELAY_SEC)

        result = video_still_exists(it["url"])

        if result == "exists":
            if it.get("fail_count", 0) != 0 or it.get("unknown_count", 0) != 0:
                it["fail_count"] = 0
                it["unknown_count"] = 0
                changed = True
            continue

        if result == "deleted":
            # fast path: yt_dlp explicitly told us it's gone
            it["fail_count"] = it.get("fail_count", 0) + 1
            changed = True
            if it["fail_count"] >= DELETE_FAIL_THRESHOLD:
                it["deleted"] = True
                title = it.get("title", "")
                tg_send_text(f"🗑️ Video deleted by @{username}\n{title}\n{it['url']}")
            continue

        # result == "unknown": blocked/rate-limited/timeout/unrecognized error
        # not proof of deletion on its own, but if it NEVER resolves, we still
        # want to eventually tell the user rather than staying silent forever
        it["unknown_count"] = it.get("unknown_count", 0) + 1
        changed = True
        if it["unknown_count"] >= UNKNOWN_FAIL_THRESHOLD:
            it["deleted"] = True
            title = it.get("title", "")
            tg_send_text(
                f"🗑️ Video likely deleted by @{username} (unconfirmed - repeated errors, not a direct removal message)\n{title}\n{it['url']}"
            )

    if changed:
        save_state(username, state)

# ---------- Worker ----------
def worker():
    tg_send_text(f"👋 Bot online. Watching: {', '.join('@'+u for u in USERNAMES)}")
    last_delete_check = 0
    cycle = 0
    while True:
        # Priority accounts get checked every cycle. Everyone else only gets
        # checked every NORMAL_ACCOUNT_CYCLE_SKIP cycles, so the priority
        # account(s) get faster detection without raising total request volume.
        if PRIORITY_USERNAMES:
            to_check_this_cycle = [
                u for u in USERNAMES
                if u in PRIORITY_USERNAMES or cycle % NORMAL_ACCOUNT_CYCLE_SKIP == 0
            ]
        else:
            to_check_this_cycle = USERNAMES

        futures = [ACCOUNT_EXECUTOR.submit(process_account, u) for u in to_check_this_cycle]
        for f, username in zip(futures, to_check_this_cycle):
            try:
                f.result()
            except Exception as e:
                print(f"process_account crashed for @{username}: {e}")
                tg_send_text(f"⚠️ Error on @{username}")

        if time.time() - last_delete_check >= DELETE_CHECK_EVERY_SEC:
            del_futures = [ACCOUNT_EXECUTOR.submit(check_deletions_for_account, u) for u in USERNAMES]
            for f, username in zip(del_futures, USERNAMES):
                try:
                    f.result()
                except Exception as e:
                    print(f"check_deletions_for_account crashed for @{username}: {e}")
                    tg_send_text(f"⚠️ Error checking deletions for @{username}")
            last_delete_check = time.time()

        cycle += 1
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
