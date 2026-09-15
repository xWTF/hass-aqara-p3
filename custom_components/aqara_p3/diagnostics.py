# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {
        "version": "0.4.4",
        "audio_available": coordinator.audio.last_update_success,
        "audio": coordinator.audio.data,
        "model": entry.data.get("model"),
        "firmware": entry.data.get("firmware"),
        "available": coordinator.last_update_success,
        "last_cache_read": coordinator.last_cache_read,
        "data_source": "firmware_cache",
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
