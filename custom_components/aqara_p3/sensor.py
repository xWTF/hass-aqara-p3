# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)

from .entity import P3Entity

PARALLEL_UPDATES = 0
DESCRIPTIONS = (
    SensorEntityDescription(
        key="power_w",
        translation_key="power_w",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="energy_kwh",
        translation_key="energy_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="chip_temperature",
        translation_key="chip_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(P3Sensor(entry.runtime_data, desc) for desc in DESCRIPTIONS)


class P3Sensor(P3Entity, SensorEntity):
    @property
    def native_value(self):
        return self.coordinator.data.get(self.entity_description.key)
