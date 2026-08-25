# Changelog

[<- Docs index](README.md)

Notable changes, newest first. Add an entry for anything user-visible - see
[CONTRIBUTING.md](CONTRIBUTING.md#the-checklist).

## 2026-08-24

### Added: moisture history survives reboots
The chart previously lived only in RAM, so every reboot - including the
new nightly maintenance one - wiped it. History is now also written to
`history.csv` on flash: one point per 15 minutes, kept for 7 days,
~21KB for two zones.

The Moisture History card gains **3h / 24h / 7d** buttons. 3h still serves
the live 1/minute RAM buffer (finest detail, resets on reboot); 24h and 7d
stream from flash and persist.

A week at one point per *minute* would have been ~10,000 dicts - several
times the entire heap, and memory exhaustion has twice taken this device's
network down. Hence flash, compact CSV, appended one short line at a time
and never read into RAM as a whole.

Old lines are purged roughly once a day, along with any torn by a power cut
mid-append. Nothing is written before NTP sync, since 2000-epoch timestamps
would sort before all real data.

New: `GET /api/history?hours=N`.

### Enabled: nightly maintenance reboot at midnight
`DAILY_REBOOT_HOUR = 0`. Guards were already in place and are unchanged: it
skips if a valve is open or queued, if the clock isn't NTP-synced, or if the
board has been up less than an hour (so a reboot can't become a loop).

---

## 2026-08-22

### Added: guided moisture sensor calibration
**Watering Zones -> Calibrate** walks through the two-point calibration:
capture the raw reading with the soil at its driest, then again when
saturated. Each capture averages the probe for 10 seconds (a single
capacitive reading wanders by a few percent) and reports the spread, so a
probe that hasn't settled is visible rather than silently baked in. Points
save individually, so dry and wet can be captured days apart.

The dialog also flags a backwards calibration (dry must read higher than
wet) and a suspiciously narrow range, and accepts raw values typed by hand.

New endpoints `POST`/`GET /api/calibrate`. Like the OTA check, the capture
runs in the main loop rather than the HTTP handler - 10 seconds of blocking
there would freeze the web server and valve timing.

### Fixed: calibration was unreachable for UI-added zones
Calibration lived only in `config.ZONES`, so any zone created through the
dashboard silently used hardcoded defaults with no way to correct them. It
is now `hardware.zone_calibration` in `settings.json` - real runtime state,
like every other per-zone setting - and is preserved when a zone is renamed
or edited.

---

## 2026-08-19

### Added: RGB (WS2812) status LED support
Newer boards - WROOM-32UE and most recent devkits - have a WS2812 RGB LED
on GPIO 2 rather than the classic plain blue one. A WS2812 needs a timed
data protocol, so the old code driving that pin as a plain output did
nothing at all and the LED sat dark.

The firmware now auto-detects which kind is present (`STATUS_LED_TYPE`,
default `"auto"`; force with `"rgb"` or `"plain"`). Colour carries the
state, which is far easier to read across a garden than counting blinks:
dim green = healthy, breathing blue = watering, purple = updating, dim
amber = settling after boot, amber blink = WiFi down, red blink = web
server down. Writes only happen on a colour change, so the bit-banged
WS2812 protocol isn't run every loop iteration.

Plain-LED boards behave exactly as before.

### Documented: `import main` at the REPL breaks networking
Starting the controller with `import main` in Thonny leaves the ESP-IDF C
heap exhausted (936 bytes free vs the usual ~138,000), so WiFi associates
and gets an IP but the device can't allocate enough to answer HTTP -
`[Errno 116] ETIMEDOUT` on every request. Press EN/RST for a real boot
instead. Added to troubleshooting.

---

## 2026-07-25 (later)

### WiFi credential saves are now verified
Changing the network from the dashboard already persisted to `wifi.json`
(and still does - it survives reboots and outranks `config.py`). But the
write wasn't checked: if it failed, the device rebooted onto the *old*
network having reported success - the worst case, since you're usually
changing WiFi because the old network is gone. The save is now read back
and compared before rebooting, and a failure surfaces in the dashboard
with the planter left on its current network. Same protection in the setup
portal.

### Fixed: watered immediately on every power-on
Reported after a week of real use: unplugging and replugging the planter
made it water straight away, regardless of how wet the soil was.

Two independent causes, both fixed:

- **No settling time.** The first moisture check ran on the very first loop
  iteration, so a valve could open on a single reading taken microseconds
  after power-on - before a capacitive probe has settled. Moisture watering
  now waits `STARTUP_GRACE_SEC` (60s default) after boot. Sensors are still
  read immediately, so the dashboard populates right away; only watering is
  held, and the System Status card shows `Startup: settling - watering held
  for Ns` so it doesn't look like a fault.
- **Cooldowns lived only in RAM.** A power cut erased all memory of recent
  watering, so `min_supplemental_interval_sec` and `post_daily_lockout_sec`
  both read as "never watered". They're now written to
  `watering_state.json` when a watering finishes and restored at boot.
  Timestamps from before an NTP sync (2000 epoch) are discarded rather than
  trusted.

New setting `STARTUP_GRACE_SEC` in `config.py`; 0 disables. New
`startup_grace_left` field in `/api/status`.

---

## 2026-07-25

### Fixed: OTA downloads truncated on larger files
The download loop treated an empty `read()` as end-of-stream. On a socket
with a timeout an empty read *also* means "the next packet hasn't arrived
yet", so the loop stopped early, hashed a partial file, and reported a hash
mismatch. `index.html` (~90KB) failed consistently while the small `.mpy`
files passed - they never spanned enough packets to hit the window.

The loop now reads until it has the expected byte count (manifest `size`,
falling back to `Content-Length`), gives up only after ~2s of genuine
silence, and fails explicitly on a short read.

### Fixed: "Updated: never" straight after an update
The install timestamp lived only in RAM, and installing *reboots the
device* - so it was always blank exactly when it mattered. It's now read
from `version.json`, which persists.

### Watering Settings inputs aligned
The rows were a `space-between` flex, so each input started wherever its
label ended, and the unit wrapped onto its own line when the label was
long. They're now a 3-column grid (label | input | unit).

### Added: source-code link on the Firmware card

---

## 2026-07-24 (later)

### HTTPS disabled by default - it hangs on ESP32-WROOM-32
Measured, not theoretical: `ssl.wrap_socket()` blocks indefinitely when it
can't complete the handshake, honoring no timeout and raising nothing. It
froze the main loop until the watchdog rebooted. Hung with 30,944 bytes free
and a 29,696-byte largest contiguous block, so no heap threshold makes it
safe.

`ALLOW_HTTPS` is now `False` by default; the updater refuses up front with a
clear message. Use a plain-HTTP mirror - `tools/cloudflare-worker.js`
(free, nothing to maintain, GitHub stays the source of truth) for kits, or
`serve_updates.ps1` for local testing.

### Fixed: update requests froze the whole device
The `/api/update/*` handlers performed the network fetch inline, so a
stalled handshake blocked the main loop - no HTTP responses (browser showed
"TypeError: Failed to fetch"), no valve timing. The endpoints now queue the
request and return immediately; the main loop does the work and the
dashboard polls `/api/status` for the outcome.

### Fixed: TLS timeout was applied too late
`settimeout()` was called after `ssl.wrap_socket()`, but the handshake
happens *inside* that call. Also added a DNS-failure path and a wall-clock
bound on header reads, plus `UPDATE_TIMEOUT_SEC` (default 15s).

---

## 2026-07-24

### Repo reorganized
Source moved to `src/`, artifacts to `build/`, 3D models to `hardware/`.
The repo root now shows five folders instead of 28 files.

The OTA manifest now records each file's **path within the repo**, so the
device knows where to fetch from and the repo can be reorganized without
touching device code. Manifests without a `path` (older format) fall back to
the bare filename. Paths containing `..`, a leading `/`, or a URL scheme are
refused - the manifest is remote input and treated as untrusted.

New setting: `UPDATE_MANIFEST_PATH` (default `build/manifest.json`).

### Documentation restructured
The 520-line README became a 147-line quick start plus task-focused guides
in `docs/`. Added `CONTRIBUTING.md` with a doc-update checklist, and this
changelog.

### Fixed: `UPDATE_REPO` pointed at a nonexistent repo
It read `supercrossed/esp32-planter`; the actual repo is
`supercrossed/ESP32-watering`. **Every OTA check would have failed with a
404.**

### Fixed: manifest written with a UTF-8 BOM
PowerShell's `-Encoding utf8` prepends `EF BB BF`, which MicroPython's
`json.loads()` rejects. Every OTA check would have failed to parse. The
build now writes BOM-free UTF-8.

### Fixed: `src/config.example.py` was missing the OTA settings
It was hand-maintained and had gone stale, so every fresh clone had OTA
silently disabled. It's now generated from `config.py` during the build,
with a hard abort if a real credential survives scrubbing.

---

## 2026-07-13

### Added: over-the-air updates
Daily check against the GitHub repo, one-click install from the dashboard's
Firmware card. SHA-256 verified, atomic (nothing swaps until every file
verifies), with `.bak` copies kept. `boot.py` restores them automatically
after 3 failed boots, so a bad update self-heals.

`config.py`, `wifi.json`, and `settings.json` are never updatable.

### Added: status LED
The onboard D2 LED (GPIO 2): solid = WiFi + web server up, fast blink = WiFi
down, slow blink = web server down.

### Added: `WEB_DEBUG`
Prints one console line per HTTP request - the definitive test for whether
packets are reaching the device.

### Fixed: captive rescue hotspot was unjoinable
Three separate bugs. `ap.config()` ran *after* `ap.active(True)`, which
restarts the AP without reliably restarting its DHCP server - phones
associated but never got an IP ("Unable to join network" on iOS). The open
AP set `authmode` without an empty `password`, which can leave a keyless
WPA2 network. And the station interface kept scanning for the dead router,
dragging the shared radio off-channel mid-handshake.

### Fixed: web server crash took down the controller
`web.init()` raised `OSError: -203` from `getaddrinfo` and killed `main.py`
entirely - no watering, no web server. The bind now uses a numeric address
(no DNS resolution), failures are caught, and the main loop retries every
30s.

### Changed: modules ship as pre-compiled `.mpy`
On-device compilation left **764 bytes** of ESP-IDF C heap, starving the
WiFi stack while `gc.mem_free()` looked healthy. Building with
`build_mpy.ps1` moved that cost to build time; the same board now idles at
~33KB free. Both heap figures are printed at boot and exposed in
`/api/status`.

### Added: sensor failure backoff
Three consecutive failed moisture reads back the interval off from 15s to 5
minutes, recovering on the next good read. Failing I2C transactions churn
the same C heap the WiFi stack needs.

---

## Earlier

- Soak-and-recheck watering sessions with per-zone wet targets (hysteresis)
- Multiple ADS1115 boards on one I2C bus with global channel numbering
- Multi-valve support: zones map to one or more valves, sequential runs,
  per-valve cooldowns
- Runtime WiFi rescue hotspot (AP+STA) reachable at 192.168.4.1
- AHT20/BMP280 environment sensors, auto-detected; LM393 rain sensor
- Config export/import for kit presets
- Weather card, dark mode, moisture history chart
- Hardware watchdog, NTP retry, per-subsystem exception isolation
