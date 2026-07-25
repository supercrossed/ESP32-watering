# Contributing

[<- Docs index](README.md)

## Repo layout

```
README.md            quick start only - keep it short
LICENSE
build_mpy.ps1        build script
src/                 all device source (edit here)
build/               generated artifacts - what the ESP32 runs, what OTA publishes
docs/                these guides
hardware/            3D models, enclosures, reference material
```

Edit `src/`, run `.\build_mpy.ps1`, upload `build/`. Never edit `build/` by
hand - it's regenerated on every build.

---

## The checklist

**Every change that alters behavior must update the docs in the same
commit.** Nothing enforces this automatically, and drift has already caused
two shipped bugs (a missing OTA config block, and an `UPDATE_REPO` pointing
at a nonexistent repo).

Before committing, ask which of these your change touches:

- [ ] **`CLAUDE.md`** - architecture, constraints, API list, reliability
      measures. Update if you added a module, endpoint, or constraint.
- [ ] **`README.md`** - only if install, first-run, or the parts list
      changed. Keep it ~150 lines.
- [ ] **`docs/*.md`** - the guide covering the area you changed:

  | You changed | Update |
  |---|---|
  | Watering behavior, thresholds, cooldowns | `watering.md` |
  | Pins, sensors, wiring | `hardware.md` |
  | Setup flow, calibration | `setup.md` |
  | A dashboard card | `dashboard.md` |
  | An endpoint | `api.md` |
  | The updater or build | `ota-updates.md` |
  | Architecture, memory, testing | `development.md` |
  | Fixed a bug users might hit | `troubleshooting.md` |

- [ ] **`docs/CHANGELOG.md`** - a dated entry for anything user-visible.
- [ ] **`src/config.py`** - if you added a setting, the build regenerates
      `src/config.example.py` automatically. Verify it appears there.

---

## Verification before committing

```bash
# 1. every .py parses (they won't RUN under CPython - they import machine)
for f in src/*.py; do python3 -c "import ast; ast.parse(open('$f').read())"; done

# 2. dashboard JS: extract the <script> block from src/index.html
node --check dash.js

# 3. index.html must be ASCII-only, with balanced divs
python3 -c "
h=open('src/index.html','rb').read()
print('non-ascii:', sum(1 for b in h if b>127))
print('divs:', h.count(b'<div'), h.count(b'</div>'))"

# 4. the build must succeed and produce a parseable manifest
./build_mpy.ps1
python3 -c "import json; json.load(open('build/manifest.json'))"
```

**Verify against real artifacts, not reconstructions.** Both shipped bugs
passed their unit tests. One was a UTF-8 BOM that only PowerShell produced;
the other only appeared in a fresh clone. When it matters, clone the repo to
a temp directory and build it as a stranger would.

---

## Testing device modules

There's no hardware in CI. Modules are testable under CPython by stubbing
MicroPython's built-ins:

```python
import sys, types, importlib.util, json
sys.modules['machine'] = types.ModuleType('machine')
sys.modules['ujson'] = json
# stub network / socket / ssl as the module needs

def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
```

Driving `web._handle()` with a fake socket exercises the real request path.
This is how the updater, rollback guard, rescue AP, and HTTP endpoints are
verified.

---

## Non-negotiable constraints

These have each caused a real failure. See
[development.md](development.md#the-two-heap-problem) for the full story.

1. **Ship `.mpy`, never raw `.py` modules.** On-device compilation starves
   the ESP-IDF C heap and kills WiFi.
2. **Never build large strings.** Stream instead.
3. **`index.html` stays ASCII-only.** Use HTML entities and inline SVG.
4. **The web server must never block.** The same loop closes valves.
5. **Never commit `src/config.py`.** It's gitignored; the build derives
   `src/config.example.py` from it with credentials scrubbed.

---

## Publishing an OTA update

```powershell
.\build_mpy.ps1
git add -A
git commit -m "describe the change"
git push
```

Committing `build/` is what publishes a release - every planter picks it up
on its next daily check. Test on one device before pushing to a fleet: the
rollback guard catches a build that won't *boot*, not one that boots fine
and waters wrongly.
