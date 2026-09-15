# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Independent polling of the factory audio service."""

import asyncio
import json
import logging
import re
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN
from .protocol.errors import P3Error
from .sounds import REPEAT_GAP, SOUND_DURATIONS

LOGGER = logging.getLogger(__name__)


def decode_audio(raw):
    volume = raw[(5, 2)]
    if type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError("Invalid audio volume")
    catalog = json.loads(raw[(9, 5)])
    if not isinstance(catalog, dict):
        raise TypeError("Invalid sound catalog")
    tones = []
    for category in ("doorbell", "welcome", "alarm"):
        names = catalog.get(category, [])
        if not isinstance(names, list) or len(names) > 64:
            raise TypeError("Invalid sound catalog")
        for name in names:
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9_]{1,32}", name
            ):
                raise ValueError("Invalid sound name")
            if name not in tones:
                tones.append(name)
    return {
        "volume": volume,
        "tones": tones,
    }


class AudioCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, parent):
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN + "_audio",
            config_entry=entry,
            update_interval=timedelta(seconds=15),
        )
        self.parent = parent
        self.selected_tone = "DingDong"
        self._tasks = set()
        self._command_lock = asyncio.Lock()
        self.start_task = None
        self._playback_task = None
        self._closed = False

    async def _async_update_data(self):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with asyncio.timeout(40):
                return decode_audio(await self.parent.control.audio_read())
        except (P3Error, OSError, TimeoutError, ValueError, KeyError, TypeError) as err:
            raise UpdateFailed("Audio service unavailable") from err
        finally:
            self._tasks.discard(task)

    async def write(self, siid, piid, value):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with self._command_lock:
                await self.parent.control.audio_write(siid, piid, value)
        except (P3Error, OSError, TimeoutError) as err:
            await self.async_refresh()
            raise HomeAssistantError("Audio command was not confirmed") from err
        finally:
            self._tasks.discard(task)
        await self.async_request_refresh()

    async def _send(self, piid, value):
        try:
            await self.parent.control.audio_write(9, piid, value)
        except (P3Error, OSError, TimeoutError) as err:
            raise HomeAssistantError("Audio command was not confirmed") from err

    def _cancel_playback(self):
        if self._playback_task:
            self._playback_task.cancel()
            self._playback_task = None

    async def play(self, tone=None, volume=None, *, stop_after=None, repeat=1):
        """Start a finite sequence; automation continues after the first ACK."""
        if not self.last_update_success or not self.data:
            raise HomeAssistantError("Audio service unavailable")
        tone = self.selected_tone if tone is None else tone
        volume = self.data["volume"] if volume is None else volume
        if (
            tone not in self.data["tones"]
            or type(volume) is not int
            or not 0 <= volume <= 100
        ):
            raise HomeAssistantError("Invalid sound or volume")
        if (
            type(repeat) is not int
            or not 1 <= repeat <= 100
            or (
                stop_after is not None
                and (
                    type(stop_after) not in (int, float) or not 0 < stop_after <= 86400
                )
            )
        ):
            raise HomeAssistantError("Invalid repeat count or stop time")
        duration = SOUND_DURATIONS.get(tone)
        if duration is None:
            raise HomeAssistantError("Sound duration unavailable")
        payload = json.dumps({"name": tone, "volume": volume}, separators=(",", ":"))
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with self._command_lock:
                if self._closed:
                    raise HomeAssistantError("Audio service unavailable")
                replacing = self._playback_task is not None
                self._cancel_playback()
                if replacing:
                    await self._send(4, 1)
                await self._send(1, payload)
                started = self.hass.loop.time()
                deadline = None if stop_after is None else started + stop_after
                self._playback_task = self.hass.async_create_background_task(
                    self._play_sequence(payload, duration, repeat, deadline),
                    "Aqara P3 sound playback",
                )
        finally:
            self._tasks.discard(task)

    async def _play_sequence(self, payload, duration, repeat, deadline):
        task = asyncio.current_task()
        try:
            for remaining in range(repeat - 1, -1, -1):
                next_play = (
                    self.hass.loop.time() + duration + (REPEAT_GAP if remaining else 0)
                )
                wake = next_play if deadline is None else min(next_play, deadline)
                await asyncio.sleep(max(0, wake - self.hass.loop.time()))
                async with self._command_lock:
                    # A new command may have replaced this sequence while waiting.
                    if self._playback_task is not task or self._closed:
                        return
                    if deadline is not None and self.hass.loop.time() >= deadline:
                        await self._send(4, 1)
                        return
                    if remaining:
                        await self._send(1, payload)
        except HomeAssistantError:
            LOGGER.exception("Sound sequence ended: audio command was not confirmed")
        finally:
            if self._playback_task is task:
                self._playback_task = None

    async def stop(self):
        """Stop the current sound and discard all remaining repetitions."""
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with self._command_lock:
                self._cancel_playback()
                if self._closed:
                    raise HomeAssistantError("Audio service unavailable")
                await self._send(4, 1)
        finally:
            self._tasks.discard(task)

    async def async_shutdown(self):
        self._closed = True
        await super().async_shutdown()
        tasks = set(self._tasks)
        playback = self._playback_task
        if playback:
            tasks.add(playback)
        self._cancel_playback()
        if self.start_task:
            tasks.add(self.start_task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if playback:
            try:
                async with asyncio.timeout(5):
                    await self._send(4, 1)
            except (HomeAssistantError, TimeoutError):
                LOGGER.warning("Could not stop sound during shutdown")


class AudioEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, description):
        super().__init__(coordinator)
        self.entity_description = description
        parent = coordinator.parent
        self._attr_unique_id = f"{parent.expected_uid}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, parent.expected_uid + "_security")},
            via_device_id=dr.async_get(coordinator.hass)
            .async_get_device_by_identifier(
                (DOMAIN, parent.expected_uid), parent.config_entry.entry_id
            )
            .id,
            manufacturer="Aqara",
            model="KTBL12LM",
            sw_version=parent.identity.firmware,
            name=(
                "Aqara P3 声音 "
                if coordinator.hass.config.language.startswith("zh")
                else "Aqara P3 Sound "
            )
            + parent.identity.mac[-5:].replace(":", ""),
        )
