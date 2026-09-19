# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Non-blocking reconciliation of the user's explicit cloud/local choice."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from .protocol.control import LocalModeError
from .protocol.errors import P3Error

LOGGER = logging.getLogger(__name__)


class LocalModeController:
    def __init__(self, parent, entry):
        self.parent = parent
        self.hass = parent.hass
        self.entry = entry
        self.state = None
        self.last_error = None
        self._lock = asyncio.Lock()
        self._task = None
        self._change = None
        self._unsubscribe = None
        self._closed = False

    def start(self, *, reconcile=True):
        if self._closed or self._unsubscribe or "local_mode" not in self.entry.options:
            return
        self._unsubscribe = async_track_time_interval(
            self.hass, self._schedule, timedelta(seconds=60)
        )
        if reconcile:
            self._schedule()

    @callback
    def _schedule(self, _now=None):
        if self._closed or (self._task and not self._task.done()):
            return
        self._task = self.hass.async_create_background_task(
            self._reconcile(), "Aqara P3 local mode maintenance"
        )

    async def _reconcile(self):
        try:
            async with self._lock:
                if self._closed:
                    return
                action = "enable" if self.entry.options["local_mode"] else "disable"
                self.state = await self.parent.control.local_mode(action)
                await self.parent.control.sync_data_led(action == "enable")
                self.last_error = None
        except (P3Error, OSError, TimeoutError, ValueError) as err:
            code = err.code if isinstance(err, LocalModeError) else "unavailable"
            self.state = None
            if code != self.last_error:
                LOGGER.warning("Could not maintain selected operating mode: %s", code)
            self.last_error = code

    async def refresh(self):
        try:
            async with asyncio.timeout(15):
                async with self._lock:
                    self.state = await self.parent.control.local_mode("status")
                    return self.state
        except (P3Error, OSError, TimeoutError, ValueError):
            self.state = None
            raise

    def start_change(self, enabled):
        if self._closed or (self._change and not self._change.done()):
            raise HomeAssistantError("Operating mode change already in progress")

        async def change():
            async with self._lock:
                if self._closed:
                    raise HomeAssistantError("Integration is unloading")
                try:
                    self.state = await self.parent.control.local_mode(
                        "enable" if enabled else "disable"
                    )
                    await self.parent.control.sync_data_led(enabled)
                except (P3Error, OSError, TimeoutError, ValueError) as err:
                    self.state = None
                    self.last_error = (
                        err.code if isinstance(err, LocalModeError) else "unavailable"
                    )
                    raise
                self.last_error = None
                self.hass.config_entries.async_update_entry(
                    self.entry, options={**self.entry.options, "local_mode": enabled}
                )
                return self.state

        self._change = self.hass.async_create_background_task(
            change(), "Aqara P3 operating mode change"
        )
        # The operation survives closing the dialog, including saving the choice.
        self._change.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return self._change

    async def shutdown(self):
        self._closed = True
        if self._unsubscribe:
            self._unsubscribe()
        tasks = {
            t
            for t in (self._task, self._change)
            if t is not None and t is not asyncio.current_task()
        }
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
