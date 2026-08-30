# Hardware & Wiring

[<- Docs index](README.md)

## Parts list

| Part | Qty | Notes |
|---|---|---|
| ESP32-WROOM-32 devkit | 1 | 38-pin. WROVER also works, but GPIO 16/17 are taken by its PSRAM |
| ADS1115 16-bit ADC board | 1-4 | 4 analog channels each. Moisture sensors are analog - they do **not** connect to GPIO |
| Capacitive soil moisture sensor | 1 per zone | e.g. AITRIP. Capacitive, not resistive - resistive probes corrode within weeks |
| 12V solenoid valve | 1 per valve | Normally-closed |
| MOSFET switch module | 1 per valve | D4184 / XY-MOS recommended. See [MOSFET selection](#mosfet-selection) |
| 1N4007 diode | 1 per valve | **Required.** Flyback protection |
| 12V power supply | 1 | Sized for your valves (a typical solenoid draws ~500mA) |
| AHT20 + BMP280 board | optional | Temp/humidity/pressure. Auto-detected on the I2C bus |
| LM393 rain sensor | optional | Digital out, LOW when wet |
| YF-S201 flow meter | optional | Config/UI groundwork only - pulse counting isn't implemented |

---

## ESP32 pin safety

Not every GPIO is usable. On a WROOM-32:

| Pins | Status |
|---|---|
| **6-11** | **Never use.** Wired to the SPI flash chip - using them crashes the board |
| **1, 3** | UART0 (USB serial). Using them breaks Thonny/flashing |
| **0, 2, 12, 15** | Boot-strapping pins. Usable as outputs *after* boot with care. **GPIO 12 is the riskiest** - held high at reset, the board won't boot. GPIO 2 is the onboard "D2" LED, used here as the status light |
| **34, 35, 36, 39** | **Input-only**, no internal pull-ups. Fine for a flow meter or rain sensor; **cannot drive a valve** |
| **16, 17** | Usable on WROOM. Reserved on WROVER (PSRAM) |
| **4, 5, 13, 14, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33** | Freely usable |

The dashboard's **GPIO Pin Map** card shows this live and refuses assignments
that would break the board.

---

## I2C bus

Every ADS1115 and the optional environment sensor share **one** two-wire bus:

```
ESP32 GPIO 22 ----> SCL   (every board)
ESP32 GPIO 21 ----> SDA   (every board)
ESP32 3V3     ----> VCC
ESP32 GND     ----> GND
```

This is the most common point of confusion: **boards are daisy-chained on the
same two pins.** A second ADS1115 does not need two more GPIO.

### Bus reliability

I2C was designed for short traces on a circuit board, not garden wiring, and
a stalled bus can take WiFi down with it (see
[troubleshooting](troubleshooting.md#wifi-drops-after-sensors-are-connected)).
The firmware bounds every transaction with a timeout and can rebuild the bus
if it wedges, but good wiring matters:

- **Keep runs short** - under ~1m of unshielded cable. Longer needs
  shielded or twisted pair.
- **One set of pull-ups.** Most ADS1115 breakouts carry 10k pull-ups on SDA
  and SCL. Two boards halve that, four quarter it - remove the resistors
  from all but one board.
- **Route away from solenoid wiring** - switching 12V inductive loads
  couples noise straight into an adjacent signal pair.
- **Solid common ground** between ESP32, ADS1115 and sensors.

The bus runs at 100kHz (`I2C_FREQ` in `config.py`) rather than 400kHz for
exactly this reason. Only raise it if your wiring is short and clean.

### Multiple ADS1115 boards

Each board needs a unique address, set by wiring its **ADDR** pin:

| ADDR wired to | Address |
|---|---|
| GND | `0x48` |
| VDD | `0x49` |
| SDA | `0x4A` |
| SCL | `0x4B` |

ADDR is a pin *on the ADS1115 board*, wired to another pin on that same
board - it does not connect to the ESP32.

Add each address in the dashboard's pin map (I2C section). Zone channels are
numbered **globally**:

| Board | Address | Channels |
|---|---|---|
| 1st | 0x48 | 0, 1, 2, 3 |
| 2nd | 0x49 | 4, 5, 6, 7 |
| 3rd | 0x4A | 8, 9, 10, 11 |
| 4th | 0x4B | 12, 13, 14, 15 |

So "channel 5" means the second board's A1 input. The **Scan Bus** button in
the dashboard lists which addresses actually answer - use it to confirm
wiring before assigning zones.

---

## Moisture sensors

```
Sensor VCC  ----> ESP32 3V3      (3.3V, not 5V - more stable readings)
Sensor GND  ----> GND
Sensor AOUT ----> ADS1115 A0 / A1 / A2 / A3
```

Do **not** submerge the sensor above the marked line - only the probe area
is protected. Calibration is per-sensor; see
[setup.md](setup.md#calibrating-a-moisture-sensor).

---

## Valves

Per valve:

```
ESP32 GPIO (e.g. 26) ----> MOSFET module PWM / SIG
ESP32 GND            ----> MOSFET module GND        (shared ground is essential)
12V supply +         ----> MOSFET module DC+
12V supply -         ----> MOSFET module DC-
MOSFET OUT+ / OUT-   ----> Solenoid valve
1N4007 across the solenoid terminals, band toward +
```

### Gate pull-down resistor

**Recommended: 10k from the MOSFET module's signal/gate pin to GND.**

An ESP32 GPIO is a floating input until the firmware configures it as an
output. A floating MOSFET gate can drift high enough to partially conduct,
so a valve can sit part-open whenever the pin is not being driven:

- during the first moments of boot, before the firmware runs
- if the signal wire works loose or a connector corrodes outdoors
- while the board is held in reset

The firmware closes this window in software - valve pins are driven to their
closed state as one of the very first things `main.py` does, before WiFi and
before the setup portal. But software cannot help before it is running, and
it cannot help at all if the wire falls off. A resistor can.

Many D4184 / XY-MOS modules already include one; check for a resistor
between the signal pin and GND before adding your own. If in doubt, adding a
10k in parallel with an existing pull-down is harmless.

### The flyback diode

**Do not skip this.** When a solenoid closes, its collapsing magnetic field
produces a large reverse voltage spike. Without a diode across the coil to
absorb it, that spike destroys the MOSFET - sometimes immediately, sometimes
after weeks of reliable operation.

Wire the 1N4007 directly across the solenoid's two terminals, with the banded
end toward the positive terminal.

### MOSFET selection

Many cheap **IRF520** modules are sold for Arduino use but are *not*
logic-level MOSFETs. At 3.3V gate drive they only partially turn on: the
module's LED lights, the output measures a few volts, but under the
solenoid's actual current draw the voltage sags and the valve never pulls in.

A **D4184** or **XY-MOS** module switches fully at 3.3V and works correctly.
If your valve clicks weakly or not at all while the module's LED is lit, this
is almost certainly the cause - see
[troubleshooting](troubleshooting.md#valve-wont-open).

---

## Where resistors are needed

| Connection | Resistor | Why |
|---|---|---|
| **I2C SDA / SCL** | 10k pull-up to 3.3V, **one set for the whole bus** | I2C is open-drain - devices only pull LOW, so without a pull-up the line never returns high. Usually already on the ADS1115 breakout |
| **MOSFET gate** | 10k pull-down to GND | Keeps a valve closed while the GPIO floats (boot, reset, broken wire). See [above](#gate-pull-down-resistor) |
| **Solenoid** | 1N4007 diode (not a resistor) | Absorbs the inductive spike. **Required** |
| **Flow meter (YF-S201)** | 10k pull-up to 3.3V | Open-collector Hall output. **Mandatory if wired to GPIO 34-39** - those pins have no internal pull-ups at all |
| **LM393 rain sensor** | none | Push-pull comparator output; the firmware enables the ESP32's internal pull-up anyway |
| **Moisture sensor AOUT** | none | Analog voltage into the ADS1115 |

**On the ESP32's internal pull-ups:** they exist but are weak (~45k). That
is fine for a rain sensor's digital output and useless for I2C - a 45k
pull-up gives slow rise times that work on a 10cm jumper and fail on a
garden run. **Do not rely on internal pull-ups for I2C.** If your ADS1115
breakout has no pull-up resistors, fit 4.7k-10k externally.

Note the two are opposites and easy to confuse: I2C wants pull-**ups** to
3.3V; a MOSFET gate wants a pull-**down** to ground.

## Optional sensors

**AHT20 + BMP280** (usually one combined board): wire to the same I2C bus as
the ADS1115. Auto-detected at boot at `0x38` (AHT20) and `0x76`/`0x77`
(BMP280) - no configuration needed. Readings appear in the Environment card.

**LM393 rain sensor**: connect its **DO** (digital out) pin to any free GPIO,
including input-only pins like 34-39. Assign it in the pin map. Output is LOW
when the plate is wet. Currently display-only; rain-skip logic is on the
roadmap.

**Flow meter (YF-S201)**: config and UI groundwork exist - each valve has an
optional `flow_meter_pin` - but pulse counting is not implemented, so
volume-based watering has no runtime effect yet.

---

## Power

The ESP32 and the valves should share a common ground but not necessarily a
common supply. A typical setup:

- 12V supply -> MOSFET modules -> solenoids
- USB or a 12V->5V buck converter -> ESP32

Never power a solenoid from the ESP32's 3.3V or 5V pin - it cannot supply
that current and will brown out and reset the board mid-watering.
