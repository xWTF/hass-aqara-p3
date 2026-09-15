# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import InvalidData, UnsupportedDevice

MIOT_MODES = {0: "auto", 1: "heat", 2: "cool", 3: "dry", 4: "fan_only"}
NATIVE_MODES = {0: "cool", 1: "heat", 2: "auto", 3: "fan_only", 4: "dry"}
FAN_MODES = {0: "auto", 1: "low", 2: "medium", 3: "high"}


def strict_json(raw: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidData("JSON 存在重复字段")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                InvalidData("JSON 数值无效")
            ),
        )
    except (ValueError, TypeError, RecursionError):
        raise InvalidData("设备 JSON 数据无效") from None


def properties(raw: str, siid: int) -> dict:
    doc = strict_json(raw)
    if (
        not isinstance(doc, dict)
        or type(doc.get("siid")) is not int
        or doc["siid"] != siid
    ):
        raise InvalidData(f"服务编号不匹配，预期 {siid}")
    props = doc.get("property")
    if not isinstance(props, list) or len(props) > 128:
        raise InvalidData("属性列表无效")
    result = {}
    for item in props:
        if (
            not isinstance(item, dict)
            or type(item.get("piid")) is not int
            or "value" not in item
        ):
            raise InvalidData("属性记录无效")
        piid = item["piid"]
        if piid in result:
            raise InvalidData("属性编号重复")
        result[piid] = item["value"]
    return result


def finite_number(value, low: float, high: float):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise InvalidData("设备数值无效或超出范围")
    return value


def boolean(value):
    if type(value) is not bool:
        raise InvalidData("设备布尔值无效")
    return value


def enum_value(value, mapping):
    if type(value) is not int or value not in mapping:
        raise InvalidData("设备枚举值无效")
    return mapping[value]


def legacy_hex_cstring(raw: str) -> str:
    """Only for properties known to use the firmware's hex C-string storage."""
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,128}", raw):
        raise InvalidData("原厂属性编码无效")
    data = bytes.fromhex(raw)
    if not data.endswith(b"\0") or b"\0" in data[:-1]:
        raise InvalidData("原厂属性缺少结束符或包含嵌入 NUL")
    try:
        return data[:-1].decode("ascii")
    except UnicodeError:
        raise InvalidData("原厂属性不是 ASCII 字符串") from None


@dataclass(frozen=True)
class DeviceIdentity:
    mac: str
    model: str
    firmware: str

    def __post_init__(self):
        if self.model != "lumi.aircondition.acn05":
            raise UnsupportedDevice("当前只支持已核对固件的 P3 acn05")
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", self.mac):
            raise InvalidData("设备 MAC 格式无效")

    @property
    def uid(self):
        return "p3_" + self.mac.replace(":", "")


@dataclass(frozen=True)
class Snapshot:
    identity: DeviceIdentity
    values: dict
    acquired_at: str

    @classmethod
    def decode(
        cls,
        identity,
        *,
        power,
        ac,
        fan,
        relay,
        ac_function,
        chip_temperature,
        load_power=None,
    ):
        p = properties(power, 12)
        a = properties(ac, 10)
        f = properties(fan, 18)
        r = properties(relay, 14)
        af = properties(ac_function, 11)
        try:
            on = boolean(a[1])
            configured_mode = enum_value(a[2], MIOT_MODES)
            values = {
                "power_w": finite_number(p[2], 0, 5000),
                "power_source": "firmware_cache",
                "energy_kwh": finite_number(p[1], 0, 1e9),
                "relay_on": boolean(r[1]),
                "ac_on": on,
                "ac_mode": configured_mode if on else "off",
                "fan_mode": enum_value(f[2], FAN_MODES),
                "vertical_swing": boolean(f[4]),
                "native_ac_state": af[9],
                "source": "firmware_cache",
                "control_enabled": False,
            }
            # mha_ir writes this property before mha_master synchronizes its
            # slower power-consumption JSON. Prefer the property when present.
            if load_power is not None and load_power.strip():
                try:
                    watts = float(load_power)
                except ValueError:
                    raise InvalidData("负载功率属性无效") from None
                values["power_w"] = finite_number(watts, 0, 5000)
                values["power_source"] = "device_property"
                values["source"] = "firmware_cache_and_device_properties"
            # Fan-only and dry may have no meaningful setpoint. Do not expose
            # the vendor's sentinel 0 as a room or target temperature.
            target = a.get(4)
            values["target_temperature"] = (
                None if target == 0 else finite_number(target, 10, 40)
            )
            if not isinstance(af[9], str) or not re.fullmatch(
                r"[PMTSDL0-9_]{1,64}", af[9]
            ):
                raise InvalidData("原厂空调状态字符串无效")
            if chip_temperature.strip():
                try:
                    temperature = float(chip_temperature)
                except ValueError:
                    raise InvalidData("芯片温度数据无效") from None
                values["chip_temperature"] = finite_number(temperature, -40, 150)
            else:
                values["chip_temperature"] = None
        except KeyError:
            raise InvalidData("设备快照缺少必要属性") from None
        return cls(identity, values, datetime.now(timezone.utc).isoformat())

    def payload(self):
        # Acquisition time is deliberately not called a sensor sample time.
        return {**self.values, "acquired_at": self.acquired_at}
