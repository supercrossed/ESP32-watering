# settings_store.py
# Runtime-editable settings, persisted to settings.json on flash so
# changes made via the web UI survive a reboot. Falls back to config.py
# defaults if no file exists yet or a key is missing.

import ujson as json

SETTINGS_FILE = "settings.json"

_settings = None


def load(config):
    global _settings
    data = {}
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except OSError:
        pass  # no file yet, use defaults

    # ---- Hardware: build/migrate first so schedules can default valve_names ----
    hardware = data.get("hardware")
    if hardware is None:
        hardware = {
            "i2c_scl_pin": config.I2C_SCL_PIN,
            "i2c_sda_pin": config.I2C_SDA_PIN,
            "ads1115_addresses": [config.ADS1115_ADDRESS],
            "valves": [dict(v) for v in config.VALVES],
            "zone_channels": {z["name"]: z["channel"] for z in config.ZONES},
            "zone_valves": {z["name"]: [z.get("valve", config.VALVES[0]["name"])] for z in config.ZONES},
            "flow_meter_pins": [],
            "rain_sensor_pin": None,
        }
    elif "valves" not in hardware:
        # Migrate pre-multi-valve settings.json: old single valve_pin /
        # valve_active_high become one VALVES-shaped entry named "valve1",
        # and every existing zone is pointed at it.
        hardware["valves"] = [
            {
                "name": "valve1",
                "pin": hardware.pop("valve_pin", config.VALVES[0]["pin"]),
                "active_high": hardware.pop("valve_active_high", config.VALVES[0]["active_high"]),
                "flow_meter_pin": None,
            }
        ]
        hardware["zone_valves"] = {name: ["valve1"] for name in hardware.get("zone_channels", {})}

    # ---- ADS1115: single-address -> list-of-addresses migration ----
    # Multiple boards share the one I2C bus, each at its own address.
    # A zone's channel is a global index: board 1 = 0-3, board 2 = 4-7, etc.
    if "ads1115_addresses" not in hardware:
        hardware["ads1115_addresses"] = [
            hardware.pop("ads1115_address", config.ADS1115_ADDRESS)
        ]

    # Each valve gets a watering_mode ("duration" or "volume") and a
    # target_volume_l used only once real flow-meter pulse counting exists -
    # for now "volume" mode is config-only groundwork, not yet enforced.
    for v in hardware.get("valves", []):
        v.setdefault("watering_mode", "duration")
        v.setdefault("target_volume_l", None)

    hardware.setdefault("zone_valves", {})
    hardware.setdefault("rain_sensor_pin", None)
    valve_names = {v["name"] for v in hardware.get("valves", [])}
    first_valve_name = hardware["valves"][0]["name"] if hardware["valves"] else None
    for zone_name in hardware.get("zone_channels", {}):
        existing = hardware["zone_valves"].get(zone_name)
        if existing is None:
            hardware["zone_valves"][zone_name] = [first_valve_name] if first_valve_name else []
        elif isinstance(existing, str):
            # Migrate pre-multi-valve-per-zone settings.json: a single
            # valve name string becomes a one-item list.
            hardware["zone_valves"][zone_name] = [existing]
    # drop any valve names that no longer exist (e.g. a valve was removed)
    for zone_name, names in list(hardware["zone_valves"].items()):
        hardware["zone_valves"][zone_name] = [n for n in names if n in valve_names]

    # ---- Schedules: a list of {id, hour, minute, duration_sec, enabled, valve_names} ----
    # Migrate an older single-time settings file into the new list form so
    # nobody loses their existing schedule on upgrade.
    schedules = data.get("schedules")
    if schedules is None:
        schedules = [
            {
                "id": 1,
                "hour": data.get("daily_hour", config.DAILY_WATER_HOUR),
                "minute": data.get("daily_minute", config.DAILY_WATER_MINUTE),
                "duration_sec": data.get(
                    "daily_duration_sec", config.DAILY_WATER_DURATION_SEC
                ),
                "enabled": True,
                "valve_names": [first_valve_name] if first_valve_name else [],
                "zone_names": [],
            }
        ]
    else:
        # Pre-multi-valve schedules have no valve_names - default to the
        # first configured valve so they keep firing after upgrade.
        for sched in schedules:
            if not sched.get("valve_names") and not sched.get("zone_names"):
                sched["valve_names"] = [first_valve_name] if first_valve_name else []
            sched.setdefault("zone_names", [])

    _settings = {
        "schedules": schedules,
        "supplemental_duration_sec": data.get(
            "supplemental_duration_sec", config.SUPPLEMENTAL_WATER_DURATION_SEC
        ),
        "min_supplemental_interval_sec": data.get(
            "min_supplemental_interval_sec", config.MIN_SUPPLEMENTAL_INTERVAL_SEC
        ),
        # cooldown after a SCHEDULED watering before moisture watering can
        # trigger the same valve again (ground is presumably still wet)
        "post_daily_lockout_sec": data.get(
            "post_daily_lockout_sec", config.POST_DAILY_LOCKOUT_SEC
        ),
        "zone_thresholds": data.get(
            "zone_thresholds", {z["name"]: z["threshold_percent"] for z in config.ZONES}
        ),
        # per-zone watering run time (sec); zones missing here fall back to
        # supplemental_duration_sec
        "zone_durations": data.get("zone_durations", {}),
        # per-zone "adequately watered" target percent - the soak re-check
        # keeps watering until the zone reads at/above this (not merely
        # above the dry-trigger threshold). Missing = threshold + 10.
        "zone_wet_targets": data.get("zone_wet_targets", {}),
        # soak-and-recheck: after a moisture watering, wait this long, then
        # re-read; if the zone is still below its wet target, water again -
        # up to max_water_cycles per dry event (1 = feature off). The
        # cooldowns apply AFTER the whole session, not between cycles.
        "soak_recheck_sec": data.get("soak_recheck_sec", 30),
        "max_water_cycles": data.get("max_water_cycles", 3),
        # US ZIP for the dashboard's weather widget (browser-side fetch;
        # empty = auto-locate from the viewer's IP)
        "weather_zip": data.get("weather_zip", ""),
        # UTC offset in minutes, editable in the web UI (config.py value is
        # only the first-boot default). No DST automation - adjust manually.
        "tz_offset_min": data.get("tz_offset_min", config.TZ_OFFSET_SEC // 60),
        "daily_enabled": data.get("daily_enabled", True),
        "moisture_watering_enabled": data.get("moisture_watering_enabled", True),
        "hardware": hardware,
    }
    return _settings


def get():
    return _settings


def update(patch):
    _settings.update(patch)
    save()


def save():
    with open(SETTINGS_FILE, "w") as f:
        json.dump(_settings, f)
