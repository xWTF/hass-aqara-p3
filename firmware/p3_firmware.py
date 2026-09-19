# SPDX-License-Identifier: GPL-3.0-only
"""Read-only collection, offline validation and staging for the P3 image pair.

This program never invokes fw_update or reboot. Only the generated apply.sh can
write flash, after the operator has reviewed the plan and explicitly runs it.
"""

import argparse
import asyncio
import base64
import getpass
import hashlib
import json
import re
import struct
import sys
import uuid
from array import array
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = HERE / "4.0.4-user-init"
STAGE = "/data/aqara-p3-firmware-install"
BACKUP = "/data/aqara-p3-firmware-backup"
PARTITIONS = [
    ("bootloader", 0xA0000),
    ("boot_info", 0x40000),
    ("factory", 0x40000),
    ("bbt", 0xE0000),
    ("linux_1", 0x300000),
    ("rootfs_1", 0x1000000),
    ("linux_2", 0x300000),
    ("rootfs_2", 0x1000000),
    ("data", 0x4B20000),
]
PROMPT = re.compile(r"(?:^|\n)(?:[^\n]{0,100} )?[#$] $")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checksum(data):
    words = array("H", data + (b"\0" if len(data) % 2 else b""))
    if sys.byteorder == "little":
        words.byteswap()
    value = sum(words) & 0xFFFFFFFF
    while value >> 16:
        value = (value & 65535) + (value >> 16)
    return (~value) & 65535


def boot_info(data):
    require(len(data) == 0x40000, "Wrong boot_info size")
    require(data[:4] == b"\x7c\x91\0\0", "Unsupported boot_info format")
    require(
        int.from_bytes(data[4:6], "big") == checksum(data[6:55]),
        "Invalid boot_info checksum",
    )
    require(all(n in (0, 1) for n in data[6:10]), "Invalid bank")
    require(
        data[6:8] == data[8:10],
        "Pending bank change; resolve it before preparing installation",
    )
    return data[6], data[7]


def updated_info(data, kind, bank, payload):
    result = bytearray(data)
    result[8 if kind == "kernel" else 9] = bank
    offset = (10 if kind == "kernel" else 24) + 7 * bank
    struct.pack_into(">IHB", result, offset, len(payload), checksum(payload), 0)
    result[4:6] = struct.pack(">H", checksum(result[6:55]))
    return bytes(result)


def load_bundle(bundle=BUNDLE):
    manifest = json.loads((bundle / "manifest.json").read_text())
    require(manifest["schema"] == 1, "Unsupported bundle schema")
    files = {}
    for name in ("kernel.bin", "rootfs.bin", "post_init.sh"):
        value = (bundle / name).read_bytes()
        record = manifest["files"][name]
        require(
            len(value) == record["size"] and sha(value) == record["sha256"],
            f"Bundle checksum: {name}",
        )
        if name.endswith(".bin"):
            sign, _, _, length = struct.unpack(">4sIII", value[:16])
            expected = b"cr6c" if name == "kernel.bin" else b"r6cr"
            require(
                sign == expected and length == len(value) - 16,
                "Invalid image container",
            )
            require(
                length == record["payload_size"]
                and sha(value[16:]) == record["payload_sha256"],
                "Payload checksum mismatch",
            )
            require(length % 2 == 0, "Odd firmware payload")
            words = array("H", value[16:])
            if sys.byteorder == "little":
                words.byteswap()
            require(sum(words) & 65535 == 0, "Invalid vendor trailer")
            capacity = 0x300000 if name == "kernel.bin" else 0x1000000
            require(length <= capacity - 0x20000, "Insufficient image partition margin")
        files[name] = value
    return manifest, files


def validate_snapshot(snapshot, dumps, manifest):
    require(snapshot["model"] == manifest["model"], "Unsupported model")
    require(
        re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", snapshot["mac"]) is not None,
        "Invalid MAC",
    )
    require(
        re.fullmatch(r"[0-9a-f-]{36}", snapshot["boot_id"]) is not None,
        "Invalid boot ID",
    )
    cpu = snapshot["cpuinfo"]
    require(
        re.search(r"^system type\s*:\s*RTL8197F\s*$", cpu, re.MULTILINE) is not None,
        "Unsupported SoC",
    )
    require(
        re.search(r"^cpu model\s*:\s*MIPS 24Kc\b", cpu, re.MULTILINE) is not None
        and "mips32r2" in cpu,
        "Unsupported CPU",
    )
    rows = re.findall(
        r'^mtd(\d+):\s+([0-9a-f]+)\s+([0-9a-f]+)\s+"([^"]+)"$',
        snapshot["mtd"],
        re.MULTILINE,
    )
    layout = [
        (int(i), int(size, 16), int(erase, 16), name) for i, size, erase, name in rows
    ]
    require(
        layout
        == [(i, size, 0x20000, name) for i, (name, size) in enumerate(PARTITIONS)],
        "Unsupported flash layout",
    )
    require(
        any(
            snapshot["tools"] == profile["tools"]
            for profile in manifest["tool_profiles"]
        ),
        "Unverified updater/flash tools; do not bypass this check",
    )
    require(
        snapshot["script_state"] == "absent",
        "Existing user startup script requires a separate reviewed plan",
    )
    for i in range(8):
        data = dumps[i]
        require(len(data) == PARTITIONS[i][1], f"Truncated mtd{i} backup")
        require(
            sha(data) == snapshot["partition_sha256"][str(i)],
            f"mtd{i} backup checksum mismatch",
        )
    require(
        sha(dumps[0]) == manifest["required_bootloader_sha256"], "Unverified bootloader"
    )
    kernel, root = boot_info(dumps[1])
    require(
        snapshot["cmdline"] == f"root=/dev/mtdblock{5 + root * 2} console=ttyS0,38400",
        "Running root and boot_info disagree",
    )
    for kind, bank, index, offset in (
        ("kernel", kernel, 4 + 2 * kernel, 10),
        ("rootfs", root, 5 + 2 * root, 24),
    ):
        size, expected, fails = struct.unpack_from(">IHB", dumps[1], offset + 7 * bank)
        require(
            0 < size <= len(dumps[index]) and fails == 0,
            f"Invalid active {kind} metadata",
        )
        require(
            checksum(dumps[index][:size]) == expected,
            f"Active {kind} disagrees with boot_info",
        )
    require(dumps[5 + 2 * root].startswith(b"hsqs"), "Active rootfs is not SquashFS")
    return kernel, root


def prepare(backup, output, bundle=BUNDLE):
    manifest, images = load_bundle(bundle)
    snapshot = json.loads((backup / "snapshot.json").read_text())
    dumps = {i: (backup / f"mtd{i}.bin").read_bytes() for i in range(8)}
    kernel, root = validate_snapshot(snapshot, dumps, manifest)
    kernel_target, root_target = 4 + 2 * (1 - kernel), 5 + 2 * (1 - root)
    after_kernel = updated_info(
        dumps[1], "kernel", 1 - kernel, images["kernel.bin"][16:]
    )
    after_root = updated_info(
        after_kernel, "rootfs", 1 - root, images["rootfs.bin"][16:]
    )
    values = {
        "MAC": snapshot["mac"],
        "BOOT_ID": snapshot["boot_id"],
        "CMDLINE": snapshot["cmdline"],
        "MTD_SHA": sha((snapshot["mtd"] + "\n").encode()),
        "BOOT_ORIGINAL": sha(dumps[1]),
        "BOOT_KERNEL": sha(after_kernel),
        "BOOT_FINAL": sha(after_root),
        "BOOTLOADER": manifest["required_bootloader_sha256"],
        "FACTORY": sha(dumps[2]),
        "KERNEL_ACTIVE": str(4 + 2 * kernel),
        "ROOT_ACTIVE": str(5 + 2 * root),
        "KERNEL_ACTIVE_SHA": sha(dumps[4 + 2 * kernel]),
        "ROOT_ACTIVE_SHA": sha(dumps[5 + 2 * root]),
        "KERNEL_TARGET": str(kernel_target),
        "ROOT_TARGET": str(root_target),
        "TOOLS_CHECKS": "\n".join(
            f"verify {path} {digest}" for path, digest in snapshot["tools"].items()
        ),
    }
    for name in ("kernel", "rootfs"):
        record = manifest["files"][name + ".bin"]
        for suffix, field in (
            ("IMAGE", "sha256"),
            ("PAYLOAD", "payload_sha256"),
            ("SIZE", "payload_size"),
        ):
            values[name.upper() + "_" + suffix] = str(record[field])
    values["POST_INIT"] = manifest["files"]["post_init.sh"]["sha256"]
    script = (HERE / "apply.sh.in").read_text()
    for key, value in values.items():
        script = script.replace("@" + key + "@", value)
    require(re.search(r"@[A-Z_]+@", script) is None, "Unresolved installation template")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = {
        **images,
        "apply.sh": script.encode(),
        "boot-info.after-kernel.bin": after_kernel,
        "boot-info.after-root.bin": after_root,
    }
    for name, data in files.items():
        (output / name).write_bytes(data)
    plan = {
        "schema": 1,
        "model": snapshot["model"],
        "mac": snapshot["mac"],
        "boot_id": snapshot["boot_id"],
        "kernel_target": kernel_target,
        "rootfs_target": root_target,
        "preserved_kernel": 4 + 2 * kernel,
        "preserved_rootfs": 5 + 2 * root,
        "stage": STAGE,
        "device_backup": BACKUP,
        "flash_authorized": False,
        "files": {
            name: {"size": len(data), "sha256": sha(data)}
            for name, data in files.items()
        },
    }
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


class Session:
    """One bounded Telnet shell; commands are generated from fixed operations."""

    def __init__(self, host, password):
        self.host, self.password = host, password
        self.reader = self.writer = None
        self.pending = ""

    async def until(self, pattern, limit=2_000_000):
        text, self.pending = self.pending, ""
        while True:
            match = pattern.search(text)
            if match:
                self.pending = text[match.end() :]
                return text[: match.end()]
            require(len(text) <= limit, "Response too long")
            chunk = await self.reader.read(8192)
            require(
                bool(chunk), "Telnet disconnected; do not retry a pending installation"
            )
            text += chunk.replace("\r", "")

    async def line(self, command):
        self.writer.write(command + "\r\n")
        await self.writer.drain()

    async def connect(self):
        import telnetlib3

        async with asyncio.timeout(15):
            self.reader, self.writer = await telnetlib3.open_connection(
                self.host,
                23,
                encoding="utf8",
                connect_minwait=0,
                connect_maxwait=0.2,
                send_environ=[],
                term="dumb",
                cols=240,
            )
            await self.until(re.compile(r"login:\s*$", re.IGNORECASE), 8192)
            await self.line("admin")
            response = await self.until(
                re.compile(r"(?i)password:\s*$|(?:^|\n)(?:[^\n]{0,100} )?[#$] $"), 8192
            )
            if re.search(r"password:\s*$", response, re.IGNORECASE):
                await self.line(self.password)
                response = await self.until(
                    re.compile(
                        r"(?i)login incorrect|login:\s*$|(?:^|\n)(?:[^\n]{0,100} )?[#$] $"
                    ),
                    8192,
                )
            require(PROMPT.search(response) is not None, "Telnet authentication failed")
            await self.line("busybox stty -echo")
            await self.until(PROMPT, 8192)

    async def run(self, command, timeout=45):
        marker = "P3_" + uuid.uuid4().hex
        async with asyncio.timeout(timeout):
            for line in command.splitlines():
                await self.line(line)
            await self.line("printf '\\n" + marker + ':%s\\n\' "$?"')
            response = await self.until(
                re.compile(r"(?:^|\n)" + marker + r":([0-9]+)\n")
            )
            await self.until(PROMPT, 8192)
        match = re.search(marker + r":([0-9]+)\n", response)
        require(
            match[1] == "0", "Device operation failed; inspect state before retrying"
        )
        # An interactive shell prints its prompt between the command and the
        # separate trailer, even with terminal echo disabled.
        body = response[: match.start()].rstrip("\n")
        return PROMPT.sub("", body).strip()

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                async with asyncio.timeout(2):
                    await self.writer.wait_closed()
            except (TimeoutError, OSError):
                pass


async def collect(session, output):
    manifest, _ = load_bundle()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    commands = {
        "model": "getprop persist.sys.model",
        "mac": "cat /sys/class/net/wlan0/address",
        "boot_id": "cat /proc/sys/kernel/random/boot_id",
        "cpuinfo": "cat /proc/cpuinfo",
        "mtd": "cat /proc/mtd",
        "cmdline": "cat /proc/cmdline",
        "script_state": "if [ -e /data/scripts/post_init.sh ] || [ -L /data/scripts/post_init.sh ]; then echo present; else echo absent; fi",
    }
    snapshot = {key: await session.run(command) for key, command in commands.items()}
    require(snapshot["model"] == manifest["model"], "Unsupported model")
    snapshot["mac"] = snapshot["mac"].lower()
    snapshot["tools"] = {}
    for path in manifest["required_tools"]:
        snapshot["tools"][path] = (await session.run("sha256sum " + path)).split()[0]
    snapshot["partition_sha256"] = {}
    for index, (_, size) in enumerate(PARTITIONS[:8]):
        digest = (await session.run(f"sha256sum /dev/mtdblock{index}")).split()[0]
        snapshot["partition_sha256"][str(index)] = digest
        with (output / f"mtd{index}.bin").open("xb") as dest:
            for position in range(0, size, 262144):
                count = min(262144, size - position) // 65536
                text = await session.run(
                    f"dd if=/dev/mtdblock{index} bs=65536 skip={position // 65536} count={count} 2>/dev/null | busybox base64"
                )
                block = base64.b64decode("".join(text.split()), validate=True)
                require(len(block) == count * 65536, f"Short mtd{index} read")
                dest.write(block)
        require(
            sha((output / f"mtd{index}.bin").read_bytes()) == digest,
            f"mtd{index} changed during collection",
        )
        print(f"mtd{index}: backup verified", flush=True)
    require(
        await session.run(commands["boot_id"]) == snapshot["boot_id"],
        "Device rebooted during collection",
    )
    require(
        (await session.run("sha256sum /dev/mtdblock1")).split()[0]
        == snapshot["partition_sha256"]["1"],
        "boot_info changed during collection",
    )
    (output / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    validate_snapshot(
        snapshot, {i: (output / f"mtd{i}.bin").read_bytes() for i in range(8)}, manifest
    )
    print(
        "Compatibility checks passed. Backups are private; keep this directory out of Git."
    )


async def upload(session, directory):
    plan = json.loads((directory / "plan.json").read_text())
    require(plan["schema"] == 1 and plan["stage"] == STAGE, "Invalid installation plan")
    require(
        await session.run("cat /sys/class/net/wlan0/address") == plan["mac"],
        "Wrong device",
    )
    require(
        await session.run("cat /proc/sys/kernel/random/boot_id") == plan["boot_id"],
        "Device rebooted; collect a fresh snapshot",
    )
    names = ("kernel.bin", "rootfs.bin", "post_init.sh", "apply.sh")
    payloads = {}
    for name in names:
        data = (directory / name).read_bytes()
        require(
            len(data) == plan["files"][name]["size"]
            and sha(data) == plan["files"][name]["sha256"],
            f"Changed staged file: {name}",
        )
        payloads[name] = data
    free = await session.run("df -k /data | tail -n 1")
    require(
        int(free.split()[-3]) * 1024
        >= sum(map(len, payloads.values())) + 2 * 1024 * 1024,
        "Insufficient /data space",
    )
    await session.run(
        f"test ! -e {STAGE} && test ! -L {STAGE} && umask 077 && mkdir {STAGE}"
    )
    for name, data in payloads.items():
        remote = STAGE + "/" + name
        for position in range(0, len(data), 131072):
            encoded = base64.b64encode(data[position : position + 131072]).decode()
            body = "\n".join(encoded[i : i + 768] for i in range(0, len(encoded), 768))
            await session.run(
                f"busybox base64 -d <<'P3_UPLOAD_EOF' >> {remote}\n{body}\nP3_UPLOAD_EOF",
                timeout=60,
            )
        require(
            (await session.run("sha256sum " + remote)).split()[0] == sha(data),
            f"Upload checksum: {name}",
        )
        print(name + ": uploaded and verified", flush=True)
    await session.run(f"/bin/busybox sh -n {STAGE}/apply.sh")
    print("Files staged only. Review plan.json and the README before running apply.sh.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "upload"):
        sub = commands.add_parser(name)
        sub.add_argument("--host", required=True)
        sub.add_argument("--directory", type=Path, required=True)
    sub = commands.add_parser("prepare")
    sub.add_argument("--backup", type=Path, required=True)
    sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        plan = prepare(args.backup, args.output)
        print(
            f"Prepared kernel mtd{plan['kernel_target']}, rootfs mtd{plan['rootfs_target']}; flash has not been written."
        )
        return

    async def online():
        session = Session(
            args.host, getpass.getpass("Telnet admin password (blank if unset): ")
        )
        try:
            await session.connect()
            await (
                collect(session, args.directory)
                if args.command == "collect"
                else upload(session, args.directory)
            )
        finally:
            await session.close()

    asyncio.run(online())


if __name__ == "__main__":
    main()
