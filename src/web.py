# web.py
# Lightweight synchronous HTTP server (no external deps) serving a
# dashboard page and a small JSON API. Designed to be polled from the
# main loop via poll_once() so it never blocks moisture checks or the
# daily schedule.
#
# The dashboard page itself lives in index.html on flash, not as a Python
# string constant - a ~34KB string literal needs one contiguous heap
# allocation to compile, which reliably fails with MemoryError on a
# fragmented ESP32 heap even when plenty of total free memory remains.
# GET / streams index.html straight off the filesystem in small chunks.

import socket
import select
import ujson as json
import time
import machine
import os
import gc

import state
import settings_store
import config
import wifi

INDEX_HTML_PATH = "index.html"
CRLF = "\r\n"

_server_sock = None
_accepted = 0                 # connections served since boot
_listener_restarts = 0        # times the listener had to be rebuilt
_last_accept_ms = None        # monotonic ticks of the last accepted connection

# EAGAIN / EWOULDBLOCK: "nothing pending right now", the normal idle case.
# Anything else from accept() means the listening socket is broken.
_EAGAIN = (11, 35)

# Rebuild the listener after this long with no connection accepted at all.
# A dashboard in use polls every 5s, so this never fires in normal service;
# it exists because the worst observed wedge never surfaced an error to
# react to - select() simply stopped reporting the socket readable and
# accept() was never called. Rebuilding is cheap and drops nothing when
# there is nothing to drop.
_LISTENER_IDLE_REBUILD_MS = 60000        # 1 minute
_valves = {}  # injected by main.py: dict of valve name -> Valve
_trigger_watering_cb = None  # injected by main.py: fn(valve_name, duration_sec, reason)
_trigger_valves_cb = None  # injected by main.py: fn(valve_names, duration_sec, reason) - sequential
_default_valve_name = None  # injected by main.py
_i2c = None  # injected by main.py - used by /api/i2c/scan
_wdt = None  # injected by main.py - fed during long socket loops
_update_cb = None  # injected by main.py: fn(install_bool) -> result dict


def set_update_cb(cb):
    global _update_cb
    _update_cb = cb


def set_wdt(wdt):
    global _wdt
    _wdt = wdt


def set_i2c(i2c):
    """Re-point /api/i2c/scan at a rebuilt bus. main.reinit_i2c() creates a
    new I2C object when the bus wedges; without this the scan endpoint would
    keep using the dead one."""
    global _i2c
    _i2c = i2c


def _feed():
    if _wdt is not None:
        _wdt.feed()


def init(valves, trigger_watering_cb, trigger_valves_cb, default_valve_name, i2c=None):
    global _server_sock, _valves, _trigger_watering_cb, _trigger_valves_cb, _default_valve_name, _i2c
    _valves = valves
    _trigger_watering_cb = trigger_watering_cb
    _trigger_valves_cb = trigger_valves_cb
    _default_valve_name = default_valve_name
    _i2c = i2c

    start_server()


def start_server():
    """Bind and listen on port 80. Separate from init() so main.py can
    retry it from the loop if it fails at boot (e.g. lwIP OSError -203) -
    a web server hiccup must never take down the watering controller.
    No getaddrinfo: it allocates lwIP DNS structures that can fail with
    EAI_MEMORY (-203), and bind() takes a numeric (ip, port) tuple directly."""
    global _server_sock
    if _server_sock is not None:
        return True
    gc.collect()
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 80))
        s.listen(3)
        s.setblocking(False)
    except OSError as e:
        try:
            s.close()
        except OSError:
            pass
        print("web server start failed:", e)
        return False
    _server_sock = s
    print("Web server listening on port 80")
    return True


def server_running():
    """Whether the device can currently accept connections.

    main.py calls this before retrying start_server(). It used to check
    `_server_sock is None`, which stayed False while every connection was
    being refused - so the retry never ran and the dashboard stayed dead
    until a reboot."""
    return _server_sock is not None


def server_stats():
    """(connections accepted, listener rebuilds) - surfaced in /api/status
    so a recurring fault is visible rather than inferred."""
    return _accepted, _listener_restarts


def seconds_since_accept():
    """How long since any client connected. Large means either nobody is
    browsing or the listener is wedged - listener_alive() tells them
    apart."""
    if _last_accept_ms is None:
        return 0
    return time.ticks_diff(time.ticks_ms(), _last_accept_ms) // 1000


def rebuild_listener(why):
    """Public wrapper so main.py can force a rebuild."""
    return _rebuild_listener(why)


def listener_alive(timeout=2):
    """Can this device open a TCP connection to its own web server?

    True  - the listening socket is healthy.
    False - it is not; nothing is completing handshakes.
    None  - cannot tell (no IP yet), never act on this.

    Works despite the server being single-threaded: lwIP performs the
    handshake itself, so the connection is established without poll_once
    ever accepting it. The probe socket is closed immediately; whatever it
    left behind is reaped by the normal accept path.

    Blocks for up to `timeout` seconds, so callers run it rarely and feed
    the watchdog around it."""
    try:
        import network
        ip = network.WLAN(network.STA_IF).ifconfig()[0]
    except Exception:
        return None
    if not ip or ip == "0.0.0.0":
        return None
    probe = None
    try:
        probe = socket.socket()
        probe.settimeout(timeout)
        probe.connect((ip, 80))
        return True
    except OSError:
        # Refused, reset or timed out - in every case nothing is serving.
        return False
    except Exception:
        return None
    finally:
        if probe is not None:
            try:
                probe.close()
            except OSError:
                pass


def _rebuild_listener(why, log=True):
    """Drop the listening socket so the next start_server() recreates it.

    `log=False` for the routine idle rebuild: a device nobody is browsing
    hits that path every minute, and writing an event each time would
    churn flash and bury the real entries. Genuine accept() failures are
    still logged."""
    global _server_sock, _listener_restarts, _last_accept_ms
    _listener_restarts += 1
    if log:
        print("web: rebuilding listener ({})".format(why))
        try:
            state.log_event("web", "listener rebuilt: {}".format(why))
        except Exception:
            pass
    elif _listener_restarts % 30 == 1:
        # occasional breadcrumb so a silent device is not a mystery
        print("web: listener refreshed after idle ({} so far)".format(
            _listener_restarts))
    try:
        if _server_sock is not None:
            _server_sock.close()
    except OSError:
        pass
    _server_sock = None
    _last_accept_ms = None
    return start_server()


# A LAN request completes in tens of milliseconds. Eight seconds was far
# too generous: a client that opened a connection and went away (a closed
# tab, a timed-out fetch) held the whole main loop for that long, and the
# dashboard's 5s polling then queued faster than the device could drain.
# Three seconds still leaves ample room to stream the ~100KB dashboard to a
# slow client, while bounding what one dead connection can cost.
# Bytes handed to a single send() call. One TCP segment's worth keeps
# each write small and predictable.
# Bytes per socket write. MEASURED, not chosen for tidiness: on this board
# a single send() of 1024 bytes blocks until the socket timeout fires,
# while 512 goes out in milliseconds. The endpoint sweep was unambiguous -
# every response up to 947 bytes returned in under 0.7s and every response
# from 1025 bytes up failed, because the first flush was the one 1024-byte
# write. It is not memory: the C heap had 32560 bytes free with a
# 29696-byte largest block at the moment it stalled.
#
# The signature is a write that cannot be queued in one piece - lwIP's send
# buffer is around a segment, so a 1024-byte write plus the 92-byte header
# already in flight does not fit, and the write waits rather than doing a
# partial one. 512 always fits. Throughput is unaffected (the 6.2KB pin map
# serves in 0.30s either way) because this bounds the write size, not the
# window.
_SEND_CHUNK = 512
_CONN_TIMEOUT_SEC = 3.0
# Requests handled per poll_once() call, and the wall-clock ceiling for the
# whole batch.
_MAX_CONNS_PER_POLL = 4
_POLL_BUDGET_MS = 1200


# Time spent waiting in select() is the system's idle time - everything
# else is work. main.py reads this every few seconds to compute the
# "CPU load" figure shown in the dashboard.
_idle_ms = 0


def take_idle_ms():
    global _idle_ms
    v = _idle_ms
    _idle_ms = 0
    return v


# Refuse work below this much contiguous ESP-IDF C heap (the heap lwIP
# takes its TCP buffers from - NOT the Python GC heap).
#
# Why this exists: with a single client the server is stable indefinitely -
# 220 requests across 20 full page loads with the heap flat. With TWO
# clients (a browser open while something else polls) connections overlap,
# and a client that gives up mid-response leaves lwIP retransmitting to
# nobody, holding its buffers for minutes. A few of those and memory is
# tight; tight memory makes responses slow; slow responses make more
# clients give up. That spiral does not recover on its own - measured, the
# C heap sat at 508 bytes free / 208 largest and was still dead 400 seconds
# after all traffic stopped, so the dashboard was gone until a reboot.
#
# Shedding load breaks the spiral. A 503 is a few dozen bytes and always
# fits, the loop stays fast, the stuck buffers time out, and the device
# recovers by itself. An honest "busy, retry" beats a device that needs
# the EN button.
#
# Normal operation sits at 18-31KB largest, and collapse was at 208 bytes,
# so this floor is far below healthy traffic and far above the spiral.
# MEASURED, and set low on purpose. An earlier value of 8192 was a
# mistake: under real browser use this device settles at ~7936 largest
# block, so that floor refused nearly every request and showed "busy"
# instead of the dashboard - the guard was doing more damage than the
# condition it guarded against. The device serves fine at 8KB; genuine
# collapse is at ~200-800 bytes, which is what this is for.
_MIN_SERVE_BLOCK = 2048

_shed_count = 0
# NOTE: recycling the listening socket was tried here and REMOVED. It
# fires, main.py rebuilds the listener - and the C heap stays exactly
# where it was (measured: 9668 free before and after). The lost memory is
# not held by our sockets, so closing them reclaims nothing. Do not
# re-add it without new evidence.


_last_heap_check = None
_last_heap_largest = 0


def _largest_c_block():
    """Largest contiguous ESP-IDF C-heap block, sampled at most once a
    second.

    Deliberately NOT read from state.idf_largest: main.py refreshes that
    only every 5 seconds, and it stops refreshing entirely while the loop
    is stalled - which is precisely when this guard has to fire. A stale
    healthy number would keep the door open through the collapse. Falls
    back to main.py's sample only if esp32.idf_heap_info is unavailable."""
    global _last_heap_check, _last_heap_largest
    now = time.ticks_ms()
    if (_last_heap_check is not None
            and time.ticks_diff(now, _last_heap_check) < 1000):
        return _last_heap_largest
    _last_heap_check = now
    try:
        import esp32
        largest = 0
        for region in esp32.idf_heap_info(esp32.HEAP_DATA):
            if region[2] > largest:
                largest = region[2]
        _last_heap_largest = largest
    except Exception:
        _last_heap_largest = getattr(state, "idf_largest", 0) or 0
    return _last_heap_largest


def _overloaded():
    """True when the C heap is too fragmented to serve a real response."""
    largest = _largest_c_block()
    return 0 < largest < _MIN_SERVE_BLOCK


def poll_once(timeout=0.2):
    """Service pending HTTP connections. Call frequently from the main loop.

    Handles up to _MAX_CONNS_PER_POLL requests, bounded by a wall-clock
    budget, instead of exactly one. Serving one per loop pass was a
    self-sustaining collapse: the dashboard polls every 5s, a stalled
    connection could occupy the loop for the whole socket timeout, and any
    request that arrived meanwhile queued behind it. Once the device fell
    behind it never caught up - the backlog grew, the page half-loaded, and
    more tabs made it worse. Draining lets a burst clear in one pass, while
    the budget keeps valve timing and the watchdog serviced."""
    global _idle_ms, _accepted, _last_accept_ms
    if _server_sock is None:
        return
    # Nothing has connected for a long time. On a device in use that is
    # impossible (the dashboard polls every 5s), so treat it as a wedged
    # listener and rebuild - this is the only handle we have on the failure
    # mode where accept() is never called at all.
    if _last_accept_ms is None:
        _last_accept_ms = time.ticks_ms()
    elif time.ticks_diff(time.ticks_ms(), _last_accept_ms) > _LISTENER_IDLE_REBUILD_MS:
        _rebuild_listener("idle", log=False)
        return
    t0 = time.ticks_ms()
    try:
        r, _, _ = select.select([_server_sock], [], [], timeout)
    except OSError:
        return
    finally:
        _idle_ms += time.ticks_diff(time.ticks_ms(), t0)
    if not r:
        return

    started = time.ticks_ms()
    for _ in range(_MAX_CONNS_PER_POLL):
        try:
            cl, addr = _server_sock.accept()
        except OSError as e:
            err = e.args[0] if e.args else None
            if err in _EAGAIN:
                return      # genuinely nothing waiting - the normal case
            # NOT the normal case: the listening socket itself is broken.
            # This used to be indistinguishable from "nothing waiting",
            # which is how a dead listener went unnoticed indefinitely.
            _rebuild_listener("accept failed: {}".format(e))
            return
        _accepted += 1
        _last_accept_ms = time.ticks_ms()
        try:
            # Shed rather than spiral: serving a full response with almost
            # no contiguous C heap is what turns a slow patch into a
            # permanent wedge.
            if _overloaded():
                _send_busy(cl)
            else:
                _handle(cl)
        except Exception as e:
            print("web handler error:", e)
        finally:
            try:
                cl.close()
            except OSError:
                pass
        # Never hold the loop longer than the budget: valve cutoffs and the
        # watchdog run out there.
        if time.ticks_diff(time.ticks_ms(), started) > _POLL_BUDGET_MS:
            return
        try:
            more, _, _ = select.select([_server_sock], [], [], 0)
        except OSError:
            return
        if not more:
            return



def _handle(cl):
    cl.settimeout(_CONN_TIMEOUT_SEC)
    header_part, leftover = _read_headers(cl)
    if not header_part:
        return
    # Cheap enough to do on every request, and it decides whether the 101KB
    # dashboard goes out compressed - which is what keeps the lwIP buffers
    # inside the C heap (see _send_file).
    gzip_ok = b"gzip" in header_part.lower()
    try:
        lines = header_part.split(b"\r\n")
        method, path, _ = lines[0].decode().split(" ")
    except Exception:
        _send(cl, 400, "text/plain", "bad request")
        return

    if getattr(config, "WEB_DEBUG", False):
        # one line per request on the serial console - the definitive
        # "did the browser's packets actually reach the device?" signal
        print("web:", method, path)

    if "?" in path:
        path, query = path.split("?", 1)
    else:
        query = ""
    params = _parse_query(query)

    # Uploads are streamed straight to flash instead of buffered in RAM -
    # a handful of .py files easily exceeds the ESP32's ~100KB heap if
    # read into one bytes object first (see _read_body below).
    if path == "/api/upload" and method == "POST":
        content_type_line = _get_header(header_part, "content-type")
        boundary = _extract_boundary(content_type_line)
        if not boundary:
            _send(cl, 400, "text/plain", "missing multipart boundary")
            return
        content_length = _content_length(header_part)
        if content_length > _MAX_UPLOAD_BYTES:
            _send(cl, 413, "application/json", json.dumps(
                {"ok": False, "error": "upload too large ({} bytes, max {})".format(
                    content_length, _MAX_UPLOAD_BYTES)}))
            return
        try:
            saved, rejected = _stream_multipart_to_disk(
                cl, boundary, content_length, leftover)
        except Exception as e:
            # A stalled or overlong transfer aborts here rather than
            # spinning. Whatever landed is reported so the user knows
            # the device is in a half-updated state and can retry.
            state.log_event("code_upload", "FAILED: {}".format(e))
            _send(cl, 500, "application/json",
                  json.dumps({"ok": False, "error": str(e)}))
            return
        state.log_event("code_upload", "saved={} rejected={}".format(saved, rejected))
        _send(cl, 200, "application/json", json.dumps({"ok": True, "saved": saved, "rejected": rejected}))
        return

    body = _read_body(cl, header_part, leftover)

    if path == "/" and method == "GET":
        _send_file(cl, INDEX_HTML_PATH, "text/html", gzip_ok)
    elif path == "/api/status" and method == "GET":
        _send_json(cl, _status_payload())
    elif path == "/api/history" and method == "GET":
        # ?hours=N serves the flash-backed long history (survives reboots);
        # no parameter keeps the live 3-hour RAM buffer, which is finer
        # grained (1/min vs 1/15min) and costs no file reads.
        try:
            _hours = _int_param(params, "hours", 0, lo=0)
        except ValueError:
            _hours = 0
        if _hours > 0:
            _send_long_history(cl, min(_hours, 24 * 14))
        else:
            _send_history(cl)
    elif path == "/api/events" and method == "GET":
        # 60 events x ~100 bytes = ~6KB, the same size that broke the pin
        # map. Streamed for the same reason.
        _send_json(cl, state.get_events())
    elif path == "/api/valve" and method == "POST":
        action = params.get("state", "")
        valve_name = params.get("valve", _default_valve_name)
        valve = _valves.get(valve_name)
        if valve:
            if action == "open":
                valve.open(reason="manual_web")
            elif action == "close":
                valve.close(reason="manual_web")
        _send(cl, 200, "application/json", json.dumps({"ok": bool(valve)}))
    elif path == "/api/water/trigger" and method == "POST":
        settings = settings_store.get()
        duration = _int_param(params, "duration",
                              settings["supplemental_duration_sec"], lo=1)
        valve_name = params.get("valve", _default_valve_name)
        ok = _trigger_watering_cb(valve_name, duration, "manual_web") if valve_name else False
        _send(cl, 200, "application/json", json.dumps({"ok": bool(ok)}))
    elif path == "/api/water/all" and method == "POST":
        # Master quick-water: every valve runs, one at a time, in order.
        settings = settings_store.get()
        duration = _int_param(params, "duration",
                              settings["supplemental_duration_sec"], lo=1)
        names = [v["name"] for v in settings["hardware"].get("valves", [])]
        ok = _trigger_valves_cb(names, duration, "manual_web_all") if names else False
        _send(cl, 200, "application/json", json.dumps({"ok": bool(ok), "valves": names}))
    elif path == "/api/zone/trigger" and method == "POST":
        # Water a whole zone: opens its valves one at a time, in order,
        # each for the zone's own run time (fallback: supplemental default).
        settings = settings_store.get()
        zone_name = params.get("zone", "")
        if "duration" in params:
            duration = _int_param(params, "duration", 60, lo=1)
        else:
            duration = settings.get("zone_durations", {}).get(zone_name) or settings["supplemental_duration_sec"]
        valve_names = settings["hardware"].get("zone_valves", {}).get(zone_name, [])
        ok = _trigger_valves_cb(valve_names, duration, "manual_web_zone") if valve_names else False
        _send(cl, 200, "application/json", json.dumps({"ok": bool(ok), "valves": valve_names}))
    elif path == "/api/settings" and method == "GET":
        _send_json(cl, settings_store.get())
    elif path == "/api/settings" and method == "POST":
        _apply_settings_patch(params, body)
        _send_json(cl, settings_store.get())
    elif path == "/api/schedules" and method == "GET":
        _send_json(cl, settings_store.get().get("schedules", []))
    elif path == "/api/schedules" and method == "POST":
        # Body is the full replacement list of schedules as JSON.
        _apply_schedules(body)
        _send_json(cl, settings_store.get().get("schedules", []))
    elif path == "/api/pinmap" and method == "GET":
        _send_pinmap(cl)
    elif path == "/api/i2c/scan" and method == "GET":
        # Live I2C bus scan - returns the address of every device that
        # answers. ADS1115 boards show up as 0x48-0x4B depending on how
        # their ADDR pin is wired. Lets the UI show what's really connected.
        # Queue it - never scan inline. See state.scan_requested for why.
        # The response carries the previous result so a repeat call is
        # instant, and `busy` tells the UI to poll for the fresh one.
        if _i2c is not None and not state.scan_busy:
            state.scan_requested = True
        prev = state.scan_result or {}
        _send(cl, 200, "application/json", json.dumps({
            "found": prev.get("found", []),
            "at": prev.get("at"),
            "busy": state.scan_busy or state.scan_requested,
        }))
    elif path == "/api/valves" and method == "GET":
        _send_json(cl, settings_store.get()["hardware"].get("valves", []))
    elif path == "/api/valves" and method == "POST":
        _apply_valves_patch(body)
        _send(cl, 200, "application/json", json.dumps({"ok": True, "rebooting": True}))
        cl.close()
        state.log_event("reboot", "applying valve config change")
        time.sleep(1)
        machine.reset()
    elif path == "/api/zones" and method == "GET":
        _send_json(cl, _zones_payload())
    elif path == "/api/zones" and method == "POST":
        # Body is the full replacement zone list. Zones are live config
        # (read fresh every moisture check) - no reboot needed.
        _apply_zones_patch(body)
        _send_json(cl, _zones_payload())
    elif path == "/api/hardware" and method == "GET":
        _send_json(cl, settings_store.get()["hardware"])
    elif path == "/api/hardware" and method == "POST":
        _apply_hardware_patch(params, body)
        _send(cl, 200, "application/json", json.dumps({"ok": True, "rebooting": True}))
        cl.close()
        state.log_event("reboot", "applying hardware config change")
        time.sleep(1)
        machine.reset()
    elif path == "/api/config/export" and method == "GET":
        # Full device configuration (everything except WiFi creds, which
        # live in config.py) as a downloadable file - for backups and for
        # cloning a working setup onto a new kit.
        _send_json(cl, {"planter_config": 1, "settings": settings_store.get()},
                   filename="planter-config.json")
    elif path == "/api/config/import" and method == "POST":
        if _apply_config_import(body):
            _send(cl, 200, "application/json", json.dumps({"ok": True, "rebooting": True}))
            cl.close()
            state.log_event("reboot", "config imported")
            time.sleep(1)
            machine.reset()
        else:
            _send(cl, 400, "application/json", json.dumps({"ok": False, "error": "not a planter config file"}))
    elif path == "/api/wifi" and method == "GET":
        # saved SSID only - never send the password to the browser
        _send(cl, 200, "application/json", json.dumps({
            "ssid": wifi.load_creds(config)["ssid"],
            "connected": wifi.is_connected(),
        }))
    elif path == "/api/wifi" and method == "POST":
        ssid, password = "", ""
        try:
            incoming = json.loads(body)
            ssid = str(incoming.get("ssid", "")).strip()
            password = str(incoming.get("password", ""))
        except Exception:
            pass
        if not ssid:
            _send(cl, 400, "application/json", json.dumps({"ok": False, "error": "ssid required"}))
        else:
            # Save and verify BEFORE promising a reboot: if the write
            # failed we'd come back on the old network having said "ok".
            try:
                wifi.save_creds(ssid, password)
            except Exception as e:
                state.log_event("wifi", "credential save FAILED: {}".format(e))
                _send(cl, 500, "application/json",
                      json.dumps({"ok": False,
                                  "error": "could not save credentials: {}".format(e)}))
                return
            _send(cl, 200, "application/json", json.dumps({"ok": True, "rebooting": True}))
            cl.close()
            state.log_event("reboot", "wifi credentials changed to " + ssid)
            time.sleep(1)
            machine.reset()
    elif path == "/api/calibrate" and method == "POST":
        # Queue a calibration capture. Averaging the probe takes ~10s, so
        # the main loop does it (see the update endpoints below for the
        # same reasoning) and the dashboard polls /api/calibrate for the
        # result.
        try:
            incoming = json.loads(body)
            zone = str(incoming.get("zone", "")).strip()
            point = str(incoming.get("point", "")).strip()
        except Exception:
            zone, point = "", ""
        hw = settings_store.get()["hardware"]
        if point not in ("dry", "wet"):
            _send(cl, 400, "application/json",
                  json.dumps({"ok": False, "error": "point must be 'dry' or 'wet'"}))
        elif zone not in hw.get("zone_channels", {}):
            _send(cl, 400, "application/json",
                  json.dumps({"ok": False, "error": "unknown zone: " + zone}))
        elif state.calibration_busy or state.calibration_requested:
            _send(cl, 200, "application/json",
                  json.dumps({"ok": True, "queued": False,
                              "error": "a calibration is already running"}))
        else:
            state.calibration_result = None
            state.calibration_requested = {"zone": zone, "point": point, "seconds": 10}
            _send(cl, 200, "application/json",
                  json.dumps({"ok": True, "queued": True, "seconds": 10}))
    elif path == "/api/calibrate" and method == "GET":
        _send_json(cl, {
            "busy": state.calibration_busy or bool(state.calibration_requested),
            "result": state.calibration_result,
            "calibration": settings_store.get()["hardware"].get("zone_calibration", {}),
        })
    elif path in ("/api/update/check", "/api/update/apply") and method == "POST":
        # NEVER do the network work here. A TLS handshake on MicroPython
        # can block indefinitely - wrap_socket() performs the handshake
        # internally and some ESP32 builds ignore the socket timeout while
        # doing it. Blocking inside the handler froze the whole main loop:
        # no HTTP responses (browser reported "Failed to fetch"), no valve
        # timing, until the watchdog rebooted the board.
        #
        # Instead: queue the request, answer immediately, and let the main
        # loop run it between iterations. The dashboard polls /api/status
        # for the outcome.
        if _update_cb is None:
            _send(cl, 503, "application/json",
                  json.dumps({"ok": False, "error": "updater not available"}))
        else:
            want_install = path.endswith("/apply")
            if state.update_in_progress:
                _send(cl, 200, "application/json",
                      json.dumps({"ok": True, "queued": False,
                                  "error": "an update check is already running"}))
            else:
                state.update_requested = "apply" if want_install else "check"
                state.update_error = None
                state.log_event("update",
                                "dashboard requested " + state.update_requested)
                _send(cl, 200, "application/json",
                      json.dumps({"ok": True, "queued": True}))
    elif path == "/api/reboot" and method == "POST":
        _send(cl, 200, "application/json", json.dumps({"ok": True}))
        cl.close()
        state.log_event("reboot", "manual reboot requested")
        time.sleep(1)
        machine.reset()
    elif path.startswith("/api/"):
        _send(cl, 404, "text/plain", "not found")
    else:
        # Unknown non-API path: send the browser to the dashboard. This is
        # also what makes a phone's captive-portal probe pop the dashboard
        # open when it joins the rescue hotspot.
        _send_all(cl, b"HTTP/1.1 302 Found\r\nLocation: /\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


_MAX_HEADER_BYTES = 4096  # headers are small; bail out rather than loop forever on garbage


def _read_headers(cl):
    """Read only up through the blank line ending the headers. Returns
    (header_part, leftover) - leftover is any bytes already read past the
    header boundary (start of the body), which callers must pass along
    explicitly since MicroPython's built-in socket objects don't support
    stashing arbitrary attributes on them (unlike CPython sockets)."""
    data = b""
    while b"\r\n\r\n" not in data:
        _feed()
        chunk = cl.recv(512)
        if not chunk:
            break
        data += chunk
        if len(data) > _MAX_HEADER_BYTES:
            return b"", b""
    if b"\r\n\r\n" not in data:
        return b"", b""
    header_part, rest = data.split(b"\r\n\r\n", 1)
    return header_part, rest


def _int_param(params, key, default, lo=None, hi=None):
    """Read an integer query parameter without trusting it.

    Query strings are remote input: "?duration=abc" used to raise
    ValueError straight out of the handler, so the request got no
    response at all and the browser saw a dropped connection. Anything
    unparseable now falls back to the default, and callers can clamp."""
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _content_length(header_part):
    """Parse Content-Length, tolerating a malformed value. This is remote
    input: an unparseable header used to raise ValueError out of the
    handler, dropping the connection with no response."""
    for line in header_part.split(CRLF.encode())[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                n = int(line.split(b":", 1)[1].strip())
            except (ValueError, IndexError):
                return 0
            return n if n > 0 else 0
    return 0


# Every non-upload route sends a small JSON payload; the largest real one
# (a full zones or schedules replacement) is a few KB. Anything claiming
# more is malformed or hostile, and MUST NOT be buffered: growing a bytes
# object toward the Content-Length a client asserts is a straight path to
# exhausting a ~100KB heap and taking the WiFi stack down with it.
_MAX_BODY_BYTES = 16 * 1024


def _read_body(cl, header_part, leftover):
    """Buffer the full body in RAM - fine for the small JSON payloads every
    route except /api/upload sends. Uploads use _stream_multipart_to_disk
    instead, which never holds more than one chunk in memory.

    Hard-capped at _MAX_BODY_BYTES: the length is client-controlled, and an
    unbounded buffer here is a memory-exhaustion vector rather than merely
    a slow request."""
    content_length = _content_length(header_part)
    if content_length > _MAX_BODY_BYTES:
        print("web: refusing oversized body ({} bytes)".format(content_length))
        return b""
    body = leftover
    while len(body) < content_length:
        _feed()
        chunk = cl.recv(2048)
        if not chunk:
            break
        body += chunk
        if len(body) > _MAX_BODY_BYTES:
            print("web: body exceeded cap, truncating")
            break
    return body


def _get_header(header_part, name):
    needle = (name.lower() + ":").encode()
    for line in header_part.split(b"\r\n"):
        if line.lower().startswith(needle):
            return line.split(b":", 1)[1].strip()
    return b""


def _extract_boundary(content_type_line):
    if b"boundary=" not in content_type_line:
        return None
    return content_type_line.split(b"boundary=", 1)[1].strip()


_ALLOWED_UPLOAD_EXTENSIONS = (".py", ".mpy", ".html")


def _sanitize_filename(name):
    """Strip any path components and only allow .py/.html files - blocks
    overwriting settings.json/events.log by mistake and blocks path
    traversal. .html is allowed alongside .py since the dashboard
    (index.html) is now a separate file, not embedded in web.py."""
    name = name.replace("\\", "/").split("/")[-1]
    if not name or name.startswith("."):
        return None
    if not any(name.endswith(ext) for ext in _ALLOWED_UPLOAD_EXTENSIONS):
        return None
    return name


_UPLOAD_CHUNK = 1024
# The whole firmware is ~150KB and index.html alone is ~90KB, so half a
# megabyte is generous. The cap matters because the upload loop calls
# _feed() while it reads: without a bound, a client that claims a huge
# Content-Length and trickles bytes would keep the watchdog fed forever
# - a hang with its own safety net disarmed.
_MAX_UPLOAD_BYTES = 512 * 1024
# Wall-clock ceiling for one upload, for the same reason.
_UPLOAD_DEADLINE_SEC = 120


def _upload_feed(deadline):
    """Feed the watchdog during an upload, but give up if the transfer
    has overrun its deadline. Feeding unconditionally inside a loop whose
    length a client controls would let a stalled transfer hang the device
    indefinitely - the watchdog would never fire."""
    if time.time() > deadline:
        raise OSError("upload exceeded {}s deadline".format(_UPLOAD_DEADLINE_SEC))
    _feed()


def _stream_multipart_to_disk(cl, boundary, content_length, leftover):
    _deadline = time.time() + _UPLOAD_DEADLINE_SEC
    """Read a multipart/form-data body straight off the socket and write
    each file's content directly to flash as it arrives, one small chunk
    at a time. Never holds more than ~1-2KB in RAM regardless of how many
    files or how large the total upload is - buffering the whole request
    (the old approach) can exceed the ESP32's ~100KB heap when dragging in
    several files at once, which kills the connection with no response and
    shows up in the browser as "Failed to fetch"."""
    delim = b"\r\n--" + boundary
    bytes_read = len(leftover)
    buf = leftover

    saved, rejected = [], []
    out_file = None
    out_name = None
    # Search buf for the next delimiter; anything before it (once we're
    # past a part's headers) is file content to flush to disk.
    in_headers = False  # True once we're past the opening "--boundary\r\n" and reading that part's headers
    first_boundary_consumed = False

    def close_current():
        nonlocal out_file, out_name
        if out_file is not None:
            out_file.close()
            if out_name:
                saved.append(out_name)
                # The uploaded file is now authoritative - remove its .py/
                # .mpy counterpart, or the import system may keep loading
                # the stale one (a .py shadows its .mpy).
                twin = None
                if out_name.endswith(".py"):
                    twin = out_name[:-3] + ".mpy"
                elif out_name.endswith(".mpy"):
                    twin = out_name[:-4] + ".py"
                if twin:
                    try:
                        os.remove(twin)
                        state.log_event("code_upload", "removed stale " + twin)
                    except OSError:
                        pass  # no counterpart - nothing to do
        out_file = None
        out_name = None

    while True:
        if not first_boundary_consumed:
            idx = buf.find(b"--" + boundary + b"\r\n")
            if idx == -1:
                if bytes_read >= content_length:
                    break
                _upload_feed(_deadline)
                chunk = cl.recv(_UPLOAD_CHUNK)
                if not chunk:
                    break
                buf += chunk
                bytes_read += len(chunk)
                continue
            buf = buf[idx + len(b"--" + boundary + b"\r\n"):]
            first_boundary_consumed = True
            in_headers = True

        if in_headers:
            while b"\r\n\r\n" not in buf:
                if bytes_read >= content_length:
                    break
                _upload_feed(_deadline)
                chunk = cl.recv(_UPLOAD_CHUNK)
                if not chunk:
                    break
                buf += chunk
                bytes_read += len(chunk)
            if b"\r\n\r\n" not in buf:
                break
            part_headers, buf = buf.split(b"\r\n\r\n", 1)
            filename = None
            for line in part_headers.split(b"\r\n"):
                if b"filename=" in line:
                    try:
                        tail = line.split(b"filename=", 1)[1]
                        filename = tail.strip(b'"; \r\n').decode()
                    except Exception:
                        filename = None
            safe_name = _sanitize_filename(filename) if filename else None
            if safe_name:
                out_name = safe_name
                out_file = open(safe_name, "wb")
            elif filename is not None:
                rejected.append(filename)
            in_headers = False

        # Flush content up to the next boundary marker, streaming in
        # small pieces so a large file never sits fully in RAM.
        while True:
            idx = buf.find(delim)
            if idx != -1:
                if out_file is not None:
                    out_file.write(buf[:idx])
                close_current()
                buf = buf[idx + len(delim):]
                # buf now starts with "--" (end) or "\r\n" (next part)
                if buf.startswith(b"--"):
                    return saved, rejected
                if buf.startswith(b"\r\n"):
                    buf = buf[2:]
                in_headers = True
                break
            # No boundary in what we have yet - flush all but a small tail
            # (long enough to still catch a boundary split across chunks)
            safe_flush = max(0, len(buf) - len(delim))
            if safe_flush and out_file is not None:
                out_file.write(buf[:safe_flush])
            if safe_flush:
                buf = buf[safe_flush:]
            if bytes_read >= content_length:
                # ran out of body without finding a closing boundary
                close_current()
                return saved, rejected
            _upload_feed(_deadline)
            chunk = cl.recv(_UPLOAD_CHUNK)
            if not chunk:
                close_current()
                return saved, rejected
            buf += chunk
            bytes_read += len(chunk)

    close_current()
    return saved, rejected


_SETTINGS_INT_KEYS = (
    "supplemental_duration_sec",
    "min_supplemental_interval_sec",
    "post_daily_lockout_sec",
    "soak_recheck_sec",
    "max_water_cycles",
)
_SETTINGS_BOOL_KEYS = ("daily_enabled", "moisture_watering_enabled")


def _apply_settings_patch(params, body):
    patch = {}
    # Accept either query-string params or a JSON body. Only whitelisted
    # keys are accepted - a stray body must not be able to clobber
    # "hardware" or "schedules" wholesale.
    if body:
        try:
            incoming = json.loads(body)
            if isinstance(incoming, dict):
                for key in _SETTINGS_INT_KEYS:
                    if key in incoming:
                        patch[key] = max(0, int(incoming[key]))
                for key in _SETTINGS_BOOL_KEYS:
                    if key in incoming:
                        patch[key] = bool(incoming[key])
                if isinstance(incoming.get("zone_thresholds"), dict):
                    patch["zone_thresholds"] = incoming["zone_thresholds"]
                if "weather_zip" in incoming:
                    patch["weather_zip"] = str(incoming["weather_zip"])[:10]
                if "tz_offset_min" in incoming:
                    try:
                        # -12h .. +14h, may be negative (unlike the int keys above)
                        patch["tz_offset_min"] = max(-720, min(840, int(incoming["tz_offset_min"])))
                    except Exception:
                        pass
        except Exception:
            pass
    for key in _SETTINGS_INT_KEYS:
        if key in params:
            patch[key] = max(0, int(params[key]))
    for key in _SETTINGS_BOOL_KEYS:
        if key in params:
            patch[key] = params[key] in ("1", "true", "True")

    # zone thresholds come as threshold_<zonename>=NN
    settings = settings_store.get()
    thresholds = dict(settings["zone_thresholds"])
    for key, val in params.items():
        if key.startswith("threshold_"):
            zone_name = key[len("threshold_") :]
            thresholds[zone_name] = int(val)
    if thresholds != settings["zone_thresholds"]:
        patch["zone_thresholds"] = thresholds

    if patch:
        settings_store.update(patch)
        state.log_event("settings_update", str(patch))


def _apply_schedules(body):
    """Body is a JSON list of schedules. Validate and normalize each one,
    assign fresh sequential ids, then persist the whole list."""
    try:
        incoming = json.loads(body)
    except Exception:
        incoming = []
    hw = settings_store.get()["hardware"]
    valve_names = {v["name"] for v in hw.get("valves", [])}
    zone_names = set(hw.get("zone_channels", {}).keys())
    clean = []
    next_id = 1
    for s in incoming:
        try:
            hour = int(s.get("hour", 0))
            minute = int(s.get("minute", 0))
            duration = int(s.get("duration_sec", 60))
        except Exception:
            continue
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            continue
        if duration < 1:
            continue
        sched_valves = [v for v in s.get("valve_names", []) if v in valve_names]
        # Zones are stored by NAME and expanded to their valves at fire
        # time, so re-mapping a zone automatically updates its schedules.
        sched_zones = [z for z in s.get("zone_names", []) if z in zone_names]
        if not sched_valves and not sched_zones:
            continue  # a schedule with nothing to open does nothing - reject it
        clean.append(
            {
                "id": next_id,
                "hour": hour,
                "minute": minute,
                "duration_sec": duration,
                "enabled": bool(s.get("enabled", True)),
                "valve_names": sched_valves,
                "zone_names": sched_zones,
            }
        )
        next_id += 1
    settings_store.update({"schedules": clean})
    # reset fired-tracking so edited schedules can fire cleanly today
    state.last_schedule_fired = {}
    state.log_event("schedules_update", "{} schedules".format(len(clean)))


# Devices that live on real GPIO pins (not moisture - those are on the ADS)

# ESP32-WROOM-32 (38-pin devkit) pin classification.
_ALL_GPIO = list(range(40))
# Input-only pins, no internal pull-ups - fine for a flow meter
# (interrupt/counter input), not for a valve output.
_INPUT_ONLY = [34, 35, 36, 39]
# GPIO 6-11 wire the module's SPI flash chip. Touching them crashes or
# bricks the boot - never assignable.
_FLASH = [6, 7, 8, 9, 10, 11]
# GPIO 1 (TX0) / 3 (RX0) are the USB serial console - physically present,
# but using them breaks Thonny/flashing/REPL. Not assignable.
_SERIAL = [1, 3]
# Boot-strapping pins - sampled at reset, but usable as outputs after boot
# with care (GPIO2 drives the onboard LED on most devkits). Assignable,
# shown with a warning. GPIO12 is the riskiest: pulled high at reset it
# selects the wrong flash voltage and the board won't boot.
_STRAPPING = [0, 2, 12, 15]
# Not bonded out on the WROOM-32 module (GPIO 16/17 ARE available here -
# they're only reserved on WROVER modules, where PSRAM uses them).
_NOT_BROKEN_OUT = [20, 24, 28, 29, 30, 31, 37, 38]


def _pinmap_roles(hw):
    """GPIO -> human label, shared by the streaming and dict forms."""
    roles = {}
    roles[hw["i2c_scl_pin"]] = "I2C SCL (ADS1115)"
    roles[hw["i2c_sda_pin"]] = "I2C SDA (ADS1115)"
    for v in hw.get("valves", []):
        roles[v["pin"]] = "Solenoid valve: {}".format(v["name"])
        if v.get("flow_meter_pin") is not None:
            roles[v["flow_meter_pin"]] = "Flow meter: {}".format(v["name"])
    for pin in hw.get("flow_meter_pins", []):
        roles.setdefault(pin, "Flow meter")
    if hw.get("rain_sensor_pin") is not None:
        roles[hw["rain_sensor_pin"]] = "Rain sensor (LM393)"
    _led = getattr(config, "STATUS_LED_PIN", None)
    if _led is not None:
        roles.setdefault(_led, "Status LED (onboard D2)")
    return roles


def _pinmap_payload():
    """Return the role/status of every GPIO 0-39 so the UI can draw a full
    reference table. Moisture sensors are NOT here - they live on ADS1115
    channels, reported separately under 'ads_channels'."""
    hw = settings_store.get()["hardware"]

    roles = _pinmap_roles(hw)

    pins = []
    for p in _ALL_GPIO:
        pins.append(
            {
                "gpio": p,
                "role": roles.get(p, None),
                "input_only": p in _INPUT_ONLY,
                "flash": p in _FLASH,
                "serial": p in _SERIAL,
                "strapping": p in _STRAPPING,
                "not_broken_out": p in _NOT_BROKEN_OUT,
            }
        )

    # ADS1115 channels (moisture sensors) - 4 global channels per board
    zone_channels = hw.get("zone_channels", {})
    channel_roles = {}
    for name, ch in zone_channels.items():
        channel_roles[ch] = name
    ads_channels = []
    for ch in range(4 * len(hw.get("ads1115_addresses", [0]))):
        ads_channels.append({"channel": ch, "zone": channel_roles.get(ch, None)})

    return {"pins": pins, "ads_channels": ads_channels, "hardware": hw}


def _apply_hardware_patch(params, body):
    original = settings_store.get()["hardware"]
    hw = dict(original)

    # Preferred path: the pin-map UI sends the whole hardware object as JSON.
    if body:
        try:
            incoming = json.loads(body)
            if isinstance(incoming, dict):
                hw.update(incoming.get("hardware", incoming))
        except Exception:
            pass

    # Legacy path: individual query-string params (old Hardware Config card).
    for key in ("i2c_scl_pin", "i2c_sda_pin"):
        if key in params:
            pin = _int_param(params, key, None, lo=0, hi=39)
            if pin is not None:
                hw[key] = pin

    zone_channels = dict(hw.get("zone_channels", {}))
    for key, val in params.items():
        if key.startswith("channel_"):
            zone_name = key[len("channel_") :]
            zone_channels[zone_name] = int(val)
    if "zone_channels" not in hw or zone_channels != hw.get("zone_channels"):
        # only overwrite from params if params actually carried channel_ keys
        if any(k.startswith("channel_") for k in params):
            hw["zone_channels"] = zone_channels

    # I2C must drive both lines, so input-only pins can never host it -
    # and flash/serial/nonexistent pins are off limits as always. A bad
    # value silently keeps the previous pin rather than bricking I2C.
    for key in ("i2c_scl_pin", "i2c_sda_pin"):
        p = hw.get(key)
        if p in _INPUT_ONLY or p in _FLASH or p in _SERIAL or p in _NOT_BROKEN_OUT:
            hw[key] = original[key]

    # Normalize the ADS1115 board address list: ints, unique, 1-4 boards.
    addrs = hw.get("ads1115_addresses")
    if not isinstance(addrs, list):
        addrs = [hw.get("ads1115_address", 0x48)]
    clean_addrs = []
    for a in addrs:
        try:
            a = int(a)
        except Exception:
            continue
        if a not in clean_addrs:
            clean_addrs.append(a)
    hw["ads1115_addresses"] = clean_addrs[:4] or [0x48]
    hw.pop("ads1115_address", None)

    hw.setdefault("flow_meter_pins", [])
    hw.setdefault("valves", [])
    hw.setdefault("zone_valves", {})

    # Rain sensor pin: int or None; never a flash/serial/nonexistent pin.
    rp = hw.get("rain_sensor_pin")
    try:
        rp = int(rp) if rp not in (None, "") else None
    except Exception:
        rp = None
    if rp in _FLASH or rp in _SERIAL or rp in _NOT_BROKEN_OUT:
        rp = None
    hw["rain_sensor_pin"] = rp

    settings_store.update({"hardware": hw})
    state.log_event("hardware_update", str(hw))


_WATERING_MODES = ("duration", "volume")


def _apply_valves_patch(body):
    """Body is {"valves": [{name, pin, active_high, flow_meter_pin,
    watering_mode, target_volume_l}, ...], "renames": {old: new}}, the full
    replacement list. Renames are applied to zone assignments and schedule
    valve_names so a renamed valve keeps its zones and schedule slots.
    Requires a reboot since valve Pin objects are constructed once at boot.
    watering_mode "volume" is config-only groundwork - nothing reads flow
    meter pulses yet, so it has no effect until that's implemented."""
    hw = dict(settings_store.get()["hardware"])
    renames = {}
    try:
        incoming = json.loads(body)
        if isinstance(incoming, dict):
            valve_list = incoming.get("valves", incoming)
            renames = incoming.get("renames", {}) or {}
            # Optional: the standalone flow-meter pin list rides along so a
            # flow meter can move between valve-attached and standalone in
            # one atomic save (one reboot), not two.
            if isinstance(incoming.get("flow_meter_pins"), list):
                fm = []
                for p in incoming["flow_meter_pins"]:
                    try:
                        p = int(p)
                    except Exception:
                        continue
                    if p not in fm:
                        fm.append(p)
                hw["flow_meter_pins"] = fm
        else:
            valve_list = incoming
    except Exception:
        valve_list = []

    clean = []
    seen_names = set()
    for v in valve_list:
        try:
            name = str(v["name"]).strip()
            pin = int(v["pin"])
        except Exception:
            continue
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        flow_pin = v.get("flow_meter_pin")
        mode = v.get("watering_mode", "duration")
        if mode not in _WATERING_MODES:
            mode = "duration"
        volume = v.get("target_volume_l")
        clean.append(
            {
                "name": name,
                "pin": pin,
                "active_high": bool(v.get("active_high", True)),
                "flow_meter_pin": int(flow_pin) if flow_pin not in (None, "") else None,
                "watering_mode": mode,
                "target_volume_l": float(volume) if volume not in (None, "") else None,
            }
        )

    if not clean:
        return  # refuse to save an empty valve list - would leave nothing to water with

    hw["valves"] = clean

    # Apply renames first so a renamed valve keeps its zone assignments
    # and schedule slots, THEN drop anything pointing at a valve that
    # genuinely no longer exists.
    zone_valves = {}
    for zn, names in hw.get("zone_valves", {}).items():
        kept = [renames.get(n, n) for n in names]
        kept = [n for n in kept if n in seen_names]
        if kept:
            zone_valves[zn] = kept
    hw["zone_valves"] = zone_valves

    if renames:
        # schedules live on the same in-memory settings dict, so mutating
        # them here gets persisted by the settings_store.update below
        for sched in settings_store.get().get("schedules", []):
            sched["valve_names"] = [
                renames.get(n, n) for n in sched.get("valve_names", [])
            ]

    settings_store.update({"hardware": hw})
    state.log_event("valves_update", str(clean))


# Everything a config file may carry - deliberately excludes anything not
# in settings.json (WiFi creds stay in config.py on the device).
_CONFIG_KEYS = (
    "schedules",
    "supplemental_duration_sec",
    "min_supplemental_interval_sec",
    "post_daily_lockout_sec",
    "soak_recheck_sec",
    "max_water_cycles",
    "zone_thresholds",
    "zone_durations",
    "zone_wet_targets",
    "daily_enabled",
    "moisture_watering_enabled",
    "weather_zip",
    "tz_offset_min",
    "hardware",
)


def _apply_config_import(body):
    """Body is the file produced by /api/config/export (or a hand-built
    preset with the same shape). Returns True and persists on success -
    the caller reboots, and settings_store.load() re-runs its migrations
    and sanity checks against the imported data at boot."""
    try:
        incoming = json.loads(body)
    except Exception:
        return False
    if not isinstance(incoming, dict):
        return False
    data = incoming.get("settings", incoming)
    if not isinstance(data, dict) or not isinstance(data.get("hardware"), dict):
        return False
    hw = data["hardware"]
    if not hw.get("valves") or "zone_channels" not in hw:
        return False
    patch = {k: data[k] for k in _CONFIG_KEYS if k in data}
    settings_store.update(patch)
    state.log_event("config_import", "{} keys".format(len(patch)))
    return True


def _zones_payload():
    """Zones as one editable unit: name + sensor channel + dry threshold +
    the valve(s) that zone opens. Assembled from zone_channels/zone_valves
    (hardware) and zone_thresholds (settings)."""
    settings = settings_store.get()
    hw = settings["hardware"]
    thresholds = settings.get("zone_thresholds", {})
    durations = settings.get("zone_durations", {})
    wet_targets = settings.get("zone_wet_targets", {})
    cal = hw.get("zone_calibration", {})
    default_duration = settings.get("supplemental_duration_sec", 60)
    zones = []
    for name, ch in hw.get("zone_channels", {}).items():
        threshold = thresholds.get(name, 30)
        zones.append(
            {
                "name": name,
                "channel": ch,
                "valves": hw.get("zone_valves", {}).get(name, []),
                "threshold": threshold,
                "wet_target": wet_targets.get(name, min(100, threshold + 10)),
                "water_duration_sec": durations.get(name, default_duration),
                "dry_raw": cal.get(name, {}).get("dry_raw", settings_store.DEFAULT_DRY_RAW),
                "wet_raw": cal.get(name, {}).get("wet_raw", settings_store.DEFAULT_WET_RAW),
            }
        )
    zones.sort(key=lambda z: z["channel"])
    return zones


def _apply_zones_patch(body):
    """Body is {"zones": [{name, channel, valves, threshold}, ...]}, the
    full replacement list. Renaming a zone just changes its key everywhere
    since the whole set is rewritten at once. Live config - no reboot."""
    settings = settings_store.get()
    hw = dict(settings["hardware"])
    valve_names = {v["name"] for v in hw.get("valves", [])}
    try:
        incoming = json.loads(body)
        zone_list = incoming.get("zones", incoming) if isinstance(incoming, dict) else incoming
    except Exception:
        return

    zone_channels = {}
    zone_valves = {}
    thresholds = {}
    durations = {}
    wet_targets = {}
    # Calibration must SURVIVE a zone edit. This function rebuilds the zone
    # maps wholesale, so without carrying it forward a user would lose their
    # captured dry/wet points just by renaming a zone or nudging a
    # threshold. Keyed by the zone's CURRENT name; a rename is handled by
    # the incoming entry carrying its own values.
    old_calib = hw.get("zone_calibration", {})
    calibration = {}
    used_channels = set()
    # global channels: 4 per ADS1115 board (board 1 = 0-3, board 2 = 4-7...)
    max_channel = 4 * len(hw.get("ads1115_addresses", [0])) - 1
    for z in zone_list:
        try:
            name = str(z["name"]).strip()
            channel = int(z["channel"])
        except Exception:
            continue
        if not name or name in zone_channels:
            continue
        if not (0 <= channel <= max_channel) or channel in used_channels:
            continue
        used_channels.add(channel)
        zone_channels[name] = channel
        zone_valves[name] = [v for v in z.get("valves", []) if v in valve_names]
        try:
            t = int(z.get("threshold", 30))
        except Exception:
            t = 30
        thresholds[name] = min(100, max(0, t))
        try:
            d = int(z.get("water_duration_sec", 60))
        except Exception:
            d = 60
        durations[name] = max(1, d)
        # "adequately watered" target - must sit at or above the dry
        # threshold or the soak re-check could never finish
        try:
            w = int(z.get("wet_target", thresholds[name] + 10))
        except Exception:
            w = thresholds[name] + 10
        wet_targets[name] = min(100, max(thresholds[name], w))

        # explicit values win (manual entry / a rename carrying its own),
        # otherwise keep whatever this zone already had
        prev = old_calib.get(name, {})
        try:
            dry_raw = int(z["dry_raw"]) if z.get("dry_raw") is not None else None
        except (TypeError, ValueError):
            dry_raw = None
        try:
            wet_raw = int(z["wet_raw"]) if z.get("wet_raw") is not None else None
        except (TypeError, ValueError):
            wet_raw = None
        calibration[name] = {
            "dry_raw": dry_raw if dry_raw is not None
            else prev.get("dry_raw", settings_store.DEFAULT_DRY_RAW),
            "wet_raw": wet_raw if wet_raw is not None
            else prev.get("wet_raw", settings_store.DEFAULT_WET_RAW),
        }

    hw["zone_channels"] = zone_channels
    hw["zone_valves"] = zone_valves
    hw["zone_calibration"] = calibration
    settings_store.update(
        {
            "hardware": hw,
            "zone_thresholds": thresholds,
            "zone_durations": durations,
            "zone_wet_targets": wet_targets,
        }
    )
    state.log_event("zones_update", str(zone_channels))


def _status_payload():
    valves = {}
    for name, valve in _valves.items():
        slot = state.valves.get(name, {})
        valves[name] = {
            "open": slot.get("is_open", False),
            "seconds_open": valve.seconds_open(),
            "open_reason": slot.get("open_reason"),
            "last_close_ts": slot.get("last_close_ts"),
            "last_close_reason": slot.get("last_close_reason"),
            "last_open_duration": slot.get("last_open_duration"),
            # placeholder until flow-meter pulse counting is implemented -
            # the UI shows "-" while this is None
            "last_volume_l": None,
        }
    # Deliberately lean - this is polled every 5s by the dashboard. The
    # full settings blob is NOT included (fetch /api/settings for that).
    return {
        "moisture": state.latest_moisture,
        "valves": valves,
        "any_valve_open": state.any_valve_open(),
        "uptime_sec": time.time() - state.boot_time,
        "now": time.time(),
        # seconds left before moisture watering is allowed after boot (0 =
        # ready). The dashboard shows this so "why isn't it watering?" has
        # a visible answer right after a power cycle.
        "startup_grace_left": _startup_grace_left(),
        "time_synced": state.time_synced,
        # True/False/None - whether the last probe actually reached the
        # router, as opposed to merely being associated with it
        "lan_ok": state.lan_ok,
        "rssi": state.rssi,
        "wifi_connected": wifi.is_connected(),
        "env": state.env,
        "mem_free": gc.mem_free() if hasattr(gc, "mem_free") else None,
        "mem_alloc": gc.mem_alloc() if hasattr(gc, "mem_alloc") else None,
        "cpu_percent": state.cpu_percent,
        # IDF C-heap (WiFi/lwIP/I2C drivers) - separate from the GC heap
        # above; when THIS runs out the network dies while Python keeps going
        "idf_free": state.idf_free,
        "idf_largest": state.idf_largest,
        # Connections served, and how often the listener had to be rebuilt.
        # A climbing restart count is the signature of the fault that used
        # to take the dashboard down silently until a reboot.
        "conns_accepted": _accepted,
        "listener_restarts": _listener_restarts,
        # OTA updater status (see updater.py) - drives the dashboard's
        # Firmware Updates card
        "update": {
            "version": _installed_version(),
            "last_check": state.last_update_check,
            # Prefer the persisted timestamp: an update REBOOTS the device,
            # so the in-RAM value is always None right when it matters most
            # ("Updated: never" immediately after a successful update).
            "last_install": _installed_at() or state.last_update_install,
            "available": state.update_available,
            "error": state.update_error,
            "busy": state.update_in_progress or bool(state.update_requested),
            "result": state.update_last_result,
        },
    }


def _startup_grace_left():
    """Seconds until moisture watering is allowed after boot, 0 once past."""
    grace = getattr(config, "STARTUP_GRACE_SEC", 60)
    if not grace:
        return 0
    left = grace - (time.time() - state.boot_time)
    return int(left) if left > 0 else 0


def _installed_version():
    try:
        with open("version.json") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


def _installed_at():
    """Unix ts of the last OTA install, read from version.json so it
    survives the reboot the install itself triggers."""
    try:
        with open("version.json") as f:
            return json.load(f).get("installed_at")
    except (OSError, ValueError):
        return None


def _parse_query(query):
    params = {}
    if not query:
        return params
    for pair in query.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[_url_decode(k)] = _url_decode(v)
    return params


def _url_decode(s):
    s = s.replace("+", " ")
    out = ""
    i = 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            out += chr(int(s[i + 1 : i + 3], 16))
            i += 3
        else:
            out += s[i]
            i += 1
    return out


def _send_long_history(cl, hours):
    """Stream the flash-backed history as JSON in the same shape the live
    chart uses. Read line by line and written out in chunks - the file can
    be ~15KB for a week and must never sit in RAM alongside a JSON copy of
    itself (see the two-heap notes in docs/development.md).

    Two passes: one to measure so Content-Length is exact, one to send.
    Reading a small file twice is far cheaper than buffering it once."""
    cutoff = time.time() - hours * 3600

    def _points():
        try:
            with open(state.HISTORY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    fields = line.split(",")
                    try:
                        ts = int(fields[0])
                    except (ValueError, IndexError):
                        continue
                    if ts < cutoff:
                        continue
                    readings = []
                    for pair in fields[1:]:
                        name, _, pct = pair.partition("=")
                        if not name or not pct:
                            continue
                        try:
                            readings.append({"name": name, "percent": float(pct)})
                        except ValueError:
                            continue
                    if readings:
                        yield {"t": ts, "readings": readings}
        except OSError:
            return

    total = 2
    count = 0
    for pt in _points():
        n = len(json.dumps(pt))
        total += n + (1 if count else 0)
        count += 1
        if count % 50 == 0:
            _feed()

    hdr = "HTTP/1.1 200 OK" + CRLF
    hdr += "Content-Type: application/json" + CRLF
    hdr += "Content-Length: {}".format(total) + CRLF
    hdr += "Connection: close" + CRLF + CRLF
    _send_all(cl, hdr.encode())
    _send_all(cl, b"[")
    i = 0
    for pt in _points():
        if i:
            _send_all(cl, b",")
        _send_all(cl, json.dumps(pt).encode())
        i += 1
        if i % 20 == 0:
            _feed()
    _send_all(cl, b"]")


def _send(cl, status, content_type, body):
    """One-shot response for small fixed payloads (errors, {"ok": true}).
    Anything that grows with the user's config goes through _send_json,
    which streams instead."""
    if isinstance(body, str):
        body = body.encode()
    header = "HTTP/1.1 {} OK".format(status) + CRLF
    header += "Content-Type: {}".format(content_type) + CRLF
    header += "Content-Length: {}".format(len(body)) + CRLF
    header += "Connection: close" + CRLF + CRLF
    _send_all(cl, header.encode())
    _send_all(cl, body)


_MAX_STREAM_DEPTH = 6


def _json_fragments(obj, depth=0):
    """Yield an object's JSON encoding one small piece at a time.

    Walks lists and dicts rather than handing the whole structure to
    json.dumps(), so no single string is ever larger than one leaf value.
    Below _MAX_STREAM_DEPTH it falls back to json.dumps for the remainder -
    nothing in this firmware nests that deep, and the cap means a
    surprising structure costs one allocation rather than unbounded
    recursion on a device with a small C stack.

    Output is compact (no spaces), matching MicroPython's json.dumps."""
    if depth >= _MAX_STREAM_DEPTH or not isinstance(obj, (dict, list, tuple)):
        yield json.dumps(obj)
        return
    if isinstance(obj, dict):
        yield "{"
        first = True
        for k, v in obj.items():
            if not first:
                yield ","
            first = False
            yield json.dumps(k if isinstance(k, str) else str(k))
            yield ":"
            for frag in _json_fragments(v, depth + 1):
                yield frag
        yield "}"
    else:
        yield "["
        first = True
        for v in obj:
            if not first:
                yield ","
            first = False
            for frag in _json_fragments(v, depth + 1):
                yield frag
        yield "]"


# Coalesce fragments up to this much before writing. Must not exceed
# _SEND_CHUNK, or _send_all splits the block and the first piece is the
# oversized write that stalls (see _SEND_CHUNK).
_JSON_FLUSH_BYTES = 512


def _send_fragments(cl, frags):
    """Write small JSON fragments to the socket in coalesced blocks.

    Two constraints pull against each other here.

    Allocation: no single buffer may be large, or MicroPython grows its GC
    heap out of the ESP-IDF C heap and lwIP can no longer allocate a TCP
    segment (see _send_json).

    Throughput: but one send() per fragment is ~45 tiny writes for the pin
    map, and Nagle plus the receiver's delayed ACK turns most of them into
    a ~30ms stall. Measured on the board: 6KB took 1.33s that way, and
    /api/status - polled every 5 seconds - went from 128ms to 1.13s. That
    is a real regression, not a rounding error.

    Coalescing into 1KB blocks fixes the throughput without reintroducing
    the large allocation."""
    buf = []
    n = 0
    for frag in frags:
        b = frag.encode()
        buf.append(b)
        n += len(b)
        if n >= _JSON_FLUSH_BYTES:
            _send_all(cl, b"".join(buf))
            buf = []
            n = 0
            _feed()
    if buf:
        _send_all(cl, b"".join(buf))


def _send_json(cl, obj, filename=None):
    """Stream a JSON response without ever building it as one string.

    THIS IS THE TWO-HEAP TRAP. A single json.dumps() of a whole payload is
    the largest allocation this firmware makes, and on a fragmented heap it
    does not merely fail - it forces MicroPython to grow its GC heap OUT OF
    the ESP-IDF C heap that lwIP allocates TCP buffers from, and it never
    gives that memory back. So the NETWORK dies, not just the request.
    Measured mid-request on a real board: the C heap fell to 824 bytes free
    with a largest block of 304 while gc.mem_free() still reported 54KB.
    socket.send() then blocked forever on the 92-byte response HEADER -
    there was no segment to be had - which is why the symptom looked like
    "some dashboard cards load and others hang".

    The payloads that reach this size are the ones that grow with the
    user's setup: the pin map, the event log, and - on a fully populated
    kit - settings, schedules and the config export. A dev board with two
    zones never hits it, which is exactly why it shipped.

    Two passes: measure for an exact Content-Length, then send. Everything
    is measured in BYTES, because json.dumps() emits non-ASCII raw rather
    than escaping it - len() on the string under-counts, and a short
    Content-Length truncates the body, which the browser waits on forever.

    Pass `filename` to send it as a download instead of a page."""
    total = 0
    for frag in _json_fragments(obj):
        total += len(frag.encode())

    header = "HTTP/1.1 200 OK" + CRLF + "Content-Type: application/json" + CRLF
    if filename:
        header += 'Content-Disposition: attachment; filename="{}"'.format(filename) + CRLF
    header += "Content-Length: {}".format(total) + CRLF
    header += "Connection: close" + CRLF + CRLF
    _send_all(cl, header.encode())
    _send_fragments(cl, _json_fragments(obj))


def _send_history(cl):
    _send_json(cl, state.get_moisture_history())


def _send_pinmap(cl):
    """Stream the GPIO pin map instead of building it as one JSON string.

    THIS IS THE TWO-HEAP TRAP, and it is why the GPIO card was the one that
    never loaded. The payload is ~6KB; json.dumps() produced a 6KB string
    and .encode() a second 6KB copy. Allocations that size make MicroPython
    grow its GC heap OUT OF the ESP-IDF C heap - and it never gives that
    memory back. Measured mid-request: IDF free fell to 824 bytes with a
    largest block of 304, while gc.mem_free() still reported 54KB and
    everything looked fine.

    lwIP allocates its TCP send buffers from that same C heap. With 304
    bytes available it cannot get a segment, so socket.send() blocks
    forever - not on the 6KB body, but on the 92-byte HEADER, which is what
    made the symptom so confusing. Small endpoints stayed under the
    threshold and worked, so the dashboard half-loaded.

    Streaming keeps every allocation to one small piece at a time. Two
    passes: measure for an exact Content-Length, then send."""
    settings = settings_store.get()
    hw = settings["hardware"]

    roles = _pinmap_roles(hw)
    ads_count = 4 * len(hw.get("ads1115_addresses", [0]))
    zone_channels = hw.get("zone_channels", {})
    channel_roles = {}
    for name, ch in zone_channels.items():
        channel_roles[ch] = name

    def pieces():
        """Yield the response one small fragment at a time."""
        yield '{"pins":['
        first = True
        for gp in _ALL_GPIO:
            frag = json.dumps({
                "gpio": gp,
                "role": roles.get(gp),
                "input_only": gp in _INPUT_ONLY,
                "flash": gp in _FLASH,
                "serial": gp in _SERIAL,
                "strapping": gp in _STRAPPING,
                "not_broken_out": gp in _NOT_BROKEN_OUT,
            })
            yield (frag if first else "," + frag)
            first = False
        yield '],"ads_channels":['
        first = True
        for ch in range(ads_count):
            frag = json.dumps({"channel": ch, "zone": channel_roles.get(ch)})
            yield (frag if first else "," + frag)
            first = False
        # The dashboard reads hardware for board addresses and valve config.
        # It is ~1.5KB - comfortably under the size that forces the GC heap
        # to grow - so one fragment is fine here.
        yield '],"hardware":'
        # hardware grows with the kit (4 ADS boards, 8 valves, per-zone
        # calibration): 1.8KB on a full setup, so it gets walked too
        # rather than dumped in one piece.
        for frag in _json_fragments(hw):
            yield frag
        yield "}"

    # Measure in BYTES, not characters. json.dumps() emits non-ASCII raw
    # rather than escaping it, so a zone named "Jardin" with an accent is
    # longer encoded than len() reports - and a short Content-Length
    # truncates the body, which the browser then waits on forever.
    total = 0
    for frag in pieces():
        total += len(frag.encode())

    hdr = "HTTP/1.1 200 OK" + CRLF
    hdr += "Content-Type: application/json" + CRLF
    hdr += "Content-Length: {}".format(total) + CRLF
    hdr += "Connection: close" + CRLF + CRLF
    _send_all(cl, hdr.encode())
    _send_fragments(cl, pieces())


# A response must never hold the loop longer than this, no matter what the
# client does.
_SEND_DEADLINE_MS = 4000

# EAGAIN / EWOULDBLOCK from a non-blocking send - "no buffer space right
# now", not an error.
_EAGAIN = (11, 35)


def _send_all(cl, data):
    """Write every byte, or give up on a deadline.

    settimeout() DOES NOT BOUND send() on this port. Measured: a single
    1024-byte send blocked for 30 seconds on a socket with a 3-second
    timeout. Because the server is single-threaded and polled from the main
    loop, that one stuck client froze the whole dashboard - and the valve
    safety cutoff runs in that same loop. One client going away mid-response
    was enough to make the device look dead to everyone else.

    So the socket is switched to non-blocking and the waiting is done here,
    against an explicit deadline: EAGAIN means "lwIP has no buffer yet",
    which is normal back-pressure and worth a short wait; passing the
    deadline means the peer is gone or wedged and the connection is
    abandoned so the loop can get on with its work. The watchdog is fed
    while waiting, and abandoning raises, which makes poll_once close the
    socket in its finally.

    Partial writes are the norm here - send() returns what it accepted, and
    ignoring that return was itself an old bug in this file (it silently
    truncated responses)."""
    if isinstance(data, str):
        data = data.encode()
    try:
        cl.setblocking(False)
    except (OSError, AttributeError):
        pass
    total = len(data)
    sent = 0
    _dbg = getattr(config, "WEB_SEND_DEBUG", False)
    start = time.ticks_ms()
    # Belt and braces alongside the deadline: a deadline is only as good as
    # the clock behind it, and this loop must NEVER become unbounded - it
    # runs inside the main loop that also enforces the valve cutoff. Each
    # spin costs ~2ms, so this caps a stalled send at a few seconds even if
    # ticks_ms misbehaves.
    spins = 0
    while sent < total:
        spins += 1
        if spins > 4000:
            raise OSError("send made no progress: {} of {} bytes".format(sent, total))
        chunk = data[sent:sent + _SEND_CHUNK]
        if _dbg:
            print("  send: attempting", len(chunk), "of", total - sent, "left")
        try:
            n = cl.send(chunk)
        except OSError as e:
            err = e.args[0] if e.args else None
            if err in _EAGAIN:
                if time.ticks_diff(time.ticks_ms(), start) > _SEND_DEADLINE_MS:
                    raise OSError("send deadline exceeded after {} of {} bytes".format(
                        sent, total))
                _feed()
                time.sleep_ms(2)
                continue
            raise
        if _dbg:
            print("  send: returned", n)
        if n is None:
            # non-blocking socket with nothing accepted this time
            if time.ticks_diff(time.ticks_ms(), start) > _SEND_DEADLINE_MS:
                raise OSError("send deadline exceeded after {} of {} bytes".format(
                    sent, total))
            _feed()
            time.sleep_ms(2)
            continue
        if n < 0:
            raise OSError("socket send returned {}".format(n))
        if n == 0:
            if time.ticks_diff(time.ticks_ms(), start) > _SEND_DEADLINE_MS:
                raise OSError("send stalled at {} of {} bytes".format(sent, total))
            _feed()
            time.sleep_ms(2)
            continue
        sent += n
        if sent < total:
            _feed()
    return sent


_SEND_FILE_CHUNK = 512   # same ceiling as _SEND_CHUNK - see the note there


def _send_file(cl, path, content_type, gzip_ok=False):
    """Stream a file straight from flash in small chunks instead of loading
    it into one big string first - see the note at the top of this file for
    why (a single large contiguous allocation reliably fails on a
    fragmented ESP32 heap).

    Prefers a pre-compressed `<path>.gz` twin when the client accepts gzip.
    THIS IS NOT AN OPTIMISATION, it is a fix. index.html is ~101KB and the
    whole ESP-IDF C heap is ~33KB. Pushing the uncompressed file at ~70KB/s
    filled lwIP's retransmit and TIME_WAIT queues with more pbufs than the
    heap could hold: measured straight after one page load, the C heap sat
    at 1328 bytes free / 960 largest, and EVERY other request - the 5s
    dashboard poll included - then blocked for ~31s until those buffers
    drained. That is the "web UI goes down periodically" symptom, and it
    was self-inflicted by the page load itself.

    Compressed the same file is ~20KB, which stays comfortably inside the
    available buffer space, so the queues never get deep enough to starve
    the heap. It also loads five times faster."""
    if gzip_ok:
        try:
            size = os.stat(path + ".gz")[6]
            path = path + ".gz"
            content_encoding = "gzip"
        except OSError:
            content_encoding = None
    else:
        content_encoding = None

    if content_encoding is None:
        try:
            size = os.stat(path)[6]
        except OSError:
            _send(cl, 404, "text/plain", "not found")
            return

    header = "HTTP/1.1 200 OK" + CRLF
    header += "Content-Type: {}".format(content_type) + CRLF
    if content_encoding:
        header += "Content-Encoding: gzip" + CRLF
    header += "Content-Length: {}".format(size) + CRLF
    header += "Connection: close" + CRLF + CRLF
    _send_all(cl, header.encode())
    with open(path, "rb") as f:
        while True:
            _feed()
            chunk = f.read(_SEND_FILE_CHUNK)
            if not chunk:
                break
            _send_all(cl, chunk)

