# Project context for Claude Code

## Keeping this file (and the docs) current

**Every behavior change updates the docs in the SAME commit.** Nothing
enforces this automatically, and drift has already shipped two real bugs:
`config.example.py` went stale and left every fresh clone with OTA disabled,
and `UPDATE_REPO` pointed at a repo that never existed (every OTA check
would have 404'd). The checklist lives in `docs/CONTRIBUTING.md`:

- **This file** — architecture, constraints, API list, reliability measures
- **`README.md`** — only if install/first-run/parts changed (keep it ~150 lines)
- **`docs/<area>.md`** — the guide for whatever changed
- **`docs/CHANGELOG.md`** — a dated entry for anything user-visible

## Repo layout

```
src/        device source - EDIT HERE
build/      generated artifacts; committed, because OTA publishes from it
docs/       guides (README.md there is the index)
hardware/   3D models, enclosures, reference material
```

Build with `.\build_mpy.ps1` from the repo root. The device filesystem is
flat; the OTA manifest records each file's repo path so devices know where
to fetch from.

This is a **MicroPython** firmware project for an **ESP32-WROOM-32** that runs
a standalone automated plant-watering controller with a built-in web
dashboard. It is NOT a CPython project — do not assume CPython stdlib is
available.

## Critical environment constraints

- Target runtime is **MicroPython on ESP32**, not desktop Python. Only
  MicroPython-available modules exist: `machine`, `network`, `socket`,
  `select`, `time`, `ntptime`, `ujson`. There is **no** `asyncio` assumed,
  no `zipfile`, no `threading`, no `pip` packages.
- **RAM is tiny** (~100KB usable). Avoid buffering large data, avoid
  building big in-memory structures. History lists are intentionally capped.
- **There are TWO heaps.** The Python GC heap (what `gc.mem_free()` shows)
  and the ESP-IDF C heap that the WiFi/lwIP/I2C drivers allocate from.
  MicroPython 1.28 grows the GC heap on demand OUT OF the C heap and
  doesn't give it back — on-device compilation of the larger modules once
  left 764 bytes of C heap at loop start, killing the network
  ("wifi:fail to alloc timer", "i2c command link malloc error",
  getaddrinfo OSError -203) while `gc.mem_free()` looked fine. Therefore
  **device modules ship as pre-compiled `.mpy`** (see `build_mpy.ps1`;
  `pip install mpy-cross`, bytecode v6.3): everything except `main.py`
  (boot entry, executed not imported) and `config.py` (small,
  hand-editable). A `.py` on the device shadows its `.mpy` — delete the
  old `.py` when converting. C-heap health is sampled via
  `esp32.idf_heap_info` into `state.idf_free`/`idf_largest`, printed
  each minute and exposed in `/api/status`.
- The web server is a **hand-rolled synchronous** server polled from the
  main loop via `web.poll_once()`. It must never block — the main loop also
  handles valve safety cutoffs and scheduling. Do not introduce blocking
  reads without timeouts.
- Files run on the device: `main.py` auto-runs at boot. Changing pin/hardware
  objects (I2C, valve Pin) requires a **reboot** to take effect because they
  are constructed once at boot.

## Architecture

- `config.py` — first-boot defaults only (WiFi creds, pins, zone calibration).
  After first boot, `settings.json` on the device overrides these.
- `settings_store.py` — loads/persists runtime settings to `settings.json`.
  Handles migration of old single-schedule format to the new list format.
- `state.py` — shared in-memory state (per-valve status dict keyed by valve
  name, moisture history, event log, per-schedule fired timestamps). Capped
  ring buffers.
- `ads1x15.py` — minimal ADS1115 I2C driver (single-ended reads).
- `env_sensors.py` — minimal AHT20 (temp/humidity, 0x38) + BMP280
  (temp/pressure, 0x76/0x77) drivers. Both share the ADS1115s' I2C bus and
  are **auto-detected at boot** by scan — no config. Readings land in
  `state.env` every sensor tick, shown in the dashboard's Environment card.
  An optional LM393 rain sensor DO pin (`hardware["rain_sensor_pin"]`,
  assignable in the pin map, input-only pins OK, LOW = wet) rides along in
  the same `state.env` dict. Display-only for now (rain-skip is roadmap).
- `moisture.py` — raw ADC → percent via two-point calibration.
- `valve.py` — solenoid control via IRF520 MOSFET gate pin, plus a hard
  safety cutoff (`MAX_VALVE_OPEN_SEC`) checked every loop. Multiple `Valve`
  instances exist (one per `hardware.valves` entry), each keyed by name in
  `state.valves`.
- `wifi.py` — WiFi connect + reconnect helpers. Credentials live in
  `wifi.json` on flash (written by the setup portal), falling back to
  config.py. `ensure_connected()` is polled from the main loop so the
  device rejoins after a router reboot; watering keeps running offline.
- `wifi_setup.py` — two recovery modes. (1) Blocking boot portal: if
  boot-time connect fails AND the target SSID is visible (wrong password /
  fresh kit), the device becomes an open AP ("Planter-Setup-xxxx"),
  hijacks all DNS so phones auto-open a setup page, saves creds to
  wifi.json, reboots. If the SSID simply isn't visible (router down), no
  portal - normal offline operation with retries. (2) Non-blocking runtime
  rescue (`start_rescue_ap`/`poll_rescue`/`stop_rescue_ap`): if WiFi stays
  down for `config.WIFI_RESCUE_AFTER_SEC` (default 5 min) mid-operation,
  main.py opens the same hotspot ALONGSIDE station mode (AP+STA) — watering
  keeps running, the full dashboard is reachable at 192.168.4.1 (web.py
  redirects unknown non-API paths to "/" so captive probes land there, and
  the dashboard's WiFi card can change creds via POST /api/wifi). Closes
  itself when the real network returns. Imported lazily (RAM).
  **AP startup order is load-bearing**: `_start_ap()` deactivates,
  configures (ssid + `password=""` + AUTH_OPEN together — authmode alone
  can leave a keyless WPA2 AP), THEN activates. Config applied to an
  already-active AP restarts it without reliably restarting its DHCP
  server, so phones associate but get no lease ("Unable to join network"
  on iOS). AP and STA share ONE radio, so `start_rescue_ap()` parks the
  station (a scanning STA drags the AP off-channel mid-handshake) and
  `poll_rescue(creds)` owns router-return retries at
  `RESCUE_STA_RETRY_SEC` (120s); main.py only observes while rescue is up.
- `web.py` — HTTP server + JSON API.
- `index.html` — the single-page dashboard (HTML/CSS/JS). Served by
  streaming it straight from flash in small chunks (`_send_file()` in
  web.py), not loaded as a Python string - a ~34KB string literal needs one
  contiguous heap allocation to compile, which reliably fails with
  `MemoryError` on a fragmented ESP32 heap even with plenty of total free
  memory. Must be uploaded alongside the `.py` files; `main.py` will crash
  on the first page load if it's missing.
- `main.py` — boot sequence + main loop: safety cutoff, active-watering
  close, moisture checks, schedule checks, web polling.

## Watering logic

The system supports **multiple solenoid valves** (`hardware["valves"]`, a
list of `{name, pin, active_high, flow_meter_pin, watering_mode,
target_volume_l}`). Only one valve is open at a time system-wide
(`state.any_valve_open()` gates every trigger) — this is a deliberate
simplification, not a hardware limit, since supply line/pump pressure was
the concern that motivated it. `watering_mode` ("duration" or "volume") and
`target_volume_l` are config-only groundwork for a future flow meter - no
pulse counting exists yet (see Hardware notes), so "volume" mode currently
has no runtime effect regardless of what's configured.

Two independent triggers, both resolved to one or more valve names before
opening anything. Multiple valves always run **sequentially** — queued in
`main.py:_pending_valves`, drained one at a time by `check_active_watering()`
as each valve closes, since only one valve runs at a time system-wide:
1. **Schedules** (`settings["schedules"]`, a list of
   `{id, hour, minute, duration_sec, enabled, valve_names, zone_names}`) —
   each fires once at its time, running every valve in `valve_names` plus
   each zone's valves (zone_names are expanded to valves AT FIRE TIME in
   `check_daily_schedule`, so re-mapping a zone updates its schedules
   automatically), deduped, in order.
2. **Moisture** — each zone maps to **one or more** valves via
   `hardware["zone_valves"]` (zone name → list of valve names, e.g. one
   sensor watering several beds). If any zone reads below its threshold,
   its mapped valves fire in sequence, **each for that zone's own run time**
   (`settings["zone_durations"]`, zone name → sec; falls back to
   `supplemental_duration_sec` if unset). Subject to a post-schedule lockout
   (`settings["post_daily_lockout_sec"]`, default 4hr, editable in Watering
   Settings) and a min interval between moisture triggers
   (`settings["min_supplemental_interval_sec"]`), both tracked **per valve**
   in `state.last_daily_watering_ts`/`state.last_supplemental_watering_ts`
   so a trigger on one valve never blocks an unrelated one). Only valves that
   pass their lockout check are queued; if a zone's only valve(s) are all
   locked out, the next dry zone in the list is tried instead. If multiple
   dry zones exist, only the first runnable one is triggered immediately —
   the rest are picked up on the next moisture-check cycle (they'll still
   read dry) rather than being queued alongside it.

   **Soak-and-recheck**: a moisture trigger starts a *session*
   (`main._soak_session`). After the watering closes, wait
   `settings["soak_recheck_sec"]` (clamped ≥ one sensor-read interval),
   re-read, and water again if the zone is still below its **wet target**
   (`settings["zone_wet_targets"]`, per zone, default threshold+10 — the
   hysteresis: trigger at "Dry below", stop at "Water until"). Re-waters
   within a session bypass the cooldowns (same dry event) up to
   `settings["max_water_cycles"]` (default 3; 1 disables the recheck —
   also the flood-guard if a sensor fails). New moisture triggers are
   blocked while a session is active; cooldowns run from the session's
   last close.

**Power-on behavior**: moisture watering is held for
`config.STARTUP_GRACE_SEC` (default 60s) after boot — sensors are read
immediately (dashboard populates) but no valve opens. Capacitive probes
need time to settle, and one reading taken microseconds after power-on
once opened a valve on already-wet soil. Additionally the per-valve
cooldown timestamps are persisted to `watering_state.json` on every
watering close and restored at boot (`state.save_watering_state()` /
`load_watering_state()`), because they previously lived only in RAM — a
power cut erased all memory of recent watering and the planter re-watered
immediately. Restored timestamps that are in the future or >7 days old are
discarded (pre-NTP 2000-epoch values). `/api/status` exposes
`startup_grace_left`.

A hard safety cutoff force-closes any valve open longer than
`MAX_VALVE_OPEN_SEC`, checked every loop, independently per valve. A
hardware watchdog (`config.WATCHDOG_TIMEOUT_SEC`, default 120s; **set to 0
while developing with Thonny** - once armed it can't be stopped and a board
sitting at the REPL boot-loops) reboots the ESP32 if the main loop ever
hangs; valves close on boot, so a hang can't leave water running. web.py
feeds the watchdog during long uploads/downloads via `web.set_wdt()`.

Scheduled watering is **held off until NTP sync succeeds**
(`state.time_synced`) - otherwise the clock sits at the 2000 epoch and
schedules would fire at bogus times. NTP is retried every 5 min from the
main loop; moisture watering runs regardless (it doesn't need the time).
The UTC offset is runtime config (`settings["tz_offset_min"]`, editable in
the Watering Settings card; no DST automation).

Zones added purely through the web UI (present in `zone_channels` but not
in `config.ZONES`) are synthesized with default calibration
(`dry_raw=17500, wet_raw=8000, threshold_percent=30`) in
`main.py:check_moisture_and_water()` so they're actually read — a zone name
alone in the UI does nothing without this.

## Hardware notes

- Moisture sensors (AITRIP capacitive) → **ADS1115 analog channels** (A0-A3),
  NOT GPIO. **Multiple ADS1115 boards are supported** (up to 4): they all
  share the ONE I2C bus (same 2 GPIO pins), each at its own address set by
  its ADDR pin (GND=0x48, VDD=0x49, SDA=0x4A, SCL=0x4B). Addresses live in
  `hardware["ads1115_addresses"]` (a list; migrated from the old scalar
  `ads1115_address`). A zone's `channel` is a **global index**: board 1 =
  0-3, board 2 = 4-7, etc. — resolved to (board, A0-A3) in
  `moisture.read_all()`. A "zone" is a physical spot in the garden/planter
  where one sensor sits; it is not the same thing as a GPIO pin or an ADS
  channel — those are just what a zone is wired to.
- Solenoid valves → **IRF520 MOSFET modules**, one GPIO pin each, configured
  in `hardware["valves"]`. Each needs an external 1N4007 flyback diode
  across the solenoid (hardware, not code). Changing a valve's pin/
  active-high requires a reboot (Pin objects are constructed once at boot).
- Flow meter (YF-S201) — groundwork only, not wired into watering logic yet.
  Each valve has an optional `flow_meter_pin` (set via the web UI's Valve
  Configuration card); `Valve.flow_meter_pin` carries it through to
  `main.py` but nothing reads pulses yet. When implemented, a valve with a
  flow meter should switch that valve's watering from duration-based to
  volume-based (using `config.PULSES_PER_LITER`) in
  `main.py:check_active_watering()`. The older standalone
  `hardware["flow_meter_pins"]` list (a flow meter not tied to a specific
  valve, e.g. a main-line meter) still exists separately and is assignable
  from the GPIO pin table.
- WROOM-32 pin safety (38-pin devkit): GPIO 6-11 wire the SPI flash - never
  use. GPIO 1/3 are UART0 (USB serial for Thonny/flashing) - don't assign.
  GPIO 0/2/12/15 are boot-strapping pins - usable as outputs after boot
  with care (12 is riskiest: held high at reset breaks boot). GPIO 16/17
  ARE usable on WROOM (only reserved on WROVER, where PSRAM takes them).
  GPIO 34/35/36/39 are input-only, no pull-ups (fine for flow meter, not
  for valve). Freely usable: 4/5/13/14/16/17/18/19/21/22/23/25/26/27/32/33.

## API endpoints

```
GET  /api/status         moisture + per-valve state (lean - polled every 5s;
                          settings deliberately NOT included, use /api/settings)
GET  /api/history        moisture readings over time
GET  /api/events         recent event log
POST /api/valve?state=open|close&valve=NAME
POST /api/water/trigger?duration=N&valve=NAME
POST /api/zone/trigger?zone=NAME&duration=N   (runs the zone's valves sequentially)
POST /api/water/all?duration=N                (runs every valve sequentially)
GET  /api/settings
POST /api/settings       (supplemental/thresholds/toggles)
GET  /api/schedules
POST /api/schedules      (JSON list, full replacement; each entry needs valve_names)
GET  /api/pinmap         GPIO roles (incl. valve pins) + ADS channel assignments
GET  /api/valves         hardware["valves"] list
POST /api/valves         (JSON {valves:[...], renames:{old:new}, flow_meter_pins:[...]},
                          full replacement, triggers reboot; renames propagate to
                          zones + schedules; standalone flow-meter pins ride along
                          so a meter can move valve<->standalone in one save)
GET  /api/i2c/scan       live bus scan -> {found:[addr,...]} (ADS1115 = 0x48-0x4B)
GET  /api/config/export  full config as a downloadable JSON file (no WiFi creds)
POST /api/config/import  restore/preset a config file, validates then reboots
GET  /api/wifi           saved SSID + connection state (never the password)
POST /api/wifi           (JSON {ssid, password}) save to wifi.json, reboot
GET  /api/zones          zones as [{name, channel, valves, threshold, water_duration_sec}]
POST /api/zones          (JSON {zones:[...]}, full replacement, live - no reboot;
                          renaming a zone rewrites its key in zone_channels/
                          zone_valves/zone_thresholds atomically)
GET  /api/hardware
POST /api/hardware       (JSON body {hardware:{...}}, triggers reboot)
POST /api/upload         multipart .py upload (code updater) - streamed to flash, not buffered in RAM
POST /api/reboot
```

## Testing constraints

There is no hardware in CI. When editing, at minimum keep files parseable —
`python3 -c "import ast; ast.parse(open('FILE').read())"` should pass for all
`.py` files even though they won't *run* under CPython (they import machine).
For the dashboard JS, it can be extracted from `index.html`'s `<script>`
block and checked with `node --check`.

## Roadmap / not done yet

- Flow meter volume-based watering pulse counting (hardware pending;
  config/UI groundwork done)
- Rain sensor / rain-skip integration (weather data exists browser-side)
- Optional Raspberry Pi orchestration layer (would poll the JSON API over
  HTTP; no code here yet). RS-485/MAX485 remains a wired fallback option.
- Sensor-fault detection + daily watering budget (guard against a broken
  sensor reading permanently dry)
- Simple auth for the dashboard (anyone on the LAN can control it)

## Reliability measures (already in place)

- Hardware watchdog (`WATCHDOG_TIMEOUT_SEC`); every main-loop subsystem
  individually try/except-wrapped; `gc.collect()` per loop iteration +
  `gc.threshold()` for mid-handler collection.
- History decimated to 1 point/min, 180 points max, slim shape; /api/history
  and index.html and uploads all streamed (no large contiguous allocations).
- events.log auto-rotates at 32KB (keeps the 8KB tail), checked every 50
  events in `state.log_event`.
- "CPU load" = 1 - (select() idle time / wall time) over 5s windows
  (`web.take_idle_ms` + main loop calc); shown with memory %/free in the
  System Status card. Optional nightly maintenance reboot
  (`config.DAILY_REBOOT_HOUR`, default off).
- Web server startup can't kill the controller: `web.init` failure is
  caught in main.py and `web.start_server()` (binds ("0.0.0.0",80)
  directly, no getaddrinfo — its lwIP allocation once failed with -203)
  is retried from the loop every 30s. mDNS hostname "planter" set before
  connect (dashboard at http://planter.local); reconnect events log the
  (possibly new) DHCP IP.
- 3 consecutive moisture-read failures back the sensor interval off to
  5 min (missing ADS1115 shouldn't churn the C heap the WiFi stack uses);
  auto-recovers on the next good read.
- Onboard status LED (`config.STATUS_LED_PIN`, default GPIO 2 = the
  devkit's blue "D2" LED): solid = WiFi + web server up, fast blink
  (~2.5Hz) = WiFi down, slow blink (~0.5Hz) = web server down. Shown as a
  role in the pin map. `config.WEB_DEBUG` prints one console line per
  HTTP request ("web: GET /api/status") - the definitive reachability
  signal; leave True while debugging, off for production.
- The web uploader accepts `.py`, `.mpy`, `.html`; saving a file DELETES
  its `.py`/`.mpy` counterpart on the device so a stale twin can never
  shadow the fresh upload.
