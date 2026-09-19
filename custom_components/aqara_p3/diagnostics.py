# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {
        "version": "0.4.12",
        "local_mode": {
            "requested": entry.options.get("local_mode"),
            "state": coordinator.local_mode.state,
            "last_error": coordinator.local_mode.last_error,
            "led_error": coordinator.led_error,
        },
        "audio_available": coordinator.audio.last_update_success,
        "audio": coordinator.audio.data,
        "model": entry.data.get("model"),
        "firmware": entry.data.get("firmware"),
        "available": coordinator.last_update_success,
        "last_cache_read": coordinator.last_cache_read,
        "energy_error": coordinator.energy_error,
        "data_source": (coordinator.data or {}).get("source"),
        "values": coordinator.data,
        "control": {
            "profile": coordinator.ac.profile,
            "state_source": coordinator.ac.source,
            "state_confirmed_by_ipc": coordinator.ac.valid,
            "state_hex": coordinator.ac.state.raw.hex(),
            "last_command_timestamp": coordinator.ac.updated_at,
        },
        "capture": coordinator.ac.capture_result,
    }
