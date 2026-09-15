# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Offline index and command planning; never sends IR or changes the device."""

import base64
import hashlib
import re
from dataclasses import dataclass, field
from types import MappingProxyType

from .errors import InvalidData, UnsupportedState
from .models import NATIVE_MODES, strict_json

KEY = re.compile(
    r"P(?P<p>[01])_M(?P<m>[0-4])(?:_T(?P<t>[0-9]{1,2}))?_S(?P<s>[0-3])_D(?P<d>[01])"
)


@dataclass(frozen=True)
class IRRecord:
    key: str
    declared_length: int
    encoded: str = field(repr=False)


@dataclass(frozen=True)
class NativeState:
    on: bool
    mode: int
    temperature: int | None
    fan: int
    direction: int

    def __post_init__(self):
        if (
            type(self.on) is not bool
            or type(self.mode) is not int
            or self.mode not in NATIVE_MODES
        ):
            raise UnsupportedState("空调状态无效")
        if type(self.fan) is not int or self.fan not in range(4):
            raise UnsupportedState("风速无效")
        if type(self.direction) is not int or self.direction not in (0, 1):
            raise UnsupportedState("原厂摆风编码无效")
        if self.temperature is not None and (
            type(self.temperature) is not int or not 10 <= self.temperature <= 40
        ):
            raise UnsupportedState("目标温度无效")
        if self.mode in (3, 4) and self.temperature is not None:
            raise UnsupportedState("此模式不能在状态码中附加温度")
        if self.mode in (0, 1, 2) and self.temperature is None:
            raise UnsupportedState("此模式需要目标温度")

    @property
    def key(self):
        # Firmware's P0 means powered on. Do not use HA's boolean as the byte.
        temp = "" if self.temperature is None else f"_T{self.temperature}"
        return (
            f"P{0 if self.on else 1}_M{self.mode}{temp}_S{self.fan}_D{self.direction}"
        )


@dataclass(frozen=True)
class Codebook:
    sha256: str
    records: dict = field(repr=False)
    metadata: dict = field(repr=False)

    @classmethod
    def parse(cls, data: bytes):
        if not 0 < len(data) <= 1_400_000:
            raise InvalidData("码库大小无效")
        try:
            lines = data.decode("ascii").splitlines()
        except UnicodeError:
            raise InvalidData("码库编码无效") from None
        if len(lines) < 4 or lines[0] != "0|2|0|0":
            raise InvalidData("不支持的码库版本")
        header = re.fullmatch(r"1\|\|([0-9]+)", lines[1])
        if header is None or not 1 <= int(header[1]) <= 10000:
            raise InvalidData("码库计数头无效")
        records = {}
        for line in lines[2:-1]:
            parts = line.split("|", 4)
            if len(parts) != 5 or parts[:2] != ["2", ""]:
                raise InvalidData("码库记录格式无效")
            _, _, key, size, encoded = parts
            if not re.fullmatch(r"[PMTSDL0-9_]{1,64}", key) or key in records:
                raise InvalidData("码库状态键无效或重复")
            if not size.isdecimal() or not 1 <= int(size) <= 65535:
                raise InvalidData("红外声明长度无效")
            if not 1 <= len(encoded) <= 16384:
                raise InvalidData("红外编码长度无效")
            try:
                base64.b64decode(encoded, validate=True)
            except ValueError:
                raise InvalidData("红外数据不是有效 Base64") from None
            records[key] = IRRecord(key, int(size), encoded)
        if len(records) != int(header[1]):
            raise InvalidData("码库被截断或记录计数不匹配")
        if not lines[-1].startswith("3|"):
            raise InvalidData("码库缺少规则尾部")
        metadata = strict_json(lines[-1][2:])
        if not isinstance(metadata, dict):
            raise InvalidData("码库规则格式无效")
        return cls(
            hashlib.sha256(data).hexdigest(), MappingProxyType(records), metadata
        )

    def plan(self, state: NativeState) -> IRRecord:
        # Exact full-state lookup, never a sequence of power/mode mutations.
        try:
            return self.records[state.key]
        except KeyError:
            raise UnsupportedState("本地码库没有该完整状态，不发送替代命令") from None

    def summary(self):
        modes = {}
        complete = 0
        for key in self.records:
            match = KEY.fullmatch(key)
            if match is None:
                continue
            complete += 1
            if match["p"] != "0":
                continue
            mode = NATIVE_MODES[int(match["m"])]
            entry = modes.setdefault(
                mode, {"temperatures": set(), "fan": set(), "direction": set()}
            )
            if match["t"] is not None:
                entry["temperatures"].add(int(match["t"]))
            entry["fan"].add(int(match["s"]))
            entry["direction"].add(int(match["d"]))
        return {
            "sha256": self.sha256,
            "records": len(self.records),
            "complete_state_records": complete,
            "modes": {
                mode: {k: sorted(v) for k, v in entry.items()}
                for mode, entry in modes.items()
            },
            "execution_verified": False,
        }
