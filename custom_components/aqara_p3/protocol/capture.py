# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Bounded UART framing for GUI captures, independent of any remote profile."""

import struct
from datetime import datetime, timezone

from .errors import InvalidData
from .profiles.daikin_p3 import DaikinP3


def crc_x25(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc ^ 0xFFFF


def decode_daikin(pulses):
    if len(pulses) != 320:
        return None
    if not (3000 < pulses[12] < 4000 and 1400 < pulses[13] < 2000):
        return None
    bits = []
    for i in range(14, 318, 2):
        mark, space = pulses[i : i + 2]
        if not 250 < mark < 700 or not (250 < space < 700 or 1000 < space < 1900):
            return None
        bits.append(int(space > 800))
    state = bytes(sum(bits[i + j] << j for j in range(8)) for i in range(0, 152, 8))
    try:
        return DaikinP3(state)
    except InvalidData:
        return None


class CaptureDecoder:
    def __init__(self):
        self.pending = bytearray()
        self.frames = []
        self.errors = 0
        self.total = 0

    def feed(self, data):
        self.pending.extend(data)
        while len(self.pending) >= 8:
            if self.pending[:2] != b"\xff\xfe":
                pos = self.pending.find(b"\xff\xfe", 1)
                if pos < 0:
                    self.pending = self.pending[-1:]
                    self.errors += 1
                    return
                del self.pending[:pos]
                self.errors += 1
            size = int.from_bytes(self.pending[4:6], "big")
            if size > 8192:
                del self.pending[:2]
                self.errors += 1
                continue
            # For IR pulse payloads 0xFFFE0026 cannot be a valid pulse pair.
            next_header = self.pending.find(b"\xff\xfe\x00\x26", 4)
            if 0 <= next_header < size + 8:
                del self.pending[:next_header]
                self.errors += 1
                continue
            if len(self.pending) < size + 8:
                return
            raw = bytes(self.pending[: size + 8])
            del self.pending[: size + 8]
            if crc_x25(raw[:-2]) != int.from_bytes(raw[-2:], "big"):
                self.errors += 1
                continue
            if raw[2:4] != b"\x00\x26" or size % 2:
                continue
            pulses = list(struct.unpack(f">{size // 2}H", raw[6:-2]))
            state = decode_daikin(pulses)
            record = {
                "utc": datetime.now(timezone.utc).isoformat(),
                "uart_hex": raw.hex(),
                "pulses": pulses,
                "profile": "daikin_p3" if state else None,
                "state_hex": state.raw.hex() if state else None,
            }
            if state:
                record.update(
                    mode=state.mode,
                    power=state.power,
                    temperature=state.temperature,
                    dry_offset=state.dry_offset,
                    fan=state.fan,
                    swing=state.swing,
                )
            self.total += 1
            self.frames.append(record)
            self.frames = self.frames[-64:]

    def result(self):
        return {
            "total_frames": self.total,
            "errors": self.errors + bool(self.pending),
            "frames": self.frames,
        }
