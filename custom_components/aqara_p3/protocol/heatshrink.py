# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Bounded Heatshrink (window=11/lookahead=4), matching local firmware codec."""

import base64


def decompress(data, window=11, lookahead=4):
    bits = "".join(f"{b:08b}" for b in data)
    out = bytearray()
    pos = 0
    while pos < len(bits):
        tag = bits[pos]
        pos += 1
        if tag == "1":
            if len(bits) - pos < 8:
                break
            out.append(int(bits[pos : pos + 8], 2))
            pos += 8
        else:
            if len(bits) - pos < window + lookahead:
                break
            distance = int(bits[pos : pos + window], 2) + 1
            pos += window
            length = int(bits[pos : pos + lookahead], 2) + 1
            pos += lookahead
            for _ in range(length):
                out.append(out[-distance] if distance <= len(out) else 0)
        if len(out) > 4096:
            raise ValueError("decoded data too large")
    return bytes(out)


def compress(data, window=11, lookahead=4):
    if not 0 < len(data) <= 4096:
        raise ValueError("invalid data size")
    bits = []
    pos = 0
    while pos < len(data):
        best = 0
        distance = 0
        for d in range(1, min(pos, 1 << window) + 1):
            n = 0
            while (
                n < min(1 << lookahead, len(data) - pos)
                and data[pos + n] == data[pos + n - d]
            ):
                n += 1
            if n > best:
                best, distance = n, d
            if best == 1 << lookahead:
                break
        if best >= 2:
            bits.append(
                "0" + f"{distance - 1:0{window}b}" + f"{best - 1:0{lookahead}b}"
            )
            pos += best
        else:
            bits.append("1" + f"{data[pos]:08b}")
            pos += 1
    packed = "".join(bits)
    packed += "0" * (-len(packed) % 8)
    result = bytes(int(packed[i : i + 8], 2) for i in range(0, len(packed), 8))
    if decompress(result, window, lookahead) != data:
        raise ValueError("codec roundtrip failure")
    return result


def encode_pulses(pulses):
    if not 10 <= len(pulses) <= 1024 or any(not 10 <= p <= 65535 for p in pulses):
        raise ValueError("invalid waveform")
    plain = ",".join(map(str, pulses)).encode("ascii")
    window = 11 if len(plain) <= 2048 else 12
    return base64.b64encode(compress(plain, window)).decode(), len(plain)
