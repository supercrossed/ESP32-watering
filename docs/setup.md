# Setup & Calibration

[<- Docs index](README.md)

Getting from a freshly flashed board to a working planter.

## First boot

Reset the board and watch the serial console (Thonny's Shell). A healthy boot
looks like:

```
Booting planter controller...
Free heap before WiFi connect: 149088 bytes
IDF C-heap free before WiFi: 138988 largest block: 110592
Connecting to WiFi: YourNetwork
WiFi connected, IP: 192.168.1.144 mask: 255.255.255.0 gw: 192.168.1.1 dns: ...
Time synced via NTP (UTC): (2026, 7, 13, 4, 1, 9, 0, 194)
[EVENT] boot controller started
Web server listening on port 80
IDF C-heap free at loop start: 33392 largest block: 32768
[EVENT] ready entering main loop
```

Open the printed IP in a browser, or try <http://planter.local>.

> **`IDF C-heap free at loop start` below ~10000 is a problem** - it means
> modules are being compiled on the device. See
> [development.md](development.md#the-two-heap-problem).

## WiFi

**Configured in `src/config.py`:** the planter joins on boot and prints its IP.

**Not configured, or wrong password:** it opens a captive setup portal.

1. On your phone, join the open network **`Planter-Setup-xxxx`**.
2. A setup page opens automatically. If not, browse to <http://192.168.4.1>.
3. Pick your network, enter the password, save. The planter reboots and
   joins.

Credentials are saved to `wifi.json` on the device - never in `config.py`,
never in the repo.

**They persist across reboots and power cuts.** `wifi.json` is written to
flash and takes priority over `config.py` at every boot, so a network
changed from the dashboard's WiFi card (or the setup portal) stays changed.
The write is read back and verified before the device reboots - if it
couldn't be saved you get an error and stay on the current network, rather
than rebooting onto the old one believing it worked.

**If the router is simply down** (network not visible), no portal opens - the
planter keeps running offline and retries. Watering doesn't need WiFi.

**If WiFi drops for 5 minutes mid-operation**, the planter opens the same
hotspot *alongside* normal operation, so you can reach the dashboard at
<http://192.168.4.1> and fix the network. It closes itself when the real
network returns.

---

## Configure the hardware

### 1. Pin map

Open the **GPIO Pin Map** card. Click any pin to assign a role:

- **Solenoid valve** - pick an output-capable pin (not 34-39)
- **I2C SCL / SDA** - defaults are 22 and 21
- **Flow meter** or **rain sensor** - input-only pins are fine here

Click **Scan Bus** to confirm your ADS1115 boards answer. You should see
`0x48` (or whatever you addressed them to). Nothing found means a wiring or
power problem - fix that before continuing.

Changing valve pins or I2C pins **reboots** the board, because those objects
are constructed once at boot.

### 2. Zones

A **zone** is a physical spot in the garden where one sensor sits. It is not
a GPIO pin and not an ADS channel - it's the thing those connect to.

In **Watering Zones**, add one zone per sensor:

| Field | Meaning |
|---|---|
| Name | Whatever you'll recognize: "tomatoes", "bed2" |
| Channel | Global ADS channel (board 1 = 0-3, board 2 = 4-7...) |
| Dry below | Water when moisture drops under this |
| Water until | Stop at this - see [hysteresis](watering.md#hysteresis-dry-below-vs-water-until) |
| Water for | Run time for this zone |
| Valves | Which valve(s) this zone opens - one sensor can drive several |

Zone changes apply live; no reboot.

### 3. Schedules

Optional. In **Schedules**, add a daily run: tap the time chip for a
phone-style picker, set a duration, and select valves and/or zones.

Schedules don't fire until the clock is NTP-synced.

### 4. Test before trusting it

From **Valve Controls**, open a valve and confirm water flows, then close it
and confirm it stops. Do this before leaving the planter unattended.

---

## Calibrating a moisture sensor

Raw ADC readings become a percentage from two calibration points. Every
sensor differs, and so does every soil mix - **calibrate each zone
individually**, don't assume they share a curve.

The dashboard walks you through it: **Watering Zones -> Calibrate** on the
zone you want. Each capture averages the probe for 10 seconds, because a
single reading from a capacitive sensor wanders by a few percent.

### 1. Dry soil

With the probe in its normal spot, wait until the pot is **as dry as you
would ever let it get** - just before you would normally water. Press
**Capture dry**.

### 2. Saturated soil

Water the pot thoroughly, wait a few minutes for it to soak through, then
press **Capture saturated** with the probe in the same position.

Each point saves to the planter the moment it is captured, so the two can
happen days apart - dry today, wet after you next water.

### Reading the result

| What you see | What it means |
|---|---|
| Dry higher than wet | Correct. These sensors output less as moisture rises |
| **"looks backwards"** | The two points were swapped - recapture |
| Spread over ~400 | The probe hadn't settled; recapture |
| Range under ~2000 counts | Narrow - readings will be jumpy. Check the probe is fully inserted |

### Soil vs. air-and-water

Calibrating against **soil** means 0% is "as dry as this pot gets" and 100%
is "saturated", so a 30% threshold describes something real about your
plant's conditions.

Calibrating against air and a glass of water is faster and gives a wider
range, but real soil never reaches either endpoint - readings then cluster
in the middle and thresholds are harder to reason about. You can still do
it that way by entering raw values by hand in the Calibrate dialog.

### Entering values manually

The Calibrate dialog has fields for typing raw dry/wet values directly -
useful for copying a known-good calibration onto a replacement sensor, or
for restoring one from a config backup.

### Choosing a threshold

Once calibrated, watch the readings over a few days:

- Freshly watered soil typically reads **60-80%**
- "Time to water" for most plants is **25-35%**
- Below 15% is quite dry for most vegetables

Start at 30% and adjust based on how your plants actually look. Set the wet
target 10-20 points above the threshold.

---

## Backing up your configuration

`config.py` and `settings.json` live only on your device and aren't in the
repo. Once things are dialed in, use **Config Backup -> Download Config** in
the dashboard to save everything (zones, valves, pin map, schedules,
settings - not WiFi) as a JSON file.

Restoring that file onto a fresh board reproduces the whole setup, which is
also how you'd preset a kit before shipping it.
