# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import ipaddress
from dataclasses import dataclass, field

from .errors import InvalidData


@dataclass(frozen=True)
class DeviceConfig:
    device_ip: str
    telnet_password: str = field(default="", repr=False)
    poll_interval: int = 15
    connect_timeout: float = 8.0
    command_timeout: float = 5.0

    def __post_init__(self):
        try:
            addr = ipaddress.ip_address(self.device_ip)
        except ValueError:
            raise InvalidData("设备地址必须是 IP 地址") from None
        if addr.is_unspecified or addr.is_multicast:
            raise InvalidData("不能使用未指定地址或组播地址")
        if any(c in self.telnet_password for c in "\r\n\x00"):
            raise InvalidData("Telnet 密码不能包含换行或 NUL")
        if type(self.poll_interval) is not int or not 10 <= self.poll_interval <= 300:
            raise InvalidData("轮询间隔必须是 10–300 秒")
        if not 0 < self.connect_timeout <= 30 or not 0 < self.command_timeout <= 30:
            raise InvalidData("连接或命令超时超出范围")

    @classmethod
    def from_options(cls, options: dict):
        password = options.get("telnet_password", "")
        if not isinstance(password, str):
            raise InvalidData("Telnet 密码必须是字符串")
        return cls(
            device_ip=options.get("device_ip", ""),
            telnet_password=password,
            poll_interval=options.get("poll_interval", 15),
        )
