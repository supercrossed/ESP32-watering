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
        slot["opened_at"] = time.time()
        slot["open_reason"] = reason
        state.log_event("valve_open", "{}: {}".format(self.name, reason))

    def close(self, reason="manual"):
        slot = state._valve_slot(self.name)
        was_open = slot["is_open"]
        self._write(False)
        slot["is_open"] = False
        if was_open and slot["opened_at"]:
            duration = time.time() - slot["opened_at"]
            slot["last_open_duration"] = duration
            state.log_event(
                "valve_close", "{}: {} (open {}s)".format(self.name, reason, duration)
            )
        slot["opened_at"] = None
        slot["last_close_ts"] = time.time()
        slot["last_close_reason"] = reason

    def seconds_open(self):
        slot = state._valve_slot(self.name)
        if not slot["is_open"] or slot["opened_at"] is None:
            return 0
        return time.time() - slot["opened_at"]

    def check_safety_cutoff(self):
        """Call this frequently from the main loop. Hard stop regardless
        of any scheduling logic if the valve has been open too long -
        protects against a stuck-open bug or a flow sensor that never
        arrives (once you add one)."""
        if state._valve_slot(self.name)["is_open"] and self.seconds_open() > self._max_open_sec:
            self.close(reason="safety_cutoff")
            return True
        return False
