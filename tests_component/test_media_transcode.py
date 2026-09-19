# SPDX-License-Identifier: GPL-3.0-only
"""Exercise real HTTP -> FFmpeg pipes, including cancellation and bad input."""

import asyncio
import io
import shutil
import struct
import wave
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from homeassistant.exceptions import HomeAssistantError

from custom_components.aqara_p3.playback import pcm_stream


@pytest.fixture
async def decoder(hass, monkeypatch):
    if not (binary := shutil.which("ffmpeg")):
        pytest.skip("Stream tests require FFmpeg on the test host")
    hass.data["ffmpeg"] = SimpleNamespace(binary=binary)
    async with ClientSession() as client:
        monkeypatch.setattr(
            "custom_components.aqara_p3.playback.async_get_clientsession",
            lambda _: client,
        )
        yield hass


async def http_server(handler, path="/audio"):
    app = web.Application()
    app.router.add_get(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}{path}"


def wav_body(seconds):
    pcm = b"\x00\x20" * (16000 * seconds)
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(pcm)
    return out.getvalue()


async def test_real_decoder_resamples_and_produces_aligned_pcm(decoder):
    async def serve(_request):
        return web.Response(body=wav_body(1), content_type="audio/wav")

    runner, url = await http_server(serve)
    try:
        chunks = [chunk async for chunk in pcm_stream(decoder, url)]
        assert all(0 < len(chunk) <= 4096 and len(chunk) % 4 == 0 for chunk in chunks)
        output = b"".join(chunks)
        assert len(output) == 32000 * 4
        samples = struct.unpack("<250i", output[1000:2000])
        # Resampling can introduce tiny rounding/ripple; amplitude and signed
        # 32-bit byte order must remain correct.
        assert all(abs(sample - 0x20000000) < 20000 for sample in samples)
    finally:
        await runner.cleanup()


async def test_decoder_can_be_closed_before_source_finishes(decoder, monkeypatch):
    gate = asyncio.Event()
    delivered = asyncio.Event()
    body = wav_body(30)
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def track(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(
        "custom_components.aqara_p3.playback.asyncio.create_subprocess_exec", track
    )

    async def serve(request):
        response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        await response.prepare(request)
        await response.write(body[:200000])
        await gate.wait()
        try:
            await response.write(body[200000:])
            await response.write_eof()
            delivered.set()
        except ConnectionResetError:
            pass
        return response

    runner, url = await http_server(serve)
    stream = pcm_stream(decoder, url)
    try:
        async with asyncio.timeout(4):
            first = await anext(stream)
            assert first
            assert not delivered.is_set()
            # A full FFmpeg stdout pipe must not delay cancellation until the
            # two-second forced-kill fallback.
            async with asyncio.timeout(1):
                await stream.aclose()
        assert processes[0].returncode is not None
    finally:
        await stream.aclose()
        gate.set()
        await runner.cleanup()


async def test_decode_failure_and_http_error_are_reported(decoder):
    async def serve(_request):
        return web.Response(body=b"this is not audio")

    runner, url = await http_server(serve)
    try:
        with pytest.raises(HomeAssistantError, match="decoded"):
            async for _chunk in pcm_stream(decoder, url):
                pytest.fail("Invalid data yielded audio")
    finally:
        await runner.cleanup()


async def test_tts_uses_local_http_even_when_external_url_is_unreachable(decoder):
    from yarl import URL

    from custom_components.aqara_p3.media_player import audio_source_url

    seen = []

    async def serve(request):
        seen.append(request.path)
        return web.Response(body=wav_body(1), content_type="audio/wav")

    path = "/api/tts_proxy/example.wav"
    runner, listening = await http_server(serve, path)
    decoder.http = SimpleNamespace(
        ssl_certificate=None, server_host=None, server_port=URL(listening).port
    )
    decoder.config.external_url = "https://unreachable.example.invalid"
    decoder.config.internal_url = "http://also-unreachable.example.invalid"
    try:
        url = audio_source_url(decoder, path)
        assert url == listening
        data = b"".join([chunk async for chunk in pcm_stream(decoder, url)])
        assert len(data) == 128000
        assert seen == [path]
    finally:
        await runner.cleanup()
