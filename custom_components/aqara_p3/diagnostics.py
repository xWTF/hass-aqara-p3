# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {
        "version": "0.5.1",
        "local_mode": {
            "requested": entry.options.get("local_mode"),
            "state": coordinator.local_mode.state,
            "last_error": coordinator.local_mode.last_error,
            "led_error": coordinator.led_error,
        },
        "audio_available": coordinator.audio.last_update_success,
        "audio": coordinator.audio.data,
        "media": {
            "state": coordinator.audio.media.state,
            "last_error": coordinator.audio.media.error,
            "last_error_detail": coordinator.audio.media.error_detail,
            "last_error_stage": coordinator.audio.media.error_stage,
            "format": "S32_LE/32000/mono",
        },
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
