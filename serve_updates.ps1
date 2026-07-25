# serve_updates.ps1
# Serves this repo over PLAIN HTTP on your LAN so the ESP32 can fetch
# updates without TLS.
#
# WHY THIS EXISTS
#   GitHub is HTTPS-only, and a TLS handshake needs a large contiguous
#   block (~28KB+) from the ESP-IDF C heap - the same pool the WiFi stack
#   uses. On a board already running the application that block often
#   isn't available, and ssl.wrap_socket() does NOT fail cleanly: it
#   blocks indefinitely with no way to interrupt it from Python.
#
#   Plain HTTP needs none of that, so OTA works reliably. Everything else
#   (manifest, SHA-256 verification, atomic install, rollback) is
#   unchanged - only the transport differs.
#
# USAGE
#   1. .\serve_updates.ps1
#   2. Note the URL it prints, e.g. http://192.168.1.243:8080/
#   3. In src\config.py set:
#        UPDATE_BASE_URL = "http://192.168.1.243:8080/"
#   4. Rebuild, upload config.py, reboot, press "Check for Updates"
#
#   Leave this running while you update. Ctrl-C to stop.

param(
    [int]$Port = 8080,
    [string]$Root = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

# Pick the LAN address the ESP32 can actually reach (skip loopback/VPN).
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object {
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.PrefixOrigin -ne "WellKnown"
    } |
    Sort-Object -Property InterfaceMetric |
    Select-Object -First 1).IPAddress

if (-not $ip) { $ip = "YOUR-PC-IP" }

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://+:$Port/")

try {
    $listener.Start()
} catch {
    Write-Host "Could not bind port $Port." -ForegroundColor Red
    Write-Host "Run PowerShell as Administrator, or pick another port:" -ForegroundColor Yellow
    Write-Host "    .\serve_updates.ps1 -Port 8081"
    Write-Host ""
    Write-Host "You may also need a firewall rule (once, as Administrator):"
    Write-Host "    New-NetFirewallRule -DisplayName 'ESP32 planter updates' ``"
    Write-Host "      -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow"
    exit 1
}

Write-Host ""
Write-Host "Serving $Root" -ForegroundColor Green
Write-Host ""
Write-Host "  Set this in src\config.py:" -ForegroundColor Cyan
Write-Host "      UPDATE_BASE_URL = `"http://${ip}:$Port/`"" -ForegroundColor White
Write-Host ""
Write-Host "  Manifest URL: http://${ip}:$Port/build/manifest.json"
Write-Host ""
Write-Host "Ctrl-C to stop."
Write-Host ""

$types = @{
    ".json" = "application/json"
    ".py"   = "text/plain"
    ".mpy"  = "application/octet-stream"
    ".html" = "text/html"
}

try {
    while ($listener.IsListening) {
        $ctx = $listener.GetContext()
        $rel = [Uri]::UnescapeDataString($ctx.Request.Url.AbsolutePath.TrimStart("/"))
        $full = Join-Path $Root $rel

        # Refuse anything outside the served root.
        $rootFull = [IO.Path]::GetFullPath($Root)
        $reqFull  = [IO.Path]::GetFullPath($full)
        if (-not $reqFull.StartsWith($rootFull)) {
            $ctx.Response.StatusCode = 403
            $ctx.Response.Close()
            Write-Host "  403 $rel" -ForegroundColor Red
            continue
        }

        if (Test-Path $reqFull -PathType Leaf) {
            $bytes = [IO.File]::ReadAllBytes($reqFull)
            $ext = [IO.Path]::GetExtension($reqFull).ToLower()
            $ctx.Response.ContentType = if ($types[$ext]) { $types[$ext] } else { "application/octet-stream" }
            $ctx.Response.ContentLength64 = $bytes.Length
            $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
            Write-Host ("  200 {0} ({1} bytes)" -f $rel, $bytes.Length) -ForegroundColor Gray
        } else {
            $ctx.Response.StatusCode = 404
            Write-Host "  404 $rel" -ForegroundColor Yellow
        }
        $ctx.Response.Close()
    }
} finally {
    $listener.Stop()
    Write-Host ""
    Write-Host "Stopped."
}
