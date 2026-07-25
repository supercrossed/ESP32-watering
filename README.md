# ESP32 Planter

**A self-contained automated plant-watering controller.** Soil moisture
sensors decide when to water, solenoid valves do the watering, and the ESP32
serves its own web dashboard on your network. No cloud account, no hub, no
app.

<!-- Add a dashboard screenshot here: docs/images/dashboard.png -->

- **Waters when the soil is dry**, not on a blind timer - per-zone
  thresholds with a "water until" target, then a soak-and-recheck cycle.
- **Multiple zones and valves**, run sequentially so supply pressure holds up.
- **Daily schedules** alongside moisture watering.
- **Keeps watering when WiFi drops** - and opens its own rescue hotspot so
  you can still reach the dashboard.
- **Updates itself** from this repo, with automatic rollback if a build
  fails to boot.

**[Full documentation ->](docs/)** &nbsp;|&nbsp;
[Wiring](docs/hardware.md) &nbsp;|&nbsp;
[Watering logic](docs/watering.md) &nbsp;|&nbsp;
[OTA updates](docs/ota-updates.md) &nbsp;|&nbsp;
[Troubleshooting](docs/troubleshooting.md)

---

## What you need

| Part | Notes |
|---|---|
| ESP32-WROOM-32 devkit | 38-pin, flashed with MicroPython 1.23+ |
| ADS1115 ADC board | Moisture sensors are analog - they do **not** go on GPIO |
| Capacitive moisture sensors | One per zone |
| 12V solenoid valve(s) + MOSFET module(s) | One MOSFET per valve |
| 1N4007 diode per valve | Flyback protection - **required** |
| 12V power supply | Sized for your valves |

Optional: AHT20/BMP280 environment sensor (auto-detected), LM393 rain sensor.

Full parts list, pin-safety table, and wiring diagrams:
**[docs/hardware.md](docs/hardware.md)**

---

## Install

**1. Flash MicroPython** ([firmware](https://micropython.org/download/ESP32_GENERIC/)):

```bash
pip install esptool
esptool.py --chip esp32 --port COM3 erase_flash
esptool.py --chip esp32 --port COM3 --baud 460800 write_flash -z 0x1000 ESP32_GENERIC-*.bin
```

**2. Build:**

```bash
git clone https://github.com/supercrossed/ESP32-watering.git
cd ESP32-watering
cp config.example.py config.py
pip install mpy-cross
.\build_mpy.ps1
```

> Modules ship as pre-compiled `.mpy` on purpose - compiling them on the
> device starves the memory the WiFi driver needs. See
> [docs/development.md](docs/development.md#the-two-heap-problem).

**3. Upload** the **contents of `build/`** to the device with
[Thonny](https://thonny.org/) (View -> Files, select all, Upload to /) or:

```bash
mpremote connect COM3 fs cp build/* :
```

Reset the board.

---

## First run

The planter prints its IP to the serial console - open it in a browser, or
try <http://planter.local>.

**No WiFi configured?** It opens a `Planter-Setup-xxxx` hotspot. Join it from
your phone, a setup page appears, pick your network. Done.

Then, in the dashboard:

1. **GPIO Pin Map** - assign your valve pin(s), confirm I2C pins, hit
   **Scan Bus** to check your ADS1115 is detected.
2. **Watering Zones** - add a zone per sensor: ADS channel, which valve(s)
   it waters, dry threshold.
3. **Schedules** - optional daily runs.

Before leaving it unattended, open and close a valve from the dashboard and
confirm water actually flows and stops.

Sensor calibration takes two minutes and matters:
**[docs/setup.md#calibrating-a-moisture-sensor](docs/setup.md#calibrating-a-moisture-sensor)**

---

## Status LED

The onboard blue "D2" LED tells you where things stand at a glance:

| LED | Meaning |
|---|---|
| Solid | WiFi connected, dashboard up |
| Fast blink | WiFi down, reconnecting |
| Slow blink | WiFi up, web server down |

---

## Documentation

| Guide | Covers |
|---|---|
| [Hardware & wiring](docs/hardware.md) | Parts, pin safety, I2C addressing, wiring diagrams |
| [Setup & calibration](docs/setup.md) | First boot, zones, schedules, sensor calibration |
| [Watering logic](docs/watering.md) | Thresholds, hysteresis, soak-and-recheck, cooldowns, safety |
| [The dashboard](docs/dashboard.md) | Every card and what it does |
| [OTA updates](docs/ota-updates.md) | How updates work, publishing, rollback |
| [API reference](docs/api.md) | Every HTTP endpoint |
| [Development](docs/development.md) | Architecture, the two-heap problem, testing |
| [Troubleshooting](docs/troubleshooting.md) | Common failures and fixes |

---

## Safety

This controls water near electronics and, potentially, your house.

- Every valve needs its **1N4007 flyback diode**. Not optional.
- A hard cutoff force-closes any valve open past `MAX_VALVE_OPEN_SEC`,
  checked every loop.
- A hardware watchdog reboots the board if the loop hangs; **valves close on
  boot**, so a hang can't leave water running.
- The dashboard has **no authentication**. Anyone on your LAN can control it.
  Don't port-forward it.

---

## License

MIT - see [LICENSE](LICENSE).
