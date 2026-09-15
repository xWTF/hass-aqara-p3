# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Bounded read-only Telnet transport, used for discovery and v0.1 diagnostics.

This interface cannot execute caller-provided shell commands. No password/token
is read from firmware, and no remote binary is installed or downloaded.
"""

import asyncio
import base64
import re
import uuid
from enum import StrEnum

import telnetlib3

from .config import DeviceConfig
from .errors import AuthenticationError, ConnectionLost, InvalidData


class Resource(StrEnum):
    MIIO_DID = "miio_did"
    MODEL = "model"
    MAC = "mac"
    FIRMWARE = "firmware"
    POWER = "power"
    LOAD_POWER = "load_power"
    AC = "ac"
    AC_FUNCTION = "ac_function"
    FAN = "fan"
    RELAY = "relay"
    CHIP_TEMPERATURE = "chip_temperature"
    CODEBOOK = "codebook"


COMMANDS = {
    Resource.MIIO_DID: "getprop persist.sys.miio_did",
    Resource.MODEL: "getprop persist.sys.model",
    Resource.MAC: "cat /sys/class/net/wlan0/address",
    Resource.FIRMWARE: "getprop ro.sys.mi_fw_ver",
    Resource.POWER: "cat /data/alarm/prop-data/power-consumption-data.json",
    Resource.LOAD_POWER: "getprop persist.app.ir.load_power",
    Resource.AC: "cat /data/alarm/prop-data/air-conditioner-data.json",
    Resource.AC_FUNCTION: "cat /data/alarm/prop-data/ac-function-data.json",
    Resource.FAN: "cat /data/alarm/prop-data/fan-control-data.json",
    Resource.RELAY: "cat /data/alarm/prop-data/switch-data.json",
    Resource.CHIP_TEMPERATURE: "getprop sys.chip_temperature",
    Resource.CODEBOOK: "busybox base64 /data/storage/irfile.tmp_success",
}
PROMPT = re.compile(r"(?:^|\n)(?:[^\n]{0,100} )?[#$] $")
LOGIN = re.compile(r"(?:login|username):\s*$", re.IGNORECASE)
PASSWORD = re.compile(r"password:\s*$", re.IGNORECASE)
DENIED = re.compile(
    r"(?:login incorrect|authentication failed|login failed)", re.IGNORECASE
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class TelnetReader:
    def __init__(self, config: DeviceConfig, *, port: int = 23):
        self.config = config
        self.port = port
        self._reader = None
        self._writer = None
        self._pending = ""
        self._lock = asyncio.Lock()

    @property
    def connected(self):
        return self._writer is not None and not self._writer.is_closing()

    async def close(self):
        writer, self._writer = self._writer, None
        self._reader = None
        self._pending = ""
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (TimeoutError, OSError):
                pass

    async def _send_line(self, line: str):
        self._writer.write(line + "\r\n")
        await self._writer.drain()

    async def _until(self, patterns, *, limit=65536):
        # Search just the newly extended tail; never rescan a multi-MiB result
        # for each incoming byte (the historical blocking client did this).
        pieces = []
        length = 0
        tail = ""
        while True:
            chunk, self._pending = self._pending, ""
            if not chunk:
                chunk = await self._reader.read(4096)
            if not chunk:
                raise ConnectionLost("设备关闭了 Telnet 连接")
            chunk = chunk.replace("\r", "")
            length += len(chunk)
            if length > limit:
                raise InvalidData("设备响应超过长度限制")
            scan = tail + chunk
            for i, pattern in enumerate(patterns):
                match = pattern.search(scan)
                if match:
                    end = len(chunk) - (len(scan) - match.end())
                    if end < 0:
                        raise InvalidData("响应帧边界无效")
                    pieces.append(chunk[:end])
                    self._pending = chunk[end:]
                    return i, "".join(pieces)
            pieces.append(chunk)
            tail = scan[-512:]

    async def _connect(self):
        if self.connected:
            return
        async with asyncio.timeout(self.config.connect_timeout):
            self._reader, self._writer = await telnetlib3.open_connection(
                self.config.device_ip,
                self.port,
                encoding="utf8",
                encoding_errors="strict",
                connect_minwait=0,
                connect_maxwait=0.2,
                connect_timeout=self.config.connect_timeout,
                term="dumb",
                cols=240,
                send_environ=[],
                limit=32768,
            )
            idx, _ = await self._until([LOGIN, PASSWORD, PROMPT], limit=8192)
            if idx != 0:
                raise AuthenticationError("未收到预期的登录提示")
            await self._send_line("admin")
            idx, _ = await self._until([PROMPT, PASSWORD, DENIED, LOGIN], limit=8192)
            if idx == 1:
                # Only send the password while the server requests it.
                await self._send_line(self.config.telnet_password)
                idx, _ = await self._until(
                    [PROMPT, PASSWORD, DENIED, LOGIN], limit=8192
                )
            if idx != 0:
                raise AuthenticationError("Telnet 登录失败，请检查密码")

    async def read(self, resource: Resource) -> str:
        if not isinstance(resource, Resource):
            raise InvalidData("不允许执行自定义命令")
        async with self._lock:
            try:
                await self._connect()
                cmd = COMMANDS[resource]
                marker = "__P3_" + uuid.uuid4().hex + "__"
                # Separate stdout markers avoid mistaking '# ' in shell scripts
                # or file contents for the real prompt. These only print text.
                trailer = "printf '\\n" + marker + ':%s\\n\' "$?"'
                done = re.compile(r"(?:^|\n)" + marker + r":([0-9]+)\n")
                cap = 2_000_000 if resource == Resource.CODEBOOK else 65536
                timeout = (
                    25 if resource == Resource.CODEBOOK else self.config.command_timeout
                )
                async with asyncio.timeout(timeout):
                    await self._send_line(cmd)
                    await self._send_line(trailer)
                    _, result = await self._until([done], limit=cap)
                    await self._until([PROMPT], limit=8192)
                match = done.search(result)
                code = int(match[1])
                result = result[: match.start()]
                lines = result.splitlines()
                # Ignore exact echoed commands, never arbitrary matching values.
                lines = [x for x in lines if x not in (cmd, trailer, "# " + trailer)]
                if code:
                    raise InvalidData(f"设备无法读取 {resource.value}，退出码 {code}")
                return ANSI.sub("", "\n".join(lines)).strip("\n")
            except BaseException:
                # Timeout/cancellation discards the stream. Late bytes must not
                # be consumed as the next request's answer.
                await self.close()
                raise

    async def read_codebook(self) -> bytes:
        value = await self.read(Resource.CODEBOOK)
        try:
            raw = base64.b64decode("".join(value.split()), validate=True)
        except (ValueError, UnicodeError):
            raise InvalidData("设备码库的 Base64 数据无效") from None
        if len(raw) > 1_400_000:
            raise InvalidData("码库超过长度限制")
        return raw
