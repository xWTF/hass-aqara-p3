# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.aqara_p3 import async_reload_entry
from custom_components.aqara_p3.local_mode import LocalModeController
from custom_components.aqara_p3.protocol.config import DeviceConfig
from custom_components.aqara_p3.protocol.control import LocalModeError, P3Control
from custom_components.aqara_p3.protocol.errors import InvalidData

CLOUD = {
    "mode": "cloud",
    "backup": "absent",
    "cloud_running": True,
    "monitor_running": True,
}
LOCAL = {
    "mode": "local",
    "backup": "ready",
    "cloud_running": False,
    "monitor_running": True,
}


@pytest.fixture
async def mode(hass, config_entry, monkeypatch):
    monkeypatch.setattr(
        "custom_components.aqara_p3.async_setup_entry", AsyncMock(return_value=True)
    )
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    await hass.config_entries.async_add(config_entry)
    await hass.async_block_till_done()
    parent = SimpleNamespace(hass=hass, control=AsyncMock())
    controller = LocalModeController(parent, config_entry)
    parent.control.local_mode.return_value = LOCAL
    yield controller
    await controller.shutdown()


async def test_default_never_touches_cloud(mode):
    mode.start()
    assert mode._task is None
    mode.parent.control.local_mode.assert_not_awaited()


async def test_maintenance_callback_runs_on_event_loop(mode, hass):
    from homeassistant.core import is_callback

    assert is_callback(mode._schedule)
    hass.config_entries.async_update_entry(mode.entry, options={"local_mode": True})
    mode.start()
    await mode._task
    mode.parent.control.local_mode.assert_awaited_once_with("enable")


async def test_reconcile_failure_bounded_and_recoverable(mode, hass):
    hass.config_entries.async_update_entry(mode.entry, options={"local_mode": True})
    mode.parent.control.local_mode.side_effect = LocalModeError("backup_conflict")
    await mode._reconcile()
    assert mode.last_error == "backup_conflict" and mode.state is None
    mode.parent.control.local_mode.side_effect = None
    await mode._reconcile()
    assert mode.state == LOCAL and mode.last_error is None


async def test_success_persists_choice_and_preserves_options(mode, hass):
    hass.config_entries.async_update_entry(
        mode.entry, options={"profile": "capture_only"}
    )
    await mode.start_change(True)
    assert mode.entry.options == {"profile": "capture_only", "local_mode": True}
    mode.parent.control.local_mode.assert_awaited_once_with("enable")


async def test_failure_preserves_choice(mode, hass):
    hass.config_entries.async_update_entry(mode.entry, options={"local_mode": False})
    mode.parent.control.local_mode.side_effect = LocalModeError("backup_conflict")
    with pytest.raises(LocalModeError):
        await mode.start_change(True)
    assert mode.entry.options["local_mode"] is False


async def test_explicit_choice_serializes_with_maintenance(mode, hass):
    hass.config_entries.async_update_entry(mode.entry, options={"local_mode": False})
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(action):
        if action == "enable" and not entered.is_set():
            entered.set()
            await release.wait()
        return LOCAL

    mode.parent.control.local_mode.side_effect = command
    change = mode.start_change(True)
    await entered.wait()
    maintenance = asyncio.create_task(mode._reconcile())
    release.set()
    await asyncio.gather(change, maintenance)
    assert [c.args[0] for c in mode.parent.control.local_mode.call_args_list] == [
        "enable",
        "enable",
    ]


@pytest.mark.parametrize("close_dialog", [False, True])
async def test_options_change_survives_dialog_close(
    hass, config_entry, monkeypatch, close_dialog
):
    monkeypatch.setattr(
        "custom_components.aqara_p3.async_setup_entry", AsyncMock(return_value=True)
    )
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    await hass.config_entries.async_add(config_entry)
    await hass.async_block_till_done()
    parent = SimpleNamespace(hass=hass, control=AsyncMock())
    controller = LocalModeController(parent, config_entry)
    config_entry.runtime_data = SimpleNamespace(
        local_mode=controller,
        reload_data=dict(config_entry.data),
        reload_options=dict(config_entry.options),
    )
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    unsubscribe = config_entry.add_update_listener(async_reload_entry)
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(action):
        if action == "status":
            return CLOUD
        entered.set()
        await release.wait()
        return LOCAL

    parent.control.local_mode.side_effect = command
    try:
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        fid = result["flow_id"]
        result = await hass.config_entries.options.async_configure(
            fid, {"next_step_id": "local_mode"}
        )
        assert result["type"] == "form"
        result = await hass.config_entries.options.async_configure(
            fid, {"mode": "local"}
        )
        assert result["type"] == "progress"
        await entered.wait()
        if close_dialog:
            hass.config_entries.options.async_abort(fid)
            await asyncio.sleep(0)
            assert not controller._change.cancelled()
        release.set()
        await controller._change
        await hass.async_block_till_done()
        assert config_entry.options["local_mode"] is True
        reload.assert_not_awaited()
        assert controller._unsubscribe is not None
        assert controller._task is None
        if not close_dialog:
            result = await hass.config_entries.options.async_configure(fid)
            assert result["type"] == "create_entry"
            assert result["data"] is None
    finally:
        unsubscribe()
        await controller.shutdown()


@pytest.mark.parametrize("update", ["mode", "poll_interval", "profile", "password"])
async def test_entry_updates_reload_only_settings_that_need_it(
    mode, hass, monkeypatch, update
):
    mode.entry.runtime_data = SimpleNamespace(
        local_mode=mode,
        reload_data=dict(mode.entry.data),
        reload_options=dict(mode.entry.options),
    )
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    if update == "password":
        hass.config_entries.async_update_entry(
            mode.entry, data={**mode.entry.data, "password": "updated-test-password"}
        )
    else:
        key, value = {
            "mode": ("local_mode", True),
            "poll_interval": ("poll_interval", 30),
            "profile": ("profile", "capture_only"),
        }[update]
        hass.config_entries.async_update_entry(mode.entry, options={key: value})
    await async_reload_entry(hass, mode.entry)
    if update == "mode":
        reload.assert_not_awaited()
        unsubscribe = mode._unsubscribe
        await async_reload_entry(hass, mode.entry)
        assert mode._unsubscribe is unsubscribe
    else:
        reload.assert_awaited_once_with(mode.entry.entry_id)


@pytest.mark.parametrize(
    "reply", [LOCAL, {**LOCAL, "cloud_running": True}, "backup_conflict"]
)
async def test_control_checks_device_script_and_postconditions(reply):
    control = P3Control(DeviceConfig("127.0.0.1"), "test")
    control._prepare = AsyncMock()
    script = (
        Path(__file__).resolve().parents[1]
        / "custom_components/aqara_p3/native/local_mode.sh"
    )
    digest = hashlib.sha256(script.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    def run(cmd, **kwargs):
        if cmd.startswith("busybox sha256sum"):
            return digest + "  /tmp/script.sh"
        if isinstance(reply, str):
            return "P3_LOCAL_ERROR " + reply
        return "P3_LOCAL_STATE " + json.dumps(reply)

    control.session.run = AsyncMock(side_effect=run)
    control.session.close = AsyncMock()
    if reply == LOCAL:
        assert await control.local_mode("enable") == LOCAL
    else:
        with pytest.raises(LocalModeError):
            await control.local_mode("enable")
    control._prepare.assert_awaited_once()
    control.session.close.assert_awaited_once()
    assert control.session.run.await_count == 2
    with pytest.raises(InvalidData):
        await control.local_mode("enable; command")


@pytest.fixture
def backup_shell(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "custom_components/aqara_p3/native/local_mode.sh"
    )
    functions = script.read_text().split("# Entry point")[0]
    # BusyBox uses these same coreutils operations; no device paths are accessed.
    shim = tmp_path / "busybox"
    shim.write_text('#!/bin/sh\nexec "$@"\n')
    shim.chmod(0o755)
    target = tmp_path / "original.sh"
    target.write_bytes(b"#!/bin/sh\nfactory monitor\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    data = tmp_path / "data"
    base = data / "local-mode-v1"
    backup = base / "app_monitor.original.sh"
    setup = f"\nDATA_DIR={shlex.quote(str(data))}\nBASE={shlex.quote(str(base))}\nBACKUP={shlex.quote(str(backup))}\nTARGET={shlex.quote(str(target))}\nORIGINAL_SHA={digest}\n"

    def run():
        return subprocess.run(
            ["sh", "-c", functions + setup + "save_backup"],
            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
            text=True,
            capture_output=True,
            check=False,
        )

    return SimpleNamespace(run=run, backup=backup, target=target)


def test_backup_created_once_and_never_replaced(backup_shell):
    b = backup_shell
    assert b.run().returncode == 0
    original = b.backup.read_bytes()
    inode = b.backup.stat().st_ino
    b.target.write_bytes(b"modified runtime overlay")
    assert b.run().returncode == 0
    assert b.backup.read_bytes() == original
    assert b.backup.stat().st_ino == inode
    assert b.backup.stat().st_mode & 0o222 == 0


def test_corrupt_backup_is_preserved_and_rejected(backup_shell):
    b = backup_shell
    assert b.run().returncode == 0
    b.backup.chmod(0o600)
    b.backup.write_bytes(b"unexpected existing backup")
    result = b.run()
    assert result.returncode != 0 and "backup_conflict" in result.stdout
    assert b.backup.read_bytes() == b"unexpected existing backup"


def test_symlink_backup_is_rejected(backup_shell):
    b = backup_shell
    b.backup.parent.mkdir(parents=True)
    b.backup.symlink_to(b.target)
    original = b.target.read_bytes()
    assert "backup_conflict" in b.run().stdout
    assert b.backup.is_symlink() and b.target.read_bytes() == original


def test_cloud_detection_uses_process_identity_not_launch_arguments(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "custom_components/aqara_p3/native/local_mode.sh"
    )
    functions = script.read_text().split("# Entry point")[0]
    shim = tmp_path / "busybox"
    shim.write_text('#!/bin/sh\nexec "$@"\n')
    shim.chmod(0o755)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import ctypes,time; ctypes.CDLL(None).prctl(15,b'miio_client',0,0,0); print('ready',flush=True); time.sleep(20)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        setup = (
            "\nCLOUD_EXE="
            + shlex.quote(str(Path(sys.executable).resolve()))
            + "\ncloud_pids\n"
        )
        result = subprocess.run(
            ["sh", "-c", functions + setup],
            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
            text=True,
            capture_output=True,
            check=True,
        )
        assert result.stdout.split() == [str(child.pid)]
    finally:
        child.terminate()
        child.wait(timeout=5)
