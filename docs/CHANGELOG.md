# Changelog

[<- Docs index](README.md)

Notable changes, newest first. Add an entry for anything user-visible - see
[CONTRIBUTING.md](CONTRIBUTING.md#the-checklist).

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
