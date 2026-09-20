import os
import time
import threading
import tempfile
import subprocess
import json
import shutil
import requests

from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

USERNAMES = [
    u.strip().lstrip("@")
    for u in os.environ["TIKTOK_USERNAMES"].split(",")
    if u.strip()
]

ADMIN_KEY = os.environ.get("ADMIN_KEY", "secret")


# -------------------- Timing --------------------

# How often each TikTok account is checked for NEW videos.
CHECK_EVERY_SEC = 30

# How often each account's old videos are checked for deletion.
DELETE_CHECK_EVERY_SEC = 600

# Number of recent videos yt-dlp asks TikTok for.
CHECK_BATCH = 8


# -------------------- Deletion --------------------

# A video must return an explicit "deleted/unavailable" result this many
# consecutive times before being marked deleted.
DELETE_FAIL_THRESHOLD = 3

# Unknown errors are NOT treated as deletion immediately.
# After this many consecutive unknown results we send an "unconfirmed"
# notification and stop checking that video.
UNKNOWN_FAIL_THRESHOLD = 30

# Small delay between checking individual old videos.
DELETE_CHECK_DELAY_SEC = 2


# -------------------- Resource limits --------------------

# New-video downloads are the priority.
#
# Two simultaneous downloads is a reasonable starting point for a small
# Render instance. If Render handles it easily, this can be increased to 3.
DOWNLOAD_WORKERS = 2

# Only this many account-check subprocesses are allowed simultaneously.
#
# IMPORTANT:
# This is ONLY for checking accounts.
# It does NOT block video downloads.
ACCOUNT_CHECK_WORKERS = 2

# Deletion checking gets its own separate slot.
#
# This means a deletion sweep cannot occupy a slot needed by a new-video
# download or normal account checking.
DELETION_WORKERS = 1


# -------------------- Download --------------------

# Prefer MP4 because Telegram handles it well.
DOWNLOAD_FORMAT = "best[ext=mp4]/best"

# Number of attempts to download a video.
DOWNLOAD_ATTEMPTS = 2

# Maximum time allowed for ONE yt-dlp download attempt.
DOWNLOAD_TIMEOUT_SEC = 20

# Delay between download attempts.
DOWNLOAD_RETRY_DELAY_SEC = 2


# -------------------- yt-dlp timeouts --------------------

# Maximum time for checking an account's recent videos.
LATEST_ITEMS_TIMEOUT_SEC = 25

# Maximum time for checking whether an old video still exists.
VIDEO_EXISTS_TIMEOUT_SEC = 20


# -------------------- Telegram --------------------

TG_SEND_VIDEO_ATTEMPTS = 2
TG_SEND_VIDEO_RETRY_DELAY_SEC = 3

# Telegram upload timeout.
TG_UPLOAD_TIMEOUT_SEC = 180

# Telegram text-message timeout.
TG_TEXT_TIMEOUT_SEC = 15


# -------------------- State --------------------

STATE_DIR = "/tmp/tiktok_bot_state"
os.makedirs(STATE_DIR, exist_ok=True)


# ============================================================
# APP / THREADING
# ============================================================

app = Flask(__name__)

# State has two layers of protection:
#
# STATE_LOCK protects the dictionary of per-account locks.
# Each account then gets its own lock so its watcher and deletion
# checker cannot simultaneously load/save the same JSON file.
STATE_LOCK = threading.Lock()

ACCOUNT_STATE_LOCKS = {}


def get_account_state_lock(username):
    with STATE_LOCK:
        if username not in ACCOUNT_STATE_LOCKS:
            ACCOUNT_STATE_LOCKS[username] = threading.Lock()

        return ACCOUNT_STATE_LOCKS[username]


# Normal account checking.
ACCOUNT_CHECK_EXECUTOR = ThreadPoolExecutor(
    max_workers=ACCOUNT_CHECK_WORKERS
)

# Video downloads/uploads.
#
# These are deliberately separate from account checking.
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=DOWNLOAD_WORKERS
)

# Deletion checks.
DELETION_EXECUTOR = ThreadPoolExecutor(
    max_workers=DELETION_WORKERS
)


# These semaphores prevent too many yt-dlp processes from running at once
# in the two lower-priority areas.
#
# They DO NOT affect video downloads.
ACCOUNT_CHECK_SLOT = threading.Semaphore(ACCOUNT_CHECK_WORKERS)
DELETION_SLOT = threading.Semaphore(DELETION_WORKERS)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


# ============================================================
# TELEGRAM
# ============================================================

def tg_send_text(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=TG_TEXT_TIMEOUT_SEC,
        )

    except Exception as e:
        log(f"Telegram text message failed: {e}")


def _try_send_video_once(path, caption):
    started = time.time()

    try:
        with open(path, "rb") as f:

            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",

                data={
                    "chat_id": CHAT_ID,
                    "caption": caption,

                    # Lets Telegram treat the upload as a streamable video.
                    "supports_streaming": True,
                },

                files={
                    "video": f,
                },

                timeout=TG_UPLOAD_TIMEOUT_SEC,
            )

        elapsed = time.time() - started

        log(
            f"Telegram upload finished in {elapsed:.2f}s "
            f"(HTTP {response.status_code})"
        )

        if response.status_code != 200:
            log(
                "Telegram returned non-200 response: "
                f"{response.text}"
            )
            return False

        return True

    except Exception as e:

        elapsed = time.time() - started

        log(
            f"Telegram upload failed after "
            f"{elapsed:.2f}s: {e}"
        )

        return False


def tg_send_video(path, caption=""):

    for attempt in range(
        1,
        TG_SEND_VIDEO_ATTEMPTS + 1
    ):

        log(
            f"Telegram video upload attempt "
            f"{attempt}/{TG_SEND_VIDEO_ATTEMPTS}"
        )

        if _try_send_video_once(path, caption):
            return True

        if attempt < TG_SEND_VIDEO_ATTEMPTS:
            time.sleep(TG_SEND_VIDEO_RETRY_DELAY_SEC)

    return False


# ============================================================
# STATE
# ============================================================

def state_file(username):
    return os.path.join(
        STATE_DIR,
        f"{username}_sent.json"
    )


def load_state(username):

    filename = state_file(username)

    try:

        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

    except Exception:

        return []


    if not data:
        return []


    migrated = []

    for item in data:

        # Oldest format:
        # ["videoid", "videoid2", ...]
        if isinstance(item, str):

            migrated.append({
                "id": item,
                "url": "",
                "title": "",
                "deleted": False,
                "fail_count": 0,
                "unknown_count": 0,
            })

            continue


        migrated.append({
            "id": item.get("id"),
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "deleted": item.get("deleted", False),
            "fail_count": item.get("fail_count", 0),
            "unknown_count": item.get("unknown_count", 0),
        })


    return migrated


def save_state(username, items):

    filename = state_file(username)
    temporary = filename + ".tmp"

    try:

        # Keep the newest 500 records.
        items_to_save = items[-500:]

        with open(
            temporary,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                items_to_save,
                f,
                ensure_ascii=False,
            )


        # Atomic replacement.
        os.replace(
            temporary,
            filename
        )

    except Exception as e:

        log(
            f"save_state failed for @{username}: {e}"
        )

        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except Exception:
            pass


# ============================================================
# RUN YT-DLP
# ============================================================

def run_yt_dlp(
    cmd,
    timeout,
    slot,
    slot_name,
):
    """
    Runs yt-dlp while respecting the appropriate resource limit.

    New-video downloads DO NOT use this function.
    This is intentional: downloads have their own executor and priority.
    """

    acquired = False

    try:

        # Don't wait forever for a lower-priority slot.
        acquired = slot.acquire(timeout=5)

        if not acquired:

            log(
                f"{slot_name}: no subprocess slot available; "
                f"skipping this check"
            )

            return None


        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


    except subprocess.TimeoutExpired:

        raise


    except Exception as e:

        log(
            f"{slot_name}: subprocess exception: {e}"
        )

        return None


    finally:

        if acquired:
            slot.release()


# ============================================================
# GET LATEST TIKTOK VIDEOS
# ============================================================

def latest_items(username):

    started = time.time()

    url = f"https://www.tiktok.com/@{username}"

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


    try:

        result = run_yt_dlp(
            cmd,
            LATEST_ITEMS_TIMEOUT_SEC,
            ACCOUNT_CHECK_SLOT,
            "account-check",
        )


        if result is None:
            return []


        if result.returncode != 0:

            log(
                f"latest_items failed for @{username} "
                f"(exit {result.returncode}):\n"
                f"{result.stderr}"
            )

            return []


        items = []


        for line in result.stdout.splitlines():

            try:
                obj = json.loads(line)

            except Exception:
                continue


            video_id = obj.get("id")

            video_url = (
                obj.get("webpage_url")
                or obj.get("url")
            )

            title = obj.get("title") or ""


            if video_id and video_url:

                items.append({
                    "id": str(video_id),
                    "url": video_url,
                    "title": title,
                })


        elapsed = time.time() - started

        log(
            f"@{username}: account check finished "
            f"in {elapsed:.2f}s, found {len(items)} videos"
        )

        return items


    except subprocess.TimeoutExpired:

        elapsed = time.time() - started

        log(
            f"@{username}: account check timed out "
            f"after {elapsed:.2f}s"
        )

        return []


    except Exception as e:

        log(
            f"@{username}: account check exception: {e}"
        )

        return []


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def _try_download_once(url):

    started = time.time()

    temp_dir = tempfile.mkdtemp(
        prefix="tiktok_"
    )

    output_template = os.path.join(
        temp_dir,
        "%(id)s.%(ext)s"
    )


    cmd = [
        "python3",
        "-m",
        "yt_dlp",

        "-o",
        output_template,

        "-f",
        DOWNLOAD_FORMAT,

        "--user-agent",
        "Mozilla/5.0",

        url,
    ]


    try:

        log(
            f"DOWNLOAD START: {url}"
        )


        # IMPORTANT:
        # There is intentionally NO global subprocess semaphore here.
        #
        # This download is already limited by DOWNLOAD_EXECUTOR.
        # A deletion check must never be able to block this.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT_SEC,
        )


        elapsed = time.time() - started


        if result.returncode != 0:

            log(
                f"DOWNLOAD FAILED after {elapsed:.2f}s: "
                f"{url}\n"
                f"{result.stderr}"
            )

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )

            return None


        # Find the resulting media file.
        for name in os.listdir(temp_dir):

            if name.lower().endswith(
                (
                    ".mp4",
                    ".webm",
                    ".mkv",
                    ".mov",
                )
            ):

                path = os.path.join(
                    temp_dir,
                    name
                )

                log(
                    f"DOWNLOAD SUCCESS in {elapsed:.2f}s: "
                    f"{path}"
                )

                return path


        log(
            f"yt-dlp reported success but no video "
            f"was found for {url}"
        )


        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return None


    except subprocess.TimeoutExpired:

        elapsed = time.time() - started

        log(
            f"DOWNLOAD TIMEOUT after {elapsed:.2f}s: "
            f"{url}"
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return None


    except Exception as e:

        log(
            f"DOWNLOAD EXCEPTION for {url}: {e}"
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return None


def download_video(url):

    for attempt in range(
        1,
        DOWNLOAD_ATTEMPTS + 1
    ):

        log(
            f"Download attempt "
            f"{attempt}/{DOWNLOAD_ATTEMPTS}: {url}"
        )


        path = _try_download_once(url)


        if path:
            return path


        if attempt < DOWNLOAD_ATTEMPTS:

            time.sleep(
                DOWNLOAD_RETRY_DELAY_SEC
            )


    return None


# ============================================================
# DELETE TEMP VIDEO
# ============================================================

def cleanup_download(path):

    if not path:
        return


    try:

        directory = os.path.dirname(path)

        if directory.startswith(
            tempfile.gettempdir()
        ):

            shutil.rmtree(
                directory,
                ignore_errors=True
            )


    except Exception as e:

        log(
            f"Cleanup failed for {path}: {e}"
        )


# ============================================================
# HANDLE NEW VIDEO
# ============================================================

def handle_new_video(username, item):

    total_started = time.time()

    video_id = item["id"]
    url = item["url"]
    title = item["title"]


    log(
        f"NEW VIDEO PIPELINE START "
        f"@{username} / {video_id}"
    )


    caption = (
        f"🎬 New video from @{username}\n"
        f"{title}\n"
        f"{url}"
    )


    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    download_started = time.time()

    path = download_video(url)

    download_elapsed = (
        time.time() - download_started
    )


    if not path:

        log(
            f"@{username} / {video_id}: "
            f"download failed after "
            f"{download_elapsed:.2f}s"
        )

        tg_send_text(
            "⚠️ Couldn't download the video, "
            "here's the link instead:\n"
            f"{caption}"
        )

        return


    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    upload_started = time.time()

    sent_ok = False

    try:

        sent_ok = tg_send_video(
            path,
            caption
        )

    finally:

        # Delete the downloaded file immediately after
        # Telegram has finished with it.
        cleanup_download(path)


    upload_elapsed = (
        time.time() - upload_started
    )

    total_elapsed = (
        time.time() - total_started
    )


    log(
        f"PIPELINE COMPLETE "
        f"@{username} / {video_id} | "
        f"download={download_elapsed:.2f}s | "
        f"telegram={upload_elapsed:.2f}s | "
        f"total={total_elapsed:.2f}s | "
        f"success={sent_ok}"
    )


    if not sent_ok:

        tg_send_text(
            "⚠️ Couldn't send the video to Telegram, "
            "here's the link instead:\n"
            f"{caption}"
        )


# ============================================================
# PROCESS ACCOUNT FOR NEW VIDEOS
# ============================================================

def process_account(username):

    # This lock prevents the deletion thread and watch thread
    # from simultaneously loading/modifying/saving this account's state.
    state_lock = get_account_state_lock(username)


    with state_lock:

        state = load_state(username)

        sent_ids = {
            item["id"]
            for item in state
        }


        items = latest_items(username)


        if not items:
            return


        new_items = [
            item
            for item in items
            if item["id"] not in sent_ids
        ]


        if not new_items:
            return


        # ----------------------------------------------------
        # First-ever run
        #
        # Do NOT flood Telegram with the previous 8 videos.
        # Only send the newest one.
        # ----------------------------------------------------

        if len(state) == 0 and len(new_items) > 1:

            new_items = new_items[:1]


        log(
            f"@{username}: detected "
            f"{len(new_items)} new video(s)"
        )


        # ----------------------------------------------------
        # Record IDs BEFORE downloading.
        #
        # This prevents duplicate detection if another poll
        # happens while the download is still running.
        # ----------------------------------------------------

        for item in reversed(new_items):

            state.append({
                "id": item["id"],
                "url": item["url"],
                "title": item["title"],
                "deleted": False,
                "fail_count": 0,
                "unknown_count": 0,
            })


        save_state(
            username,
            state
        )


    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Downloading happens OUTSIDE the state lock.
    # The account watcher is therefore never held up by a
    # slow download or Telegram upload.
    # --------------------------------------------------------

    for item in reversed(new_items):

        DOWNLOAD_EXECUTOR.submit(
            handle_new_video,
            username,
            item
        )


# ============================================================
# VIDEO EXISTENCE / DELETION CHECK
# ============================================================

REMOVED_PHRASES = [
    "video currently unavailable",
    "this post is unavailable",
    "content isn't available",
    "content unavailable",
    "video not available",
    "removed by the creator",
    "user's account is private",
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

        result = run_yt_dlp(
            cmd,
            VIDEO_EXISTS_TIMEOUT_SEC,
            DELETION_SLOT,
            "deletion-check",
        )


        if result is None:
            return "unknown"


    except subprocess.TimeoutExpired:

        log(
            f"Deletion check timed out: {url}"
        )

        return "unknown"


    except Exception as e:

        log(
            f"Deletion check exception: {e}"
        )

        return "unknown"


    if result.returncode == 0:
        return "exists"


    error = (
        result.stderr or ""
    ).lower()


    if any(
        phrase in error
        for phrase in REMOVED_PHRASES
    ):

        return "deleted"


    log(
        f"Unknown deletion-check result for {url}:\n"
        f"{result.stderr}"
    )

    return "unknown"


# ============================================================
# DELETION CHECK FOR ONE ACCOUNT
# ============================================================

def check_deletions_for_account(username):

    state_lock = get_account_state_lock(username)


    # Hold the state lock for the entire sweep.
    #
    # This prevents a new-video watcher from loading an old state,
    # while this deletion sweep is modifying it, and then overwriting
    # the newly discovered video.
    with state_lock:

        state = load_state(username)

        changed = False


        candidates = [
            item
            for item in state
            if not item.get("deleted")
            and item.get("url")
        ]


        # Newest first.
        candidates = list(
            reversed(candidates)
        )


        if not candidates:
            return


        log(
            f"@{username}: starting deletion sweep "
            f"({len(candidates)} videos)"
        )


        for index, item in enumerate(candidates):

            if index > 0:

                time.sleep(
                    DELETE_CHECK_DELAY_SEC
                )


            result = video_still_exists(
                item["url"]
            )


            # ------------------------------------------------
            # Video still exists
            # ------------------------------------------------

            if result == "exists":

                if (
                    item.get("fail_count", 0) != 0
                    or
                    item.get("unknown_count", 0) != 0
                ):

                    item["fail_count"] = 0
                    item["unknown_count"] = 0

                    changed = True


                continue


            # ------------------------------------------------
            # Explicitly deleted
            # ------------------------------------------------

            if result == "deleted":

                item["fail_count"] = (
                    item.get("fail_count", 0) + 1
                )

                changed = True


                log(
                    f"@{username}: deletion confirmation "
                    f"{item['fail_count']}/"
                    f"{DELETE_FAIL_THRESHOLD} "
                    f"for {item['id']}"
                )


                if (
                    item["fail_count"]
                    >= DELETE_FAIL_THRESHOLD
                ):

                    item["deleted"] = True


                    tg_send_text(
                        f"🗑️ Video deleted by @{username}\n"
                        f"{item.get('title', '')}\n"
                        f"{item['url']}"
                    )


                continue


            # ------------------------------------------------
            # Unknown result
            #
            # NEVER immediately assume deletion.
            # ------------------------------------------------

            item["unknown_count"] = (
                item.get("unknown_count", 0) + 1
            )

            changed = True


            log(
                f"@{username}: unknown deletion result "
                f"{item['unknown_count']}/"
                f"{UNKNOWN_FAIL_THRESHOLD} "
                f"for {item['id']}"
            )


            if (
                item["unknown_count"]
                >= UNKNOWN_FAIL_THRESHOLD
            ):

                item["deleted"] = True


                tg_send_text(
                    f"🗑️ Video likely deleted by @{username} "
                    f"(unconfirmed - repeated errors)\n"
                    f"{item.get('title', '')}\n"
                    f"{item['url']}"
                )


        if changed:

            save_state(
                username,
                state
            )


        log(
            f"@{username}: deletion sweep complete"
        )


# ============================================================
# ACCOUNT WATCH LOOP
# ============================================================

def account_watch_loop(username):

    # Slight deterministic staggering prevents every account from
    # hitting TikTok at precisely the same instant after startup.
    #
    # It does NOT affect how frequently the account is subsequently
    # checked.
    startup_delay = (
        sum(ord(c) for c in username)
        % CHECK_EVERY_SEC
    )

    time.sleep(startup_delay)


    while True:

        cycle_started = time.time()


        try:

            process_account(username)

        except Exception as e:

            log(
                f"ERROR watching @{username}: {e}"
            )


        # ----------------------------------------------------
        # Schedule the next check based on the START of this
        # cycle rather than adding 30 seconds after it finishes.
        #
        # Example:
        #
        # check takes 8 sec
        # next check starts 22 sec later
        #
        # = roughly 30 sec between starts.
        # ----------------------------------------------------

        elapsed = (
            time.time() - cycle_started
        )

        sleep_for = max(
            0,
            CHECK_EVERY_SEC - elapsed
        )


        time.sleep(sleep_for)


# ============================================================
# ACCOUNT DELETION LOOP
# ============================================================

def account_deletion_loop(username):

    # Stagger deletion sweeps.
    startup_delay = (
        sum(ord(c) for c in username)
        % 60
    )

    time.sleep(startup_delay)


    while True:

        cycle_started = time.time()


        try:

            # Run deletion sweep in its dedicated executor.
            #
            # This keeps the account's deletion logic separate
            # from new-video download workers.
            future = DELETION_EXECUTOR.submit(
                check_deletions_for_account,
                username
            )

            future.result()

        except Exception as e:

            log(
                f"ERROR checking deletions for "
                f"@{username}: {e}"
            )


        elapsed = (
            time.time() - cycle_started
        )


        sleep_for = max(
            0,
            DELETE_CHECK_EVERY_SEC - elapsed
        )


        time.sleep(sleep_for)


# ============================================================
# START ALL WORKERS
# ============================================================

def start_workers():

    tg_send_text(
        "👋 Bot online. Watching: "
        + ", ".join(
            "@" + username
            for username in USERNAMES
        )
    )


    log(
        "Bot started."
    )


    log(
        "Watching: "
        + ", ".join(
            "@" + username
            for username in USERNAMES
        )
    )


    for username in USERNAMES:

        threading.Thread(
            target=account_watch_loop,
            args=(username,),
            daemon=True,
            name=f"watch-{username}",
        ).start()


        threading.Thread(
            target=account_deletion_loop,
            args=(username,),
            daemon=True,
            name=f"delete-{username}",
        ).start()


# ============================================================
# WEB / HEALTH CHECK
# ============================================================

@app.get("/")
def health():

    return "ok"


# ============================================================
# MANUAL NEW-VIDEO CHECK
# ============================================================

@app.get("/check")
def check_now():

    if request.args.get("key") != ADMIN_KEY:
        return "forbidden", 403


    futures = []


    for username in USERNAMES:

        futures.append(
            ACCOUNT_CHECK_EXECUTOR.submit(
                process_account,
                username
            )
        )


    for future in futures:

        try:
            future.result(
                timeout=60
            )

        except Exception as e:

            log(
                f"Manual check failed: {e}"
            )


    return jsonify({
        "status": "checked"
    })


# ============================================================
# MANUAL DELETION CHECK
# ============================================================

@app.get("/check_deletions")
def check_deletions_now():

    if request.args.get("key") != ADMIN_KEY:
        return "forbidden", 403


    futures = []


    for username in USERNAMES:

        futures.append(
            DELETION_EXECUTOR.submit(
                check_deletions_for_account,
                username
            )
        )


    for future in futures:

        try:
            future.result(
                timeout=300
            )

        except Exception as e:

            log(
                f"Manual deletion check failed: {e}"
            )


    return jsonify({
        "status": "checked_deletions"
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start_workers()


    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )


    app.run(
        host="0.0.0.0",
        port=port
    )
