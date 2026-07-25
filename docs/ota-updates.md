# Over-the-Air Updates

[<- Docs index](README.md)

The planter updates itself from this GitHub repo. No Raspberry Pi, no host,
no build server.

## How it works

`build_mpy.ps1` generates a `manifest.json` listing every device file with
its SHA-256 hash and a version string. The device fetches that manifest,
compares each hash against the file it already has, and downloads **only
what differs**.

```
GitHub repo                    ESP32
-----------                    -----
manifest.json  --------------> fetch, compare hashes
web.mpy        --------------> download only if changed
                               verify SHA-256
                               write to web.mpy.new
                               (all files verified?)
                               rename .new -> live, keep .bak
                               reboot
```

## Safety properties

Each of these exists because of a specific way this could go wrong:

| Property | Prevents |
|---|---|
| Streams to flash 512 bytes at a time | Running out of RAM on a 14KB file |
| Downloads to `.new` temp files; nothing swaps until **every** file verifies | A dropped connection leaving half an update installed |
| SHA-256 verified per file | A truncated or corrupted download being run |
| Replaced files kept as `.bak`, restored after 3 failed boots | A bad build bricking a planter in a garden |
| `config.py`, `wifi.json`, `settings.json` never updatable | An update wiping your credentials or calibration |
| Refuses to run while a valve is open | Files swapping mid-watering |
| A failed check is "try tomorrow", not an error state | A GitHub outage taking the planter down |

## Configuration

In `config.py`:

```python
UPDATE_REPO = "supercrossed/ESP32-watering"
UPDATE_BRANCH = "main"
UPDATE_CHECK_HOUR = 4        # daily check at 4am local time; None disables
UPDATE_AUTO_INSTALL = False  # notify only; you press "Update Now"
```

`UPDATE_AUTO_INSTALL = False` is the default deliberately: this device
controls water valves, so *you* choose when it reboots. Set it to `True` for
true set-and-forget operation.

## Using it

The **Firmware** card at the bottom of the dashboard shows the installed
version, when it last checked, and when it last updated.

- **Check for Updates** - fetches the manifest, reports what changed
- **Update Now** - appears when an update is available; downloads, verifies,
  installs, and reboots. The dashboard reloads itself once the planter
  returns.

The daily automatic check does the same thing at `UPDATE_CHECK_HOUR`,
skipping if a valve is open or queued.

## Publishing an update (maintainer)

```powershell
.\build_mpy.ps1
git add -A
git commit -m "describe the change"
git push
```

Committing the regenerated `build/manifest.json` and `.mpy` files is what
publishes a release. Every planter picks it up on its next check.

The version string is a build timestamp (`2026.07.24.2247`), so it always
moves forward.

> Test an update on one device before pushing it to a fleet. The rollback
> guard handles a build that won't *boot*, but not one that boots fine and
> waters wrongly.

## Rollback

`boot.py` runs before `main.py` on every boot and counts boots in
`boot_count.txt`. Once `main.py` has run stably for 60 seconds it clears that
counter and deletes the `.bak` files.

If the counter reaches 3 without a stable run, `boot.py` restores every
`.bak` file and reboots - undoing the last update automatically.

```
boot 1  counter=1   (new build crashes)
boot 2  counter=2   (crashes again)
boot 3  counter=3   (crashes again)
boot 4  counter=4 > 3  -> restore .bak files, reset counter, reboot
        -> planter comes back on the previous known-good build
```

Nothing in the rollback path touches `config.py`, `wifi.json`, or
`settings.json`, so credentials and settings always survive.

## TLS notes

GitHub is HTTPS-only, and a TLS handshake needs roughly **30-45KB of
contiguous memory** from the ESP-IDF C heap - the same pool the WiFi stack
uses. On a board with an application already running, that allocation is not
guaranteed.

The updater collects garbage before connecting and treats a failed handshake
as "try again tomorrow" rather than an error.

If TLS proves unreliable on your board, mirror the files over plain HTTP and
point the updater at it:

```python
UPDATE_BASE_URL = "http://192.168.1.50/planter/"
```

Everything else works identically - the manifest, hashing, atomic install,
and rollback don't care about the transport. Any static file host works: a
Pi, a NAS, or a Cloudflare Worker mirroring the repo.

## What gets updated

Everything in `build/` except `config.py`:

```
main.py  boot.py  index.html  manifest.json  version.json
state.mpy  settings_store.mpy  ads1x15.mpy  moisture.mpy  valve.mpy
web.mpy  wifi.mpy  wifi_setup.mpy  env_sensors.mpy  updater.mpy
```

`updater.mpy` is included, so the updater can update itself.

Installing a `.mpy` also deletes any same-named `.py` on the device (and vice
versa), because a `.py` shadows its `.mpy` in MicroPython's import order.
