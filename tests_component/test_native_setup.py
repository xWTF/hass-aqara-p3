# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er


async def test_native_platform_setup_and_unload(hass, identity, snapshot, monkeypatch):
    device = AsyncMock()
    device.snapshot.return_value = snapshot
    monkeypatch.setattr(
        "custom_components.aqara_p3.coordinator.ReadOnlyDevice", lambda _: device
    )
    monkeypatch.setattr(
        "custom_components.aqara_p3.audio.AudioCoordinator._async_update_data",
        AsyncMock(
            return_value={
                "mode": 0,
                "target": 0,
                "alarm": 0,
                "volume": 40,
                "tones": ["DingDong", "PoliceCar_1"],
            }
        ),
    )
    # Keep the genuine transport configuration used for the coordinator's interval.
    from types import SimpleNamespace

    from custom_components.aqara_p3.protocol.config import DeviceConfig

    device.transport = SimpleNamespace(config=DeviceConfig("127.0.0.1"))
    entry = ConfigEntry(
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
        options={"temperature_entity": "sensor.room"},
        source="user",
        unique_id=identity.uid,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == 19
    disabled = {
        "chip_temperature",
        "on_timer",
        "off_timer",
        "coanda_sleep",
        "cancel_timers",
    }
    for entity in entities:
        key = entity.unique_id.removeprefix(identity.uid + "_")
        assert (entity.disabled_by == er.RegistryEntryDisabler.INTEGRATION) == (
            key in disabled
        )
    assert not any(e.unique_id.endswith("_stop_capture") for e in entities)
    power = next(e for e in entities if e.unique_id.endswith("_power_w"))
    assert hass.states.get(power.entity_id).state == "196.0"
    climate = next(e for e in entities if e.unique_id.endswith("_air_conditioner"))
    control = entry.runtime_data.control = AsyncMock()
    assert hass.states.get(climate.entity_id).state == "unknown"
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate.entity_id, "temperature": 28.5, "hvac_mode": "cool"},
        blocking=True,
    )
    control.send.assert_awaited_once()
    sent = control.send.call_args.args[0]
    assert sent.power and sent.mode == "cool" and sent.temperature == 28.5
    assert hass.states.get(climate.entity_id).state == "cool"
    hass.states.async_set("sensor.room", "77", {"unit_of_measurement": "°F"})
    await hass.async_block_till_done()
    assert hass.states.get(climate.entity_id).attributes["current_temperature"] == 25
    hass.states.async_set("sensor.room", "unavailable", {"unit_of_measurement": "°F"})
    await hass.async_block_till_done()
    assert hass.states.get(climate.entity_id).attributes["current_temperature"] is None
    from homeassistant.helpers import device_registry as dr

    devices = dr.async_get(hass)
    main_device = devices.async_get_device_by_identifier(
        ("aqara_p3", identity.uid), entry.entry_id
    )
    security_device = devices.async_get_device_by_identifier(
        ("aqara_p3", identity.uid + "_security"), entry.entry_id
    )
    assert main_device.id != security_device.id
    assert security_device.via_device_id == main_device.id
    assert "Sound" in security_device.name
    assert all(e.domain != "alarm_control_panel" for e in entities)
    sec_keys = {"sound", "sound_volume", "play_sound", "stop_sound"}
    for e in entities:
        if e.unique_id.removeprefix(identity.uid + "_") in sec_keys:
            assert e.device_id == security_device.id
    assert not any(e.unique_id.endswith("_outlet") for e in entities)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "aqara_p3",
            "turn_off_outlet",
            {"device_id": main_device.id, "confirm_power_cut": True},
            blocking=True,
        )
    control.relay.assert_not_awaited()
    await hass.services.async_call(
        "aqara_p3",
        "turn_off_outlet",
        {
            "device_id": main_device.id,
            "confirm_power_cut": True,
            "confirm_running": True,
        },
        blocking=True,
    )
    control.relay.assert_awaited_once_with(False)
    await hass.services.async_call(
        "aqara_p3",
        "play_sound",
        {"device_id": security_device.id, "tone": "DingDong", "volume": 20},
        blocking=True,
    )
    control.audio_write.assert_awaited_once_with(
        9, 1, '{"name":"DingDong","volume":20}'
    )
    await hass.services.async_call(
        "aqara_p3", "stop_sound", {"device_id": security_device.id}, blocking=True
    )
    assert entry.runtime_data.audio._playback_task is None
    control.audio_write.assert_awaited_with(9, 4, 1)
    await hass.services.async_call(
        "aqara_p3",
        "play_sound",
        {"device_id": security_device.id, "repeat": 3, "stop_after": 0.01},
        blocking=True,
    )
    import asyncio

    await asyncio.wait_for(entry.runtime_data.audio._playback_task, 1)
    control.audio_write.assert_awaited_with(9, 4, 1)
    dry = next(e for e in entities if e.unique_id.endswith("_dry_offset"))
    assert dry.domain == "select"
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": climate.entity_id, "hvac_mode": "dry"},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": dry.entity_id, "option": "-1.5"},
        blocking=True,
    )
    assert control.send.call_args.args[0].dry_offset == -1.5
    removed = [
        registry.async_get_or_create(
            platform, "aqara_p3", identity.uid + "_" + key, config_entry=entry
        )
        for platform, key in [
            ("switch", "outlet"),
            ("number", "dry_offset"),
            ("button", "stop_capture"),
            ("alarm_control_panel", "security_alarm"),
            ("sensor", "ac_mode"),
            ("sensor", "fan_mode"),
            ("sensor", "target_temperature"),
            ("binary_sensor", "ac_on"),
        ]
    ]
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert all(registry.async_get(e.entity_id) is None for e in removed)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state == ConfigEntryState.NOT_LOADED
    device.close.assert_awaited()
