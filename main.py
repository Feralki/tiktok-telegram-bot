import os
import time
import threading
import tempfile
import subprocess
import json
import requests
import shutil
import hmac

from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
ADMIN_KEY = os.environ["ADMIN_KEY"]

USERNAMES = [
    u.strip().lstrip("@")
    for u in os.environ["TIKTOK_USERNAMES"].split(",")
    if u.strip()
]

# Normal TikTok checking
CHECK_EVERY_SEC = 60

# Lowered from 20 back down: at 20 items, yt-dlp checks for
# several accounts were landing right at the old 80s timeout
# and getting skipped entirely for that cycle. 12 is still
# comfortably more than enough to never miss a burst of posts
# within a single 60s check interval, and finishes faster.
CHECK_BATCH = 12

# How long a single account's "list latest videos" check is
# allowed to take. This used to be a hardcoded 80 inside
# latest_items() - now it's a named constant with real
# headroom, since real-world checks were observed taking
# 76-80s with CHECK_BATCH = 20 and timing out at 80s.
ACCOUNT_CHECK_TIMEOUT_SEC = 120

# If a single check finds more "new" videos than this, it is
# almost certainly backlog catching up after earlier checks
# failed/timed out for that account - not a real burst of
# fresh posts. Only announce/deliver the most recent
# MAX_NEW_ITEMS_PER_CYCLE of them; the rest are silently
# recorded as already-seen so they are never announced late
# and never counted as new again.
MAX_NEW_ITEMS_PER_CYCLE = 3

# Delivery
DOWNLOAD_WORKERS = 2
UPLOAD_WORKERS = 2

# Allow slower TikTok downloads without keeping a worker
# stuck for an excessive amount of time.
DOWNLOAD_TIMEOUT_SEC = 60
DOWNLOAD_ATTEMPTS_PER_JOB = 2

# How long delivery_job will wait for a download_video call
# submitted to DOWNLOAD_EXECUTOR - covering BOTH time spent
# queued behind other jobs (only DOWNLOAD_WORKERS run at once)
# and the download itself. Generous on purpose: the actual
# work is already self-bounded by DOWNLOAD_TIMEOUT_SEC inside
# download_video, so this is a safety net against a truly
# stuck worker, not a race against queue position.
DOWNLOAD_QUEUE_WAIT_TIMEOUT_SEC = 300

UPLOAD_TIMEOUT_SEC = 180
UPLOAD_ATTEMPTS_PER_JOB = 2

DELIVERY_RETRY_DELAY_SEC = 30
DELIVERY_RETRY_SCAN_SEC = 30

# Same reasoning as DOWNLOAD_QUEUE_WAIT_TIMEOUT_SEC, but for
# UPLOAD_EXECUTOR / UPLOAD_WORKERS.
UPLOAD_QUEUE_WAIT_TIMEOUT_SEC = 300

# Deletion checking
DELETE_CHECK_EVERY_SEC = 600
DELETE_CHECK_MAX_PER_CYCLE = 10
DELETE_CHECK_DELAY_SEC = 3

DELETE_FAIL_THRESHOLD = 3
UNKNOWN_FAIL_THRESHOLD = 30

# State
STATE_DIR = "/tmp/tiktok_bot_state"
STATE_MAX_ITEMS = 500

os.makedirs(STATE_DIR, exist_ok=True)


# ============================================================
# APP / LOCKS / EXECUTORS
# ============================================================

app = Flask(__name__)

# Kept for compatibility with older state handling.
# Actual state protection uses per-account locks below.
STATE_LOCK = threading.RLock()

# One lock per account.
#
# Normal checking and deletion checking cannot run
# simultaneously for the same account.
ACCOUNT_LOCKS = {
    username: threading.Lock()
    for username in USERNAMES
}

# Protects reading/writing the state for each account.
STATE_LOCKS = {
    username: threading.RLock()
    for username in USERNAMES
}

# IMPORTANT:
#
# delivery_job does NOT run on DOWNLOAD_EXECUTOR.
#
# delivery_job itself submits download_video to
# DOWNLOAD_EXECUTOR and waits for the result. Running
# delivery_job on the same executor would cause a
# self-deadlock when all workers are occupied.
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=DOWNLOAD_WORKERS,
    thread_name_prefix="download",
)

UPLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=UPLOAD_WORKERS,
    thread_name_prefix="upload",
)

DELIVERY_QUEUED = set()
DELIVERY_QUEUED_LOCK = threading.Lock()


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

        return response.ok

    except Exception as e:

        print(
            f"[telegram] send text failed: {e}",
            flush=True,
        )

        return False


def tg_send_video(path, caption=""):

    try:

        with open(path, "rb") as f:

            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                data={
                    "chat_id": CHAT_ID,
                    "caption": caption,
                },
                files={
                    "video": f,
                },
                timeout=UPLOAD_TIMEOUT_SEC,
            )

        return response.ok

    except Exception as e:

        print(
            f"[telegram] send video failed: {e}",
            flush=True,
        )

        return False


# ============================================================
# VIDEO DOWNLOAD
# ============================================================

def download_video(url):

    tmpdir = tempfile.mkdtemp()

    out = os.path.join(
        tmpdir,
        "%(id)s.%(ext)s",
    )

    cmd = [
        "python3",
        "-m",
        "yt_dlp",
        "-o",
        out,
        "-f",
        "best[ext=mp4]/best",
        "--user-agent",
        "Mozilla/5.0",
        url,
    ]

    try:

        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOWNLOAD_TIMEOUT_SEC,
        )

        for name in os.listdir(tmpdir):

            if name.lower().endswith(
                (".mp4", ".webm", ".mkv")
            ):

                return os.path.join(
                    tmpdir,
                    name,
                )

    except subprocess.TimeoutExpired:

        print(
            f"[download] timeout after "
            f"{DOWNLOAD_TIMEOUT_SEC}s",
            flush=True,
        )

    except Exception as e:

        print(
            f"[download] failed: {e}",
            flush=True,
        )

    shutil.rmtree(
        tmpdir,
        ignore_errors=True,
    )

    return None


# ============================================================
# STATE
# ============================================================

def state_file(username):

    return os.path.join(
        STATE_DIR,
        f"{username}_sent.json",
    )


def _load_state_unlocked(username):

    try:

        with open(
            state_file(username),
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

    except FileNotFoundError:

        return []

    except Exception as e:

        print(
            f"[state] load failed for "
            f"@{username}: {e}",
            flush=True,
        )

        return []

    if not data:
        return []

    migrated = []

    for item in data:

        # ----------------------------------------------------
        # Old format:
        #
        # ["video_id", "video_id", ...]
        # ----------------------------------------------------

        if isinstance(item, str):

            migrated.append({
                "id": item,
                "url": "",
                "title": "",
                "deleted": False,
                "fail_count": 0,
                "unknown_count": 0,
                "video_sent": True,
                "delivery_retry_after": 0,
                "last_delete_check": 0,
            })

            continue

        if not isinstance(item, dict):
            continue

        migrated.append({
            "id": item.get("id"),
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "deleted": item.get("deleted", False),
            "fail_count": item.get("fail_count", 0),
            "unknown_count": item.get("unknown_count", 0),

            # Existing entries from the old system were
            # already delivered, so default them to True.
            "video_sent": item.get(
                "video_sent",
                True,
            ),

            "delivery_retry_after": item.get(
                "delivery_retry_after",
                0,
            ),

            "last_delete_check": item.get(
                "last_delete_check",
                0,
            ),
        })

    return migrated


def _save_state_unlocked(username, items):

    try:

        path = state_file(username)
        tmp = path + ".tmp"

        with open(
            tmp,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                items[-STATE_MAX_ITEMS:],
                f,
            )

        os.replace(
            tmp,
            path,
        )

    except Exception as e:

        print(
            f"[state] save failed for "
            f"@{username}: {e}",
            flush=True,
        )


def load_state(username):

    with STATE_LOCKS[username]:

        return _load_state_unlocked(
            username
        )


def save_state(username, items):

    with STATE_LOCKS[username]:

        _save_state_unlocked(
            username,
            items,
        )


# ============================================================
# TIKTOK LOOKUP
# ============================================================

def latest_items(username):

    url = (
        f"https://www.tiktok.com/@{username}"
    )

    cmd = [
        "python3",
        "-m",
        "yt_dlp",
        "-j",
        "--playlist-end",
        str(CHECK_BATCH),
        "--user-agent",
        "Mozilla/5.0",
        url,
    ]

    start = time.time()

    try:

        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ACCOUNT_CHECK_TIMEOUT_SEC,
            check=True,
        )

        items = []

        for line in p.stdout.splitlines():

            try:

                obj = json.loads(line)

            except Exception:

                continue

            vid_id = obj.get("id")

            vid_url = (
                obj.get("webpage_url")
                or obj.get("url")
            )

            title = obj.get("title") or ""

            if vid_id and vid_url:

                items.append({
                    "id": str(vid_id),
                    "url": vid_url,
                    "title": title,
                })

        elapsed = (
            time.time()
            - start
        )

        print(
            f"[check] @{username}: "
            f"found {len(items)} videos "
            f"in {elapsed:.2f}s",
            flush=True,
        )

        return items

    except subprocess.TimeoutExpired:

        elapsed = (
            time.time()
            - start
        )

        print(
            f"[check] @{username}: "
            f"timeout after {elapsed:.2f}s",
            flush=True,
        )

        return []

    except Exception as e:

        elapsed = (
            time.time()
            - start
        )

        print(
            f"[check] @{username}: "
            f"failed after {elapsed:.2f}s: {e}",
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

    cmd = [
        "python3",
        "-m",
        "yt_dlp",
        "-j",
        "--user-agent",
        "Mozilla/5.0",
        url,
    ]

    try:

        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

    except subprocess.TimeoutExpired:

        return "unknown"

    except Exception:

        return "unknown"

    if p.returncode == 0:

        return "exists"

    err = (
        (p.stderr or "")
        + "\n"
        + (p.stdout or "")
    ).lower()

    if any(
        phrase in err
        for phrase in REMOVED_PHRASES
    ):

        return "deleted"

    # A private account is not proof that the
    # individual video was deleted.
    if "user's account is private" in err:

        return "unknown"

    return "unknown"


# ============================================================
# DELIVERY QUEUE
# ============================================================

def queue_delivery(username, video_id):

    key = (
        username,
        video_id,
    )

    with DELIVERY_QUEUED_LOCK:

        if key in DELIVERY_QUEUED:

            return False

        DELIVERY_QUEUED.add(key)

    try:

        # delivery_job runs on its own daemon thread.
        # It must NOT run on DOWNLOAD_EXECUTOR.
        threading.Thread(
            target=delivery_job,
            args=(
                username,
                video_id,
            ),
            daemon=True,
            name=f"delivery-{username}-{video_id}",
        ).start()

        return True

    except Exception:

        with DELIVERY_QUEUED_LOCK:

            DELIVERY_QUEUED.discard(key)

        return False


def delivery_job(username, video_id):

    key = (
        username,
        video_id,
    )

    path = None

    try:

        # ----------------------------------------------------
        # Get current state
        # ----------------------------------------------------

        with STATE_LOCKS[username]:

            state = _load_state_unlocked(
                username
            )

            item = None

            for entry in state:

                if entry.get("id") == video_id:

                    item = dict(entry)
                    break

        if item is None:

            print(
                f"[delivery] @{username} "
                f"{video_id}: state entry missing",
                flush=True,
            )

            return

        # ----------------------------------------------------
        # Never deliver something already deleted
        # ----------------------------------------------------

        if item.get("deleted"):

            print(
                f"[delivery] @{username} "
                f"{video_id}: deleted; "
                f"delivery stopped",
                flush=True,
            )

            return

        if item.get("video_sent"):

            print(
                f"[delivery] @{username} "
                f"{video_id}: already delivered",
                flush=True,
            )

            return

        url = item.get(
            "url",
            "",
        )

        title = item.get(
            "title",
            "",
        )

        if not url:

            print(
                f"[delivery] @{username} "
                f"{video_id}: no URL",
                flush=True,
            )

            return

        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

        path = None

        for attempt in range(
            1,
            DOWNLOAD_ATTEMPTS_PER_JOB + 1,
        ):

            start = time.time()

            print(
                f"[delivery] @{username} "
                f"{video_id}: "
                f"download attempt "
                f"{attempt}/"
                f"{DOWNLOAD_ATTEMPTS_PER_JOB}",
                flush=True,
            )

            # Only the actual download occupies a
            # DOWNLOAD_EXECUTOR worker.
            download_future = (
                DOWNLOAD_EXECUTOR.submit(
                    download_video,
                    url,
                )
            )

            try:

                # NOTE: this timeout must cover both the time
                # spent waiting for a free DOWNLOAD_WORKERS
                # slot AND the actual download. download_video
                # already self-bounds its own work via
                # DOWNLOAD_TIMEOUT_SEC internally, so this
                # outer wait is a generous safety net rather
                # than a tight race - if it were too tight,
                # a job that's merely waiting in queue behind
                # other jobs (e.g. after a burst of new videos)
                # would get marked "failed" before it ever got
                # to run, then requeue and repeat forever.
                path = download_future.result(
                    timeout=DOWNLOAD_QUEUE_WAIT_TIMEOUT_SEC
                )

            except Exception as e:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"download exception: {e}",
                    flush=True,
                )

                path = None

            elapsed = (
                time.time()
                - start
            )

            if path:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"download success "
                    f"in {elapsed:.2f}s",
                    flush=True,
                )

                break

            print(
                f"[delivery] @{username} "
                f"{video_id}: "
                f"download failed "
                f"after {elapsed:.2f}s",
                flush=True,
            )

            if attempt < DOWNLOAD_ATTEMPTS_PER_JOB:

                time.sleep(1)

        # ----------------------------------------------------
        # Download failed
        # ----------------------------------------------------

        if not path:

            deleted_now = False
            entry_found = False

            with STATE_LOCKS[username]:

                state = _load_state_unlocked(
                    username
                )

                for entry in state:

                    if entry.get("id") == video_id:

                        entry_found = True

                        if entry.get("deleted"):

                            deleted_now = True
                            break

                        entry["video_sent"] = False

                        entry[
                            "delivery_retry_after"
                        ] = (
                            time.time()
                            + DELIVERY_RETRY_DELAY_SEC
                        )

                        break

                if entry_found:

                    _save_state_unlocked(
                        username,
                        state,
                    )

            if deleted_now:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"deleted; "
                    f"delivery stopped",
                    flush=True,
                )

            else:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"download failed; "
                    f"full retry scheduled",
                    flush=True,
                )

            return

        # ----------------------------------------------------
        # Upload
        # ----------------------------------------------------

        caption = (
            f"🎬 New video from @{username}\n"
            f"{title}\n"
            f"{url}"
        )

        uploaded = False

        for attempt in range(
            1,
            UPLOAD_ATTEMPTS_PER_JOB + 1,
        ):

            # Re-check deletion before every upload attempt.
            with STATE_LOCKS[username]:

                state = _load_state_unlocked(
                    username
                )

                current = None

                for entry in state:

                    if entry.get("id") == video_id:

                        current = entry
                        break

                if current is None:

                    print(
                        f"[delivery] @{username} "
                        f"{video_id}: "
                        f"state entry disappeared",
                        flush=True,
                    )

                    return

                if current.get("deleted"):

                    print(
                        f"[delivery] @{username} "
                        f"{video_id}: "
                        f"deleted before upload; "
                        f"delivery stopped",
                        flush=True,
                    )

                    return

            start = time.time()

            print(
                f"[delivery] @{username} "
                f"{video_id}: "
                f"upload attempt "
                f"{attempt}/"
                f"{UPLOAD_ATTEMPTS_PER_JOB}",
                flush=True,
            )

            upload_future = (
                UPLOAD_EXECUTOR.submit(
                    tg_send_video,
                    path,
                    caption,
                )
            )

            try:

                uploaded = upload_future.result(
                    timeout=UPLOAD_QUEUE_WAIT_TIMEOUT_SEC
                )

            except Exception as e:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"upload exception: {e}",
                    flush=True,
                )

                uploaded = False

            elapsed = (
                time.time()
                - start
            )

            if uploaded:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"Telegram upload finished "
                    f"in {elapsed:.2f}s",
                    flush=True,
                )

                break

            print(
                f"[delivery] @{username} "
                f"{video_id}: "
                f"upload failed "
                f"after {elapsed:.2f}s",
                flush=True,
            )

            if attempt < UPLOAD_ATTEMPTS_PER_JOB:

                time.sleep(1)

        # ----------------------------------------------------
        # Upload failed
        # ----------------------------------------------------

        if not uploaded:

            deleted_now = False
            entry_found = False

            with STATE_LOCKS[username]:

                state = _load_state_unlocked(
                    username
                )

                for entry in state:

                    if entry.get("id") == video_id:

                        entry_found = True

                        if entry.get("deleted"):

                            deleted_now = True
                            break

                        entry["video_sent"] = False

                        entry[
                            "delivery_retry_after"
                        ] = (
                            time.time()
                            + DELIVERY_RETRY_DELAY_SEC
                        )

                        break

                if entry_found:

                    _save_state_unlocked(
                        username,
                        state,
                    )

            if deleted_now:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"deleted; "
                    f"delivery stopped",
                    flush=True,
                )

            else:

                print(
                    f"[delivery] @{username} "
                    f"{video_id}: "
                    f"upload failed; "
                    f"full retry scheduled",
                    flush=True,
                )

            return

        # ----------------------------------------------------
        # Successful delivery
        # ----------------------------------------------------

        with STATE_LOCKS[username]:

            state = _load_state_unlocked(
                username
            )

            for entry in state:

                if entry.get("id") == video_id:

                    if entry.get("deleted"):

                        print(
                            f"[delivery] @{username} "
                            f"{video_id}: "
                            f"deleted after upload",
                            flush=True,
                        )

                        return

                    entry["video_sent"] = True
                    entry["delivery_retry_after"] = 0

                    break

            _save_state_unlocked(
                username,
                state,
            )

        print(
            f"[delivery] @{username} "
            f"{video_id}: "
            f"delivery complete",
            flush=True,
        )

    except Exception as e:

        print(
            f"[delivery] @{username} "
            f"{video_id}: "
            f"unexpected error: {e}",
            flush=True,
        )

    finally:

        # Clean up downloaded video.
        if path:

            try:

                tmpdir = os.path.dirname(
                    path
                )

                shutil.rmtree(
                    tmpdir,
                    ignore_errors=True,
                )

            except Exception:
                pass

        with DELIVERY_QUEUED_LOCK:

            DELIVERY_QUEUED.discard(key)


# ============================================================
# NORMAL ACCOUNT CHECK
# ============================================================

def process_account(username):

    # Do not allow this account's normal check to overlap
    # with another normal/deletion check.
    if not ACCOUNT_LOCKS[username].acquire(
        blocking=False
    ):

        print(
            f"[check] @{username}: "
            f"previous check still running; "
            f"skipping this cycle",
            flush=True,
        )

        return

    try:

        items = latest_items(
            username
        )

        if not items:
            return

        # ----------------------------------------------------
        # Find new videos.
        #
        # Only state access is protected by the state lock.
        # No Telegram network request happens while holding
        # STATE_LOCKS.
        # ----------------------------------------------------

        with STATE_LOCKS[username]:

            state = _load_state_unlocked(
                username
            )

            sent_ids = {
                str(entry.get("id"))
                for entry in state
                if entry.get("id")
            }

            new_items = [
                item
                for item in items
                if item["id"] not in sent_ids
            ]

            # ------------------------------------------------
            # Guard against announcing a whole backlog at
            # once.
            #
            # This covers BOTH:
            #   - a completely new account (state empty)
            #   - an account that failed/timed out for
            #     several cycles and then suddenly succeeds,
            #     making many older videos look "new" all at
            #     once.
            #
            # Anything beyond MAX_NEW_ITEMS_PER_CYCLE is
            # silently recorded as already delivered
            # (video_sent=True) so it is never announced late
            # and never re-evaluated as new again.
            # ------------------------------------------------

            if len(new_items) > MAX_NEW_ITEMS_PER_CYCLE:

                overflow = new_items[
                    MAX_NEW_ITEMS_PER_CYCLE:
                ]

                new_items = new_items[
                    :MAX_NEW_ITEMS_PER_CYCLE
                ]

                print(
                    f"[new] @{username}: "
                    f"{len(overflow) + len(new_items)} "
                    f"items appeared new at once "
                    f"(likely backlog from earlier failed "
                    f"checks); announcing only the "
                    f"{len(new_items)} most recent and "
                    f"silently recording the rest",
                    flush=True,
                )

                existing_ids = {
                    entry.get("id")
                    for entry in state
                }

                overflow_changed = False

                for item in overflow:

                    if item["id"] not in existing_ids:

                        state.append({
                            "id": item["id"],
                            "url": item["url"],
                            "title": item["title"],
                            "deleted": False,
                            "fail_count": 0,
                            "unknown_count": 0,

                            # Suppress delivery: treat as
                            # already handled.
                            "video_sent": True,

                            "delivery_retry_after": 0,
                            "last_delete_check": time.time(),
                        })

                        overflow_changed = True

                if overflow_changed:

                    _save_state_unlocked(
                        username,
                        state,
                    )

        # ----------------------------------------------------
        # Send links immediately.
        #
        # IMPORTANT:
        # tg_send_text() is outside STATE_LOCKS because it
        # performs network I/O.
        # ----------------------------------------------------

        for item in reversed(new_items):

            video_id = item["id"]
            url = item["url"]
            title = item["title"]

            caption = (
                f"🎬 New video from @{username}\n"
                f"{title}\n"
                f"{url}"
            )

            link_start = time.time()

            link_sent = tg_send_text(
                caption
            )

            link_elapsed = (
                time.time()
                - link_start
            )

            if not link_sent:

                print(
                    f"[new] @{username} "
                    f"{video_id}: "
                    f"link send failed "
                    f"after "
                    f"{link_elapsed:.2f}s; "
                    f"will retry",
                    flush=True,
                )

                continue

            print(
                f"[new] @{username} "
                f"{video_id}: "
                f"link sent in "
                f"{link_elapsed:.2f}s",
                flush=True,
            )

            # ------------------------------------------------
            # Re-acquire state lock after the network call.
            #
            # Another thread may have changed state while
            # Telegram was processing the request, so re-load
            # the current state and check the ID again.
            # ------------------------------------------------

            already_saved = False

            with STATE_LOCKS[username]:

                state = _load_state_unlocked(
                    username
                )

                for entry in state:

                    if entry.get("id") == video_id:

                        already_saved = True
                        break

                if not already_saved:

                    state.append({
                        "id": video_id,
                        "url": url,
                        "title": title,
                        "deleted": False,
                        "fail_count": 0,
                        "unknown_count": 0,

                        # Link has been sent, but the actual
                        # video still needs to be delivered.
                        "video_sent": False,

                        "delivery_retry_after": 0,

                        # Don't immediately declare a freshly
                        # posted video deleted.
                        "last_delete_check": time.time(),
                    })

                    _save_state_unlocked(
                        username,
                        state,
                    )

            if already_saved:

                print(
                    f"[new] @{username} "
                    f"{video_id}: "
                    f"already exists in state; "
                    f"not queueing duplicate",
                    flush=True,
                )

                continue

            # Queue actual video delivery after the
            # link has already been sent.
            queue_delivery(
                username,
                video_id,
            )

    finally:

        ACCOUNT_LOCKS[username].release()


# ============================================================
# DELETION CHECK FOR ONE ACCOUNT
# ============================================================

def check_deletions_for_account(username):

    # Deletion checks are secondary to normal checks.
    #
    # The entire deletion cycle owns this account lock so
    # normal checking can never overlap with it.
    if not ACCOUNT_LOCKS[username].acquire(
        blocking=False
    ):

        print(
            f"[delete] @{username}: "
            f"account busy; "
            f"skipping deletion cycle",
            flush=True,
        )

        return

    try:

        with STATE_LOCKS[username]:

            state = _load_state_unlocked(
                username
            )

            candidates = [
                entry
                for entry in state
                if (
                    not entry.get("deleted")
                    and entry.get("url")
                )
            ]

            # Rotate through the oldest checked videos.
            candidates.sort(
                key=lambda entry:
                entry.get(
                    "last_delete_check",
                    0,
                )
            )

            candidates = candidates[
                :DELETE_CHECK_MAX_PER_CYCLE
            ]

        if not candidates:
            return

        for index, item in enumerate(
            candidates
        ):

            video_id = item.get("id")
            url = item.get("url")

            if not video_id or not url:
                continue

            # Never immediately deletion-check a fresh post.
            age_since_check = (
                time.time()
                - item.get(
                    "last_delete_check",
                    0,
                )
            )

            if (
                item.get("last_delete_check", 0)
                and age_since_check < 120
            ):

                continue

            result = video_still_exists(
                url
            )

            changed = False
            deletion_alert = None

            # ------------------------------------------------
            # State modification only.
            #
            # Telegram is deliberately NOT called here while
            # the state lock is held.
            # ------------------------------------------------

            with STATE_LOCKS[username]:

                state = _load_state_unlocked(
                    username
                )

                current = None

                for entry in state:

                    if entry.get("id") == video_id:

                        current = entry
                        break

                if current is None:
                    continue

                current[
                    "last_delete_check"
                ] = time.time()

                changed = True

                if result == "exists":

                    if (
                        current.get(
                            "fail_count",
                            0,
                        ) != 0
                        or current.get(
                            "unknown_count",
                            0,
                        ) != 0
                    ):

                        current[
                            "fail_count"
                        ] = 0

                        current[
                            "unknown_count"
                        ] = 0

                    changed = True

                elif result == "deleted":

                    current[
                        "fail_count"
                    ] = (
                        current.get(
                            "fail_count",
                            0,
                        )
                        + 1
                    )

                    if (
                        current[
                            "fail_count"
                        ]
                        >= DELETE_FAIL_THRESHOLD
                    ):

                        current[
                            "deleted"
                        ] = True

                        title = current.get(
                            "title",
                            "",
                        )

                        deletion_alert = (
                            f"🗑️ Video deleted by "
                            f"@{username}\n"
                            f"{title}\n"
                            f"{url}"
                        )

                        print(
                            f"[delete] @{username} "
                            f"{video_id}: "
                            f"confirmed deleted",
                            flush=True,
                        )

                else:

                    current[
                        "unknown_count"
                    ] = (
                        current.get(
                            "unknown_count",
                            0,
                        )
                        + 1
                    )

                    # Unknown errors are deliberately
                    # much harder to turn into a deletion.
                    if (
                        current[
                            "unknown_count"
                        ]
                        >= UNKNOWN_FAIL_THRESHOLD
                    ):

                        current[
                            "deleted"
                        ] = True

                        title = current.get(
                            "title",
                            "",
                        )

                        deletion_alert = (
                            f"🗑️ Video likely "
                            f"deleted by "
                            f"@{username} "
                            f"(unconfirmed - "
                            f"repeated errors, "
                            f"not a direct removal "
                            f"message)\n"
                            f"{title}\n"
                            f"{url}"
                        )

                        print(
                            f"[delete] @{username} "
                            f"{video_id}: "
                            f"marked likely deleted "
                            f"after repeated "
                            f"unknown errors",
                            flush=True,
                        )

                if changed:

                    _save_state_unlocked(
                        username,
                        state,
                    )

            # Telegram network request happens AFTER
            # releasing the state lock.
            if deletion_alert:

                tg_send_text(
                    deletion_alert
                )

            if index < len(candidates) - 1:

                time.sleep(
                    DELETE_CHECK_DELAY_SEC
                )

    finally:

        ACCOUNT_LOCKS[username].release()


# ============================================================
# DELIVERY RETRY WORKER
# ============================================================

def delivery_retry_worker():

    while True:

        try:

            now = time.time()

            for username in USERNAMES:

                with STATE_LOCKS[username]:

                    state = _load_state_unlocked(
                        username
                    )

                    candidates = []

                    for item in state:

                        # Never retry deleted videos.
                        if item.get("deleted"):
                            continue

                        if item.get("video_sent"):
                            continue

                        if not item.get("url"):
                            continue

                        retry_after = item.get(
                            "delivery_retry_after",
                            0,
                        )

                        if retry_after > now:
                            continue

                        video_id = item.get("id")

                        if video_id:

                            candidates.append(
                                video_id
                            )

                # Queue outside the state lock.
                for video_id in candidates:

                    queue_delivery(
                        username,
                        video_id,
                    )

        except Exception as e:

            print(
                f"[retry-worker] error: {e}",
                flush=True,
            )

        time.sleep(
            DELIVERY_RETRY_SCAN_SEC
        )


# ============================================================
# DELETION WORKER
# ============================================================

def deletion_worker():

    # Give startup/new-video processing time before
    # the first deletion sweep.
    time.sleep(
        DELETE_CHECK_EVERY_SEC
    )

    while True:

        cycle_start = time.time()

        for username in USERNAMES:

            try:

                check_deletions_for_account(
                    username
                )

            except Exception as e:

                print(
                    f"[delete] @{username}: "
                    f"error: {e}",
                    flush=True,
                )

        elapsed = (
            time.time()
            - cycle_start
        )

        sleep_for = max(
            1,
            DELETE_CHECK_EVERY_SEC
            - elapsed,
        )

        time.sleep(
            sleep_for
        )


# ============================================================
# INDEPENDENT ACCOUNT WORKER
# ============================================================

def account_worker(username):

    print(
        f"[worker] started for @{username}",
        flush=True,
    )

    # Stagger startup very slightly so all account
    # yt-dlp processes do not launch at precisely
    # the same moment.
    time.sleep(
        USERNAMES.index(username) * 0.5
    )

    next_check = time.time()

    while True:

        try:

            now = time.time()

            if now >= next_check:

                process_account(
                    username
                )

                # Start-to-start scheduling.
                next_check += CHECK_EVERY_SEC

                # If a check took longer than the interval,
                # don't perform a pile of catch-up checks.
                if next_check <= time.time():

                    next_check = (
                        time.time()
                        + CHECK_EVERY_SEC
                    )

        except Exception as e:

            print(
                f"[worker] @{username}: "
                f"error: {e}",
                flush=True,
            )

            next_check = (
                time.time()
                + CHECK_EVERY_SEC
            )

        sleep_for = max(
            0.2,
            next_check - time.time(),
        )

        time.sleep(
            min(
                sleep_for,
                1.0,
            )
        )


# ============================================================
# ADMIN AUTH
# ============================================================

def admin_authorized():

    supplied_key = request.args.get(
        "key",
        "",
    )

    return hmac.compare_digest(
        supplied_key,
        ADMIN_KEY,
    )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.get("/")
def health():

    return "ok"


@app.get("/health")
def health_check():

    return jsonify({
        "status": "ok",
        "accounts": USERNAMES,
    })


@app.get("/check")
def check_now():

    if not admin_authorized():

        return "forbidden", 403

    # Trigger checks concurrently.
    for username in USERNAMES:

        threading.Thread(
            target=process_account,
            args=(username,),
            daemon=True,
            name=f"manual-check-{username}",
        ).start()

    return jsonify({
        "status": "checks_started",
        "accounts": USERNAMES,
    })


@app.get("/check_deletions")
def check_deletions_now():

    if not admin_authorized():

        return "forbidden", 403

    # Trigger deletion checks concurrently.
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

    # One independent worker per TikTok account.
    for username in USERNAMES:

        threading.Thread(
            target=account_worker,
            args=(username,),
            daemon=True,
            name=f"account-{username}",
        ).start()

    # Delivery retry worker.
    threading.Thread(
        target=delivery_retry_worker,
        daemon=True,
        name="delivery-retry",
    ).start()

    # Deletion worker.
    threading.Thread(
        target=deletion_worker,
        daemon=True,
        name="deletion",
    ).start()

    print(
        f"[startup] started workers for "
        f"{len(USERNAMES)} accounts",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start_background_workers()

    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
