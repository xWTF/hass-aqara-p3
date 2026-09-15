# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.aqara_p3.ac_controller import ACController
from custom_components.aqara_p3.coordinator import P3Coordinator
from custom_components.aqara_p3.protocol.control import CommandError


@pytest.fixture
async def controller(hass, config_entry, snapshot):
    co = P3Coordinator(hass, config_entry)
    co.device = AsyncMock()
    co.device.snapshot.return_value = snapshot
    co.control = AsyncMock()
    co.ac.store = AsyncMock()
    co.ac.store.async_load.return_value = None
    await co.async_config_entry_first_refresh()
    yield co.ac
    await co.async_shutdown()


async def test_commands_serialize_without_losing_fields(controller):
    seen = []

    async def send(state):
        await asyncio.sleep(0.005)
        seen.append(state)

    controller.coordinator.control.send.side_effect = send
    await asyncio.gather(
        controller.apply(mode="cool", power=True, temperature=28.5),
        controller.apply(swing="both", fan="3"),
    )
    assert len(seen) == 2
    assert seen[-1].temperature == 28.5 and seen[-1].swing == "both" and seen[-1].power


async def test_firmware_cache_never_overwrites_command(controller):
    await controller.apply(power=True, mode="heat", temperature=10, fan="quiet")
    await controller.coordinator.async_refresh()
    assert controller.state.temperature == 10 and controller.state.mode == "heat"
    assert controller.coordinator.data["ac_mode"] == "cool"


async def test_error_is_not_retried_and_uncertainty_persisted(controller):
    controller.coordinator.control.send.side_effect = CommandError("timeout")
    with pytest.raises(HomeAssistantError):
        await controller.apply(power=True)
    controller.coordinator.control.send.assert_awaited_once()
    assert not controller.valid
    assert controller.store.async_save.call_args.args[0]["valid"] is False


async def test_cancelled_command_cannot_restore_stale_certainty(controller):
    controller.valid = True
    started = asyncio.Event()

    async def hang(state):
        started.set()
        await asyncio.Event().wait()

    controller.coordinator.control.send.side_effect = hang
    task = asyncio.create_task(controller.apply(power=True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.store.async_save.call_args.args[0]["valid"] is False


async def test_restore_does_not_transmit(controller):
    await controller.apply(mode="cool", temperature=27.5, power=True)
    saved = controller.store.async_save.call_args.args[0]
    other = ACController(controller.coordinator, controller.coordinator.config_entry)
    other.store = AsyncMock()
    other.store.async_load.return_value = saved
    before = controller.coordinator.control.send.await_count
    await other.load()
    assert other.state.temperature == 27.5 and other.valid
    assert controller.coordinator.control.send.await_count == before


async def test_unrelated_commands_preserve_remaining_timer(controller, monkeypatch):
    from custom_components.aqara_p3 import ac_controller as module

    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    await controller.apply(power=True, timers={"on": 120, "off": 60})
    monkeypatch.setattr(module.time, "time", lambda: 1600.0)
    await controller.apply(fan="2")
    sent = controller.coordinator.control.send.call_args.args[0]
    assert sent.on_minutes == 110 and sent.off_minutes == 50
    monkeypatch.setattr(module.time, "time", lambda: 10000.0)
    await controller.expire_timers()
    assert controller.source == "timer_elapsed_unconfirmed" and not controller.valid


async def test_capture_stops_and_preserves_data(controller):
    stopped = asyncio.Event()

    async def capture(seconds, on_ready):
        on_ready()
        await stopped.wait()
        return {"total_frames": 1, "errors": 0, "frames": []}

    controller.coordinator.control.capture.side_effect = capture
    controller.coordinator.control._stop_capture_process.side_effect = lambda: (
        stopped.set()
    )
    task = controller.start_capture(60)
    await controller.capture_started.wait()
    with pytest.raises(HomeAssistantError):
        await controller.apply(power=True)
    await controller.stop_capture()
    assert task.done() and controller.capture_result["total_frames"] == 1


@pytest.mark.parametrize("timer", ["off", "sleep"])
async def test_expiry_clears_timer_blocks_accidental_restart(
    controller, monkeypatch, timer
):
    from custom_components.aqara_p3 import ac_controller as module

    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    await controller.apply(power=True, timers={timer: 60})
    controller.coordinator.control.send.reset_mock()
    monkeypatch.setattr(module.time, "time", lambda: 4601.0)
    # Even with no preceding poll, an unrelated command must not replay power-on.
    with pytest.raises(HomeAssistantError) as error:
        await controller.apply(fan="3")
    assert error.value.translation_key == "timer_power_required"
    assert controller.state.off_minutes == controller.state.sleep_minutes == 0
    assert controller.deadlines == {}
    assert controller.power_confirmation_required and not controller.valid
    controller.coordinator.control.send.assert_not_awaited()
    saved = controller.store.async_save.call_args.args[0]
    other = ACController(controller.coordinator, controller.coordinator.config_entry)
    other.store = AsyncMock()
    other.store.async_load.return_value = saved
    await other.load()
    assert other.power_confirmation_required
    await other.shutdown()
    await controller.apply(power=False)
    sent = controller.coordinator.control.send.call_args.args[0]
    assert not sent.power and not sent.off_minutes and not sent.sleep_minutes
    assert not controller.power_confirmation_required


async def test_timer_callback_clears_even_while_device_unavailable(controller):
    import time

    await controller.apply(power=True, timers={"sleep": 60})
    controller.coordinator.last_update_success = False
    controller.deadlines["sleep"] = time.time() - 1
    controller._schedule_timer()
    controller.coordinator.control.send.reset_mock()
    await asyncio.sleep(0.15)
    assert controller.state.sleep_minutes == 0
    assert not controller.deadlines and controller.power_confirmation_required
    controller.coordinator.control.send.assert_not_awaited()


async def test_adopt_old_capture_immediately_expires_without_transmission(controller):
    from custom_components.aqara_p3.protocol.profiles.daikin_p3 import DaikinP3

    state = DaikinP3.default().changed(power=True).timers(sleep=120)
    controller.capture_result = {
        "frames": [
            {
                "profile": "daikin_p3",
                "state_hex": state.raw.hex(),
                "utc": "2020-01-01T00:00:00+00:00",
            }
        ]
    }
    await controller.adopt_capture()
    assert controller.state.sleep_minutes == 0
    assert not controller.deadlines and not controller.valid
    assert controller.power_confirmation_required
    controller.coordinator.control.send.assert_not_awaited()
    assert controller.store.async_save.call_args.args[0]["deadlines"] == {}


async def test_restore_elapsed_off_timer_clears_before_first_control(controller):
    from custom_components.aqara_p3.protocol.profiles.daikin_p3 import DaikinP3

    controller.store.async_load.return_value = {
        "state": DaikinP3.default().changed(power=True).timers(off=60).raw.hex(),
        "valid": True,
        "deadlines": {"off": 1000.0},
    }
    await controller.load()
    assert controller.state.off_minutes == 0
    assert controller.power_confirmation_required
    controller.coordinator.control.send.assert_not_awaited()


async def test_expiring_one_timer_preserves_other_deadline(controller, monkeypatch):
    from custom_components.aqara_p3 import ac_controller as module

    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    await controller.apply(power=True, timers={"off": 60, "sleep": 120})
    monkeypatch.setattr(module.time, "time", lambda: 4600.0)
    await controller.expire_timers()
    assert controller.state.off_minutes == 0
    assert controller.state.sleep_minutes == 60
    assert controller.deadlines == {"sleep": 8200.0}


async def test_relay_confirmation_checks_serialized_current_state(controller):
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(_state):
        started.set()
        await release.wait()

    controller.coordinator.control.send.side_effect = send
    task = asyncio.create_task(controller.apply(power=True))
    await started.wait()
    # Confirmation was made while the old recorded state was still off.
    cut = asyncio.create_task(controller.relay(False, confirmed=True))
    await asyncio.sleep(0)
    release.set()
    await task
    with pytest.raises(HomeAssistantError) as error:
        await cut
    assert error.value.translation_key == "outlet_running_confirmation_required"
    controller.coordinator.control.relay.assert_not_awaited()
    await controller.relay(False, confirmed=True, running_confirmed=True)
    controller.coordinator.control.relay.assert_awaited_once_with(False)
    assert (
        not controller.state.power and not controller.deadlines and not controller.valid
    )


async def test_relay_unconfirmed_on_record_still_needs_second_confirmation(controller):
    controller.state = controller.state.changed(power=True)
    controller.valid = False
    with pytest.raises(HomeAssistantError):
        await controller.relay(False, confirmed=True)
    controller.coordinator.control.relay.assert_not_awaited()


@pytest.mark.parametrize(
    "recorded_on,power,blocked",
    [
        (False, 0, False),
        (False, 100, False),
        (False, 100.01, True),
        (False, 150, True),
        (True, 0, True),
        (True, None, True),
        (False, None, False),
    ],
)
async def test_relay_running_warning_uses_record_or_power(
    controller, recorded_on, power, blocked
):
    controller.state = controller.state.changed(power=recorded_on)
    controller.coordinator.data = {**controller.coordinator.data, "power_w": power}
    if blocked:
        with pytest.raises(HomeAssistantError) as error:
            await controller.relay(False, confirmed=True)
        assert error.value.translation_key == "outlet_running_confirmation_required"
        controller.coordinator.control.relay.assert_not_awaited()
    else:
        await controller.relay(False, confirmed=True)
        controller.coordinator.control.relay.assert_awaited_once_with(False)


async def test_relay_rechecks_power_after_waiting_for_lock(controller):
    controller.state = controller.state.changed(power=False)
    controller.coordinator.data = {**controller.coordinator.data, "power_w": 50}
    async with controller._lock:
        cut = asyncio.create_task(controller.relay(False, confirmed=True))
        await asyncio.sleep(0)
        controller.coordinator.data = {**controller.coordinator.data, "power_w": 150}
    with pytest.raises(HomeAssistantError) as error:
        await cut
    assert error.value.translation_key == "outlet_running_confirmation_required"
    controller.coordinator.control.relay.assert_not_awaited()
    await controller.relay(False, confirmed=True, running_confirmed=True)
    controller.coordinator.control.relay.assert_awaited_once_with(False)


async def test_powerful_expires_after_20_minutes_without_restarting_on_other_commands(
    controller, monkeypatch
):
    from custom_components.aqara_p3 import ac_controller as module

    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    await controller.apply(power=True, powerful=True)
    assert controller.powerful_deadline == 2200
    monkeypatch.setattr(module.time, "time", lambda: 1600.0)
    await controller.apply(swing="both")
    assert controller.powerful_deadline == 2200
    before = controller.coordinator.control.send.await_count
    monkeypatch.setattr(module.time, "time", lambda: 2200.0)
    await controller.expire_timers()
    assert (
        not controller.state.feature("powerful")
        and controller.powerful_deadline is None
    )
    assert controller.valid and not controller.power_confirmation_required
    assert controller.coordinator.control.send.await_count == before
    await controller.apply(fan="2")
    assert not controller.coordinator.control.send.call_args.args[0].feature("powerful")


async def test_restore_and_adopt_expired_powerful(controller):
    state = controller.state.changed(power=True, powerful=True)
    controller.store.async_load.return_value = {
        "state": state.raw.hex(),
        "valid": True,
        "updated_at": 1000.0,
        "powerful_deadline": 2200.0,
    }
    await controller.load()
    assert not controller.state.feature("powerful")
    controller.capture_result = {
        "frames": [
            {
                "profile": "daikin_p3",
                "state_hex": state.raw.hex(),
                "utc": "2020-01-01T00:00:00+00:00",
            }
        ]
    }
    await controller.adopt_capture()
    assert (
        not controller.state.feature("powerful")
        and controller.powerful_deadline is None
    )
    controller.coordinator.control.send.assert_not_awaited()


async def test_stopping_during_preparation_waits_for_ready(controller):
    preparing = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()

    async def capture(seconds, ready):
        preparing.set()
        await release.wait()
        ready()
        await stopped.wait()
        return {"total_frames": 1, "errors": 0, "frames": [{"profile": None}]}

    controller.coordinator.control.capture.side_effect = capture
    controller.coordinator.control._stop_capture_process.side_effect = stopped.set
    task = controller.start_capture(60)
    await preparing.wait()
    stop = asyncio.create_task(controller.stop_capture())
    await asyncio.sleep(0)
    assert not task.cancelled()
    release.set()
    await stop
    assert task.done() and not task.cancelled()
    assert controller.capture_result["total_frames"] == 1
    assert controller.store.async_save.call_args.args[0]["capture"]["total_frames"] == 1


async def test_capture_error_keeps_partial_result(controller):
    controller.coordinator.control.last_capture_result = {
        "total_frames": 2,
        "errors": 1,
        "frames": [],
        "complete": False,
    }
    controller.coordinator.control.capture.side_effect = CommandError("connection lost")
    with pytest.raises(CommandError):
        await controller.start_capture(60)
    assert controller.capture_result["total_frames"] == 2
    assert controller.store.async_save.call_args.args[0]["capture"]["complete"] is False


async def test_switching_modes_restores_each_temperature_and_fan(controller):
    expected = {"cool": (27.5, "5"), "heat": (21, "quiet"), "auto": (25.5, "2")}
    for mode, (temperature, fan) in expected.items():
        await controller.apply(mode=mode, power=True, temperature=temperature, fan=fan)
    await controller.apply(mode="fan_only", fan="3")
    await controller.apply(mode="dry", dry_offset=-1.5, fan="auto")
    for mode, (temperature, fan) in expected.items():
        before = controller.coordinator.control.send.await_count
        await controller.apply(mode=mode, power=True)
        sent = controller.coordinator.control.send.call_args.args[0]
        assert sent.temperature == temperature and sent.fan == fan
        assert controller.coordinator.control.send.await_count == before + 1
    await controller.apply(mode="dry")
    assert controller.state.dry_offset == -1.5 and controller.state.fan == "auto"
    await controller.apply(mode="fan_only")
    assert controller.state.fan == "3"


async def test_explicit_new_values_override_mode_memory(controller):
    await controller.apply(mode="cool", temperature=28, fan="5")
    await controller.apply(mode="heat", temperature=20, fan="quiet")
    await controller.apply(mode="cool", temperature=26.5, fan="2")
    assert controller.state.temperature == 26.5 and controller.state.fan == "2"
    await controller.apply(mode="heat")
    assert controller.state.temperature == 20 and controller.state.fan == "quiet"
    await controller.apply(mode="cool")
    assert controller.state.temperature == 26.5 and controller.state.fan == "2"


async def test_mode_memory_restores_after_restart_and_power_off(controller):
    await controller.apply(mode="dry", dry_offset=1.5, fan="quiet")
    await controller.apply(mode="cool", temperature=29.5, fan="4")
    await controller.apply(power=False)
    saved = controller.store.async_save.call_args.args[0]
    other = ACController(controller.coordinator, controller.coordinator.config_entry)
    other.store = AsyncMock()
    other.store.async_load.return_value = saved
    before = controller.coordinator.control.send.await_count
    await other.load()
    assert controller.coordinator.control.send.await_count == before
    await other.apply(mode="dry", power=True)
    assert other.state.dry_offset == 1.5 and other.state.fan == "quiet"
    await other.apply(mode="cool", power=True)
    assert other.state.temperature == 29.5 and other.state.fan == "4"
    await other.shutdown()


async def test_failed_send_does_not_overwrite_mode_preferences(controller):
    await controller.apply(mode="dry", dry_offset=-2, fan="auto")
    await controller.apply(mode="cool", temperature=27, fan="1")
    controller.coordinator.control.send.side_effect = CommandError("unconfirmed")
    with pytest.raises(HomeAssistantError):
        await controller.apply(mode="dry", dry_offset=2)
    assert controller.mode_preferences["dry"]["dry_offset"] == -2
    assert controller.mode_preferences["cool"] == {"temperature": 27, "fan": "1"}
    controller.coordinator.control.send.side_effect = None
    await controller.apply(mode="dry", power=True)
    assert controller.state.dry_offset == -2


async def test_capture_updates_only_matching_mode_memory(controller):
    await controller.apply(mode="cool", temperature=28.5, fan="4")
    state = controller.state.changed(mode="dry", dry_offset=-0.5, fan="quiet")
    controller.capture_result = {
        "frames": [
            {
                "profile": "daikin_p3",
                "state_hex": state.raw.hex(),
                "utc": "2026-09-16T00:00:00+00:00",
            }
        ]
    }
    await controller.adopt_capture()
    await controller.apply(mode="cool")
    assert controller.state.temperature == 28.5 and controller.state.fan == "4"
    await controller.apply(mode="dry")
    assert controller.state.dry_offset == -0.5 and controller.state.fan == "quiet"


async def test_old_store_migrates_current_mode_and_invalid_memory_is_ignored(
    controller,
):
    state = controller.state.changed(mode="dry", dry_offset=-1, fan="quiet")
    controller.store.async_load.return_value = {
        "state": state.raw.hex(),
        "valid": True,
        "mode_preferences": {
            "heat": {"temperature": 999, "fan": "auto"},
            "cool": {"temperature": 26, "fan": "2", "power": True},
            "auto": {"temperature": 25.5, "fan": "3"},
            "fan_only": {"fan": []},
        },
    }
    await controller.load()
    assert set(controller.mode_preferences) == {"dry", "auto"}
    await controller.apply(mode="cool")
    await controller.apply(mode="dry")
    assert controller.state.dry_offset == -1 and controller.state.fan == "quiet"


@pytest.mark.parametrize("mode", ["cool", "heat"])
@pytest.mark.parametrize("change", [{"temperature": 26.5}, {"fan": "quiet"}])
async def test_adjustments_exit_powerful_in_one_command(controller, mode, change):
    await controller.apply(
        mode=mode, power=True, temperature=27, fan="4", powerful=True
    )
    assert controller.state.feature("powerful")
    before = controller.coordinator.control.send.await_count
    await controller.apply(**change)
    sent = controller.coordinator.control.send.call_args.args[0]
    assert controller.coordinator.control.send.await_count == before + 1
    assert not sent.feature("powerful") and sent.mode == mode and sent.power
    assert controller.powerful_deadline is None
    for key, value in change.items():
        assert getattr(sent, key) == value


@pytest.mark.parametrize("mode", ["dry", "auto", "fan_only"])
async def test_powerful_disabled_and_rejected_outside_cool_heat(controller, mode):
    from custom_components.aqara_p3.switch import P3Switch

    await controller.apply(mode=mode, power=True)
    entity = P3Switch(controller.coordinator, "powerful")
    assert not entity.available
    before = controller.coordinator.control.send.await_count
    with pytest.raises(HomeAssistantError) as error:
        await controller.apply(powerful=True)
    assert error.value.translation_key == "powerful_mode_required"
    assert controller.coordinator.control.send.await_count == before


@pytest.mark.parametrize(
    "change", [{"mode": "heat"}, {"mode": "dry"}, {"power": False}]
)
async def test_mode_switch_and_power_off_exit_powerful(controller, change):
    await controller.apply(mode="cool", power=True, powerful=True)
    await controller.apply(**change)
    assert not controller.state.feature("powerful")
    assert controller.powerful_deadline is None


async def test_rapid_stays_enabled_after_twenty_minutes(controller, monkeypatch):
    from custom_components.aqara_p3 import ac_controller as module

    monkeypatch.setattr(module.time, "time", lambda: 3000.0)
    await controller.apply(mode="cool", power=True, rapid=True)
    assert controller.state.feature("rapid") and controller.powerful_deadline is None
    await controller.apply(temperature=26)
    assert controller.state.feature("rapid")
    monkeypatch.setattr(module.time, "time", lambda: 6000.0)
    await controller.expire_timers()
    assert controller.state.feature("rapid")


async def test_powerful_cancel_failure_preserves_last_acknowledged_state(controller):
    await controller.apply(mode="cool", power=True, powerful=True)
    deadline = controller.powerful_deadline
    controller.coordinator.control.send.side_effect = CommandError("lost reply")
    with pytest.raises(HomeAssistantError):
        await controller.apply(fan="2")
    assert (
        controller.state.feature("powerful")
        and controller.powerful_deadline == deadline
    )
    assert not controller.valid
    assert not controller.coordinator.control.send.call_args.args[0].feature("powerful")
