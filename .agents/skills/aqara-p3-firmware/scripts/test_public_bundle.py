"""Exercise the public paired-image installer with the original MIPS updater.

All flash devices are ordinary files in an isolated chroot. Device-type tests,
PATH and QEMU invocation are the only installation-script substitutions.
"""

import argparse
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from test_runtime import QEMU, host_binary, invoke, make_runtime, script

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "firmware"))
import p3_firmware as public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-rootfs", type=Path)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=False)
    manifest, images = public.load_bundle()
    squash = args.work / "rootfs.squashfs"
    squash.write_bytes(
        args.legacy_rootfs.read_bytes()
        if args.legacy_rootfs
        else images["rootfs.bin"][16:-4]
    )
    original = args.work / "original"
    subprocess.run(
        [
            "unsquashfs",
            "-processors",
            "2",
            "-no-progress",
            "-d",
            str(original),
            str(squash),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    dumps = {}
    for i in range(8):
        path = next(args.backup.glob(f"mtd{i}-*.bin"))
        dumps[i] = path.read_bytes()
        public.require(
            public.sha(dumps[i]) == path.with_suffix(".sha256").read_text().split()[0],
            f"Backup mtd{i}",
        )
    public.require(
        public.sha(dumps[0]) == manifest["required_bootloader_sha256"],
        "Wrong bootloader",
    )
    results = []
    tools = {
        path: public.sha((original / path.lstrip("/")).read_bytes())
        for path in manifest["required_tools"]
    }
    cases = [(f"normal_k{k}_r{r}", k, r, "") for k in (0, 1) for r in (0, 1)]
    cases += [
        (fault, 1, 1, fault)
        for fault in (
            "kernel_write",
            "kernel_boot_save",
            "rootfs_write",
            "rootfs_boot_save",
            "corrupt_image",
            "backup_exists",
            "script_exists",
        )
    ]
    if args.legacy_rootfs:
        cases = [
            case
            for case in cases
            if case[3] in ("", "corrupt_image", "backup_exists", "script_exists")
        ]
    for name, kernel, rootbank, fault in cases:
        work = args.work / name
        work.mkdir()
        backup = work / "backup"
        backup.mkdir()
        source = dict(dumps)
        info = bytearray(source[1])
        info[6:10] = bytes((kernel, rootbank, kernel, rootbank))
        info[4:6] = struct.pack(">H", public.checksum(info[6:55]))
        source[1] = bytes(info)
        snapshot = {
            "model": manifest["model"],
            "mac": "02:00:00:00:00:01",
            "boot_id": "00000000-0000-0000-0000-000000000001",
            "cpuinfo": "system type : RTL8197F\ncpu model : MIPS 24Kc V8.5\nisa : mips1 mips2 mips32r2",
            "mtd": "dev:    size   erasesize  name\n"
            + "\n".join(
                f'mtd{i}: {size:08x} 00020000 "{n}"'
                for i, (n, size) in enumerate(public.PARTITIONS)
            ),
            "cmdline": f"root=/dev/mtdblock{5 + rootbank * 2} console=ttyS0,38400",
            "tools": tools,
            "script_state": "absent",
            "partition_sha256": {
                str(i): public.sha(value) for i, value in source.items()
            },
        }
        (backup / "snapshot.json").write_text(json.dumps(snapshot))
        for i, data in source.items():
            (backup / f"mtd{i}.bin").write_bytes(data)
        install = work / "install"
        plan = public.prepare(backup, install)
        runtime = make_runtime(work, original)
        for tool in (
            "sha256sum",
            "readlink",
            "grep",
            "df",
            "tail",
            "awk",
            "chmod",
            "ln",
            "sync",
        ):
            host_binary(runtime, shutil.which(tool), "/bin/" + tool)
        for tool in ("cat", "head", "mkdir"):
            shutil.copy2(runtime / "lab-host" / tool, runtime / "bin" / tool)
        for tool in ("fw_update", "busybox", "flash_erase", "nandwrite", "nanddump"):
            shutil.copy2(original / "bin" / tool, runtime / "bin" / tool)
        # df is only used to check staging headroom, not to inspect flash.
        script(
            runtime,
            "/lab-stubs/df",
            "echo 'Filesystem 1K-blocks Used Available Use% Mounted on'\necho 'test 100000 1000 99000 1% /data'",
        )
        script(runtime, "/lab-stubs/getprop", "echo lumi.aircondition.acn05")
        script(
            runtime,
            "/lab-stubs/flash_erase",
            """echo "erase $*" >> /lab/events
case "$1" in /dev/mtd4|/dev/mtd6) /lab-host/cp /lab/erased-kernel "$1";; /dev/mtd5|/dev/mtd7) /lab-host/cp /lab/erased-root "$1";; *) exit 90;; esac""",
        )
        script(
            runtime,
            "/lab-stubs/nandwrite",
            """echo "write $*" >> /lab/events
kind=kernel
case "$2" in /dev/mtd5|/dev/mtd7) kind=rootfs;; /dev/mtd4|/dev/mtd6) ;; *) exit 90;; esac
if [ "${FAULT:-}" = "${kind}_write" ]; then /lab-host/cat > /dev/null; exit 1; fi
/lab-host/dd of="$2" conv=notrunc status=none""",
        )
        script(
            runtime,
            "/lab-stubs/nanddump",
            '''echo "read $*" >> /lab/events
kind=kernel
case "$2" in /dev/mtd5|/dev/mtd7) kind=rootfs;; /dev/mtd4|/dev/mtd6) ;; *) exit 90;; esac
if [ "${FAULT:-}" = "${kind}_boot_save" ]; then /lab-host/rm /dev/mtdblock1; /lab-host/mkdir /dev/mtdblock1; fi
/lab-host/cat "$2"''',
        )
        (runtime / "lab/erased-kernel").write_bytes(b"\xff" * 0x300000)
        (runtime / "lab/erased-root").write_bytes(b"\xff" * 0x1000000)
        for i, data in source.items():
            path = runtime / f"dev/mtd{i}"
            path.write_bytes(data)
            (runtime / f"dev/mtdblock{i}").symlink_to(f"mtd{i}")
        for path, value in {
            "proc/mtd": snapshot["mtd"],
            "proc/cmdline": snapshot["cmdline"],
            "proc/cpuinfo": snapshot["cpuinfo"],
            "proc/sys/kernel/random/boot_id": snapshot["boot_id"],
            "sys/class/net/wlan0/address": snapshot["mac"],
        }.items():
            target = runtime / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value + "\n")
        shutil.copy2(original / "etc/init.d/rcS", runtime / "etc/init.d/rcS")
        stage = runtime / public.STAGE.lstrip("/")
        shutil.copytree(install, stage)
        device_backup = runtime / public.BACKUP.lstrip("/")
        if fault == "backup_exists":
            device_backup.mkdir()
            (device_backup / "keep").write_bytes(b"preserve this backup")
        if fault == "script_exists":
            (runtime / "data/scripts/post_init.sh").write_bytes(b"preserve this script")
        if fault == "corrupt_image":
            (stage / "rootfs.bin").write_bytes(b"corrupt")
        apply = (stage / "apply.sh").read_text()
        apply = apply.replace("PATH=/bin:", "PATH=/lab-stubs:/bin:")
        apply = apply.replace('[ -b "/dev/mtdblock$n" ]', '[ -f "/dev/mtdblock$n" ]')
        apply = apply.replace('[ -c "/dev/mtd$n" ]', '[ -f "/dev/mtd$n" ]')
        apply = apply.replace(
            '/bin/fw_update "$STAGE/', " ".join(QEMU) + ' /lab/fw_update "$STAGE/'
        )
        (stage / "apply-test.sh").write_text(apply)
        rc, text = invoke(
            runtime,
            QEMU
            + ["/lab/busybox", "sh", public.STAGE + "/apply-test.sh", "--confirmed"],
            {"FAULT": fault},
            timeout=90,
        )
        (work / "console.log").write_text(text)
        state_file = stage / "execution/state"
        state = (
            state_file.read_text().strip()
            if state_file.exists()
            else "PREFLIGHT_REJECTED"
        )
        public.require(
            all(
                (runtime / f"dev/mtd{i}").read_bytes() == source[i]
                for i in (0, 2, 3, plan["preserved_kernel"], plan["preserved_rootfs"])
            ),
            "Changed protected partition",
        )
        if not fault:
            public.require(
                rc == 0 and state == "VERIFIED_READY_TO_REBOOT", (name, rc, state, text)
            )
            public.require(
                (runtime / "dev/mtdblock1").read_bytes()
                == (install / "boot-info.after-root.bin").read_bytes(),
                "Final boot_info mismatch",
            )
            for kind, partition in (
                ("kernel", plan["kernel_target"]),
                ("rootfs", plan["rootfs_target"]),
            ):
                payload = images[kind + ".bin"][16:]
                public.require(
                    (runtime / f"dev/mtd{partition}").read_bytes()[: len(payload)]
                    == payload,
                    "Payload mismatch",
                )
        else:
            public.require(
                rc != 0 and state != "VERIFIED_READY_TO_REBOOT", (name, rc, state)
            )
            if fault in ("backup_exists", "script_exists", "corrupt_image"):
                public.require(
                    not (runtime / "lab/events").exists(),
                    "Flashed after preflight rejection",
                )
            if fault == "backup_exists":
                public.require(
                    (device_backup / "keep").read_bytes() == b"preserve this backup",
                    "Backup overwritten",
                )
            if fault == "script_exists":
                public.require(
                    (runtime / "data/scripts/post_init.sh").read_bytes()
                    == b"preserve this script",
                    "User script overwritten",
                )
            if fault.startswith("kernel_"):
                public.require(
                    (runtime / f"dev/mtd{plan['rootfs_target']}").read_bytes()
                    == source[plan["rootfs_target"]],
                    "Root written after kernel failure",
                )
        result = {
            "case": name,
            "exit_code": rc,
            "state": state,
            "protected_partitions_unchanged": True,
        }
        results.append(result)
        print(json.dumps(result), flush=True)
    args.output.write_text(
        json.dumps(
            {
                "status": "PASS",
                "cases": results,
                "images": manifest["files"],
                "scope": "Original MIPS fw_update and BusyBox; regular-file flash doubles, device-node and PATH instrumentation; no hardware NAND or Linux boot",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
