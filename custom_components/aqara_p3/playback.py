# SPDX-License-Identifier: GPL-3.0-only
"""Bounded HA-side decoding and software volume, with no P3 audio files."""

import asyncio
import logging
import math
import sys
from array import array

import aiohttp
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .const import DOMAIN
from .protocol.errors import P3Error
from .protocol.media import MediaSession

LOGGER = logging.getLogger(__name__)
MAX_PLAY_SECONDS = 3600


def playback_failure(error, stage):
    """Expose useful causes without URLs, signatures or arbitrary exception text."""
    values = None
    if isinstance(error, HomeAssistantError):
        return error
    if isinstance(error, aiohttp.ClientResponseError):
        key = "media_http_error"
        values = {"status": str(error.status)}
        message = f"Audio download returned HTTP {error.status}."
    elif isinstance(error, aiohttp.ClientSSLError):
        key = "media_tls_error"
        message = "Home Assistant could not establish a verified TLS connection to the audio source."
    elif isinstance(error, aiohttp.ClientConnectorError):
        key = "media_source_connection"
        reason = type(error.os_error).__name__
        values = {"reason": reason}
        message = f"Home Assistant could not connect to the audio source ({reason}). Check the Home Assistant internal URL, DNS and port."
    elif isinstance(error, FileNotFoundError) and stage == "source":
        key = "media_ffmpeg_missing"
        message = "FFmpeg was not found on the Home Assistant host."
    elif isinstance(error, P3Error):
        key = "media_device_error"
        # P3Error is explicitly restricted to messages safe for application logs.
        values = {"reason": str(error)}
        message = f"P3 audio connection failed: {error}"
    elif isinstance(error, TimeoutError):
        key = "media_source_timeout" if stage == "source" else "media_device_timeout"
        message = (
            "Timed out fetching or decoding audio."
            if stage == "source"
            else "Timed out connecting to or playing audio on P3."
        )
    else:
        key = "media_source_error" if stage == "source" else "media_stream_error"
        values = {"reason": type(error).__name__}
        message = (
            f"Audio source failed ({type(error).__name__})."
            if stage == "source"
            else f"P3 audio stream failed ({type(error).__name__})."
        )
    return HomeAssistantError(
        message,
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=values,
    )


def pcm_gain(data, gain):
    """Scale aligned signed little-endian 32-bit PCM, without amplification."""
    if len(data) % 4 or not math.isfinite(gain) or not 0 <= gain <= 1:
        raise ValueError("Invalid PCM gain")
    if gain == 1:
        return data
    if gain == 0:
        return bytes(len(data))
    samples = array("i", data)
    if sys.byteorder != "little":
        samples.byteswap()
    for index, sample in enumerate(samples):
        samples[index] = int(sample * gain)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


async def pcm_stream(hass, url):
    """Fetch via HA's TLS-aware client; give FFmpeg only a pipe, never a URL."""
    parsed = URL(url)
    if parsed.scheme not in ("http", "https") or not parsed.host or parsed.user:
        raise HomeAssistantError("Select an HTTP audio URL or Home Assistant media")
    process = None
    feeder = None
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=15)
        async with async_get_clientsession(hass).get(url, timeout=timeout) as response:
            response.raise_for_status()
            binary = get_ffmpeg_manager(hass).binary
            process = await asyncio.create_subprocess_exec(
                binary,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-probesize",
                "65536",
                "-analyzeduration",
                "1000000",
                "-i",
                "pipe:0",
                "-vn",
                "-sn",
                "-dn",
                "-ac",
                "1",
                "-ar",
                "32000",
                "-acodec",
                "pcm_s32le",
                "-f",
                "s32le",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=16384,
            )

            async def feed():
                try:
                    async for chunk in response.content.iter_chunked(16384):
                        process.stdin.write(chunk)
                        await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Decoder status below distinguishes rejection from EOF.
                finally:
                    process.stdin.close()

            feeder = asyncio.create_task(feed(), name="Aqara P3 media input")
            pending = b""
            while True:
                async with asyncio.timeout(20):
                    chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                pending += chunk
                count = len(pending) // 4 * 4
                if count:
                    yield pending[:count]
                    pending = pending[count:]
            await feeder
            async with asyncio.timeout(5):
                result = await process.wait()
            if result or pending:
                raise HomeAssistantError(
                    "Audio format could not be decoded",
                    translation_domain=DOMAIN,
                    translation_key="media_decode_error",
                )
    finally:
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        if process:
            if process.stdin:
                process.stdin.close()

            # asyncio's process.wait can wait for paused stdout pipes even after
            # the child exits. Drain/discard concurrently during termination.
            async def discard():
                while await process.stdout.read(16384):
                    pass

            draining = asyncio.create_task(discard())
            try:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    async with asyncio.timeout(2):
                        await process.wait()
                except TimeoutError:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
            finally:
                draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)


class MediaPlayback:
    def __init__(self, audio):
        self.audio = audio
        self.hass = audio.hass
        self.state = MediaPlayerState.IDLE
        self.volume = 0.2
        self.muted = False
        self.title = None
        self.error = None
        self.error_detail = None
        self.error_stage = None
        self.task = None
        self._ready = None
        self.listeners = set()

    def changed(self):
        for listener in tuple(self.listeners):
            listener()

    def set_volume(self, volume):
        if not math.isfinite(volume) or not 0 <= volume <= 1:
            raise HomeAssistantError("Volume must be between 0 and 1")
        self.volume = volume
        self.changed()

    async def stop_locked(self):
        task, self.task = self.task, None
        if self._ready is not None and not self._ready.done():
            self._ready.set_result(None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.state = MediaPlayerState.IDLE
        self.changed()

    async def play(self, url, title=None):
        # Validate before replacing an existing playback.
        parsed = URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.user:
            raise HomeAssistantError("Select an HTTP audio URL or Home Assistant media")
        ready = self.hass.loop.create_future()
        async with self.audio._command_lock:
            if self.audio._closed:
                raise HomeAssistantError("Audio service unavailable")
            await self.stop_locked()
            if self.audio._playback_task:
                self.audio._cancel_playback()
                await self.audio._send(4, 1)
            self.state = MediaPlayerState.BUFFERING
            self.error = None
            self.error_detail = None
            self.error_stage = None
            self.title = title[:200] if isinstance(title, str) else None
            self._ready = ready
            task = self.task = self.hass.async_create_background_task(
                self._run(url, ready), "Aqara P3 media playback"
            )
            self.changed()
        try:
            async with asyncio.timeout(60):
                outcome = await asyncio.shield(ready)
                if outcome is not None:
                    raise outcome from None
        except TimeoutError:
            async with self.audio._command_lock:
                if self.task is task:
                    await self.stop_locked()
            raise HomeAssistantError(
                "Audio playback did not start within 60 seconds.",
                translation_domain=DOMAIN,
                translation_key="media_start_timeout",
            ) from None
        except BaseException:
            async with self.audio._command_lock:
                if self.task is task:
                    await self.stop_locked()
            # A canceled caller must also consume the worker's startup result.
            if ready.done() and not ready.cancelled():
                ready.exception()
            raise

    async def _run(self, url, ready):
        session = MediaSession(self.audio.parent.control)
        stream = pcm_stream(self.hass, url)
        received = False
        failure = None
        stage = "source"
        try:
            async with asyncio.timeout(MAX_PLAY_SECONDS):
                async for pcm in stream:
                    if not received:
                        stage = "device"
                        await session.open()
                    stage = "stream"
                    await session.send(pcm_gain(pcm, 0 if self.muted else self.volume))
                    if not received:
                        received = True
                        self.state = MediaPlayerState.PLAYING
                        self.changed()
                        ready.set_result(None)
                    stage = "source"
                if not received:
                    raise HomeAssistantError(
                        "Audio source is empty",
                        translation_domain=DOMAIN,
                        translation_key="media_empty",
                    )
                stage = "stream"
                await session.finish()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- settle the caller's future with a sanitized failure
            # Never log URLs, query signatures, device credentials or decoder
            # stderr. This code is also displayed in entity diagnostics.
            self.error = type(error).__name__
            failure = playback_failure(error, stage)
            self.error_detail = str(failure)
            self.error_stage = stage
            LOGGER.warning("Media playback failed at %s: %s", stage, failure)
        finally:
            try:
                await session.close()
            finally:
                await stream.aclose()
                if self.task is asyncio.current_task():
                    self.task = None
                self.state = MediaPlayerState.IDLE
                self.changed()
                if not ready.done():
                    ready.set_result(failure)
