# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
import json

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN, UPDATE_TIMEOUT
from .protocol.config import DeviceConfig
from .protocol.control import LocalModeError
from .protocol.device import ReadOnlyDevice
from .protocol.errors import (
    AuthenticationError,
    InvalidData,
    P3Error,
    UnsupportedDevice,
)
from .protocol.telnet import TelnetReader


async def validate_input(data):
    device = ReadOnlyDevice(
        TelnetReader(
            DeviceConfig(
                device_ip=data[CONF_HOST],
                telnet_password=data.get(CONF_PASSWORD, ""),
            )
        )
    )
    try:
        async with asyncio.timeout(UPDATE_TIMEOUT):
            snapshot = await device.snapshot()
        return snapshot.identity
    finally:
        await device.close()


def schema(host=None):
    host_key = (
        vol.Required(CONF_HOST, default=host) if host else vol.Required(CONF_HOST)
    )
    return vol.Schema(
        {
            host_key: str,
            vol.Optional(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


class P3ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return P3OptionsFlow()

    async def async_step_user(self, user_input=None):
        return await self._form("user", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self._form(
            "reconfigure", user_input, self._get_reconfigure_entry()
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._form("reauth_confirm", user_input, self._get_reauth_entry())

    async def _form(self, step, user_input, entry=None):
        errors = {}
        if user_input is not None:
            data = {**(dict(entry.data) if entry else {}), **user_input}
            data.setdefault(CONF_PASSWORD, "")
            try:
                identity = await validate_input(data)
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except UnsupportedDevice:
                errors["base"] = "unsupported_device"
            except InvalidData:
                errors["base"] = "invalid_data"
            except (P3Error, OSError, TimeoutError, UnicodeError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(identity.uid)
                data.update(
                    mac=identity.mac, model=identity.model, firmware=identity.firmware
                )
                if entry is not None:
                    if entry.unique_id != identity.uid:
                        return self.async_abort(reason="wrong_device")
                    return self.async_update_reload_and_abort(entry, data_updates=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Aqara P3 " + identity.mac[-5:].replace(":", ""), data=data
                )
        host = user_input.get(CONF_HOST) if user_input else None
        if host is None and entry:
            host = entry.data[CONF_HOST]
        return self.async_show_form(
            step_id=step, data_schema=schema(host), errors=errors
        )


class P3OptionsFlow(config_entries.OptionsFlow):
    def __init__(self):
        self._capture_task = None
        self._prepare_task = None
        self._outlet_confirmed = False
        self._mode_task = None
        self._mode_progress = None

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "settings",
                "local_mode",
                "capture",
                "capture_result",
                "outlet_off",
                "outlet_on",
            ],
        )

    async def async_step_local_mode(self, user_input=None, *, error=None):
        controller = self.config_entry.runtime_data.local_mode
        errors = {"base": error} if error else {}
        if user_input is not None:
            try:
                self._mode_task = controller.start_change(user_input["mode"] == "local")

                async def wait_change():
                    return await asyncio.shield(self._mode_task)

                self._mode_progress = self.hass.async_create_task(
                    wait_change(), "Aqara P3 mode change progress"
                )
                return await self.async_step_local_mode_progress()
            except HomeAssistantError:
                errors["base"] = "local_mode_busy"
        elif not error:
            try:
                await controller.refresh()
            except LocalModeError as err:
                key = (
                    err.code
                    if err.code in ("backup_conflict", "system_changed", "busy")
                    else "failed"
                )
                errors["base"] = "local_mode_" + key
            except (P3Error, OSError, TimeoutError, ValueError):
                errors["base"] = "local_mode_failed"
        state = controller.state
        zh = self.hass.config.language.startswith("zh")
        labels = {
            "local": "本地模式" if zh else "Local mode",
            "cloud": "米家云服务" if zh else "Mijia cloud",
        }
        status = (
            labels.get(state["mode"], "") if state else ("待确认" if zh else "Unknown")
        )
        return self.async_show_form(
            step_id="local_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "mode",
                        default="local"
                        if self.config_entry.options.get("local_mode")
                        else "cloud",
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["cloud", "local"], translation_key="operating_mode"
                        )
                    )
                }
            ),
            description_placeholders={"status": status},
            errors=errors,
        )

    async def async_step_local_mode_progress(self, user_input=None):
        if not self._mode_progress.done():
            return self.async_show_progress(
                step_id="local_mode_progress",
                progress_action="changing_local_mode",
                progress_task=self._mode_progress,
            )
        return self.async_show_progress_done(next_step_id="local_mode_result")

    async def async_step_local_mode_result(self, user_input=None):
        try:
            self._mode_progress.result()
        except LocalModeError as err:
            key = (
                err.code
                if err.code
                in ("backup_conflict", "system_changed", "busy", "rollback_failed")
                else "failed"
            )
            return await self.async_step_local_mode(error="local_mode_" + key)
        except (P3Error, HomeAssistantError, OSError, TimeoutError, ValueError):
            return await self.async_step_local_mode(error="local_mode_failed")
        # The controller already saved the choice, even if this dialog was closed.
        # None completes the options flow without writing a second options snapshot.
        return self.async_create_entry(title="", data=None)

    async def async_step_outlet_on(self, user_input=None):
        try:
            await self.config_entry.runtime_data.ac.relay(True)
        except HomeAssistantError:
            return self.async_abort(reason="outlet_failed")
        return self.async_abort(reason="outlet_on_done")

    async def async_step_outlet_off(self, user_input=None):
        self._outlet_confirmed = False
        errors = {}
        if user_input is not None:
            if not user_input.get("confirm", False):
                errors["base"] = "confirmation_required"
            else:
                self._outlet_confirmed = True
                return await self._finish_outlet_off(running_confirmed=False)
        return self.async_show_form(
            step_id="outlet_off",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def async_step_outlet_off_running(self, user_input=None):
        if not self._outlet_confirmed:
            return await self.async_step_outlet_off()
        errors = {}
        if user_input is not None:
            if not user_input.get("confirm", False):
                errors["base"] = "confirmation_required"
            else:
                return await self._finish_outlet_off(running_confirmed=True)
        return self.async_show_form(
            step_id="outlet_off_running",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def _finish_outlet_off(self, *, running_confirmed):
        ac = self.config_entry.runtime_data.ac
        if ac.outlet_running and not running_confirmed:
            return await self.async_step_outlet_off_running()
        try:
            await ac.relay(False, confirmed=True, running_confirmed=running_confirmed)
        except HomeAssistantError as err:
            if err.translation_key == "outlet_running_confirmation_required":
                return await self.async_step_outlet_off_running()
            self._outlet_confirmed = False
            return self.async_abort(reason="outlet_failed")
        self._outlet_confirmed = False
        return self.async_abort(reason="outlet_off_done")

    async def async_step_settings(self, user_input=None):
        if user_input is not None:
            options = {**self.config_entry.options, **user_input}
            if not user_input.get("temperature_entity"):
                options.pop("temperature_entity", None)
            return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Required(
                        "profile",
                        default=self.config_entry.options.get("profile", "daikin_p3"),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["daikin_p3", "capture_only"],
                            translation_key="profile",
                        )
                    ),
                    vol.Optional(
                        "temperature_entity",
                        description={
                            "suggested_value": self.config_entry.options.get(
                                "temperature_entity"
                            )
                        },
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
        )

    async def async_step_capture(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                ac = self.config_entry.runtime_data.ac
                self._capture_task = ac.start_capture(int(user_input["seconds"]))

                async def wait_ready():
                    waiter = asyncio.create_task(ac.capture_started.wait())
                    try:
                        await asyncio.wait(
                            [waiter, self._capture_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if self._capture_task.done():
                            self._capture_task.result()
                    finally:
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)

                self._prepare_task = self.hass.async_create_task(
                    wait_ready(), "P3 capture preparation"
                )
                return await self.async_step_capture_prepare()
            except (P3Error, HomeAssistantError, OSError, TimeoutError, ValueError):
                errors["base"] = "capture_failed"
        return self.async_show_form(
            step_id="capture",
            data_schema=vol.Schema(
                {
                    vol.Required("seconds", default=60): vol.All(
                        vol.Coerce(int), vol.Range(min=10, max=300)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_capture_prepare(self, user_input=None):
        if not self._prepare_task.done():
            return self.async_show_progress(
                step_id="capture_prepare",
                progress_action="preparing_capture",
                progress_task=self._prepare_task,
            )
        try:
            self._prepare_task.result()
        except (P3Error, HomeAssistantError, OSError, TimeoutError, ValueError):
            return self.async_abort(reason="capture_failed")
        return self.async_show_progress_done(next_step_id="capturing")

    async def async_step_capturing(self, user_input=None):
        if self._capture_task.done():
            return await self.async_step_capture_result()
        return self.async_show_menu(step_id="capturing", menu_options=["capture_stop"])

    async def async_step_capture_stop(self, user_input=None):
        await self.config_entry.runtime_data.ac.stop_capture()
        return await self.async_step_capture_result()

    async def async_step_capture_result(self, user_input=None):
        if self._capture_task and self._capture_task.done():
            try:
                self._capture_task.result()
            except (
                P3Error,
                HomeAssistantError,
                OSError,
                TimeoutError,
                ValueError,
                asyncio.CancelledError,
            ):
                return self.async_abort(reason="capture_failed")
        ac = self.config_entry.runtime_data.ac
        errors = {}
        if user_input is not None:
            if user_input.get("adopt", False):
                try:
                    await ac.adopt_capture()
                except (P3Error, HomeAssistantError, OSError, TimeoutError, ValueError):
                    errors["base"] = "no_matching_frame"
            if not errors:
                return self.async_abort(reason="capture_done")
        result = ac.capture_result
        return self.async_show_form(
            step_id="capture_result",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "export",
                        default=json.dumps(result, ensure_ascii=False, indent=2),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional("adopt", default=False): bool,
                }
            ),
            description_placeholders={
                "count": str(result.get("total_frames", 0)),
                "errors": str(result.get("errors", 0)),
            },
            errors=errors,
        )

    @callback
    def async_remove(self):
        if self._capture_task and not self._capture_task.done():
            self.hass.async_create_task(
                self.config_entry.runtime_data.ac.stop_capture(), "Stop P3 capture"
            )
        if self._prepare_task and not self._prepare_task.done():
            self._prepare_task.cancel()
