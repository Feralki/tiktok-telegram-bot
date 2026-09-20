import os
import time
import json
import shutil
import tempfile
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
import requests


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

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")


# ============================================================
# TIMING
# ============================================================

# How often each TikTok account is checked.
CHECK_EVERY_SEC = 30

# How often deletion checks run.
DELETE_CHECK_EVERY_SEC = 600

# Number of recent videos requested from each account.
CHECK_BATCH = 8


# ============================================================
# DELETION SETTINGS
# ============================================================

DELETE_FAIL_THRESHOLD = 3
UNKNOWN_FAIL_THRESHOLD = 30

# Delay between deletion checks.
DELETE_CHECK_DELAY_SEC = 2


# ============================================================
# ACCOUNT CHECKING
# ============================================================

# Keep this low on Render Free to avoid unnecessary TikTok load.
ACCOUNT_CHECK_WORKERS = 2

# Maximum time allowed for one account check.
ACCOUNT_CHECK_TIMEOUT_SEC = 25


# ============================================================
# VIDEO DOWNLOADING
# ============================================================

# Keep at 2 on Render Free.
DOWNLOAD_WORKERS = 2

DOWNLOAD_ATTEMPTS = 2

# 30 seconds gives normal downloads some breathing room.
DOWNLOAD_TIMEOUT_SEC = 30

DOWNLOAD_RETRY_DELAY_SEC = 2


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_UPLOAD_ATTEMPTS = 2
TELEGRAM_TIMEOUT_SEC = 120


# ============================================================
# STATE
# ============================================================

STATE_DIR = "/tmp/tiktok_bot_state"

os.makedirs(STATE_DIR, exist_ok=True)


# ============================================================
# GLOBALS
# ============================================================

app = Flask(__name__)


STATE_LOCKS = {}
STATE_LOCKS_GLOBAL = threading.Lock()


# Account checking executor.
ACCOUNT_CHECK_EXECUTOR = ThreadPoolExecutor(
    max_workers=ACCOUNT_CHECK_WORKERS
)


# Video downloads have their own completely separate workers.
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=DOWNLOAD_WORKERS
)


# Deletion checking is deliberately kept to one worker.
DELETION_EXECUTOR = ThreadPoolExecutor(
    max_workers=1
)


# IMPORTANT:
#
# Only account/deletion yt-dlp processes use this semaphore.
# Downloads DO NOT use it.
#
# This keeps TikTok account checking relatively conservative
# while allowing detected videos to download independently.
ACCOUNT_SUBPROCESS_SEMAPHORE = threading.Semaphore(2)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True
    )


# ============================================================
# STATE HELPERS
# ============================================================

def get_state_lock(username):

    with STATE_LOCKS_GLOBAL:

        if username not in STATE_LOCKS:
            STATE_LOCKS[username] = threading.Lock()

        return STATE_LOCKS[username]


def state_path(username):

    safe = "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in username
    )

    return os.path.join(
        STATE_DIR,
        f"{safe}_sent.json"
    )


def load_state(username):

    path = state_path(username)

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(data, dict):

            return {
                "sent": {},
                "deleted": {},
                "unknown": {}
            }

        data.setdefault("sent", {})
        data.setdefault("deleted", {})
        data.setdefault("unknown", {})

        return data

    except Exception:

        return {
            "sent": {},
            "deleted": {},
            "unknown": {}
        }


def save_state(username, state):

    path = state_path(username)
    tmp = path + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        tmp,
        path
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):

    return (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/{method}"
    )


def send_telegram_message(text):

    start = time.monotonic()

    try:

        response = requests.post(
            telegram_url("sendMessage"),
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": False
            },
            timeout=15
        )

        elapsed = time.monotonic() - start

        log(
            f"Telegram message finished in "
            f"{elapsed:.2f}s "
            f"(HTTP {response.status_code})"
        )

        if response.ok:
            return True

        log(
            f"Telegram message failed: "
            f"{response.text[:500]}"
        )

        return False

    except Exception as e:

        log(
            f"Telegram message exception: {e}"
        )

        return False


def send_telegram_video(
    video_path,
    caption=None
):

    for attempt in range(
        1,
        TELEGRAM_UPLOAD_ATTEMPTS + 1
    ):

        start = time.monotonic()

        try:

            log(
                f"Telegram video upload attempt "
                f"{attempt}/{TELEGRAM_UPLOAD_ATTEMPTS}"
            )

            with open(
                video_path,
                "rb"
            ) as video_file:

                files = {
                    "video": (
                        os.path.basename(video_path),
                        video_file,
                        "video/mp4"
                    )
                }

                data = {
                    "chat_id": CHAT_ID
                }

                if caption:
                    data["caption"] = caption

                response = requests.post(
                    telegram_url("sendVideo"),
                    data=data,
                    files=files,
                    timeout=TELEGRAM_TIMEOUT_SEC
                )

            elapsed = time.monotonic() - start

            log(
                f"Telegram upload finished in "
                f"{elapsed:.2f}s "
                f"(HTTP {response.status_code})"
            )

            if response.ok:
                return True

            log(
                f"Telegram video upload failed: "
                f"{response.text[:500]}"
            )

        except Exception as e:

            elapsed = time.monotonic() - start

            log(
                f"Telegram upload exception after "
                f"{elapsed:.2f}s: {e}"
            )

        if attempt < TELEGRAM_UPLOAD_ATTEMPTS:
            time.sleep(2)

    return False


# ============================================================
# TIKTOK URL
# ============================================================

def video_url(username, video_id):

    return (
        f"https://www.tiktok.com/"
        f"@{username}/video/{video_id}"
    )


# ============================================================
# ACCOUNT CHECK
# ============================================================

def latest_items(username):

    command = [
        "yt-dlp",
        "--flat-playlist",
        "-j",
        "--playlist-end",
        str(CHECK_BATCH),
        "--user-agent",
        "Mozilla/5.0",
        f"https://www.tiktok.com/@{username}"
    ]

    start = time.monotonic()

    # IMPORTANT:
    #
    # Wait for a slot instead of skipping the account.
    #
    # This keeps TikTok requests limited to 2 at once,
    # but means accounts are queued rather than lost.
    ACCOUNT_SUBPROCESS_SEMAPHORE.acquire()

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=ACCOUNT_CHECK_TIMEOUT_SEC
        )

    except subprocess.TimeoutExpired:

        elapsed = time.monotonic() - start

        log(
            f"@{username}: account check timed out "
            f"after {elapsed:.2f}s"
        )

        return []

    except Exception as e:

        log(
            f"@{username}: account check exception: "
            f"{e}"
        )

        return []

    finally:

        ACCOUNT_SUBPROCESS_SEMAPHORE.release()

    elapsed = time.monotonic() - start

    if result.returncode != 0:

        log(
            f"@{username}: account check failed "
            f"after {elapsed:.2f}s: "
            f"{result.stderr[-500:]}"
        )

        return []

    items = []

    for line in result.stdout.splitlines():

        line = line.strip()

        if not line:
            continue

        try:
            data = json.loads(line)

        except Exception:
            continue

        video_id = data.get("id")

        if not video_id:
            continue

        items.append({
            "id": str(video_id),
            "title": data.get("title") or "",
            "url": video_url(
                username,
                video_id
            )
        })

    log(
        f"@{username}: account check finished "
        f"in {elapsed:.2f}s, "
        f"found {len(items)} videos"
    )

    return items


# ============================================================
# VIDEO DOWNLOAD
# ============================================================

def try_download_once(
    username,
    video_id,
    workdir
):

    url = video_url(
        username,
        video_id
    )

    output_template = os.path.join(
        workdir,
        "%(id)s.%(ext)s"
    )

    command = [
        "yt-dlp",
        "-o",
        output_template,
        "-f",
        "best[ext=mp4]/best",
        "--no-playlist",
        "--user-agent",
        "Mozilla/5.0",
        url
    ]

    start = time.monotonic()

    log(
        f"DOWNLOAD START: {url}"
    )

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT_SEC
        )

    except subprocess.TimeoutExpired:

        elapsed = time.monotonic() - start

        log(
            f"DOWNLOAD TIMEOUT after "
            f"{elapsed:.2f}s: {url}"
        )

        return None

    except Exception as e:

        log(
            f"DOWNLOAD EXCEPTION: {e}"
        )

        return None

    elapsed = time.monotonic() - start

    if result.returncode != 0:

        log(
            f"DOWNLOAD FAILED after "
            f"{elapsed:.2f}s: {url}"
        )

        if result.stderr:

            log(
                f"yt-dlp error: "
                f"{result.stderr[-800:]}"
            )

        return None

    files = []

    try:

        for name in os.listdir(workdir):

            path = os.path.join(
                workdir,
                name
            )

            if os.path.isfile(path):
                files.append(path)

    except Exception:

        return None

    if not files:

        log(
            f"DOWNLOAD FAILED: no file produced "
            f"after {elapsed:.2f}s"
        )

        return None

    mp4_files = [
        path
        for path in files
        if path.lower().endswith(".mp4")
    ]

    if mp4_files:

        path = max(
            mp4_files,
            key=os.path.getsize
        )

    else:

        path = max(
            files,
            key=os.path.getsize
        )

    try:

        size = os.path.getsize(path)

    except Exception:

        return None

    if size <= 0:

        log(
            "DOWNLOAD FAILED: empty file"
        )

        return None

    log(
        f"DOWNLOAD SUCCESS in "
        f"{elapsed:.2f}s: {path}"
    )

    return path


def download_video(
    username,
    video_id
):

    url = video_url(
        username,
        video_id
    )

    for attempt in range(
        1,
        DOWNLOAD_ATTEMPTS + 1
    ):

        log(
            f"Download attempt "
            f"{attempt}/{DOWNLOAD_ATTEMPTS}: "
            f"{url}"
        )

        workdir = tempfile.mkdtemp(
            prefix="tiktok_"
        )

        result = try_download_once(
            username,
            video_id,
            workdir
        )

        if result:

            return result

        shutil.rmtree(
            workdir,
            ignore_errors=True
        )

        if attempt < DOWNLOAD_ATTEMPTS:

            time.sleep(
                DOWNLOAD_RETRY_DELAY_SEC
            )

    return None


# ============================================================
# NEW VIDEO PIPELINE
# ============================================================

def handle_new_video(
    username,
    item
):

    video_id = item["id"]
    url = item["url"]

    pipeline_start = time.monotonic()

    log(
        f"NEW VIDEO PIPELINE START "
        f"@{username} / {video_id}"
    )

    # --------------------------------------------------------
    # 1. SEND LINK IMMEDIATELY
    # --------------------------------------------------------

    link_start = time.monotonic()

    link_text = (
        f"@{username} posted a new TikTok:\n"
        f"{url}"
    )

    link_success = send_telegram_message(
        link_text
    )

    link_elapsed = (
        time.monotonic() - link_start
    )

    log(
        f"@{username} / {video_id}: "
        f"link sent={link_success} "
        f"in {link_elapsed:.2f}s"
    )

    # --------------------------------------------------------
    # 2. DOWNLOAD VIDEO
    # --------------------------------------------------------

    download_start = time.monotonic()

    video_path = download_video(
        username,
        video_id
    )

    download_elapsed = (
        time.monotonic() - download_start
    )

    if not video_path:

        log(
            f"@{username} / {video_id}: "
            f"download failed after "
            f"{download_elapsed:.2f}s"
        )

        return False

    # --------------------------------------------------------
    # 3. SEND VIDEO AS SEPARATE MESSAGE
    # --------------------------------------------------------

    telegram_start = time.monotonic()

    video_success = send_telegram_video(
        video_path
    )

    telegram_elapsed = (
        time.monotonic() - telegram_start
    )

    # --------------------------------------------------------
    # CLEANUP
    # --------------------------------------------------------

    try:

        workdir = os.path.dirname(
            video_path
        )

        shutil.rmtree(
            workdir,
            ignore_errors=True
        )

    except Exception:
        pass

    total_elapsed = (
        time.monotonic() - pipeline_start
    )

    log(
        f"PIPELINE COMPLETE "
        f"@{username} / {video_id} | "
        f"download={download_elapsed:.2f}s | "
        f"telegram={telegram_elapsed:.2f}s | "
        f"total={total_elapsed:.2f}s | "
        f"link={link_success} | "
        f"video={video_success}"
    )

    return video_success


# ============================================================
# ACCOUNT PROCESSING
# ============================================================

def process_account(username):

    items = latest_items(
        username
    )

    if not items:
        return

    lock = get_state_lock(
        username
    )

    new_items = []

    with lock:

        state = load_state(
            username
        )

        sent = state.setdefault(
            "sent",
            {}
        )

        # Reverse so oldest detected item is queued first.
        for item in reversed(items):

            video_id = item["id"]

            if video_id in sent:
                continue

            # Mark it immediately.
            #
            # This prevents duplicate downloads if two checks
            # overlap.
            sent[video_id] = {
                "first_seen": int(
                    time.time()
                ),
                "url": item["url"],
                "video_sent": False
            }

            new_items.append(
                item
            )

        save_state(
            username,
            state
        )

    if not new_items:
        return

    log(
        f"@{username}: detected "
        f"{len(new_items)} new video(s)"
    )

    # IMPORTANT:
    #
    # Do NOT run deletion checking here.
    #
    # Submit directly to the independent download executor.
    for item in new_items:

        DOWNLOAD_EXECUTOR.submit(
            handle_new_video,
            username,
            item
        )


# ============================================================
# VIDEO EXISTENCE / DELETION CHECK
# ============================================================

def check_video_exists(
    username,
    video_id
):

    url = video_url(
        username,
        video_id
    )

    command = [
        "yt-dlp",
        "--simulate",
        "--no-playlist",
        "--user-agent",
        "Mozilla/5.0",
        url
    ]

    # Deletion checks share ONLY the conservative
    # account/deletion semaphore.
    ACCOUNT_SUBPROCESS_SEMAPHORE.acquire()

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20
        )

    except subprocess.TimeoutExpired:

        log(
            f"Deletion check timed out: {url}"
        )

        return None

    except Exception as e:

        log(
            f"Deletion check exception: {e}"
        )

        return None

    finally:

        ACCOUNT_SUBPROCESS_SEMAPHORE.release()

    if result.returncode == 0:
        return True

    stderr = (
        result.stderr or ""
    ).lower()

    deleted_words = [
        "not available",
        "does not exist",
        "has been removed",
        "video unavailable",
        "video is unavailable",
        "content is unavailable",
        "private video"
    ]

    if any(
        word in stderr
        for word in deleted_words
    ):

        return False

    return None


def deletion_sweep(username):

    lock = get_state_lock(
        username
    )

    with lock:

        state = load_state(
            username
        )

        sent = state.setdefault(
            "sent",
            {}
        )

        deleted = state.setdefault(
            "deleted",
            {}
        )

        unknown = state.setdefault(
            "unknown",
            {}
        )

        now = int(
            time.time()
        )

        candidates = []

        for video_id, info in list(
            sent.items()
        ):

            first_seen = int(
                info.get(
                    "first_seen",
                    now
                )
            )

            age = now - first_seen

            # Never immediately deletion-check a newly
            # detected video.
            if age < 120:
                continue

            if video_id in deleted:
                continue

            candidates.append(
                video_id
            )

    if not candidates:
        return

    log(
        f"@{username}: starting deletion sweep "
        f"({len(candidates)} videos)"
    )

    for video_id in candidates:

        result = check_video_exists(
            username,
            video_id
        )

        with lock:

            state = load_state(
                username
            )

            sent = state.setdefault(
                "sent",
                {}
            )

            deleted = state.setdefault(
                "deleted",
                {}
            )

            unknown = state.setdefault(
                "unknown",
                {}
            )

            if result is True:

                unknown.pop(
                    video_id,
                    None
                )

            elif result is False:

                previous = deleted.get(
                    video_id,
                    0
                )

                previous += 1

                deleted[video_id] = previous

                unknown.pop(
                    video_id,
                    None
                )

                log(
                    f"@{username}: deletion "
                    f"confirmation "
                    f"{previous}/"
                    f"{DELETE_FAIL_THRESHOLD} "
                    f"for {video_id}"
                )

                if previous >= DELETE_FAIL_THRESHOLD:

                    sent.pop(
                        video_id,
                        None
                    )

                    deleted.pop(
                        video_id,
                        None
                    )

                    unknown.pop(
                        video_id,
                        None
                    )

                    log(
                        f"@{username}: confirmed deleted "
                        f"{video_id}"
                    )

            else:

                count = unknown.get(
                    video_id,
                    0
                )

                count += 1

                unknown[video_id] = count

                log(
                    f"@{username}: unknown deletion "
                    f"result {count}/"
                    f"{UNKNOWN_FAIL_THRESHOLD} "
                    f"for {video_id}"
                )

            save_state(
                username,
                state
            )

        time.sleep(
            DELETE_CHECK_DELAY_SEC
        )


# ============================================================
# ACCOUNT WATCH LOOP
# ============================================================

def watch_account(username):

    log(
        f"Started watcher for @{username}"
    )

    # Small startup staggering.
    stagger = (
        sum(ord(c) for c in username)
        % 10
    )

    if stagger:
        time.sleep(stagger)

    while True:

        start = time.monotonic()

        try:

            process_account(
                username
            )

        except Exception as e:

            log(
                f"@{username}: watcher exception: "
                f"{e}"
            )

        elapsed = (
            time.monotonic() - start
        )

        # Aim for roughly 30 seconds between
        # the START of checks for each account.
        sleep_for = max(
            0,
            CHECK_EVERY_SEC - elapsed
        )

        time.sleep(
            sleep_for
        )


# ============================================================
# DELETION LOOP
# ============================================================

def deletion_loop(username):

    stagger = (
        sum(ord(c) for c in username)
        % 60
    )

    if stagger:
        time.sleep(stagger)

    while True:

        start = time.monotonic()

        try:

            deletion_sweep(
                username
            )

        except Exception as e:

            log(
                f"@{username}: deletion sweep exception: "
                f"{e}"
            )

        elapsed = (
            time.monotonic() - start
        )

        sleep_for = max(
            0,
            DELETE_CHECK_EVERY_SEC - elapsed
        )

        time.sleep(
            sleep_for
        )


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return "TikTok bot running"


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "accounts": len(USERNAMES)
    })


# ============================================================
# MANUAL CHECK
# ============================================================

@app.route("/check")
def manual_check():

    key = request.args.get(
        "key",
        ""
    )

    if ADMIN_KEY and key != ADMIN_KEY:

        return jsonify({
            "error": "unauthorized"
        }), 401

    for username in USERNAMES:

        ACCOUNT_CHECK_EXECUTOR.submit(
            process_account,
            username
        )

    return jsonify({
        "status": "checks queued"
    })


# ============================================================
# MANUAL DELETION CHECK
# ============================================================

@app.route("/check_deletions")
def manual_deletions():

    key = request.args.get(
        "key",
        ""
    )

    if ADMIN_KEY and key != ADMIN_KEY:

        return jsonify({
            "error": "unauthorized"
        }), 401

    for username in USERNAMES:

        DELETION_EXECUTOR.submit(
            deletion_sweep,
            username
        )

    return jsonify({
        "status": "deletion checks queued"
    })


# ============================================================
# START BACKGROUND WORKERS
# ============================================================

def start_background_threads():

    for username in USERNAMES:

        threading.Thread(
            target=watch_account,
            args=(username,),
            daemon=True
        ).start()

        threading.Thread(
            target=deletion_loop,
            args=(username,),
            daemon=True
        ).start()

        log(
            f"Background workers started for "
            f"@{username}"
        )


start_background_threads()


# ============================================================
# RENDER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    log(
        f"Starting Flask server on port {port}"
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
