# Changelog

[<- Docs index](README.md)

Notable changes, newest first. Add an entry for anything user-visible - see
[CONTRIBUTING.md](CONTRIBUTING.md#the-checklist).

## 2026-09-06

A long debugging session against real hardware. The headline is that
"WiFi breaks when the sensor is plugged in" was never one bug - it was
five, each individually capable of taking the dashboard down, plus a
faulty ADS1115 module. Every claim below was measured on the device;
where a plausible theory turned out to be wrong, that is recorded too,
because the wrong turns cost hours and should not be repeated.

### Fixed: large JSON responses killed the network, not just the request
`/api/pinmap` builds ~6KB of JSON. `json.dumps()` produced a 6KB string and
`.encode()` a second copy, and allocations that size make MicroPython grow
its GC heap **out of the ESP-IDF C heap**, which it never gives back.
Measured mid-request: the C heap fell to **824 bytes free with a 304-byte
largest block** while `gc.mem_free()` still reported 54KB and everything
looked healthy.

lwIP takes its TCP buffers from that same C heap. With 304 bytes available
it could not allocate a segment, so `socket.send()` blocked forever - not on
the 6KB body but on the **92-byte header**, which is why the symptom was so
confusing. Small endpoints stayed under the threshold and worked, so the
dashboard half-loaded and the GPIO card was the one that hung.

Every response that grows with the user's configuration is now streamed
fragment by fragment (`_send_json` / `_json_fragments`): `/api/status`,
`/api/settings`, `/api/schedules`, `/api/valves`, `/api/zones`,
`/api/hardware`, `/api/events`, `/api/pinmap`, `/api/calibrate` and the
config export. Peak single allocation went from **5159 bytes to 145**,
verified against a fully populated kit config (4 ADS boards, 8 zones,
8 valves, 20 schedules).

Content-Length is measured in BYTES, not characters - `json.dumps()` emits
non-ASCII raw, so a zone named "Jardin" with an accent would otherwise
under-report and truncate the body, which the browser waits on forever.

### Fixed: a 1024-byte socket write stalls this hardware
An endpoint sweep was unambiguous: every response up to **947 bytes**
returned in under 0.7s, and every response from **1025 bytes** up failed.
Not memory - the C heap had 32560 free with a 29696-byte largest block at
the moment it stalled. The failing write was always the first 1024-byte
flush. All socket writes are now bounded to 512 bytes
(`_SEND_CHUNK`, `_JSON_FLUSH_BYTES`, `_SEND_FILE_CHUNK`). Throughput is
unaffected: the 6.2KB pin map serves in 0.30s either way.

### Fixed: `send()` ignores `settimeout()`, so one dead client froze everything
A single send blocked for **30 seconds** on a socket with a 3-second
timeout. The web server is single-threaded and polled from the main loop,
so that one stuck client froze the dashboard for everyone - and the valve
safety cutoff runs in that same loop. Sends are now non-blocking with an
explicit 4-second deadline and a spin cap, so an abandoned connection costs
a bounded amount of loop time instead of unbounded.

### Fixed: main.py sits on a compile cliff that breaks WiFi
`main.py` is the only module still compiled **on the device** at every boot,
and its code size decides how much C heap is left afterwards:

| main.py | C heap before WiFi | result |
|---|---|---|
| 805 statements | 79400 free / 53248 largest | boots, WiFi connects |
| 816 statements | 26148 free / 14336 largest | `OSError: WiFi Out of Memory` |

Eleven statements was the difference between a working device and one that
could not bring up its radio. Comments are free - a 3KB comment-only pad
changed the number not at all - so only executable code counts.
`build_zone_list` and `sample_zone_raw` moved into `moisture.py` (which
ships pre-compiled), taking main.py to 786 statements and giving it real
headroom for the first time. **Before adding code to main.py, check this.**

### Fixed: an absent sensor stalled the main loop every cycle
Reading an ADS1115 that is not on the bus is neither free nor fast: the I2C
timeout passed to `I2C()` is not honoured by this port, so each doomed read
blocked for **~0.8 seconds**. Three unreadable zones stretched the main loop
from a 15s cycle to **45s**, which timed out every dashboard request and
looked exactly like a network fault.

Worse, the existing backoff never engaged. `moisture.read_all()` isolates
per-zone errors so one bad probe cannot blind the rest - which means a
*total* failure returns an empty list rather than raising, and the caller's
recovery logic only watched for an exception. The device retried doomed
reads every 15 seconds forever.

Now `moisture.online_boards()` probes the bus (a scan costs ~30ms whether
anything answers or not) and no reads are issued when no board replies. It
logs why, re-checks every `ADS_PROBE_SEC` (60s), and picks the board up by
itself once the wiring is fixed. A total read failure now also raises, so
the backoff and `reinit_i2c()` actually run.

### Fixed: the dashboard was attacking the device
`initDashboard()` awaited its first three calls and then fired five more
without awaiting - and two of those used `Promise.all` internally - so
opening the page burst **6-7 simultaneous connections**. Measured, that
drops the C heap from ~31KB to **760 bytes** for the duration, and anything
arriving in that window fails: a second tab, a refresh, a background poll.
Between bursts it recovered to ~30KB, which is exactly why the fault looked
random and intermittent.

The same requests issued one at a time are stable indefinitely - 220
requests across 20 page loads with the heap flat. Every startup fetch is now
awaited, both `Promise.all` pairs are sequential, and the 5s/15s/60s timers
are staggered (they all divide into 60, so unstaggered they fired as one
burst every minute). The page is no slower: the constraint is
per-connection memory, not round trips.

### Fixed: WiFi modem power-save added 100-450ms to every request
MicroPython defaults the station to `PM_PERFORMANCE`, so the radio sleeps
between DTIM beacons and the AP buffers packets until it wakes. Ping to the
device averaged **173ms (30-441ms) with 0% packet loss** - which is why it
never looked like a fault. `PM_NONE` took it to **10ms**, and `/api/status`
from 2586ms to 128ms. Set `WIFI_POWER_SAVE = True` only on a battery build.

### Added: the dashboard is served gzip-compressed
`index.html` is ~101KB and is now pre-compressed at build time to ~29KB
(`build/index.html.gz`), served with `Content-Encoding: gzip` when the
client accepts it and falling back to the plain file otherwise.

### Added: load shedding when the C heap is genuinely exhausted
Below 2048 bytes of contiguous C heap the server answers a tiny
`503 Retry-After` instead of attempting a real response. An honest "busy"
beats a device that needs the reset button.

**The threshold matters.** It was first set to 8192, which turned out to sit
exactly at this hardware's degraded steady state (~7936), so it refused
nearly every request and showed "busy" instead of the dashboard - the guard
did more damage than the condition it guarded. Genuine collapse is at
200-800 bytes.

### Things that were measured and found NOT to be the cause
Recorded so nobody spends hours on them again:

- **I2C timeouts do not leak C heap.** 100 failing reads (200 timed-out
  transactions) moved `idf_free` from 89756 to 90180 - *up*.
- **The WS2812 status LED does not leak.** 300 writes plus 200 raw
  `machine.bitstream` calls: zero change, despite an `rmt: no mem for
  group` line in the log that looked incriminating.
- **The GC heap does not grow into the C heap under load.** When degraded,
  GC total was 143168 vs 143744 fresh - 576 bytes *smaller* - while the C
  heap had lost 23300. The missing memory is inside the WiFi/lwIP driver.
- **Recycling the listening socket recovers nothing.** It fires, main.py
  rebuilds the listener, and the C heap sits at exactly 9668 before and
  after. Removed; do not re-add without new evidence.

### Tried and REVERTED: listener self-healing
Recorded because the measurements are worth keeping and the approach
should not be re-attempted blind.

**The fault.** The device stops serving while otherwise perfectly healthy -
main loop cycling, watchdog fed, 8MB of heap free, answering pings.
Probing port 80 in that state gave **REFUSED** eight times in a row on one
occasion and **TIMEOUT** on another. Either way the dashboard is gone until
something reboots the board, and nothing does.

**Why nothing notices.** Two blind spots in `poll_once`:
`except OSError: return` treats *every* `accept()` failure as "nothing
waiting", so a broken listening socket is indistinguishable from an idle
one; and `main.py` checks `web._server_sock is None` before retrying, which
stays False because the socket object outlives its usefulness, so the
existing 30-second retry never fires. Both remain true today.

**What was tried:** telling `EAGAIN` apart from a real `accept()` error and
rebuilding the listener on a real one; an honest `server_running()`;
rebuilding after a long silence; and `listener_alive()`, which connects to
the device's own IP:80 to distinguish a wedged server from an idle one
(lwIP completes the handshake itself, so the probe succeeds even though the
server is single-threaded and never accepts it - verified on hardware, it
answers in 2ms).

**Why it was reverted.** A/B on the same hardware under a light load of one
request every 10 seconds for 5 minutes:

| build | result |
|---|---|
| without these changes | **29/29 served, 0 failures, 0 reboots**, uptime climbing steadily |
| with these changes | 16 served, 6 failures, **2 watchdog reboots** |

The changes made a device that was stable under that load unstable. The
cause was not identified; the reboots continued with the reboot escalation
disabled, so it is not the escalation itself.

**Two things worth carrying forward.**

- **Rebuilding the listening socket does not restore service** (measured).
  lwIP itself wedges, not just our socket, and only a reboot brings it
  back. Any future fix has to account for that.
- A blocking `connect()` was added inside the main loop. Socket timeouts
  are already proven unreliable on this port (`settimeout()` does not bound
  `send()` - measured, a 30 second block on a 3 second timeout), so a
  blocking probe there is a plausible way to hang the loop and is the first
  thing to rule out next time.

The work is preserved on the `wip/asyncio-server` branch alongside the
asyncio port, both clearly marked as not working.

### Known: the listener stalls, and it is NOT this firmware
Under page-load-shaped traffic the device stops accepting connections for
tens of seconds while staying otherwise healthy - main loop cycling,
watchdog fed, 8MB of heap free, answering pings - then recovers on its own.
Probing port 80 during a stall gives REFUSED or TIMEOUT depending on the
moment.

**This was chased through the entire firmware before being isolated.** For
the record, all measured on hardware, none of it helped:

| changed | result |
|---|---|
| blocking inline server -> asyncio, one task per connection | still stalls |
| ESP32 (33KB C heap) -> ESP32-S3 (8MB, 260x) | still stalls |
| 11 sockets per page -> keep-alive, 26 requests per socket | still stalls |
| rebuilding the listening socket when it fails | does not restore service |

Also ruled out by measurement: memory (8MB free at the moment of failure),
`gc.collect()` (0-20ms), the filesystem (2% used), I2C timeouts (100 failed
reads moved the C heap *up*), and the WS2812 LED (300 writes, no change).

**`tools/minimal_server_repro.py` settles it.** A ~40 line HTTP server with
none of the planter code - no watchdog, no I2C, no LED, no flash writes, no
settings, no gzip, no streaming, no keep-alive - reproduces the stall
exactly: 332 served / 352 failed over 7 minutes, and after the load stopped
it timed out four times then answered in 0.02s.

So the fault is in MicroPython/lwIP on this platform, not in this project.
Rewriting `web.py` or `main.py` will not fix it - two full server
architectures have already been tried.

What this means in practice: normal single-tab use is fine, and a stall
clears itself in tens of seconds. Two tabs, or repeated fast refreshes,
can provoke it. The watchdog and the nightly reboot both remain as
backstops, and valves close on boot, so watering is never at risk.

Next step is to reproduce it on a stock MicroPython build with no
application code at all and raise it upstream, rather than continuing to
move it around inside this repo.

### Hardware note: a faulty ADS1115 can take the whole board down
A module in this state was diagnosed live. Both I2C lines pinned low 100%
of the time, the USB serial port browning out mid-test, the ESP32 rebooting,
and the board warm to the touch. On a devkit the module is powered from the
ESP32's own 3.3V regulator, so a reversed VCC/GND conducts through the
chip's substrate diodes - clamping both signal lines and heating that
regulator. Check VCC and GND before SDA and SCL.

Useful discriminators, all available in software:
- `ENODEV` means the bus is healthy and nobody answered; `ETIMEDOUT` means
  something is holding the bus.
- A soft-I2C scan that "finds" 112 addresses is not 112 devices - it is SDA
  stuck low, read as an ACK at every address.
- Release a line and see whether it springs back: an ADS1115 breakout ties
  its pull-ups to its own VDD, so a line that stays low means no power, no
  ground, or a short - not a signalling problem.

## 2026-08-30

### Fixed: valve pins floated for the whole boot sequence
Until a GPIO is configured as an output it sits in the chip's power-on
state - a floating input. A MOSFET gate left floating can drift high enough
to partially conduct, so **a valve could sit partly open during boot**.

The window was not brief: the valve objects were built *after* WiFi
association, which takes up to 20 seconds - and if the setup portal opened
it blocked forever, leaving the pins floating indefinitely. With the nightly
reboot enabled this repeated every night.

Valve pins are now driven to their closed state as one of the first things
`main.py` does, before WiFi and before the portal. Pins come from
`settings.json` (where the web UI writes them) with `config.VALVES` as a
fallback, and active-low wiring is handled correctly. A malformed entry
doesn't stop the others being closed.

### Added: reset cause logged at boot
The console now says why the board last restarted - power-on, EN button,
watchdog, or software. This is the cheapest test of "is it browning out?":
a supply collapsing under WiFi transmit peaks reports differently from a
clean power-on, so the log answers the question directly instead of leaving
it to inference.

### Fixed: the I2C scan endpoint could freeze the web server
`i2c.scan()` probes 128 addresses. On a healthy bus that is milliseconds,
but on a wedged one each probe can burn the full 50ms I2C timeout - up to
several seconds of frozen web server and delayed valve safety checks, all
inside an HTTP handler. It is now queued and performed by the main loop,
the same pattern already used for OTA checks and calibration; the dashboard
polls for the result.

---

### Fixed: a wedged I2C bus survived reboots and was never cleared
The existing recovery rebuilt the I2C **peripheral**, which resets only the
ESP32's side of the bus. It did nothing about the actual lockup mode: a
slave left holding **SDA low**.

That happens whenever the master stops mid-transaction - a watchdog reboot,
the nightly maintenance reboot, or a brownout. The ADS1115 is left part-way
through clocking out a byte, waiting for clocks that never come, and it
clamps SDA. Every device on the bus is then wedged, the freshly-built
peripheral sees a busy bus, and **every transaction fails permanently until
the sensor loses power**. The reboot meant to fix things is what causes it.

The standard bus-clear is now implemented (`i2c_bus_recover`): bit-bang up
to 9 clock pulses - one byte plus ACK - to walk the stuck slave through
whatever it was sending until it releases SDA, then issue a manual STOP.

Crucially it runs **at boot, before the I2C object is created**, not only
during recovery - otherwise the bus is dead from the first transaction. It
also runs inside `reinit_i2c()` between the deinit and the rebuild. A
healthy bus (SDA already high) is left completely untouched.

If SDA is still low after 9 clocks the console says so, which points at
wiring or power rather than a stuck slave:

```
I2C: SDA held low, clocking the bus free...
I2C: bus still held low after 9 clocks - check wiring/power
```

### Added: WiFi signal strength (RSSI) logging
RSSI now appears in the once-a-minute console line and in the dashboard's
System Status card, because it distinguishes two faults that look identical
from outside: if signal *drops* when sensors are attached, the wiring is
coupling noise into the radio or detuning the antenna; if it stays strong
and WiFi still fails, the problem is power or memory, not RF.

---

## 2026-08-29

### Added: link health checks - detecting a "zombie" WiFi connection
`isconnected()` only reports that the radio is **associated** with an access
point. It says nothing about whether packets move. A router whose DHCP lease
expired, whose NAT table was cleared, or a wedged lwIP state on the device
all leave it reporting `True` while the dashboard is unreachable - the
failure where every status field reads healthy and nothing responds.

Every 15 minutes (`WIFI_HEALTH_CHECK_SEC`) the planter now opens a TCP
connection to its **gateway**. Any reply proves the path - including a
connection refusal, since a refusal is still a packet coming back. Only a
timeout counts as failure, and an unrecognised error is treated as
inconclusive rather than acted on: a false positive costs a working
connection, which is worse than missing one bad cycle.

On failure it escalates rather than thrashing:

| Consecutive failures | Action |
|---|---|
| 1 | Logged only - a blip is not a fault |
| 2 | Soft reconnect (disconnect + connect) |
| 3+ | Hard reset: drop the interface and re-activate it, clearing wedged lwIP state |

Still failing after that hands off to the existing rescue-hotspot path.

**The gateway is deliberately the probe target, not the internet.** The
dashboard only needs the LAN, so an ISP outage must not drop a working local
connection. NTP failure alone therefore never recycles WiFi - but if NTP has
been failing for 6+ hours *while the LAN is healthy*
(`NTP_STALE_RECYCLE_SEC`), one single reconnect is attempted in case
something upstream is stuck, and it does not repeat.

The check is skipped while a valve is open, during an OTA or a calibration,
and while the rescue hotspot owns the radio.

The dashboard's WiFi row now distinguishes the two states: "connected" vs
"connected but the router is not responding - reconnecting". New `lan_ok`
field in `/api/status`.

### Fixed: NTP never resynced after the first success
The retry was gated on `if not state.time_synced`, so once the clock was set
it never ran again. The ESP32's RTC drifts, so a planter left running for
months fired its schedules increasingly off the intended time. It now
resyncs hourly (`NTP_RESYNC_SEC`).

---

### Hardening sweep: hangs, crashes and WiFi stability
A systematic review of the codebase for the same class of bug as the I2C
stall. Six issues found and fixed:

- **The valve safety cutoff measured wall-clock time.** An NTP sync moves
  that clock - forward by ~26 years from the 2000 epoch at first sync. A
  forward jump force-closed a valve spuriously; a **backward** jump made
  the elapsed time negative, so the cutoff would never fire and water would
  run with the last safety net silently disabled. It now uses the monotonic
  `ticks_ms()` clock, which no clock change can affect.
- **The safety-cutoff loop was the one main-loop section not wrapped** in
  try/except, against the project's own stated invariant. An exception
  there would end `main.py` outright and leave an open valve open until the
  watchdog rebooted. It is now isolated per valve and fails closed.
- **Request bodies were buffered without a cap.** `Content-Length` is
  client-supplied, so a large or malformed value grew a bytes object toward
  it on a ~100KB heap - a direct route to memory exhaustion and the WiFi
  death that follows. Capped at 16KB (uploads stream to flash separately
  and are unaffected).
- **Uploads could hang the device with the watchdog disarmed.** The upload
  loop calls `_feed()` while reading, so a client trickling bytes against a
  huge `Content-Length` would keep the watchdog alive indefinitely. Uploads
  are now capped at 512KB with a 120s deadline.
- **Unguarded `int()` on query strings and headers.** `?duration=abc` or a
  malformed `Content-Length` raised out of the handler, dropping the
  connection with no response. All such conversions now fall back to a
  default and clamp.
- **A failing environment sensor was retried every cycle forever.** Each
  AHT20 attempt blocks 85ms and churns the same C heap the WiFi stack uses
  - the exact pattern the moisture backoff exists to prevent. Failures now
  back off, per chip, and recover automatically.

The captive portal's body reader had the same unbounded-read and
unguarded-`int()` bugs as the main server; both are fixed there too.

---

## 2026-08-25

### Fixed: WiFi dropping once moisture sensors are connected
The ESP32 services I2C, WiFi and watering from one loop. A sensor holding
SDA low - realistic on long garden runs or a marginal connector - stalled
the I2C transaction and therefore the loop, so nothing fed the WiFi stack.
The symptom was WiFi dying only after sensors were added.

Four fixes:

- The I2C bus is now created with an explicit **timeout**
  (`I2C_TIMEOUT_US`, 50ms). A stuck transaction raises `OSError` instead of
  blocking indefinitely.
- Bus speed is pinned to **100kHz** (`I2C_FREQ`) - far more tolerant of the
  long unshielded runs a planter uses than 400kHz.
- The ADS1115 driver **retries once** on a transient error.
- **A failing zone no longer blinds the others.** An exception mid-loop
  used to abort the whole cycle, so every zone after the bad one silently
  went unread. Each zone is now isolated.
- After 3 consecutive failures the device **rebuilds the I2C peripheral**
  (`reinit_i2c`), which clears a slave latching SDA low, before falling
  back to the slow interval.

Wiring guidance added to `docs/hardware.md` - notably that parallel
pull-ups from multiple ADS1115 breakouts are a common cause.

---

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
