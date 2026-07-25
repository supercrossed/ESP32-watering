# Hardware

[<- Docs index](../docs/README.md)

3D-printable models and physical reference material for the planter.

```
hardware/
  case/        enclosure STLs / source CAD
  reference/   datasheets, pinout diagrams, photos
```

Nothing here is fetched by the device - the OTA updater only publishes from
`build/`, so models can be as large as they need to be.

## Planned

- [ ] ESP32 + ADS1115 enclosure (weather-resistant, wall-mountable)
- [ ] Moisture sensor collar / depth stop
- [ ] MOSFET module mounting plate
- [ ] Valve manifold bracket

## Conventions

When adding a model:

- Include the **source** file (`.f3d`, `.scad`, `.step`) alongside the
  `.stl`, so it can be modified later
- Name by function and revision: `esp32-enclosure-v2.stl`
- Note print settings in this file if they matter (supports, layer height,
  material)

Weatherproofing note: this device sits near water. Anything mounted outdoors
wants a gasket channel and drainage, and cable entries should point
downward.
