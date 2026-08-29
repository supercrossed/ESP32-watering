# state.py
# Shared in-memory state used by main.py and web.py.
# Not persisted across reboot (except watering totals, see events.log file).

import time
import os

MAX_EVENTS = 60
# History is decimated to ONE point per minute regardless of how often
# sensors are read, and each stored point keeps only {name, percent} (the
# chart uses nothing else). 180 points = 3 hours. MicroPython dicts are
# memory-hungry: a 720-point full-fidelity buffer once consumed the whole
# heap over ~3 hours and knocked the WiFi stack over - keep this lean.
MAX_MOISTURE_POINTS = 180
_last_history_ts = 0

latest_moisture = []   # list of {"name","raw","percent","threshold"}

# Per-valve runtime state, keyed by valve name. Each entry:
# {"is_open", "opened_at", "open_reason", "last_close_ts", "last_close_reason"}
valves = {}

# Per-valve, keyed by valve name - a schedule/moisture trigger on one valve
# must not lock out watering on an unrelated valve.
last_daily_watering_ts = {}
last_supplemental_watering_ts = {}
# True once the "holding watering while sensors settle" event has been
# logged this boot - the check runs every sensor cycle, log it once.
startup_grace_logged = False

# Watering timestamps are persisted to flash so a power cut doesn't erase
# the cooldowns. Without this, unplugging and replugging the planter made
# it forget it had just watered and it would water again immediately.
WATERING_STATE_FILE = "watering_state.json"


def save_watering_state():
    """Persist the per-valve cooldown timestamps. Called after each close -
    cheap (a few hundred bytes) and only on state change, not per loop."""
    try:
        import ujson as json
    except ImportError:
        import json
    try:
        with open(WATERING_STATE_FILE, "w") as f:
            json.dump({
                "daily": last_daily_watering_ts,
                "supplemental": last_supplemental_watering_ts,
                "saved_at": time.time(),
            }, f)
    except (OSError, ValueError) as e:
        print("could not save watering state:", e)


def load_watering_state():
    """Restore cooldowns at boot. Silently ignores a missing or corrupt
    file - worst case we're back to the old behaviour for one cycle.

    Timestamps from BEFORE an NTP sync are in the 2000 epoch and would look
    like the distant past (or future) once the clock jumps, so anything not
    plausibly in the past relative to now is dropped."""
    global last_daily_watering_ts, last_supplemental_watering_ts
    try:
        import ujson as json
    except ImportError:
        import json
    try:
        with open(WATERING_STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    now = time.time()

    def _sane(d):
        out = {}
        for name, ts in (d or {}).items():
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            # a timestamp in the future, or older than a week, is stale or
            # from a different clock epoch - ignore it
            if 0 < ts <= now and (now - ts) < 7 * 24 * 3600:
                out[name] = ts
        return out

    last_daily_watering_ts = _sane(data.get("daily"))
    last_supplemental_watering_ts = _sane(data.get("supplemental"))
    return bool(last_daily_watering_ts or last_supplemental_watering_ts)


last_schedule_fired = {}  # schedule id -> unix ts it last fired
boot_time = time.time()
time_synced = False  # set once NTP succeeds; schedules are held off until then
# latest environment sensor readings (AHT20/BMP280/rain), e.g.
# {"temp_c": 22.4, "humidity": 51, "pressure_hpa": 1013.2, "rain": False}
env = {}
# main-loop busy fraction over the last sample window (see main.py) - the
# closest thing to "CPU %" on a system with no OS scheduler
cpu_percent = None
# ESP-IDF C-heap (WiFi/lwIP/I2C drivers - NOT the Python GC heap): free
# bytes and largest contiguous block, sampled by main.py. When this heap
# runs out, WiFi/I2C start failing ("fail to alloc timer", "command link
# malloc error") even though gc.mem_free() looks fine.
# Last link-health probe result (see wifi.lan_healthy): True = packets
# reach the gateway, False = associated but nothing comes back ("zombie"),
# None = not probed yet.
lan_ok = None

idf_free = None
idf_largest = None

# ---- OTA update status (see updater.py) ----
last_update_check = None    # unix ts of the last successful manifest fetch
last_update_install = None  # unix ts of the last applied update
update_available = None     # version string if an update is waiting, else None
update_error = None         # last check/apply error, for the dashboard
update_in_progress = False  # blocks watering triggers while files are swapping
# Set by web.py when the dashboard asks for a check/apply; the MAIN LOOP
# picks it up and does the network work. The HTTP handler must never do it
# itself - a TLS handshake can block indefinitely on MicroPython and would
# freeze the loop (no responses, no valve timing) until the watchdog fires.
update_requested = None     # None | "check" | "apply"
update_last_result = None   # short human-readable outcome for the dashboard

# ---- Sensor calibration (see main.sample_zone_raw) ----
# Averaging a probe takes ~10s, far too long for an HTTP handler, so the
# dashboard queues a request here and the main loop performs it - same
# pattern as the OTA check.
calibration_requested = None   # None | {"zone": name, "point": "dry"|"wet"}
calibration_busy = False
calibration_result = None      # last capture, for the dashboard to display

_events = []
_moisture_history = []


def _valve_slot(name):
    return valves.setdefault(
        name,
        {
            "is_open": False,
            "opened_at": None,       # wall clock, for display
            "opened_ticks": None,    # monotonic ticks_ms, for the safety cutoff
            "open_reason": None,
            "last_close_ts": None,
            "last_close_reason": None,
            "last_open_duration": None,
        },
    )


def any_valve_open():
    return any(v["is_open"] for v in valves.values())


# events.log grows on every event; without a cap it eventually fills the
# ~2MB flash, at which point settings.json can no longer save. Keep it
# bounded: when it exceeds the cap, rewrite it with just the recent tail.
MAX_EVENTLOG_BYTES = 32 * 1024
_EVENTLOG_KEEP_TAIL = 8 * 1024
_event_writes = 0


def _rotate_event_log():
    try:
        size = os.stat("events.log")[6]
        if size <= MAX_EVENTLOG_BYTES:
            return
        with open("events.log", "rb") as f:
            f.seek(size - _EVENTLOG_KEEP_TAIL)
            tail = f.read()
        nl = tail.find(b"\n")  # drop the first partial line
        with open("events.log", "wb") as f:
            f.write(tail[nl + 1:])
        print("events.log rotated ({} -> {} bytes)".format(size, len(tail) - nl - 1))
    except OSError as e:
        print("event log rotation failed:", e)


def log_event(kind, detail=""):
    global _event_writes
    evt = {"t": time.time(), "kind": kind, "detail": detail}
    _events.append(evt)
    if len(_events) > MAX_EVENTS:
        del _events[0]
    print("[EVENT]", kind, detail)
    try:
        with open("events.log", "a") as f:
            f.write("{},{},{}\n".format(evt["t"], kind, detail))
    except OSError as e:
        print("event log write failed:", e)
    _event_writes += 1
    if _event_writes >= 50:  # size check costs a stat - don't do it per event
        _event_writes = 0
        _rotate_event_log()


def log_moisture(readings):
    global latest_moisture, _last_history_ts
    latest_moisture = readings
    now = time.time()
    if now - _last_history_ts >= 55:
        # live 3-hour buffer, one point per minute, RAM only
        _last_history_ts = now
        _moisture_history.append({
            "t": now,
            "readings": [{"name": r["name"], "percent": r["percent"]} for r in readings],
        })
        if len(_moisture_history) > MAX_MOISTURE_POINTS:
            del _moisture_history[0]
    _log_long_history(readings, now)


# ---- Long-term history (flash) ---------------------------------------
# A week at one point per minute would be ~10,000 dicts - several times the
# whole heap, and this project has twice had the network killed by memory
# exhaustion. So the long view lives on FLASH as compact CSV, appended one
# short line at a time and never read into RAM as a whole.
#
# Format (one line per sample):   <unix_ts>,<zone>=<pct>,<zone>=<pct>
# Zone names are included per line so the file stays valid when zones are
# added, removed or renamed mid-week.
HISTORY_FILE = "history.csv"
HISTORY_INTERVAL_SEC = 15 * 60      # 15 min -> 672 points/week, ~15KB
HISTORY_KEEP_SEC = 7 * 24 * 3600    # purge anything older than a week
_last_long_history_ts = 0
_history_writes = 0


def _log_long_history(readings, now):
    """Append one line every HISTORY_INTERVAL_SEC. Cheap: a short append,
    plus a purge scan roughly once a day."""
    global _last_long_history_ts, _history_writes
    if not readings:
        return
    # Before NTP sync the clock sits in the 2000 epoch; those timestamps
    # would sort before everything real and confuse the purge.
    if not time_synced:
        return
    if now - _last_long_history_ts < HISTORY_INTERVAL_SEC:
        return
    _last_long_history_ts = now
    try:
        parts = [str(int(now))]
        for r in readings:
            # commas and '=' would break the format; zone names are
            # user-supplied so sanitise rather than trust them
            name = str(r["name"]).replace(",", "_").replace("=", "_")
            parts.append("{}={}".format(name, r["percent"]))
        with open(HISTORY_FILE, "a") as f:
            f.write(",".join(parts) + chr(10))
    except OSError as e:
        print("history write failed:", e)
        return
    _history_writes += 1
    # ~once a day at 15-min spacing; a purge rewrites the file, so it is
    # deliberately infrequent
    if _history_writes >= 96:
        _history_writes = 0
        purge_history()


def purge_history(now=None):
    """Drop lines older than HISTORY_KEEP_SEC. Streams line by line - the
    file is never held in RAM in full."""
    if now is None:
        now = time.time()
    cutoff = now - HISTORY_KEEP_SEC
    tmp = HISTORY_FILE + ".tmp"
    kept = dropped = bad = 0
    try:
        with open(HISTORY_FILE) as src, open(tmp, "w") as dst:
            for line in src:
                if not line.strip():
                    bad += 1
                    continue
                try:
                    ts = int(line.split(",", 1)[0])
                except (ValueError, IndexError):
                    # A torn line (power cut mid-append) or corruption.
                    # Count it so the rewrite below actually happens -
                    # otherwise it would survive every purge forever.
                    bad += 1
                    continue
                if ts >= cutoff:
                    dst.write(line)
                    kept += 1
                else:
                    dropped += 1
    except OSError as e:
        print("history purge failed:", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return
    if dropped or bad:
        try:
            os.remove(HISTORY_FILE)
            os.rename(tmp, HISTORY_FILE)
            print("history purged: kept {}, dropped {}{}".format(
                kept, dropped, ", discarded {} bad line(s)".format(bad) if bad else ""))
        except OSError as e:
            print("history purge rename failed:", e)
    else:
        try:
            os.remove(tmp)
        except OSError:
            pass


def history_file_info():
    """(bytes, approximate line count) for the dashboard/API."""
    try:
        size = os.stat(HISTORY_FILE)[6]
    except OSError:
        return 0, 0
    return size, 0


def get_events():
    return _events


def get_moisture_history():
    return _moisture_history
