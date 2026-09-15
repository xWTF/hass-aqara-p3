# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

from homeassistant.const import Platform

DOMAIN = "aqara_p3"
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.SELECT,
]
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 15
UPDATE_TIMEOUT = 12
OUTLET_RUNNING_POWER_W = 100
