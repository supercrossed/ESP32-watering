# The Dashboard

[<- Docs index](README.md)

Served directly from the ESP32 at its IP address, or <http://planter.local>.
It's a single page, streamed from flash - no internet needed to load it
(the weather card is the one exception, and it fails gracefully offline).

The header carries a live clock and a **dark mode toggle** (remembered
between visits).

---

## Cards

### Weather
Local forecast, fetched by *your browser* (not the ESP32), so it costs the
device nothing. Set a ZIP code, or let it locate you by IP. Click a day to
expand an hourly breakdown.

### System Status
The at-a-glance card. Plant art reflects overall state (happy / dry /
watering / just watered), alongside:

- Per-zone moisture with thresholds
- Per-valve state: open/closed, how long, last run and why
- Uptime, clock sync status, WiFi state
- `Startup: settling...` for the first minute after a boot, while
  moisture watering is held ([why](watering.md#at-power-on))
- CPU load and memory - including the ESP-IDF C heap
  ([why that matters](development.md#the-two-heap-problem))

### Zone Controls
Water any single zone on demand - runs that zone's mapped valves
sequentially for its configured duration.

### Valve Controls
Per valve: open/close toggle, a 30-second quick-water button, and a master
"water all" that runs every valve in sequence.

### Moisture Readings / Moisture History
Live values per zone, and a 3-hour chart (one point per minute, 180 points).
Hover for exact values.

### Environment
Temperature, humidity, pressure, and rain-sensor state - shown only if those
sensors are detected. Temperature in both F and C, pressure in inHg and hPa.

### Watering Zones
Where zones are defined:

| Field | Meaning |
|---|---|
| Name | Your label for a physical spot |
| Channel | Global ADS1115 channel (board 1 = 0-3, board 2 = 4-7...) |
| Dry below | Trigger threshold |
| Water until | Stop target ([hysteresis](watering.md#hysteresis-dry-below-vs-water-until)) |
| Water for | Run time for this zone |
| Valves | Which valve(s) this zone opens |

Each zone shows its current calibration and a **Calibrate** button that
walks through capturing dry and saturated soil readings - see
[setup.md](setup.md#calibrating-a-moisture-sensor).

Changes apply live - no reboot.

### Watering Settings
Default run time, minimum interval between moisture triggers, post-schedule
lockout, soak wait, max water cycles, and UTC offset. See
[watering logic](watering.md#settings-reference).

### Schedules
Daily runs. Tap the time chip for a phone-style time picker; select any
combination of valves and zones. Schedules don't fire until the clock is
NTP-synced.

### GPIO Pin Map
Full GPIO 0-39 table with each pin's role and safety status (flash pins,
UART, strapping pins, input-only). Click a pin to assign it as a valve, flow
meter, rain sensor, or I2C line. Includes the ADS1115 board manager and a
**Scan Bus** button that lists which I2C addresses actually respond.

### WiFi
Shows the saved network (never the password) and lets you switch networks
without reflashing. Saving reboots the device.

### Config Backup
Download the entire configuration - zones, valves, pin map, schedules,
settings, everything except WiFi - as a JSON file, or restore one. Useful for
backups and for presetting a kit before shipping it.

### Update Code
Drag-and-drop `.mpy`, `.py`, or `.html` files straight onto the device, no
Thonny needed. Uploading a module deletes its `.py`/`.mpy` counterpart so a
stale copy can't shadow the new one. Reboot to apply.

### Firmware
Installed version, when it last checked the repo, when it last updated, and
buttons to **Check for Updates** / **Update Now**. See
[OTA updates](ota-updates.md).

---

## Layout

Two columns on screens wider than 920px, single column on phones. The
weather and status cards span the full width. Everything is touch-friendly -
the dashboard is meant to be usable from a phone standing in the garden.
