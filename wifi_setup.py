# wifi_setup.py
# Two WiFi-recovery modes:
#
# 1. run() - blocking captive setup portal, used at BOOT when the device
#    can't join and needs new credentials (fresh kit / wrong password):
#    open AP + DNS hijack + a minimal setup page, saves to wifi.json,
#    reboots. Never returns. Runs before the heavy app modules load.
#
# 2. start_rescue_ap()/poll_rescue()/stop_rescue_ap() - non-blocking
#    RUNTIME rescue: if WiFi drops mid-operation and stays down, main.py
#    opens the same "Planter-Setup-xxxx" hotspot ALONGSIDE station mode
#    (AP+STA) while watering keeps running. Joining it reaches the full
#    dashboard at http://192.168.4.1 (web.py serves every interface), so
#    the network can be fixed from the WiFi card there. The hotspot closes
#    itself as soon as the real network comes back.

import network
import socket
import select
import time
import machine
import ubinascii

import wifi

AP_IP = "192.168.4.1"

_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Planter WiFi Setup</title><style>
body{font-family:system-ui,sans-serif;max-width:400px;margin:40px auto;padding:0 16px;background:#f2f4ef;color:#1a1c17}
.card{background:#fff;border-radius:14px;padding:20px;box-shadow:0 2px 10px rgba(0,0,0,.08)}
h2{margin:0 0 6px;font-size:1.2rem}p{font-size:.88rem;color:#5a5d55}
select,input{width:100%;font-size:1rem;padding:10px;margin:6px 0 14px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box}
button{width:100%;padding:12px;font-size:1rem;font-weight:600;border:none;border-radius:8px;background:#2e7d32;color:#fff}
</style></head><body><div class="card">
<h2>Planter WiFi Setup</h2>
<p>Pick your home WiFi and enter its password. The planter will save it, reboot, and join your network.</p>
<form method="POST" action="/save">
<label>Network</label><select name="ssid">{options}</select>
<label>Password</label><input name="password" type="password" placeholder="WiFi password">
<button type="submit">Save &amp; Connect</button>
</form></div></body></html>"""

_SAVED = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Saved</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding-top:60px">
<h2>Saved!</h2><p>The planter is rebooting and joining your WiFi.<br>
Reconnect your phone to your home network, then find the dashboard at the planter's new address.</p>
</body></html>"""


def _url_decode(s):
    s = s.replace("+", " ")
    out = ""
    i = 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            out += chr(int(s[i + 1 : i + 3], 16))
            i += 3
        else:
            out += s[i]
            i += 1
    return out


def _dns_answer(query, ip):
    """Answer ANY DNS query with our own IP. Phones probe a known URL when
    they join a network; steering that probe to us makes them auto-open
    the setup page (the 'captive portal' popup)."""
    if len(query) < 12:
        return None
    # end of the question name
    i = 12
    while i < len(query) and query[i] != 0:
        i += query[i] + 1
    i += 5  # null byte + qtype(2) + qclass(2)
    question = query[12:i]
    return (
        query[:2]                      # transaction id
        + b"\x81\x80"                  # standard response, no error
        + query[4:6] + b"\x00\x01"     # question count, 1 answer
        + b"\x00\x00\x00\x00"          # no authority/additional
        + question
        + b"\xc0\x0c"                  # pointer to the name
        + b"\x00\x01\x00\x01"          # type A, class IN
        + b"\x00\x00\x00\x3c"          # TTL 60s
        + b"\x00\x04"
        + bytes(int(x) for x in ip.split("."))
    )


def _scan_networks():
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    names = []
    try:
        for net in sta.scan():
            try:
                name = net[0].decode()
            except Exception:
                continue
            if name and name not in names:
                names.append(name)
    except OSError:
        pass
    return names


def _handle_http(server, networks):
    try:
        cl, addr = server.accept()
    except OSError:
        return
    try:
        cl.settimeout(4.0)
        req = b""
        while b"\r\n\r\n" not in req and len(req) < 2048:
            chunk = cl.recv(512)
            if not chunk:
                break
            req += chunk
        line = req.split(b"\r\n", 1)[0].decode()

        if line.startswith("POST /save"):
            body = req.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in req else b""
            # read any remaining body bytes
            for header in req.split(b"\r\n"):
                if header.lower().startswith(b"content-length:"):
                    need = int(header.split(b":", 1)[1])
                    while len(body) < need:
                        chunk = cl.recv(256)
                        if not chunk:
                            break
                        body += chunk
            fields = {}
            for pair in body.decode().split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    fields[_url_decode(k)] = _url_decode(v)
            if fields.get("ssid"):
                wifi.save_creds(fields["ssid"], fields.get("password", ""))
                _send(cl, _SAVED)
                cl.close()
                print("WiFi credentials saved for", fields["ssid"], "- rebooting")
                time.sleep(2)
                machine.reset()
            _send(cl, _PAGE.replace("{options}", _options(networks)))
        else:
            # every other request - including the phone's connectivity
            # probes - gets the setup page, which triggers the portal popup
            _send(cl, _PAGE.replace("{options}", _options(networks)))
    except Exception as e:
        print("portal http error:", e)
    finally:
        try:
            cl.close()
        except OSError:
            pass


def _options(networks):
    return "".join('<option value="{0}">{0}</option>'.format(n) for n in networks) \
        or '<option value="">(no networks found - power cycle to rescan)</option>'


def _send(cl, html):
    body = html.encode()
    cl.send(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n")
    cl.send(body)


def _ap_config(ap, name):
    """Apply SSID + open auth, coping with MicroPython naming differences.
    MicroPython >= ~1.20 uses ssid=; older builds used essid=. Getting this
    wrong leaves the AP broadcasting the chip's default identity."""
    # An OPEN network must have BOTH authmode open and an empty password.
    # Passing authmode alone leaves some ESP-IDF builds in a WPA2 state with
    # no usable key - the AP is then visible but impossible to join.
    for kw in ("ssid", "essid"):
        try:
            ap.config(**{kw: name, "password": "", "authmode": network.AUTH_OPEN})
            return
        except (ValueError, TypeError):
            continue
    # Last resort: name only, so the AP is at least identifiable.
    try:
        ap.config(ssid=name)
    except (ValueError, TypeError):
        ap.config(essid=name)


def _start_ap():
    """Bring up the open setup/rescue access point. Returns its SSID.

    ORDER MATTERS: configure the interface, THEN activate it. Calling
    ap.config() on an already-active interface restarts the AP but does not
    reliably restart the DHCP server that active(True) brought up - the
    network then appears in the phone's WiFi list but never hands out a
    lease, and iOS reports the generic 'Unable to join network'."""
    suffix = ubinascii.hexlify(machine.unique_id())[-4:].decode()
    name = "Planter-Setup-" + suffix
    ap = network.WLAN(network.AP_IF)

    # Start from a known-inactive state so config lands before the AP and
    # its DHCP server are started (also clears any half-configured AP left
    # by a previous rescue cycle).
    try:
        ap.active(False)
        time.sleep(0.1)
    except OSError:
        pass

    _ap_config(ap, name)

    ap.active(True)
    # active() returns before the interface is fully up; DHCP isn't serving
    # until it reports an address. Without this wait the first phone to
    # join can race the DHCP server and fail to get a lease.
    for _ in range(30):  # up to ~3s
        if ap.active():
            break
        time.sleep(0.1)

    # Some builds ignore config applied while inactive - re-assert now that
    # the interface is up, then confirm what we actually ended up with.
    try:
        if ap.config("essid") != name:
            _ap_config(ap, name)
    except (OSError, ValueError, TypeError):
        pass

    try:
        print("AP up: ssid='{}' ifconfig={}".format(name, ap.ifconfig()))
    except OSError:
        pass
    return name


def _open_dns():
    """The captive-portal DNS hijack socket: answers every lookup with our
    own IP so phones auto-open a page when they join the AP."""
    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dns.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    dns.bind(("0.0.0.0", 53))
    dns.setblocking(False)
    return dns


# ---- Runtime rescue hotspot (non-blocking, AP alongside station) ----
_rescue_dns = None
# While the hotspot is up the station is parked (see start_rescue_ap), so
# nothing else would ever notice the router coming back. Retry it here, but
# infrequently: each attempt disturbs the shared radio and can interrupt a
# phone that is joining the hotspot right then.
RESCUE_STA_RETRY_SEC = 120
_last_sta_retry = 0


def rescue_active():
    return _rescue_dns is not None


def start_rescue_ap():
    """Open the hotspot WITHOUT stopping normal operation - watering and
    the reconnect loop keep running; the dashboard is reachable at
    http://192.168.4.1 through the hotspot. Idempotent."""
    global _rescue_dns
    if _rescue_dns is not None:
        return
    # The ESP32 has ONE radio shared by AP and STA. While the station is
    # trying to rejoin a dead router it scans across channels, dragging the
    # AP off its channel mid-handshake - a joining phone then fails with a
    # generic error. Park the station on the AP's channel by disconnecting
    # it; main.py's ensure_connected() reconnects it when the router is
    # back (see poll_rescue, which retries the real network periodically).
    try:
        sta = network.WLAN(network.STA_IF)
        if not sta.isconnected():
            sta.disconnect()
    except OSError:
        pass
    essid = _start_ap()
    _rescue_dns = _open_dns()
    print("Rescue hotspot up: join '" + essid + "', dashboard at http://" + AP_IP)


def stop_rescue_ap():
    """Close the hotspot once the real network is back."""
    global _rescue_dns
    if _rescue_dns is None:
        return
    try:
        _rescue_dns.close()
    except OSError:
        pass
    _rescue_dns = None
    try:
        network.WLAN(network.AP_IF).active(False)
    except OSError:
        pass
    print("WiFi restored - rescue hotspot closed")


def poll_rescue(creds=None):
    """Answer pending captive-probe DNS queries, and occasionally re-try the
    real network (the station is parked while the hotspot is up, so this is
    what notices the router coming back). Called every main-loop iteration;
    non-blocking."""
    global _last_sta_retry
    if _rescue_dns is None:
        return
    for _ in range(4):  # a joining phone fires a small burst of lookups
        try:
            query, addr = _rescue_dns.recvfrom(512)
        except OSError:
            break
        answer = _dns_answer(query, AP_IP)
        if answer:
            try:
                _rescue_dns.sendto(answer, addr)
            except OSError:
                pass

    if creds:
        now = time.time()
        if now - _last_sta_retry >= RESCUE_STA_RETRY_SEC:
            _last_sta_retry = now
            try:
                sta = network.WLAN(network.STA_IF)
                if not sta.isconnected():
                    sta.active(True)
                    sta.connect(creds["ssid"], creds["password"])
            except OSError:
                pass


def run():
    networks = _scan_networks()
    essid = _start_ap()
    dns = _open_dns()

    web = socket.socket()
    web.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    web.bind(("0.0.0.0", 80))
    web.listen(2)
    web.setblocking(False)

    print("Setup portal running: join the WiFi network '" + essid
          + "' - a setup page should pop up (or browse to http://" + AP_IP + ")")

    while True:
        try:
            readable, _, _ = select.select([dns, web], [], [], 0.5)
        except OSError:
            continue
        for s in readable:
            if s is dns:
                try:
                    query, addr = dns.recvfrom(512)
                    answer = _dns_answer(query, AP_IP)
                    if answer:
                        dns.sendto(answer, addr)
                except OSError:
                    pass
            else:
                _handle_http(web, networks)
