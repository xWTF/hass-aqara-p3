# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.aqara_p3.ac_controller import ACController
from custom_components.aqara_p3.protocol.profiles.daikin_p3 import DaikinP3


@pytest.fixture
async def entry(hass, config_entry, monkeypatch):
    monkeypatch.setattr(
        "custom_components.aqara_p3.async_setup_entry", AsyncMock(return_value=True)
    )
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    await hass.config_entries.async_add(config_entry)
    await hass.async_block_till_done()
    return config_entry


async def test_capture_options_prepare_receive_export(hass, entry):
    ready = asyncio.Event()
    finish = asyncio.Event()
    ac = SimpleNamespace(
        capture_started=ready,
        capture_result={},
        adopt_capture=AsyncMock(),
        stop_capture=AsyncMock(),
    )
    tasks = []

    def start(seconds):
        assert seconds == 60

        async def work():
            await ready.wait()
            await finish.wait()
            ac.capture_result = {
                "total_frames": 1,
                "errors": 0,
                "frames": [
                    {"profile": "daikin_p3", "state_hex": DaikinP3.default().raw.hex()}
                ],
            }
            return ac.capture_result

        task = hass.async_create_background_task(work(), "test capture")
        tasks.append(task)
        return task

    ac.start_capture = start
    entry.runtime_data = SimpleNamespace(ac=ac)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "capture"}
    )
    assert result["step_id"] == "capture"
    result = await hass.config_entries.options.async_configure(fid, {"seconds": 60})
    assert (
        result["type"] == "progress"
        and result["progress_action"] == "preparing_capture"
    )
    ready.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(fid)
    assert result["type"] == "menu" and result["step_id"] == "capturing"
    finish.set()
    await tasks[0]
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "capture_stop"}
    )
    assert result["step_id"] == "capture_result"
    assert result["description_placeholders"]["count"] == "1"
    result = await hass.config_entries.options.async_configure(fid, {"adopt": True})
    assert result["type"] == "abort" and result["reason"] == "capture_done"
    ac.adopt_capture.assert_awaited_once()


async def test_capture_dialog_abort_stops_task(hass, entry):
    ready = asyncio.Event()
    finished = asyncio.Event()

    async def work():
        await finished.wait()

    task = hass.async_create_background_task(work(), "test cancelled capture")

    async def stop():
        finished.set()
        await task

    ac = SimpleNamespace(
        capture_started=ready,
        capture_result={},
        start_capture=lambda _: task,
        stop_capture=AsyncMock(side_effect=stop),
    )
    entry.runtime_data = SimpleNamespace(ac=ac)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "capture"})
    await hass.config_entries.options.async_configure(fid, {"seconds": 60})
    hass.config_entries.options.async_abort(fid)
    await hass.async_block_till_done()
    ac.stop_capture.assert_awaited_once()
    assert task.done()


@pytest.mark.parametrize(
    "recorded_on,power,running",
    [(False, 100, False), (True, 0, True), (False, 100.1, True), (True, 150, True)],
)
async def test_outlet_options_require_each_confirmation(
    hass, entry, recorded_on, power, running
):
    ac = ACController(SimpleNamespace(hass=hass, data={"power_w": power}), entry)
    ac.state = DaikinP3.default().changed(power=recorded_on)
    ac.relay = AsyncMock()
    entry.runtime_data = SimpleNamespace(ac=ac)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "outlet_off"}
    )
    assert result["step_id"] == "outlet_off"
    result = await hass.config_entries.options.async_configure(fid, {"confirm": False})
    assert result["errors"]["base"] == "confirmation_required"
    ac.relay.assert_not_awaited()
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    if running:
        assert result["step_id"] == "outlet_off_running"
        ac.relay.assert_not_awaited()
        result = await hass.config_entries.options.async_configure(
            fid, {"confirm": False}
        )
        assert result["errors"]["base"] == "confirmation_required"
        ac.relay.assert_not_awaited()
        result = await hass.config_entries.options.async_configure(
            fid, {"confirm": True}
        )
    assert result["type"] == "abort" and result["reason"] == "outlet_off_done"
    ac.relay.assert_awaited_once_with(False, confirmed=True, running_confirmed=running)


async def test_closing_outlet_warning_does_not_cut_power(hass, entry):
    ac = SimpleNamespace(
        state=DaikinP3.default().changed(power=True),
        outlet_running=True,
        relay=AsyncMock(),
    )
    entry.runtime_data = SimpleNamespace(ac=ac)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "outlet_off"}
    )
    await hass.config_entries.options.async_configure(fid, {"confirm": True})
    hass.config_entries.options.async_abort(fid)
    ac.relay.assert_not_awaited()


@pytest.mark.parametrize("action", ["stop_button", "close_dialog"])
async def test_receiving_menu_stop_or_close_preserves_capture(hass, entry, action):
    ready = asyncio.Event()
    stopped = asyncio.Event()
    result_data = {"total_frames": 3, "errors": 0, "frames": []}
    ac = SimpleNamespace(capture_started=ready, capture_result={})
    tasks = []

    def start(seconds):
        async def work():
            ready.set()
            await stopped.wait()
            ac.capture_result = result_data
            return result_data

        task = hass.async_create_background_task(work(), "capture under dialog")
        tasks.append(task)
        return task

    async def stop():
        stopped.set()
        await tasks[0]

    ac.start_capture = start
    ac.stop_capture = AsyncMock(side_effect=stop)
    entry.runtime_data = SimpleNamespace(ac=ac)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "capture"})
    await hass.config_entries.options.async_configure(fid, {"seconds": 60})
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(fid)
    assert result["type"] == "menu" and result["menu_options"] == ["capture_stop"]
    if action == "stop_button":
        result = await hass.config_entries.options.async_configure(
            fid, {"next_step_id": "capture_stop"}
        )
        assert result["step_id"] == "capture_result"
        assert result["description_placeholders"]["count"] == "3"
    else:
        hass.config_entries.options.async_abort(fid)
        await hass.async_block_till_done()
    assert tasks[0].done() and not tasks[0].cancelled()
    assert ac.capture_result == result_data
    ac.stop_capture.assert_awaited_once()
