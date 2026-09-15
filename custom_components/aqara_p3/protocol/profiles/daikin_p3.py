# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Daikin E-Max 7 152-bit protocol, verified against labelled test vectors.

Unlike generic Daikin152, this remote has half-degree setpoints, signed dry
offsets, independent horizontal swing, Coanda and rapid-operation bits.
Special-function mappings come from paired captures; physical effects remain
unverified except power and fan. Keep unrelated/unknown bits when editing.
"""

import math
from dataclasses import dataclass

from ..errors import InvalidData

MODES = {"auto": 0, "dry": 2, "cool": 3, "heat": 4, "fan_only": 6}
FANS = {"auto": 10, "quiet": 11, "1": 3, "2": 4, "3": 5, "4": 6, "5": 7}
RANGES = {"cool": (18, 32), "heat": (10, 30), "auto": (18, 30)}
POWERFUL_MODES = frozenset({"cool", "heat"})
POWERFUL_DURATION = 20 * 60
FEATURES = {
    "powerful": (13, 0x01),
    "rapid": (17, 0x01),
    "coanda": (17, 0x02),
    "econo": (16, 0x04),
    "outdoor_quiet": (13, 0x20),
}


def half(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise InvalidData("Invalid temperature")
    if not low <= value <= high or value * 2 != int(value * 2):
        raise InvalidData(f"Expected {low}–{high} in steps of 0.5")
    return int(value * 2)


@dataclass(frozen=True)
class DaikinP3:
    raw: bytes

    def __post_init__(self):
        if (
            len(self.raw) != 19
            or self.raw[:3] != b"\x11\xda\x27"
            or self.raw[15] != 0xC5
            or not self.raw[16] & 0x80
            or sum(self.raw[:-1]) & 255 != self.raw[-1]
        ):
            raise InvalidData("Invalid Daikin P3 state/checksum")
        if self.mode not in MODES or self.fan not in FANS:
            raise InvalidData("Unrecognized Daikin P3 mode/fan")

    @classmethod
    def default(cls):
        raw = bytearray.fromhex(
            "11 DA 27 00 00 30 36 00 A0 00 00 00 00 00 00 C5 C0 00 00"
        )
        raw[-1] = sum(raw[:-1]) & 255
        return cls(bytes(raw))

    @classmethod
    def from_hex(cls, value):
        try:
            return cls(bytes.fromhex(value))
        except (TypeError, ValueError):
            raise InvalidData("Invalid saved state") from None

    @property
    def power(self):
        return bool(self.raw[5] & 1)

    @property
    def mode(self):
        return next((k for k, v in MODES.items() if v == (self.raw[5] >> 4) & 7), None)

    @property
    def fan(self):
        return next((k for k, v in FANS.items() if v == self.raw[8] >> 4), None)

    @property
    def temperature(self):
        return self.raw[6] / 2 if self.mode in RANGES else None

    @property
    def dry_offset(self):
        n = self.raw[6] & 31
        return ((n - 32) if n & 16 else n) / 2 if self.mode == "dry" else None

    @property
    def swing(self):
        v, h = bool(self.raw[8] & 15), bool(self.raw[9] & 15)
        return "both" if v and h else "vertical" if v else "horizontal" if h else "off"

    def feature(self, key):
        index, mask = FEATURES[key]
        return bool(self.raw[index] & mask)

    @property
    def on_minutes(self):
        return self.raw[10] | ((self.raw[11] & 15) << 8) if self.raw[5] & 2 else 0

    @property
    def off_minutes(self):
        return (self.raw[11] >> 4) | (self.raw[12] << 4) if self.raw[5] & 4 else 0

    @property
    def sleep_minutes(self):
        return self.raw[10] | ((self.raw[11] & 15) << 8) if self.raw[16] & 0x20 else 0

    def timers(self, *, on=None, off=None, sleep=None):
        b = bytearray(self.raw)
        for value in (on, off, sleep):
            if value is not None and (type(value) is not int or not 0 <= value <= 720):
                raise InvalidData("Timer must be 0–720 minutes")
        if on is not None and sleep is not None and on and sleep:
            raise InvalidData("On timer and Coanda sleep share a timer field")
        if on is not None:
            b[5] = (b[5] & ~2) | (2 if on else 0)
            if on or not b[16] & 0x20:
                b[16] &= ~0x20
                b[10] = on & 255
                b[11] = (b[11] & 0xF0) | (on >> 8)
        if sleep is not None:
            b[16] = (b[16] & ~0x20) | (0x20 if sleep else 0)
            if sleep or not b[5] & 2:
                b[5] &= ~2
                b[10] = sleep & 255
                b[11] = (b[11] & 0xF0) | (sleep >> 8)
            if sleep:
                b[8] = (b[8] & 15) | 0xA0
                b[17] |= 2
        if off is not None:
            b[5] = (b[5] & ~4) | (4 if off else 0)
            b[11] = (b[11] & 15) | ((off & 15) << 4)
            b[12] = off >> 4
        b[-1] = sum(b[:-1]) & 255
        return type(self)(bytes(b))

    def changed(self, **changes):
        allowed = {
            "power",
            "mode",
            "temperature",
            "dry_offset",
            "fan",
            "swing",
            *FEATURES,
        }
        if set(changes) - allowed:
            raise InvalidData("Unknown protocol field")
        b = bytearray(self.raw)
        mode = changes.get("mode", self.mode)
        if mode not in MODES:
            raise InvalidData("Unsupported HVAC mode")
        if mode != self.mode:
            b[5] = (b[5] & 0x8F) | (MODES[mode] << 4)
            if mode == "dry":
                b[6] = 0xC0
                b[8] = (b[8] & 15) | 0xA0
            elif mode == "fan_only":
                b[6] = 0x32
            else:
                lo, hi = RANGES[mode]
                b[6] = half(
                    min(
                        hi,
                        max(
                            lo, self.temperature if self.temperature is not None else 25
                        ),
                    ),
                    lo,
                    hi,
                )
        if "power" in changes:
            if type(changes["power"]) is not bool:
                raise InvalidData("Invalid power")
            b[5] = (b[5] & ~1) | int(changes["power"])
            b[16] = (b[16] & ~0x40) | (0 if changes["power"] else 0x40)
        if "temperature" in changes:
            if mode not in RANGES:
                raise InvalidData("This mode uses no absolute setpoint")
            b[6] = half(changes["temperature"], *RANGES[mode])
        if "dry_offset" in changes:
            if mode != "dry":
                raise InvalidData("Select dry mode first")
            b[6] = 0xC0 | (half(changes["dry_offset"], -2, 2) & 31)
        if "fan" in changes:
            if changes["fan"] not in FANS:
                raise InvalidData("Unsupported fan")
            b[8] = (b[8] & 15) | (FANS[changes["fan"]] << 4)
        if "swing" in changes:
            swing = changes["swing"]
            if swing not in ("off", "vertical", "horizontal", "both"):
                raise InvalidData("Unsupported swing")
            b[8] = (b[8] & 0xF0) | (15 if swing in ("vertical", "both") else 0)
            b[9] = (b[9] & 0xF0) | (15 if swing in ("horizontal", "both") else 0)
        for key, (index, mask) in FEATURES.items():
            if key not in changes:
                continue
            if type(changes[key]) is not bool:
                raise InvalidData("Invalid feature")
            b[index] = (b[index] & ~mask) | (mask if changes[key] else 0)
            # Captured Powerful activation cancels Rapid; conservatively keep
            # mutually exclusive boost/economy/quiet choices consistent.
            if changes[key] and key in ("powerful", "rapid", "econo", "outdoor_quiet"):
                others = (
                    ("rapid", "econo", "outdoor_quiet")
                    if key == "powerful"
                    else ("powerful",)
                )
                for other in others:
                    oi, om = FEATURES[other]
                    b[oi] &= ~om
        b[-1] = sum(b[:-1]) & 255
        return type(self)(bytes(b))

    def pulses(self):
        # Timings taken from the device's existing Daikin codebook. Carrier is
        # supplied by the factory transmitter; a complete 152-bit transaction.
        out = [420, 440] * 5 + [420, 25200, 3500, 1700]
        for byte in self.raw:
            for bit in range(8):
                out.extend((420, 1300 if byte & (1 << bit) else 440))
        return out + [420, 50000]
