# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import math
from typing import ClassVar

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from .entity import P3Entity
from .protocol.profiles.daikin_p3 import FANS, RANGES

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.runtime_data.ac.profile == "daikin_p3":
        async_add_entities([P3Climate(entry.runtime_data, entry)])


class P3Climate(P3Entity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_precision = 0.5
    _attr_hvac_modes: ClassVar = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
        HVACMode.AUTO,
    ]
    _attr_fan_modes: ClassVar = list(FANS)
    _attr_swing_modes: ClassVar = ["off", "vertical", "horizontal", "both"]
    _attr_assumed_state = True

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator,
            EntityDescription(key="air_conditioner", translation_key="air_conditioner"),
        )
        self.room_sensor = entry.options.get("temperature_entity")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.room_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.room_sensor], self._room_changed
                )
            )

    @callback
    def _room_changed(self, event):
        self.async_write_ha_state()

    @property
    def supported_features(self):
        features = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self.coordinator.ac.state.mode in RANGES:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        return features

    @property
    def hvac_mode(self):
        ac = self.coordinator.ac
        return (
            (HVACMode(ac.state.mode) if ac.state.power else HVACMode.OFF)
            if ac.valid
            else None
        )

    @property
    def target_temperature(self):
        return self.coordinator.ac.state.temperature

    @property
    def min_temp(self):
        return RANGES.get(self.coordinator.ac.state.mode, (18, 32))[0]

    @property
    def max_temp(self):
        return RANGES.get(self.coordinator.ac.state.mode, (18, 32))[1]

    @property
    def fan_mode(self):
        return self.coordinator.ac.state.fan

    @property
    def swing_mode(self):
        return self.coordinator.ac.state.swing

    @property
    def current_temperature(self):
        if not self.room_sensor:
            return None
        state = self.hass.states.get(self.room_sensor)
        if state is None:
            return None
        try:
            value = float(state.state)
            unit = state.attributes.get("unit_of_measurement")
            if not math.isfinite(value) or unit not in ("°C", "°F", "K"):
                return None
            return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self):
        ac = self.coordinator.ac
        return {
            "state_source": ac.source,
            "power_confirmation_required": ac.power_confirmation_required,
            "last_command_timestamp": ac.updated_at,
            "room_temperature_entity": self.room_sensor,
            "profile": ac.profile,
        }

    async def async_set_hvac_mode(self, hvac_mode):
        await self.coordinator.ac.apply(
            **(
                {"power": False}
                if hvac_mode == HVACMode.OFF
                else {"power": True, "mode": str(hvac_mode)}
            )
        )

    async def async_turn_on(self):
        await self.coordinator.ac.apply(power=True)

    async def async_turn_off(self):
        await self.coordinator.ac.apply(power=False)

    async def async_set_temperature(self, **kwargs):
        changes = {"temperature": kwargs[ATTR_TEMPERATURE]}
        mode = kwargs.get("hvac_mode")
        if mode is not None:
            changes.update(
                {"power": False}
                if mode == HVACMode.OFF
                else {"mode": str(mode), "power": True}
            )
        await self.coordinator.ac.apply(**changes)

    async def async_set_fan_mode(self, fan_mode):
        await self.coordinator.ac.apply(fan=fan_mode)

    async def async_set_swing_mode(self, swing_mode):
        await self.coordinator.ac.apply(swing=swing_mode)
