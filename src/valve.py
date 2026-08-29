# valve.py
from machine import Pin
import time
import state


class Valve:
    def __init__(self, name, pin_num, active_high, max_open_sec, flow_meter_pin=None):
        self.name = name
        self._pin = Pin(pin_num, Pin.OUT)
        self._active_high = active_high
        self._max_open_sec = max_open_sec
        self.flow_meter_pin = flow_meter_pin  # not yet read - groundwork only
        self.close()  # ensure known-safe state on boot

    def _write(self, on):
        level = on if self._active_high else (not on)
        self._pin.value(1 if level else 0)

    def open(self, reason="manual"):
        slot = state._valve_slot(self.name)
        if slot["is_open"]:
            return
        self._write(True)
        slot["is_open"] = True
        slot["opened_at"] = time.time()          # wall clock, for display
        slot["opened_ticks"] = time.ticks_ms()   # monotonic, for the cutoff
        slot["open_reason"] = reason
        state.log_event("valve_open", "{}: {}".format(self.name, reason))

    def close(self, reason="manual"):
        slot = state._valve_slot(self.name)
        was_open = slot["is_open"]
        self._write(False)
        slot["is_open"] = False
        if was_open:
            # Report the duration from the monotonic clock too - a wall-clock
            # difference spanning an NTP sync reads as ~26 years.
            duration = self.seconds_open_monotonic(slot)
            slot["last_open_duration"] = duration
            state.log_event(
                "valve_close", "{}: {} (open {}s)".format(self.name, reason, duration)
            )
        slot["opened_at"] = None
        slot["opened_ticks"] = None
        slot["last_close_ts"] = time.time()
        slot["last_close_reason"] = reason

    def seconds_open(self):
        """Wall-clock seconds open, for display. Do NOT use this for the
        safety cutoff - see seconds_open_monotonic()."""
        slot = state._valve_slot(self.name)
        if not slot["is_open"] or slot["opened_at"] is None:
            return 0
        elapsed = time.time() - slot["opened_at"]
        # An NTP sync mid-watering jumps the wall clock (the 2000 epoch to
        # real time is ~26 years), so fall back to the monotonic figure
        # rather than reporting nonsense.
        if elapsed < 0 or elapsed > 86400:
            return self.seconds_open_monotonic(slot)
        return elapsed

    def seconds_open_monotonic(self, slot=None):
        """Seconds open measured with ticks_ms(), which is monotonic and
        immune to clock changes. ticks_diff() handles the counter wrapping."""
        if slot is None:
            slot = state._valve_slot(self.name)
        started = slot.get("opened_ticks")
        if started is None:
            return 0
        return time.ticks_diff(time.ticks_ms(), started) // 1000

    def check_safety_cutoff(self):
        """Call this frequently from the main loop. Hard stop regardless
        of any scheduling logic if the valve has been open too long -
        protects against a stuck-open bug or a flow sensor that never
        arrives (once you add one).

        MUST use the monotonic clock. This was measuring wall-clock time,
        which an NTP sync can move: forward and the valve force-closes
        early, BACKWARD and the elapsed time goes negative so the cutoff
        never fires at all - water running with the last safety net
        silently disabled."""
        slot = state._valve_slot(self.name)
        if slot["is_open"] and self.seconds_open_monotonic(slot) > self._max_open_sec:
            self.close(reason="safety_cutoff")
            return True
        return False
