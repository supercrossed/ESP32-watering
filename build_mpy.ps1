# build_mpy.ps1
# Pre-compiles the device modules to .mpy bytecode so the ESP32 never has
# to compile them itself. On-device compilation of the larger modules
# (web.py especially) makes MicroPython grow the Python GC heap out of the
# ESP-IDF C heap - the same pool the WiFi/lwIP/I2C drivers allocate from -
# which starved the network stack (~764 bytes free at loop start).
#
# One-time setup:   pip install mpy-cross
# Build:            .\build_mpy.ps1
# Then upload the CONTENTS of build\ to the ESP32 and DELETE the matching
# .py module files from the device (keep main.py/config.py - they stay .py).
#
# NOTE: the mpy-cross version must emit bytecode your firmware accepts
# (v6.3 works for MicroPython 1.23+). If the board says "incompatible .mpy
# file", update mpy-cross:  pip install --upgrade mpy-cross

param(
    # Record that build\'s current contents are now on the device, so the
    # next build can report exactly what changed since.
    [switch]$Deployed
)

$ErrorActionPreference = "Stop"

if ($Deployed) {
    if (-not (Test-Path build)) { throw "no build\ folder - run .\build_mpy.ps1 first" }
    Set-Content "build\.last_deploy" (Get-Date).ToString("o") -Encoding utf8
    Write-Host "Marked build\ as deployed at $(Get-Date -Format 'HH:mm:ss')."
    return
}

# main.py stays .py (it's the boot entry point, executed not imported).
# config.py stays .py (small, and hand-editable in Thonny in the field).
$modules = @(
    "state", "settings_store", "ads1x15", "moisture", "valve",
    "web", "wifi", "wifi_setup", "env_sensors", "updater"
)

New-Item -ItemType Directory -Force build | Out-Null
# Wipe artifacts but keep the deploy marker (it's what dates the "changed
# since last deploy" report below).
Get-ChildItem build -File | Where-Object { $_.Name -ne ".last_deploy" } | Remove-Item -Force

foreach ($m in $modules) {
    python -m mpy_cross "$m.py" -o "build\$m.mpy"
    if ($LASTEXITCODE -ne 0) { throw "mpy-cross failed on $m.py" }
    Write-Host ("  {0}.py -> build\{0}.mpy" -f $m)
}

Copy-Item main.py, config.py, index.html build\
Copy-Item boot.py build\ -ErrorAction SilentlyContinue

# ---- OTA manifest -------------------------------------------------------
# updater.py on the device fetches manifest.json, compares each file's
# SHA-256 against what it has, and downloads only what differs. config.py
# is deliberately EXCLUDED: it holds per-device WiFi credentials and pin
# choices that must never be overwritten by an update.
$version = (Get-Date -Format "yyyy.MM.dd.HHmm")
$files = @{}
Get-ChildItem build -File | Where-Object {
    $_.Name -ne "manifest.json" -and
    $_.Name -ne "config.py" -and
    $_.Name -ne ".last_deploy"
} | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    $files[$_.Name] = @{ sha256 = $hash; size = $_.Length }
}
$manifest = [ordered]@{
    version   = $version
    generated = (Get-Date).ToString("o")
    files     = $files
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content build\manifest.json -Encoding utf8

# The device records which version it installed; ship it so a fresh flash
# starts from a known version rather than "unknown".
[ordered]@{ version = $version; installed_at = $null } |
    ConvertTo-Json | Set-Content build\version.json -Encoding utf8

Write-Host ""
Write-Host "manifest.json written - version $version ($($files.Count) files)"

Write-Host ""
Write-Host "build\ is ready. Upload its contents to the ESP32, then DELETE"
Write-Host "these stale files from the device (a .py shadows its .mpy):"
Write-Host ("  " + (($modules | ForEach-Object { "$_.py" }) -join " "))

# Which artifacts changed since the last deploy? Uploading everything is
# always safe, but on a slow link it's useful to know the minimum set.
$stampFile = "build\.last_deploy"
if (Test-Path $stampFile) {
    $since = (Get-Item $stampFile).LastWriteTime
    $changed = Get-ChildItem build -File |
        Where-Object { $_.Name -ne ".last_deploy" -and $_.LastWriteTime -gt $since } |
        ForEach-Object { $_.Name }
    Write-Host ""
    if ($changed) {
        Write-Host "Changed since your last '.\build_mpy.ps1 -Deployed':" -ForegroundColor Yellow
        $changed | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "No files changed since your last recorded deploy."
    }
}
Write-Host ""
Write-Host "After uploading, run '.\build_mpy.ps1 -Deployed' to mark this set as deployed."
