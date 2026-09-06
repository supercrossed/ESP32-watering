"""Minimal reproduction of the listener stall - NOT planter firmware.

RESULT: this reproduces the fault. A ~40 line HTTP server containing none
of the planter code stalls exactly the same way - serves a few hundred
requests, stops accepting for tens of seconds while the board stays up,
then recovers on its own. Measured: 332 served / 352 failed over 7 minutes
of page-load-shaped load, and after the load stopped it timed out four
times in a row and then answered in 0.02s.

That matters because it rules out the entire firmware. There is no
watchdog here, no I2C, no status LED, no flash writes, no settings, no
gzip, no streaming, no keep-alive, no asyncio - just accept, read, write a
fixed body, close. So the fault is in MicroPython/lwIP on this platform,
and no amount of rewriting web.py or main.py will fix it.

HOW TO RUN
    Hold the board at the REPL so main.py never starts (Ctrl-C through the
    boot window) - that also means no watchdog is armed - then:
        mpremote connect COM6 run tools/minimal_server_repro.py
    and drive it with parallel page-load-shaped requests.

Reads WiFi credentials from the device's own wifi.json, so none appear
here.

A caution from writing it: the first version used `b"..." % n`, which
MicroPython does not implement. It died with `NotImplementedError: opcode`
and looked exactly like the wedge being hunted. Any repro script needs its
own failures to be loud, or it will be mistaken for the bug.
"""
import gc
import network
import select
import socket
import time
import ujson

# ---- WiFi (credentials come from the device's own wifi.json) ----------
with open("wifi.json") as f:
    creds = ujson.load(f)

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
if not wlan.isconnected():
    wlan.connect(creds["ssid"], creds.get("password", ""))
    for _ in range(40):
        if wlan.isconnected():
            break
        time.sleep(0.5)
try:
    wlan.config(pm=network.WLAN.PM_NONE)      # same as the real firmware
except Exception:
    pass
print("minimal server on", wlan.ifconfig()[0])

# A body roughly the size of the gzipped dashboard, so the traffic profile
# resembles a real page load rather than a toy request.
BIG = b"x" * 29000
# NOTE: no %-formatting on bytes - MicroPython does not implement it
# (it raises NotImplementedError: opcode, which killed the first run
# of this script and looked exactly like the wedge being hunted).
SMALL = b'{"ok":true,"minimal":1}'

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 80))
srv.listen(4)
srv.setblocking(False)

served = 0
errors = 0
last_report = time.ticks_ms()
started = time.ticks_ms()

while True:
    try:
        r, _, _ = select.select([srv], [], [], 0.2)
    except OSError as e:
        print("select error:", e)
        errors += 1
        continue
    if r:
        try:
            cl, addr = srv.accept()
        except OSError as e:
            # Record what accept() actually raises - the real firmware
            # cannot tell these apart, which is half the open bug.
            errors += 1
            if errors < 10 or errors % 50 == 0:
                print("accept error #{}: {}".format(errors, e.args))
            continue
        try:
            cl.settimeout(3)
            req = cl.recv(512)
            path = b"/"
            if req:
                try:
                    path = req.split(b" ")[1]
                except Exception:
                    pass
            body = BIG if path == b"/" else SMALL
            hdr = ("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                   "Content-Length: {}\r\nConnection: close\r\n\r\n"
                   .format(len(body)))
            cl.send(hdr.encode())
            sent = 0
            while sent < len(body):
                n = cl.send(body[sent:sent + 512])
                if not n:
                    break
                sent += n
            served += 1
        except Exception as e:
            errors += 1
            print("handler error #{}: {}: {}".format(errors, type(e).__name__, e))
        finally:
            try:
                cl.close()
            except Exception:
                pass

    now = time.ticks_ms()
    if time.ticks_diff(now, last_report) >= 15000:
        last_report = now
        gc.collect()
        print("[{:5.0f}s] served={} errors={} gc_free={}".format(
            time.ticks_diff(now, started) / 1000, served, errors, gc.mem_free()))
