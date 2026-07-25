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
