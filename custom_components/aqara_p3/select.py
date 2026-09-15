# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.exceptions import HomeAssistantError

from .audio import AudioEntity
from .entity import P3Entity

PARALLEL_UPDATES = 0
DRY_OPTIONS = ["-2", "-1.5", "-1", "-0.5", "0", "+0.5", "+1", "+1.5", "+2"]


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [P3SoundSelect(entry.runtime_data.audio)]
    if entry.runtime_data.ac.profile == "daikin_p3":
        entities.append(P3DrySelect(entry.runtime_data))
    async_add_entities(entities)


class P3DrySelect(P3Entity, SelectEntity):
    _attr_options = DRY_OPTIONS

    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            SelectEntityDescription(key="dry_offset", translation_key="dry_offset"),
        )

    @property
    def available(self):
        return super().available and self.coordinator.ac.state.mode == "dry"

    @property
    def current_option(self):
        value = self.coordinator.ac.state.dry_offset
        return next((s for s in DRY_OPTIONS if float(s) == value), None)

    async def async_select_option(self, option):
        if option not in DRY_OPTIONS:
            raise HomeAssistantError("Invalid dry offset")
        await self.coordinator.ac.apply(dry_offset=float(option))


class P3SoundSelect(AudioEntity, SelectEntity):
    def __init__(self, coordinator):
        super().__init__(
            coordinator, SelectEntityDescription(key="sound", translation_key="sound")
        )

    @property
    def options(self):
        return self.coordinator.data["tones"] if self.coordinator.data else []

    @property
    def current_option(self):
        tone = self.coordinator.selected_tone
        return tone if tone in self.options else None

    async def async_select_option(self, option):
        if option not in self.options:
            raise HomeAssistantError("Invalid sound")
        self.coordinator.selected_tone = option
        self.coordinator.async_update_listeners()
