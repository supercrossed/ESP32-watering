/**
 * Cloudflare Worker - plain-HTTP mirror of the ESP32 planter repo.
 *
 * WHY THIS EXISTS
 *   GitHub is HTTPS-only. On an ESP32-WROOM-32 running this application,
 *   MicroPython's ssl.wrap_socket() does not merely fail when memory is
 *   tight - it BLOCKS INDEFINITELY, wedging the main loop until the
 *   watchdog reboots the board. Measured: it hung with 30944 bytes free
 *   and a 29696-byte largest contiguous block.
 *
 *   This Worker fetches from GitHub itself (over HTTPS, from Cloudflare's
 *   servers, where memory is not a concern) and serves the bytes to the
 *   device over plain HTTP. The device does no TLS at all.
 *
 *   GitHub stays the source of truth - your publishing workflow is still
 *   just `git push`. The Worker holds no state.
 *
 * SECURITY NOTE
 *   Plain HTTP is not authenticated, so a hostile network could serve
 *   different bytes. The updater's SHA-256 verification means a tampered
 *   FILE is rejected - but the manifest itself arrives over the same
 *   channel. For a garden controller on a home LAN this is a reasonable
 *   trade; signing the manifest (ed25519, public key baked into the
 *   firmware) is the roadmap fix.
 *
 * DEPLOY (free tier, ~2 minutes)
 *   1. https://dash.cloudflare.com -> Workers & Pages -> Create Worker
 *   2. Paste this file, edit REPO/BRANCH below, Deploy
 *   3. Note the URL, e.g. https://planter-updates.YOURNAME.workers.dev
 *   4. In src/config.py:
 *        UPDATE_BASE_URL = "http://planter-updates.YOURNAME.workers.dev/"
 *      NOTE the http:// - not https://. Cloudflare serves both; the
 *      device must use plain HTTP.
 *   5. Rebuild, upload config.py, reboot.
 *
 *   Custom domain (optional): add a route like updates.yourdomain.com/*
 *   in the Worker settings, then use http://updates.yourdomain.com/
 */

const REPO = "supercrossed/ESP32-watering";
const BRANCH = "main";

// Only these may be fetched - the device never needs anything else, and
// this keeps the mirror from becoming an open proxy for the whole repo.
const ALLOWED = /^build\/[A-Za-z0-9._-]+\.(mpy|py|html|json)$/;

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, "");

    if (path === "" || path === "index.html") {
      return new Response(
        "ESP32 planter update mirror\n" +
        "Serving " + REPO + "@" + BRANCH + " over plain HTTP.\n" +
        "Try /build/manifest.json\n",
        { headers: { "content-type": "text/plain" } }
      );
    }

    if (!ALLOWED.test(path)) {
      return new Response("not found\n", { status: 404 });
    }

    const upstream =
      "https://raw.githubusercontent.com/" + REPO + "/" + BRANCH + "/" + path;

    const res = await fetch(upstream, {
      // Cache at the edge so a fleet of planters doesn't hammer GitHub.
      cf: { cacheTtl: 300, cacheEverything: true },
      headers: {
        "User-Agent": "esp32-planter-mirror",
        // Ask GitHub for raw bytes - we must know the exact length, and a
        // gzipped body would break the device's byte-for-byte SHA-256 check.
        "Accept-Encoding": "identity",
      },
    });

    if (!res.ok) {
      return new Response("upstream " + res.status + "\n", { status: res.status });
    }

    // Buffer fully so Content-Length is exact. The device's HTTP client is
    // minimal: it sends HTTP/1.0 and reads until close, so chunked transfer
    // encoding or a compressed body would corrupt the download (and fail
    // the hash check). Files here are at most ~90KB - fine for a Worker.
    const body = await res.arrayBuffer();

    return new Response(body, {
      status: 200,
      headers: {
        "content-type": path.endsWith(".json")
          ? "application/json"
          : "application/octet-stream",
        "content-length": String(body.byteLength),
        "cache-control": "public, max-age=300",
        // Never let an intermediary compress this: the SHA-256 in the
        // manifest is of the RAW file.
        "content-encoding": "identity",
      },
    });
  },
};
