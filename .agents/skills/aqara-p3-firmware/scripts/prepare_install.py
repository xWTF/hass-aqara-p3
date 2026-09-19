"""Prepare a reviewable local installation bundle; no device access."""

import argparse
import hashlib
import json
import shutil
import struct
from pathlib import Path

from test_runtime import boot_info, checksum

TEMPLATE = r"""#!/bin/sh
set -eu
[ "${1:-}" = "--confirmed" ] || { echo 'Explicit confirmation required'; exit 2; }
STAGE=/data/aqara-p3-install-v3
BACKUP=/data/aqara-p3-firmware-backup
fail() { echo "FAILED: $*"; exit 1; }
verify() {
    value=$(sha256sum "$1") || fail "Cannot hash $1"
    [ "${value%% *}" = "$2" ] || fail "Checksum mismatch: $1"
}
[ "$(getprop persist.sys.model)" = lumi.aircondition.acn05 ] || fail 'Wrong model'
[ "$(cat /proc/cmdline)" = 'root=/dev/mtdblock7 console=ttyS0,38400' ] || fail 'Active slot changed'
[ "$(readlink -f "$STAGE")" = "$STAGE" ] || fail 'Unexpected staging path'
[ "$(readlink -f /data)" = /data ] || fail 'Unexpected data path'
for device in /dev/mtd5 /dev/mtdblock5 /dev/mtdblock6 /dev/mtdblock7 /dev/mtdblock1; do
    [ -b "$device" ] || [ -c "$device" ] || fail 'Expected flash device'
done
verify /etc/init.d/rcS @RCS_ORIGINAL@
verify /bin/fw_update @UPDATER@
verify /dev/mtdblock7 @ROOT_ORIGINAL@
verify /dev/mtdblock6 @KERNEL_ORIGINAL@
verify /dev/mtdblock1 @BOOT_ORIGINAL@
verify "$STAGE/firmware.bin" @IMAGE@
verify "$STAGE/post_init.sh" @POST_INIT@
for name in post_init.sh fw_update.log state; do
    [ ! -L "$STAGE/$name" ] || fail 'Symlink in staging directory'
done
if [ -e /data/scripts ] || [ -L /data/scripts ]; then
    [ -d /data/scripts ] && [ ! -L /data/scripts ] || fail 'Unexpected scripts path'
fi
[ ! -e /data/scripts/post_init.sh ] && [ ! -L /data/scripts/post_init.sh ] || fail 'User script already exists; review it first'
[ ! -e "$BACKUP" ] && [ ! -L "$BACKUP" ] || fail 'Backup exists; refusing to overwrite'
umask 077
mkdir "$BACKUP" || fail 'Cannot reserve backup directory'
cat /etc/init.d/rcS > "$BACKUP/rcS.original"
cat /dev/mtdblock1 > "$BACKUP/boot_info.original.bin"
printf 'ABSENT\n' > "$BACKUP/post_init.original.absent"
verify "$BACKUP/rcS.original" @RCS_ORIGINAL@
verify "$BACKUP/boot_info.original.bin" @BOOT_ORIGINAL@
chmod 400 "$BACKUP/rcS.original" "$BACKUP/boot_info.original.bin" "$BACKUP/post_init.original.absent"
chmod 500 "$BACKUP"
[ -d /data/scripts ] || mkdir /data/scripts
chmod 755 "$STAGE/post_init.sh"
ln "$STAGE/post_init.sh" /data/scripts/post_init.sh || fail 'Cannot exclusively install user script'
verify /data/scripts/post_init.sh @POST_INIT@
sync
printf 'FLASH_STARTED\n' > "$STAGE/state"
/bin/fw_update "$STAGE/firmware.bin" > "$STAGE/fw_update.log" 2>&1
sync
# fw_update exit status and its Success message are not sufficient.
value=$(head -c @PAYLOAD_SIZE@ /dev/mtdblock5 | sha256sum)
[ "${value%% *}" = @PAYLOAD@ ] || fail 'Written rootfs readback mismatch'
verify /dev/mtdblock1 @BOOT_EXPECTED@
verify /dev/mtdblock7 @ROOT_ORIGINAL@
verify /dev/mtdblock6 @KERNEL_ORIGINAL@
verify /data/scripts/post_init.sh @POST_INIT@
verify "$BACKUP/rcS.original" @RCS_ORIGINAL@
printf 'VERIFIED_READY_TO_REBOOT\n' > "$STAGE/state"
sync
echo VERIFIED_READY_TO_REBOOT
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("build", type=Path)
    ap.add_argument("backup", type=Path)
    args = ap.parse_args()
    out = args.build / "install"
    out.mkdir()
    report = json.loads((args.build / "build-report.json").read_text())
    runtime = json.loads((args.build / "runtime-report.json").read_text())
    policy = json.loads((args.build / "boot-policy-report.json").read_text())
    assert policy["status"] == "PASS"
    assert policy["checksum_vectors"] == 6 and len(policy["cases"]) == 12
    assert len(runtime["updater"]) == 11 and len(runtime["startup"]) == 6
    assert report["profile"] == "user-partition-post-init"
    assert runtime["status"] == "PASS_WITH_DOCUMENTED_HARDWARE_LIMITS"
    image = (args.build / "candidate.OFFLINE-ONLY.bin").read_bytes()
    assert sha(image) == report["wrapper_sha256"]
    assert len(image) == 16 + report["payload_size"]
    old = (args.backup / "mtd1-boot_info.bin").read_bytes()
    assert sha(old) == (args.backup / "mtd1-boot_info.sha256").read_text().split()[0]
    slots = boot_info(old)
    assert slots["current_root"] == slots["preferred_root"] == 1
    assert slots["current_kernel"] == slots["preferred_kernel"] == 1
    assert sha(image[16:]) == report["payload_sha256"]
    assert (
        sha((args.build / "post_init.sh").read_bytes()) == report["user_script_sha256"]
    )
    assert (
        sha((args.backup / "mtd7-rootfs_2.bin").read_bytes()) == report["source_sha256"]
    )
    new = bytearray(old)
    new[9] = 0
    struct.pack_into(">IHB", new, 24, len(image) - 16, checksum(image[16:]), 0)
    new[4:6] = struct.pack(">H", checksum(new[6:55]))
    normal = next(item for item in runtime["updater"] if item["case"] == "normal")
    assert normal["boot_info"]["roots"][0]["sum"] == checksum(image[16:])
    # Retain the exact simulated output for byte-for-byte comparison.
    # The runtime's normal snapshot is saved separately before later fault cases.
    actual = Path(runtime["work"]) / "normal-boot_info.bin"
    assert actual.is_file(), "Missing simulated boot_info output"
    assert actual.read_bytes() == new
    values = {
        "RCS_ORIGINAL": sha((args.build / "rcS.original").read_bytes()),
        "UPDATER": "97efaf9b4d2a7b623ba8469ad192c1e0ef65ff036869a4911299a6c7059c0dc7",
        "ROOT_ORIGINAL": report["source_sha256"],
        "KERNEL_ORIGINAL": "d3ce6d04ee741de577657303e289ca416035cb3edc3708625c645d68a26f169d",
        "BOOT_ORIGINAL": sha(old),
        "BOOT_EXPECTED": sha(new),
        "IMAGE": sha(image),
        "POST_INIT": report["user_script_sha256"],
        "PAYLOAD_SIZE": str(report["payload_size"]),
        "PAYLOAD": report["payload_sha256"],
    }
    assert (
        sha((args.backup / "mtd6-linux_2.bin").read_bytes())
        == values["KERNEL_ORIGINAL"]
    )
    original = Path(report["work"]) / "original"
    assert sha((original / "bin/fw_update").read_bytes()) == values["UPDATER"]
    script = TEMPLATE
    for key, value in values.items():
        script = script.replace("@" + key + "@", value)
    assert "@" not in script
    (out / "apply.sh").write_text(script, encoding="utf-8", newline="\n")
    (out / "firmware.bin").write_bytes(image)
    shutil.copyfile(args.build / "post_init.sh", out / "post_init.sh")
    (out / "boot_info.expected.bin").write_bytes(new)
    plan = {
        "approval": "PENDING_USER_CONFIRMATION",
        "target_rootfs": "mtd5 (rootfs bank 0)",
        "preserved_rootfs": "mtd7 (bank 1)",
        "preserved_kernel": "mtd6 (bank 1)",
        "device_backup": "/data/aqara-p3-firmware-backup",
        "user_script": "/data/scripts/post_init.sh",
        "requires_reboot_after_independent_verification": True,
        "checksums": values,
    }
    (out / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError(
            "Optimized Python disables required validation; remove -O/PYTHONOPTIMIZE"
        )
    main()
