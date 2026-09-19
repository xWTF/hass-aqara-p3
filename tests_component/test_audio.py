# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.aqara_p3.audio import AudioCoordinator, decode_audio
from custom_components.aqara_p3.protocol.config import DeviceConfig
from custom_components.aqara_p3.protocol.control import CommandError, P3Control
from custom_components.aqara_p3.protocol.errors import InvalidData


def raw_state():
    return {
        (5, 2): 40,
        (9, 5): json.dumps({"alarm": ["PoliceCar_1"], "doorbell": ["DingDong"]}),
    }


def test_audio_decoder():
    data = decode_audio(raw_state())
    assert data["tones"] == ["DingDong", "PoliceCar_1"]
    for key, value in [
        ((5, 2), 101),
        ((9, 5), "[]"),
        ((9, 5), '{"alarm":["../../shell"]}'),
    ]:
        raw = raw_state()
        raw[key] = value
        with pytest.raises((ValueError, TypeError)):
            decode_audio(raw)


@pytest.mark.parametrize("bad", [None, "code", "id", "missing", "duplicate"])
async def test_audio_ipc_exact_replies_and_no_retry(bad):
    control = P3Control(DeviceConfig("127.0.0.1"), "test")
    control._prepare = AsyncMock()
    control.session.close = AsyncMock()

    def reply(cmd):
        if cmd == "getprop persist.sys.miio_did":
            return "123456"
        request = json.loads(shlex.split(cmd)[-1])
        assert request["_to"] == 32 and request["method"] == "get_properties"
        data = [
            {**p, "code": 0, "value": raw_state()[(p["siid"], p["piid"])]}
            for p in request["params"]
        ]
        if bad == "code":
            data[0]["code"] = -4003
        if bad == "id":
            data[0]["did"] = "999"
        if bad == "missing":
            data.pop()
        if bad == "duplicate":
            data[1] = data[0]
        return 'ignored echo\n{"did":"123",\n' + json.dumps(
            {"id": request["id"], "_from": 32, "result": data}
        )

    control.session.run = AsyncMock(side_effect=reply)
    if bad:
        with pytest.raises(CommandError):
            await control.audio_read()
    else:
        assert await control.audio_read() == raw_state()
    assert control.session.run.await_count == 2
    if bad:
        control.session.close.assert_awaited_once()
    else:
        control.session.close.assert_not_awaited()


@pytest.mark.parametrize(
    "siid,piid,value",
    [
        (1, 1, True),
        (3, 21, 1),
        (3, 22, 1),
        (5, 2, 101),
        (9, 1, '{"name":"../../x","volume":40}'),
        (9, 1, "[]"),
    ],
)
async def test_audio_write_whitelist(siid, piid, value):
    control = P3Control(DeviceConfig("127.0.0.1"), "test")
    control._ipc = AsyncMock()
    with pytest.raises(InvalidData):
        await control.audio_write(siid, piid, value)
    control._ipc.assert_not_awaited()


async def test_audio_play_is_one_validated_json_command(hass, config_entry):
    parent = SimpleNamespace(control=AsyncMock())
    co = AudioCoordinator(hass, config_entry, parent)
    co.async_request_refresh = AsyncMock()
    co.async_set_updated_data(decode_audio(raw_state()))
    await co.play("DingDong", 20)
    parent.control.audio_write.assert_awaited_once_with(
        9, 1, '{"name":"DingDong","volume":20}'
    )
    with pytest.raises(HomeAssistantError):
        await co.play("bad_name", 20)
    await co.async_shutdown()


@pytest.fixture
async def audio(hass, config_entry, monkeypatch):
    co = AudioCoordinator(hass, config_entry, SimpleNamespace(control=AsyncMock()))
    co.async_set_updated_data(decode_audio(raw_state()))
    monkeypatch.setattr("custom_components.aqara_p3.audio.REPEAT_GAP", 0)
    monkeypatch.setattr(
        "custom_components.aqara_p3.audio.SOUND_DURATIONS",
        {"DingDong": 0.02, "PoliceCar_1": 1},
    )
    yield co
    await co.async_shutdown()


async def finish_playback(audio):
    await asyncio.wait_for(audio._playback_task, timeout=2)
    assert audio._playback_task is None


async def test_play_defaults_and_natural_finish(audio):
    await audio.play()
    await finish_playback(audio)
    audio.parent.control.audio_write.assert_awaited_once_with(
        9, 1, '{"name":"DingDong","volume":40}'
    )


async def test_repeat_exact_count_and_fixed_volume(audio):
    await audio.play(volume=12, repeat=3)
    audio.data["volume"] = 90
    await finish_playback(audio)
    calls = audio.parent.control.audio_write.call_args_list
    assert len(calls) == 3
    assert all(c.args == (9, 1, '{"name":"DingDong","volume":12}') for c in calls)


async def test_time_limit_stops_current_play_and_future_repeats(audio):
    await audio.play("PoliceCar_1", stop_after=0.02, repeat=10)
    await finish_playback(audio)
    assert [c.args[:2] for c in audio.parent.control.audio_write.call_args_list] == [
        (9, 1),
        (9, 4),
    ]


async def test_repeat_count_finishes_before_long_deadline(audio):
    await audio.play(repeat=2, stop_after=100)
    await finish_playback(audio)
    assert [c.args[1] for c in audio.parent.control.audio_write.call_args_list] == [
        1,
        1,
    ]


async def test_new_play_cancels_old_deadline_and_repeats(audio):
    await audio.play("PoliceCar_1", stop_after=0.02, repeat=10)
    previous = audio._playback_task
    await asyncio.sleep(0)
    await audio.play("DingDong", repeat=3)
    await finish_playback(audio)
    assert previous.cancelled()
    assert [c.args[1] for c in audio.parent.control.audio_write.call_args_list] == [
        1,
        4,
        1,
        1,
        1,
    ]


async def test_explicit_stop_discards_repetitions_even_when_offline(audio):
    await audio.play(repeat=10)
    previous = audio._playback_task
    audio.parent.control.audio_write.side_effect = CommandError("offline")
    with pytest.raises(HomeAssistantError):
        await audio.stop()
    await asyncio.gather(previous, return_exceptions=True)
    assert audio._playback_task is None
    assert previous.cancelled()
    assert audio.parent.control.audio_write.await_count == 2


async def test_repeat_failure_does_not_retry(audio, caplog):
    await audio.play(repeat=10)
    audio.parent.control.audio_write.side_effect = CommandError("offline")
    await finish_playback(audio)
    assert audio.parent.control.audio_write.await_count == 2
    assert "Sound sequence ended" in caplog.text


async def test_shutdown_cancels_sequence_and_stops_sound(audio):
    await audio.play(repeat=10)
    task = audio._playback_task
    await audio.async_shutdown()
    assert task.cancelled()
    assert audio._playback_task is None
    assert [c.args[1] for c in audio.parent.control.audio_write.call_args_list] == [
        1,
        4,
    ]
    with pytest.raises(HomeAssistantError):
        await audio.play()


@pytest.mark.parametrize(
    "params",
    [
        {"repeat": 0},
        {"repeat": 101},
        {"repeat": 1.5},
        {"repeat": True},
        {"stop_after": 0},
        {"stop_after": -1},
        {"stop_after": float("nan")},
        {"stop_after": float("inf")},
        {"stop_after": True},
    ],
)
async def test_bad_parameters_preserve_existing_playback(audio, params):
    await audio.play(repeat=10)
    task = audio._playback_task
    with pytest.raises(HomeAssistantError):
        await audio.play(**params)
    assert audio._playback_task is task
    assert audio.parent.control.audio_write.await_count == 1


def test_sound_durations_match_reference_metadata_and_action_choices():
    from pathlib import Path

    import yaml

    from custom_components.aqara_p3.sounds import SOUND_DURATIONS

    root = Path(__file__).resolve().parents[1]
    frames = json.loads(
        (root / "tests_component/fixtures/sound_frames.json").read_text()
    )
    for tone, metadata in frames.items():
        assert SOUND_DURATIONS[tone] == metadata["frames"] / metadata["sample_rate"]
    assert SOUND_DURATIONS["MusixBox"] == SOUND_DURATIONS["MusicBox"]
    services = yaml.safe_load(
        (root / "custom_components/aqara_p3/services.yaml").read_text()
    )
    choices = services["play_sound"]["fields"]["tone"]["selector"]["select"]["options"]
    assert set(choices) == SOUND_DURATIONS.keys() - {"MusixBox"}
