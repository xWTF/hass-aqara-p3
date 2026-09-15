# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.aqara_p3.protocol.config import DeviceConfig
from custom_components.aqara_p3.protocol.control import CommandError, P3Control
from custom_components.aqara_p3.protocol.errors import InvalidData
from custom_components.aqara_p3.protocol.profiles.daikin_p3 import DaikinP3


@pytest.fixture
def control():
    c = P3Control(DeviceConfig("127.0.0.1"), "p3_020000000001")
    c._prepare = AsyncMock()
    c.session = SimpleNamespace(run=AsyncMock(), close=AsyncMock())
    return c


async def test_one_bounded_request_and_exact_target(control):
    async def reply(cmd):
        request = json.loads(shlex.split(cmd)[-1])
        assert request["_to"] == 512 and request["method"] == "miIO.ir_play"
        assert len(json.dumps(request, separators=(",", ":"))) < 1000
        return json.dumps({"id": request["id"], "_from": 512, "result": ["ok"]})

    control.session.run.side_effect = reply
    await control.send(DaikinP3.default())
    control.session.run.assert_awaited_once()
    control.session.close.assert_awaited_once()


@pytest.mark.parametrize("reply", ["{bad json", '{"id":0,"result":["ok"]}', "nothing"])
async def test_bad_ack_never_retries(control, reply):
    control.session.run.return_value = reply
    with pytest.raises(CommandError):
        await control.send(DaikinP3.default())
    control.session.run.assert_awaited_once()
    control.session.close.assert_awaited_once()


async def test_timeout_never_retries(control):
    control.session.run.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await control.send(DaikinP3.default())
    control.session.run.assert_awaited_once()


async def test_identity_mismatch_does_not_upload():
    c = P3Control(DeviceConfig("127.0.0.1"), "p3_020000000001")
    session = SimpleNamespace(
        read=AsyncMock(return_value="02:00:00:00:00:02"), run=AsyncMock()
    )
    with pytest.raises(InvalidData):
        await c._prepare(session)
    session.run.assert_not_awaited()


async def test_capture_blocks_transmit(control):
    control.capture_session = object()
    with pytest.raises(CommandError):
        await control.send(DaikinP3.default())
    control.session.run.assert_not_awaited()


async def test_command_session_discards_its_own_printf_echo():
    import re

    from custom_components.aqara_p3.protocol.control import CommandSession

    session = CommandSession(DeviceConfig("127.0.0.1"))
    session._connect = AsyncMock()
    sent = []

    async def send(line):
        sent.append(line)

    session._send_line = send
    calls = 0

    async def until(patterns, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            marker = re.search(r"__P3_CMD_[a-f0-9]+__", sent[-1])[0]
            return 0, "\n".join(
                [sent[0], sent[1], "# 123456", "# " + sent[-1], marker + ":0", ""]
            )
        return 0, "\n# "

    session._until = until
    assert await session.run("getprop persist.sys.miio_did") == "123456"
