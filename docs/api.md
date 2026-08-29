# API Reference

[<- Docs index](README.md)

Every endpoint the ESP32 serves. All responses are JSON unless noted.

> **No authentication.** Anyone on your LAN can call these. Don't
> port-forward the planter.

## Status & data

### `GET /api/status`
Current state. Polled by the dashboard every 5 seconds, so it's kept lean -
settings are deliberately *not* included.

```json
{
  "moisture": [{"name": "zone1", "raw": 14200, "percent": 42, "threshold": 30}],
  "valves": {"valve1": {"open": false, "seconds_open": 0,
                        "last_close_ts": 1753412400, "last_close_reason": "moisture_trigger"}},
  "uptime_sec": 8412, "now": 1753420812,
  "startup_grace_left": 0,
  "time_synced": true, "wifi_connected": true, "lan_ok": true,
  "env": {"temp_c": 22.4, "humidity": 51, "pressure_hpa": 1013.2, "rain": false},
  "mem_free": 94704, "mem_alloc": 45312, "cpu_percent": 3,
  "idf_free": 33548, "idf_largest": 32768,
  "update": {"version": "2026.07.24.2247", "last_check": 1753400000,
             "last_install": null, "available": null, "error": null, "busy": false}
}
```

`startup_grace_left` counts down the seconds until moisture watering is
allowed after a boot (0 once past). See
[watering.md](watering.md#at-power-on).

`lan_ok` is the result of the last gateway probe: `true` = packets reach the
router, `false` = associated but nothing comes back (a zombie connection,
being reconnected), `null` = not probed yet. `wifi_connected` alone only
means the radio is associated. See
[troubleshooting.md](troubleshooting.md#dashboard-unreachable-but-the-device-says-wifi-is-connected).

### `GET /api/history` &nbsp;&middot;&nbsp; `GET /api/history?hours=N`
Moisture readings over time, streamed per-point.

Without `hours`: the **live** buffer - one point per minute, 180 points
(3 hours), held in RAM and lost on reboot.

With `hours=N`: the **saved** history from `history.csv` on flash - one
point per 15 minutes, kept for 7 days, and it **survives reboots**. `N` is
clamped to 14 days.

Both return the same shape:

```json
[{"t": 1756000000, "readings": [{"name": "bed1", "percent": 42.1}]}]
```

A week of two zones is ~21KB on flash. Lines older than 7 days are purged
automatically (roughly once a day), as are any torn by a power cut.

### `GET /api/events`
Recent event log - watering starts/stops, reboots, WiFi transitions, updates.

---

## Watering control

### `POST /api/valve?state=open|close&valve=NAME`
Directly open or close a valve. Subject to the one-valve-at-a-time rule and
the hard safety cutoff.

### `POST /api/water/trigger?duration=N&valve=NAME`
Run one valve for N seconds.

### `POST /api/zone/trigger?zone=NAME&duration=N`
Run a zone's valves sequentially. `duration` is optional - defaults to the
zone's configured run time.

### `POST /api/water/all?duration=N`
Run every valve, sequentially.

---

## Configuration

### `GET /api/settings` &nbsp;&middot;&nbsp; `POST /api/settings`
Watering settings: durations, thresholds, cooldowns, soak parameters,
timezone, feature toggles. POST accepts a JSON body of whitelisted keys.

### `GET /api/schedules` &nbsp;&middot;&nbsp; `POST /api/schedules`
Full-replacement list. Each entry:

```json
{"id": 1, "hour": 6, "minute": 0, "duration_sec": 300,
 "enabled": true, "valve_names": ["valve1"], "zone_names": []}
```

### `GET /api/zones` &nbsp;&middot;&nbsp; `POST /api/zones`
Full replacement, applied **live** (no reboot). Renaming a zone rewrites its
key in `zone_channels`, `zone_valves`, `zone_thresholds`, and
`zone_calibration` atomically. Omitting `dry_raw`/`wet_raw` keeps whatever
the zone already had, so editing a threshold never discards a calibration.

```json
{"zones": [{"name": "bed1", "channel": 0, "valves": ["valve1"],
            "threshold": 30, "wet_target": 45, "water_duration_sec": 60,
            "dry_raw": 16240, "wet_raw": 7100}]}
```

### `GET /api/valves` &nbsp;&middot;&nbsp; `POST /api/valves`
Full replacement; **triggers a reboot** (Pin objects are built once at boot).
Renames propagate to zones and schedules.

```json
{"valves": [{"name": "valve1", "pin": 26, "active_high": true,
             "flow_meter_pin": null, "watering_mode": "duration"}],
 "renames": {"old_name": "new_name"},
 "flow_meter_pins": []}
```

### `GET /api/hardware` &nbsp;&middot;&nbsp; `POST /api/hardware`
I2C pins, ADS1115 addresses, rain sensor pin. **Triggers a reboot.**

### `GET /api/pinmap`
Every GPIO 0-39 with its assigned role and safety flags (flash, serial,
strapping, input-only), plus ADS1115 channel assignments.

### `POST /api/calibrate` &nbsp;&middot;&nbsp; `GET /api/calibrate`
POST `{"zone": "bed1", "point": "dry"|"wet"}` queues a calibration capture.
The device averages that zone's raw ADC for ~10s **in the main loop** (not
in the handler - it would freeze the web server and valve timing) and saves
the point as soon as it lands.

GET returns `{busy, result, calibration}` - poll it for the outcome:

```json
{"busy": false,
 "result": {"raw": 16240, "samples": 40, "min": 16180, "max": 16310,
            "spread": 130, "zone": "bed1", "point": "dry"},
 "calibration": {"bed1": {"dry_raw": 16240, "wet_raw": 7100}}}
```

A `spread` over ~400 means the probe hadn't settled. See
[setup.md](setup.md#calibrating-a-moisture-sensor).

### `GET /api/i2c/scan`
Live bus scan: `{"found": [72, 73]}` (decimal; 0x48 = 72).

---

## System

### `GET /api/wifi` &nbsp;&middot;&nbsp; `POST /api/wifi`
GET returns the saved SSID and connection state - **never the password**.

POST `{"ssid": "...", "password": "..."}` saves to `wifi.json` (which
persists across reboots and takes priority over `config.py`) and reboots.
The write is verified by reading it back first; if it fails the device
responds `500` with an error and does **not** reboot, so it can't land back
on the old network while reporting success.

### `GET /api/config/export` &nbsp;&middot;&nbsp; `POST /api/config/import`
Download or restore the full configuration as a JSON file. Excludes WiFi
credentials. Import validates, then reboots.

### `POST /api/update/check` &nbsp;&middot;&nbsp; `POST /api/update/apply`
Check the repo for a new version, or download/verify/install it. See
[OTA updates](ota-updates.md).

`check` returns:
```json
{"ok": true, "available": "2026.07.24.2247", "installed": "2026.07.20.1130",
 "changed": ["web.mpy", "index.html"], "checked_at": 1753420812, "error": null}
```

`apply` responds **before** installing, because the reboot closes the socket.

### `POST /api/upload`
Multipart upload of `.mpy`, `.py`, or `.html` files. Streamed straight to
flash, never buffered in RAM. Saving a file deletes its `.py`/`.mpy`
counterpart.

### `POST /api/reboot`
Reboots the ESP32. Valves close on boot.

---

## Notes for integrators

- `GET /` serves the dashboard, streamed from flash in 1KB chunks
- Unknown `/api/*` paths return 404; unknown non-API paths redirect to `/`
  (this is what makes captive-portal probes open the dashboard)
- The server is single-threaded and handles one connection per main-loop
  poll - don't hammer it with concurrent requests
- A Raspberry Pi orchestration layer would poll `/api/status` and
  `/api/history` over HTTP; no protocol work needed
