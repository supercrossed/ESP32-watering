# ESP32 Planter

A standalone automated plant-watering controller for the ESP32-WROOM-32,
written in MicroPython. Capacitive moisture sensors decide when to water,
solenoid valves do the watering, and a built-in web dashboard runs the whole
thing from your phone or laptop. No cloud account, no hub, no app - the
device serves its own dashboard on your LAN.

> **Status:** working and in daily use. Flow-meter volume watering and
> rain-skip are groundwork-only (see [Roadmap](#roadmap)).

---

## Contents

- [What it does](#what-it-does)
- [Hardware](#hardware)
- [Wiring](#wiring)
- [Install](#install)
- [First-time setup](#first-time-setup)
- [Calibrating a moisture sensor](#calibrating-a-moisture-sensor)
- [How the watering logic works](#how-the-watering-logic-works)
- [The dashboard](#the-dashboard)
- [Over-the-air updates](#over-the-air-updates)
- [Development](#development)
- [API reference](#api-reference)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)

---

## What it does

- **Waters on soil moisture.** Each zone has its own "dry below" threshold
  and a "water until" wet target, so watering stops when the soil is
  actually wet rather than after a fixed guess.
- **Waters on a schedule.** Any number of daily schedules, each targeting
  one or more valves or zones.
- **Soak-and-recheck.** After watering it waits for the water to soak in,
  re-reads the sensor, and waters again if the zone is still dry - up to a
  configurable cycle limit, so a stuck sensor can't flood a bed.
- **Multiple zones and valves.** One sensor can drive several valves; one
  valve can serve several zones. Only one valve runs at a time (deliberate:
  supply pressure), and multi-valve runs are queued sequentially.
- **Keeps working when the network doesn't.** Watering is entirely local.
  If WiFi drops, the device retries; if it stays down, it opens its own
  rescue hotspot so you can still reach the dashboard.
- **Environment sensors (optional).** AHT20 temp/humidity and BMP280
  pressure are auto-detected on the I2C bus. An LM393 rain sensor can be
  added from the pin map.
- **Updates itself.** Daily check against this repo, one-click install from
  the dashboard, with automatic rollback if a bad build won't boot.

---

## Hardware

| Part | Notes |
|---|---|
| ESP32-WROOM-32 devkit | 38-pin. Flash with MicroPython 1.23+ |
| ADS1115 ADC board | Up to 4 on one I2C bus. Moisture sensors are analog - they do **not** connect to GPIO |
| Capacitive soil moisture sensors | e.g. AITRIP. One per zone, into an ADS1115 channel (A0-A3) |
| 12V solenoid valve(s) | One per valve/bed |
| MOSFET switch module | One per valve. A D4184/XY-MOS module works well at 3.3V logic; some IRF520 boards do **not** switch fully at 3.3V (see [Troubleshooting](#troubleshooting)) |
| 1N4007 diode | One per solenoid, flyback protection - **required** |
| 12V power supply | Sized for your valves |
| AHT20 + BMP280 board | Optional, auto-detected |
| LM393 rain sensor | Optional |
| YF-S201 flow meter | Optional; config/UI groundwork only so far |

### Pin safety (ESP32-WROOM-32)

| Pins | Status |
|---|---|
| 6-11 | **Never use** - wired to the SPI flash |
| 1, 3 | UART0 (USB serial) - don't assign |
| 0, 2, 12, 15 | Boot-strapping pins; usable as outputs after boot with care (12 is riskiest - held high at reset breaks boot). GPIO 2 is the onboard "D2" LED, used here as a status light |
| 34, 35, 36, 39 | Input-only, no pull-ups. Fine for a flow meter or rain sensor, **not** for a valve |
| 16, 17 | Usable on WROOM (only reserved on WROVER, where PSRAM takes them) |
| 4, 5, 13, 14, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33 | Freely usable |

The dashboard's GPIO pin map shows all of this live, and refuses assignments
that would break the board.

---

## Wiring

**I2C bus** (shared by every ADS1115 and the optional environment sensor):

```
ESP32 GPIO 22 -> SCL   (all boards)
ESP32 GPIO 21 -> SDA   (all boards)
ESP32 3V3     -> VCC
ESP32 GND     -> GND
```

Multiple ADS1115 boards share those same two pins - they're a **bus**, not
one-board-per-pin. Give each board a different address with its ADDR pin:

| ADDR wired to | Address |
|---|---|
| GND | 0x48 |
| VDD | 0x49 |
| SDA | 0x4A |
| SCL | 0x4B |

Zone channels are numbered globally: board 1 = channels 0-3, board 2 = 4-7,
and so on. The dashboard's **Scan Bus** button lists which addresses actually
answer, so you can confirm wiring before assigning zones.

**Moisture sensor** -> ADS1115 `A0`-`A3` (analog out), `VCC` -> 3V3, `GND` -> GND.
Power these from 3.3V, not 5V - readings are more stable.

**Valve** (per valve):

```
ESP32 GPIO (e.g. 26) -> MOSFET module PWM/SIG
ESP32 GND            -> MOSFET module GND
12V supply +/-       -> MOSFET module DC+ / DC-
Solenoid             -> MOSFET module OUT+ / OUT-
1N4007 across the solenoid terminals (band toward +)
```

The diode is not optional - it absorbs the inductive kickback when the
solenoid closes, which otherwise destroys the MOSFET.

---

## Install

### 1. Flash MicroPython

Download the ESP32 build from [micropython.org/download/ESP32_GENERIC](https://micropython.org/download/ESP32_GENERIC/),
then:

```bash
pip install esptool
esptool.py --chip esp32 --port COM3 erase_flash
esptool.py --chip esp32 --port COM3 --baud 460800 write_flash -z 0x1000 ESP32_GENERIC-*.bin
```

Replace `COM3` with your port (`/dev/ttyUSB0` on Linux, `/dev/cu.*` on macOS).

### 2. Get the code

```bash
git clone https://github.com/supercrossed/esp32-planter.git
cd esp32-planter
cp config.example.py config.py
```

Edit `config.py` with your WiFi details, or leave them blank and use the
setup hotspot (see [First-time setup](#first-time-setup)).

### 3. Build

Device modules ship as pre-compiled `.mpy` bytecode. This is not an
optimization - it's required. Compiling these modules **on the device**
makes MicroPython grow its heap out of the ESP-IDF C heap that the WiFi
driver needs, which starves the network stack (see
[The two-heap problem](#the-two-heap-problem)).

```powershell
pip install mpy-cross
.\build_mpy.ps1
```

This produces `build/` containing the `.mpy` modules plus `main.py`,
`boot.py`, `config.py`, `index.html`, and a `manifest.json` for OTA.

### 4. Upload

Copy the **contents of `build/`** to the device with
[Thonny](https://thonny.org/) (View -> Files, select all, Upload to /) or
`mpremote`:

```bash
mpremote connect COM3 fs cp build/* :
```

Reset the board. It boots straight into `main.py`.

> If you previously ran the plain-`.py` version, delete the old module
> `.py` files from the device - a `.py` shadows its `.mpy` counterpart.

---

## First-time setup

**If you edited `config.py` with your WiFi details**, the planter joins your
network on boot and prints its IP to the serial console. Open that IP in a
browser, or try <http://planter.local>.

**If you left the WiFi fields blank** (or the password is wrong), the planter
opens a setup hotspot:

1. On your phone, join **`Planter-Setup-xxxx`** (open network).
2. A setup page opens automatically (captive portal). If not, browse to
   <http://192.168.4.1>.
3. Pick your network, enter the password, save. The planter reboots and
   joins your WiFi.

Credentials are stored in `wifi.json` on the device, never in the repo.

Then, in the dashboard:

1. **GPIO Pin Map** - assign your valve pin(s) and confirm the I2C pins.
   Click **Scan Bus** to verify your ADS1115 boards are detected.
2. **Watering Zones** - add a zone per sensor, set its ADS channel, pick
   which valve(s) it waters, and set thresholds.
3. **Schedules** - optional; add a daily run if you want one.

Before trusting it unattended, open and close a valve manually from the
Valve Controls card and confirm water actually flows and stops.

---

## Calibrating a moisture sensor

Raw ADC readings map to a percentage via two calibration points. Defaults
(`dry_raw=17500`, `wet_raw=8000`) are reasonable for AITRIP sensors, but
each sensor differs - don't assume they share a curve.

1. Hold the sensor **in dry air**. Note the raw value in the Moisture
   Readings card - that's `dry_raw`.
2. Put it **in a glass of water** up to (not past) the line. That's
   `wet_raw`.
3. Enter both in the zone editor.

Do not submerge the electronics above the marked line.

---

## How the watering logic works

Two independent triggers, both resolving to a list of valve names before
anything opens. **Only one valve is ever open at a time** system-wide;
multi-valve runs are queued and run sequentially.

### Moisture

Every zone is read on a cycle (15s by default). If a zone reads below its
**dry threshold**, its mapped valves run - each for that zone's own run time.

Then **soak-and-recheck** begins: wait `soak_recheck_sec`, re-read, and if
the zone is still below its **wet target**, water again. Up to
`max_water_cycles` (default 3; set to 1 to disable). This hysteresis - dry
below X, water until Y - is what stops the "water a little, still dry, water
again forever" cycle. It's also the flood guard if a sensor fails.

Two cooldowns prevent over-watering, tracked **per valve** so a trigger on
one valve never blocks an unrelated one:

- `min_supplemental_interval_sec` - minimum gap between moisture triggers
- `post_daily_lockout_sec` - skip moisture watering for N hours after a
  scheduled run (default 4h)

Re-waters inside one soak session bypass the cooldowns (same dry event);
cooldowns then run from the session's last close.

### Schedules

Each schedule has a time, duration, and a set of valves and/or zones. Zones
are expanded to valves **at fire time**, so re-mapping a zone automatically
updates every schedule using it.

Scheduled watering is held until NTP sync succeeds - otherwise the clock
sits at the year-2000 epoch and schedules would fire at nonsense times.
Moisture watering runs regardless; it doesn't care what time it is.

### Safety

- A hard cutoff force-closes any valve open longer than
  `MAX_VALVE_OPEN_SEC`, checked every loop iteration, per valve.
- A hardware watchdog reboots the board if the main loop ever hangs. Valves
  close on boot, so a hang cannot leave water running.
- Every main-loop subsystem is individually exception-wrapped: a failing
  sensor or a network error can't stop the watering logic.

---

## The dashboard

Served directly from the ESP32 at its IP (or <http://planter.local>).

| Card | What it does |
|---|---|
| Weather | Local forecast, hourly expansion (browser-side; set a ZIP) |
| System Status | Plant-art status banner, per-zone and per-valve state, uptime, WiFi, CPU/memory |
| Zone Controls | Water a single zone on demand |
| Valve Controls | Open/close each valve, quick 30s water, water-all |
| Moisture Readings / History | Live values and a 3-hour chart |
| Environment | Temp, humidity, pressure, rain (if those sensors exist) |
| Watering Zones | Add/edit zones: channel, thresholds, run time, valve mapping |
| Watering Settings | Durations, cooldowns, soak settings, timezone |
| Schedules | Daily schedules with a phone-style time picker |
| GPIO Pin Map | Full pin table; click a pin to assign a role |
| WiFi | Change network without reflashing |
| Config Backup | Export/import the whole configuration as JSON |
| Update Code | Drag-and-drop `.mpy`/`.py`/`.html` upload |
| Firmware | Version, last check, last update, and a **Check/Update now** button |

Dark mode toggle and clock live in the header. Settings are stored in
`settings.json` on flash and survive reboots.

---

## Over-the-air updates

The planter can update itself from this repo - no Pi or host needed.

**How it works.** `build_mpy.ps1` generates `manifest.json` listing every
device file with its SHA-256. The device fetches that manifest, compares
hashes against what it has, and downloads only what differs.

**Safety properties** (each one exists because of a specific failure mode):

- Files stream to flash 512 bytes at a time - never buffered in RAM.
- Downloads land on `.new` temp files. **Nothing** overwrites a live file
  until every file in the batch has downloaded and verified, so a dropped
  connection leaves the running firmware untouched.
- Every file is SHA-256 verified; a mismatch is discarded.
- Replaced files are kept as `.bak`. If the new build fails to boot three
  times, `boot.py` restores them automatically and reboots. A bad update
  self-heals instead of requiring a laptop and a walk out to the garden.
- `config.py`, `wifi.json`, and `settings.json` are never updatable - your
  credentials and per-device settings always survive.
- Updates never run while a valve is open.

**Configuration** (in `config.py`):

```python
UPDATE_REPO = "supercrossed/esp32-planter"
UPDATE_BRANCH = "main"
UPDATE_CHECK_HOUR = 4        # daily check at 4am local; None disables
UPDATE_AUTO_INSTALL = False  # notify only; you press "Update Now"
```

`UPDATE_AUTO_INSTALL = False` is the default on purpose: this device
controls water valves, so you choose when it reboots. Set it to `True` for
true set-and-forget.

**Publishing an update** (maintainer):

```powershell
.\build_mpy.ps1
git add -A
git commit -m "describe the change"
git push
```

Committing `build/manifest.json` and the `.mpy` files is what publishes a
release to every planter.

**TLS note.** GitHub is HTTPS-only and a handshake needs ~30-45KB of
contiguous ESP-IDF heap. The updater collects garbage first and treats a
failed handshake as "try again tomorrow" rather than an error. If TLS proves
unreliable on your board, point `UPDATE_BASE_URL` at a plain HTTP mirror -
everything else works unchanged.

---

## Development

```
config.py           first-boot defaults (gitignored; copy from config.example.py)
settings_store.py   runtime settings + migrations -> settings.json
state.py            shared in-memory state, capped ring buffers
ads1x15.py          minimal ADS1115 driver
env_sensors.py      AHT20 + BMP280 drivers
moisture.py         raw ADC -> percent
valve.py            solenoid control + safety cutoff
wifi.py             connect/reconnect helpers
wifi_setup.py       captive portal + runtime rescue hotspot
web.py              HTTP server + JSON API
updater.py          OTA: manifest, verify, atomic install
index.html          the dashboard (single page)
boot.py             OTA rollback guard
main.py             boot sequence + main loop
```

`config.py` holds first-boot defaults only; after the first boot,
`settings.json` on the device takes priority.

### The two-heap problem

The ESP32 has **two** memory pools: MicroPython's GC heap (what
`gc.mem_free()` reports) and the ESP-IDF C heap that the WiFi, lwIP, and
I2C drivers allocate from. MicroPython grows its GC heap out of the C heap
on demand and never gives it back.

Compiling this project's modules on-device once left **764 bytes** of C heap
at loop start - the network died (`wifi:fail to alloc timer`,
`i2c command link malloc error`, `getaddrinfo OSError -203`) while
`gc.mem_free()` looked perfectly healthy. Shipping `.mpy` bytecode moved
that cost to build time: the same board now idles at ~33KB free.

Both figures are printed at boot and exposed as `idf_free`/`idf_largest`
in `/api/status`.

Practical consequences when editing:

- Never build large strings; stream instead (see `_send_file`,
  `_send_history`, `_stream_multipart_to_disk` in `web.py`).
- `index.html` is streamed from flash in 1KB chunks. It used to be a Python
  string literal, which needs one contiguous allocation to compile and
  reliably failed with `MemoryError`.
- Keep `index.html` **ASCII-only** - use `&deg;`, `&rarr;`, inline SVG.
  Non-ASCII bytes have corrupted in transit.
- The web server is hand-rolled and synchronous, polled from the main loop
  via `web.poll_once()`. It must never block - the same loop handles valve
  safety cutoffs.

### Testing

There's no hardware in CI, so at minimum keep everything parseable:

```bash
# every .py must parse (they won't *run* under CPython - they import machine)
for f in *.py; do python3 -c "import ast; ast.parse(open('$f').read())"; done

# dashboard JS: extract the <script> block from index.html, then
node --check dash.js
```

Device modules are testable under CPython by stubbing `machine`, `network`,
and `ujson` and loading modules by path - that's how the updater, the
rollback guard, and the rescue-AP logic are verified.

---

## API reference

```
GET  /api/status              moisture + per-valve state + system health
GET  /api/history             moisture readings over time
GET  /api/events              recent event log
POST /api/valve?state=open|close&valve=NAME
POST /api/water/trigger?duration=N&valve=NAME
POST /api/zone/trigger?zone=NAME&duration=N
POST /api/water/all?duration=N
GET  /api/settings            watering settings
POST /api/settings
GET  /api/schedules
POST /api/schedules           full replacement
GET  /api/zones
POST /api/zones               full replacement, live (no reboot)
GET  /api/valves
POST /api/valves              full replacement, reboots
GET  /api/pinmap              GPIO roles + ADS channel assignments
GET  /api/i2c/scan            live bus scan
GET  /api/hardware
POST /api/hardware            reboots
GET  /api/wifi                saved SSID + state (never the password)
POST /api/wifi                save credentials, reboot
GET  /api/config/export       full config as a JSON download
POST /api/config/import       restore, validates then reboots
POST /api/update/check        check the repo for a new version
POST /api/update/apply        download, verify, install, reboot
POST /api/upload              multipart .mpy/.py/.html upload
POST /api/reboot
```

There is **no authentication** - anyone on your LAN can control the planter.
Don't port-forward it. See [Roadmap](#roadmap).

---

## Troubleshooting

**Valve won't open / MOSFET output reads ~1V.** Many IRF520 modules are not
true logic-level at 3.3V gate drive. Under load the output sags and the
solenoid never pulls in. A D4184/XY-MOS module switches properly at 3.3V.

**`[Errno 19] ENODEV` on moisture reads.** The ADS1115 isn't answering.
Check SDA/SCL wiring and power, then hit **Scan Bus** in the dashboard. After
3 consecutive failures the read interval backs off to 5 minutes automatically
(a failing I2C transaction churns the same C heap WiFi needs).

**Dashboard unreachable but the console says WiFi is connected.** Check the
gateway on the boot line: if it differs from your PC's, the ESP32 joined a
different access point (extender/mesh node) that may isolate clients. Set
`WEB_DEBUG = True` in `config.py` - every HTTP request then prints to the
console, which tells you definitively whether packets are arriving.

**Status LED meaning** (onboard D2, GPIO 2):

| LED | Meaning |
|---|---|
| Solid | WiFi connected and web server up |
| Fast blink (~2.5Hz) | WiFi down, reconnecting |
| Slow blink (~0.5Hz) | WiFi up, web server down |

**Board boot-loops at the REPL.** The watchdog can't be stopped once armed.
Set `WATCHDOG_TIMEOUT_SEC = 0` in `config.py` while developing with Thonny.

**Uploaded a broken file and it won't boot.** The board isn't bricked -
reconnect via USB in Thonny and re-upload a known-good file. OTA updates
roll back automatically; manual uploads don't.

**"incompatible .mpy file"** after a firmware flash. Bytecode versions
changed: `pip install --upgrade mpy-cross` and rebuild.

---

## Roadmap

- Flow-meter volume-based watering (pulse counting; config/UI groundwork done)
- Rain-skip using the rain sensor and forecast data
- Sensor-fault detection + a daily watering budget
- Authentication for the dashboard
- Optional Raspberry Pi orchestration layer for multi-planter setups
  (it would poll this JSON API over HTTP; no rewrite needed)

---

## License

MIT - see [LICENSE](LICENSE).
