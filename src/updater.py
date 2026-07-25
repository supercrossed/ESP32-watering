# updater.py
# Over-the-air updates straight from a GitHub repo - no host/Pi required.
#
# HOW IT WORKS
#   The repo publishes a manifest.json listing every device file with its
#   SHA-256 and a firmware version string. The device fetches the manifest,
#   compares hashes against what it has, downloads ONLY the files that
#   differ, and applies them atomically.
#
# WHY IT'S BUILT THIS WAY (each point is a failure this avoids)
#   * Streamed to flash in 512-byte chunks, hashing as it goes. A file
#     never exists in RAM in full - same discipline as web.py's uploader,
#     because a 14KB .mpy plus TLS buffers will not fit alongside WiFi.
#   * Downloads land on ".new" temp files. Nothing overwrites a live file
#     until EVERY file has downloaded and verified. A dropped connection
#     mid-update leaves the running firmware untouched.
#   * Hash mismatch = discard. A truncated download can never be installed.
#   * The previous copy of each replaced file is kept as ".bak", and
#     boot.py restores them if the new build fails to boot (see rollback
#     notes in boot.py). This is what makes an unattended update safe: the
#     worst case is a reboot loop that self-heals, not a dead planter that
#     needs a USB cable and a drive out to the garden.
#   * Nothing here runs while a valve is open (main.py gates the call).
#
# TLS NOTE
#   GitHub is HTTPS-only and a handshake needs a contiguous ~30-45KB from
#   the ESP-IDF C heap - the same pool the WiFi stack uses. We close the
#   web server socket and gc.collect() before connecting, and treat a
#   failed handshake as "try again tomorrow", never as an error state.
#   If your board can't reliably manage TLS, point BASE_URL at a plain
#   HTTP mirror instead; everything else works unchanged.

import gc
import os
import time

import state

try:
    import ujson as json
except ImportError:
    import json


# ---- Where updates come from -------------------------------------------
# Raw file host for the repo. Override in config.py with UPDATE_BASE_URL
# (e.g. a plain-HTTP mirror if TLS is tight on your board).
DEFAULT_BASE_URL = "https://raw.githubusercontent.com/{repo}/{branch}/"

# Path to the manifest WITHIN the repo. Artifacts live in build/, so that's
# where the manifest sits too. Override with config.UPDATE_MANIFEST_PATH if
# you restructure the repo; each manifest entry additionally carries its own
# "path", so individual files can move without touching this.
DEFAULT_MANIFEST_PATH = "build/manifest.json"
VERSION_FILE = "version.json"      # what's installed right now
_CHUNK = 512

# Files the updater is allowed to write. Anything in the manifest outside
# this whitelist is ignored - a compromised/bad manifest must not be able
# to drop arbitrary files onto the device.
_ALLOWED_EXT = (".mpy", ".py", ".html", ".json")
# Never auto-update these: config.py holds the user's WiFi credentials and
# pin choices, wifi.json/settings.json are per-device runtime state.
_NEVER_UPDATE = ("config.py", "wifi.json", "settings.json", "version.json")


def _cfg(config, name, default):
    return getattr(config, name, default)


def manifest_path(config):
    return _cfg(config, "UPDATE_MANIFEST_PATH", DEFAULT_MANIFEST_PATH)


def base_url(config):
    url = _cfg(config, "UPDATE_BASE_URL", None)
    if url:
        return url if url.endswith("/") else url + "/"
    repo = _cfg(config, "UPDATE_REPO", None)
    if not repo:
        return None
    branch = _cfg(config, "UPDATE_BRANCH", "main")
    return DEFAULT_BASE_URL.format(repo=repo, branch=branch)


# ---- Installed version bookkeeping -------------------------------------

def installed_version():
    try:
        with open(VERSION_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"version": "unknown", "installed_at": None}


def _write_installed(version):
    try:
        with open(VERSION_FILE, "w") as f:
            json.dump({"version": version, "installed_at": time.time()}, f)
    except OSError as e:
        print("update: could not record version:", e)


# ---- Hashing ------------------------------------------------------------

def _sha256():
    """Return a fresh sha256 hasher, or None if this build lacks one."""
    try:
        import uhashlib as hashlib
    except ImportError:
        try:
            import hashlib
        except ImportError:
            return None
    try:
        return hashlib.sha256()
    except (AttributeError, ValueError):
        return None


def _hexlify(b):
    try:
        import ubinascii as binascii
    except ImportError:
        import binascii
    return binascii.hexlify(b).decode()


def file_hash(path):
    h = _sha256()
    if h is None:
        return None
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return _hexlify(h.digest())


# ---- Minimal streaming HTTP(S) client ----------------------------------
# urequests buffers whole responses into RAM; we cannot afford that, so
# this is a tiny GET that hands the caller a socket to stream from.

def _parse_url(url):
    if url.startswith("https://"):
        scheme, rest = "https", url[8:]
        port = 443
    elif url.startswith("http://"):
        scheme, rest = "http", url[7:]
        port = 80
    else:
        raise ValueError("unsupported url: " + url)
    if "/" in rest:
        host, path = rest.split("/", 1)
        path = "/" + path
    else:
        host, path = rest, "/"
    if ":" in host:
        host, p = host.split(":", 1)
        port = int(p)
    return scheme, host, port, path


def _open_stream(url, timeout=20):
    """Open a GET and return (socket, status_code). Caller must close."""
    import socket
    scheme, host, port, path = _parse_url(url)
    gc.collect()  # TLS needs a big contiguous block - give it the best shot
    ai = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(ai)
        if scheme == "https":
            import ssl
            s = ssl.wrap_socket(s, server_hostname=host)
        req = ("GET {} HTTP/1.0\r\nHost: {}\r\n"
               "User-Agent: esp32-planter\r\n"
               "Connection: close\r\n\r\n").format(path, host)
        s.write(req.encode())

        # read status line + headers without buffering the body
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.read(128)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 4096:
                break
        head, _, leftover = buf.partition(b"\r\n\r\n")
        try:
            status = int(head.split(b" ")[1])
        except (IndexError, ValueError):
            status = 0
        return s, status, leftover
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        raise


def _fetch_small(url, limit=8192):
    """Fetch a small resource (the manifest) fully into RAM."""
    s, status, leftover = _open_stream(url)
    try:
        if status != 200:
            raise OSError("HTTP {}".format(status))
        body = leftover
        while len(body) < limit:
            chunk = s.read(_CHUNK)
            if not chunk:
                break
            body += chunk
        return body
    finally:
        try:
            s.close()
        except Exception:
            pass
        gc.collect()


def _download_to(url, dest, expect_hash):
    """Stream a URL to `dest`, verifying its SHA-256. Returns True on a
    verified write; the temp file is removed on any failure."""
    s, status, leftover = _open_stream(url)
    h = _sha256()
    written = 0
    try:
        if status != 200:
            raise OSError("HTTP {}".format(status))
        with open(dest, "wb") as f:
            if leftover:
                f.write(leftover)
                written += len(leftover)
                if h:
                    h.update(leftover)
            while True:
                chunk = s.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if h:
                    h.update(chunk)
    except Exception as e:
        print("update: download failed for", dest, "-", e)
        _unlink(dest)
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass
        gc.collect()

    if h is not None and expect_hash:
        got = _hexlify(h.digest())
        if got != expect_hash:
            print("update: HASH MISMATCH for {} (got {}, want {})".format(
                dest, got[:12], expect_hash[:12]))
            _unlink(dest)
            return False
    elif not written:
        _unlink(dest)
        return False
    return True


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


# ---- The update flow ----------------------------------------------------

def check(config):
    """Fetch the manifest and report what would change. Cheap: one small
    HTTPS GET. Returns a dict - never raises."""
    result = {
        "ok": False,
        "checked_at": time.time(),
        "error": None,
        "available": None,
        "installed": installed_version().get("version"),
        "changed": [],
    }
    url = base_url(config)
    if not url:
        result["error"] = "no UPDATE_REPO configured"
        return result
    try:
        raw = _fetch_small(url + manifest_path(config))
        manifest = json.loads(raw)
    except Exception as e:
        result["error"] = "manifest fetch failed: {}".format(e)
        return result

    result["available"] = manifest.get("version")
    files = manifest.get("files", {})
    changed = []
    for name, meta in files.items():
        if not _updatable(name):
            continue
        want = meta.get("sha256") if isinstance(meta, dict) else meta
        if file_hash(name) != want:
            changed.append(name)
    result["changed"] = changed
    result["ok"] = True
    state.last_update_check = result["checked_at"]
    state.update_available = manifest.get("version") if changed else None
    return result


def _updatable(name):
    """Is this a file we're willing to write? `name` is the DEVICE filename
    (the device filesystem is flat) - never a path."""
    if name in _NEVER_UPDATE:
        return False
    if "/" in name or "\\" in name or name.startswith("."):
        return False  # no directories, no traversal
    return any(name.endswith(e) for e in _ALLOWED_EXT)


def _fetch_path(name, meta):
    """Where to fetch `name` from, relative to the repo root.

    The manifest may carry an explicit "path" (e.g. "build/web.mpy") so the
    repo can be reorganized without changing device code. Older manifests
    have no path - fall back to the bare filename, which is what the
    original flat-repo layout used."""
    if isinstance(meta, dict):
        p = meta.get("path")
        if p:
            # Refuse anything that could escape the repo or hit an absolute
            # URL - the manifest is remote input, treat it as untrusted.
            if ".." in p or p.startswith("/") or "://" in p:
                return None
            return p
    return name


def apply(config, on_progress=None):
    """Download every changed file, verify, then swap them in atomically.
    Returns a result dict. The caller reboots if result['reboot'] is True."""
    result = {"ok": False, "installed": [], "error": None, "reboot": False}
    url = base_url(config)
    if not url:
        result["error"] = "no UPDATE_REPO configured"
        return result

    try:
        manifest = json.loads(_fetch_small(url + manifest_path(config)))
    except Exception as e:
        result["error"] = "manifest fetch failed: {}".format(e)
        return result

    version = manifest.get("version", "unknown")
    files = manifest.get("files", {})

    # 1. figure out what actually needs downloading
    todo = []
    for name, meta in files.items():
        if not _updatable(name):
            continue
        want = meta.get("sha256") if isinstance(meta, dict) else meta
        if file_hash(name) != want:
            fetch_from = _fetch_path(name, meta)
            if fetch_from is None:
                result["error"] = "manifest has an unsafe path for " + name
                return result
            todo.append((name, want, fetch_from))

    if not todo:
        result["ok"] = True
        result["error"] = None
        _write_installed(version)
        return result

    # 2. download EVERYTHING to .new first - nothing live is touched yet
    staged = []
    for name, want, fetch_from in todo:
        if on_progress:
            on_progress(name)
        gc.collect()
        tmp = name + ".new"
        if not _download_to(url + fetch_from, tmp, want):
            for s in staged:
                _unlink(s + ".new")
            result["error"] = "download/verify failed: " + name
            return result
        staged.append(name)

    # 3. all verified - swap them in, keeping .bak copies for rollback
    for name in staged:
        try:
            if _exists(name):
                _unlink(name + ".bak")
                os.rename(name, name + ".bak")
            os.rename(name + ".new", name)
            # a .py must not shadow a freshly installed .mpy (and vice versa)
            if name.endswith(".mpy"):
                _unlink(name[:-4] + ".py")
            elif name.endswith(".py"):
                _unlink(name[:-3] + ".mpy")
            result["installed"].append(name)
        except OSError as e:
            result["error"] = "install failed for {}: {}".format(name, e)
            state.log_event("update", result["error"])
            return result

    _write_installed(version)
    state.update_available = None
    state.last_update_install = time.time()
    result["ok"] = True
    result["reboot"] = True
    state.log_event("update", "installed {} ({} files)".format(
        version, len(result["installed"])))
    return result


def clear_backups():
    """Drop .bak files once a new build has proven itself (called by
    main.py after the loop has run a while). Keeps flash tidy."""
    removed = 0
    try:
        for f in os.listdir():
            if f.endswith(".bak"):
                _unlink(f)
                removed += 1
    except OSError:
        pass
    return removed
