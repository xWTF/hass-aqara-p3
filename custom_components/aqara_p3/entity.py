# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import P3Coordinator


class P3Entity(CoordinatorEntity[P3Coordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator, description):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.expected_uid}_{description.key}"
        identity = coordinator.identity
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.expected_uid)},
            manufacturer="Aqara",
            model="KTBL12LM",
            sw_version=identity.firmware,
            name="Aqara P3 " + identity.mac[-5:].replace(":", ""),
        )
