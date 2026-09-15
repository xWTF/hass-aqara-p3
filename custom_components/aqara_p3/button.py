# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .audio import AudioEntity
from .entity import P3Entity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    co = entry.runtime_data
    entities = [
        P3SoundButton(co.audio, "play_sound"),
        P3SoundButton(co.audio, "stop_sound"),
    ]
    if co.ac.profile == "daikin_p3":
        entities.append(P3Button(co))
    async_add_entities(entities)


class P3Button(P3Entity, ButtonEntity):
    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            ButtonEntityDescription(
                key="cancel_timers",
                translation_key="cancel_timers",
                entity_registry_enabled_default=False,
            ),
        )

    async def async_press(self):
        await self.coordinator.ac.apply(timers={"on": 0, "off": 0, "sleep": 0})


class P3SoundButton(AudioEntity, ButtonEntity):
    def __init__(self, coordinator, key):
        super().__init__(
            coordinator, ButtonEntityDescription(key=key, translation_key=key)
        )
        self.key = key

    async def async_press(self):
        if self.key == "play_sound":
            await self.coordinator.play()
        else:
            await self.coordinator.stop()
