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

_server_sock = None
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


# Time spent waiting in select() is the system's idle time - everything
# else is work. main.py reads this every few seconds to compute the
# "CPU load" figure shown in the dashboard.
_idle_ms = 0


def take_idle_ms():
    global _idle_ms
    v = _idle_ms
    _idle_ms = 0
    return v


def poll_once(timeout=0.2):
    """Call this frequently from the main loop. Handles at most one
    incoming connection per call so it never stalls the rest of the app."""
    global _idle_ms
    if _server_sock is None:
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
    try:
        cl, addr = _server_sock.accept()
    except OSError:
        return
    try:
        _handle(cl)
    except Exception as e:
        print("web handler error:", e)
    finally:
        try:
            cl.close()
        except OSError:
            pass


def _handle(cl):
    cl.settimeout(8.0)
    header_part, leftover = _read_headers(cl)
    if not header_part:
        return
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
        saved, rejected = _stream_multipart_to_disk(cl, boundary, content_length, leftover)
        state.log_event("code_upload", "saved={} rejected={}".format(saved, rejected))
        _send(cl, 200, "application/json", json.dumps({"ok": True, "saved": saved, "rejected": rejected}))
        return

    body = _read_body(cl, header_part, leftover)

    if path == "/" and method == "GET":
        _send_file(cl, INDEX_HTML_PATH, "text/html")
    elif path == "/api/status" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(_status_payload()))
    elif path == "/api/history" and method == "GET":
        _send_history(cl)
    elif path == "/api/events" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(state.get_events()))
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
        duration = int(params.get("duration", settings["supplemental_duration_sec"]))
        valve_name = params.get("valve", _default_valve_name)
        ok = _trigger_watering_cb(valve_name, duration, "manual_web") if valve_name else False
        _send(cl, 200, "application/json", json.dumps({"ok": bool(ok)}))
    elif path == "/api/water/all" and method == "POST":
        # Master quick-water: every valve runs, one at a time, in order.
        settings = settings_store.get()
        duration = int(params.get("duration", settings["supplemental_duration_sec"]))
        names = [v["name"] for v in settings["hardware"].get("valves", [])]
        ok = _trigger_valves_cb(names, duration, "manual_web_all") if names else False
        _send(cl, 200, "application/json", json.dumps({"ok": bool(ok), "valves": names}))
    elif path == "/api/zone/trigger" and method == "POST":
        # Water a whole zone: opens its valves one at a time, in order,
        # each for the zone's own run time (fallback: supplemental default).
        settings = settings_store.get()
        zone_name = params.get("zone", "")
        if "duration" in params:
            duration = int(params["duration"])
        else:
            duration = settings.get("zone_durations", {}).get(zone_name) or settings["supplemental_duration_sec"]
        valve_names = settings["hardware"].get("zone_valves", {}).get(zone_name, [])
        ok = _trigger_valves_cb(valve_names, duration, "manual_web_zone") if valve_names else False
        _send(cl, 200, "application/json", json.dumps({"ok": bool(ok), "valves": valve_names}))
    elif path == "/api/settings" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(settings_store.get()))
    elif path == "/api/settings" and method == "POST":
        _apply_settings_patch(params, body)
        _send(cl, 200, "application/json", json.dumps(settings_store.get()))
    elif path == "/api/schedules" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(settings_store.get().get("schedules", [])))
    elif path == "/api/schedules" and method == "POST":
        # Body is the full replacement list of schedules as JSON.
        _apply_schedules(body)
        _send(cl, 200, "application/json", json.dumps(settings_store.get().get("schedules", [])))
    elif path == "/api/pinmap" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(_pinmap_payload()))
    elif path == "/api/i2c/scan" and method == "GET":
        # Live I2C bus scan - returns the address of every device that
        # answers. ADS1115 boards show up as 0x48-0x4B depending on how
        # their ADDR pin is wired. Lets the UI show what's really connected.
        found = []
        if _i2c is not None:
            try:
                found = _i2c.scan()
            except Exception as e:
                print("i2c scan failed:", e)
        _send(cl, 200, "application/json", json.dumps({"found": found}))
    elif path == "/api/valves" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(settings_store.get()["hardware"].get("valves", [])))
    elif path == "/api/valves" and method == "POST":
        _apply_valves_patch(body)
        _send(cl, 200, "application/json", json.dumps({"ok": True, "rebooting": True}))
        cl.close()
        state.log_event("reboot", "applying valve config change")
        time.sleep(1)
        machine.reset()
    elif path == "/api/zones" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(_zones_payload()))
    elif path == "/api/zones" and method == "POST":
        # Body is the full replacement zone list. Zones are live config
        # (read fresh every moisture check) - no reboot needed.
        _apply_zones_patch(body)
        _send(cl, 200, "application/json", json.dumps(_zones_payload()))
    elif path == "/api/hardware" and method == "GET":
        _send(cl, 200, "application/json", json.dumps(settings_store.get()["hardware"]))
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
        payload = json.dumps({"planter_config": 1, "settings": settings_store.get()})
        _send_download(cl, payload, "planter-config.json")
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
            wifi.save_creds(ssid, password)
            _send(cl, 200, "application/json", json.dumps({"ok": True, "rebooting": True}))
            cl.close()
            state.log_event("reboot", "wifi credentials changed to " + ssid)
            time.sleep(1)
            machine.reset()
    elif path == "/api/update/check" and method == "POST":
        # Ask the repo what's available. Cheap, no files touched.
        if _update_cb is None:
            _send(cl, 503, "application/json",
                  json.dumps({"ok": False, "error": "updater not available"}))
        else:
            _feed()  # a TLS handshake can take a few seconds
            res = _update_cb(False)
            _send(cl, 200, "application/json", json.dumps(res))
    elif path == "/api/update/apply" and method == "POST":
        # Download + verify + install, then reboot. The response is sent
        # BEFORE the work starts, because the reboot kills this socket -
        # the dashboard polls /api/status afterwards to see the new version.
        if _update_cb is None:
            _send(cl, 503, "application/json",
                  json.dumps({"ok": False, "error": "updater not available"}))
        else:
            _send(cl, 200, "application/json",
                  json.dumps({"ok": True, "started": True}))
            try:
                cl.close()
            except OSError:
                pass
            _feed()
            state.log_event("update", "update requested from dashboard")
            _update_cb(True)  # reboots on success
        return
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
        cl.send(b"HTTP/1.1 302 Found\r\nLocation: /\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


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


def _content_length(header_part):
    for line in header_part.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":", 1)[1].strip())
    return 0


def _read_body(cl, header_part, leftover):
    """Buffer the full body in RAM - fine for the small JSON payloads every
    route except /api/upload sends. Uploads use _stream_multipart_to_disk
    instead, which never holds more than one chunk in memory."""
    content_length = _content_length(header_part)
    body = leftover
    while len(body) < content_length:
        _feed()
        chunk = cl.recv(2048)
        if not chunk:
            break
        body += chunk
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


def _stream_multipart_to_disk(cl, boundary, content_length, leftover):
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
                _feed()
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
                _feed()
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
            _feed()
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


def _pinmap_payload():
    """Return the role/status of every GPIO 0-39 so the UI can draw a full
    reference table. Moisture sensors are NOT here - they live on ADS1115
    channels, reported separately under 'ads_channels'."""
    hw = settings_store.get()["hardware"]

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
            hw[key] = int(params[key])

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

    hw["zone_channels"] = zone_channels
    hw["zone_valves"] = zone_valves
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
        "time_synced": state.time_synced,
        "wifi_connected": wifi.is_connected(),
        "env": state.env,
        "mem_free": gc.mem_free() if hasattr(gc, "mem_free") else None,
        "mem_alloc": gc.mem_alloc() if hasattr(gc, "mem_alloc") else None,
        "cpu_percent": state.cpu_percent,
        # IDF C-heap (WiFi/lwIP/I2C drivers) - separate from the GC heap
        # above; when THIS runs out the network dies while Python keeps going
        "idf_free": state.idf_free,
        "idf_largest": state.idf_largest,
        # OTA updater status (see updater.py) - drives the dashboard's
        # Firmware Updates card
        "update": {
            "version": _installed_version(),
            "last_check": state.last_update_check,
            "last_install": state.last_update_install,
            "available": state.update_available,
            "error": state.update_error,
            "busy": state.update_in_progress,
        },
    }


def _installed_version():
    try:
        with open("version.json") as f:
            return json.load(f).get("version")
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


def _send_history(cl):
    """Serialize the history point-by-point instead of one json.dumps of
    the whole list - that single string was the largest allocation in the
    firmware and reliably fails on a fragmented heap after hours of
    uptime, taking the WiFi stack down with it."""
    parts = []
    total = 2  # the surrounding [ ]
    for pt in state.get_moisture_history():
        s = json.dumps(pt)
        if parts:
            total += 1  # comma
        parts.append(s)
        total += len(s)
    header = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n".format(total)
    cl.send(header.encode())
    cl.send(b"[")
    for i, s in enumerate(parts):
        _feed()
        if i:
            cl.send(b",")
        cl.send(s.encode())
    cl.send(b"]")


def _send(cl, status, content_type, body):
    if isinstance(body, str):
        body = body.encode()
    header = "HTTP/1.1 {} OK\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n".format(
        status, content_type, len(body)
    )
    cl.send(header.encode())
    cl.send(body)


def _send_download(cl, body, filename):
    """Like _send but with a Content-Disposition header so the browser
    saves the response as a file instead of displaying it."""
    if isinstance(body, str):
        body = body.encode()
    header = (
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        "Content-Disposition: attachment; filename=\"{}\"\r\n"
        "Content-Length: {}\r\nConnection: close\r\n\r\n"
    ).format(filename, len(body))
    cl.send(header.encode())
    cl.send(body)


_SEND_FILE_CHUNK = 1024


def _send_file(cl, path, content_type):
    """Stream a file straight from flash in small chunks instead of
    loading it into one big string first - see the note at the top of this
    file for why (a single large contiguous allocation reliably fails on a
    fragmented ESP32 heap)."""
    try:
        size = os.stat(path)[6]
    except OSError:
        _send(cl, 404, "text/plain", "not found")
        return
    header = "HTTP/1.1 200 OK\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n".format(
        content_type, size
    )
    cl.send(header.encode())
    with open(path, "rb") as f:
        while True:
            _feed()
            chunk = f.read(_SEND_FILE_CHUNK)
            if not chunk:
                break
            cl.send(chunk)

