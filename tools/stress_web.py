#!/usr/bin/env python3
"""Reproduce (or prove fixed) the concurrent-connection watchdog hang.

    python tools/stress_web.py 192.168.1.225 [minutes]

WHY THIS EXISTS
---------------
The web server hangs the main loop under concurrent connections. It is not
memory - it reproduces on an ESP32-S3 with 8MB of C heap free - and it is
not one bad call: traced hangs stopped at different points, once after
close() returned and once between two successful sends. The common factor
is blocking sockets plus overlapping connections. See docs/CHANGELOG.md
(2026-09-06) and the Reliability section of CLAUDE.md.

Sequential requests never trigger it (220 in a row, flat heap), so a test
that fetches one URL at a time will pass on broken firmware and tell you
nothing. This deliberately opens connections in PARALLEL, the way a real
browser does on every page load - which is what made the fault look random
and unreproducible for so long.

MEASURED BASELINE, blocking-socket server, ESP32-S3, 6 minutes of this
load: 4 watchdog reboots. That is the number to beat.

HOW IT DETECTS A HANG
---------------------
No serial cable needed: /api/status reports uptime_sec, so uptime going
BACKWARDS means the board rebooted under us. The watchdog is the only
thing that reboots it during a run, so a drop in uptime is a hang. The
script also reports failures, but failures alone are not the verdict -
a device that refuses requests and stays up is far better than one that
reboots, and only the reboot count says which happened.

PASS = zero uptime resets. Failures should be low but are secondary.
"""
import json
import socket
import sys
import threading
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.225"
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0

# One page load: the dashboard plus every card it asks for.
PAGE = ["/"]
CARDS = ["/api/status", "/api/settings", "/api/schedules", "/api/valves",
         "/api/zones", "/api/hardware", "/api/pinmap", "/api/events",
         "/api/history", "/api/wifi"]

_lock = threading.Lock()
stats = {"ok": 0, "busy": 0, "fail": 0}


def request(path, timeout=10):
    """One request on its own connection, like a browser."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((HOST, 80))
        s.sendall(
            "GET {} HTTP/1.1\r\nHost: {}\r\nAccept-Encoding: gzip\r\n"
            "Connection: close\r\n\r\n".format(path, HOST).encode())
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf
    except OSError:
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def record(path):
    body = request(path)
    with _lock:
        if body is None:
            stats["fail"] += 1
        elif body.startswith(b"HTTP/1.1 503"):
            stats["busy"] += 1
        else:
            stats["ok"] += 1


def uptime():
    """Seconds since boot, or None if the device did not answer."""
    body = request("/api/status", timeout=8)
    if not body or body.startswith(b"HTTP/1.1 503"):
        return None
    try:
        return int(json.loads(body.partition(b"\r\n\r\n")[2]).get("uptime_sec", 0))
    except (ValueError, AttributeError):
        return None


def wait_for_device(limit=180):
    print("waiting for {} ...".format(HOST), end="", flush=True)
    start = time.time()
    while time.time() - start < limit:
        up = uptime()
        if up is not None and up > 15:
            print(" up ({}s since boot)".format(up))
            return True
        print(".", end="", flush=True)
        time.sleep(4)
    print(" never came up")
    return False


stop = threading.Event()


def loader(paths, pause):
    """Fire a whole batch of requests AT ONCE, repeatedly."""
    while not stop.is_set():
        threads = [threading.Thread(target=record, args=(p,)) for p in paths]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if stop.wait(pause):
            return


if not wait_for_device():
    sys.exit(2)

print()
print("{} minutes of PARALLEL page loads against {}".format(MINUTES, HOST))
print("(a browser does this on every refresh; sequential tests miss the bug)")
print()

workers = [
    threading.Thread(target=loader, args=(PAGE + CARDS[:5], 1.0)),
    threading.Thread(target=loader, args=(CARDS[5:], 1.0)),
]
for w in workers:
    w.start()

reboots = 0
last_up = uptime() or 0
started = time.time()
deadline = started + MINUTES * 60
try:
    while time.time() < deadline:
        time.sleep(20)
        up = uptime()
        note = ""
        if up is None:
            note = "  (not answering)"
        elif up < last_up:
            reboots += 1
            note = "  *** REBOOTED (uptime {} -> {}) ***".format(last_up, up)
        if up is not None:
            last_up = up
        with _lock:
            print("  [{:4.0f}s] ok={:<5} busy={:<4} fail={:<5} reboots={}{}".format(
                time.time() - started,
                stats["ok"], stats["busy"], stats["fail"], reboots, note),
                flush=True)
finally:
    stop.set()
    for w in workers:
        w.join(timeout=10)

print()
print("  requests: ok={} busy(503)={} failed={}".format(
    stats["ok"], stats["busy"], stats["fail"]))
print("  watchdog reboots during the run: {}".format(reboots))
print()
if reboots == 0:
    print("PASS - survived concurrent load without a single reboot")
    print("       (blocking-socket baseline was 4 reboots in 6 minutes)")
    sys.exit(0)
print("FAIL - {} reboot(s); the loop is still blocking under concurrency".format(reboots))
sys.exit(1)
