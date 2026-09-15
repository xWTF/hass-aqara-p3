# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER

from custom_components.aqara_p3 import config_flow
from custom_components.aqara_p3.protocol.errors import AuthenticationError, InvalidData


@pytest.fixture(autouse=True)
def no_device_setup(monkeypatch):
    monkeypatch.setattr(
        "custom_components.aqara_p3.async_setup_entry", AsyncMock(return_value=True)
    )


async def pair(hass, identity, monkeypatch):
    monkeypatch.setattr(config_flow, "validate_input", AsyncMock(return_value=identity))
    return await hass.config_entries.flow.async_init(
        "aqara_p3",
        context={"source": SOURCE_USER},
        data={"host": "127.0.0.1", "password": "private-password"},
    )


async def test_user_flow_and_duplicate(hass, identity, monkeypatch):
    form = await hass.config_entries.flow.async_init(
        "aqara_p3", context={"source": SOURCE_USER}
    )
    assert form["type"] == "form"
    assert {str(k) for k in form["data_schema"].schema} == {"host", "password"}
    hass.config_entries.flow.async_abort(form["flow_id"])
    result = await pair(hass, identity, monkeypatch)
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == identity.uid
    assert result["data"]["password"] == "private-password"
    second = await pair(hass, identity, monkeypatch)
    assert second["type"] == "abort"
    assert second["reason"] == "already_configured"


@pytest.mark.parametrize(
    "error,code",
    [
        (AuthenticationError("private"), "invalid_auth"),
        (TimeoutError(), "cannot_connect"),
        (InvalidData("bad"), "invalid_data"),
    ],
)
async def test_errors_stay_in_flow(hass, monkeypatch, error, code, caplog):
    monkeypatch.setattr(config_flow, "validate_input", AsyncMock(side_effect=error))
    result = await hass.config_entries.flow.async_init(
        "aqara_p3",
        context={"source": SOURCE_USER},
        data={"host": "127.0.0.1", "password": "private-password"},
    )
    assert result["errors"] == {"base": code}
    assert "private-password" not in caplog.text


async def test_reconfigure_preserves_identity_and_password(hass, identity, monkeypatch):
    result = await pair(hass, identity, monkeypatch)
    entry = result["result"]
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_init(
        "aqara_p3",
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        data={"host": "127.0.0.2"},
    )
    assert result["type"] == "abort"
    assert entry.data["host"] == "127.0.0.2"
    assert entry.data["password"] == "private-password"
    assert len(hass.config_entries.async_entries("aqara_p3")) == 1


async def test_reauth_updates_password(hass, identity, monkeypatch):
    result = await pair(hass, identity, monkeypatch)
    entry = result["result"]
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_init(
        "aqara_p3",
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=dict(entry.data),
    )
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "password": "new-password"}
    )
    assert result["type"] == "abort"
    assert entry.data["password"] == "new-password"


async def test_reconfigure_rejects_different_p3(hass, identity, monkeypatch):
    from custom_components.aqara_p3.protocol.models import DeviceIdentity

    result = await pair(hass, identity, monkeypatch)
    entry = result["result"]
    other = DeviceIdentity("02:00:00:00:00:02", identity.model, identity.firmware)
    monkeypatch.setattr(config_flow, "validate_input", AsyncMock(return_value=other))
    result = await hass.config_entries.flow.async_init(
        "aqara_p3",
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        data={"host": "127.0.0.2"},
    )
    assert result["reason"] == "wrong_device"
    assert entry.data["host"] == "127.0.0.1"


async def test_options_interval(hass, identity, monkeypatch):
    entry = (await pair(hass, identity, monkeypatch))["result"]
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] == "menu"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "settings"}
    )
    assert flow["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"poll_interval": 30}
    )
    assert result["type"] == "create_entry"
    assert entry.options["poll_interval"] == 30


async def test_validation_cleans_up_on_timeout(monkeypatch):
    device = AsyncMock()

    async def hang():
        await asyncio.Event().wait()

    device.snapshot.side_effect = hang
    monkeypatch.setattr(config_flow, "ReadOnlyDevice", lambda _: device)
    monkeypatch.setattr(config_flow, "UPDATE_TIMEOUT", 0.01)
    with pytest.raises(TimeoutError):
        await config_flow.validate_input({"host": "127.0.0.1"})
    device.close.assert_awaited_once()
