# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio

from .errors import ResourceMissing
from .models import DeviceIdentity, Snapshot
from .telnet import Resource, TelnetReader


class ReadOnlyDevice:
    def __init__(self, transport: TelnetReader):
        self.transport = transport
        self.identity = None

    async def identify(self):
        # Serial requests on one shell; interleaving would corrupt framing.
        async with asyncio.timeout(30):
            model = (await self.transport.read(Resource.MODEL)).strip()
            mac = (await self.transport.read(Resource.MAC)).strip().lower()
            firmware = (await self.transport.read(Resource.FIRMWARE)).strip()
            self.identity = DeviceIdentity(mac, model, firmware)
        return self.identity

    async def snapshot(self):
        async with asyncio.timeout(45):
            if not self.transport.connected:
                self.identity = None
            if self.identity is None:
                await self.identify()
            values = {}
            for name, resource in (
                ("power", Resource.POWER),
                ("load_power", Resource.LOAD_POWER),
                ("ac", Resource.AC),
                ("fan", Resource.FAN),
                ("relay", Resource.RELAY),
                ("relay_state", Resource.RELAY_STATE),
                ("ac_function", Resource.AC_FUNCTION),
                ("chip_temperature", Resource.CHIP_TEMPERATURE),
            ):
                try:
                    values[name] = await self.transport.read(resource)
                except ResourceMissing:
                    values[name] = None
            return Snapshot.decode(self.identity, **values)

    async def close(self):
        await self.transport.close()
