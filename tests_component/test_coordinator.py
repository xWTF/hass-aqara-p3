# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from custom_components.aqara_p3.coordinator import P3Coordinator
from custom_components.aqara_p3.protocol.errors import AuthenticationError, InvalidData


async def test_refresh_native_entities_and_diagnostics(hass, config_entry, snapshot):
    from custom_components.aqara_p3.binary_sensor import DESCRIPTIONS as BINARY
    from custom_components.aqara_p3.binary_sensor import P3BinarySensor
    from custom_components.aqara_p3.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    from custom_components.aqara_p3.sensor import DESCRIPTIONS as SENSORS
    from custom_components.aqara_p3.sensor import P3Sensor

    coordinator = P3Coordinator(hass, config_entry)
    coordinator.device = AsyncMock()
    coordinator.device.snapshot.return_value = snapshot
    try:
        await coordinator.async_config_entry_first_refresh()
        sensors = {d.key: P3Sensor(coordinator, d) for d in SENSORS}
        binary = {d.key: P3BinarySensor(coordinator, d) for d in BINARY}
        assert sensors["power_w"].native_value == 196
        assert sensors["energy_kwh"].native_value == 0.26
        assert binary["relay_on"].is_on is True
        assert sensors["chip_temperature"].entity_registry_enabled_default is False
        assert set(sensors) == {"power_w", "energy_kwh", "chip_temperature"}
        assert set(binary) == {"relay_on"}
        config_entry.runtime_data = coordinator
        diag = await async_get_config_entry_diagnostics(hass, config_entry)
        assert "secret" not in str(diag)
        assert "127.0.0.1" not in str(diag)
    finally:
        await coordinator.async_shutdown()


async def test_offline_first_refresh_has_a_deadline(hass, config_entry, monkeypatch):
    coordinator = P3Coordinator(hass, config_entry)
    coordinator.device = AsyncMock()

    async def hang():
        await asyncio.Event().wait()

    coordinator.device.snapshot.side_effect = hang
    monkeypatch.setattr("custom_components.aqara_p3.coordinator.UPDATE_TIMEOUT", 0.03)
    heartbeat = False

    async def tick():
        nonlocal heartbeat
        await asyncio.sleep(0.005)
        heartbeat = True

    task = asyncio.create_task(tick())
    try:
        async with asyncio.timeout(0.5):
            with pytest.raises(ConfigEntryNotReady):
                await coordinator.async_config_entry_first_refresh()
        assert heartbeat
        assert not coordinator.last_update_success
    finally:
        await coordinator.async_shutdown()
        await task


async def test_auth_failure_requests_reauth(hass, config_entry):
    coordinator = P3Coordinator(hass, config_entry)
    coordinator.device = AsyncMock()
    coordinator.device.snapshot.side_effect = AuthenticationError("do not log password")
    try:
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator.async_config_entry_first_refresh()
    finally:
        await coordinator.async_shutdown()


async def test_energy_failure_keeps_power_and_controls_available(
    hass, config_entry, snapshot
):
    coordinator = P3Coordinator(hass, config_entry)
    coordinator.device = AsyncMock()
    coordinator.device.snapshot.return_value = snapshot
    coordinator.control.read_energy = AsyncMock(
        side_effect=InvalidData("unsupported firmware")
    )
    try:
        await coordinator.async_config_entry_first_refresh()
        assert coordinator.last_update_success
        assert coordinator.data["power_w"] == 196
        assert coordinator.data["relay_on"] is True
        assert coordinator.data["energy_kwh"] is None
        assert coordinator.energy_error == "unsupported firmware"
    finally:
        await coordinator.async_shutdown()


async def test_shutdown_cancels_inflight_read(hass, config_entry):
    coordinator = P3Coordinator(hass, config_entry)
    coordinator.device = AsyncMock()
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    coordinator.device.snapshot.side_effect = hang
    task = asyncio.create_task(coordinator._async_update_data())
    await started.wait()
    async with asyncio.timeout(0.5):
        await coordinator.async_shutdown()
    assert task.cancelled()
    coordinator.device.close.assert_awaited()
