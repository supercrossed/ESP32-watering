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

## HTTPS does not work on ESP32-WROOM-32 (measured)

**The device cannot fetch from GitHub directly.** This is not a tuning
problem - it was measured on real hardware and HTTPS is disabled by default
as a result.

MicroPython's `ssl.wrap_socket()` performs the TLS handshake *inside* the
call. When it can't complete, it does not raise and does not honor the
socket timeout - it **blocks indefinitely**, wedging the main loop (no HTTP
responses, no valve timing) until the watchdog reboots the board.

Measured on an ESP32-WROOM-32 running this application against
`raw.githubusercontent.com`:

| Free C heap | Largest block | Result |
|---|---|---|
| 30,944 | 29,696 | **hung** |
| 27,308 | 26,624 | (refused by the guard) |

The handshake needs room for record buffers *and* certificate-chain
verification at once, and a running WiFi stack plus this application doesn't
leave it. So the updater refuses HTTPS up front rather than risking a hang.

To try anyway on a board with more headroom (PSRAM, or a stripped-down
application), set `ALLOW_HTTPS = True` in `src/config.py` - a heap check
still applies.

## Use a plain-HTTP mirror instead

```python
UPDATE_BASE_URL = "http://your-mirror.example.com/"
```

Everything else is unchanged - manifest, SHA-256 verification, atomic
install, and rollback are all transport-agnostic.

### Cloudflare Worker (recommended for kits)

[`tools/cloudflare-worker.js`](../tools/cloudflare-worker.js) mirrors this
repo over plain HTTP. Cloudflare does the HTTPS fetch from GitHub on its own
servers, where memory isn't a constraint, and serves plain bytes to devices.
Free tier, nothing to maintain, edge-cached so a fleet doesn't hammer GitHub,
and **GitHub stays the source of truth** - publishing is still `git push`.

1. dash.cloudflare.com -> Workers & Pages -> Create Worker
2. Paste the file, edit `REPO`/`BRANCH`, Deploy
3. `UPDATE_BASE_URL = "http://your-worker.workers.dev/"`
   (**http**, not https - the device must not do TLS)

### Local mirror (development only)

`serve_updates.ps1` serves the repo from your PC over HTTP. Useful for
testing an update before publishing; **only reachable on your own LAN**, so
it is not a solution for kit owners.

## Security trade-off

Plain HTTP is unauthenticated. Per-file SHA-256 verification means a
tampered *file* is rejected, but the manifest arrives over the same channel,
so a hostile network could serve a coherent older or malicious set.

For a garden controller on a home LAN this is a reasonable trade. **Before
shipping kits**, sign the manifest (ed25519, public key baked into the
firmware) - that closes the gap completely and is on the roadmap.

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
