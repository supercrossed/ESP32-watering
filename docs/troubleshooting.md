# Troubleshooting

[<- Docs index](README.md)

## Status LED

The onboard LED (GPIO 2) is the fastest diagnostic. Boards ship with one
of two kinds and the firmware auto-detects which.

**RGB (WS2812)** - common on WROOM-32UE and newer devkits:

| Colour | Meaning |
|---|---|
| **Dim green** | All good - WiFi and web server up |
| **Breathing blue** | A valve is open, water is flowing |
| **Purple** | An OTA update is running |
| **Dim amber (steady)** | Just booted - watering held while sensors settle |
| **Amber blink** | WiFi down, reconnecting. Rescue hotspot opens after 5 min |
| **Red blink** | WiFi up but the web server isn't listening - retries every 30s |
| **Off** | Not running (crashed at import, or LED disabled) |

**Plain LED** - the classic blue "D2":

| LED | Meaning |
|---|---|
| **Solid** | WiFi connected and web server up - all good |
| **Fast blink** (~2.5Hz) | WiFi down, reconnecting |
| **Slow blink** (~0.5Hz) | Web server down - it retries every 30s |
| **Off** | Not running |

If your board has an RGB LED that never lights, force it with
`STATUS_LED_TYPE = "rgb"` in `src/config.py`. A WS2812 needs a timed data
protocol, so driving it as a plain on/off output does nothing at all.

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

## WiFi drops after sensors are connected

**Symptom:** the planter is stable until moisture sensors are wired in;
after that WiFi drops out, the dashboard becomes unreachable, or the board
reboots on its own.

**Cause:** the ESP32 runs I2C, WiFi and the watering logic from one main
loop. If a sensor holds the SDA line low - realistic with capacitive probes
on long unshielded garden runs, a marginal connector, or a solenoid
switching nearby - the I2C transaction stalls. The loop stalls with it, so
nothing feeds the WiFi stack or the watchdog, and the connection dies.

**Fixed in current versions.** Four changes, all in place:

- The I2C bus is created with an explicit **timeout** (`I2C_TIMEOUT_US`,
  50ms). A stuck transaction now raises `OSError` instead of blocking.
- Bus speed defaults to **100kHz** (`I2C_FREQ`), which tolerates long runs
  far better than 400kHz.
- The ADS1115 driver **retries once** on a transient error before failing.
- After 3 consecutive failures the device **rebuilds the I2C peripheral**
  entirely - which clears a slave latching SDA low - and only falls back to
  the 5-minute interval if that doesn't help. Look for
  `I2C bus reinitialised` in the event log.

**If it still happens, the wiring is the next place to look:**

- **Pull-up resistors.** Most ADS1115 breakouts include 10k pull-ups. With
  several boards on one bus those parallel down too far; remove the
  pull-ups from all but one board.
- **Run length.** Over ~1m of unshielded cable, I2C gets unreliable. Use
  shielded or twisted cable, and keep it away from the solenoid wiring.
- **Shared ground.** Sensors, ADS1115, and ESP32 all need a solid common
  ground - a marginal one shows up as intermittent I2C failures.
- **Power.** Capacitive probes on the 3.3V rail alongside WiFi can brown
  out the rail during transmit peaks. If failures correlate with WiFi
  activity, try powering the probes separately.

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

## `import main` at the REPL breaks networking

Running `import main` in Thonny to start the controller **will not work
properly**. The REPL session itself consumes the ESP-IDF C heap, so by the
time the app starts there's nothing left for the network stack:

```
IDF C-heap free before WiFi: 936 largest block: 512   <- should be ~138000
...
web handler error: [Errno 116] ETIMEDOUT
```

WiFi associates and gets an IP, but the device can't allocate enough to
answer an HTTP request. **Press the EN/RST button for a real boot instead.**

`import main` is only useful for surfacing import-time errors. If the
console stays blank after pressing EN, click Thonny's Stop/Restart once to
re-attach - the board was probably booting fine and you weren't seeing it.

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

## Valve closed early, or a nonsense "open 1800000000s" in the log

Fixed in current versions. The safety cutoff used the wall clock, which an
NTP sync moves - the first sync of a boot jumps it ~26 years from the 2000
epoch. A valve open across that jump was force-closed immediately and the
event log recorded an absurd duration.

The more serious half of the same bug: if the clock ever moved *backward*,
elapsed time went negative and **the cutoff never fired at all**. The cutoff
now uses the monotonic `ticks_ms()` clock, which no clock change affects.

---

## Dashboard unreachable but the device says WiFi is connected

This is a **zombie connection**: the radio is associated with the access
point, so `isconnected()` and the status card both read healthy, but no
packets actually reach the router. Causes include an expired DHCP lease, a
router that rebooted and cleared its NAT table, or a wedged lwIP state on
the ESP32.

**Handled automatically in current versions.** Every 15 minutes the planter
opens a TCP connection to its gateway to prove packets move. On repeated
failure it reconnects, then resets the interface entirely. Watch the event
log for:

```
[EVENT] wifi gateway unreachable though associated - watching
[EVENT] wifi gateway still unreachable - reconnecting
[EVENT] wifi gateway unreachable x3 - resetting the interface
```

The dashboard's WiFi row shows `connected but the router is not responding`
while this is happening.

Tuning lives in `src/config.py`: `WIFI_HEALTH_CHECK_SEC` (0 disables),
`WIFI_HEALTH_TIMEOUT_SEC`.

**Note the probe targets the gateway, not the internet.** An ISP outage
does not drop a working local dashboard - see
[watering.md](watering.md#at-power-on) for the general principle that the
planter's core functions never depend on the internet.

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
