# SPDX-License-Identifier: GPL-3.0-only
"""One temporary Telnet-owned, authenticated PCM connection."""

import asyncio
import ipaddress
import re
import socket
import struct
import uuid

from .control import REMOTE, CommandError, CommandSession
from .errors import InvalidData, P3Error

APLAY_SHA256 = "5eaed5696930be8be58be72c4a017d5cdeb42e7afec9468f0423badc53786216"


class MediaSession:
    def __init__(self, control):
        self.control = control
        self.session = CommandSession(control.config)
        self.reader = None
        self.writer = None
        self.started = False
        self.finished = False

    async def open(self):
        try:
            async with asyncio.timeout(45):
                if ipaddress.ip_address(self.control.config.device_ip).version != 4:
                    raise InvalidData("Media playback currently requires IPv4")
                await self.control._prepare(self.session)
                digest = await self.session.run("busybox sha256sum /bin/aplay")
                if not re.search(r"(?m)^" + APLAY_SHA256 + r"\s", digest):
                    raise InvalidData("Unsupported audio player firmware")
                token = uuid.uuid4().hex
                # Replace the login shell. Closing this session sends HUP to the
                # helper; its child also has a parent-death kill signal.
                self.started = True
                await self.session._send_line(f"exec {REMOTE} audio {token}")
                port_re = re.compile(r"(?:^|\n)P3_AUDIO_PORT ([0-9]+)\n")
                error_re = re.compile(r"(?:^|\n)P3_AUDIO_DONE ([0-9]+)\n")
                async with asyncio.timeout(5):
                    index, text = await self.session._until(
                        [port_re, error_re], limit=8192
                    )
                if index:
                    raise CommandError("Audio player is busy or unavailable")
                port = int(port_re.search(text)[1])
                if not 1024 <= port <= 65535:
                    raise InvalidData("Invalid audio endpoint")
                async with asyncio.timeout(5):
                    self.reader, self.writer = await asyncio.open_connection(
                        self.control.config.device_ip, port, limit=1024
                    )
                    sock = self.writer.get_extra_info("socket")
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
                    self.writer.transport.set_write_buffer_limits(high=16384, low=4096)
                    self.writer.write(b"P3AUDIO1" + token.encode("ascii"))
                    await self.writer.drain()
                    if await self.reader.readline() != b"READY\n":
                        raise CommandError("Audio session rejected")
        except BaseException:
            await self.close()
            raise

    async def send(self, pcm):
        if not pcm or len(pcm) > 16384 or len(pcm) % 4:
            raise InvalidData("Invalid PCM frame")
        async with asyncio.timeout(10):
            self.writer.write(struct.pack("!I", len(pcm)) + pcm)
            await self.writer.drain()

    async def finish(self):
        async with asyncio.timeout(5):
            self.writer.write(b"\0\0\0\0")
            await self.writer.drain()
            if await self.reader.readline() != b"DONE 0\n":
                raise CommandError("Audio playback did not complete")
            self.finished = True

    async def close(self):
        writer, self.writer = self.writer, None
        if self.started and not self.finished:
            # Stop through the independent Telnet lifecycle: closing a PCM
            # socket alone would play its already queued bytes before seeing EOF.
            await self.session.close()
            if writer:
                try:
                    async with asyncio.timeout(4):
                        await self.reader.readline()
                except (TimeoutError, OSError):
                    pass
        if writer:
            writer.transport.abort()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (TimeoutError, OSError):
                pass
        # Normal completion also checks the Telnet-side termination marker.
        if self.started and self.session.connected:
            try:
                async with asyncio.timeout(4):
                    await self.session._until(
                        [re.compile(r"(?:^|\n)P3_AUDIO_DONE [0-9]+\n")], limit=8192
                    )
            except (TimeoutError, OSError, EOFError, P3Error):
                pass
        await self.session.close()
        self.started = False
