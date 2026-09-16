# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Keep the firmware's actual watt-second integrator continuous in HA."""

import re

from homeassistant.helpers.storage import Store

from .protocol.errors import InvalidData


def validate_sample(sample):
    if not isinstance(sample, dict) or set(sample) != {
        "epoch",
        "committed_wh",
        "pending_ws",
    }:
        raise InvalidData("原厂电量计数格式无效")
    if not isinstance(sample["epoch"], str) or not re.fullmatch(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}:[0-9]{1,8}:[0-9]{1,20}",
        sample["epoch"],
    ):
        raise InvalidData("原厂电量计数周期无效")
    for key, maximum in (("committed_wh", 1_000_000_000), ("pending_ws", 8_000_000)):
        if type(sample[key]) is not int or not 0 <= sample[key] <= maximum:
            raise InvalidData("原厂电量计数越界")
    return sample


class EnergyCounter:
    def __init__(self, hass, uid):
        self.store = Store(hass, 1, "aqara_p3_energy_" + uid)
        self.loaded = False
        self.data = None

    async def update(self, sample):
        sample = validate_sample(sample)
        if not self.loaded:
            stored = await self.store.async_load()
            if stored is not None:
                if (
                    not isinstance(stored, dict)
                    or type(stored.get("total_ws")) is not int
                    or not 0 <= stored["total_ws"] <= 10**16
                ):
                    raise InvalidData("HA 保存的累计电量无效")
                validate_sample(stored.get("sample"))
            self.data = stored
            self.loaded = True
        raw = sample["committed_wh"] * 3600 + sample["pending_ws"]
        if self.data is None:
            total = raw
        else:
            previous = self.data["sample"]
            if sample["committed_wh"] < previous["committed_wh"]:
                # Factory reset/explicit native counter reset. Continue HA's
                # observed total while adding the new counter's consumption.
                delta = raw
            elif sample["epoch"] != previous["epoch"]:
                # The old process's pending fraction was already observed by
                # HA. A new process starts another pending interval from zero.
                delta = (
                    max(
                        0,
                        (sample["committed_wh"] - previous["committed_wh"]) * 3600
                        - previous["pending_ws"],
                    )
                    + sample["pending_ws"]
                )
            else:
                old = previous["committed_wh"] * 3600 + previous["pending_ws"]
                # Native commits truncate to integer Wh; process restarts may
                # also discard the not-yet-persisted fraction. Neither is new
                # consumption and neither should look like a meter rollover.
                delta = max(0, raw - old)
            total = self.data["total_ws"] + delta
        changed = self.data != {"total_ws": total, "sample": sample}
        first = self.data is None
        self.data = {"total_ws": total, "sample": dict(sample)}
        if first:
            await self.store.async_save(self.data)
        elif changed:
            # Shorter than the minimum poll interval: repeated samples must
            # not postpone a debounced Store write indefinitely.
            self.store.async_delay_save(lambda: self.data, 5)
        return total / 3_600_000

    async def shutdown(self):
        if self.loaded and self.data is not None:
            await self.store.async_save(self.data)
