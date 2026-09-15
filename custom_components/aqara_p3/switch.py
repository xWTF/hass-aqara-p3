# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription

from .entity import P3Entity
from .protocol.profiles.daikin_p3 import FEATURES, POWERFUL_MODES

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    co = entry.runtime_data
    if co.ac.profile == "daikin_p3":
        async_add_entities(P3Switch(co, key) for key in FEATURES)


class P3Switch(P3Entity, SwitchEntity):
    def __init__(self, coordinator, key):
        super().__init__(
            coordinator, SwitchEntityDescription(key=key, translation_key=key)
        )
        self.key = key
        self._attr_assumed_state = True

    @property
    def available(self):
        return super().available and (
            self.key != "powerful" or self.coordinator.ac.state.mode in POWERFUL_MODES
        )

    @property
    def is_on(self):
        return (
            self.coordinator.ac.state.feature(self.key)
            if self.coordinator.ac.valid
            else None
        )

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)

    async def _set(self, value):
        await self.coordinator.ac.apply(**{self.key: value})
