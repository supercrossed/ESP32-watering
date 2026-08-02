# Troubleshooting

[<- Docs index](README.md)

## Status LED

The onboard blue "D2" LED (GPIO 2) is the fastest diagnostic:

| LED | Meaning |
|---|---|
| **Solid** | WiFi connected and web server up - all good |
| **Fast blink** (~2.5Hz) | WiFi down, reconnecting. Rescue hotspot opens after 5 min |
| **Slow blink** (~0.5Hz) | WiFi up but the web server isn't listening - it retries every 30s |
| **Off** | Not running (crashed at import, or LED disabled) |

---

## Valve won't open

**Symptom:** the MOSFET module's LED lights when you trigger a valve, but the
solenoid doesn't click - or clicks weakly. Measuring the output shows only a
volt or two instead of 12V.

**Cause:** many **IRF520** modules sold for Arduino are not logic-level
MOSFETs. At 3.3V gate drive they only partially turn on. With no load the
output can look plausible; under the solenoid's real current draw the voltage
collapses.

**Fix:** use a **D4184** or **XY-MOS** module, which switches fully at 3.3V.

**Also check:**
- ESP32 GND and the 12V supply GND are connected (shared ground is required)
- The valve's polarity, if it's a DC solenoid
- The valve pin is output-capable - GPIO 34-39 are input-only
- 12V supply can deliver the solenoid's current (~500mA typical)

---

## `[Errno 19] ENODEV` on moisture reads

The ADS1115 isn't responding on the I2C bus.

1. Check SDA/SCL wiring (defaults: GPIO 21 = SDA, 22 = SCL)
2. Check the board has 3.3V and GND
3. Click **Scan Bus** in the dashboard - it lists addresses that answer
4. Confirm the address matches what's configured (ADDR pin: GND=0x48,
   VDD=0x49, SDA=0x4A, SCL=0x4B)

After 3 consecutive failures the read interval backs off from 15s to 5
minutes automatically, and logs an event. This is intentional: each failing
I2C transaction churns the same memory pool the WiFi stack needs. It
recovers on the next good read.

---

## Dashboard unreachable

**First, check the LED.** Fast blink means WiFi is down - that's a different
problem. Solid means the server is up and the issue is network-side.

**Turn on request logging.** Set `WEB_DEBUG = True` in `src/config.py`. Every
HTTP request then prints to the console:

```
web: GET /
web: GET /api/status
```

Browse to the planter while watching the console:

- **Lines appear** -> packets are arriving; the problem is in the response
  path or the browser
- **Silence** -> packets never reach the device; it's a network problem

**Check the gateway.** The boot line prints it:

```
WiFi connected, IP: 192.168.1.144 mask: 255.255.255.0 gw: 192.168.1.1 ...
```

If that gateway differs from your computer's (`ipconfig` / `ip route`), the
ESP32 joined a **different access point** - an extender or mesh node - that
may isolate clients from the main LAN. This is a common cause of "the device
is online but nothing can reach it."

**Other causes:**
- Your PC on 5GHz, the ESP32 on 2.4GHz, with band isolation enabled on the router
- Guest/IoT SSID with client isolation
- DHCP handed out a new IP (reconnect events log the current one)
- Stale ARP entry - `arp -d *` in an admin shell

---

## Network dies after hours of uptime

**Symptom:** works fine, then hours later the dashboard partially loads and
WiFi drops. `gc.mem_free()` looks healthy.

**Cause:** the ESP-IDF C heap - separate from MicroPython's heap - is
exhausted. Watch the once-a-minute console line:

```
IDF C-heap free: 33548 largest block: 32768 | GC free: 94704
```

If `IDF C-heap free` is a few hundred bytes, the network stack has no room to
work. Typical errors:

```
E (35649) wifi:fail to alloc timer, type=1
E (41079) i2c: i2c command link malloc error
OSError: -203
```

**Fix:** make sure you're running `.mpy` files, not `.py`. Compiling modules
on the device is what consumes that heap. See
[development.md](development.md#the-two-heap-problem).

---

## Board boot-loops

**At the REPL:** the hardware watchdog reboots the board every
`WATCHDOG_TIMEOUT_SEC` (120s default) because the main loop isn't feeding it.
Set `WATCHDOG_TIMEOUT_SEC = 0` in `src/config.py` while developing with Thonny.

**After an update:** `boot.py` restores the previous build automatically after
3 failed boots. Watch for:

```
boot: 3 consecutive unstable boots - rolling back last update
boot: restored web.mpy, state.mpy
```

**After a manual upload:** manual uploads have no rollback. Reconnect via USB
in Thonny and re-upload a known-good file. The board isn't bricked - it's
failing at import.

---

## Wrong uptime (e.g. 232557h)

Fixed in current versions. The boot timestamp was captured before NTP sync,
in the year-2000 epoch, then compared against real time. It's now rebased
across the clock jump. Update if you still see it.

---

## It waters as soon as it powers on

Fixed in current versions - update if you see this.

Two causes, both addressed. Moisture watering now waits
`STARTUP_GRACE_SEC` (60s default) after boot so the sensors can settle;
before that, a single reading taken microseconds after power-on could open
a valve. And the per-valve cooldowns are now persisted to
`watering_state.json` and restored at boot - previously they lived only in
RAM, so a power cut erased all memory of recent watering and the planter
would water again immediately regardless of soil moisture.

During the grace window the System Status card shows
`Startup: settling - watering held for Ns`.

If it still waters at power-on after updating, check that the zone really
is below its threshold in the Moisture Readings card - a mis-calibrated
sensor reading permanently dry will water whenever the cooldowns allow.
See [setup.md](setup.md#calibrating-a-moisture-sensor).

---

## Schedules never fire

Schedules are held until NTP sync succeeds - check for `Time synced via NTP`
in the console, or the System Status card's Clock row. Without internet the
clock never syncs and schedules stay paused. **Moisture watering still
works.**

Also check:
- The schedule is enabled
- `daily_enabled` is on in Watering Settings
- The UTC offset (`tz_offset_min`) is right for your timezone - there's no
  DST automation
- A valve isn't already open or queued at that minute

---

## Zone added in the UI never gets read

Fixed in current versions - zones present in `zone_channels` but absent from
`config.ZONES` are synthesized with default calibration. If you're on an old
build, update.

---

## `incompatible .mpy file`

The bytecode version doesn't match your firmware. Update the compiler and
rebuild:

```powershell
pip install --upgrade mpy-cross
.\build_mpy.ps1
```

---

## Upload fails / "Failed to fetch"

Large multi-file uploads through the dashboard are streamed to flash, but a
flaky WiFi connection can still drop mid-transfer. Retry with fewer files, or
use Thonny over USB.

If a `.py` and `.mpy` of the same module both exist on the device, the `.py`
wins and shadows the compiled version. The uploader deletes the counterpart
automatically, but if you copied files manually, check for stale twins.

---

## Getting more detail

- `WEB_DEBUG = True` - one console line per HTTP request
- The **Recent Events** card and `events.log` on the device record watering,
  reboots, WiFi transitions, and update activity
- `/api/status` returns full system state including both heap figures
