"""Execute the extracted bootloader's actual MIPS selection code with Unicorn.

Hardware reads/writes, console output, large checksums and the final kernel jump
are hooks. This deliberately stops before Linux: it is not a board emulator.
"""

import argparse
import hashlib
import json
import struct
import sys
import zlib
from array import array
from pathlib import Path

from unicorn import (
    UC_ARCH_MIPS,
    UC_HOOK_CODE,
    UC_MODE_LITTLE_ENDIAN,
    UC_MODE_MIPS32,
    Uc,
)
from unicorn.mips_const import (
    UC_MIPS_REG_A0,
    UC_MIPS_REG_A1,
    UC_MIPS_REG_A2,
    UC_MIPS_REG_PC,
    UC_MIPS_REG_RA,
    UC_MIPS_REG_SP,
    UC_MIPS_REG_V0,
)


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    words = array("H", data)
    if sys.byteorder == "little":
        words.byteswap()
    n = sum(words) & 0xFFFFFFFF
    while n >> 16:
        n = (n & 65535) + (n >> 16)
    return (~n) & 65535


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("build", type=Path)
    ap.add_argument("backup", type=Path)
    ap.add_argument("--paired-bundle", type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()

    def backup(name):
        p = args.backup / name
        data = p.read_bytes()
        assert (
            hashlib.sha256(data).hexdigest()
            == p.with_suffix(".sha256").read_text().split()[0]
        )
        return data

    boot = backup("mtd0-bootloader.bin")
    assert (
        hashlib.sha256(boot).hexdigest()
        == "1d1426dbdfc98537cf5d435ec3870e9c3ede288bf273605e01d02f0ac7f24f10"
    )
    inflater = zlib.decompressobj(31)
    code = inflater.decompress(boot[0xA230:])
    assert inflater.eof
    original_info = backup("mtd1-boot_info.bin")[:55]
    flash = {
        0x200000: backup("mtd4-linux_1.bin"),
        0x500000: backup("mtd5-rootfs_1.bin"),
        0x1500000: backup("mtd6-linux_2.bin"),
        0x1800000: backup("mtd7-rootfs_2.bin"),
    }
    wrapper = (args.build / "candidate.OFFLINE-ONLY.bin").read_bytes()
    build = json.loads((args.build / "build-report.json").read_text())
    assert hashlib.sha256(wrapper).hexdigest() == build["wrapper_sha256"]
    candidate = wrapper[16:]

    def make_cpu():
        uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        # KSEG0/KSEG1 virtual addresses alias physical RAM in Unicorn MIPS.
        uc.mem_map(0, 64 * 1024 * 1024)
        uc.mem_write(0, code)
        uc.reg_write(UC_MIPS_REG_SP, 0x806FF000)
        uc.reg_write(UC_MIPS_REG_RA, 0xA001FFF0)
        return uc

    # Validate the arithmetic hook against the ORIGINAL machine-code routine.
    vectors = [b"", b"a", b"ab", b"abc", bytes(range(256)), b"\xff" * 2048]
    for data in vectors:
        uc = make_cpu()
        uc.mem_write(0x300000, data or b"\0")
        uc.reg_write(UC_MIPS_REG_A0, 0xA0300000)
        uc.reg_write(UC_MIPS_REG_A1, len(data))
        uc.emu_start(0xA0002654, 0xA001FFF0, count=100000)
        assert uc.reg_read(UC_MIPS_REG_V0) == checksum(data)

    def execute(info, images, save_fail=False):
        uc = make_cpu()
        uc.mem_write(0x18C70, bytes(info))
        uc.mem_write(0x18CA8, struct.pack("<I", 1))
        uc.reg_write(UC_MIPS_REG_A0, 0)
        result = {
            "jumped_to_kernel": False,
            "flash_reads": [],
            "boot_info_save_attempts": 0,
        }
        saved = bytes(info)

        def read(addr, n):
            return bytes(uc.mem_read(addr & 0x1FFFFFFF, n))

        def ret(value=0):
            uc.reg_write(UC_MIPS_REG_V0, value & 0xFFFFFFFF)
            uc.reg_write(UC_MIPS_REG_PC, uc.reg_read(UC_MIPS_REG_RA))

        def hook(_, address, size, user):
            nonlocal saved
            a0 = uc.reg_read(UC_MIPS_REG_A0)
            a1 = uc.reg_read(UC_MIPS_REG_A1)
            a2 = uc.reg_read(UC_MIPS_REG_A2)
            if address == 0xA00068C4:
                assert a1 in images
                result["flash_reads"].append({"offset": hex(a1), "length": a2})
                data = images[a1][:a2]
                if len(data) != a2:
                    ret(-1)
                    return
                uc.mem_write(a0 & 0x1FFFFFFF, data)
                ret()
            elif address == 0xA0006308:
                assert a0 == 0xA0000 and a2 == 55
                result["boot_info_save_attempts"] += 1
                if not save_fail:
                    saved = read(a1, a2)
                ret(-1 if save_fail else 0)
            elif address == 0xA0002654:
                ret(checksum(read(a0, a1)))
            elif address == 0xA000F9C0:
                ret()
            elif address == 0xA000F8E8:
                assert a0 == 0x81F00000
                cmd = f"root=/dev/mtdblock{a2} console=ttyS0,38400".encode() + b"\0"
                uc.mem_write(a0 & 0x1FFFFFFF, cmd)
                ret(len(cmd) - 1)
            elif address == 0xA0010D20:
                assert a0 == 0x80A00000
                result["jumped_to_kernel"] = True
                uc.emu_stop()

        for address in (
            0xA00068C4,
            0xA0006308,
            0xA0002654,
            0xA000F9C0,
            0xA000F8E8,
            0xA0010D20,
        ):
            uc.hook_add(UC_HOOK_CODE, hook, begin=address, end=address)
        uc.emu_start(0xA0003650, 0xA001FFF0, count=100000)
        now = read(0xA0018C70, 55)
        result.update(
            selected_kernel=now[6] if result["jumped_to_kernel"] else None,
            selected_root=now[7] if result["jumped_to_kernel"] else None,
            preferred_root=now[9],
            root_fail=[now[30], now[37]],
            boot_info_saved=saved != bytes(info),
        )
        return result, saved

    trial = bytearray(original_info)
    trial[9] = 0
    struct.pack_into(">IHB", trial, 24, len(candidate), checksum(candidate), 0)
    results = []
    cases = [
        ("stock", 0, False, False, None, False, 1),
        ("trial_checksum_off", 0, False, False, None, False, 0),
        ("corrupt_trial_checksum_off", 0, True, False, None, False, 0),
        ("trial_checksum_on", 1, False, False, None, False, 0),
        ("corrupt_trial_checksum_on", 1, True, False, None, False, 1),
        ("both_corrupt_checksum_on", 1, True, True, None, False, None),
        ("failed_three_checksum_on", 1, False, False, 3, False, 1),
        ("failed_three_checksum_off", 0, False, False, 3, False, 0),
        ("boot_info_save_failure", 1, False, False, None, True, 0),
    ]
    for name, check, badnew, badold, fail, savefail, expected in cases:
        info = bytearray(original_info if name == "stock" else trial)
        info[38] = check
        images = flash.copy()
        if name != "stock":
            images[0x500000] = candidate
        if badnew:
            images[0x500000] = b"NOFS" + images[0x500000][4:]
        if badold:
            images[0x1800000] = b"NOFS" + images[0x1800000][4:]
        if fail is not None:
            info[30] = fail
        info[4:6] = struct.pack(">H", checksum(info[6:55]))
        result, saved = execute(info, images, savefail)
        assert result["selected_root"] == expected, (name, result)
        results.append(dict(case=name, **result))
        print(name, expected, flush=True)
    # Model repeated resets AFTER the loader's kernel jump, without claiming to
    # execute Linux or the watchdog. The actual boot_info policy runs each time.
    state = bytearray(trial)
    state[38] = 1
    state[4:6] = struct.pack(">H", checksum(state[6:55]))
    images = flash.copy()
    images[0x500000] = candidate
    for i in range(3):
        result, saved = execute(state, images)
        assert result["selected_root"] == 0 and result["root_fail"][0] == 0
        results.append(dict(case=f"reset_after_kernel_jump_{i + 1}", **result))
        state = bytearray(saved)
    if args.paired_bundle:
        pair_manifest = json.loads((args.paired_bundle / "manifest.json").read_text())
        kernel_image = (args.paired_bundle / "kernel.bin").read_bytes()
        assert (
            hashlib.sha256(kernel_image).hexdigest()
            == pair_manifest["files"]["kernel.bin"]["sha256"]
        )
        assert (
            hashlib.sha256(wrapper).hexdigest()
            == pair_manifest["files"]["rootfs.bin"]["sha256"]
        )
        kernel_payload = kernel_image[16:]
        assert (
            len(kernel_payload) == pair_manifest["files"]["kernel.bin"]["payload_size"]
        )
        for current_kernel in (0, 1):
            for current_root in (0, 1):
                target_kernel, target_root = 1 - current_kernel, 1 - current_root
                info = bytearray(original_info)
                info[6:10] = bytes(
                    (current_kernel, current_root, target_kernel, target_root)
                )
                struct.pack_into(
                    ">IHB",
                    info,
                    10 + target_kernel * 7,
                    len(kernel_payload),
                    checksum(kernel_payload),
                    0,
                )
                struct.pack_into(
                    ">IHB",
                    info,
                    24 + target_root * 7,
                    len(candidate),
                    checksum(candidate),
                    0,
                )
                info[38] = 1
                info[4:6] = struct.pack(">H", checksum(info[6:55]))
                images = flash.copy()
                images[(0x200000, 0x1500000)[target_kernel]] = kernel_payload
                images[(0x500000, 0x1800000)[target_root]] = candidate
                result, _saved = execute(info, images)
                assert (result["selected_kernel"], result["selected_root"]) == (
                    target_kernel,
                    target_root,
                ), result
                name = f"paired_k{current_kernel}_r{current_root}"
                results.append(dict(case=name, **result))
                print(name, target_kernel, target_root, flush=True)
    report = {
        "status": "PASS",
        "engine": "Unicorn 2.1.4",
        "original_machine_code_entry": "0xa0003650",
        "bootloader_sha256": hashlib.sha256(boot).hexdigest(),
        "checksum_vectors": len(vectors),
        "cases": results,
        "hooks": [
            "Flash reads from verified files",
            "boot_info writes to memory",
            "console formatting",
            "checksum arithmetic (verified against original routine)",
            "stop at kernel jump",
        ],
        "excluded": "No CPU reset/DRAM/NAND initialization, Linux, watchdog or Wi-Fi emulation",
    }
    (args.output or args.build / "boot-policy-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError(
            "Optimized Python disables required validation; remove -O/PYTHONOPTIMIZE"
        )
    main()
