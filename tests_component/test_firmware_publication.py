# SPDX-License-Identifier: GPL-3.0-only
"""Public image integrity and refusal rules, using synthetic private snapshots."""

import copy
import json
import struct
from unittest.mock import AsyncMock

import pytest

from firmware import p3_firmware as fw


@pytest.fixture(scope="module")
def source():
    manifest, _ = fw.load_bundle()
    data = {i: bytes(size) for i, (_, size) in enumerate(fw.PARTITIONS[:8])}
    for i in (5, 7):
        data[i] = b"hsqs" + data[i][4:]
    info = bytearray(data[1])
    info[:4] = b"\x7c\x91\0\0"
    info[6:10] = bytes((1, 1, 1, 1))
    for bank in (0, 1):
        for offset, index in ((10, 4), (24, 5)):
            struct.pack_into(
                ">IHB",
                info,
                offset + bank * 7,
                32,
                fw.checksum(data[index + 2 * bank][:32]),
                0,
            )
    info[4:6] = struct.pack(">H", fw.checksum(info[6:55]))
    data[1] = bytes(info)
    manifest["required_bootloader_sha256"] = fw.sha(data[0])
    snapshot = {
        "model": manifest["model"],
        "mac": "02:00:00:00:00:01",
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "cpuinfo": "system type : RTL8197F\ncpu model : MIPS 24Kc V8.5\nisa : mips32r2",
        "mtd": "dev:    size   erasesize  name\n"
        + "\n".join(
            f'mtd{i}: {size:08x} 00020000 "{name}"'
            for i, (name, size) in enumerate(fw.PARTITIONS)
        ),
        "cmdline": "root=/dev/mtdblock7 console=ttyS0,38400",
        "tools": manifest["required_tools"],
        "script_state": "absent",
        "partition_sha256": {str(i): fw.sha(value) for i, value in data.items()},
    }
    return snapshot, data, manifest


def test_public_images_and_checksum_file_agree():
    manifest, images = fw.load_bundle()
    lines = (fw.BUNDLE / "SHA256SUMS").read_text().splitlines()
    for line in lines:
        digest, name = line.split()
        assert fw.sha((fw.BUNDLE / name).read_bytes()) == digest
    assert images["kernel.bin"][:4] == b"cr6c"
    assert images["rootfs.bin"][16:20] == b"hsqs"
    assert manifest["minimum_source_version"] is None


@pytest.mark.parametrize("profile", [0, 1])
def test_verified_tool_profiles_accept_without_version_string(source, profile):
    snapshot, dumps, manifest = source
    snapshot = copy.deepcopy(snapshot)
    snapshot["tools"] = manifest["tool_profiles"][profile]["tools"]
    assert fw.validate_snapshot(snapshot, dumps, manifest) == (1, 1)


@pytest.mark.parametrize(
    "fault",
    [
        "model",
        "cpu",
        "layout",
        "tools",
        "root",
        "script",
        "bootloader",
        "backup_hash",
        "truncated",
        "boot_sum",
        "pending",
        "active_sum",
    ],
)
def test_unknown_or_changed_device_state_is_rejected(source, fault):
    saved, original, manifest = source
    snapshot, dumps = copy.deepcopy(saved), dict(original)
    manifest = copy.deepcopy(manifest)
    if fault == "model":
        snapshot["model"] = "other.model"
    elif fault == "cpu":
        snapshot["cpuinfo"] = snapshot["cpuinfo"].replace("RTL8197F", "other")
    elif fault == "layout":
        snapshot["mtd"] = snapshot["mtd"].replace("01000000", "00800000")
    elif fault == "tools":
        snapshot["tools"]["/bin/fw_update"] = "0" * 64
    elif fault == "root":
        snapshot["cmdline"] = "root=/dev/mtdblock5 console=ttyS0,38400"
    elif fault == "script":
        snapshot["script_state"] = "present"
    elif fault == "bootloader":
        manifest["required_bootloader_sha256"] = "0" * 64
    elif fault == "backup_hash":
        snapshot["partition_sha256"]["2"] = "0" * 64
    elif fault == "truncated":
        dumps[7] = dumps[7][:-1]
    else:
        info = bytearray(dumps[1])
        if fault == "boot_sum":
            info[4] ^= 1
        elif fault == "pending":
            info[9] = 0
        elif fault == "active_sum":
            info[17 + 4] ^= 1
        if fault != "boot_sum":
            info[4:6] = struct.pack(">H", fw.checksum(info[6:55]))
        dumps[1] = bytes(info)
        snapshot["partition_sha256"]["1"] = fw.sha(dumps[1])
    with pytest.raises(ValueError):
        fw.validate_snapshot(snapshot, dumps, manifest)


def test_plan_never_overwrites_existing_output(source, tmp_path, monkeypatch):
    snapshot, dumps, manifest = source
    _original, images = fw.load_bundle()
    monkeypatch.setattr(fw, "load_bundle", lambda _: (manifest, images))
    backup, output = tmp_path / "backup", tmp_path / "install"
    backup.mkdir()
    (backup / "snapshot.json").write_text(json.dumps(snapshot))
    for i, data in dumps.items():
        (backup / f"mtd{i}.bin").write_bytes(data)
    plan = fw.prepare(backup, output)
    assert plan["kernel_target"] == 4 and plan["rootfs_target"] == 5
    assert not plan["flash_authorized"]
    original = (output / "apply.sh").read_bytes()
    with pytest.raises(FileExistsError):
        fw.prepare(backup, output)
    assert (output / "apply.sh").read_bytes() == original


async def test_upload_refuses_other_device_before_creating_stage(tmp_path):
    (tmp_path / "plan.json").write_text(
        json.dumps({"schema": 1, "stage": fw.STAGE, "mac": "02:00:00:00:00:01"})
    )
    session = AsyncMock()
    session.run.return_value = "02:00:00:00:00:02"
    with pytest.raises(ValueError, match="Wrong device"):
        await fw.upload(session, tmp_path)
    session.run.assert_awaited_once_with("cat /sys/class/net/wlan0/address")


async def test_cli_session_framing_and_credentials():
    import asyncio

    import telnetlib3

    seen = []
    finished = asyncio.Event()

    async def shell(reader, writer):
        try:
            writer.write("P3 login: ")
            assert (await reader.readline()).strip() == "admin"
            writer.write("Password: ")
            assert (await reader.readline()).strip() == "test-only-password"
            writer.write("\r\n# ")
            while line := await reader.readline():
                line = line.strip()
                seen.append(line)
                if line == "busybox stty -echo":
                    writer.write("\r\n# ")
                elif line.startswith("printf"):
                    marker = line.split("\\n")[1].split(":")[0]
                    writer.write(f"\r\n{marker}:0\r\n# ")
                else:
                    writer.write("sample\r\n# ")
        finally:
            writer.close()
            finished.set()

    server = await telnetlib3.create_server(
        host="127.0.0.1", port=0, shell=shell, connect_maxwait=0.1
    )
    session = fw.Session("127.0.0.1", "test-only-password")
    original_open = telnetlib3.open_connection

    async def open_test(host, port, **kwargs):
        return await original_open(host, server.sockets[0].getsockname()[1], **kwargs)

    from unittest.mock import patch

    try:
        with patch.object(telnetlib3, "open_connection", open_test):
            await session.connect()
            assert await session.run("getprop persist.sys.model") == "sample"
    finally:
        await session.close()
        server.close()
        await server.wait_closed()
        async with asyncio.timeout(2):
            await finished.wait()
    assert "test-only-password" not in seen
