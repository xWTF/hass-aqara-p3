# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .entity import P3Entity

PARALLEL_UPDATES = 0
DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="relay_on",
        translation_key="relay_on",
        device_class=BinarySensorDeviceClass.POWER,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        P3BinarySensor(entry.runtime_data, desc) for desc in DESCRIPTIONS
    )


class P3BinarySensor(P3Entity, BinarySensorEntity):
    @property
    def is_on(self):
        return self.coordinator.data.get(self.entity_description.key)
