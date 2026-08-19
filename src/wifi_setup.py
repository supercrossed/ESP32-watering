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
<label>Network</label><select name="ssid" id="net">{options}</select>
<label>Password</label><input name="password" type="password" placeholder="WiFi password">
<button type="submit">Save &amp; Connect</button>
</form>
<p style="margin-top:14px;border-top:1px solid #e3e6de;padding-top:12px">
Network not listed? <a href="/rescan">Scan again</a> &middot;
or <a href="/manual">type the name yourself</a>.<br>
<span style="font-size:.8rem">2.4GHz networks only - the planter cannot see 5GHz.</span></p>
</div></body></html>"""

_MANUAL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Planter WiFi Setup</title><style>
body{font-family:system-ui,sans-serif;max-width:400px;margin:40px auto;padding:0 16px;background:#f2f4ef;color:#1a1c17}
.card{background:#fff;border-radius:14px;padding:20px;box-shadow:0 2px 10px rgba(0,0,0,.08)}
h2{margin:0 0 6px;font-size:1.2rem}p{font-size:.88rem;color:#5a5d55}
input{width:100%;font-size:1rem;padding:10px;margin:6px 0 14px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box}
button{width:100%;padding:12px;font-size:1rem;font-weight:600;border:none;border-radius:8px;background:#2e7d32;color:#fff}
</style></head><body><div class="card">
<h2>Enter WiFi manually</h2>
<p>Type your network name exactly as it appears, including capitals.</p>
<form method="POST" action="/save">
<label>Network name (SSID)</label><input name="ssid" placeholder="MyNetwork" autocapitalize="none" autocorrect="off" required>
<label>Password</label><input name="password" type="password" placeholder="WiFi password">
<button type="submit">Save &amp; Connect</button>
</form>
<p style="margin-top:14px"><a href="/">Back to the network list</a></p>
</div></body></html>"""

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
        print("portal:", line[:60], "from", addr[0])

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
                # save_creds verifies the write by reading it back; only
                # reboot once we know the credentials actually landed.
                try:
                    wifi.save_creds(fields["ssid"], fields.get("password", ""))
                except Exception as e:
                    print("WiFi credential save FAILED:", e)
                    _send(cl, _PAGE.replace(
                        "{options}", _options(networks)).replace(
                        "<h2>Planter WiFi Setup</h2>",
                        "<h2>Planter WiFi Setup</h2>"
                        "<p style='color:#b00'>Could not save - please try again.</p>"))
                    return
                _send(cl, _SAVED)
                cl.close()
                print("WiFi credentials saved for", fields["ssid"], "- rebooting")
                time.sleep(2)
                machine.reset()
            _send(cl, _PAGE.replace("{options}", _options(networks)))
        else:
            # Captive-portal probes need a REDIRECT, not a page.
            #
            # iOS requests /hotspot-detect.html the instant it associates and
            # expects one of exactly two things: the literal "Success" body
            # (meaning real internet) or a 3xx redirect (meaning "captive
            # portal - show the sign-in sheet"). A 200 carrying our setup
            # page is neither, and iOS treats that ambiguity as a broken
            # network: it drops the association and reports "failed to
            # join". Desktop browsers are far more tolerant, which is why
            # the portal worked on a PC but not an iPhone.
            #
            # Android (/generate_204) and Windows (/ncsi.txt, /connecttest)
            # follow the same convention, so one redirect satisfies all of
            # them and pops the portal automatically.
            path = ""
            try:
                path = line.split(" ")[1]
            except IndexError:
                pass
            if path == "/" or path.startswith("/?"):
                # An empty scan means the dropdown is useless - send the
                # user straight to manual entry instead of a dead control.
                if networks:
                    _send(cl, _PAGE.replace("{options}", _options(networks)))
                else:
                    _send(cl, _MANUAL.replace(
                        "<h2>Enter WiFi manually</h2>",
                        "<h2>Enter WiFi manually</h2>"
                        "<p>No networks were found in the scan - type your "
                        "network name below, or <a href='/rescan'>scan "
                        "again</a>.</p>"))
            elif path.startswith("/rescan"):
                # Re-scan without rebooting. Rebooting would drop the AP the
                # user is connected through - the old "power cycle to
                # rescan" advice was a loop with no way out.
                found = _rescan_networks()
                del networks[:]
                networks.extend(found)
                _redirect(cl, "http://" + AP_IP + "/")
            elif path.startswith("/manual"):
                # Typing the SSID by hand always works - hidden networks
                # never show up in a scan, and a scan can come back empty.
                _send(cl, _MANUAL)
            else:
                _redirect(cl, "http://" + AP_IP + "/")
    except Exception as e:
        print("portal http error:", e)
    finally:
        try:
            cl.close()
        except OSError:
            pass


def _options(networks):
    return "".join('<option value="{0}">{0}</option>'.format(n) for n in networks) \
        or '<option value="">(none found - use "Scan again" below)</option>'


def _rescan_networks():
    """Re-scan from inside the portal, then park the station again.

    The station interface has to be active to scan, but leaving it active
    drags the shared radio off-channel and drops whoever is connected to
    the AP - so it goes straight back off afterwards. Takes a couple of
    seconds, during which the phone's connection may stall briefly."""
    names = []
    try:
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        time.sleep(0.3)
        for net in sta.scan():
            try:
                name = net[0].decode()
            except Exception:
                continue
            if name and name not in names:
                names.append(name)
    except OSError as e:
        print("portal rescan failed:", e)
    finally:
        try:
            sta.active(False)   # park it again - the AP needs the radio
            time.sleep(0.2)
        except Exception:
            pass
    print("portal: rescan found {} network(s)".format(len(names)))
    return names


def _redirect(cl, location):
    """302 to the setup page. This is what tells a phone's captive-portal
    detector that it's behind a portal and makes the sign-in sheet appear."""
    cl.send(b"HTTP/1.1 302 Found\r\nLocation: " + location.encode() +
            b"\r\nContent-Length: 0\r\nCache-Control: no-store\r\n"
            b"Connection: close\r\n\r\n")


def _send(cl, html):
    body = html.encode()
    # no-store matters: phones cache captive-portal pages aggressively, and
    # a stale copy on a later attempt shows the form without it working.
    cl.send(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode() +
            b"\r\nCache-Control: no-store, no-cache, must-revalidate\r\n"
            b"Connection: close\r\n\r\n")
    cl.send(body)


def _ap_config(ap, name):
    """Apply SSID + open auth, coping with MicroPython naming differences.
    MicroPython >= ~1.20 uses ssid=; older builds used essid=. Getting this
    wrong leaves the AP broadcasting the chip's default identity."""
    # An OPEN network must have BOTH authmode open and an empty password.
    # Passing authmode alone leaves some ESP-IDF builds in a WPA2 state with
    # no usable key - the AP is then visible but impossible to join.
    # OSError is included deliberately: MicroPython 1.28 raises
    # "OSError: Wifi Invalid Mode" if the interface isn't active yet, and
    # older builds raise ValueError/TypeError for an unknown keyword. A
    # failure here must never escape - the caller retries once the
    # interface is up.
    for kw in ("ssid", "essid"):
        try:
            ap.config(**{kw: name, "password": "", "authmode": network.AUTH_OPEN})
            return True
        except (ValueError, TypeError, OSError):
            continue
    # Last resort: name only, so the AP is at least identifiable.
    for kw in ("ssid", "essid"):
        try:
            ap.config(**{kw: name})
            return True
        except (ValueError, TypeError, OSError):
            continue
    return False


def _start_ap():
    """Bring up the open setup/rescue access point. Returns its SSID.

    ORDER MATTERS, and two firmware behaviours pull in opposite directions:

      * MicroPython 1.28 raises "OSError: Wifi Invalid Mode" if you call
        ap.config() while the interface is INACTIVE - so config cannot come
        first.
      * Applying config to an already-running AP restarts it without
        reliably restarting its DHCP server - phones then associate but
        never get a lease ("Unable to join network" on iOS).

    The sequence that satisfies both: activate, configure, then bounce the
    interface so DHCP starts fresh against the final config."""
    suffix = ubinascii.hexlify(machine.unique_id())[-4:].decode()
    name = "Planter-Setup-" + suffix
    ap = network.WLAN(network.AP_IF)

    # 1. clear any half-configured AP from a previous cycle
    try:
        ap.active(False)
        time.sleep(0.2)
    except OSError:
        pass

    # 2. activate FIRST - config() needs a live interface on 1.28
    ap.active(True)
    for _ in range(30):  # active() can return before the iface is really up
        if ap.active():
            break
        time.sleep(0.1)

    # 3. now configure (ssid + empty password + open auth together)
    if not _ap_config(ap, name):
        print("WARNING: could not configure the AP - using firmware defaults")

    # 4. bounce so the DHCP server restarts against the config above
    try:
        ap.active(False)
        time.sleep(0.3)
        ap.active(True)
        for _ in range(30):
            if ap.active():
                break
            time.sleep(0.1)
        # re-assert if the bounce dropped the name
        try:
            if ap.config("essid") != name:
                _ap_config(ap, name)
        except (OSError, ValueError, TypeError):
            pass
    except OSError as e:
        print("AP restart failed (continuing):", e)

    time.sleep(0.5)  # let DHCP settle before anyone can join

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

    # Park the station interface BEFORE bringing up the AP. The ESP32 has
    # ONE radio: _scan_networks() above leaves STA active, and an active
    # station keeps scanning/retrying across channels, dragging the AP off
    # its channel mid-association. The phone sees the network, starts the
    # handshake, and it never completes - iOS reports "failed to join".
    try:
        sta = network.WLAN(network.STA_IF)
        sta.disconnect()
        sta.active(False)
        time.sleep(0.3)
        print("station parked for the setup portal (shared radio)")
    except OSError as e:
        print("could not park the station interface:", e)

    essid = _start_ap()
    dns = _open_dns()

    web = socket.socket()
    web.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    web.bind(("0.0.0.0", 80))
    web.listen(2)
    web.setblocking(False)

    print("Setup portal running: join the WiFi network '" + essid
          + "' - a setup page should pop up (or browse to http://" + AP_IP + ")")

    ap_iface = network.WLAN(network.AP_IF)
    last_stations = -1
    last_report = time.time()

    while True:
        # Report associations as they happen: this is the difference
        # between "the phone never reached us" (a radio/DHCP problem) and
        # "it associated but the portal misbehaved" (a software problem).
        try:
            now = time.time()
            if now - last_report >= 2:
                last_report = now
                n = len(ap_iface.status("stations"))
                if n != last_stations:
                    last_stations = n
                    print("portal: {} device(s) associated".format(n))
        except (AttributeError, OSError):
            pass  # status("stations") isn't available on every build

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
                        print("portal: DNS query from", addr[0])
                except OSError:
                    pass
            else:
                _handle_http(web, networks)
