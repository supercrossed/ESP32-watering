# env_sensors.py
# Minimal drivers for the common AHT20+BMP280 combo board. Both chips sit
# on the shared I2C bus (same two pins as the ADS1115s) at fixed
# addresses: AHT20 = 0x38, BMP280 = 0x76 or 0x77. main.py auto-detects
# them with an I2C scan at boot - no configuration needed.

import time
import struct

AHT20_ADDR = 0x38
BMP280_ADDRS = (0x76, 0x77)


class AHT20:
    """Temperature + relative humidity."""

    def __init__(self, i2c, address=AHT20_ADDR):
        self.i2c = i2c
        self.address = address
        time.sleep_ms(40)  # power-on time per datasheet
        status = self.i2c.readfrom(self.address, 1)[0]
        if not status & 0x08:  # calibration bit clear -> initialize
            self.i2c.writeto(self.address, b"\xbe\x08\x00")
            time.sleep_ms(10)

    def read(self):
        """-> (temp_c, humidity_percent). Blocks ~85ms for the conversion."""
        self.i2c.writeto(self.address, b"\xac\x33\x00")
        time.sleep_ms(85)
        d = self.i2c.readfrom(self.address, 7)
        if d[0] & 0x80:
            raise OSError("AHT20 measurement not ready")
        hraw = (d[1] << 12) | (d[2] << 4) | (d[3] >> 4)
        traw = ((d[3] & 0x0F) << 16) | (d[4] << 8) | d[5]
        return traw / 1048576 * 200 - 50, hraw / 1048576 * 100


class BMP280:
    """Temperature + barometric pressure. Uses the Bosch datasheet's
    double-precision compensation formulas."""

    def __init__(self, i2c, address=BMP280_ADDRS[0]):
        self.i2c = i2c
        self.address = address
        cal = self.i2c.readfrom_mem(address, 0x88, 24)
        (self.T1, self.T2, self.T3,
         self.P1, self.P2, self.P3, self.P4, self.P5,
         self.P6, self.P7, self.P8, self.P9) = struct.unpack("<HhhHhhhhhhhh", cal)
        # standby 500ms, filter off
        self.i2c.writeto_mem(address, 0xF5, b"\xa0")
        # osrs_t x2, osrs_p x16, normal mode
        self.i2c.writeto_mem(address, 0xF4, b"\x57")

    def read(self):
        """-> (temp_c, pressure_pa)"""
        d = self.i2c.readfrom_mem(self.address, 0xF7, 6)
        praw = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        traw = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)

        v1 = (traw / 16384.0 - self.T1 / 1024.0) * self.T2
        v2 = ((traw / 131072.0 - self.T1 / 8192.0) ** 2) * self.T3
        t_fine = v1 + v2
        temp_c = t_fine / 5120.0

        v1 = t_fine / 2.0 - 64000.0
        v2 = v1 * v1 * self.P6 / 32768.0 + v1 * self.P5 * 2.0
        v2 = v2 / 4.0 + self.P4 * 65536.0
        v1 = (self.P3 * v1 * v1 / 524288.0 + self.P2 * v1) / 524288.0
        v1 = (1.0 + v1 / 32768.0) * self.P1
        if v1 == 0:
            return temp_c, 0.0
        p = 1048576.0 - praw
        p = (p - v2 / 4096.0) * 6250.0 / v1
        v1 = self.P9 * p * p / 2147483648.0
        v2 = p * self.P8 / 32768.0
        p = p + (v1 + v2 + self.P7) / 16.0
        return temp_c, p
