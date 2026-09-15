# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Persisted complete AC state; never overwrite it with stale MIOT cache."""

import asyncio
import logging
import math
import time
from datetime import datetime

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import DOMAIN, OUTLET_RUNNING_POWER_W
from .protocol.errors import P3Error
from .protocol.profiles.daikin_p3 import (
    MODES,
    POWERFUL_DURATION,
    POWERFUL_MODES,
    RANGES,
    DaikinP3,
)

LOGGER = logging.getLogger(__name__)


class ACController:
    def __init__(self, coordinator, entry):
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.profile = entry.options.get(
            "profile", entry.data.get("profile", "daikin_p3")
        )
        self.state = DaikinP3.default()
        self.mode_preferences = {}
        self.valid = False
        self.source = "uninitialized"
        self.updated_at = None
        self.deadlines = {}
        self.capture_result = {}
        self.capture_task = None
        self.capture_ready = False
        self.capture_started = asyncio.Event()
        self._lock = asyncio.Lock()
        self.store = Store(self.hass, 1, "aqara_p3_" + entry.unique_id)
        self._command_tasks = set()
        self.power_confirmation_required = False
        self._timer_cancel = None
        self.powerful_deadline = None
        self._stopping_capture = None

    async def load(self):
        doc = await self.store.async_load()
        if not doc:
            return
        try:
            self.state = DaikinP3.from_hex(doc["state"])
            if self.state.mode not in POWERFUL_MODES:
                self.state = self.state.changed(powerful=False)
            self._load_mode_preferences(doc.get("mode_preferences", {}))
            self._remember_mode(self.state)
            self.valid = bool(doc.get("valid", False))
            self.power_confirmation_required = bool(
                doc.get("power_confirmation_required", False)
            )
            self.source = "restored_last_command" if self.valid else "uninitialized"
            self.updated_at = doc.get("updated_at")
            powerful = doc.get("powerful_deadline")
            self.powerful_deadline = (
                powerful
                if self.state.feature("powerful")
                and type(powerful) in (int, float)
                and math.isfinite(powerful)
                else None
            )
            self.deadlines = {
                k: float(v)
                for k, v in doc.get("deadlines", {}).items()
                if k in ("on", "off", "sleep")
                and type(v) in (int, float)
                and math.isfinite(v)
            }
            result = doc.get("capture", {})
            if isinstance(result, dict) and len(result.get("frames", [])) <= 64:
                self.capture_result = result
        except (P3Error, KeyError, TypeError, ValueError):
            self.valid = False
            self.deadlines = {}
        await self.expire_timers()
        self._schedule_timer()

    async def save(self):
        await self.store.async_save(
            {
                "state": self.state.raw.hex(),
                "mode_preferences": {
                    mode: dict(values) for mode, values in self.mode_preferences.items()
                },
                "valid": self.valid,
                "power_confirmation_required": self.power_confirmation_required,
                "updated_at": self.updated_at,
                "deadlines": self.deadlines,
                "powerful_deadline": self.powerful_deadline,
                "capture": self.capture_result,
            }
        )

    @staticmethod
    def _mode_fields(mode):
        fields = {"fan"}
        if mode in RANGES:
            fields.add("temperature")
        elif mode == "dry":
            fields.add("dry_offset")
        return fields

    def _load_mode_preferences(self, saved):
        self.mode_preferences = {}
        if not isinstance(saved, dict):
            return
        for mode in MODES:
            values = saved.get(mode)
            if not isinstance(values, dict) or set(values) != self._mode_fields(mode):
                continue
            try:
                DaikinP3.default().changed(mode=mode, **values)
            except (P3Error, TypeError, ValueError):
                continue
            self.mode_preferences[mode] = dict(values)

    def _remember_mode(self, state):
        self.mode_preferences[state.mode] = {
            field: getattr(state, field) for field in self._mode_fields(state.mode)
        }

    def remaining_state(self):
        state = self.state
        for key, deadline in self.deadlines.items():
            remaining = max(0, min(720, math.ceil((deadline - time.time()) / 60)))
            state = state.timers(**{key: remaining})
        if self.powerful_deadline is not None and self.powerful_deadline <= time.time():
            state = state.changed(powerful=False)
        return state

    def _schedule_timer(self):
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None
        deadlines = list(self.deadlines.values())
        if self.powerful_deadline is not None:
            deadlines.append(self.powerful_deadline)
        if not deadlines:
            return

        async def elapsed(_now):
            self._timer_cancel = None
            await self.expire_timers()

        self._timer_cancel = async_call_later(
            self.hass, max(0.1, min(deadlines) - time.time()), elapsed
        )

    def _expire_timers_locked(self):
        """Clear elapsed flags without sending IR or claiming physical feedback."""
        now = time.time()
        expired = {key for key, deadline in self.deadlines.items() if deadline <= now}
        powerful_expired = (
            self.powerful_deadline is not None and self.powerful_deadline <= now
        )
        if not expired and not powerful_expired:
            return False
        self.state = self.remaining_state()
        self.deadlines = {k: v for k, v in self.deadlines.items() if v > now}
        if expired & {"off", "sleep"}:
            # Never replay the pre-shutoff power bit on an unrelated command.
            self.power_confirmation_required = True
        if powerful_expired:
            self.powerful_deadline = None
        if expired:
            self.valid = False
            self.source = "timer_elapsed_unconfirmed"
        return True

    async def expire_timers(self):
        if self._lock.locked():
            self._schedule_timer()
            return
        async with self._lock:
            if self._expire_timers_locked():
                await self.save()
                self.coordinator.async_update_listeners()
            self._schedule_timer()

    async def apply(self, *, timers=None, **changes):
        if self.profile != "daikin_p3":
            raise HomeAssistantError(
                "Select the Daikin E-Max 7 profile in integration options"
            )
        if self.capture_task and not self.capture_task.done():
            raise HomeAssistantError("Stop infrared capture first")
        if not self.coordinator.last_update_success:
            raise HomeAssistantError("P3 is unavailable")
        task = asyncio.current_task()
        self._command_tasks.add(task)
        try:
            async with self._lock:
                if self._expire_timers_locked():
                    await self.save()
                    self.coordinator.async_update_listeners()
                    self._schedule_timer()
                if self.power_confirmation_required and "power" not in changes:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="timer_power_required",
                    )
                base = self.remaining_state()
                mode = changes.get("mode", base.mode)
                remembered = (
                    self.mode_preferences.get(mode, {}) if mode != base.mode else {}
                )
                if changes.get("powerful") is True and mode not in POWERFUL_MODES:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerful_mode_required",
                    )
                effective = {**remembered, **changes}
                if (
                    mode not in POWERFUL_MODES
                    or changes.get("power") is False
                    or (
                        base.feature("powerful")
                        and (
                            "temperature" in changes
                            or "fan" in changes
                            or mode != base.mode
                        )
                    )
                ):
                    effective["powerful"] = False
                state = base.changed(**effective)
                deadlines = {k: v for k, v in self.deadlines.items() if v > time.time()}
                if timers is not None:
                    state = state.timers(**timers)
                    for key, minutes in timers.items():
                        if minutes:
                            deadlines[key] = time.time() + minutes * 60
                        else:
                            deadlines.pop(key, None)
                        if minutes and key in ("on", "sleep"):
                            deadlines.pop("sleep" if key == "on" else "on", None)
                if base.feature("powerful") and (
                    state.fan != base.fan or state.temperature != base.temperature
                ):
                    state = state.changed(powerful=False)
                try:
                    # Record uncertainty before I/O. HA exiting mid-command
                    # must not restore a stale acknowledged state on restart.
                    self.valid = False
                    self.source = "command_in_flight"
                    await self.save()
                    await self.coordinator.control.send(state)
                except (P3Error, OSError, TimeoutError, UnicodeError):
                    self.valid = False
                    self.source = "delivery_unconfirmed"
                    self.coordinator.async_update_listeners()
                    # Persist uncertainty, so restart cannot resurrect stale certainty.
                    await self.save()
                    raise HomeAssistantError(
                        "P3 did not confirm the command; it was not retried"
                    ) from None
                self.state = state
                self._remember_mode(state)
                self.deadlines = deadlines
                if not state.feature("powerful"):
                    self.powerful_deadline = None
                elif changes.get("powerful") is True:
                    self.powerful_deadline = time.time() + POWERFUL_DURATION
                self.valid = True
                self.power_confirmation_required = False
                self._schedule_timer()
                self.source = "last_sent_command"
                self.updated_at = time.time()
                self.coordinator.async_update_listeners()
                await self.save()
        finally:
            self._command_tasks.discard(task)

    @property
    def outlet_running(self):
        """Warning condition only; low power must not override a recorded on state."""
        power = (self.coordinator.data or {}).get("power_w")
        return self.state.power or (
            type(power) in (int, float)
            and math.isfinite(power)
            and power > OUTLET_RUNNING_POWER_W
        )

    async def relay(self, on, *, confirmed=False, running_confirmed=False):
        if self.capture_task and not self.capture_task.done():
            raise HomeAssistantError("Stop infrared capture first")
        task = asyncio.current_task()
        self._command_tasks.add(task)
        try:
            async with self._lock:
                if not on and not confirmed:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="outlet_confirmation_required",
                    )
                # Recheck under the same lock as AC commands, even if a warning
                # was shown while the recorded AC state was off or power was low.
                if not on and self.outlet_running and not running_confirmed:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="outlet_running_confirmation_required",
                    )
                if not on:
                    self.valid = False
                    self.source = "power_removal_unconfirmed"
                    await self.save()
                await self.coordinator.control.relay(on)
                if not on:
                    self.state = self.state.changed(power=False).timers(
                        on=0, off=0, sleep=0
                    )
                    self.deadlines = {}
                    self.powerful_deadline = None
                    self.state = self.state.changed(powerful=False, rapid=False)
                    self.power_confirmation_required = False
                    self.source = "outlet_power_removed"
                    self._schedule_timer()
                    await self.save()
                    self.coordinator.async_update_listeners()
        except (P3Error, OSError, TimeoutError):
            self.coordinator.async_update_listeners()
            raise HomeAssistantError(
                "Relay command was not confirmed; it was not retried"
            ) from None
        finally:
            self._command_tasks.discard(task)
        await self.coordinator.async_request_refresh()

    def start_capture(self, seconds):
        if self.capture_task and not self.capture_task.done():
            raise HomeAssistantError("Capture already running")
        if self._command_tasks:
            raise HomeAssistantError("Wait for the current command to finish")
        self.capture_ready = False
        self.capture_started.clear()

        async def work():
            def ready():
                self.capture_ready = True
                self.capture_started.set()
                self.coordinator.async_update_listeners()

            try:
                self.capture_result = await self.coordinator.control.capture(
                    seconds, ready
                )
                await self.save()
                return self.capture_result
            except BaseException:
                partial = self.coordinator.control.last_capture_result
                if isinstance(partial, dict):
                    self.capture_result = partial
                    await self.save()
                raise
            finally:
                self.capture_ready = False
                self.coordinator.async_update_listeners()

        self.capture_task = self.hass.async_create_background_task(
            work(), "P3 infrared capture"
        )
        # Consume unobserved exceptions if the options dialog was closed.
        self.capture_task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )
        return self.capture_task

    async def adopt_capture(self):
        if self.profile != "daikin_p3":
            raise HomeAssistantError("Select the Daikin profile first")
        frames = [
            f
            for f in self.capture_result.get("frames", [])
            if f.get("profile") == "daikin_p3"
        ]
        if not frames:
            raise HomeAssistantError("No matching Daikin state captured")
        async with self._lock:
            self.state = DaikinP3.from_hex(frames[-1]["state_hex"])
            if self.state.mode not in POWERFUL_MODES:
                self.state = self.state.changed(powerful=False)
            self._remember_mode(self.state)
            self.valid = True
            self.source = "received_remote"
            self.power_confirmation_required = False
            try:
                self.updated_at = datetime.fromisoformat(frames[-1]["utc"]).timestamp()
            except (KeyError, TypeError, ValueError):
                self.updated_at = time.time()
            self.deadlines = {
                key: self.updated_at + minutes * 60
                for key, minutes in (
                    ("on", self.state.on_minutes),
                    ("off", self.state.off_minutes),
                    ("sleep", self.state.sleep_minutes),
                )
                if minutes
            }
            self.powerful_deadline = (
                self.updated_at + POWERFUL_DURATION
                if self.state.feature("powerful")
                else None
            )
            self._expire_timers_locked()
            self._schedule_timer()
            self.coordinator.async_update_listeners()
            await self.save()

    async def stop_capture(self):
        if self._stopping_capture and not self._stopping_capture.done():
            await asyncio.shield(self._stopping_capture)
            return
        task = self.capture_task
        if task is None or task.done():
            return

        async def finish():
            # Closing during helper preparation must wait until a PID exists.
            ready = asyncio.create_task(self.capture_started.wait())
            try:
                async with asyncio.timeout(35):
                    await asyncio.wait(
                        [ready, task], return_when=asyncio.FIRST_COMPLETED
                    )
                if not task.done():
                    await self.coordinator.control._stop_capture_process()
                    async with asyncio.timeout(8):
                        await asyncio.shield(task)
                elif not task.cancelled():
                    task.result()
            except (TimeoutError, P3Error, OSError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)

        self._stopping_capture = self.hass.async_create_background_task(
            finish(), "Finish P3 capture"
        )
        await asyncio.shield(self._stopping_capture)

    async def shutdown(self):
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None
        await self.stop_capture()
        current = asyncio.current_task()
        tasks = [t for t in self._command_tasks if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.coordinator.control.close()
