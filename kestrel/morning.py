"""Builds one audio file from the newest episode of a podcast feed and a text.

- Feed: the newest item whose title contains MORNING_FILTER.
- Text: description of the item in the Home Assistant to-do list MORNING_TODO,
  but only when the item is from today. It is spoken with a Home Assistant
  text-to-speech engine.
- Result: MORNING_OUT (mp3). It is rebuilt every few minutes on weekday
  mornings whenever the episode or the text changed.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

FEED = os.environ.get("MORNING_FEED", "").replace("null", "")
FILTER = os.environ.get("MORNING_FILTER", "").replace("null", "").lower()
TODO = os.environ.get("MORNING_TODO", "").replace("null", "")
TTS_ENGINE = os.environ.get("MORNING_TTS_ENGINE", "tts.home_assistant_cloud")
TTS_LANG = os.environ.get("MORNING_TTS_LANG", "de-DE")
OUT = os.environ.get("MORNING_OUT", "/media/kestrel/morgen.mp3")
STATE_FILE = OUT + ".state"
FROM_TIME = os.environ.get("MORNING_FROM", "06:30")
TO_TIME = os.environ.get("MORNING_TO", "09:00")
PUBLIC_DIR = os.environ.get("MORNING_PUBLIC_DIR", "").replace("null", "")
SENSOR = os.environ.get("MORNING_SENSOR", "sensor.kestrel_morgen")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Berlin"))
HA_API = os.environ.get("HA_API", "http://supervisor/core/api")
HA_ROOT = HA_API.rsplit("/api", 1)[0]
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
UA = "kestrel/0.4"


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] morning: {msg}", flush=True)


def ha(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        path if path.startswith("http") else HA_API + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = resp.read()
    return payload if raw else json.loads(payload or b"null")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def newest_episode():
    channel = ET.fromstring(fetch(FEED)).find("channel")
    items = []
    for it in channel.findall("item"):
        title = it.findtext("title") or ""
        enc = it.find("enclosure")
        if FILTER in title.lower() and enc is not None:
            items.append((it.findtext("pubDate") or "", title, enc.get("url")))
    if not items:
        return None
    from email.utils import parsedate_to_datetime
    items.sort(key=lambda x: parsedate_to_datetime(x[0]), reverse=True)
    return items[0][1], items[0][2]


def todays_text():
    """Newest to-do item from today -> its description, else ''."""
    if not TODO:
        return ""
    resp = ha("POST", "/services/todo/get_items?return_response", {
        "entity_id": TODO, "status": ["needs_action", "completed"]})
    items = (resp.get("service_response", {}).get(TODO, {}) or {}).get("items", [])
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    best = ""
    for item in items:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", item.get("summary", ""))
        if m and m.group(1) == today and item.get("description"):
            best = item["description"].strip()
    return best


def chunks(text, limit=800):
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s*\n\s*", " ", text))
    out, cur = [], ""
    for s in sentences:
        while len(s) > limit:
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:cut])
            s = s[cut:].strip()
        if len(cur) + len(s) + 1 > limit and cur:
            out.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    return out


def speak(message):
    info = ha("POST", "/tts_get_url", {"engine_id": TTS_ENGINE, "message": message, "language": TTS_LANG})
    return ha("GET", HA_ROOT + info["path"], raw=True)


def build(press_url, text, workdir):
    inputs = []
    press = os.path.join(workdir, "press.mp3")
    with open(press, "wb") as f:
        f.write(fetch(press_url))
    inputs.append(press)
    if text:
        pause = os.path.join(workdir, "pause.mp3")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", "1.2", "-b:a", "96k", pause], check=True)
        inputs.append(pause)
        for i, part in enumerate(chunks(text)):
            path = os.path.join(workdir, f"tts{i}.mp3")
            with open(path, "wb") as f:
                f.write(speak(part))
            inputs.append(path)
    cmd = [FFMPEG, "-y", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", path]
    labels = "".join(f"[a{i}]" for i in range(len(inputs)))
    norm = ";".join(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono[a{i}]" for i in range(len(inputs)))
    cmd += ["-filter_complex", f"{norm};{labels}concat=n={len(inputs)}:v=0:a=1[out]",
            "-map", "[out]", "-b:a", "96k", "-ar", "44100", "-ac", "1", os.path.join(workdir, "out.mp3")]
    subprocess.run(cmd, check=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    staging = OUT + ".part"  # same directory/device as OUT, so the final rename is atomic
    shutil.copyfile(os.path.join(workdir, "out.mp3"), staging)
    os.replace(staging, OUT)
    if PUBLIC_DIR:
        os.makedirs(PUBLIC_DIR, exist_ok=True)
        target = os.path.join(PUBLIC_DIR, "morgen.mp3")
        shutil.copyfile(OUT, target + ".part")
        os.replace(target + ".part", target)


def in_window(now):
    if now.weekday() > 4:
        return False
    hh, mm = (int(x) for x in FROM_TIME.split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    hh, mm = (int(x) for x in TO_TIME.split(":"))
    end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start <= now <= end


def read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def publish(title, with_text):
    try:
        ha("POST", f"/states/{SENSOR}", {
            "state": "bereit",
            "attributes": {"episode": title, "mit_zusammenfassung": with_text,
                           "gebaut": datetime.now(TZ).isoformat(timespec="seconds"),
                           "friendly_name": "Morgen-Audio", "icon": "mdi:newspaper-variant"}})
    except Exception as exc:  # noqa: BLE001 - the sensor is optional
        log(f"sensor not updated: {exc}")


def tick():
    episode = newest_episode()
    if not episode:
        log("no matching episode")
        return
    title, url = episode
    text = todays_text()
    key = hashlib.sha1(f"{url}|{text}".encode()).hexdigest()
    if read_state().get("key") == key and os.path.exists(OUT):
        publish(title, bool(text))  # keeps the sensor alive after a Home Assistant restart
        return
    log(f"building: {title}; summary {'yes' if text else 'no'} ({len(text)} chars)")
    with tempfile.TemporaryDirectory() as workdir:
        build(url, text, workdir)
    with open(STATE_FILE, "w") as f:
        json.dump({"key": key, "title": title}, f)
    publish(title, bool(text))
    log(f"done: {os.path.getsize(OUT)} bytes")


def build_now():
    """Manual run (POST /morning/build), ignores the weekday window."""
    if not FEED:
        raise RuntimeError("disabled (no feed configured)")
    tick()


def run():
    if not FEED:
        log("disabled (no feed configured)")
        return
    log(f"started: {FEED} / '{FILTER}' / {TODO}, weekdays {FROM_TIME}-{TO_TIME}")
    while True:
        try:
            if in_window(datetime.now(TZ)):
                tick()
        except Exception as exc:  # noqa: BLE001 - try again at the next round
            log(f"error: {exc}")
        time.sleep(300)
