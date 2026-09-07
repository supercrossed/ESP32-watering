# Project overview (short version)

[<- Docs index](README.md)

**A one-page description of the whole project.** Paste it into any AI to
give it the shape of the thing quickly, or read it yourself to get
oriented.

If you are actually going to *build* it, read
[REBUILD-PROMPT.md](REBUILD-PROMPT.md) as well — it carries a section of
hardware constraints that are not guessable and that cause failures which
all look like "the WiFi is flaky."

---

## What it is

A **self-contained automated plant-watering controller**. An ESP32 running
MicroPython reads soil-moisture sensors, opens solenoid valves when the
soil is dry or when a schedule says so, and serves a single-page web
dashboard on the local network for monitoring and configuration.

It runs entirely on its own: no cloud, no phone app, no account. It is
built to be sold as a kit, so someone who cannot open a serial console has
to be able to set it up.

## The hardware

An ESP32 devkit. Moisture sensors are **analog**, so they go into an
ADS1115 ADC over I2C rather than into GPIO — up to four ADS boards share
one two-wire bus, four sensors each. Each solenoid valve gets one GPIO
driving a MOSFET module, plus a flyback diode across the solenoid.
Optional extras auto-detect on the same bus: a temperature/humidity/
pressure sensor, and a rain sensor.

## How it decides to water

Two independent triggers:

- **Schedules** — a list of times, each naming the valves and zones it
  waters. Held back until the clock is NTP-synced, so nothing fires at a
  nonsense time.
- **Moisture** — a zone reading below its threshold waters the valves
  mapped to it.

A **zone** is a physical spot in the garden where one sensor sits. Zones
map to valves, so one sensor can water several beds, and a valve can serve
several zones.

Moisture watering uses **hysteresis**: trigger when a zone drops below
"dry", keep watering (with a soak-and-recheck pause between rounds) until
it reaches "wet", capped at a few cycles so a broken sensor cannot flood
anything. Two cooldowns prevent nuisance watering — a lockout after a
scheduled run, and a minimum gap between moisture triggers — both tracked
per valve so one busy valve never blocks an unrelated one.

**Only one valve is open at a time.** Multiple valves run in sequence.
That is a deliberate choice about supply-line pressure, not a hardware
limit.

## How it stays safe

Safety is layered, because any single mechanism can fail:

- Valve pins are driven **closed as the very first thing at boot**, before
  WiFi — an unconfigured GPIO floats, and a floating MOSFET gate can
  partially conduct.
- A **hard cutoff** force-closes any valve open longer than a maximum,
  checked every loop, on a monotonic clock so an NTP jump cannot disable
  it.
- A **hardware watchdog** reboots the board if the main loop ever hangs —
  and valves close on boot, so a hang cannot leave water running.
- A **startup grace period** reads sensors immediately but refuses to
  water for the first minute, because a capacitive probe needs to settle.
- Cooldown timestamps **persist to flash**, so a power cut does not erase
  the memory of recent watering.

Watering must survive everything else failing: no WiFi, no dashboard, no
sensors, no clock.

## Software layout

Flat MicroPython files on the device, shipped as pre-compiled `.mpy`
except the boot entry and the config file:

- `main.py` — boot sequence and the main loop (~5 Hz): safety cutoff,
  watering, moisture checks, schedules, network health, web polling
- `web.py` — hand-rolled HTTP server and JSON API
- `index.html` — the whole dashboard, no framework, no build step
- `settings_store.py` — runtime settings, persisted and migrated
- `state.py` — shared state, capped history buffers, event log
- `wifi.py` / `wifi_setup.py` — connection, health, captive setup portal
- `moisture.py`, `ads1x15.py`, `valve.py`, `env_sensors.py` — hardware
- `updater.py` / `boot.py` — over-the-air updates and rollback protection

**Anything slow runs in the main loop, never in a web request** — OTA
checks, sensor calibration and I2C scans are queued by the HTTP handler
and performed by the loop, because blocking the server also delays the
valve safety cutoff.

## Configuration

Two layers. `config.py` holds **first-boot defaults only** — pins, zones,
valves, timings. After first boot, `settings.json` on the device is
authoritative, and that is what the dashboard edits. WiFi credentials live
separately in `wifi.json`.

`config.py` is gitignored because it holds real credentials; the repo
ships a scrubbed example generated at build time.

Everything a user would reasonably want to change is editable from the
dashboard without touching code: schedules, thresholds, run times, wet
targets, cooldowns, timezone, zone-to-valve mapping, valve pins, ADS
addresses, and sensor calibration.

## First run

Power it on. If it cannot join WiFi, it becomes an open access point
called `Planter-Setup-xxxx` and hijacks DNS so a phone opens the setup
page automatically. Enter WiFi credentials, it saves them and reboots.

If WiFi later drops for several minutes while running, the same hotspot
opens *alongside* normal operation so the dashboard stays reachable — and
closes itself when the real network returns. Watering never stops for any
of this.

## Updates

The device checks a manifest, compares file hashes, and downloads only
what changed. If a new build fails to run, `boot.py` counts the failed
boots and restores the previous version automatically — otherwise a bad
update means a laptop, a USB cable and a walk outside. Credentials and
settings are never touched by an update or a rollback.

## Status at a glance

An onboard LED reports state without a browser: solid/green for healthy,
amber for WiFi down, red for the web server down, blue while watering,
purple while updating. The dashboard adds an event log, moisture history
(3 hours live in RAM, 7 days on flash), memory and CPU figures, and signal
strength.

## Known limitation

The web dashboard intermittently stops responding for tens of seconds to a
few minutes and then recovers on its own. This has been traced to
MicroPython/lwIP on this hardware, not to this project's code — a
forty-line server containing none of it reproduces the same fault.
**Watering is unaffected**, and the device recovers unattended. Details in
[listener-stall-handoff.md](listener-stall-handoff.md).
