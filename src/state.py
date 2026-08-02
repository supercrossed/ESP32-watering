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

_events = []
_moisture_history = []


def _valve_slot(name):
    return valves.setdefault(
        name,
        {
            "is_open": False,
            "opened_at": None,
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
    if now - _last_history_ts < 55:
        return  # live readings update every cycle; history stays 1/minute
    _last_history_ts = now
    _moisture_history.append({
        "t": now,
        "readings": [{"name": r["name"], "percent": r["percent"]} for r in readings],
    })
    if len(_moisture_history) > MAX_MOISTURE_POINTS:
        del _moisture_history[0]


def get_events():
    return _events


def get_moisture_history():
    return _moisture_history
