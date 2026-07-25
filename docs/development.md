# Development

[<- Docs index](README.md)

## Architecture

```
boot.py             OTA rollback guard - runs before main.py
main.py             boot sequence + main loop (executed, not imported)
config.py           first-boot defaults (gitignored - see config.example.py)
settings_store.py   runtime settings + migrations -> settings.json
state.py            shared in-memory state, capped ring buffers
ads1x15.py          minimal ADS1115 driver
env_sensors.py      AHT20 + BMP280 drivers
moisture.py         raw ADC -> percent
valve.py            solenoid control + per-valve safety cutoff
wifi.py             connect / reconnect helpers
wifi_setup.py       captive portal + runtime rescue hotspot
web.py              HTTP server + JSON API
updater.py          OTA: manifest, verify, atomic install
index.html          the dashboard (single page)
```

`config.py` holds first-boot defaults only. After the first boot,
`settings.json` on the device takes priority - so changing `config.py` on a
device that's already been set up usually does nothing.

The main loop, in order: feed watchdog -> valve safety cutoffs -> close
finished waterings -> soak recheck -> moisture + env (15s) -> schedules (20s)
-> WiFi check (30s) -> rescue DNS -> NTP retry -> `web.poll_once()` -> CPU
load calc -> optional nightly reboot -> `gc.collect()`.

---

## The two-heap problem

**This is the single most important constraint in the project.**

The ESP32 has two memory pools:

| Heap | Used by | Visible as |
|---|---|---|
| MicroPython GC heap | Your Python objects | `gc.mem_free()` |
| ESP-IDF C heap | WiFi driver, lwIP, I2C driver | `esp32.idf_heap_info()` |

MicroPython **grows its GC heap out of the C heap** on demand, and never
gives it back.

Compiling this project's modules on-device once left **764 bytes** of C heap
at loop start. The symptoms looked nothing like a memory problem:

```
E (35649) wifi:fail to alloc timer, type=1
E (41079) i2c: i2c command link malloc error
OSError: -203                       (getaddrinfo, from lwIP)
```

...while `gc.mem_free()` reported ~90KB free. The network was dying because
the *other* heap was empty.

**Shipping `.mpy` bytecode moves compilation to build time.** The same board
now idles at ~33KB C heap free. This is why `build_mpy.ps1` exists and why
uploading raw `.py` modules is a regression, not a shortcut.

Both figures print at boot and once a minute, and appear in `/api/status` as
`idf_free` / `idf_largest`.

### Consequences when editing

**Never build large strings.** Stream instead. See `_send_file()`,
`_send_history()`, and `_stream_multipart_to_disk()` in `web.py`.

**`index.html` is streamed from flash in 1KB chunks.** It used to be a Python
string literal in `web.py`; a ~34KB literal needs one contiguous allocation
to compile and reliably failed with `MemoryError` on a fragmented heap.

**Keep `index.html` ASCII-only.** Use `&deg;`, `&rarr;`, inline SVG - not
emoji or typographic characters. Non-ASCII bytes have corrupted in transit
and broken the page with `Uncaught SyntaxError: Invalid or unexpected token`.

**The web server must never block.** It's hand-rolled and synchronous, polled
from the main loop via `web.poll_once()`. The same loop handles valve safety
cutoffs - a blocking read there could leave water running.

**Sockets can't hold attributes.** MicroPython sockets reject dynamic
attribute assignment (`sock._leftover = ...` raises). Thread state through
explicit parameters instead.

**History is capped and slim.** A 720-point full-fidelity buffer once
consumed the entire heap over ~3 hours. It's now 180 points of
`{name, percent}`, decimated to one per minute.

---

## Building

```powershell
pip install mpy-cross
.\build_mpy.ps1
```

Produces `build/` with `.mpy` modules plus `main.py`, `boot.py`, `config.py`,
`index.html`, `manifest.json`, and `version.json`.

`main.py` stays uncompiled (it's executed by name at boot, not imported) and
`config.py` stays plain text so it's hand-editable in Thonny in the field.

The script also regenerates `config.example.py` from `config.py` with
credentials scrubbed, and **aborts** if a real credential survives scrubbing.

After uploading, `.\build_mpy.ps1 -Deployed` records what's on the device so
the next build reports what changed.

---

## Testing

There's no hardware in CI. At minimum, keep everything parseable:

```bash
# every .py must parse (they won't RUN under CPython - they import machine)
for f in *.py; do python3 -c "import ast; ast.parse(open('$f').read())"; done
```

For the dashboard, extract the `<script>` block from `index.html` and:

```bash
node --check dash.js
```

Also worth checking: zero non-ASCII bytes in `index.html`, and balanced
`<div>` counts.

### Testing device modules under CPython

Modules that don't touch hardware directly can be exercised by stubbing
MicroPython's built-ins and loading by path:

```python
import sys, types, importlib.util, json
sys.modules['machine'] = types.ModuleType('machine')
sys.modules['ujson'] = json
# ... stub network, socket, ssl as needed

def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
```

This is how the updater (hash verification, atomic install, path-traversal
rejection), the rollback guard, the rescue AP lifecycle, and the HTTP
handlers are verified. Driving `web._handle()` with a fake socket object
exercises the real request path end to end.

---

## Gotchas found the hard way

- **`ap.config()` after `ap.active(True)`** restarts the AP without reliably
  restarting its DHCP server. Phones then associate but get no lease and
  report a generic "unable to join." Configure *before* activating.
- **An open AP needs `password=""` AND `authmode=AUTH_OPEN`** together.
  Setting authmode alone can leave a keyless WPA2 AP.
- **AP and STA share one radio.** A station scanning for a dead router drags
  the AP off-channel mid-handshake.
- **`essid=` vs `ssid=`** - MicroPython renamed this. Try both.
- **NTP jumps the clock.** Rebase any timestamp captured before
  `ntptime.settime()` or uptime reads as ~26 years.
- **PowerShell's `-Encoding utf8` writes a BOM**, which MicroPython's
  `json.loads()` rejects. Use `[IO.File]::WriteAllText` with
  `UTF8Encoding($false)`.
- **A `.py` shadows its `.mpy`.** Always delete the counterpart.

---

## Roadmap

- Flow-meter volume-based watering (pulse counting; config/UI groundwork done)
- Rain-skip using the rain sensor and forecast data
- Sensor-fault detection + a daily watering budget
- Authentication for the dashboard
- Optional Raspberry Pi orchestration layer (would poll the JSON API; no
  protocol work needed)
