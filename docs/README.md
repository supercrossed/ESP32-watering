# ESP32 Planter - Documentation

Everything beyond the quick start on the [main page](../README.md).

## Guides

| Guide | Covers |
|---|---|
| **[Hardware & wiring](hardware.md)** | Parts list, ESP32 pin safety, I2C bus and ADS1115 addressing, wiring diagrams for valves and sensors |
| **[Setup & calibration](setup.md)** | First boot, WiFi (including the captive portal), adding zones and schedules, calibrating a moisture sensor |
| **[Watering logic](watering.md)** | How the planter decides to water: thresholds, hysteresis, soak-and-recheck, cooldowns, schedules, and the safety layers |
| **[The dashboard](dashboard.md)** | Every card and what it controls |
| **[OTA updates](ota-updates.md)** | How self-updating works, publishing an update, rollback, TLS notes |
| **[API reference](api.md)** | Every HTTP endpoint with parameters |
| **[Development](development.md)** | Architecture, the two-heap memory problem, coding constraints, testing |
| **[Troubleshooting](troubleshooting.md)** | Symptoms, causes, fixes |

## Quick answers

**Why won't my valve open?** Many IRF520 modules don't switch fully at 3.3V
gate drive. See [troubleshooting](troubleshooting.md#valve-wont-open).

**Where do moisture sensors plug in?** Into the ADS1115's A0-A3 analog
inputs, *not* ESP32 GPIO. See [hardware](hardware.md#moisture-sensors).

**How do I add a second ADS1115?** Same two I2C pins - it's a bus. Change the
board's ADDR pin. See [hardware](hardware.md#multiple-ads1115-boards).

**How often does it water?** Only when a zone reads dry, subject to
cooldowns, plus any schedules. See [watering](watering.md).

**Board reboots every 2 minutes at the REPL.** The watchdog. Set
`WATCHDOG_TIMEOUT_SEC = 0` while developing. See
[troubleshooting](troubleshooting.md#board-boot-loops).
