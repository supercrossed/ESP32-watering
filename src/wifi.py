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
