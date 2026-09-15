# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Device-targeted actions that remain available without an outlet switch."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


@callback
def register_services(hass):
    def coordinator(device_id):
        device = dr.async_get(hass).async_get(device_id)
        if device is None:
            raise HomeAssistantError("Select an Aqara P3 device")
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if (
                entry
                and entry.domain == DOMAIN
                and entry.state == ConfigEntryState.LOADED
            ):
                return entry.runtime_data
        raise HomeAssistantError("Selected Aqara P3 is unavailable")

    async def outlet_off(call):
        co = coordinator(call.data["device_id"])
        await co.ac.relay(
            False,
            confirmed=call.data["confirm_power_cut"],
            running_confirmed=call.data["confirm_running"],
        )

    async def outlet_on(call):
        await coordinator(call.data["device_id"]).ac.relay(True)

    async def play_sound(call):
        await coordinator(call.data["device_id"]).audio.play(
            call.data.get("tone"),
            call.data.get("volume"),
            stop_after=call.data.get("stop_after"),
            repeat=call.data["repeat"],
        )

    async def stop_sound(call):
        await coordinator(call.data["device_id"]).audio.stop()

    definitions = {
        "turn_off_outlet": (
            outlet_off,
            {
                vol.Required("confirm_power_cut"): bool,
                vol.Optional("confirm_running", default=False): bool,
            },
        ),
        "turn_on_outlet": (outlet_on, {}),
        "play_sound": (
            play_sound,
            {
                vol.Optional("tone"): str,
                vol.Optional("volume"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
                vol.Optional("stop_after"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, min_included=False, max=86400)
                ),
                vol.Optional("repeat", default=1): vol.All(
                    int, vol.Range(min=1, max=100)
                ),
            },
        ),
        "stop_sound": (stop_sound, {}),
    }
    for name, (handler, fields) in definitions.items():
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN,
                name,
                handler,
                schema=vol.Schema({vol.Required("device_id"): str, **fields}),
            )
