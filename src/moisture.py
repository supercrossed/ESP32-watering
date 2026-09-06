# moisture.py
import time

def raw_to_percent(raw, dry_raw, wet_raw):
    """Map a raw ADC reading to 0-100% wetness using two-point calibration.
    dry_raw = raw reading in dry air, wet_raw = raw reading fully submerged.
    Assumes dry_raw > wet_raw (voltage drops as moisture increases), which
    is the typical behavior for capacitive sensors including AITRIP.
    """
    if dry_raw == wet_raw:
        return 0.0
    pct = (dry_raw - raw) / (dry_raw - wet_raw) * 100.0
    if pct < 0:
        pct = 0.0
    if pct > 100:
        pct = 100.0
    return round(pct, 1)


def read_all(ads_boards, zones, thresholds):
    """Read every configured zone. `ads_boards` is a list of ADS1115 driver
    instances sharing the I2C bus (one per board/address). zone["channel"]
    is a global index: board 1 = channels 0-3, board 2 = 4-7, and so on.
    `thresholds` is a dict name->percent, normally sourced from
    settings_store so it can be changed at runtime."""
    results = []
    for zone in zones:
        ch = zone["channel"]
        board = ch // 4
        if board >= len(ads_boards):
            continue  # that board was removed - nothing to read
        # Isolate each zone. A single flaky probe (a loose connector, a long
        # run picking up noise) used to raise out of this loop and abort the
        # whole cycle, so every zone AFTER the bad one silently went unread.
        # An I2C error on one channel must not blind the others.
        try:
            raw = ads_boards[board].read(ch % 4)
        except OSError as e:
            print("zone {} read failed: {}".format(zone["name"], e))
            continue
        pct = raw_to_percent(raw, zone["dry_raw"], zone["wet_raw"])
        results.append(
            {
                "name": zone["name"],
                "raw": raw,
                "percent": pct,
                "threshold": thresholds.get(zone["name"], zone.get("threshold_percent", 30)),
            }
        )
    return results


# ---- Board presence ---------------------------------------------------
# Reading an ADS1115 that is not on the bus is neither free nor fast: the
# I2C timeout main.py passes to I2C() is not honoured by this port, so each
# doomed read blocks for ~0.8s. The loop it blocks is the same one feeding
# WiFi, servicing the web server and checking the valve cutoff, so three
# unreadable zones stretched the main loop from a 15s cycle to 45s. Every
# dashboard request then timed out, which looks exactly like a network
# fault - this is the real "WiFi breaks when I plug the sensor in", and it
# is not RF.
#
# A bus scan costs ~30ms whether anything answers or not. So probe rather
# than read: with no board present no reads are issued at all, the
# controller stays responsive, and it picks the board up again by itself
# once the wiring is fixed.
_online = None          # None until the first probe, so the very first
                        # result is reported even when it is 'nothing there'
_last_probe = None      # monotonic ticks; None = never probed


def note_scan(addresses, found):
    """Record the result of a scan someone else already did (reinit_i2c)."""
    global _online, _last_probe
    _online = [a for a in addresses if a in found]
    _last_probe = time.ticks_ms()
    return _online


def online_boards(i2c, addresses, probe_sec=60, force=False, log=None):
    """Which of `addresses` actually answer on the bus.

    Rate-limited to one scan per `probe_sec`, because the read path calls
    this every cycle. Returns the live addresses; an EMPTY LIST means
    "issue no reads at all".

    Uses the monotonic clock: an NTP sync steps the wall clock, and a
    backward step would make the elapsed time negative and stall the
    re-probe until the clock caught up. The same mistake once disabled the
    valve safety cutoff in this project."""
    global _online, _last_probe
    if (not force and _last_probe is not None
            and time.ticks_diff(time.ticks_ms(), _last_probe) < probe_sec * 1000):
        return _online or []
    _last_probe = time.ticks_ms()
    try:
        found = i2c.scan()
    except Exception as e:
        print("ADS probe failed:", e)
        found = []
    online = [a for a in addresses if a in found]
    if online != _online:
        if log:
            if online:
                log("sensors", "ADS1115 detected at {} - moisture reads resumed".format(
                    ",".join(hex(a) for a in online)))
            else:
                log("sensors",
                    "no ADS1115 answering on the I2C bus - moisture reads paused "
                    "(check the module's VCC, GND, SDA and SCL); re-checking "
                    "every {}s".format(probe_sec))
        _online = online
    return _online or []


# ---- Zone assembly and calibration -----------------------------------
# These moved out of main.py: main.py is the only module still compiled on
# the device at boot, and its CODE size decides whether the ESP-IDF C heap
# still has room for the WiFi driver's buffers afterwards. Anything that
# does not have to be compiled at boot belongs in a .mpy like this one.

# Defaults for a zone added through the web UI that has never been
# calibrated. Kept here rather than imported from settings_store so this
# module has no import cycle back into the settings layer.
DEFAULT_DRY_RAW = 17500
DEFAULT_WET_RAW = 8000


def build_zone_list(hw):
    """Every configured zone as {name, channel, dry_raw, wet_raw,
    threshold_percent}, ready for moisture.read_all().

    Shared by the moisture loop and the calibration endpoint so both read a
    zone the same way."""
    zone_channels = hw.get("zone_channels", {})
    calib = hw.get("zone_calibration", {})
    zones = []
    for name, channel in zone_channels.items():
        c = calib.get(name) or {}
        zones.append({
            "name": name,
            "channel": channel,
            "dry_raw": c.get("dry_raw", DEFAULT_DRY_RAW),
            "wet_raw": c.get("wet_raw", DEFAULT_WET_RAW),
            "threshold_percent": 30,
        })
    return zones


def sample_zone_raw(ads_boards, hw, zone_name, seconds=10, wdt_ref=None):
    """Average the raw ADC for one zone over `seconds`, for calibration.

    A single reading from a capacitive probe wanders by a few percent, so
    both calibration endpoints are averaged. Also reports the spread, which
    is how the UI can tell the user their probe hasn't settled yet.

    Returns {"raw", "samples", "min", "max", "spread"} or {"error": ...}.
    Blocks for `seconds` - the caller runs it from the main loop, not from
    an HTTP handler, and feeds the watchdog."""
    zones = [z for z in build_zone_list(hw) if z["name"] == zone_name]
    if not zones:
        return {"error": "unknown zone: " + str(zone_name)}
    zone = zones[0]
    ch = zone["channel"]
    board = ch // 4
    if board >= len(ads_boards):
        return {"error": "channel {} has no ADS1115 board".format(ch)}

    readings = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            readings.append(ads_boards[board].read(ch % 4))
        except Exception as e:
            return {"error": "sensor read failed: {}".format(e)}
        if wdt_ref:
            wdt_ref.feed()
        time.sleep(0.25)

    if not readings:
        return {"error": "no readings captured"}
    lo, hi = min(readings), max(readings)
    return {
        "raw": int(sum(readings) / len(readings)),
        "samples": len(readings),
        "min": lo,
        "max": hi,
        "spread": hi - lo,
    }
