# Handoff: the web listener stall

[<- Docs index](README.md)

**Copy this whole file into a new session (with me or any other assistant)
to pick this up without re-deriving it.** Everything below was measured on
real hardware on 2026-09-06; nothing is inferred.

---

## The symptom

The ESP32 planter controller stops serving its web dashboard for tens of
seconds to minutes at a time, then recovers on its own. While it is
stalled the device is **otherwise completely healthy**:

- main loop running (it keeps feeding the watchdog)
- 8 MB of heap free
- WiFi associated, and the board answers ICMP pings
- valves, scheduling and watering all unaffected

Probing port 80 during a stall gives either `ECONNREFUSED` or a TCP
timeout depending on the moment. Sometimes the watchdog fires
(`Reset cause: WATCHDOG - the main loop hung`) and the board reboots,
which restores service for a while.

## THE HEADLINE: this is not the application firmware

`tools/minimal_server_repro.py` is a ~40 line MicroPython HTTP server
containing **none** of this project's code — no watchdog, no I2C, no
status LED, no flash writes, no settings, no gzip, no streaming, no
keep-alive, no asyncio. Just `accept`, `recv`, write a fixed body, `close`.

**It reproduces the stall exactly.** 332 requests served / 352 failed over
7 minutes of page-load-shaped traffic; after the load stopped entirely it
timed out four times in a row and then answered in 0.02 s.

So the fault lives in MicroPython/lwIP on this platform. **Do not attempt
to fix it by rewriting `web.py` or `main.py`** — two complete server
architectures have already been tried (see below).

### How to run the reproduction

Hold the board at the REPL so `main.py` never starts (send Ctrl-C through
the boot window — this also means no watchdog is armed), then:

```
mpremote connect COM6 run tools/minimal_server_repro.py
```

and drive it with parallel page-load-shaped requests. It reads WiFi
credentials from the device's own `wifi.json`, so none appear in the file.

## What has already been tried, and did NOT help

All measured on hardware:

| Change | Result |
|---|---|
| Blocking inline server → asyncio, one task per connection | still stalls |
| ESP32 (33 KB C heap) → ESP32-S3 (8 MB, **260×** more) | still stalls |
| 11 sockets per page load → HTTP keep-alive, 26 requests per socket | still stalls |
| Rebuilding the listening socket when `accept()` fails | **does not restore service** |
| Load shedding (503) below a C-heap floor | no effect on the stall |

## Ruled out by measurement — do not re-investigate

| Suspected cause | Measurement that killed it |
|---|---|
| Memory exhaustion | 8 MB C heap free at the moment of failure |
| `gc.collect()` on an 8 MB PSRAM heap | 0–20 ms |
| Filesystem full / corrupt | 2% used, 14 MB free |
| I2C timeouts leaking C heap | 100 failed reads moved `idf_free` **up** (89756 → 90180) |
| WS2812 status LED / RMT allocator | 300 writes + 200 raw `bitstream` calls: zero change |
| GC heap growing into the C heap | when degraded, GC total was 576 bytes *smaller*, while the C heap had lost 23300 |
| Connection churn / TIME_WAIT | keep-alive verified working (26 req/socket), still stalls |
| Clients hanging up mid-response | removing them changed nothing (4 reboots either way) |

## What is NOT yet established

Be careful here — an earlier version of this document overstated it.

- **The trigger is not understood.** It is intermittent. A same-device A/B
  of "poll every 5 s only" versus "poll + full page refresh every 30 s",
  3 minutes each, gave 8 failures/1 reboot for poll-only and 1 failure/1
  reboot for poll+refresh. **Refreshes are not a reliable trigger** and it
  occurs under light load too.
- Frequency varies a lot between runs. One 5-minute poll-only run was
  completely clean (29/29, no reboots); another gave a reboot every
  ~3 minutes.

## Two real bugs in this repo that are still worth fixing

These are genuine and independent of the platform fault, but a fix for
them was **written and reverted** (it regressed a device that had been
stable: 29/29 requests and 0 reboots → 16 ok, 6 fail, 2 reboots). The code
is preserved on the `wip/asyncio-server` branch.

1. `poll_once` does `except OSError: return` around `accept()`, so a
   genuinely broken listening socket is indistinguishable from an idle one
   (`EAGAIN`). A dead listener therefore goes unnoticed forever.
2. `main.py` checks `web._server_sock is None` before retrying
   `start_server()`. That stays `False` because the socket object outlives
   its usefulness, so the existing 30-second retry never fires.

**Important constraint if you touch this:** `settimeout()` does not bound
socket calls on this port — measured, a single 1024-byte `send()` blocked
for **30 seconds** on a socket with a 3-second timeout. Anything added to
the main loop must not block; a blocking `connect()` added there is the
prime suspect for why the reverted fix regressed things.

## Hardware / environment

- **Board:** ESP32-S3-WROOM-1 N16R8 (16 MB flash, 8 MB octal PSRAM), on
  **COM6**. (`COM9` is a different, unrelated project — do not touch it.)
- **Firmware:** MicroPython v1.28.0, `ESP32_GENERIC_S3-SPIRAM_OCT`
- **Pins on this build:** I2C SDA 8, SCL 9; valve 1 on GPIO 4; WS2812
  status LED on GPIO 48. GPIO **33–37 are consumed by the octal PSRAM**
  even though the pinout card shows them free. Verified usable:
  1, 2, 4–18, 21, 38–42, 47, 48.
- The repo otherwise targets ESP32-WROOM-32; the S3 config is device-local.

## Suggested next step

Reproduce it on a **stock MicroPython build with no application code at
all** (the minimal server is already close to this) and raise it upstream
with MicroPython/ESP-IDF, rather than continuing to move it around inside
this repo.

## Practical impact today

Watering is never at risk: valves are driven closed at boot, the hardware
watchdog reboots a hung loop, and the optional nightly reboot is a further
backstop. The cost is dashboard availability — it can be unreachable for
tens of seconds to a few minutes, and recovers unattended.
