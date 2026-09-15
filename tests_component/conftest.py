# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import json
from types import MappingProxyType

import pytest
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.aqara_p3.protocol.models import DeviceIdentity, Snapshot


@pytest.fixture
def identity():
    return DeviceIdentity("02:00:00:00:00:01", "lumi.aircondition.acn05", "4.0.4")


@pytest.fixture
def raw_snapshot():
    def data(siid, values):
        return json.dumps(
            {
                "siid": siid,
                "property": [{"piid": p, "value": v} for p, v in values.items()],
            }
        )

    return {
        "power": data(12, {1: 0.26, 2: 196.0}),
        "ac": data(10, {1: True, 2: 2, 4: 27.0}),
        "fan": data(18, {2: 0, 4: True}),
        "relay": data(14, {1: True}),
        "ac_function": data(11, {9: "P0_M0_T27_S0_D0"}),
        "chip_temperature": "43",
    }


@pytest.fixture
def snapshot(identity, raw_snapshot):
    return Snapshot.decode(identity, **raw_snapshot)


@pytest.fixture
def book_bytes():
    keys = ["P0_M0_T27_S0_D0", "P1_M0_T27_S0_D0", "P0_M3_S0_D0"]
    return (
        "0|2|0|0\r\n1||3\r\n" + "".join(f"2||{k}|3|AQID\r\n" for k in keys) + "3|{}\r\n"
    ).encode()


@pytest.fixture
async def hass(tmp_path):
    from pathlib import Path

    from homeassistant import loader
    from homeassistant.helpers import (
        area_registry,
        device_registry,
        entity_registry,
        frame,
    )

    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parents[1] / "custom_components",
        target_is_directory=True,
    )
    instance = HomeAssistant(str(tmp_path))
    instance.config.skip_pip = True
    instance.config.language = "en"
    frame.async_setup(instance)
    loader.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()
    await area_registry.async_load(instance)
    device_registry.async_setup(instance)
    await device_registry.async_load(instance)
    await entity_registry.async_load(instance)
    yield instance
    await instance.async_stop(force=True)


@pytest.fixture
def config_entry(identity):
    return ConfigEntry(
        domain="aqara_p3",
        title="P3 test",
        version=1,
        minor_version=1,
        data={
            "host": "127.0.0.1",
            "password": "secret",
            "mac": identity.mac,
            "model": identity.model,
            "firmware": identity.firmware,
        },
        options={},
        source="user",
        unique_id=identity.uid,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )
