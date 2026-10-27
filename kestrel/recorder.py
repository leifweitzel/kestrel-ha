"""Records a stream once a day and plays it back time-shifted.

- Recording: every day from START_TIME for DURATION_MIN minutes into REC_DIR,
  together with a small index (wall clock -> byte offset).
- Playback: GET /today.mp3 returns the latest recording from the beginning and
  keeps following the file while it is still growing. The playback is composed
  of two parts (start..PART1_TO, PART1_TO..PART2_TO). The second part is only
  sent when the Home Assistant entity PART2_SWITCH is "on"; switching it off while the
  second part plays ends the stream. Everything after PART2_TO is never sent.
- Old recordings are deleted after KEEP_HOURS hours.
"""
import bisect
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

STREAM_URL = os.environ["STREAM_URL"]
START_TIME = os.environ.get("START_TIME", "18:00")
DURATION_MIN = int(os.environ.get("DURATION_MIN", "150"))
KEEP_HOURS = int(os.environ.get("KEEP_HOURS", "36"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Berlin"))
DELAY = int(os.environ.get("DELAY_SECONDS", "0"))
PART1_TO = os.environ.get("PART1_TO", "")
PART2_TO = os.environ.get("PART2_TO", "")
PART2_SWITCH = os.environ.get("PART2_SWITCH", "")
REC_DIR = os.environ.get("REC_DIR", "/media/kestrel")
PORT = int(os.environ.get("PORT", "8765"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API = os.environ.get("HA_API", "http://supervisor/core/api")
CHUNK = 16 * 1024

state = {"recording": None}
state_lock = threading.Lock()

# Players treat a stream that ends as a dropout and reconnect. After a client has
# received everything it is entitled to, it gets "410 Gone" for a short while
# instead of the recording from the beginning again.
FINISHED_LOCK_SECONDS = 180
finished = {}


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def now():
    return datetime.now(TZ)


def at(day, hms):
    parts = [int(x) for x in hms.split(":")]
    hh, mm = parts[0], parts[1]
    ss = parts[2] if len(parts) > 2 else 0
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=TZ)


def window_for(day):
    start = at(day, START_TIME)
    return start, start + timedelta(minutes=DURATION_MIN)


def file_for(day):
    return os.path.join(REC_DIR, f"{day:%Y-%m-%d}.mp3")


def cleanup():
    cutoff = time.time() - KEEP_HOURS * 3600
    for name in os.listdir(REC_DIR):
        path = os.path.join(REC_DIR, name)
        if name.endswith((".mp3", ".idx")) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            log(f"deleted {name}")


def record(path, end):
    """Records until `end` and reconnects after dropouts."""
    with open(path, "ab") as out, open(path[:-4] + ".idx", "a") as idx:
        last_idx = 0.0
        while now() < end:
            try:
                req = urllib.request.Request(STREAM_URL, headers={"User-Agent": "kestrel/0.2"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    log("connected")
                    while now() < end:
                        data = resp.read(CHUNK)
                        if not data:
                            break
                        out.write(data)
                        out.flush()
                        t = time.time()
                        if t - last_idx >= 1.0:
                            idx.write(f"{t:.1f} {out.tell()}\n")
                            idx.flush()
                            last_idx = t
            except Exception as exc:  # noqa: BLE001 - network errors must not end the recording
                log(f"connection error: {exc}")
            if now() < end:
                time.sleep(5)


def scheduler():
    while True:
        try:
            cleanup()
            today = now().date()
            start, end = window_for(today)
            if now() >= end:
                start, end = window_for(today + timedelta(days=1))
            if now() < start:
                time.sleep(min(30, max(1, (start - now()).total_seconds())))
                continue
            path = file_for(start.date())
            with state_lock:
                state["recording"] = path
            log(f"recording starts (late by {(now() - start).total_seconds():.0f}s)")
            record(path, end)
            log("recording finished")
        except Exception as exc:  # noqa: BLE001
            log(f"scheduler error: {exc}")
            time.sleep(10)
        finally:
            with state_lock:
                state["recording"] = None
        time.sleep(2)


def latest_file():
    files = sorted(f for f in os.listdir(REC_DIR) if f.endswith(".mp3"))
    return os.path.join(REC_DIR, files[-1]) if files else None


def read_index(path):
    times, offsets = [], []
    try:
        with open(path[:-4] + ".idx") as f:
            for line in f:
                a, _, b = line.partition(" ")
                if b:
                    times.append(float(a))
                    offsets.append(int(b))
    except FileNotFoundError:
        pass
    return times, offsets


def is_active(path):
    with state_lock:
        return state["recording"] == path


def offset_at(path, ts):
    """Byte offset in the recording for wall clock `ts`, or None if not reached yet."""
    times, offsets = read_index(path)
    if not times:
        return None
    if ts <= times[0]:
        return 0
    i = bisect.bisect_left(times, ts)
    if i < len(times):
        return offsets[i]
    return None if is_active(path) else offsets[-1]


def part2_wanted():
    if not PART2_SWITCH or not SUPERVISOR_TOKEN:
        return False
    try:
        req = urllib.request.Request(
            f"{HA_API}/states/{PART2_SWITCH}",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp).get("state") == "on"
    except Exception as exc:  # noqa: BLE001
        log(f"switch not readable: {exc}")
        return False


def rec_day(path):
    return datetime.strptime(os.path.basename(path)[:10], "%Y-%m-%d").date()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        log("http " + fmt % args)

    def do_HEAD(self):
        self.serve(head=True)

    def do_GET(self):
        self.serve(head=False)

    def serve(self, head):
        path = self.path.partition("?")[0]
        if path == "/status":
            return self.status(head)
        if path == "/today.mp3":
            return self.play(head)
        if path == "/full.mp3":
            return self.play(head, raw=True)
        self.send_error(404)

    def status(self, head):
        info = latest_file()
        body = json.dumps({
            "recording": state["recording"] is not None,
            "file": os.path.basename(info) if info else None,
            "bytes": os.path.getsize(info) if info else 0,
            "part2_wanted": part2_wanted(),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def copy_range(self, f, target, start, stop_ts=None, keep_going=None):
        """Sends the recording from byte `start` up to wall clock `stop_ts`.

        While the stop position is not recorded yet, the file is followed
        live. stop_ts=None -> until the recording ends. `keep_going` is polled
        every two seconds; once it returns False the transfer ends (used to
        skip the second part while it is playing). Returns the byte position
        reached and whether the stop position was reached.
        """
        f.seek(start)
        pos = start
        stop = None
        idle = 0.0
        last_check = last_stop = 0.0
        while True:
            t = time.time()
            if keep_going and t - last_check >= 2.0:
                last_check = t
                if not keep_going():
                    return pos, False
            if stop is None and stop_ts is not None and t - last_stop >= 1.0:
                last_stop = t
                stop = offset_at(target, stop_ts)
            if stop is not None and pos >= stop:
                return pos, True
            want = CHUNK if stop is None else min(CHUNK, stop - pos)
            data = f.read(want)
            if data:
                self.wfile.write(data)
                pos += len(data)
                idle = 0.0
                continue
            if not is_active(target):
                return pos, stop is not None and pos >= stop
            time.sleep(0.5)
            idle += 0.5
            if idle > 120:
                return pos, False

    def play(self, head, raw=False):
        target = latest_file()
        if not target:
            return self.send_error(404, "no recording")
        client = self.client_address[0]
        if not raw and time.time() - finished.get(client, 0) < FINISHED_LOCK_SECONDS:
            return self.send_error(410, "already played")
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head:
            return
        try:
            with open(target, "rb") as f:
                if raw:
                    self.copy_range(f, target, 0)
                    return
                day = rec_day(target)
                p1 = at(day, PART1_TO).timestamp() + DELAY if PART1_TO else None
                p2 = at(day, PART2_TO).timestamp() + DELAY if PART2_TO else None
                if p1 is None:
                    self.copy_range(f, target, 0)
                    return
                pos, done = self.copy_range(f, target, 0, p1)
                if done and p2 is not None and part2_wanted():
                    self.copy_range(f, target, pos, p2, keep_going=part2_wanted)
                finished[client] = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    os.makedirs(REC_DIR, exist_ok=True)
    log(f"start: {START_TIME} for {DURATION_MIN} min, parts to {PART1_TO}/{PART2_TO}, delay {DELAY}s")
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
