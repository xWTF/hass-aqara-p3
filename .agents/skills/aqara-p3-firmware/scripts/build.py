"""Offline candidate builder. Run in WSL/Linux. Never connects to the P3."""

import argparse
import hashlib
import json
import os
import stat
import struct
import subprocess
import time
from pathlib import Path

SOURCE_SHA = "0361837803c25b19200264141dbaae2eaf6f30547a5f404c8cdf7a3558adcd8b"
RCS_SHA = "405e831672151ea110051b4ed42eabd271a203ea9770d90dd59f37a319e9d721"
OLD_TAIL = b"""CUSTOM_POST_INIT=/data/scripts/post_init.sh
# if [ -x ${CUSTOM_POST_INIT} ]; then
#     ${CUSTOM_POST_INIT} &
# else
fw_manager.sh -r
# fi
"""
NEW_TAIL = b"""CUSTOM_POST_INIT=/data/scripts/post_init.sh
fw_manager.sh -r
if [ -x "${CUSTOM_POST_INIT}" ]; then
    "${CUSTOM_POST_INIT}" > /tmp/post_init.log 2>&1 &
fi
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args, logfile):
    p = subprocess.run(
        [str(a) for a in args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    logfile.write_bytes(p.stdout)
    if p.returncode:
        raise RuntimeError(
            f"{args[0]} failed: {logfile}\n{p.stdout.decode(errors='replace')[-2000:]}"
        )
    return p.stdout


def manifest(root):
    entries = {}

    def visit(path):
        s = path.lstat()
        item = {
            "mode": s.st_mode,
            "uid": s.st_uid,
            "gid": s.st_gid,
            "mtime": int(s.st_mtime),
        }
        item["xattrs"] = {
            name: os.getxattr(path, name, follow_symlinks=False).hex()
            for name in os.listxattr(path, follow_symlinks=False)
        }
        if stat.S_ISREG(s.st_mode):
            item.update(
                size=s.st_size, sha256=digest(path.read_bytes()), nlink=s.st_nlink
            )
        elif stat.S_ISLNK(s.st_mode):
            item["target"] = os.readlink(path)
        elif stat.S_ISCHR(s.st_mode) or stat.S_ISBLK(s.st_mode):
            item["rdev"] = [os.major(s.st_rdev), os.minor(s.st_rdev)]
        entries[str(path.relative_to(root))] = item
        if stat.S_ISDIR(s.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)

    visit(root)
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument(
        "--work-root",
        type=Path,
        required=True,
        help="Fresh build work is created here; use a native Linux filesystem",
    )
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    source = args.source.resolve()
    raw = source.read_bytes()
    if digest(raw) != SOURCE_SHA:
        raise RuntimeError("Unapproved source image")
    if args.output.exists():
        raise RuntimeError("Output already exists; refusing overwrite")
    args.output.mkdir(parents=True)
    out = args.output.resolve()
    args.work_root.mkdir(parents=True, exist_ok=True)
    work = args.work_root.resolve() / ("build-" + time.strftime("%Y%m%dT%H%M%S"))
    work.mkdir()
    original = work / "original"
    run(
        ["unsquashfs", "-processors", "2", "-no-progress", "-d", original, source],
        out / "extract.log",
    )
    before = manifest(original)
    rcs = original / "etc/init.d/rcS"
    old = rcs.read_bytes()
    if digest(old) != RCS_SHA or not old.endswith(OLD_TAIL):
        raise RuntimeError("Unexpected rcS")
    new = old[: -len(OLD_TAIL)] + NEW_TAIL
    startup = (
        (Path(__file__).resolve().parent.parent / "assets/post_init.sh")
        .read_bytes()
        .replace(b"\r\n", b"\n")
    )
    (out / "rcS.original").write_bytes(old)
    (out / "rcS.candidate").write_bytes(new)
    (out / "post_init.sh").write_bytes(startup)
    timestamp = struct.unpack_from("<I", raw, 8)[0]
    base = [
        "mksquashfs",
        original,
        "PLACEHOLDER",
        "-noappend",
        "-comp",
        "xz",
        "-b",
        "131072",
        "-always-use-fragments",
        "-exports",
        "-mkfs-time",
        str(timestamp),
        "-processors",
        "2",
        "-no-progress",
    ]

    def build(name):
        cmd = base.copy()
        cmd[2] = out / (name + ".squashfs")
        run(cmd, out / (name + ".log"))
        check = work / (name + "-extracted")
        run(
            ["unsquashfs", "-processors", "2", "-no-progress", "-d", check, cmd[2]],
            out / (name + "-extract.log"),
        )
        return manifest(check), Path(cmd[2]).read_bytes()

    baseline, _baseline_bytes = build("baseline")
    if baseline != before:
        raise RuntimeError("Baseline metadata/content changed")
    old_stat = rcs.stat()
    rcs.write_bytes(new)
    os.utime(rcs, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    after, candidate = build("candidate")
    changed = [k for k in before if before[k] != after[k]]
    if set(before) != set(after) or changed != ["etc/init.d/rcS"]:
        raise RuntimeError(f"Unexpected changed paths: {changed}")
    check_old = before["etc/init.d/rcS"].copy()
    check_new = after["etc/init.d/rcS"].copy()
    for k in ("size", "sha256"):
        check_old.pop(k)
        check_new.pop(k)
    if check_old != check_new:
        raise RuntimeError("rcS metadata changed")
    repeat, repeat_bytes = build("candidate-repeat")
    if candidate != repeat_bytes or after != repeat:
        raise RuntimeError("Non-reproducible build")
    if len(candidate) > 16 * 1024 * 1024 - 131072:
        raise RuntimeError("Insufficient partition margin")
    # r6cr and the two legacy address fields match the public P3 rootfs wrapper.
    # fw_update uses the signature and big-endian length, ignoring those addresses.
    # This is an offline test container, not approved for flashing.
    # The vendor appends a four-byte trailer; the final BE16 value makes the
    # wrapping sum of BE16 words zero (distinct from boot_info's folded sum).
    trailer = (
        -sum(struct.unpack(">" + str(len(candidate) // 2) + "H", candidate))
    ) & 0xFFFF
    payload = candidate + struct.pack(">HH", 0, trailer)
    wrapper = struct.pack(">4sIII", b"r6cr", 0x2D0000, 0xE00000, len(payload)) + payload
    (out / "candidate.OFFLINE-ONLY.bin").write_bytes(wrapper)
    qemu = [
        "qemu-mipsel-static",
        "-B",
        "0x100000000",
        "-R",
        "2147483648",
        "-L",
        original,
        original / "bin/busybox",
    ]
    run(qemu + ["echo", "MIPS_OK"], out / "qemu-smoke.log")
    run(qemu + ["sh", "-n", rcs], out / "rcS-syntax.log")
    run(qemu + ["sh", "-n", out / "post_init.sh"], out / "post-init-syntax.log")
    for name, value in [
        ("manifest-original.json", before),
        ("manifest-candidate.json", after),
    ]:
        (out / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
    report = {
        "status": "OFFLINE_ONLY_NOT_FLASH_APPROVED",
        "source_sha256": SOURCE_SHA,
        "original_squashfs_bytes_used": struct.unpack_from("<Q", raw, 40)[0],
        "candidate_size": len(candidate),
        "candidate_sha256": digest(candidate),
        "wrapper_sha256": digest(wrapper),
        "payload_size": len(payload),
        "payload_sha256": digest(payload),
        "paths": len(before),
        "changed_paths": changed,
        "profile": "user-partition-post-init",
        "rcs_sha256": digest(new),
        "user_script": "/data/scripts/post_init.sh",
        "user_script_sha256": digest(startup),
        "user_script_in_rootfs": False,
        "baseline_metadata_and_content_identical": True,
        "deterministic_rebuild": True,
        "original_timestamp": timestamp,
        "work": str(work),
        "output": str(out),
        "qemu": "MIPS userspace only; no RTL8197F board or NAND emulation",
    }
    (out / "build-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
