# Rebuild prompt: ESP32 automated plant-watering controller

**Paste this whole file into any capable AI assistant to rebuild this
project from nothing.** It is written as a specification, not a tour of the
existing code, so it does not assume the reader can see the repository.

Everything in *Platform constraints* was learned by measurement on real
hardware, and most of it is counter-intuitive. An implementation that
ignores that section will appear to work on the bench and fail in the
field in ways that look like network faults.

For a one-page version of the same thing, see
[OVERVIEW.md](OVERVIEW.md).

---

## 1. What to build

A **standalone automated plant-watering controller** on an ESP32 running
MicroPython. It waters a garden or planter from soil-moisture readings and
from timed schedules, and serves a single-page web dashboard on the local
network for monitoring and configuration. It must keep watering correctly
with no internet, no phone app, and no cloud service. It is intended to be
sellable as a kit, so first-run setup must work for someone who cannot
open a serial console.

Priorities, in order:

1. **Never flood.** No fault may leave a valve open.
2. **Keep watering.** Loss of WiFi, dashboard, sensors or clock must not
   stop scheduled watering.
3. **Be diagnosable.** A user with no debugger should be able to tell what
   is wrong from the dashboard, the status LED and the event log.
4. Serve a good dashboard.

---

## 2. Hardware

| Part | Qty | Notes |
|---|---|---|
| ESP32-WROOM-32 devkit | 1 | Reference target. ESP32-S3-WROOM-1 N16R8 also supported |
| ADS1115 16-bit I2C ADC | 1–4 | Moisture sensors are **analog** and do **not** connect to GPIO |
| Capacitive soil moisture sensor | 1 per zone | e.g. AITRIP; into ADS1115 A0–A3 |
| 12 V solenoid valve | 1 per valve | |
| IRF520 / D4184 MOSFET module | 1 per valve | One GPIO each |
| 1N4007 flyback diode | 1 per solenoid | **Hardware, not optional** — across the solenoid |
| 12 V supply + 3.3 V/5 V for the ESP32 | 1 | |
| AHT20 + BMP280 board | optional | Temp/humidity/pressure, auto-detected on the I2C bus |
| LM393 rain sensor | optional | Digital out; display-only today |
| YF-S201 flow meter | optional | Groundwork only, no pulse counting yet |

### Wiring rules that matter

- **All ADS1115 boards share ONE I2C bus** — the same two GPIO pins. A
  second board does not need two more pins; it needs a different address,
  set by its ADDR pin: GND=0x48, VDD=0x49, SDA=0x4A, SCL=0x4B.
- A zone's `channel` is a **global index**: board 1 = 0–3, board 2 = 4–7,
  and so on, resolved to (board, A0–A3) at read time.
- **Pull-ups:** most ADS1115 breakouts already carry 10 k on SDA/SCL. Use
  exactly one set for the whole bus.
- **Solid common ground** between ESP32, ADS1115 and sensors.
- **Recommended: 10 k from each MOSFET gate pin to GND.** An ESP32 GPIO is
  a floating input until firmware configures it, and a floating gate can
  partially conduct — a valve can sit part-open before the firmware runs,
  or if the signal wire works loose. Software closes this window but
  cannot help before it is running.

### WROOM-32 pin safety (38-pin devkit)

- **Never use GPIO 6–11** — SPI flash.
- **Do not assign GPIO 1/3** — UART0, the USB serial console.
- GPIO 0/2/12/15 are strapping pins; usable as outputs after boot with
  care (12 is worst: held high at reset breaks boot).
- GPIO 34/35/36/39 are **input-only, no pull-ups** — fine for a flow
  meter, useless for a valve.
- GPIO 16/17 are usable on WROOM (reserved only on WROVER, where PSRAM
  takes them).
- Freely usable: 4, 5, 13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27,
  32, 33.

### ESP32-S3-WROOM-1 N16R8 differences

Reserved: 0/3/45/46 strapping, 19/20 USB, 43/44 UART0, 26–32 flash, and
**33–37 consumed by the octal PSRAM** — pinout cards commonly show those
as free. Verified usable: 1, 2, 4–18, 21, 38–42, 47, 48. The onboard
WS2812 RGB LED is on GPIO 48.

---

## 3. Platform constraints — read before writing any code

These caused real, shipped bugs. Each is a measurement, not a theory.

### 3.1 There are TWO heaps, and the one you can see is the wrong one

`gc.mem_free()` reports the **MicroPython GC heap**. The WiFi/lwIP/I2C
drivers allocate from the **ESP-IDF C heap**, which is separate. MicroPython
grows its GC heap *out of* the C heap on demand and never gives it back.

Consequence: a large allocation can starve the network stack while
`gc.mem_free()` still looks healthy. Measured mid-request on a WROOM-32:
**824 bytes of C heap free, largest block 304**, while `gc.mem_free()`
reported 54 KB. lwIP could not allocate a TCP segment, so `socket.send()`
blocked **forever on the 92-byte response header** — not on the body,
which is why the symptom was baffling.

**Rules:**
- Any response that grows with the user's configuration must be **streamed**
  in small fragments, never built with one `json.dumps()`. Getting peak
  allocation from 5159 bytes down to 145 fixed a whole class of failure.
- Sample the C heap explicitly via `esp32.idf_heap_info(esp32.HEAP_DATA)`
  and expose it; otherwise you are flying blind.

### 3.2 Ship pre-compiled `.mpy`, and watch `main.py`'s CODE size

Compiling a module on-device consumes GC heap, which comes out of the C
heap. Ship everything as pre-compiled `.mpy` (`mpy-cross`, bytecode v6.3)
except `main.py` (the boot entry, executed not imported) and `config.py`
(small, hand-editable). A `.py` on the device **shadows** its `.mpy` —
delete the old one when converting.

`main.py` is the one module still compiled at every boot, and it sits on a
cliff. Measured on WROOM-32:

| `main.py` | C heap before WiFi | Result |
|---|---|---|
| 805 statements | 79400 free / 53248 largest | boots, WiFi connects |
| 816 statements | 26148 free / 14336 largest | `OSError: WiFi Out of Memory` |

**Eleven statements.** Comments are free — a 3 KB comment-only pad changed
nothing — so document generously but move *code* into a `.mpy` module.

### 3.3 `settimeout()` does not bound socket calls

Measured: a single 1024-byte `send()` blocked for **30 seconds** on a
socket with a 3-second timeout. The server is polled from the main loop,
so one client that walked away froze the dashboard *and* delayed the valve
cutoff. Treat every socket call as potentially unbounded until measured.

### 3.4 Cap every socket write at 512 bytes

Measured on WROOM-32: every response up to **947 bytes** returned in under
0.7 s; every response from **1025 bytes** up failed — with 32 KB of heap
free, so this is a write-size limit, not memory. Never raise this without
re-measuring.

### 3.5 Content-Length is bytes, not characters

`json.dumps()` emits non-ASCII raw. A zone named `Jardín` makes
`len(str)` under-count, the body is truncated, and the browser hangs
waiting for the rest.

### 3.6 The dashboard must not fetch in parallel

A page load that fires 6–7 `fetch()` calls at once opens that many sockets
simultaneously and drops the C heap from ~31 KB to **760 bytes** for the
duration — failing anything else that arrives in that window (a second
tab, a refresh, the periodic poll). The same requests issued **one at a
time** are stable indefinitely (220 requests, flat heap). Await every
startup fetch, and stagger periodic timers — 5 s/15 s/60 s all divide into
60 and otherwise fire as one burst every minute.

### 3.7 WiFi modem power-save costs 100–450 ms per request

MicroPython defaults to `PM_PERFORMANCE`; the radio sleeps between DTIM
beacons. Measured: ping averaged **173 ms with 0 % packet loss** — which
never looks like a fault — and `/api/status` took 2586 ms. With
`wlan.config(pm=network.WLAN.PM_NONE)`: **10 ms** and 128 ms. Disable it
unless the build is battery-powered.

### 3.8 I2C will bite you

- An interrupted slave holds SDA low and wedges the bus for everything on
  it — **and this survives a reboot**, because rebuilding the peripheral
  resets only the master. Do the standard bus-clear at boot **before**
  constructing `I2C()`: up to 9 bit-banged SCL pulses, then a manual STOP.
- **Open-drain discipline is mandatory.** Never drive an I2C line high
  push-pull. Driving high while a slave holds it low shorts your driver
  into its transistor — tens of mA, a sagging 3.3 V rail, and the WiFi
  radio browning out. Emulate "high" by releasing the pin to an input.
- Construct the bus with an explicit `timeout` and pin it to 100 kHz
  (400 kHz is far less tolerant of long unshielded garden runs).
- **Reading a board that is not on the bus costs ~0.8 s per zone** — the
  timeout is not honoured. Three dead zones stretched the main loop from
  15 s to 45 s, which timed out every dashboard request and looked exactly
  like a network fault. A bus scan costs ~30 ms, so **probe for presence
  and issue no reads when nothing answers.**
- Isolate each zone's read so one bad probe cannot abort the cycle. But
  note the trap: if per-zone errors are swallowed, a *total* failure
  returns an empty list instead of raising, and any backoff that watches
  for an exception will never fire. Detect total failure from the result.

### 3.9 Use the monotonic clock for anything safety-related

`time.time()` moves when NTP syncs. A backward step made elapsed time
negative and the valve cutoff never fired — water running with the last
safety net disabled. Use `time.ticks_ms()` / `time.ticks_diff()` for the
cutoff and every interval. Note `ticks_ms()` values wrap: combine them
only with `ticks_add`/`ticks_diff`, never plain arithmetic.

### 3.10 Miscellaneous, all measured

- `socket.send()` returns bytes actually written — it is not `sendall`.
  Ignoring the return truncates responses.
- MicroPython's HTTPS (`ssl.wrap_socket`) **hangs unrecoverably** on this
  hardware. Any update mechanism must use plain HTTP to a mirror.
- Serve `index.html` by **streaming from flash in chunks**, never as a
  Python string literal — a ~34 KB literal needs one contiguous allocation
  to compile and reliably raises `MemoryError` on a fragmented heap.
- Pre-compress the dashboard at build time (~101 KB → ~29 KB) and serve it
  with `Content-Encoding: gzip`, falling back to the plain file.

---

## 4. Architecture

Flat filesystem on the device. Suggested module split (sizes are the
reference implementation's, as a sanity check on scope):

| Module | ~lines | Responsibility |
|---|---|---|
| `boot.py` | 82 | **Only** OTA rollback protection (see §11) |
| `main.py` | ~1400 | Boot sequence + main loop |
| `web.py` | ~1900 | HTTP server + JSON API |
| `index.html` | ~2200 | Single-page dashboard (HTML/CSS/JS) |
| `updater.py` | 614 | OTA check/download/install |
| `wifi_setup.py` | 562 | Captive setup portal + runtime rescue AP |
| `state.py` | 339 | Shared in-memory state, capped ring buffers, event log |
| `wifi.py` | 340 | Connect, reconnect, link health |
| `settings_store.py` | 192 | Runtime settings + migrations |
| `moisture.py` | 189 | Raw ADC → percent, zone assembly, board presence |
| `valve.py` | 88 | One solenoid + the hard cutoff |
| `ads1x15.py` | 83 | Minimal ADS1115 driver |
| `env_sensors.py` | 77 | AHT20 + BMP280, auto-detected |
| `config.py` | ~200 | First-boot defaults only |

### Boot order (order is load-bearing)

1. **Drive every valve pin to its closed state.** Before WiFi, before
   anything. Read pins straight from `settings.json` with `config.VALVES`
   as fallback — no module imports, this runs before the app loads.
   Rationale: an unconfigured GPIO is a floating input, and WiFi
   association takes up to 20 s (forever if the setup portal opens).
   Handle active-low wiring correctly.
2. Log `machine.reset_cause()` — the cheapest brownout test there is.
3. Connect WiFi (or open the setup portal).
4. Import application modules.
5. Load settings.
6. I2C bus recovery, then construct `I2C()`.
7. Auto-detect environment sensors by bus scan.
8. Start the web server (failure here must **not** be fatal).
9. Arm the hardware watchdog.
10. Enter the main loop.

### The main loop, every ~200 ms

Feed the watchdog; check the valve safety cutoff **per valve, each wrapped
individually** (an exception escaping the loop ends the program and leaves
open valves open); close finished waterings and drain the queue; check
moisture on an interval; check schedules; WiFi health; NTP; queued work;
status LED; CPU/heap sampling; optional nightly reboot; `gc.collect()`.

**Anything slow belongs in the loop, never in an HTTP handler.** OTA
checks, calibration captures and I2C scans are queued by handlers via
flags and performed by the loop, because blocking inside the server also
delays the valve cutoff.

---

## 5. Watering logic

### Valves and zones

- `hardware.valves` is a list of `{name, pin, active_high, flow_meter_pin,
  watering_mode, target_volume_l}`.
- **Only one valve is open at a time, system-wide.** A deliberate
  simplification driven by supply-line pressure, not a hardware limit.
  Multiple valves run **sequentially** via a pending queue drained as each
  valve closes.
- A **zone** is a physical spot where one sensor sits — not a GPIO and not
  an ADS channel. Zones map to one or more valves (`zone_valves`), so one
  sensor can water several beds.

### Two independent triggers

**1. Schedules** — a list of `{id, hour, minute, duration_sec, enabled,
valve_names, zone_names}`. Each fires once at its time, running every valve
in `valve_names` plus each zone's valves, deduped, in order. Zone names are
expanded to valves **at fire time**, so re-mapping a zone updates its
schedules automatically.

**2. Moisture** — if a zone reads below its threshold, its mapped valves
fire in sequence, each for that zone's own run time (`zone_durations`,
falling back to `supplemental_duration_sec`). Subject to:

- a post-schedule lockout (`post_daily_lockout_sec`, default 4 h)
- a minimum interval between moisture triggers
  (`min_supplemental_interval_sec`, default 2 h)

Both are tracked **per valve**, so a trigger on one valve never blocks an
unrelated one. Only valves passing their lockout are queued; if a zone's
valves are all locked out, the next dry zone is tried. If several zones are
dry, only the first runnable one fires now — the rest are picked up next
cycle (they will still read dry).

### Soak-and-recheck (hysteresis)

A moisture trigger starts a *session*. After the watering closes, wait
`soak_recheck_sec` (clamped to at least one sensor interval), re-read, and
water again if the zone is still below its **wet target**
(`zone_wet_targets`, default threshold + 10). So: trigger at "dry below",
stop at "water until". Re-waters within a session bypass the cooldowns —
it is the same dry event — up to `max_water_cycles` (default 3; **1
disables the recheck**, and is also the flood guard if a sensor fails).
New triggers are blocked while a session is active; cooldowns run from the
session's last close.

### Safety layers

- **Hard cutoff:** any valve open longer than `MAX_VALVE_OPEN_SEC` is
  force-closed, checked every loop, independently per valve, on the
  **monotonic** clock.
- **Hardware watchdog** (`WATCHDOG_TIMEOUT_SEC`, default 120 s). Valves
  close on boot, so a hang cannot leave water running. **Set to 0 while
  developing at the REPL** — once armed it cannot be stopped and the board
  will boot-loop.
- **Startup grace** (`STARTUP_GRACE_SEC`, default 60 s): sensors are read
  immediately so the dashboard populates, but no valve opens. Capacitive
  probes need to settle, and one reading taken microseconds after power-on
  once opened a valve on already-wet soil.
- **Cooldowns persist to flash** (`watering_state.json`) and are restored
  at boot. They previously lived only in RAM, so a power cut erased all
  memory of recent watering and the planter re-watered immediately.
  Restored timestamps in the future or older than 7 days are discarded
  (pre-NTP 2000-epoch values).
- **Schedules are held until NTP succeeds** — otherwise the clock sits at
  the 2000 epoch and schedules fire at nonsense times. Moisture watering
  runs regardless; it does not need the time.

---

## 6. Configuration

Two layers. `config.py` supplies **first-boot defaults only**; after first
boot `settings.json` on the device is authoritative and is what the web UI
edits. Credentials live in `wifi.json`, written by the setup portal.

### 6.1 `config.py` — every setting

```python
# WiFi (first boot only; wifi.json wins afterwards)
WIFI_SSID, WIFI_PASSWORD

# I2C
I2C_SCL_PIN = 22            # WROOM-32 defaults
I2C_SDA_PIN = 21
ADS1115_ADDRESS = 0x48
I2C_FREQ = 100000           # do not raise without short, clean wiring
I2C_TIMEOUT_US = 50000      # bounds a transaction so a stuck slave
                            # cannot stall the loop that feeds WiFi
I2C_BUS_RECOVERY = True     # clock a wedged bus free at boot

# Hardware
VALVES = [{"name": "valve1", "pin": 26, "active_high": True,
           "flow_meter_pin": None}]
ZONES  = [{"name": "zone1", "channel": 0, "dry_raw": 17500,
           "wet_raw": 8000, "threshold_percent": 30, "valve": "valve1"}]

# Watering defaults (all runtime-editable afterwards)
DAILY_WATER_HOUR = 6
DAILY_WATER_MINUTE = 0
DAILY_WATER_DURATION_SEC = 300
SUPPLEMENTAL_WATER_DURATION_SEC = 60
MIN_SUPPLEMENTAL_INTERVAL_SEC = 7200
POST_DAILY_LOCKOUT_SEC = 4 * 3600

# Safety
MAX_VALVE_OPEN_SEC = 600
WATCHDOG_TIMEOUT_SEC = 120    # 0 while developing at the REPL
STARTUP_GRACE_SEC = 60

# Timing
MOISTURE_CHECK_INTERVAL_SEC = 15
ADS_PROBE_SEC = 60            # how often to re-check for an absent board
TZ_OFFSET_SEC = -5 * 3600     # no DST automation
DAILY_REBOOT_HOUR = 0         # None disables

# Network behaviour
WIFI_POWER_SAVE = False       # see §3.7 - leave off
WIFI_RESCUE_AFTER_SEC = 300   # open the rescue hotspot after this long down
WIFI_HEALTH_CHECK_SEC = 900   # gateway TCP probe interval
WIFI_HEALTH_TIMEOUT_SEC = 3
NTP_RESYNC_SEC = 3600
NTP_STALE_RECYCLE_SEC = 6 * 3600

# Status LED
STATUS_LED_PIN = 2            # GPIO 48 on the S3
STATUS_LED_TYPE = "auto"      # "auto" | "rgb" (WS2812) | "plain"

# Debug
WEB_DEBUG = True              # one console line per HTTP request
WEB_SEND_DEBUG = False        # every socket send and its return value

# OTA (see §11)
UPDATE_REPO, UPDATE_BRANCH, UPDATE_CHECK_HOUR, UPDATE_AUTO_INSTALL,
UPDATE_MANIFEST_PATH, UPDATE_TIMEOUT_SEC, UPDATE_BASE_URL

# Flow meter groundwork
PULSES_PER_LITER = 450
```

**`config.py` must be gitignored** — it holds real WiFi credentials. Ship a
generated `config.example.py` with credentials scrubbed, and have the build
abort if a real credential survives scrubbing. Do not put trailing comments
on `.gitignore` pattern lines: git treats the whole line as the filename,
and that exact mistake published real credentials once.

### 6.2 `settings.json` — runtime schema

```jsonc
{
  "schedules": [ {"id":1,"hour":6,"minute":0,"duration_sec":300,
                  "enabled":true,"valve_names":[],"zone_names":[]} ],
  "supplemental_duration_sec": 60,
  "min_supplemental_interval_sec": 7200,
  "post_daily_lockout_sec": 14400,
  "zone_thresholds":   {"zone1": 30},   // trigger below this %
  "zone_durations":    {},              // per-zone run time, sec
  "zone_wet_targets":  {},              // stop at this %; default thr+10
  "soak_recheck_sec": 30,
  "max_water_cycles": 3,                // 1 disables the recheck
  "weather_zip": "",                    // browser-side widget; "" = geo-IP
  "tz_offset_min": -300,
  "daily_enabled": true,
  "moisture_watering_enabled": true,
  "hardware": {
    "i2c_scl_pin": 22, "i2c_sda_pin": 21,
    "ads1115_addresses": [72],          // 0x48.. one per board
    "valves": [ {"name":"valve1","pin":26,"active_high":true,
                 "flow_meter_pin":null,"watering_mode":"duration",
                 "target_volume_l":null} ],
    "zone_channels":    {"zone1": 0},   // GLOBAL channel index
    "zone_valves":      {"zone1": ["valve1"]},
    "zone_calibration": {"zone1": {"dry_raw":17500,"wet_raw":8000}},
    "flow_meter_pins": [],
    "rain_sensor_pin": null
  }
}
```

**Write forward-compatible migrations on load.** The reference
implementation migrates: a single schedule → a list; `valve_pin` →
`valves[]`; `ads1115_address` → `ads1115_addresses[]`; a zone's single
valve string → a list; and seeds `zone_calibration` for zones that predate
it. It also drops valve names that no longer exist. Users must never lose
their configuration to an upgrade.

Zones added purely through the web UI (present in `zone_channels` but not
in `config.ZONES`) must be synthesised with default calibration, or a zone
name alone in the UI does nothing.

---

## 7. HTTP API

Hand-rolled synchronous server polled from the main loop. All JSON.

```
GET  /                        the dashboard (gzip when accepted)
GET  /api/status              moisture + per-valve state. LEAN - polled
                              every 5 s; settings deliberately excluded
GET  /api/history             live 3 h buffer (RAM, 1/min, lost on reboot)
GET  /api/history?hours=N     saved history (flash, 1/15 min, 7-day)
GET  /api/events              recent event log
POST /api/valve?state=open|close&valve=NAME
POST /api/water/trigger?duration=N&valve=NAME
POST /api/zone/trigger?zone=NAME&duration=N     (zone's valves, sequential)
POST /api/water/all?duration=N                  (every valve, sequential)
GET  /api/settings            POST to update
GET  /api/schedules           POST = full replacement list
GET  /api/zones               POST = full replacement, live, no reboot;
                              renaming rewrites its key in zone_channels /
                              zone_valves / zone_thresholds atomically
GET  /api/valves              POST {valves, renames, flow_meter_pins};
                              full replacement, triggers reboot; renames
                              propagate to zones and schedules
GET  /api/hardware            POST {hardware:{...}}, triggers reboot
GET  /api/pinmap              GPIO roles + ADS channel assignments
GET  /api/i2c/scan            live bus scan -> {found:[addr,...]}
POST /api/calibrate           {zone, point:"dry"|"wet"} - queues a 10 s
                              averaged capture
GET  /api/calibrate           {busy, result, calibration}
GET  /api/config/export       whole config as a download (no WiFi creds)
POST /api/config/import       validate, save, reboot
GET  /api/wifi                saved SSID + state (NEVER the password)
POST /api/wifi                {ssid, password} -> wifi.json, reboot
POST /api/upload              multipart code upload, streamed to flash
POST /api/reboot
```

**Bound every length that comes off the network.** `Content-Length` is
client-supplied: cap request bodies (16 KB), uploads (512 KB with a 120 s
deadline), and portal bodies (2 KB). Parse remote integers defensively,
never with a bare `int()`. The upload deadline matters specifically because
that loop feeds the watchdog — without it, a trickling client keeps the
device alive forever with its own safety net disarmed.

---

## 8. Dashboard

One self-contained `index.html` (no build step, no framework, no CDN).
Cards: status banner, per-valve control, zones with moisture and manual
trigger, history chart with 3 h/24 h/7 d ranges, schedules editor, zones
editor, valve configuration, GPIO pin map, I2C scan, calibration wizard,
watering settings, WiFi settings, environment, weather, system status
(memory, C heap, CPU, RSSI, uptime), event log, config import/export,
code uploader, firmware updates.

Constraints: **fetch sequentially, never in parallel** (§3.6); stagger the
periodic timers; guard `refresh()` so polls cannot stack.

---

## 9. Status LED

`STATUS_LED_TYPE = "auto"` tries a WS2812 first and falls back to a plain
LED — driving a WS2812 as a plain output does nothing at all, which is why
the LED stayed dark on some devkits.

Plain: solid = all good; fast blink (~2.5 Hz) = WiFi down; slow blink
(~0.5 Hz) = web server down. RGB: dim green = good, amber blink = WiFi
down, red blink = server down, dim amber = startup grace, breathing blue =
watering, purple = updating.

---

## 10. First run and network recovery

**Boot portal (blocking).** If boot-time connect fails *and* the target
SSID is visible (wrong password, or a fresh kit), become an open AP
`Planter-Setup-xxxx`, hijack all DNS so phones auto-open the setup page,
save credentials to `wifi.json`, reboot. If the SSID simply is not visible
the router is probably down — do **not** open a portal; run offline and
retry, because watering does not need WiFi and a portal is unreachable in
a garden anyway. If credentials came from `wifi.json` and fail, always
open the portal — refusing strands the device with no network, no hotspot
and only a USB cable as a fix.

**Runtime rescue (non-blocking).** If WiFi stays down for
`WIFI_RESCUE_AFTER_SEC` mid-operation, open the same hotspot *alongside*
station mode. Watering continues, the full dashboard is reachable at
192.168.4.1, and it closes itself when the real network returns.

**AP startup order is load-bearing**, and two firmware behaviours pull
opposite ways: MicroPython raises `OSError: Wifi Invalid Mode` if
`ap.config()` is called while the interface is inactive, but applying
config to a running AP restarts it without reliably restarting its DHCP
server (phones associate, get no lease, iOS says "Unable to join
network"). Do: deactivate → **activate** → configure (ssid + `password=""`
+ `AUTH_OPEN` together; authmode alone can leave a keyless WPA2 AP) →
**bounce** the interface so DHCP restarts against the final config.

**Captive-portal probes** (`/hotspot-detect.html`, `/generate_204`,
`/ncsi.txt`) must get a **302**, not a 200 with the page — iOS treats an
unexpected 200 as a broken network and drops the association.

**AP and STA share one radio**, so park the station while the rescue AP is
up (a scanning STA drags the AP off-channel mid-handshake) and own
router-return retries from the rescue poller.

**Link health:** `isconnected()` means *associated*, not *working*. A
zombie connection (expired lease, cleared NAT, wedged lwIP) reports True
while nothing is reachable. TCP-probe the **gateway**: any reply proves the
path, **including ECONNREFUSED/ECONNRESET** — a refusal is still a packet.
Only a timeout is failure, and an unknown errno must be treated as
inconclusive, because a false positive costs a working connection. Probe
the gateway rather than the internet: the dashboard needs only the LAN, so
an ISP outage must never recycle a working link. Escalate: log → soft
reconnect → hard (drop and re-activate the interface, which clears lwIP).
Skip it while a valve is open.

---

## 11. Over-the-air updates

The device fetches a manifest, compares SHA-256 hashes, and downloads only
what changed. **Must use plain HTTP** — MicroPython's TLS hangs on this
hardware (§3.10). Point it at a mirror; a small Cloudflare Worker
proxying the public repo works and needs no maintenance.

Each manifest entry carries its own repo path, so the repository can be
reorganised without reflashing devices. Never touch `config.py`,
`wifi.json` or `settings.json` — credentials and settings must always
survive an update *and* a rollback.

**Rollback protection** lives in `boot.py`, which runs before `main.py`
and does nothing else. Every boot increments a counter; once `main.py` has
run stably for a minute it clears it. A counter reaching the limit means
"rebooted N times without ever running stably" — restore the `.bak` copies
the updater left behind. Without this, one bad build means a laptop, a USB
cable and a walk outside.

---

## 12. Build system

A script that: compiles each device module with `mpy-cross` into `build/`;
copies `main.py`, `config.py`, `index.html` and `boot.py`; pre-compresses
`index.html` to `index.html.gz`; regenerates `config.example.py` from
`config.py` with credentials scrubbed (**abort if a real credential
survives**); and writes `manifest.json` with a version and per-file
SHA-256 plus repo path.

Commit `build/` — OTA publishes from it.

---

## 13. Testing

There is no hardware in CI, and device modules import `machine`, so they
will not run under CPython. Practical approach:

- Every `.py` must at least **compile**: use `compile(src, name, "exec")`,
  not `ast.parse()` — `ast.parse` does **not** catch `await` outside an
  async function.
- Extract the dashboard's `<script>` block and check it with `node --check`.
- Test pure logic by stubbing `machine`, `network`, `socket`, `time` and
  friends in `sys.modules` before import, then driving the HTTP handler
  with a fake socket that records what was written. This catches
  truncation, wrong `Content-Length`, and oversized allocations.
- Test with a **maxed-out configuration** (4 boards, 8 zones, 8 valves, 20
  schedules), not a minimal one — several bugs only appear when a payload
  grows past a threshold.
- When you write a test for a hardware behaviour, model the hardware, not
  your assumptions. A fake I2C `Pin` with no concept of bus contention
  once passed happily while the real code shorted a held-low line.

---

## 14. Known unfixed issue — inherit this knowledge

The web listener stalls intermittently: the device stops serving for tens
of seconds to minutes while remaining completely healthy (main loop
running, watchdog fed, 8 MB free on an S3, answering pings), then
recovers. Port 80 gives either `ECONNREFUSED` or a timeout.

**It is not application code.** A ~40 line MicroPython server containing
none of this project — no watchdog, I2C, LED, flash writes, streaming or
keep-alive — reproduces it exactly. The fault is in MicroPython/lwIP on
this platform.

Ruled out by measurement, so do not re-investigate: memory (8 MB free at
failure), `gc.collect()` (0–20 ms), the filesystem, I2C timeouts (100
failed reads moved free memory *up*), the WS2812/RMT driver, GC-heap
growth, connection churn (keep-alive verified at 26 requests/socket), and
clients hanging up mid-response. Also tried without success: an asyncio
server with a task per connection, a chip with 260× the C heap, and
rebuilding the listening socket (which does **not** restore service).

Two genuine bugs remain in the reference implementation and are worth
fixing properly in a rebuild: a dead listener is indistinguishable from an
idle one because every `accept()` error is treated as "nothing waiting";
and the health check asks whether the socket *object* exists, which stays
true, so the retry never fires. Note that a fix for these was written and
reverted because it regressed a previously stable device — and that
anything added to the main loop **must not block** (§3.3).

---

## 15. Definition of done

- A valve cannot be left open by any fault path, including a hung loop, a
  crash, a power cut or a bad OTA.
- Watering continues with no WiFi, no dashboard, no sensors and no clock
  (schedules excepted, which need NTP).
- First run works for someone who cannot open a serial console.
- Settings survive reboots, updates and rollbacks.
- The dashboard is usable on a phone and stays responsive with two tabs
  open.
- Peak single allocation per response stays a few hundred bytes on a
  fully-populated configuration.
- The console explains what is wrong without a debugger.
