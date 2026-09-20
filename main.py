"""
TikTok -> Telegram bot (single file, made for Render free tier)

What it does:
  1. Checks each TikTok account every CHECK_EVERY_SEC seconds.
  2. When a new video appears, sends the LINK to Telegram immediately,
     then downloads the video and uploads it as a follow-up.
     If the video can't be delivered, you still have the link.
  3. Every 10 minutes it re-checks videos it announced and tells you
     if any were deleted.

Required environment variables (Render -> Environment):
  BOT_TOKEN, CHAT_ID, ADMIN_KEY, TIKTOK_USERNAMES (comma separated)

Optional environment variables (change these instead of editing code):
  CHECK_EVERY_SEC     default 60   seconds between checks of each account
  CHECK_BATCH         default 8    how many latest videos to look at per check
  CHECK_CONCURRENCY   default 2    yt-dlp account checks running at once
  BACKGROUND_CONCURRENCY default 1 yt-dlp downloads/deletion checks at once
  FLAT_PLAYLIST       default 0    set to 1 to test a faster listing mode

requirements.txt should contain:
  flask
  requests
  yt-dlp

Render start command must be:  python main.py
(not gunicorn, or the background workers never start)
"""

import os
import time
import json
import shutil
import hmac
import tempfile
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, request, jsonify


# ============================================================
# CONFIG
# ============================================================

def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
ADMIN_KEY = os.environ["ADMIN_KEY"]

USERNAMES = [
    u.strip().lstrip("@")
    for u in os.environ["TIKTOK_USERNAMES"].split(",")
    if u.strip()
]

# --- Checking for new videos ---
CHECK_EVERY_SEC = env_int("CHECK_EVERY_SEC", 60)
CHECK_BATCH = env_int("CHECK_BATCH", 8)
ACCOUNT_CHECK_TIMEOUT_SEC = 120
USE_FLAT_PLAYLIST = os.environ.get("FLAT_PLAYLIST", "0") == "1"

# If one check finds more "new" videos than this, it's almost
# certainly a backlog, not real fresh posts. Only the newest
# few are announced; the rest are silently recorded.
MAX_NEW_ITEMS_PER_CYCLE = 3

# --- yt-dlp concurrency ---
# Account checks get their own slots so they never wait behind
# downloads or deletion checks (that's what keeps detection fast).
CHECK_CONCURRENCY = env_int("CHECK_CONCURRENCY", 2)
BACKGROUND_CONCURRENCY = env_int("BACKGROUND_CONCURRENCY", 1)

# --- Delivery of the actual video ---
DOWNLOAD_WORKERS = 2
UPLOAD_WORKERS = 2
DOWNLOAD_TIMEOUT_SEC = 60
DOWNLOAD_ATTEMPTS_PER_JOB = 2
DOWNLOAD_QUEUE_WAIT_TIMEOUT_SEC = 300
UPLOAD_TIMEOUT_SEC = 180
UPLOAD_ATTEMPTS_PER_JOB = 2
UPLOAD_QUEUE_WAIT_TIMEOUT_SEC = 300
DELIVERY_RETRY_DELAY_SEC = 30
DELIVERY_RETRY_SCAN_SEC = 30

# After this many failed full delivery attempts, stop retrying
# and just tell you the link is all you get for that video.
MAX_DELIVERY_ATTEMPTS = 5

# Telegram bots cannot upload files bigger than this.
TELEGRAM_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# --- Deletion checking ---
DELETE_CHECK_EVERY_SEC = 600
DELETE_CHECK_MAX_PER_CYCLE = 10
DELETE_CHECK_DELAY_SEC = 3
DELETE_FAIL_THRESHOLD = 3
UNKNOWN_FAIL_THRESHOLD = 30

# --- State ---
# NOTE: /tmp is wiped by Render on redeploy/restart/spin-down.
# When that happens the bot re-seeds silently (see process_account).
STATE_DIR = "/tmp/tiktok_bot_state"
STATE_MAX_ITEMS = 500

os.makedirs(STATE_DIR, exist_ok=True)


# ============================================================
# APP / LOCKS / EXECUTORS
# ============================================================

app = Flask(__name__)

# Only one normal check per account at a time.
ACCOUNT_LOCKS = {u: threading.Lock() for u in USERNAMES}

# Only one deletion sweep per account at a time. This is
# separate from ACCOUNT_LOCKS on purpose: a deletion sweep must
# never block new-video checks for that account.
DELETION_LOCKS = {u: threading.Lock() for u in USERNAMES}

# Protects reading/writing each account's state file.
STATE_LOCKS = {u: threading.RLock() for u in USERNAMES}

# delivery_job runs on its own thread and only submits the
# actual download/upload to these executors (avoids deadlock).
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=DOWNLOAD_WORKERS, thread_name_prefix="download"
)
UPLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=UPLOAD_WORKERS, thread_name_prefix="upload"
)

DELIVERY_QUEUED = set()
DELIVERY_QUEUED_LOCK = threading.Lock()

CHECK_SEMAPHORE = threading.Semaphore(CHECK_CONCURRENCY)
BACKGROUND_SEMAPHORE = threading.Semaphore(BACKGROUND_CONCURRENCY)


# ============================================================
# TELEGRAM
# ============================================================

def tg_send_text(text):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not response.ok:
            print(
                f"[telegram] sendMessage HTTP {response.status_code}: "
                f"{response.text[:200]}",
                flush=True,
            )
        return response.ok
    except Exception as e:
        print(f"[telegram] send text failed: {e}", flush=True)
        return False


def tg_send_video(path, caption=""):
    try:
        with open(path, "rb") as f:
            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"video": f},
                timeout=UPLOAD_TIMEOUT_SEC,
            )
        if not response.ok:
            print(
                f"[telegram] sendVideo HTTP {response.status_code}: "
                f"{response.text[:200]}",
                flush=True,
            )
        return response.ok
    except Exception as e:
        print(f"[telegram] send video failed: {e}", flush=True)
        return False


# ============================================================
# VIDEO DOWNLOAD
# ============================================================

def download_video(url):
    tmpdir = tempfile.mkdtemp()
    out = os.path.join(tmpdir, "%(id)s.%(ext)s")

    cmd = [
        "python3", "-m", "yt_dlp",
        "-o", out,
        "-f", "best[ext=mp4]/best",
        "--user-agent", "Mozilla/5.0",
        url,
    ]

    try:
        with BACKGROUND_SEMAPHORE:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOWNLOAD_TIMEOUT_SEC,
            )

        for name in os.listdir(tmpdir):
            if name.lower().endswith((".mp4", ".webm", ".mkv")):
                return os.path.join(tmpdir, name)

    except subprocess.TimeoutExpired:
        print(f"[download] timeout after {DOWNLOAD_TIMEOUT_SEC}s", flush=True)
    except Exception as e:
        print(f"[download] failed: {e}", flush=True)

    shutil.rmtree(tmpdir, ignore_errors=True)
    return None


# ============================================================
# STATE
# ============================================================

def state_file(username):
    return os.path.join(STATE_DIR, f"{username}_sent.json")


def make_entry(video_id, url="", title="", video_sent=False,
               last_delete_check=0.0, announced=True):
    """
    announced=False means the video was recorded silently (seeded
    or overflow) and you were never told about it, so it is also
    excluded from deletion alerts.
    """
    return {
        "id": video_id,
        "url": url,
        "title": title,
        "deleted": False,
        "fail_count": 0,
        "unknown_count": 0,
        "video_sent": video_sent,
        "delivery_retry_after": 0,
        "delivery_attempts": 0,
        "last_delete_check": last_delete_check,
        "announced": announced,
    }


def _load_state_unlocked(username):
    try:
        with open(state_file(username), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[state] load failed for @{username}: {e}", flush=True)
        return []

    if not data:
        return []

    migrated = []

    for item in data:
        # Oldest format: just a list of id strings.
        if isinstance(item, str):
            migrated.append(make_entry(item, video_sent=True))
            continue

        if not isinstance(item, dict) or not item.get("id"):
            continue

        entry = make_entry(
            item["id"],
            item.get("url", ""),
            item.get("title", ""),
            video_sent=item.get("video_sent", True),
            last_delete_check=item.get("last_delete_check", 0),
            announced=item.get("announced", True),
        )

        for key in (
            "deleted", "fail_count", "unknown_count",
            "delivery_retry_after", "delivery_attempts",
        ):
            if key in item:
                entry[key] = item[key]

        migrated.append(entry)

    return migrated


def _save_state_unlocked(username, items):
    try:
        path = state_file(username)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[-STATE_MAX_ITEMS:], f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[state] save failed for @{username}: {e}", flush=True)


def get_entry(username, video_id):
    with STATE_LOCKS[username]:
        for entry in _load_state_unlocked(username):
            if entry.get("id") == video_id:
                return dict(entry)
    return None


# ============================================================
# TIKTOK LOOKUP
# ============================================================

def latest_items(username, timing=None):
    """
    Lists the newest videos for an account.

    If a dict is passed as `timing`, it is filled with:
      waited  - seconds spent queued for a yt-dlp slot
      ran     - seconds yt-dlp actually ran
      status  - "ok", "timeout" or "failed"
    """
    if timing is None:
        timing = {}

    url = f"https://www.tiktok.com/@{username}"

    cmd = ["python3", "-m", "yt_dlp", "-j"]
    if USE_FLAT_PLAYLIST:
        cmd.append("--flat-playlist")
    cmd += [
        "--playlist-end", str(CHECK_BATCH),
        "--user-agent", "Mozilla/5.0",
        url,
    ]

    queued_at = time.time()
    start = queued_at

    try:
        with CHECK_SEMAPHORE:
            start = time.time()
            timing["waited"] = start - queued_at

            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=ACCOUNT_CHECK_TIMEOUT_SEC,
                check=True,
            )

        timing["ran"] = time.time() - start

        items = []

        for line in p.stdout.splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue

            vid_id = obj.get("id")
            vid_url = obj.get("webpage_url") or obj.get("url")
            title = obj.get("title") or ""

            if vid_id and vid_url:
                items.append({
                    "id": str(vid_id),
                    "url": vid_url,
                    "title": title,
                })

        timing["status"] = "ok"

        print(
            f"[check] @{username}: {len(items)} videos, "
            f"waited {timing['waited']:.1f}s, ran {timing['ran']:.1f}s",
            flush=True,
        )

        return items

    except subprocess.TimeoutExpired:
        timing["ran"] = time.time() - start
        timing["status"] = "timeout"
        print(
            f"[check] @{username}: timeout after "
            f"{timing['ran']:.1f}s of running "
            f"(waited {timing.get('waited', 0):.1f}s)",
            flush=True,
        )
        return []

    except Exception as e:
        timing["ran"] = time.time() - start
        timing["status"] = "failed"

        detail = str(e)
        stderr = getattr(e, "stderr", "") or ""
        if stderr.strip():
            detail = stderr.strip().splitlines()[-1][:200]

        print(
            f"[check] @{username}: failed after "
            f"{timing['ran']:.1f}s: {detail}",
            flush=True,
        )
        return []


# ============================================================
# DELETION CHECK
# ============================================================

REMOVED_PHRASES = [
    "video currently unavailable",
    "this post is unavailable",
    "content isn't available",
    "content unavailable",
    "video not available",
    "removed by the creator",
]


def video_still_exists(url):
    """Returns "exists", "deleted" or "unknown"."""
    cmd = [
        "python3", "-m", "yt_dlp",
        "-j",
        "--user-agent", "Mozilla/5.0",
        url,
    ]

    try:
        with BACKGROUND_SEMAPHORE:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
    except Exception:
        return "unknown"

    if p.returncode == 0:
        return "exists"

    err = ((p.stderr or "") + "\n" + (p.stdout or "")).lower()

    if any(phrase in err for phrase in REMOVED_PHRASES):
        return "deleted"

    # Private accounts, rate limits, blocks, etc. are not proof
    # that the video was deleted.
    return "unknown"


def apply_deletion_result(entry, result, username):
    """Updates the entry in place. Returns an alert message or None."""
    entry["last_delete_check"] = time.time()

    title = entry.get("title", "")
    url = entry.get("url", "")

    if result == "exists":
        entry["fail_count"] = 0
        entry["unknown_count"] = 0
        return None

    if result == "deleted":
        entry["fail_count"] = entry.get("fail_count", 0) + 1

        if entry["fail_count"] >= DELETE_FAIL_THRESHOLD:
            entry["deleted"] = True
            print(f"[delete] @{username} {entry['id']}: confirmed deleted",
                  flush=True)
            return f"🗑️ Video deleted by @{username}\n{title}\n{url}"

        return None

    # result == "unknown"
    entry["unknown_count"] = entry.get("unknown_count", 0) + 1

    if entry["unknown_count"] >= UNKNOWN_FAIL_THRESHOLD:
        entry["deleted"] = True
        print(f"[delete] @{username} {entry['id']}: marked likely deleted "
              f"after repeated unknown errors", flush=True)
        return (
            f"🗑️ Video likely deleted by @{username} "
            f"(unconfirmed - repeated errors, not a direct removal "
            f"message)\n{title}\n{url}"
        )

    return None


# ============================================================
# DELIVERY
# ============================================================

def queue_delivery(username, video_id):
    key = (username, video_id)

    with DELIVERY_QUEUED_LOCK:
        if key in DELIVERY_QUEUED:
            return False
        DELIVERY_QUEUED.add(key)

    try:
        threading.Thread(
            target=delivery_job,
            args=(username, video_id),
            daemon=True,
            name=f"delivery-{username}-{video_id}",
        ).start()
        return True
    except Exception:
        with DELIVERY_QUEUED_LOCK:
            DELIVERY_QUEUED.discard(key)
        return False


def record_delivery_failure(username, video_id, permanent=False):
    """
    Records a failed delivery. Returns one of:
      "deleted"  - video was deleted, nothing more to do
      "gave_up"  - too many failures (or permanent); stop retrying
      "retry"    - a retry has been scheduled
      "missing"  - state entry not found
    """
    with STATE_LOCKS[username]:
        state = _load_state_unlocked(username)

        for entry in state:
            if entry.get("id") != video_id:
                continue

            if entry.get("deleted"):
                return "deleted"

            entry["delivery_attempts"] = entry.get("delivery_attempts", 0) + 1

            if permanent or entry["delivery_attempts"] >= MAX_DELIVERY_ATTEMPTS:
                # video_sent=True just means "stop retrying".
                entry["video_sent"] = True
                entry["delivery_retry_after"] = 0
                outcome = "gave_up"
            else:
                entry["video_sent"] = False
                entry["delivery_retry_after"] = (
                    time.time() + DELIVERY_RETRY_DELAY_SEC
                )
                outcome = "retry"

            _save_state_unlocked(username, state)
            return outcome

    return "missing"


def handle_delivery_failure(username, video_id, url, reason, permanent=False):
    outcome = record_delivery_failure(username, video_id, permanent)

    if outcome == "deleted":
        print(f"[delivery] @{username} {video_id}: deleted; delivery stopped",
              flush=True)

    elif outcome == "gave_up":
        print(f"[delivery] @{username} {video_id}: giving up ({reason})",
              flush=True)
        tg_send_text(
            f"⚠️ Couldn't deliver the video from @{username} ({reason}). "
            f"You have the link:\n{url}"
        )

    elif outcome == "retry":
        print(f"[delivery] @{username} {video_id}: {reason}; retry scheduled",
              flush=True)

    else:
        print(f"[delivery] @{username} {video_id}: state entry missing",
              flush=True)


def mark_delivered(username, video_id):
    with STATE_LOCKS[username]:
        state = _load_state_unlocked(username)

        for entry in state:
            if entry.get("id") == video_id:
                if entry.get("deleted"):
                    return False
                entry["video_sent"] = True
                entry["delivery_retry_after"] = 0
                _save_state_unlocked(username, state)
                return True

    return False


def delivery_job(username, video_id):
    key = (username, video_id)
    path = None

    try:
        item = get_entry(username, video_id)

        if item is None:
            print(f"[delivery] @{username} {video_id}: state entry missing",
                  flush=True)
            return

        if item.get("deleted"):
            print(f"[delivery] @{username} {video_id}: deleted; "
                  f"delivery stopped", flush=True)
            return

        if item.get("video_sent"):
            return

        url = item.get("url", "")
        title = item.get("title", "")

        if not url:
            print(f"[delivery] @{username} {video_id}: no URL", flush=True)
            return

        # ---------------- Download ----------------
        for attempt in range(1, DOWNLOAD_ATTEMPTS_PER_JOB + 1):
            start = time.time()

            print(f"[delivery] @{username} {video_id}: download attempt "
                  f"{attempt}/{DOWNLOAD_ATTEMPTS_PER_JOB}", flush=True)

            future = DOWNLOAD_EXECUTOR.submit(download_video, url)

            try:
                path = future.result(timeout=DOWNLOAD_QUEUE_WAIT_TIMEOUT_SEC)
            except Exception as e:
                print(f"[delivery] @{username} {video_id}: download "
                      f"exception: {type(e).__name__}: {e}", flush=True)
                path = None

            elapsed = time.time() - start

            if path:
                print(f"[delivery] @{username} {video_id}: download success "
                      f"in {elapsed:.1f}s", flush=True)
                break

            print(f"[delivery] @{username} {video_id}: download failed "
                  f"after {elapsed:.1f}s", flush=True)

            if attempt < DOWNLOAD_ATTEMPTS_PER_JOB:
                time.sleep(1)

        if not path:
            handle_delivery_failure(username, video_id, url,
                                    "download failed")
            return

        # Telegram bots can't upload files over 50 MB. Retrying
        # would never help, so stop right away.
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0

        if size > TELEGRAM_MAX_UPLOAD_BYTES:
            handle_delivery_failure(
                username, video_id, url,
                f"file is {size / 1024 / 1024:.0f} MB, over Telegram's "
                f"50 MB bot limit",
                permanent=True,
            )
            return

        # ---------------- Upload ----------------
        caption = (
            f"🎬 New video from @{username}\n"
            f"{title}\n"
            f"{url}"
        )

        uploaded = False

        for attempt in range(1, UPLOAD_ATTEMPTS_PER_JOB + 1):
            current = get_entry(username, video_id)

            if current is None or current.get("deleted"):
                print(f"[delivery] @{username} {video_id}: deleted or "
                      f"missing before upload; delivery stopped", flush=True)
                return

            start = time.time()

            print(f"[delivery] @{username} {video_id}: upload attempt "
                  f"{attempt}/{UPLOAD_ATTEMPTS_PER_JOB}", flush=True)

            future = UPLOAD_EXECUTOR.submit(tg_send_video, path, caption)

            try:
                uploaded = future.result(timeout=UPLOAD_QUEUE_WAIT_TIMEOUT_SEC)
            except Exception as e:
                print(f"[delivery] @{username} {video_id}: upload "
                      f"exception: {e}", flush=True)
                uploaded = False

            elapsed = time.time() - start

            if uploaded:
                print(f"[delivery] @{username} {video_id}: Telegram upload "
                      f"finished in {elapsed:.1f}s", flush=True)
                break

            print(f"[delivery] @{username} {video_id}: upload failed "
                  f"after {elapsed:.1f}s", flush=True)

            if attempt < UPLOAD_ATTEMPTS_PER_JOB:
                time.sleep(1)

        if not uploaded:
            handle_delivery_failure(username, video_id, url, "upload failed")
            return

        mark_delivered(username, video_id)

        print(f"[delivery] @{username} {video_id}: delivery complete",
              flush=True)

    except Exception as e:
        print(f"[delivery] @{username} {video_id}: unexpected error: {e}",
              flush=True)

    finally:
        if path:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

        with DELIVERY_QUEUED_LOCK:
            DELIVERY_QUEUED.discard(key)


# ============================================================
# NORMAL ACCOUNT CHECK
# ============================================================

def process_account(username):
    if not ACCOUNT_LOCKS[username].acquire(blocking=False):
        print(f"[check] @{username}: previous check still running; "
              f"skipping this cycle", flush=True)
        return

    try:
        items = latest_items(username)

        if not items:
            return

        with STATE_LOCKS[username]:
            state = _load_state_unlocked(username)

            # ------------------------------------------------
            # Empty state = first run for this account, or
            # Render wiped /tmp. Record what's already there
            # silently so a restart doesn't replay old videos.
            # (Anything posted while the bot was down is
            # skipped - that's the tradeoff.)
            # ------------------------------------------------
            if not state:
                now = time.time()

                for item in items:
                    state.append(make_entry(
                        item["id"], item["url"], item["title"],
                        video_sent=True,
                        last_delete_check=now,
                        announced=False,
                    ))

                _save_state_unlocked(username, state)

                print(f"[new] @{username}: no saved state; silently "
                      f"recorded {len(items)} existing videos, "
                      f"announced none", flush=True)
                return

            known = {str(e.get("id")) for e in state if e.get("id")}

            new_items = [i for i in items if i["id"] not in known]

            # Backlog guard: announce only the newest few.
            if len(new_items) > MAX_NEW_ITEMS_PER_CYCLE:
                overflow = new_items[MAX_NEW_ITEMS_PER_CYCLE:]
                new_items = new_items[:MAX_NEW_ITEMS_PER_CYCLE]

                print(f"[new] @{username}: {len(overflow) + len(new_items)} "
                      f"items appeared new at once (likely backlog); "
                      f"announcing only the {len(new_items)} newest",
                      flush=True)

                now = time.time()

                for item in overflow:
                    state.append(make_entry(
                        item["id"], item["url"], item["title"],
                        video_sent=True,
                        last_delete_check=now,
                        announced=False,
                    ))

                _save_state_unlocked(username, state)

        # Send links (oldest first). Network calls happen
        # outside the state lock.
        for item in reversed(new_items):
            video_id = item["id"]
            url = item["url"]
            title = item["title"]

            caption = (
                f"🎬 New video from @{username}\n"
                f"{title}\n"
                f"{url}"
            )

            t0 = time.time()

            if not tg_send_text(caption):
                print(f"[new] @{username} {video_id}: link send failed "
                      f"after {time.time() - t0:.1f}s; will retry",
                      flush=True)
                continue

            print(f"[new] @{username} {video_id}: link sent in "
                  f"{time.time() - t0:.1f}s", flush=True)

            already_saved = False

            with STATE_LOCKS[username]:
                state = _load_state_unlocked(username)

                if any(e.get("id") == video_id for e in state):
                    already_saved = True
                else:
                    state.append(make_entry(
                        video_id, url, title,
                        video_sent=False,
                        last_delete_check=time.time(),
                    ))
                    _save_state_unlocked(username, state)

            if already_saved:
                continue

            queue_delivery(username, video_id)

    finally:
        ACCOUNT_LOCKS[username].release()


# ============================================================
# DELETION CHECK FOR ONE ACCOUNT
# ============================================================

def check_deletions_for_account(username):
    if not DELETION_LOCKS[username].acquire(blocking=False):
        print(f"[delete] @{username}: sweep already running; skipping",
              flush=True)
        return

    try:
        with STATE_LOCKS[username]:
            state = _load_state_unlocked(username)

            # Only videos you were actually told about.
            candidates = [
                dict(e) for e in state
                if not e.get("deleted")
                and e.get("url")
                and e.get("announced", True)
            ]

        candidates.sort(key=lambda e: e.get("last_delete_check", 0))
        candidates = candidates[:DELETE_CHECK_MAX_PER_CYCLE]

        for index, item in enumerate(candidates):
            last = item.get("last_delete_check", 0)

            # Don't check a video that was only just posted/checked.
            if last and time.time() - last < 120:
                continue

            result = video_still_exists(item["url"])

            alert = None

            with STATE_LOCKS[username]:
                state = _load_state_unlocked(username)

                for entry in state:
                    if entry.get("id") == item["id"]:
                        alert = apply_deletion_result(
                            entry, result, username
                        )
                        _save_state_unlocked(username, state)
                        break

            if alert:
                tg_send_text(alert)

            if index < len(candidates) - 1:
                time.sleep(DELETE_CHECK_DELAY_SEC)

    finally:
        DELETION_LOCKS[username].release()


# ============================================================
# BACKGROUND WORKERS
# ============================================================

def delivery_retry_worker():
    while True:
        try:
            now = time.time()

            for username in USERNAMES:
                with STATE_LOCKS[username]:
                    state = _load_state_unlocked(username)

                    to_retry = [
                        e["id"] for e in state
                        if e.get("id")
                        and not e.get("deleted")
                        and not e.get("video_sent")
                        and e.get("url")
                        and e.get("delivery_retry_after", 0) <= now
                    ]

                for video_id in to_retry:
                    queue_delivery(username, video_id)

        except Exception as e:
            print(f"[retry-worker] error: {e}", flush=True)

        time.sleep(DELIVERY_RETRY_SCAN_SEC)


def deletion_worker():
    # Let startup settle before the first sweep.
    time.sleep(DELETE_CHECK_EVERY_SEC)

    while True:
        cycle_start = time.time()

        for username in USERNAMES:
            try:
                check_deletions_for_account(username)
            except Exception as e:
                print(f"[delete] @{username}: error: {e}", flush=True)

        elapsed = time.time() - cycle_start
        time.sleep(max(1, DELETE_CHECK_EVERY_SEC - elapsed))


def account_worker(username):
    print(f"[worker] started for @{username}", flush=True)

    # Spread the accounts evenly across the check interval so they
    # don't all queue for yt-dlp at the same moment.
    time.sleep(
        USERNAMES.index(username) * CHECK_EVERY_SEC / len(USERNAMES)
    )

    next_check = time.time()

    while True:
        try:
            if time.time() >= next_check:
                process_account(username)

                next_check += CHECK_EVERY_SEC

                # If a check ran long, don't pile up catch-up checks.
                if next_check <= time.time():
                    next_check = time.time() + CHECK_EVERY_SEC

        except Exception as e:
            print(f"[worker] @{username}: error: {e}", flush=True)
            next_check = time.time() + CHECK_EVERY_SEC

        time.sleep(min(max(0.2, next_check - time.time()), 1.0))


# ============================================================
# ADMIN AUTH
# ============================================================

def admin_authorized():
    supplied = request.args.get("key", "")
    return hmac.compare_digest(supplied.encode(), ADMIN_KEY.encode())


# ============================================================
# FLASK ROUTES
# ============================================================

@app.get("/")
def health():
    return "ok"


@app.get("/health")
def health_check():
    return jsonify({"status": "ok", "accounts": USERNAMES})


@app.get("/debug_check_speed")
def debug_check_speed():
    """
    Timing test only. Touches no state and sends nothing to Telegram.

      /debug_check_speed?key=KEY&account=USERNAME   (recommended)
      /debug_check_speed?key=KEY                    (all accounts, slow)

    Read "ran_sec" for the true yt-dlp time. "waited_sec" is just
    time spent queued behind other checks.
    """
    if not admin_authorized():
        return "forbidden", 403

    only_account = request.args.get("account")
    targets = [only_account] if only_account else USERNAMES

    results = []

    for username in targets:
        if username not in USERNAMES:
            results.append({
                "account": username,
                "error": "not a configured account",
            })
            continue

        timing = {}
        items = latest_items(username, timing)

        results.append({
            "account": username,
            "status": timing.get("status", "unknown"),
            "waited_sec": round(timing.get("waited", 0), 2),
            "ran_sec": round(timing.get("ran", 0), 2),
            "videos_found": len(items),
        })

    return jsonify({
        "check_concurrency": CHECK_CONCURRENCY,
        "background_concurrency": BACKGROUND_CONCURRENCY,
        "check_batch": CHECK_BATCH,
        "flat_playlist": USE_FLAT_PLAYLIST,
        "results": results,
    })


@app.get("/check")
def check_now():
    if not admin_authorized():
        return "forbidden", 403

    for username in USERNAMES:
        threading.Thread(
            target=process_account,
            args=(username,),
            daemon=True,
            name=f"manual-check-{username}",
        ).start()

    return jsonify({"status": "checks_started", "accounts": USERNAMES})


@app.get("/check_deletions")
def check_deletions_now():
    if not admin_authorized():
        return "forbidden", 403

    for username in USERNAMES:
        threading.Thread(
            target=check_deletions_for_account,
            args=(username,),
            daemon=True,
            name=f"manual-delete-{username}",
        ).start()

    return jsonify({
        "status": "deletion_checks_started",
        "accounts": USERNAMES,
    })


# ============================================================
# STARTUP
# ============================================================

def start_background_workers():
    for username in USERNAMES:
        threading.Thread(
            target=account_worker,
            args=(username,),
            daemon=True,
            name=f"account-{username}",
        ).start()

    threading.Thread(
        target=delivery_retry_worker, daemon=True, name="delivery-retry"
    ).start()

    threading.Thread(
        target=deletion_worker, daemon=True, name="deletion"
    ).start()

    # One line so you can tell when the bot (re)started.
    threading.Thread(
        target=tg_send_text,
        args=(f"👋 Bot online. Watching: "
              f"{', '.join('@' + u for u in USERNAMES)}",),
        daemon=True,
    ).start()

    print(f"[startup] started workers for {len(USERNAMES)} accounts",
          flush=True)


if __name__ == "__main__":
    start_background_workers()

    port = int(os.environ.get("PORT", "10000"))

    app.run(host="0.0.0.0", port=port)
