# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.exceptions import HomeAssistantError

from .audio import AudioEntity
from .entity import P3Entity

PARALLEL_UPDATES = 0
KEYS = ("on_timer", "off_timer", "coanda_sleep")


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [P3Volume(entry.runtime_data.audio)]
    if entry.runtime_data.ac.profile == "daikin_p3":
        entities.extend(P3Number(entry.runtime_data, key) for key in KEYS)
    async_add_entities(entities)


class P3Number(P3Entity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, key):
        super().__init__(
            coordinator,
            NumberEntityDescription(
                key=key,
                translation_key=key,
                entity_registry_enabled_default=False,
                native_min_value=0,
                native_max_value=12,
                native_step=1,
                native_unit_of_measurement="h",
            ),
        )
        self.key = key

    @property
    def native_value(self):
        state = self.coordinator.ac.remaining_state()
        return round(
            {
                "on_timer": state.on_minutes,
                "off_timer": state.off_minutes,
                "coanda_sleep": state.sleep_minutes,
            }[self.key]
            / 60,
            2,
        )

    async def async_set_native_value(self, value):
        if value != int(value) or not 0 <= value <= 12:
            raise HomeAssistantError(
                "Timer must be a whole number of hours from 0 to 12"
            )
        key = {"on_timer": "on", "off_timer": "off", "coanda_sleep": "sleep"}[self.key]
        await self.coordinator.ac.apply(timers={key: int(value * 60)})


class P3Volume(AudioEntity, NumberEntity):
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            NumberEntityDescription(
                key="sound_volume",
                translation_key="sound_volume",
                native_min_value=0,
                native_max_value=100,
                native_step=1,
                native_unit_of_measurement="%",
            ),
        )

    @property
    def native_value(self):
        return self.coordinator.data["volume"] if self.coordinator.data else None

    async def async_set_native_value(self, value):
        if value != int(value) or not 0 <= value <= 100:
            raise HomeAssistantError("Invalid volume")
        await self.coordinator.write(5, 2, int(value))
