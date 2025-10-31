import os, time, requests, tempfile, subprocess, threading
from flask import Flask

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
USERNAME = os.environ["TIKTOK_USERNAME"]

RSS = f"https://rsshub.app/tiktok/user/{USERNAME}"
LAST_FILE = "last_id.txt"

app = Flask(__name__)

def read_last():
    try:
        with open(LAST_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def write_last(v):
    with open(LAST_FILE, "w", encoding="utf-8") as f:
        f.write(v)

def latest_item():
    r = requests.get(RSS, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
    if r.status_code != 200 or not r.text:
        return None, None
    t = r.text
    i = t.find("<item")
    if i == -1:
        return None, None
    l1 = t.find("<link>", i); l2 = t.find("</link>", l1)
    link = t[l1+6:l2].strip() if l1!=-1 and l2!=-1 else None
    tt1 = t.find("<title>", i); tt2 = t.find("</title>", tt1)
    title = t[tt1+7:tt2].strip() if tt1!=-1 and tt2!=-1 else ""
    return link, title

def tg_send_text(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    except Exception:
        pass

def tg_send_video(path, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    with open(path, "rb") as f:
        requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"video": f}, timeout=120)

def download_video(url):
    tmpdir = tempfile.mkdtemp()
    out = os.path.join(tmpdir, "%(id)s.%(ext)s")
    cmd = ["python3", "-m", "yt_dlp", "-o", out, "-f", "best[ext=mp4]/best", url]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name in os.listdir(tmpdir):
            if name.lower().endswith((".mp4", ".webm", ".mkv")):
                return os.path.join(tmpdir, name)
    except subprocess.CalledProcessError:
        return None
    return None

def loop():
    tg_send_text(f"👋 Bot online. Watching @{USERNAME} every 60s.")
    last = read_last()
    while True:
        try:
            link, title = latest_item()
            if link:
                vid_id = link.rstrip("/").split("/")[-1]
                if vid_id and vid_id != last:
                    caption = f"🎬 New video from @{USERNAME}\n{title}\nOriginal: {link}"
                    path = download_video(link)
                    if path:
                        try:
                            tg_send_video(path, caption=caption)
                        except Exception:
                            tg_send_text(caption)
                    else:
                        tg_send_text(caption)
                    write_last(vid_id)
                    last = vid_id
        except Exception:
            pass
        time.sleep(60)

# simple health endpoint so Render treats it as a web service
@app.get("/")
def health():
    return "OK",

if __name__ == "__main__":
    # start background checker
    threading.Thread(target=loop, daemon=True).start()
    # start tiny web server (Render expects a listening port)
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
