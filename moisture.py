# moisture.py

def raw_to_percent(raw, dry_raw, wet_raw):
    """Map a raw ADC reading to 0-100% wetness using two-point calibration.
    dry_raw = raw reading in dry air, wet_raw = raw reading fully submerged.
    Assumes dry_raw > wet_raw (voltage drops as moisture increases), which
    is the typical behavior for capacitive sensors including AITRIP.
    """
    if dry_raw == wet_raw:
        return 0.0
    pct = (dry_raw - raw) / (dry_raw - wet_raw) * 100.0
    if pct < 0:
        pct = 0.0
    if pct > 100:
        pct = 100.0
    return round(pct, 1)


def read_all(ads_boards, zones, thresholds):
    """Read every configured zone. `ads_boards` is a list of ADS1115 driver
    instances sharing the I2C bus (one per board/address). zone["channel"]
    is a global index: board 1 = channels 0-3, board 2 = 4-7, and so on.
    `thresholds` is a dict name->percent, normally sourced from
    settings_store so it can be changed at runtime."""
    results = []
    for zone in zones:
        ch = zone["channel"]
        board = ch // 4
        if board >= len(ads_boards):
            continue  # that board was removed - nothing to read
        raw = ads_boards[board].read(ch % 4)
        pct = raw_to_percent(raw, zone["dry_raw"], zone["wet_raw"])
        results.append(
            {
                "name": zone["name"],
                "raw": raw,
                "percent": pct,
                "threshold": thresholds.get(zone["name"], zone.get("threshold_percent", 30)),
            }
        )
    return results
