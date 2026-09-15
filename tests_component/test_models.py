# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import json

import pytest

from custom_components.aqara_p3.protocol.config import DeviceConfig
from custom_components.aqara_p3.protocol.errors import InvalidData, UnsupportedDevice
from custom_components.aqara_p3.protocol.models import (
    DeviceIdentity,
    Snapshot,
    legacy_hex_cstring,
    properties,
)


def test_real_p3_property_mapping(snapshot):
    assert snapshot.values["power_w"] == 196
    assert snapshot.values["energy_kwh"] == 0.26
    assert snapshot.values["ac_mode"] == "cool"
    assert snapshot.values["relay_on"] is True
    assert snapshot.values["chip_temperature"] == 43
    assert "current_temperature" not in snapshot.values
    assert "sampled_at" not in snapshot.payload()


def test_encoded_persistent_values_are_not_decimal():
    assert legacy_hex_cstring("32363000") == "260"
    assert legacy_hex_cstring("3100") == "1"
    with pytest.raises(InvalidData):
        legacy_hex_cstring("196")
    with pytest.raises(InvalidData):
        legacy_hex_cstring("310000")


@pytest.mark.parametrize("bad", [True, "196", -1, float("nan"), float("inf"), 9000])
def test_reject_invalid_power(identity, raw_snapshot, bad):
    p = json.loads(raw_snapshot["power"])
    p["property"][1]["value"] = bad
    raw_snapshot["power"] = json.dumps(p)
    with pytest.raises(InvalidData):
        Snapshot.decode(identity, **raw_snapshot)


def test_off_does_not_mean_cut_mains(identity, raw_snapshot):
    a = json.loads(raw_snapshot["ac"])
    a["property"][0]["value"] = False
    raw_snapshot["ac"] = json.dumps(a)
    state = Snapshot.decode(identity, **raw_snapshot)
    assert state.values["ac_mode"] == "off"
    assert state.values["relay_on"] is True


def test_zero_temperature_is_not_measurement(identity, raw_snapshot):
    a = json.loads(raw_snapshot["ac"])
    a["property"][2]["value"] = 0
    raw_snapshot["ac"] = json.dumps(a)
    assert (
        Snapshot.decode(identity, **raw_snapshot).values["target_temperature"] is None
    )


def test_power_property_takes_precedence_over_slow_json_cache(identity, raw_snapshot):
    state = Snapshot.decode(identity, **raw_snapshot, load_power="321\n")
    assert state.values["power_w"] == 321
    assert state.values["power_source"] == "device_property"
    assert state.values["energy_kwh"] == 0.26
    state = Snapshot.decode(identity, **raw_snapshot, load_power="0")
    assert state.values["power_w"] == 0


@pytest.mark.parametrize("raw", [None, "", " \n"])
def test_missing_power_property_uses_labelled_cache(identity, raw_snapshot, raw):
    state = Snapshot.decode(identity, **raw_snapshot, load_power=raw)
    assert state.values["power_w"] == 196
    assert state.values["power_source"] == "firmware_cache"


@pytest.mark.parametrize("raw", ["nan", "inf", "-1", "5001", "bad"])
def test_invalid_power_property_is_not_treated_as_fresh_cache(
    identity, raw_snapshot, raw
):
    with pytest.raises(InvalidData):
        Snapshot.decode(identity, **raw_snapshot, load_power=raw)


async def test_snapshot_reads_live_power_property(identity, raw_snapshot):
    from unittest.mock import AsyncMock

    from custom_components.aqara_p3.protocol.device import ReadOnlyDevice
    from custom_components.aqara_p3.protocol.telnet import Resource

    responses = {Resource(k): v for k, v in raw_snapshot.items()}
    responses[Resource.LOAD_POWER] = "321"
    transport = AsyncMock()
    transport.connected = True
    transport.read.side_effect = lambda resource: responses[resource]
    device = ReadOnlyDevice(transport)
    device.identity = identity
    state = await device.snapshot()
    assert state.values["power_w"] == 321
    assert state.values["energy_kwh"] == 0.26


@pytest.mark.parametrize(
    "raw",
    [
        '{"siid":12,"siid":12,"property":[]}',
        '{"siid":12,"property":[{"piid":1,"value":1},{"piid":1,"value":2}]}',
        '{"siid":true,"property":[]}',
        '{"siid":11,"property":[]}',
        '{"siid":12,"property":[{"piid":1}]}',
    ],
)
def test_reject_ambiguous_properties(raw):
    with pytest.raises(InvalidData):
        properties(raw, 12)


def test_credentials_repr_and_validation():
    config = DeviceConfig("127.0.0.1", "private-$()'password")
    assert "private" not in repr(config)
    with pytest.raises(InvalidData):
        DeviceConfig("127.0.0.1", "a\ncommand")
    with pytest.raises(InvalidData):
        DeviceConfig("example.com; command")
    with pytest.raises(UnsupportedDevice):
        DeviceIdentity("02:00:00:00:00:01", "some.other.device", "1")
