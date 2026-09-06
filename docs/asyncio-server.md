# Porting the web server to asyncio

[<- Docs index](README.md)

**Status: planned, not started.** This is the fix for the one open
reliability bug. Everything needed to start is in this file.

## The bug, and why this is the fix

The web server hangs the main loop under **concurrent connections**, long
enough for the watchdog to reboot the board
(`Reset cause: WATCHDOG - the main loop hung`).

It was traced on hardware until it reproduced. The evidence rules out the
firmware's own logic:

| Ruled out | Evidence |
|---|---|
| Memory exhaustion | Reproduced on an ESP32-S3 with **8MB of C heap free** |
| One specific bad call | Two traced hangs stopped at *different* points - once after `close()` returned, once between two successful sends mid-response |
| Clients hanging up mid-response | Removing them changed nothing: still 4 watchdog reboots in 6 minutes |
| Something spinning in Python | Both CPUs report **IDLE** in the watchdog dump - the task is parked in a syscall |
| Our request volume | Strictly *sequential* requests never trigger it: 220 in a row, flat heap |

The common factor is **blocking sockets plus overlapping connections**.
`settimeout()` is already proven not to bound `send()` on this port -
measured, a single 1024-byte send blocked for **30 seconds** on a socket
with a 3-second timeout - and `_read_headers` still depends on that same
guarantee.

`asyncio` fixes this by construction rather than by working around it: it
never issues a blocking socket call, registering sockets with `select.poll`
and only reading or writing when the poll says they are ready. It is also
the heavily-exercised path on this port, where blocking-sockets-with-
timeouts evidently is not.

Verified present on ESP32-S3 firmware v1.28.0:

```python
import asyncio, select
asyncio.start_server      # -> True
asyncio.open_connection   # -> True
select.poll               # -> True
```

**Caveat, stated honestly:** asyncio sits on the same lwIP underneath. This
*should* fix it, but that is a prediction until `tools/stress_web.py`
passes. Do not close the bug on reasoning alone.

## Success is already defined

```
python tools/stress_web.py <device-ip> 6
```

Opens connections in **parallel**, the way a browser does on every page
load, and watches `uptime_sec` for a backwards jump (a reboot). Sequential
tests pass on broken firmware, which is exactly why this bug survived so
long undiagnosed.

- **Baseline (blocking sockets, ESP32-S3, 6 min): 4 watchdog reboots.**
- **Pass: zero reboots.**

Request failures are secondary. A device that refuses a request and stays
up is far better than one that reboots; only the reboot count decides.

## Scope

Change the transport, keep the logic. Roughly 300 of `web.py`'s ~1900 lines.

**Rewrite:** `start_server`, `poll_once`, `_handle`, `_read_headers`,
`_send_all`, `_send_file`, and the streaming helpers - to
`asyncio.start_server` with `await reader.readline()` /
`await writer.drain()`.

**Keep unchanged:** every route, `_status_payload`, `_pinmap_payload`,
`_zones_payload`, `_json_fragments`, settings handling, the uploader.
These are pure data and already stream in small fragments.

**`main.py`:** the main loop becomes a task -
`await asyncio.sleep_ms(...)` in place of the current sleep, with
`asyncio.run()` owning both it and the server.

## Rules that must survive the port

These are load-bearing; several were learned the hard way.

1. **The valve cutoff runs in the main loop.** It must still execute every
   ~200ms. One blocking call anywhere in a handler stalls every connection
   *and* the safety cutoff. This is the single biggest risk of the port -
   asyncio makes a stray blocking call worse, not better, because it stalls
   everything rather than one connection.
2. **Nothing slow in a handler.** OTA checks, calibration captures and I2C
   scans stay queued via `state.*_requested` and run in the loop. Unchanged
   by this work, and more important after it.
3. **Keep responses streamed.** `_send_json` / `_json_fragments` exist
   because one 6KB `json.dumps()` grew the GC heap out of the C heap and
   left lwIP unable to allocate a segment. Still true on the S3, where it
   is merely less immediately fatal.
4. **Feed the watchdog** during long uploads (`web.set_wdt`).
5. **Bound every length that comes off the network** - `Content-Length` is
   client-supplied. Caps in the current file must carry over.
6. **`main.py` size is load-bearing on WROOM-32** (805 statements boots,
   816 fails with `WiFi Out of Memory`). Adding to `main.py` needs a check;
   the S3 has no such limit, so if this port is S3-only, say so explicitly
   rather than silently breaking the ESP32 build.

## Free win once async

HTTP **keep-alive** becomes straightforward, cutting a page load from 11
connections to 1. The dashboard currently fetches sequentially on purpose
(see `initDashboard`) to avoid opening 6+ sockets at once; with keep-alive
plus a working async server, that constraint could be relaxed - but only
after the stress test passes, and re-measured afterwards.

## Open decision

The repo targets **ESP32-WROOM-32**; this bug was diagnosed on an
**ESP32-S3**. Whether the project supports both, or moves to the S3, is a
product decision that should be made *before* the port, because it decides
whether `main.py` must stay under the compile cliff and whether the pin map
in `web.py` (hardcoded WROOM-32 roles: flash on 6-11, input-only 34-39)
needs to become board-aware. See `CLAUDE.md`.
