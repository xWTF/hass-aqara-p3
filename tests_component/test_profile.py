# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import json
from pathlib import Path

import pytest

from custom_components.aqara_p3.protocol.capture import (
    CaptureDecoder,
    crc_x25,
    decode_daikin,
)
from custom_components.aqara_p3.protocol.errors import InvalidData
from custom_components.aqara_p3.protocol.heatshrink import (
    compress,
    decompress,
    encode_pulses,
)
from custom_components.aqara_p3.protocol.profiles.daikin_p3 import DaikinP3


def fixture(name):
    return json.loads(
        (Path(__file__).parent / "fixtures" / (name + ".json")).read_text()
    )


@pytest.mark.parametrize("name", ["buttons", "calibration", "modes", "timers"])
def test_real_capture_roundtrip(name):
    for f in fixture(name):
        state = DaikinP3.from_hex(f["hex"])
        assert decode_daikin(f["pulses"]) == state
        assert decode_daikin(state.pulses()) == state
        assert state.changed().raw == state.raw
        encoded, length = encode_pulses(state.pulses())
        import base64

        plain = decompress(base64.b64decode(encoded))
        assert len(plain) == length
        assert list(map(int, plain.split(b","))) == state.pulses()


def test_labelled_sweeps():
    rows = fixture("calibration")
    fans = ["auto", "quiet", "1", "2", "3", "4", "5"]
    base = DaikinP3.from_hex(rows[0]["hex"])
    for row, fan in zip(rows[:7], fans, strict=True):
        assert base.changed(fan=fan).raw == bytes.fromhex(row["hex"])
    base = DaikinP3.from_hex(rows[7]["hex"])
    for i, row in enumerate(rows[7:36]):
        assert base.changed(temperature=32 - i / 2).raw == bytes.fromhex(row["hex"])
    base = DaikinP3.from_hex(rows[38]["hex"])
    for i, row in enumerate(rows[38:47]):
        assert base.changed(dry_offset=-2 + i / 2).raw == bytes.fromhex(row["hex"])


def test_timer_packing_and_cancellation():
    rows = fixture("timers")
    base = DaikinP3.from_hex(rows[3]["hex"])
    assert base.timers(sleep=60).raw == bytes.fromhex(rows[0]["hex"])
    assert base.timers(sleep=120).raw == bytes.fromhex(rows[1]["hex"])
    assert base.timers(on=120).raw == bytes.fromhex(rows[4]["hex"])
    assert base.timers(on=720).raw == bytes.fromhex(rows[14]["hex"])
    both = base.timers(on=60, off=60)
    assert both.raw == bytes.fromhex(rows[17]["hex"])
    assert base.timers(on=60, off=720).raw == bytes.fromhex(rows[28]["hex"])
    assert both.timers(off=0).raw == bytes.fromhex(rows[29]["hex"])
    assert base.timers(on=60).timers(sleep=120).on_minutes == 0
    assert base.timers(sleep=120).timers(on=60).sleep_minutes == 0
    assert base.timers(on=120).timers(sleep=0).on_minutes == 120
    assert base.timers(sleep=120).timers(on=0).sleep_minutes == 120
    assert base.timers(on=120, sleep=0).on_minutes == 120


@pytest.mark.parametrize(
    "changes",
    [
        {"temperature": 32.5},
        {"temperature": 17.5},
        {"temperature": float("nan")},
        {"temperature": 27.3},
        {"fan": "maximum"},
        {"power": 1},
        {"mode": "turbo"},
        {"swing": "sideways"},
        {"dry_offset": 1},
        {"powerful": "true"},
    ],
)
def test_reject_invalid_state(changes):
    with pytest.raises(InvalidData):
        DaikinP3.default().changed(**changes)


def test_feature_preserves_other_fields_and_mutual_boost():
    state = DaikinP3.default().changed(
        power=True, mode="heat", temperature=10, swing="both", rapid=True
    )
    changed = state.changed(powerful=True)
    assert changed.feature("powerful") and not changed.feature("rapid")
    assert (
        changed.mode == "heat" and changed.temperature == 10 and changed.swing == "both"
    )
    assert changed.changed(econo=True).feature("powerful") is False


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"powerful": True, "rapid": True, "econo": True, "outdoor_quiet": True},
            {"powerful"},
        ),
        (
            {"outdoor_quiet": True, "econo": True, "rapid": True, "powerful": True},
            {"powerful"},
        ),
        ({"rapid": True, "econo": True, "outdoor_quiet": True}, {"rapid"}),
    ],
)
def test_conflicting_batch_has_explicit_priority(changes, expected):
    state = DaikinP3.default().changed(**changes)
    assert {
        key
        for key in ("powerful", "rapid", "econo", "outdoor_quiet")
        if state.feature(key)
    } == expected


def test_capture_fragmentation_crc_and_unknown_protocol():
    import struct

    state = DaikinP3.default()
    raw = b"\xff\xfe\x00\x26\x02\x80" + struct.pack(">320H", *state.pulses())
    raw += crc_x25(raw).to_bytes(2, "big")
    decoder = CaptureDecoder()
    for i in range(0, len(raw), 7):
        decoder.feed(raw[i : i + 7])
    assert decoder.frames[0]["state_hex"] == state.raw.hex()
    broken = bytearray(raw)
    broken[40] ^= 1
    decoder.feed(broken)
    assert decoder.result()["errors"] == 1
    assert decoder.total == 1
    # A different valid waveform is retained for a new profile, not discarded.
    raw = b"\xff\xfe\x00\x26\x00\x04" + struct.pack(">HH", 1000, 2000)
    raw += crc_x25(raw).to_bytes(2, "big")
    decoder.feed(raw)
    assert decoder.frames[-1]["profile"] is None


def test_codec_limit_and_roundtrip():
    data = b"420,440," * 100
    assert decompress(compress(data)) == data
    with pytest.raises(ValueError):
        encode_pulses([0] * 320)
