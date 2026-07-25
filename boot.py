# boot.py
# Runs before main.py on every boot. Its ONLY job is rollback protection
# for over-the-air updates.
#
# THE PROBLEM IT SOLVES
#   An OTA update replaces .mpy files and reboots. If the new build crashes
#   at import time, main.py never reaches its main loop - and the board
#   reboots (watchdog) or sits dead at the REPL. For a planter in a garden
#   that means a laptop, a USB cable, and a walk outside.
#
# HOW IT WORKS
#   Every boot increments a counter in boot_count.txt. Once main.py has
#   run stably for a minute it calls updater-adjacent code to clear that
#   counter (see main.py: "boot confirmed"). So a counter that reaches the
#   limit means "we rebooted N times without ever running stably" - the new
#   build is bad, and we restore the .bak copies the updater left behind.
#
#   Nothing here touches config.py, wifi.json or settings.json, so your
#   credentials and settings always survive a rollback.

MAX_FAILED_BOOTS = 3
_COUNT_FILE = "boot_count.txt"


def _read_count():
    try:
        with open(_COUNT_FILE) as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_count(n):
    try:
        with open(_COUNT_FILE, "w") as f:
            f.write(str(n))
    except OSError:
        pass


def _rollback():
    """Restore every .bak file the updater left, undoing the last update."""
    import os
    restored = []
    try:
        names = os.listdir()
    except OSError:
        return restored
    for f in names:
        if not f.endswith(".bak"):
            continue
        target = f[:-4]
        try:
            try:
                os.remove(target)
            except OSError:
                pass
            os.rename(f, target)
            restored.append(target)
        except OSError:
            pass
    return restored


count = _read_count() + 1
_write_count(count)

if count > MAX_FAILED_BOOTS:
    print("boot: {} consecutive unstable boots - rolling back last update".format(count - 1))
    restored = _rollback()
    if restored:
        print("boot: restored", ", ".join(restored))
        _write_count(0)
        import machine
        import time
        time.sleep(1)
        machine.reset()
    else:
        print("boot: nothing to roll back - continuing (check the console for errors)")
        _write_count(0)
elif count > 1:
    print("boot: attempt {} since last stable run".format(count))
