# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.aqara_p3.energy import EnergyCounter, validate_sample
from custom_components.aqara_p3.protocol.errors import InvalidData


def sample(committed=0, pending=0, boot=1):
    return {
        "epoch": f"00000000-0000-0000-0000-{boot:012d}:100:1000",
        "committed_wh": committed,
        "pending_ws": pending,
    }


def counter(hass, stored=None):
    instance = EnergyCounter(hass, "test")
    instance.store = Mock()
    instance.store.async_load = AsyncMock(return_value=stored)
    instance.store.async_save = AsyncMock()
    return instance


async def test_actual_native_integral_and_repeated_read(hass):
    meter = counter(hass)
    assert await meter.update(sample(10, 3600)) == pytest.approx(0.011)
    assert await meter.update(sample(10, 3600)) == pytest.approx(0.011)
    assert await meter.update(sample(10, 7200)) == pytest.approx(0.012)
    assert meter.store.async_save.await_count == 1
    meter.store.async_delay_save.assert_called_once()
    await meter.shutdown()
    assert meter.store.async_save.await_count == 2


async def test_native_commit_rounding_does_not_count_as_rollover(hass):
    meter = counter(hass)
    initial = await meter.update(sample(0, 12079))
    assert await meter.update(sample(3, 0)) == initial
    assert await meter.update(sample(3, 3600)) == pytest.approx(initial + 0.001)


async def test_reboot_preserves_observed_pending_and_counts_new_interval(hass):
    meter = counter(hass)
    assert await meter.update(sample(10, 3600)) == pytest.approx(0.011)
    assert await meter.update(sample(10, 1800, boot=2)) == pytest.approx(0.0115)
    assert await meter.update(sample(10, 3600, boot=2)) == pytest.approx(0.012)


async def test_native_commit_before_reboot_is_not_counted_twice(hass):
    meter = counter(hass)
    assert await meter.update(sample(10, 3600)) == pytest.approx(0.011)
    assert await meter.update(sample(12, 1800, boot=2)) == pytest.approx(0.0125)


async def test_factory_reset_keeps_ha_total(hass):
    meter = counter(hass)
    assert await meter.update(sample(100, 3600)) == pytest.approx(0.101)
    assert await meter.update(sample(0, 1800, boot=2)) == pytest.approx(0.1015)


async def test_ha_restart_reuses_saved_native_baseline(hass):
    saved = {"total_ws": 360000, "sample": sample(10, 3600)}
    meter = counter(hass, saved)
    assert await meter.update(sample(10, 7200)) == pytest.approx(0.101)
    assert await meter.update(sample(10, 7200)) == pytest.approx(0.101)


@pytest.mark.parametrize(
    "changes",
    [
        {"pending_ws": -1},
        {"pending_ws": 8000001},
        {"committed_wh": True},
        {"epoch": "unverified"},
        {"pending_ws": 1.5},
    ],
)
def test_reject_invalid_firmware_counters(changes):
    with pytest.raises(InvalidData):
        validate_sample({**sample(), **changes})


async def test_invalid_sample_does_not_modify_stored_total(hass):
    meter = counter(hass)
    await meter.update(sample(5, 3600))
    before = dict(meter.data)
    with pytest.raises(InvalidData):
        await meter.update({**sample(), "pending_ws": -1})
    assert meter.data == before


async def test_unsupported_firmware_never_reads_process_memory():
    from custom_components.aqara_p3.protocol.config import DeviceConfig
    from custom_components.aqara_p3.protocol.control import P3Control

    control = P3Control(DeviceConfig("127.0.0.1"), "p3_020000000001")
    control._prepare = AsyncMock()
    control.data_session = AsyncMock()
    control.data_session.run.return_value = "wrong /bin/mha_ir\nwrong /lib/libha_ir.so"
    with pytest.raises(InvalidData, match="程序版本"):
        await control.read_energy()
    assert control.data_session.run.await_count == 1
    assert "energy" not in control.data_session.run.call_args.args[0]
    control.data_session.close.assert_awaited_once()
