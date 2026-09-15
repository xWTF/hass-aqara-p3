# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ac_controller import ACController
from .audio import AudioCoordinator
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN, UPDATE_TIMEOUT
from .local_mode import LocalModeController
from .protocol.config import DeviceConfig
from .protocol.control import P3Control
from .protocol.device import ReadOnlyDevice
from .protocol.errors import AuthenticationError, P3Error
from .protocol.telnet import TelnetReader

LOGGER = logging.getLogger(__name__)


class P3Coordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.reload_data = dict(entry.data)
        self.reload_options = {
            k: v for k, v in entry.options.items() if k != "local_mode"
        }
        self.device = ReadOnlyDevice(
            TelnetReader(
                DeviceConfig(
                    device_ip=entry.data[CONF_HOST],
                    telnet_password=entry.data.get(CONF_PASSWORD, ""),
                    poll_interval=entry.options.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    ),
                )
            )
        )
        self.expected_uid = entry.unique_id
        self.identity = None
        self.last_cache_read = None
        self._update_task = None
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=self.device.transport.config.poll_interval
            ),
            always_update=False,
        )
        self.control = P3Control(self.device.transport.config, self.expected_uid)
        self.ac = ACController(self, entry)
        self.audio = AudioCoordinator(hass, entry, self)
        self.local_mode = LocalModeController(self, entry)

    async def _async_update_data(self):
        task = asyncio.current_task()
        self._update_task = task
        try:
            async with asyncio.timeout(UPDATE_TIMEOUT):
                snapshot = await self.device.snapshot()
            if snapshot.identity.uid != self.expected_uid:
                raise AuthenticationError("设备标识改变")
            self.identity = snapshot.identity
            self.last_cache_read = snapshot.acquired_at
            await self.ac.expire_timers()
            # Cache acquisition timestamp is not a hardware measurement time.
            # Unchanged readings do not force all entity states to be rewritten.
            return snapshot.values
        except AuthenticationError:
            await self.device.close()
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="authentication_failed"
            ) from None
        except (P3Error, OSError, TimeoutError, UnicodeError):
            await self.device.close()
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="read_failed"
            ) from None
        except asyncio.CancelledError:
            await self.device.close()
            raise
        finally:
            if self._update_task is task:
                self._update_task = None

    async def async_shutdown(self):
        await super().async_shutdown()
        await self.local_mode.shutdown()
        await self.audio.async_shutdown()
        await self.ac.shutdown()
        task = self._update_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.device.close()
