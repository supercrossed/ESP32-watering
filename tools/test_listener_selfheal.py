"""The listener self-heal must fire on a real fault and stay silent otherwise.

Both halves matter. A rebuild that never fires leaves the original bug; a
rebuild that fires on the normal idle path would churn the socket every
poll and break a perfectly good server. The distinction is the errno:
EAGAIN means "nothing pending", anything else means the listener is broken
- and conflating the two is exactly how a dead listener went unnoticed.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types

BASE = r"c:\Users\super\Documents\ESP32 watering\src"
sys.path.insert(0, BASE)

clock = {"ms": 0}
ft = types.ModuleType("time")
ft.ticks_ms = lambda: clock["ms"]
ft.ticks_diff = lambda a, b: a - b
ft.time = lambda: 1.8e9
ft.sleep = lambda s: None
ft.sleep_ms = lambda m: None
ft.localtime = lambda *a: (2026, 9, 6, 12, 0, 0, 4, 249)
sys.modules["time"] = ft
sys.modules["ujson"] = json
sys.modules["machine"] = types.ModuleType("machine")

net = types.ModuleType("network")


class _W:
    def __init__(self, *a):
        pass

    def isconnected(self):
        return True

    def ifconfig(self):
        return ("192.168.1.225", "255.255.255.0", "192.168.1.254", "192.168.1.254")

    def status(self, k=None):
        return -50


net.WLAN = _W
net.STA_IF = 0
sys.modules["network"] = net

# a socket module whose listener we can break on demand
sockmod = types.ModuleType("socket")
sockmod.SOL_SOCKET = 1
sockmod.SO_REUSEADDR = 4
created = []


class FakeListener:
    def __init__(self):
        self.closed = False
        self.mode = "eagain"     # eagain | broken | ready
        created.append(self)

    def setsockopt(self, *a):
        pass

    def bind(self, a):
        pass

    def listen(self, n):
        pass

    def setblocking(self, b):
        pass

    def accept(self):
        if self.mode == "eagain":
            raise OSError(11, "EAGAIN")
        if self.mode == "broken":
            raise OSError(9, "EBADF")
        return FakeConn(), ("1.2.3.4", 5)

    def close(self):
        self.closed = True


class FakeConn:
    def settimeout(self, t):
        pass

    def recv(self, n):
        return b""

    def send(self, d):
        return len(d)

    def close(self):
        pass


sockmod.socket = lambda *a: FakeListener()
sys.modules["socket"] = sockmod

sel = types.ModuleType("select")
sel.select = lambda r, w, x, t=0: (list(r), [], [])   # always "readable"
sys.modules["select"] = sel


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


work = tempfile.mkdtemp()
os.chdir(work)
load("state", BASE + r"\state.py")
ss = load("settings_store", BASE + r"\settings_store.py")


class Cfg:
    WIFI_SSID = "N"
    WIFI_PASSWORD = "p"
    I2C_SCL_PIN = 9
    I2C_SDA_PIN = 8
    ADS1115_ADDRESS = 0x48
    VALVES = [{"name": "valve1", "pin": 4, "active_high": True, "flow_meter_pin": None}]
    ZONES = []
    DAILY_WATER_HOUR = 6
    DAILY_WATER_MINUTE = 0
    DAILY_WATER_DURATION_SEC = 300
    SUPPLEMENTAL_WATER_DURATION_SEC = 60
    MIN_SUPPLEMENTAL_INTERVAL_SEC = 7200
    POST_DAILY_LOCKOUT_SEC = 14400
    TZ_OFFSET_SEC = -18000
    STATUS_LED_PIN = 48


sys.modules["config"] = Cfg
ss.SETTINGS_FILE = "s.json"
ss.load(Cfg)
web = load("web", BASE + r"\web.py")

fails = 0


def check(label, cond, detail=""):
    global fails
    if cond:
        print("  ok   {}".format(label))
    else:
        fails += 1
        print("  FAIL {}  {}".format(label, detail))


# ---- start ------------------------------------------------------------
assert web.start_server()
check("server_running() true once listening", web.server_running())
first = created[-1]
base_restarts = web.server_stats()[1]

# ---- EAGAIN is the normal idle path: no rebuild -----------------------
first.mode = "eagain"
for _ in range(20):
    clock["ms"] += 200
    web.poll_once(0)
check("EAGAIN does not rebuild the listener",
      web.server_stats()[1] == base_restarts and not first.closed,
      "restarts={} closed={}".format(web.server_stats()[1], first.closed))

# ---- a real accept error rebuilds -------------------------------------
first.mode = "broken"
clock["ms"] += 200
web.poll_once(0)
check("a real accept() error rebuilds the listener",
      web.server_stats()[1] == base_restarts + 1 and first.closed,
      "restarts={} closed={}".format(web.server_stats()[1], first.closed))
check("a fresh listener replaced it", created[-1] is not first and web.server_running())

# ---- long silence rebuilds (the case with no error to react to) -------
second = created[-1]
second.mode = "eagain"
# the first poll after a rebuild only starts the clock; the rebuild can
# only happen on a later poll, once the interval has actually elapsed
clock["ms"] += 200
web.poll_once(0)
before = web.server_stats()[1]
clock["ms"] += web._LISTENER_IDLE_REBUILD_MS + 1000
web.poll_once(0)
check("a long silence rebuilds the listener",
      web.server_stats()[1] == before + 1,
      "restarts={} (was {})".format(web.server_stats()[1], before))

# ---- but a busy server is never rebuilt -------------------------------
third = created[-1]
third.mode = "ready"          # every poll accepts a connection
before = web.server_stats()[1]
for _ in range(30):
    clock["ms"] += 60000      # a minute between polls, but traffic each time
    web.poll_once(0)
check("a server that keeps accepting is never rebuilt",
      web.server_stats()[1] == before,
      "restarts={} (was {})".format(web.server_stats()[1], before))
check("accepted connections are counted", web.server_stats()[0] >= 30,
      "accepted={}".format(web.server_stats()[0]))

os.chdir(r"c:\Users\super\Documents\ESP32 watering")
shutil.rmtree(work, ignore_errors=True)
print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
