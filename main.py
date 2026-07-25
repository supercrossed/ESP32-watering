# main.py
import time
import gc
import machine
from machine import I2C, Pin

import config
import wifi

# ---- Boot ----
# Connect WiFi before importing anything else. The WiFi driver needs a
# solid chunk of free heap for its RX buffers right at wlan.active(True);
# compiling the app modules first can starve that allocation, producing
# "OSError: WiFi Out of Memory" / "Expected to init 10 rx buffer, actual is 4".
print("Booting planter controller...")
gc.collect()
print("Free heap before WiFi connect:", gc.mem_free(), "bytes")


def idf_heap():
    """(free, largest_block) of the ESP-IDF C heap - the one the WiFi,
    lwIP and I2C drivers allocate from. It is SEPARATE from the Python GC
    heap: gc.mem_free() can look perfectly healthy while this one is
    exhausted, at which point the network dies ("fail to alloc timer") and
    I2C fails ("command link malloc error")."""
    try:
        import esp32
        info = esp32.idf_heap_info(esp32.HEAP_DATA)
        return sum(h[1] for h in info), max(h[2] for h in info)
    except (ImportError, AttributeError, ValueError):
        return None, None


_idf_free, _idf_largest = idf_heap()
print("IDF C-heap free before WiFi:", _idf_free, "largest block:", _idf_largest)

_wifi_creds = wifi.load_creds(config)
if not wifi.connect(_wifi_creds["ssid"], _wifi_creds["password"]):
    # Couldn't join. Fresh kit / wrong password (network visible) -> open
    # the captive setup portal. Router just down (network not visible) ->
    # keep running offline; watering doesn't need WiFi, and the main loop
    # retries the connection every 30s.
    if wifi.portal_needed(_wifi_creds["ssid"]):
        import wifi_setup
        wifi_setup.run()  # serves the setup page forever; reboots on save
    print("WiFi unavailable - running offline, will keep retrying")

import state
import settings_store
from ads1x15 import ADS1115
from moisture import read_all as read_moisture
from valve import Valve
import web


def try_ntp_sync():
    """Sync the clock. Until this succeeds, time.time() sits at the 2000
    epoch and scheduled watering is held off (moisture watering still
    runs - it doesn't care what time it is). Retried from the main loop."""
    try:
        import ntptime
        # settime() JUMPS the clock (from the 2000 power-on epoch to real
        # time). boot_time was captured in the old clock domain - carry the
        # elapsed-so-far across the jump or uptime shows ~26 years.
        elapsed = time.time() - state.boot_time
        ntptime.settime()
        state.boot_time = time.time() - elapsed
        state.time_synced = True
        print("Time synced via NTP (UTC):", time.localtime())
        return True
    except Exception as e:
        print("NTP sync failed (will retry):", e)
        return False


try_ntp_sync()

settings_store.load(config)
hw = settings_store.get()["hardware"]

i2c = I2C(0, scl=Pin(hw["i2c_scl_pin"]), sda=Pin(hw["i2c_sda_pin"]))
# One driver per ADS1115 board - they all share the I2C bus, each at its
# own address. A zone's channel is a global index (board 1 = 0-3,
# board 2 = 4-7, ...) resolved in moisture.read_all().
ads_boards = [ADS1115(i2c, address=a) for a in hw["ads1115_addresses"]]

# Optional AHT20+BMP280 environment board: shares the I2C bus at fixed
# addresses, so it's auto-detected - wire it up and it just appears.
aht20 = None
bmp280 = None
try:
    import env_sensors
    _found = i2c.scan()
    if env_sensors.AHT20_ADDR in _found:
        try:
            aht20 = env_sensors.AHT20(i2c)
            print("AHT20 temp/humidity sensor detected (0x38)")
        except Exception as e:
            print("AHT20 init failed:", e)
    for _addr in env_sensors.BMP280_ADDRS:
        if _addr in _found:
            try:
                bmp280 = env_sensors.BMP280(i2c, _addr)
                print("BMP280 pressure sensor detected (0x%x)" % _addr)
            except Exception as e:
                print("BMP280 init failed:", e)
            break
except Exception as e:
    print("env sensor scan failed:", e)

# Optional LM393 rain sensor: its DO pin reads LOW when the plate is wet.
rain_pin = None
if hw.get("rain_sensor_pin") is not None:
    rain_pin = Pin(hw["rain_sensor_pin"], Pin.IN, Pin.PULL_UP)
    print("Rain sensor on GPIO", hw["rain_sensor_pin"])


def read_env_sensors():
    """Refresh state.env from whatever optional sensors are present."""
    env = {}
    if aht20:
        try:
            t, h = aht20.read()
            env["temp_c"] = round(t, 1)
            env["humidity"] = round(h)
        except Exception as e:
            print("AHT20 read failed:", e)
    if bmp280:
        try:
            t2, p = bmp280.read()
            env["pressure_hpa"] = round(p / 100.0, 1)
            if "temp_c" not in env:
                env["temp_c"] = round(t2, 1)
        except Exception as e:
            print("BMP280 read failed:", e)
    if rain_pin is not None:
        env["rain"] = rain_pin.value() == 0  # LM393 DO is LOW when wet
    state.env = env

valves = {
    v["name"]: Valve(
        v["name"], v["pin"], v["active_high"], config.MAX_VALVE_OPEN_SEC,
        flow_meter_pin=v.get("flow_meter_pin"),
    )
    for v in hw["valves"]
}
_default_valve_name = hw["valves"][0]["name"] if hw["valves"] else None

state.log_event("boot", "controller started")

_active_watering = None  # {"valve_name", "reason"} while a valve is open
_pending_valves = []  # queue of (valve_name, duration_sec, reason) - multi-valve schedules and moisture triggers share this
# Soak-and-recheck session for moisture-triggered watering: water, wait
# for the water to soak in, re-read the sensor, and water again if the
# zone still reads below its wet target - up to max_water_cycles. One dry
# event = one session; the cooldowns start from the session's LAST close.
_soak_session = None  # {"zone", "cycles", "check_at", "duration", "valves"}


def trigger_watering(valve_name, duration_sec, reason):
    """Open the named valve, and schedule (via main loop check) a close
    after duration_sec. Used by manual trigger, daily schedule, and
    moisture logic. Refuses if any valve is already open system-wide -
    only one valve runs at a time."""
    global _active_watering
    if state.any_valve_open():
        print("Watering already in progress, ignoring trigger:", valve_name, reason)
        return False
    valve = valves.get(valve_name)
    if valve is None:
        print("Unknown valve, ignoring trigger:", valve_name)
        return False
    valve.open(reason=reason)
    _active_watering = {"valve_name": valve_name, "reason": reason}
    state.log_event("watering_start", "{} {} target={}s".format(valve_name, reason, duration_sec))
    valve.target_close_at = time.time() + duration_sec
    return True


def trigger_valves(valve_names, duration_sec, reason):
    """Open the first valve now and queue the rest to run one at a time as
    each prior valve closes. Used by multi-valve schedules, multi-valve
    zones, and the web UI's per-zone Water button."""
    if not valve_names:
        return False
    if not trigger_watering(valve_names[0], duration_sec, reason):
        return False
    for vn in valve_names[1:]:
        _pending_valves.append((vn, duration_sec, reason))
    return True


def check_active_watering():
    global _active_watering
    if _active_watering is None:
        return
    valve = valves.get(_active_watering["valve_name"])
    if valve is None or not hasattr(valve, "target_close_at"):
        return
    if time.time() < valve.target_close_at:
        return
    reason = _active_watering["reason"]
    valve_name = _active_watering["valve_name"]
    valve.close(reason=reason)
    if reason == "daily_schedule":
        # used by the moisture lockout window, per-valve
        state.last_daily_watering_ts[valve_name] = time.time()
    elif reason == "moisture_trigger":
        state.last_supplemental_watering_ts[valve_name] = time.time()
    _active_watering = None

    # fire the next queued valve, if any (multi-valve schedule or a zone
    # mapped to multiple valves) - always sequential, one at a time
    if _pending_valves:
        next_valve_name, next_duration, next_reason = _pending_valves.pop(0)
        trigger_watering(next_valve_name, next_duration, next_reason)
    elif reason == "moisture_trigger" and _soak_session is not None:
        # the session's watering finished - start the soak timer. Clamped
        # to at least one sensor-read interval so the re-check sees a
        # reading taken AFTER the water stopped.
        settings = settings_store.get()
        soak = max(settings.get("soak_recheck_sec", 30), config.MOISTURE_CHECK_INTERVAL_SEC + 5)
        _soak_session["check_at"] = time.time() + soak


def local_now():
    """Return a time struct adjusted by the runtime-configurable UTC
    offset (Watering Settings card), falling back to config.py."""
    tz_min = settings_store.get().get("tz_offset_min", config.TZ_OFFSET_SEC // 60)
    return time.localtime(time.time() + tz_min * 60)


def check_daily_schedule():
    if not state.time_synced:
        return  # clock is unset (Jan 2000) - don't fire schedules at bogus times
    settings = settings_store.get()
    if not settings.get("daily_enabled", True):
        return
    if state.any_valve_open() or _pending_valves:
        return  # don't stack a scheduled run on top of an active/queued one
    lt = local_now()
    hour, minute = lt[3], lt[4]
    for sched in settings.get("schedules", []):
        if not sched.get("enabled", True):
            continue
        if hour == sched["hour"] and minute == sched["minute"]:
            sid = sched.get("id")
            # guard against re-firing the same schedule within its minute
            last = state.last_schedule_fired.get(sid)
            if last and (time.time() - last) < 90:
                continue
            state.last_schedule_fired[sid] = time.time()
            # direct valves first, then each zone's valves expanded at fire
            # time (so re-mapping a zone updates its schedules automatically)
            valve_names = list(sched.get("valve_names") or [])
            zone_valves_map = settings["hardware"].get("zone_valves", {})
            for zn in sched.get("zone_names", []):
                for vn in zone_valves_map.get(zn, []):
                    if vn not in valve_names:
                        valve_names.append(vn)
            if not valve_names and not sched.get("zone_names") and _default_valve_name:
                valve_names = [_default_valve_name]  # legacy schedule fallback
            if not valve_names:
                break
            trigger_valves(valve_names, sched["duration_sec"], "daily_schedule")
            break  # one schedule at a time; others will be caught next minute


def check_moisture_and_water():
    global _soak_session
    settings = settings_store.get()
    hw = settings["hardware"]
    zone_channels = hw["zone_channels"]
    zone_valves = hw.get("zone_valves", {})

    # zone_channels is authoritative for which zones exist (zones can be
    # renamed/removed via the web UI). config.ZONES only contributes
    # calibration for names that still match; any web-UI-added zone gets
    # default calibration so it actually gets read.
    known_names = {z["name"] for z in config.ZONES}
    zones = []
    for z in config.ZONES:
        if z["name"] not in zone_channels:
            continue  # renamed or removed via the web UI
        z = dict(z)
        z["channel"] = zone_channels[z["name"]]
        zones.append(z)
    for name, channel in zone_channels.items():
        if name not in known_names:
            zones.append(
                {
                    "name": name,
                    "channel": channel,
                    "dry_raw": 17500,
                    "wet_raw": 8000,
                    "threshold_percent": 30,
                }
            )

    readings = read_moisture(ads_boards, zones, settings["zone_thresholds"])
    state.log_moisture(readings)

    if not settings.get("moisture_watering_enabled", True):
        return

    dry_zones = [r["name"] for r in readings if r["percent"] < r["threshold"]]
    if not dry_zones or state.any_valve_open() or _pending_valves or _soak_session:
        return  # (an active soak session owns watering until it finishes)

    # A zone can map to multiple valves (e.g. one sensor waters several
    # beds) - they run sequentially, same as a multi-valve schedule. Only
    # the first dry zone with at least one non-locked-out valve is used;
    # any other dry zone will still read dry next cycle and get picked up
    # then, since only one valve runs at a time. Lockouts are per-valve so
    # a schedule/moisture trigger on one valve doesn't block an unrelated one.
    now = time.time()
    for target_zone in dry_zones:
        candidate_valves = zone_valves.get(target_zone) or (
            [_default_valve_name] if _default_valve_name else []
        )
        runnable = []
        for valve_name in candidate_valves:
            last_daily = state.last_daily_watering_ts.get(valve_name)
            if last_daily and now - last_daily < settings["post_daily_lockout_sec"]:
                continue
            last_supplemental = state.last_supplemental_watering_ts.get(valve_name)
            if last_supplemental and now - last_supplemental < settings["min_supplemental_interval_sec"]:
                continue
            runnable.append(valve_name)
        if not runnable:
            continue
        # each zone waters for its own configured run time; zones without
        # one fall back to the global supplemental default
        duration = settings.get("zone_durations", {}).get(target_zone) or settings["supplemental_duration_sec"]
        if trigger_valves(runnable, duration, "moisture_trigger"):
            if settings.get("max_water_cycles", 3) > 1:
                _soak_session = {
                    "zone": target_zone,
                    "cycles": 1,
                    "check_at": None,  # set when the watering finishes
                    "duration": duration,
                    "valves": runnable,
                }
            state.log_event("dry_zones_detected", ",".join(dry_zones))
        break


def check_soak_session():
    """The re-check half of soak-and-recheck: once the soak timer expires,
    look at the zone's latest reading (the sensor loop keeps reading every
    MOISTURE_CHECK_INTERVAL_SEC). Below its wet target -> water again,
    bypassing the cooldowns (same dry event). At/above the target, or out
    of cycles -> session over; the cooldowns run from the last close."""
    global _soak_session
    if not _soak_session or _soak_session["check_at"] is None:
        return
    if time.time() < _soak_session["check_at"]:
        return
    if state.any_valve_open() or _pending_valves:
        return  # e.g. a schedule fired mid-soak; re-check once it's done

    settings = settings_store.get()
    zone = _soak_session["zone"]
    reading = None
    for r in state.latest_moisture:
        if r["name"] == zone:
            reading = r
            break
    if reading is None:
        # zone was renamed/removed mid-session - just end it
        _soak_session = None
        return

    wet_target = settings.get("zone_wet_targets", {}).get(zone)
    if wet_target is None:
        wet_target = min(100, reading["threshold"] + 10)

    if reading["percent"] >= wet_target:
        state.log_event(
            "soak_check",
            "{} at {}% (target {}%) after {} cycle(s) - done".format(
                zone, reading["percent"], wet_target, _soak_session["cycles"]),
        )
        _soak_session = None
        return

    if _soak_session["cycles"] >= max(1, settings.get("max_water_cycles", 3)):
        state.log_event(
            "soak_check",
            "{} still {}% (target {}%) after {} cycles - giving up until cooldown".format(
                zone, reading["percent"], wet_target, _soak_session["cycles"]),
        )
        _soak_session = None
        return

    if trigger_valves(_soak_session["valves"], _soak_session["duration"], "moisture_trigger"):
        _soak_session["cycles"] += 1
        _soak_session["check_at"] = None  # re-armed when this watering closes
        state.log_event(
            "soak_check",
            "{} still {}% (target {}%) - re-watering, cycle {}".format(
                zone, reading["percent"], wet_target, _soak_session["cycles"]),
        )
    # if the trigger was refused (rare race), check_at stays in the past
    # and this retries next loop


# ---- Wire up web server ----
# i2c is passed along so the UI's "Scan Bus" button can list which
# ADS1115 boards actually answer on the bus. If the socket setup fails
# (seen in the wild: lwIP OSError -203), DO NOT crash - watering must run
# regardless; the main loop retries start_server() every 30s.
try:
    web.init(valves, trigger_watering, trigger_valves, _default_valve_name, i2c)
    # dashboard's Firmware Updates card -> run_update_check(install=bool)
    web.set_update_cb(lambda install: run_update_check(install))
except Exception as e:
    print("web server init failed (will retry from loop):", e)
    state.log_event("web", "server init failed: {}".format(e))

# ---- Main loop ----
last_moisture_check = 0
last_schedule_check = 0
last_wifi_check = 0
last_ntp_retry = 0
wifi_was_up = wifi.is_connected()
wifi_down_since = None
rescue_mod = None  # wifi_setup, imported lazily the first time rescue engages

# Hardware watchdog: armed only once the loop is about to start, so an
# import-time crash drops to the REPL instead of boot-looping. If the loop
# ever hangs (I2C lockup etc.) the board reboots and valves close on boot.
wdt = None
if getattr(config, "WATCHDOG_TIMEOUT_SEC", 0):
    from machine import WDT
    wdt = WDT(timeout=config.WATCHDOG_TIMEOUT_SEC * 1000)
    web.set_wdt(wdt)  # web.py feeds it during long uploads/downloads
    print("Watchdog armed:", config.WATCHDOG_TIMEOUT_SEC, "sec")

# Let the allocator itself trigger a collection after ~25% of the heap
# has churned - the per-loop gc.collect() below can't run in the middle
# of one long request handler, but this can.
if hasattr(gc, "threshold"):
    gc.threshold((gc.mem_free() + gc.mem_alloc()) // 4)

# Onboard status LED ("D2", GPIO 2 on most WROOM devkits): solid = WiFi +
# web server both up; fast blink = WiFi down; slow blink = web server down.
status_led = None
if getattr(config, "STATUS_LED_PIN", None) is not None:
    try:
        status_led = Pin(config.STATUS_LED_PIN, Pin.OUT, value=0)
    except Exception as e:
        print("status LED init failed:", e)

last_load_calc = time.ticks_ms()

# Give back whatever the import/compile spike left as garbage before
# sampling - with .mpy modules this spike mostly disappears entirely.
gc.collect()
state.idf_free, state.idf_largest = idf_heap()
print("IDF C-heap free at loop start:", state.idf_free,
      "largest block:", state.idf_largest)
last_idf_print = time.time()

# Consecutive moisture-read failures back the read interval off to 5 min -
# a missing/broken ADS1115 shouldn't hammer the I2C driver (each failed
# transaction churns the same IDF C-heap the WiFi stack depends on).
moisture_fail_streak = 0
moisture_interval = config.MOISTURE_CHECK_INTERVAL_SEC

# ---- OTA update bookkeeping (updater.py / boot.py) ----
# updater is imported LAZILY, only when a check actually runs, so neither
# its code nor the TLS stack occupies RAM the other 23.99 hours a day.
last_update_check_day = None
boot_confirmed = False
_BOOT_CONFIRM_SEC = 60  # the loop must run this long before we trust a build


def confirm_boot():
    """Tell boot.py's rollback guard that this build actually works: clear
    the failed-boot counter and drop the .bak files the updater kept."""
    global boot_confirmed
    if boot_confirmed:
        return
    boot_confirmed = True
    try:
        with open("boot_count.txt", "w") as f:
            f.write("0")
    except OSError:
        pass
    # Only discard the rollback copies once we're sure - from here on the
    # running build is the known-good one.
    try:
        import updater
        n = updater.clear_backups()
        if n:
            print("update: build confirmed stable, cleared {} backup file(s)".format(n))
    except Exception as e:
        print("update: backup cleanup skipped:", e)


def run_update_check(install=False):
    """Fetch the manifest and optionally install. Safe to call anytime -
    refuses while watering so files never swap under an open valve."""
    if state.any_valve_open() or _pending_valves or _active_watering:
        return {"ok": False, "error": "watering in progress - try again later"}
    if not wifi.is_connected():
        return {"ok": False, "error": "no network"}
    state.update_in_progress = True
    try:
        import updater
        # Free the biggest contiguous block we can for the TLS handshake:
        # it needs ~30-45KB from the same C heap the WiFi stack uses.
        gc.collect()
        result = updater.check(config)
        state.update_error = result.get("error")
        if not result.get("ok"):
            return result
        if install and result.get("changed"):
            gc.collect()
            applied = updater.apply(config)
            state.update_error = applied.get("error")
            if applied.get("reboot"):
                state.log_event("update", "rebooting to apply update")
                time.sleep(1)
                machine.reset()
            return applied
        return result
    except Exception as e:
        state.update_error = str(e)
        print("update check error:", e)
        return {"ok": False, "error": str(e)}
    finally:
        state.update_in_progress = False
        gc.collect()

state.log_event("ready", "entering main loop")

while True:
    now = time.time()

    if wdt:
        wdt.feed()

    # safety cutoff always checked, every iteration, for every valve
    for v in valves.values():
        v.check_safety_cutoff()

    # close valve if a timed watering has hit its target
    try:
        check_active_watering()
    except Exception as e:
        print("active watering check error:", e)

    # soak-and-recheck: re-water a zone that's still below its wet target
    try:
        check_soak_session()
    except Exception as e:
        print("soak check error:", e)

    if now - last_moisture_check >= moisture_interval:
        try:
            check_moisture_and_water()
            if moisture_fail_streak >= 3:
                state.log_event("sensors", "moisture reads recovered - normal interval")
            moisture_fail_streak = 0
            moisture_interval = config.MOISTURE_CHECK_INTERVAL_SEC
        except Exception as e:
            print("moisture check error:", e)
            moisture_fail_streak += 1
            if moisture_fail_streak == 3:
                moisture_interval = 300
                state.log_event(
                    "sensors",
                    "3 straight moisture read failures - backing off to 5 min (ADS1115 wired?)")
        try:
            read_env_sensors()
        except Exception as e:
            print("env sensor error:", e)
        last_moisture_check = now

    if now - last_schedule_check >= 20:
        try:
            check_daily_schedule()
        except Exception as e:
            print("schedule check error:", e)
        last_schedule_check = now

    # WiFi self-heal: if the router rebooted, rejoin automatically. If it
    # stays down, open the rescue hotspot alongside normal operation so
    # the dashboard stays reachable from another device. Nothing in here
    # is allowed to kill the loop - watering must survive any network mess.
    if now - last_wifi_check >= 30:
        last_wifi_check = now
        # if the web server failed to start (or died), try to bring it back
        try:
            if web._server_sock is None and web.start_server():
                state.log_event("web", "server started")
        except Exception as e:
            print("web server retry failed:", e)
        try:
            # While the rescue hotspot is up the station is deliberately
            # parked (shared radio - see wifi_setup.start_rescue_ap), and
            # poll_rescue() owns the retry cadence. Just observe here.
            if rescue_mod and rescue_mod.rescue_active():
                up = wifi.is_connected()
            else:
                up = wifi.ensure_connected(_wifi_creds["ssid"], _wifi_creds["password"])
            if up != wifi_was_up:
                # log the IP on reconnect - DHCP may hand out a NEW address,
                # and without this there's no way to know where the UI went
                state.log_event(
                    "wifi",
                    "reconnected, IP: " + wifi.current_ip() if up
                    else "connection lost - retrying")
                wifi_was_up = up
            if up:
                wifi_down_since = None
                if rescue_mod and rescue_mod.rescue_active():
                    rescue_mod.stop_rescue_ap()
                    state.log_event("wifi", "rescue hotspot closed")
            else:
                if wifi_down_since is None:
                    wifi_down_since = now
                elif (getattr(config, "WIFI_RESCUE_AFTER_SEC", 0)
                      and now - wifi_down_since >= config.WIFI_RESCUE_AFTER_SEC
                      and not (rescue_mod and rescue_mod.rescue_active())):
                    import wifi_setup as rescue_mod
                    rescue_mod.start_rescue_ap()
                    state.log_event(
                        "wifi",
                        "down {}s - rescue hotspot open, dashboard at http://192.168.4.1".format(
                            int(now - wifi_down_since)),
                    )
        except Exception as e:
            print("wifi check error:", e)

    # answer captive-probe DNS while the rescue hotspot is up
    if rescue_mod and rescue_mod.rescue_active():
        try:
            rescue_mod.poll_rescue(_wifi_creds)
        except Exception as e:
            print("rescue poll error:", e)

    # clock self-heal: keep retrying NTP until it works
    if not state.time_synced and now - last_ntp_retry >= 300:
        last_ntp_retry = now
        try_ntp_sync()

    # This build has run long enough to be trusted - clear boot.py's
    # failed-boot counter so a later crash isn't blamed on an old update.
    if not boot_confirmed and now - state.boot_time >= _BOOT_CONFIRM_SEC:
        try:
            confirm_boot()
        except Exception as e:
            print("boot confirm error:", e)

    # Daily OTA check at config.UPDATE_CHECK_HOUR. Only flags an update by
    # default (UPDATE_AUTO_INSTALL=False) - installing reboots the board,
    # which is the user's call for something running water valves.
    _check_hour = getattr(config, "UPDATE_CHECK_HOUR", None)
    if (_check_hour is not None and state.time_synced
            and not state.any_valve_open() and not _pending_valves):
        _lt = local_now()
        _today = (_lt[0], _lt[1], _lt[2])
        if _lt[3] == _check_hour and last_update_check_day != _today:
            last_update_check_day = _today
            try:
                res = run_update_check(
                    install=getattr(config, "UPDATE_AUTO_INSTALL", False))
                if res.get("changed"):
                    state.log_event("update", "update available: {}".format(
                        ", ".join(res["changed"])))
            except Exception as e:
                print("daily update check error:", e)

    try:
        web.poll_once(timeout=0.2)
    except Exception as e:
        print("web poll error:", e)

    # "CPU load" = fraction of the last ~5s NOT spent idling in select()
    # status LED: reflects the two things that make the device "reachable".
    # Non-blocking - blink phase comes from the millisecond clock, and the
    # loop iterates every ~200ms (select timeout), which bounds blink speed.
    tms = time.ticks_ms()
    if status_led is not None:
        if wifi_was_up and web._server_sock is not None:
            status_led.value(1)  # solid: all good
        else:
            # fast blink (~2.5Hz) = WiFi down; slow (~0.5Hz) = web server down
            half_period = 200 if not wifi_was_up else 1000
            status_led.value(1 if (tms // half_period) % 2 == 0 else 0)

    window = time.ticks_diff(tms, last_load_calc)
    if window >= 5000:
        idle = web.take_idle_ms()
        state.cpu_percent = max(0, min(100, round(100 * (window - idle) / window)))
        last_load_calc = tms
        state.idf_free, state.idf_largest = idf_heap()
        if now - last_idf_print >= 60:
            last_idf_print = now
            print("IDF C-heap free:", state.idf_free,
                  "largest block:", state.idf_largest,
                  "| GC free:", gc.mem_free())

    # optional nightly maintenance reboot (config.DAILY_REBOOT_HOUR):
    # valves close on boot, so a quiet-hour reboot is invisible and wipes
    # any slow degradation before it can matter
    _reboot_hour = getattr(config, "DAILY_REBOOT_HOUR", None)
    if (_reboot_hour is not None and state.time_synced
            and not state.any_valve_open() and not _pending_valves
            and time.time() - state.boot_time > 3600):
        lt = local_now()
        if lt[3] == _reboot_hour and lt[4] == 0:
            state.log_event("reboot", "scheduled nightly maintenance reboot")
            time.sleep(1)
            machine.reset()

    # proactive GC keeps the heap defragmented over long uptimes - without
    # it, hours of small allocations leave no contiguous room for the
    # bigger ones (JSON responses, WiFi buffers)
    gc.collect()
