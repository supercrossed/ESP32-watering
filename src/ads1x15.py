# ads1x15.py
# Minimal MicroPython driver for the ADS1115 16-bit ADC (I2C).
# Single-ended, single-shot reads on channels 0-3.

import time

_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

_OS_SINGLE = 0x8000
_MUX_SINGLE = {0: 0x4000, 1: 0x5000, 2: 0x6000, 3: 0x7000}

# Programmable gain amplifier settings -> full-scale range in millivolts
_PGA = {
    6144: 0x0000,
    4096: 0x0200,
    2048: 0x0400,
    1024: 0x0600,
    512: 0x0800,
    256: 0x0A00,
}

_MODE_SINGLE = 0x0100
_DR_128SPS = 0x0080
_COMP_QUE_DISABLE = 0x0003


class ADS1115:
    def __init__(self, i2c, address=0x48, gain=6144):
        self.i2c = i2c
        self.address = address
        self.gain = gain  # +/- gain in mV, default 6144 = +/-6.144V full scale

    def read_raw(self, channel):
        if channel not in _MUX_SINGLE:
            raise ValueError("channel must be 0-3")
        config = (
            _OS_SINGLE
            | _MUX_SINGLE[channel]
            | _PGA[self.gain]
            | _MODE_SINGLE
            | _DR_128SPS
            | _COMP_QUE_DISABLE
        )
        buf = bytearray(2)
        buf[0] = (config >> 8) & 0xFF
        buf[1] = config & 0xFF
        self.i2c.writeto_mem(self.address, _REG_CONFIG, buf)
        time.sleep_ms(9)  # conversion time at 128SPS is ~8ms, pad a bit
        data = self.i2c.readfrom_mem(self.address, _REG_CONVERSION, 2)
        raw = (data[0] << 8) | data[1]
        if raw > 32767:
            raw -= 65536
        return raw

    def read_voltage(self, channel):
        raw = self.read_raw(channel)
        return raw * (self.gain / 1000.0) / 32768.0

    # Alias so calling code can just call .read()
    def read(self, channel):
        return self.read_raw(channel)
