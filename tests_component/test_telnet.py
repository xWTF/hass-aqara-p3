# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
import base64
import re
from contextlib import asynccontextmanager

import pytest
import telnetlib3

from custom_components.aqara_p3.protocol.config import DeviceConfig
from custom_components.aqara_p3.protocol.errors import (
    AuthenticationError,
    ConnectionLost,
    InvalidData,
    ResourceMissing,
)
from custom_components.aqara_p3.protocol.telnet import COMMANDS, Resource, TelnetReader


@asynccontextmanager
async def device_server(
    *,
    password="",
    response="lumi.aircondition.acn05",
    exit_code=0,
    stall=False,
    disconnect=False,
    fragment=False,
):
    seen = []
    tasks = set()
    writers = set()

    async def shell(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        try:
            writer.write("P3 login: ")
            assert (await reader.readline()).strip() == "admin"
            if password:
                writer.write("Password: ")
                if (await reader.readline()).rstrip("\r\n") != password:
                    writer.write("Login incorrect\r\n")
                    return
            writer.write("\r\nBusyBox\r\n# ")
            while line := await reader.readline():
                line = line.rstrip("\r\n")
                if not line:
                    continue
                # BusyBox echoes long command lines across terminal columns.
                writer.write(
                    "\r\n".join(line[i : i + 80] for i in range(0, len(line), 80))
                    + "\r\n"
                )
                start = re.match(r"printf '\\n(__P3_[0-9a-f]{32}__):BEGIN\\n'; ", line)
                if start:
                    writer.write(f"\r\n{start[1]}:BEGIN\r\n")
                    line = line[start.end() :]
                seen.append(line)
                match = re.search(r"__P3_[0-9a-f]{32}__", line)
                if match:
                    writer.write(f"\r\n{match[0]}:{exit_code}\r\n# ")
                    continue
                assert line in COMMANDS.values(), "Unexpected remote command"
                if stall:
                    await asyncio.sleep(5)
                if disconnect:
                    writer.write("incomplete response")
                    return
                if fragment:
                    for pos in range(0, len(response), 5000):
                        writer.write(response[pos : pos + 5000])
                        await writer.drain()
                        await asyncio.sleep(0)
                    writer.write("\r\n# ")
                else:
                    writer.write(response + "\r\n# ")
        finally:
            writers.discard(writer)
            tasks.discard(task)
            writer.close()

    server = await telnetlib3.create_server(
        host="127.0.0.1",
        port=0,
        shell=shell,
        encoding="utf8",
        connect_maxwait=0.1,
    )
    try:
        yield server.sockets[0].getsockname()[1], seen
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(writers):
            writer.close()
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


async def test_reads_with_and_without_password():
    for password in ("", "private'$()"):
        async with device_server(password=password) as (port, seen):
            transport = TelnetReader(DeviceConfig("127.0.0.1", password), port=port)
            try:
                assert await transport.read(Resource.MODEL) == "lumi.aircondition.acn05"
                assert await transport.read(Resource.MODEL) == "lumi.aircondition.acn05"
                assert transport.connected
            finally:
                await transport.close()
            assert len([x for x in seen if x == COMMANDS[Resource.MODEL]]) == 2
            assert password not in seen or not password


async def test_missing_cache_preserves_connection_but_other_errors_do_not():
    async with device_server(response="", exit_code=44) as (port, _):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        try:
            with pytest.raises(ResourceMissing):
                await transport.read(Resource.POWER)
            assert transport.connected
            with pytest.raises(InvalidData) as error:
                await transport.read(Resource.MODEL)
            assert not isinstance(error.value, ResourceMissing)
            assert not transport.connected
        finally:
            await transport.close()
    async with device_server(response="permission denied", exit_code=1) as (port, _):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        try:
            with pytest.raises(InvalidData) as error:
                await transport.read(Resource.POWER)
            assert not isinstance(error.value, ResourceMissing)
            assert not transport.connected
        finally:
            await transport.close()


async def test_wrong_password_is_bounded_and_not_logged(caplog):
    async with device_server(password="correct") as (port, seen):
        transport = TelnetReader(DeviceConfig("127.0.0.1", "wrong-secret"), port=port)
        with pytest.raises(AuthenticationError):
            await transport.read(Resource.MODEL)
        assert not transport.connected
        assert not seen
        assert "wrong-secret" not in caplog.text


async def test_prompt_like_file_contents_are_not_boundaries():
    content = "first\n# this is a comment\n# \nlast"
    async with device_server(response=content) as (port, _):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        try:
            assert await transport.read(Resource.MODEL) == content
        finally:
            await transport.close()


async def test_timeout_discards_connection():
    async with device_server(stall=True) as (port, _):
        transport = TelnetReader(
            DeviceConfig("127.0.0.1", command_timeout=0.1), port=port
        )
        with pytest.raises(TimeoutError):
            await transport.read(Resource.MODEL)
        assert not transport.connected


async def test_eof_and_nonzero_exit_are_not_success():
    async with device_server(disconnect=True) as (port, _):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        with pytest.raises(ConnectionLost):
            await transport.read(Resource.MODEL)
    async with device_server(response="file missing", exit_code=1) as (port, _):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        with pytest.raises(InvalidData):
            await transport.read(Resource.MODEL)


async def test_large_codebook_does_not_block_event_loop():
    raw = b"0123456789" * 100000
    encoded = base64.b64encode(raw).decode()
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    async with device_server(response=encoded, fragment=True) as (port, _):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            async with asyncio.timeout(5):
                assert await transport.read_codebook() == raw
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await transport.close()
    assert ticks >= 2


async def test_output_limit_and_command_allowlist():
    async with device_server(response="x" * 70000) as (port, seen):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        with pytest.raises(InvalidData):
            await transport.read(Resource.MODEL)
        assert not transport.connected
        before = len(seen)
        with pytest.raises(InvalidData):
            await transport.read("reboot")
        assert len(seen) == before


async def test_cancellation_closes_connection():
    async with device_server(stall=True) as (port, seen):
        transport = TelnetReader(DeviceConfig("127.0.0.1"), port=port)
        task = asyncio.create_task(transport.read(Resource.MODEL))
        async with asyncio.timeout(2):
            while not seen:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not transport.connected
