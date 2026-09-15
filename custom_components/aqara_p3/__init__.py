# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Native HA integration for P3; local asynchronous reads, no installer or MQTT."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .coordinator import P3Coordinator

type P3ConfigEntry = ConfigEntry[P3Coordinator]


async def async_setup_entry(hass: HomeAssistant, entry: P3ConfigEntry) -> bool:
    coordinator = P3Coordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.ac.load()
        entry.runtime_data = coordinator
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer="Aqara",
            model="KTBL12LM",
            name="Aqara P3 " + coordinator.identity.mac[-5:].replace(":", ""),
        )
        registry = er.async_get(hass)
        for platform, key in (
            ("switch", "outlet"),
            ("number", "dry_offset"),
            ("button", "stop_capture"),
            ("alarm_control_panel", "security_alarm"),
        ):
            old_entity = registry.async_get_entity_id(
                platform, DOMAIN, f"{entry.unique_id}_{key}"
            )
            if (
                old_entity
                and registry.async_get(old_entity).config_entry_id == entry.entry_id
            ):
                registry.async_remove(old_entity)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        coordinator.audio.start_task = hass.async_create_background_task(
            coordinator.audio.async_refresh(), "Aqara P3 audio discovery"
        )
        from .services import register_services

        register_services(hass)
        coordinator.local_mode.start()
    except BaseException:
        await coordinator.async_shutdown()
        raise

    async def stop(_event):
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass: HomeAssistant, entry: P3ConfigEntry):
    coordinator = entry.runtime_data
    options = {k: v for k, v in entry.options.items() if k != "local_mode"}
    if (
        dict(entry.data) == coordinator.reload_data
        and options == coordinator.reload_options
    ):
        # The mode controller applied the change before saving this option.
        coordinator.local_mode.start(reconcile=False)
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: P3ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        return True
    return False
