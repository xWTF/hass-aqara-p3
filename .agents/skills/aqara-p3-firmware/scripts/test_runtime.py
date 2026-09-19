"""Run vendor MIPS binaries against isolated, regular-file flash doubles.

This is NOT a NAND emulator. No network/device address is accepted. The chroot
has no real /dev, /proc, /sys or mounts from the host; all mtd paths are files.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import time
from pathlib import Path

QEMU = ["/qemu-mipsel-static", "-B", "0x100000000", "-R", "2147483648"]


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    n = sum(struct.unpack(">" + str(len(data) // 2) + "H", data)) & 0xFFFFFFFF
    while n >> 16:
        n = (n & 65535) + (n >> 16)
    return (~n) & 65535


def boot_info(data):
    if len(data) < 55 or data[:4] != b"\x7c\x91\0\0":
        raise ValueError("bad boot_info magic/version")
    if checksum(data[6:55]) != int.from_bytes(data[4:6], "big"):
        raise ValueError("bad boot_info checksum")
    result = {
        "current_kernel": data[6],
        "current_root": data[7],
        "preferred_kernel": data[8],
        "preferred_root": data[9],
        "root_sum_check": data[38],
    }
    result["roots"] = [
        dict(
            zip(["length", "sum", "fail"], struct.unpack_from(">IHB", data, 24 + i * 7))
        )
        for i in range(2)
    ]
    return result


def host_binary(root, source, dest):
    p = Path(source).resolve()
    target = root / dest.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, target)
    deps = subprocess.run(
        ["ldd", str(p)], capture_output=True, text=True, check=True
    ).stdout
    for lib in re.findall(r"(/[^\s()]+)", deps):
        t = root / lib.lstrip("/")
        t.parent.mkdir(parents=True, exist_ok=True)
        if not t.exists():
            shutil.copy2(Path(lib).resolve(), t)


def script(root, name, body):
    p = root / name.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/lab-host/sh\nset -eu\n" + body + "\n")
    p.chmod(0o755)


def invoke(root, args, env=None, timeout=25):
    proc = subprocess.Popen(
        ["/usr/sbin/chroot", str(root)] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={"PATH": "/lab-stubs:/bin", "LANG": "C", **(env or {})},
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output.decode(errors="replace")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def make_runtime(work, original):
    runtime = work / "runtime"
    runtime.mkdir()
    shutil.copytree(original / "lib", runtime / "lib", symlinks=True)
    for dirname in (
        "bin",
        "dev",
        "lab",
        "lab-stubs",
        "lab-host",
        "sys/class/tty/tty",
        "data/musics",
        "data/scripts",
        "tmp",
        "etc/init.d",
        "var",
        "proc",
    ):
        (runtime / dirname).mkdir(parents=True, exist_ok=True)
    shutil.copy2("/usr/bin/qemu-mipsel-static", runtime / "qemu-mipsel-static")
    for name in ("fw_update", "boot_ctrl", "busybox"):
        shutil.copy2(original / "bin" / name, runtime / "lab" / name)
    for name in ("dd", "cat", "cp", "mkdir", "rm", "head"):
        host_binary(runtime, shutil.which(name), "/lab-host/" + name)
    host_binary(runtime, "/bin/dash", "/lab-host/sh")
    (runtime / "bin/sh").symlink_to("/lab-host/sh")
    # A regular sink avoids exposing host device nodes, even /dev/null.
    (runtime / "dev/null").touch()
    return runtime


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("build", type=Path)
    ap.add_argument("backup", type=Path)
    args = ap.parse_args()
    report = json.loads((args.build / "build-report.json").read_text())
    original = Path(report["work"]) / "original"
    work = Path(report["work"]) / ("runtime-tests-" + time.strftime("%H%M%S"))
    work.mkdir()
    root = make_runtime(work, original)
    # Firmware libc's system()/popen() launch the host sh in this private root.
    # The updater, its checksum routines and boot-info logic remain unmodified.
    for tool, body in {
        "flash_erase": '''echo "erase $*" >> /lab/events
case "$1" in /dev/mtd5|/dev/mtd7) ;; *) exit 90;; esac
if [ "${FAULT:-}" = boot_save ]; then /lab-host/rm /dev/mtdblock1; /lab-host/mkdir /dev/mtdblock1; fi
if [ "${FAULT:-}" = erase ]; then exit 1; fi
/lab-host/cp /lab/erased "$1"''',
        "nandwrite": """echo "write $*" >> /lab/events
[ "$1" = -p ] && [ "$3" = - ]
case "$2" in /dev/mtd5|/dev/mtd7) ;; *) exit 90;; esac
if [ "${FAULT:-}" = write ]; then /lab-host/cat > /dev/null; exit 1; fi
/lab-host/dd of="$2" conv=notrunc status=none""",
        "nanddump": """echo "read $*" >> /lab/events
[ "$1" = -a ]
case "$2" in /dev/mtd5|/dev/mtd7) ;; *) exit 90;; esac
if [ "${FAULT:-}" = readback ]; then /lab-host/cat /lab/corrupt; else /lab-host/cat "$2"; fi""",
    }.items():
        script(root, "/lab-stubs/" + tool, body)
    erased = b"\xff" * (16 * 1024 * 1024)
    (root / "lab/erased").write_bytes(erased)
    (root / "lab/corrupt").write_bytes(b"\0" * len(erased))
    backup_file = args.backup / "mtd1-boot_info.bin"
    saved = backup_file.read_bytes()
    if (
        hashlib.sha256(saved).hexdigest()
        != backup_file.with_suffix(".sha256").read_text().split()[0]
    ):
        raise ValueError("Backup digest")
    before = boot_info(saved)
    if before["current_root"] != 1 or before["preferred_root"] != 1:
        raise ValueError("Unexpected source boot_info")
    image = (args.build / "candidate.OFFLINE-ONLY.bin").read_bytes()
    if hashlib.sha256(image).hexdigest() != report["wrapper_sha256"]:
        raise ValueError("Candidate digest")
    tests = []
    for case in (
        "normal",
        "bad_signature",
        "oversize",
        "truncated",
        "corrupt_source",
        "readback",
        "write",
        "erase",
        "boot_save",
        "missing_boot_info",
        "bad_boot_checksum",
    ):
        boot = root / "dev/mtdblock1"
        if boot.is_dir():
            boot.rmdir()
        boot.write_bytes(saved)
        (root / "dev/mtd5").write_bytes(erased)
        (root / "dev/mtd7").write_bytes(erased)
        (root / "lab/events").write_text("")
        data = image
        if case == "bad_signature":
            data = b"BAD!" + image[4:]
        if case == "oversize":
            data = image[:12] + struct.pack(">I", 0x1900001) + image[16:]
        if case == "truncated":
            data = image[: 16 + 1048576]
        if case == "corrupt_source":
            data = image[:16] + b"NOFS" + image[20:]
        if case == "missing_boot_info":
            boot.unlink()
        if case == "bad_boot_checksum":
            boot.write_bytes(saved[:4] + b"\0\0" + saved[6:])
        (root / "lab/image.bin").write_bytes(data)
        code, text = invoke(
            root, QEMU + ["/lab/fw_update", "/lab/image.bin"], {"FAULT": case}
        )
        events = (root / "lab/events").read_text()
        new = boot.read_bytes() if boot.is_file() else b""
        (work / (case + ".log")).write_text(text + "\nEVENTS\n" + events)
        entry = {
            "case": case,
            "exit_code": code,
            "success_message": "\nSuccess\n" in "\n" + text,
            "boot_info_changed": new != saved,
            "events": events.splitlines(),
        }
        if case == "normal":
            updated = boot_info(new)
            payload = image[16:]
            assert code == 0 and entry["success_message"], text
            assert updated["current_root"] == 1 and updated["preferred_root"] == 0, (
                updated
            )
            assert updated["roots"][0] == {
                "length": len(payload),
                "sum": checksum(payload),
                "fail": 0,
            }, updated
            assert (root / "dev/mtd5").read_bytes()[: len(payload)] == payload
            assert (root / "dev/mtd7").read_bytes() == erased
            entry["boot_info"] = updated
            (work / "normal-boot_info.bin").write_bytes(new)
            rc, view = invoke(root, QEMU + ["/lab/boot_ctrl", "show"])
            assert rc == 0
            (work / "normal-boot-ctrl.log").write_text(view)
        elif case in ("bad_signature", "oversize"):
            assert not events and new == saved and not entry["success_message"], entry
        elif case in ("truncated", "readback", "write"):
            assert (
                "erase /dev/mtd5 0 0" in events
                and new == saved
                and not entry["success_message"]
            ), entry
        elif case == "boot_save":
            assert (
                "Save boot_info fail" in text or "open /dev/mtdblock1 fail" in text
            ), text
            assert entry["success_message"], entry
        elif case in ("missing_boot_info", "bad_boot_checksum"):
            assert "erase /dev/mtd5 0 0" in events, entry
            entry["hazard"] = (
                "Missing boot_info resets bank selection to defaults; active slot is not inferred from cmdline."
            )
        elif case == "corrupt_source":
            assert entry["success_message"], entry
            entry["hazard"] = (
                "Readback CRC verifies transport only; a corrupt input image is accepted and selected."
            )
        elif case == "erase":
            entry["limitation"] = (
                "Regular-file NAND double cannot enforce real 1-to-0 program constraints."
            )
        tests.append(entry)
        print(case, entry["success_message"], "exit", code, flush=True)
    # Original MIPS BusyBox shell executes the exact candidate rcS. All external
    # hardware/application commands and telnetd creation are explicit doubles.
    (root / "etc/init.d/rcS").write_bytes((args.build / "rcS.candidate").read_bytes())
    (root / "sys/class/tty/tty/enable").touch()
    for name in (
        "mount",
        "mkdir",
        "ubifs_mount",
        "ifconfig",
        "iwpriv",
        "kick_wdog_timer.sh",
        "cp",
        "property_service",
        "fw_manager.sh",
    ):
        script(root, "/lab-stubs/" + name, f'echo "{name} $*" >> /lab/startup-events')
    # Preserve the original MIPS shell for both rcS and the user script.
    # The ELF updater tests above used host sh only for the Flash doubles.
    (root / "bin/sh").unlink()
    script(
        root,
        "/bin/sh",
        'exec /qemu-mipsel-static -B 0x100000000 -R 2147483648 /lab/busybox sh "$@"',
    )
    script(
        root,
        "/lab-stubs/pgrep",
        'if [ "${ALREADY_RUNNING:-0}" = 1 ]; then echo 123; fi',
    )
    script(
        root, "/bin/busybox", '[ "$1" = telnetd ]; echo telnetd >> /lab/startup-events'
    )
    hook = root / "data/scripts/post_init.sh"
    user_script = (args.build / "post_init.sh").read_bytes()
    assert hashlib.sha256(user_script).hexdigest() == report["user_script_sha256"]
    startup = []
    for name, running, expected in [
        ("user_script", 0, 1),
        ("existing_telnet", 1, 0),
        ("missing_script", 0, 0),
        ("not_executable", 0, 0),
        ("failing_script", 0, 0),
    ]:
        hook.write_bytes(user_script)
        hook.chmod(0o755)
        if name == "missing_script":
            hook.unlink()
        if name == "not_executable":
            hook.chmod(0o644)
        if name == "failing_script":
            hook.write_bytes(b"#!/bin/sh\nexit 37\n")
        (root / "lab/startup-events").write_text("")
        (root / "sys/class/tty/tty/enable").write_text("disable")
        rc, text = invoke(
            root,
            QEMU + ["/lab/busybox", "sh", "-c", ". /etc/init.d/rcS; wait"],
            {"ALREADY_RUNNING": str(running)},
        )
        events = (root / "lab/startup-events").read_text().splitlines()
        assert rc == 0 and events.count("telnetd") == expected, (name, rc, text, events)
        assert events.count("fw_manager.sh -r") == 1
        if expected:
            assert events.index("fw_manager.sh -r") < events.index("telnetd")
            assert (root / "sys/class/tty/tty/enable").read_text().strip() == "enable"
            assert (
                "Starting Aqara P3 user services"
                in (root / "tmp/post_init.log").read_text()
            )
        startup.append(
            {"case": name, "telnet_starts": events.count("telnetd"), "exit_code": rc}
        )
        (work / ("startup-" + name + ".log")).write_text(
            text + "\n" + "\n".join(events)
        )
    hook.write_bytes(b"#!/bin/sh\necho HOOK_STARTED\nwhile :; do :; done\n")
    hook.chmod(0o755)
    try:
        invoke(
            root,
            QEMU
            + [
                "/lab/busybox",
                "sh",
                "-c",
                ". /etc/init.d/rcS; echo MAIN_RETURNED; wait",
            ],
            timeout=2,
        )
        raise AssertionError("Hanging hook did not keep wait active")
    except subprocess.TimeoutExpired as exc:
        assert b"MAIN_RETURNED" in (exc.output or b"")
        assert "HOOK_STARTED" in (root / "tmp/post_init.log").read_text()
        startup.append(
            {
                "case": "hanging_script",
                "main_returned": True,
                "test_processes_terminated": True,
            }
        )
    result = {
        "status": "PASS_WITH_DOCUMENTED_HARDWARE_LIMITS",
        "work": str(work),
        "updater": tests,
        "startup": startup,
        "exclusions": [
            "RTL8197F kernel boot",
            "NAND/ECC/bad-block behavior",
            "power-loss atomicity",
            "Wi-Fi and peripheral initialization",
        ],
    }
    (args.build / "runtime-report.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError(
            "Optimized Python disables required validation; remove -O/PYTHONOPTIMIZE"
        )
    main()
