# Watering Logic

[<- Docs index](README.md)

How the planter decides when to open a valve, and everything that stops it
from over-watering.

## The core rule

**Only one valve is ever open at a time**, system-wide. This is deliberate -
supply line and pump pressure drop when several valves run together. Multiple
valves are queued and run **sequentially**, each for its own duration.

Two independent things can trigger watering: **moisture** and **schedules**.
Both resolve to a list of valve names before anything opens.

---

## Moisture watering

Every zone is read on a cycle (15 seconds by default). A zone reading below
its **dry threshold** triggers its mapped valves.

A zone maps to one *or more* valves (`zone_valves`), so one sensor can water
several beds. Each runs for that zone's own configured run time
(`zone_durations`), falling back to the global default.

### Hysteresis: "dry below" vs "water until"

Two separate numbers per zone:

| Setting | Meaning |
|---|---|
| **Dry below** (`zone_thresholds`) | Start watering when moisture drops under this |
| **Water until** (`zone_wet_targets`) | Stop when moisture reaches this. Defaults to threshold + 10 |

Without this gap, a zone hovering at the threshold would trigger constantly.

### Soak-and-recheck

Water doesn't reach the sensor instantly, so a single reading right after
watering is meaningless. Instead the planter runs a **session**:

1. Water the zone for its run time.
2. Wait `soak_recheck_sec` (clamped to at least one sensor interval, so the
   re-read always sees a reading taken *after* the water stopped).
3. Re-read the zone.
4. Still below the **wet target**? Water again.
5. Repeat up to `max_water_cycles` (default 3).

Re-waters inside a session bypass the cooldowns - it's all one dry event.
Cooldowns then run from the session's *last* close.

`max_water_cycles = 1` disables the recheck entirely. It's also the flood
guard: if a sensor fails and permanently reads dry, the planter waters at
most this many times before giving up until the cooldown expires.

### Cooldowns

Two limits, both tracked **per valve** so a trigger on one valve never blocks
an unrelated one:

| Setting | Default | Purpose |
|---|---|---|
| `min_supplemental_interval_sec` | 2h | Minimum gap between moisture triggers |
| `post_daily_lockout_sec` | 4h | Skip moisture watering after a scheduled run |

Both are editable in the dashboard's Watering Settings card.

If several zones read dry at once, only the first *runnable* one fires. The
rest are picked up on later cycles - they'll still read dry - rather than
being queued all at once.

---

## Scheduled watering

Each schedule has a time, a duration, and a set of valves and/or zones:

```json
{"id": 1, "hour": 6, "minute": 0, "duration_sec": 300,
 "enabled": true, "valve_names": ["valve1"], "zone_names": ["bed2"]}
```

Zones are expanded to valves **at fire time**, so re-mapping a zone
automatically updates every schedule that uses it. Direct valves and
zone-derived valves are merged, deduplicated, and run in order.

### Schedules wait for the clock

Scheduled watering is held until NTP sync succeeds. Before that the clock
sits at the year-2000 epoch and schedules would fire at nonsense times. NTP
is retried every 5 minutes.

**Moisture watering runs regardless** - it doesn't care what time it is. A
planter with no internet still waters correctly.

The UTC offset is a runtime setting (Watering Settings card). There's no DST
automation - adjust it twice a year, or don't bother if your schedule is
approximate.

---

## Safety layers

Four independent mechanisms, in order of how quickly they act:

**1. Per-valve hard cutoff.** Any valve open longer than
`MAX_VALVE_OPEN_SEC` (default 600s) is force-closed. Checked every single
loop iteration, independently per valve, regardless of what any other logic
believes.

**2. Hardware watchdog.** If the main loop hangs - an I2C lockup, a wedged
network call - the ESP32 reboots after `WATCHDOG_TIMEOUT_SEC` (default 120s).
**Valves close on boot**, so a hang cannot leave water running.

> Set `WATCHDOG_TIMEOUT_SEC = 0` while developing with Thonny. Once armed it
> cannot be stopped, and a board sitting at the REPL will reboot on a loop.

**3. Exception isolation.** Every main-loop subsystem is individually
try/except-wrapped. A failing sensor, a network error, or a bad setting
cannot stop the watering logic.

**4. One valve at a time.** Every trigger path checks `any_valve_open()`
before opening anything.

---

## Settings reference

All editable from the dashboard; stored in `settings.json` on the device.

| Setting | Default | Meaning |
|---|---|---|
| `supplemental_duration_sec` | 60 | Default moisture-trigger run time |
| `zone_durations` | - | Per-zone override of the above |
| `zone_thresholds` | 30% | Per-zone "dry below" |
| `zone_wet_targets` | threshold+10 | Per-zone "water until" |
| `soak_recheck_sec` | 30 | Wait before re-reading after watering |
| `max_water_cycles` | 3 | Max waterings per dry event (1 = no recheck) |
| `min_supplemental_interval_sec` | 7200 | Gap between moisture triggers |
| `post_daily_lockout_sec` | 14400 | Moisture lockout after a schedule |
| `tz_offset_min` | -300 | UTC offset in minutes |
| `daily_enabled` | true | Master switch for schedules |
| `moisture_watering_enabled` | true | Master switch for moisture watering |

---

## Worked example

A zone with threshold 30%, wet target 45%, run time 60s, soak 30s, max 3
cycles:

```
10:00:00  reads 24%  -> below 30, water 60s
10:01:00  valve closes, soak timer starts
10:01:30  soak expires, reads 33%  -> below 45, water again (cycle 2)
10:02:30  valve closes
10:03:00  reads 47%  -> at/above 45, session ends
          cooldown starts from 10:02:30
```

Total: 120 seconds of water delivered in two doses with a soak between,
rather than one 120-second dose that might have run off.
