# wifi.py
import network
import time
import ujson as json

WIFI_FILE = "wifi.json"


def load_creds(config):
    """Credentials from wifi.json (written by the captive setup portal),
    falling back to config.py for boards flashed the old way."""
    try:
        with open(WIFI_FILE) as f:
            data = json.load(f)
        if data.get("ssid"):
            return {"ssid": data["ssid"], "password": data.get("password", "")}
    except (OSError, ValueError):
        pass
    return {"ssid": config.WIFI_SSID, "password": config.WIFI_PASSWORD}


def save_creds(ssid, password):
    """Persist credentials to flash and READ THEM BACK to confirm.

    The caller reboots straight after this, so a silent write failure (full
    flash, filesystem error) would drop the device back onto the old
    network with no indication anything went wrong - and the user is
    usually changing WiFi precisely because the old network is gone.
    Raises OSError if the credentials didn't land."""
    with open(WIFI_FILE, "w") as f:
        json.dump({"ssid": ssid, "password": password}, f)
    # verify: reopen and compare, so we never report success on a bad write
    try:
        with open(WIFI_FILE) as f:
            back = json.load(f)
    except (OSError, ValueError) as e:
        raise OSError("saved WiFi credentials could not be read back: {}".format(e))
    if back.get("ssid") != ssid or back.get("password", "") != password:
        raise OSError("saved WiFi credentials did not match on read-back")
    return True


def current_ip():
    try:
        return network.WLAN(network.STA_IF).ifconfig()[0]
    except OSError:
        return "?"


def connect(ssid, password, timeout_sec=20):
    wlan = network.WLAN(network.STA_IF)
    try:
        # mDNS name: reach the dashboard at http://planter.local even if
        # DHCP moves the IP. Must be set before the interface associates.
        network.hostname("planter")
    except (AttributeError, OSError):
        pass  # older MicroPython - IP-only access still works
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi:", ssid)
        try:
            wlan.connect(ssid, password)
        except OSError as e:
            print("WiFi connect error:", e)
            return None
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout_sec:
                print("WiFi connect timed out")
                return None
            time.sleep(0.5)
    cfg = wlan.ifconfig()
    # full tuple, not just the IP: if the gateway here differs from the
    # router the PC uses, the ESP32 joined a DIFFERENT access point
    # broadcasting the same SSID (extender/mesh node) - a classic cause of
    # "device is online but unreachable from the LAN"
    print("WiFi connected, IP: {} mask: {} gw: {} dns: {}".format(*cfg))
    return cfg[0]


def rssi():
    """Signal strength in dBm, or None.

    Worth logging because it separates two very different faults that look
    identical from the outside. If WiFi degrades when sensors are attached
    and RSSI *drops*, the sensor wiring is coupling noise into the radio or
    detuning the antenna. If RSSI stays strong and the link still fails,
    the radio is fine and the problem is power (the WiFi PA browning out
    during transmit) or memory - not RF."""
    try:
        return network.WLAN(network.STA_IF).status("rssi")
    except (AttributeError, OSError, ValueError):
        return None


def is_connected():
    try:
        return network.WLAN(network.STA_IF).isconnected()
    except OSError:
        return False


def ensure_connected(ssid, password):
    """Called periodically from the main loop. If the connection dropped
    (router rebooted, signal loss), kick off a reconnect attempt without
    blocking - watering keeps running while WiFi is down."""
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        return True
    try:
        wlan.active(True)
        wlan.connect(ssid, password)
    except OSError:
        pass
    return False


# ---- Link health ------------------------------------------------------
# isconnected() only reports that the radio is ASSOCIATED with an access
# point. It says nothing about whether packets actually move: a router can
# be up while its DHCP lease expired, its NAT table was cleared, or the
# device's own lwIP state wedged. That "zombie" state is the one that makes
# the dashboard unreachable while every status field still reads healthy.

# Errnos that mean "the path works, the port just isn't open". A refusal or
# a reset is a REPLY - it proves packets reached the router and came back.
_ALIVE_ERRNOS = (
    104,   # ECONNRESET
    111,   # ECONNREFUSED
    113,   # ECONNABORTED
)
# Errnos that mean nothing came back at all.
_DEAD_ERRNOS = (
    110,   # ETIMEDOUT (linux)
    116,   # ETIMEDOUT (MicroPython/lwIP)
    118,   # EHOSTUNREACH
)


def gateway():
    """The router's IP, or None if we don't have one."""
    try:
        gw = network.WLAN(network.STA_IF).ifconfig()[2]
    except (OSError, IndexError):
        return None
    if not gw or gw == "0.0.0.0":
        return None
    return gw


def lan_healthy(timeout=3, port=80):
    """Can we actually reach the router?

    Returns True (packets flow), False (nothing came back), or None (can't
    tell - treat as inconclusive and do NOT act on it).

    Opens a TCP connection to the gateway. Success proves the path; so does
    a refusal or reset, because those are replies. Only a timeout means the
    link is dead, which is why the errno is inspected rather than treating
    every OSError as failure - a router with nothing listening on port 80
    is perfectly healthy and must not be misread as broken.

    Blocks for up to `timeout` seconds, so the caller runs this on a slow
    cadence and feeds the watchdog around it."""
    import socket
    gw = gateway()
    if gw is None:
        return None
    s = None
    try:
        addr = socket.getaddrinfo(gw, port)[0][-1]
    except Exception:
        # getaddrinfo allocates lwIP structures and can fail under memory
        # pressure; that is not evidence about the link.
        return None
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect(addr)
        return True
    except OSError as e:
        err = e.args[0] if e.args else None
        if err in _ALIVE_ERRNOS:
            return True
        if err in _DEAD_ERRNOS:
            return False
        # Unrecognised: stay conservative. A false "dead" costs a working
        # connection, which is worse than missing one zombie cycle.
        return None
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def recycle(ssid, password, hard=False):
    """Tear the station connection down and bring it back.

    soft (hard=False): disconnect + reconnect. Clears an expired lease or a
    stale association.
    hard (hard=True):  drop the interface entirely, then re-activate. This
    is what clears a wedged lwIP/driver state that a plain reconnect leaves
    in place.

    Returns True if the reconnect completed within the timeout. Never
    raises - a failed recycle just means we try again next cycle."""
    wlan = network.WLAN(network.STA_IF)
    try:
        try:
            wlan.disconnect()
        except OSError:
            pass
        if hard:
            wlan.active(False)
            time.sleep(1)
            wlan.active(True)
            time.sleep(0.5)
        wlan.connect(ssid, password)
    except OSError as e:
        print("wifi recycle failed:", e)
        return False
    # Give it a moment to associate, but stay well short of the watchdog.
    for _ in range(20):          # up to ~10s
        if wlan.isconnected():
            return True
        time.sleep(0.5)
    return False


def portal_needed(ssid, had_saved_creds=False):
    """Decide whether a failed boot-time connect should open the captive
    setup portal.

    Rules, in order:
      * No/placeholder SSID -> portal (fresh kit).
      * Credentials came from wifi.json, i.e. somebody typed them into the
        portal and they don't work -> portal. Refusing here strands the
        device: no network, no hotspot, and the only fix is a USB cable.
      * Otherwise fall back to visibility: if the target network is on the
        air but we couldn't join, the password is probably wrong ->
        portal. If it isn't visible the router is likely just down -> stay
        offline and let the main loop retry, since watering doesn't need
        WiFi and a portal would be unreachable in the garden anyway.
    """
    if not ssid or ssid == "YOUR_WIFI_SSID":
        return True

    # Typed-in credentials that fail are a user error we must let them fix.
    if had_saved_creds:
        return True

    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
        # Scan a few times: the first scan straight after a failed connect
        # often comes back empty while the radio is still settling, which
        # would wrongly look like "router is down".
        for attempt in range(3):
            visible = set()
            try:
                for net in wlan.scan():
                    try:
                        visible.add(net[0].decode())
                    except Exception:
                        pass
            except OSError:
                pass
            if ssid in visible:
                return True
            if visible and attempt >= 1:
                # we got a real list twice and our network wasn't in it
                return False
            time.sleep(1)
        return False
    except OSError:
        return False


def has_saved_creds():
    """True if credentials came from wifi.json (someone configured them)
    rather than from config.py defaults."""
    try:
        with open(WIFI_FILE) as f:
            return bool(json.load(f).get("ssid"))
    except (OSError, ValueError):
        return False
