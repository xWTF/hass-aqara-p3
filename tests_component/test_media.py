# SPDX-License-Identifier: GPL-3.0-only

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.exceptions import HomeAssistantError

from custom_components.aqara_p3.audio import AudioCoordinator
from custom_components.aqara_p3.playback import pcm_gain
from custom_components.aqara_p3.protocol.errors import InvalidData
from custom_components.aqara_p3.protocol.media import MediaSession


def test_pcm_software_gain_and_byte_order():
    samples = (-2147483648, -100, 0, 100, 2147483647)
    pcm = struct.pack("<5i", *samples)
    assert pcm_gain(pcm, 1) == pcm
    assert pcm_gain(pcm, 0) == bytes(len(pcm))
    assert struct.unpack("<5i", pcm_gain(pcm, 0.5)) == tuple(
        int(x * 0.5) for x in samples
    )
    for gain in (-1, 2, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            pcm_gain(pcm, gain)
    with pytest.raises(ValueError):
        pcm_gain(b"123", 0.5)


@pytest.fixture
async def media(hass, config_entry, monkeypatch):
    audio = AudioCoordinator(hass, config_entry, SimpleNamespace(control=AsyncMock()))
    audio.async_set_updated_data({"tones": ["DingDong"], "volume": 10})
    sessions = []
    gate = asyncio.Event()
    ended = []

    def session(_control):
        value = SimpleNamespace(
            open=AsyncMock(), send=AsyncMock(), finish=AsyncMock(), close=AsyncMock()
        )
        sessions.append(value)
        return value

    async def stream(_hass, _url):
        try:
            yield struct.pack("<i", 100000)
            await gate.wait()
            yield struct.pack("<i", 100000)
        finally:
            ended.append(True)

    monkeypatch.setattr("custom_components.aqara_p3.playback.MediaSession", session)
    monkeypatch.setattr("custom_components.aqara_p3.playback.pcm_stream", stream)
    try:
        yield SimpleNamespace(
            audio=audio, player=audio.media, sessions=sessions, gate=gate, ended=ended
        )
    finally:
        await audio.async_shutdown()


async def test_media_returns_after_start_and_live_volume_mute(media):
    await media.player.play("https://example.invalid/test.wav")
    assert media.player.state == MediaPlayerState.PLAYING
    assert media.sessions[0].send.call_args.args == (struct.pack("<i", 20000),)
    assert not media.ended
    task = media.player.task
    media.player.muted = True
    media.gate.set()
    await task
    assert media.sessions[0].send.call_args.args == (bytes(4),)
    media.sessions[0].finish.assert_awaited_once()
    media.sessions[0].close.assert_awaited_once()
    assert media.player.state == MediaPlayerState.IDLE


async def test_replacement_closes_old_pipeline_before_new(media):
    await media.player.play("http://example.invalid/first.wav")
    old = media.player.task
    await media.player.play("http://example.invalid/second.wav")
    assert old.cancelled()
    assert media.ended == [True]
    media.sessions[0].close.assert_awaited_once()
    media.sessions[0].finish.assert_not_awaited()
    assert media.player.state == MediaPlayerState.PLAYING


async def test_sound_and_media_replace_each_other(media):
    await media.audio.play("DingDong")
    original = media.audio._playback_task
    await media.player.play("http://example.invalid/voice.wav")
    await asyncio.gather(original, return_exceptions=True)
    assert original.cancelled()
    media.audio.parent.control.audio_write.assert_awaited_with(9, 4, 1)
    current = media.player.task
    await media.audio.play("DingDong")
    assert current.cancelled()
    media.sessions[0].close.assert_awaited_once()


async def test_shutdown_closes_decoder_and_device(media):
    await media.player.play("http://example.invalid/voice.wav")
    task = media.player.task
    await media.audio.async_shutdown()
    assert task.cancelled()
    assert media.ended == [True]
    media.sessions[0].close.assert_awaited_once()
    with pytest.raises(HomeAssistantError):
        await media.player.play("http://example.invalid/voice.wav")


async def test_failure_never_logs_url_or_signatures(media, monkeypatch, caplog):
    async def fail(_hass, url):
        raise OSError(url)
        yield b""  # make this an async generator

    monkeypatch.setattr("custom_components.aqara_p3.playback.pcm_stream", fail)
    with pytest.raises(HomeAssistantError):
        await media.player.play("https://example.invalid/private?authSig=secret")
    assert "secret" not in caplog.text
    assert "example.invalid" not in caplog.text
    assert media.player.error == "OSError"
    assert media.player.task is None


async def test_invalid_url_preserves_current_playback(media):
    await media.player.play("http://example.invalid/voice.wav")
    task = media.player.task
    for url in (
        "file:///etc/passwd",
        "ftp://example.invalid/a.wav",
        "http://user:secret@example.invalid/a.wav",
    ):
        with pytest.raises(HomeAssistantError):
            await media.player.play(url)
        assert media.player.task is task


async def test_stop_while_buffering_is_prompt(media, monkeypatch):
    begun = asyncio.Event()

    async def stream(_hass, _url):
        begun.set()
        await asyncio.Event().wait()
        yield bytes(4)

    monkeypatch.setattr("custom_components.aqara_p3.playback.pcm_stream", stream)
    caller = asyncio.create_task(media.player.play("http://example.invalid/a.wav"))
    await begun.wait()
    async with asyncio.timeout(1):
        async with media.audio._command_lock:
            await media.player.stop_locked()
        # An explicit stop/replacement is a normal lifecycle event, not a
        # misleading "could not start" service error.
        await caller


async def test_tcp_protocol_finishes_explicitly_and_validates_frames():
    seen = []

    async def server(reader, writer):
        size = struct.unpack("!I", await reader.readexactly(4))[0]
        seen.append(await reader.readexactly(size))
        assert await reader.readexactly(4) == bytes(4)
        writer.write(b"DONE 0\n")
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    control = SimpleNamespace(config=None)
    session = MediaSession(control)
    try:
        session.reader, session.writer = await asyncio.open_connection(
            "127.0.0.1", listener.sockets[0].getsockname()[1]
        )
        for bad in (b"", b"abc", bytes(16388)):
            with pytest.raises(InvalidData):
                await session.send(bad)
        await session.send(b"abcd")
        await session.finish()
        assert seen == [b"abcd"]
    finally:
        await session.close()
        listener.close()
        await listener.wait_closed()


async def test_tts_announce_resolves_media_source(media, monkeypatch):
    from custom_components.aqara_p3.media_player import P3MediaPlayer

    # Exercise the service method independently of device registry setup.
    entity = object.__new__(P3MediaPlayer)
    entity.hass = media.audio.hass
    entity.entity_id = "media_player.test_speaker"
    entity.playback = media.player
    resolver = AsyncMock(
        return_value=SimpleNamespace(url="http://example.invalid/speech.wav")
    )
    monkeypatch.setattr(
        "custom_components.aqara_p3.media_player.media_source.async_resolve_media",
        resolver,
    )
    monkeypatch.setattr(
        "custom_components.aqara_p3.media_player.async_process_play_media_url",
        lambda _hass, url: url,
    )
    await entity.async_play_media("music", "media-source://tts/test", announce=True)
    resolver.assert_awaited_once_with(
        entity.hass, "media-source://tts/test", entity.entity_id
    )
    assert media.player.state == MediaPlayerState.PLAYING
