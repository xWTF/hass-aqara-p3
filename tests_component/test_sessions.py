# SPDX-License-Identifier: GPL-3.0-only
"""Persistent session isolation, preparation and LED lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.aqara_p3.protocol.config import DeviceConfig
from custom_components.aqara_p3.protocol.control import P3Control
from custom_components.aqara_p3.protocol.errors import InvalidData
from custom_components.aqara_p3.protocol.native_info import SHA256

BASIS = "331b5e32feae71fc2f68ee6328ab86a428481841caf510ff8373875a90202687"


@pytest.fixture
def control():
    item = P3Control(DeviceConfig("127.0.0.1"), "p3_020000000001")
    item.data_session = SimpleNamespace(
        connected=True,
        connection_id="a" * 32,
        run=AsyncMock(return_value=BASIS + "  /bin/ha_basis"),
        close=AsyncMock(),
    )
    item._prepare = AsyncMock()
    return item


async def test_led_only_on_data_connection_and_reclaims_after_reconnect(control):
    await control.sync_data_led()
    control._prepare.assert_not_awaited()
    await control.sync_data_led(True)
    command = control.data_session.run.call_args.args[0]
    assert command.index("trap ") < command.index("led-session claim")
    assert "EXIT" in command and "HUP" in command
    assert "release " + "a" * 32 in command
    count = control.data_session.run.await_count
    await control.sync_data_led()
    await control.sync_data_led(True)
    assert control.data_session.run.await_count == count
    control.data_session.connection_id = "b" * 32
    await control.sync_data_led()
    assert "claim " + "b" * 32 in control.data_session.run.call_args.args[0]
    assert not control.session.connected


async def test_cloud_mode_disarms_and_invalidates_old_shell(control):
    await control.sync_data_led(True)
    await control.sync_data_led(False)
    assert "led-session reset " in control.data_session.run.call_args.args[0]
    assert "&& trap - EXIT HUP" in control.data_session.run.call_args.args[0]
    count = control.data_session.run.await_count
    await control.sync_data_led(False)
    await control.sync_data_led()
    assert control.data_session.run.await_count == count


async def test_led_failure_closes_shell_and_retries_on_new_connection(control):
    control.data_session.run.side_effect = [BASIS + "  /bin/ha_basis", TimeoutError()]
    with pytest.raises(TimeoutError):
        await control.sync_data_led(True)
    control.data_session.close.assert_awaited_once()
    control.data_session.run.side_effect = None
    control.data_session.connection_id = "c" * 32
    await control.sync_data_led()
    assert "claim " + "c" * 32 in control.data_session.run.call_args.args[0]


async def test_wrong_firmware_never_installs_trap(control):
    control.data_session.run.return_value = "unsupported"
    with pytest.raises(InvalidData):
        await control.sync_data_led(True)
    control.data_session.run.assert_awaited_once_with("busybox sha256sum /bin/ha_basis")


async def test_data_lock_does_not_block_control(control):
    control.session.run = AsyncMock(return_value="unused")
    entered = asyncio.Event()

    async def reply(*args, **kwargs):
        entered.set()
        raise TimeoutError()

    control.session.run.side_effect = reply
    async with control.data_lock:
        task = asyncio.create_task(control.relay(True))
        async with asyncio.timeout(1):
            await entered.wait()
        with pytest.raises(TimeoutError):
            await task


async def test_prepare_reused_only_for_same_authenticated_connection():
    control = P3Control(DeviceConfig("127.0.0.1"), "p3_020000000001")
    session = SimpleNamespace(
        connected=True,
        connection_id="a" * 32,
        run=AsyncMock(side_effect=[SHA256 + "  helper", "p3lan-native-1"] * 2),
    )
    control._verify_device = AsyncMock()
    await control._prepare(session)
    await control._prepare(session)
    control._verify_device.assert_awaited_once()
    assert session.run.await_count == 2
    session.connection_id = "b" * 32
    await control._prepare(session)
    assert control._verify_device.await_count == 2
    assert session.run.await_count == 4
