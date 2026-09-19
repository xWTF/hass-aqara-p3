"""Smoke test the original MIPS telnetd on loopback, with bounded cleanup."""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("build", type=Path)
    args = ap.parse_args()
    build = json.loads((args.build / "build-report.json").read_text())
    original = Path(build["work"]) / "original"
    # QEMU -L also prefixes device paths that exist there. Use a library-only
    # prefix so extracted firmware device nodes cannot shadow WSL's PTYs.
    prefix = Path(build["work"]) / "telnet-libraries"
    if not prefix.exists():
        shutil.copytree(original / "lib", prefix / "lib", symlinks=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    command = [
        "/usr/bin/qemu-mipsel-static",
        "-B",
        "0x100000000",
        "-R",
        "2147483648",
        "-L",
        str(prefix),
        str(original / "bin/busybox"),
        "telnetd",
        "-F",
        "-b",
        "127.0.0.1",
        "-p",
        str(port),
        "-l",
        "/bin/false",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    received = b""
    connected = False
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("telnetd exited before listening")
            try:
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=0.5
                ) as connection:
                    connected = True
                    received = connection.recv(128)
                    break
            except ConnectionRefusedError:
                time.sleep(0.05)
        if not connected:
            raise RuntimeError("No listening socket")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate(timeout=5)
        (args.build / "telnetd.log").write_bytes(output)
    negotiation = received.startswith(b"\xff")
    if not negotiation and b"can't find free pty" not in output:
        raise RuntimeError(f"Unexpected Telnet failure: {output!r}")
    report = {
        "status": "PASS" if negotiation else "LIMITED_BY_HOST_PTY",
        "binding": "loopback only",
        "listening_socket_verified": connected,
        "telnet_negotiation_verified": negotiation,
        "telnet_negotiation_hex": received.hex(),
        "cleaned_up": process.poll() is not None,
        "login": "/bin/false test stub",
        "limit": "WSL1 cannot supply the legacy PTY expected by this BusyBox; device TTY driver and password authentication are untested",
    }
    (args.build / "telnetd-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    (args.build / "telnetd.log").write_bytes(output)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError(
            "Optimized Python disables required validation; remove -O/PYTHONOPTIMIZE"
        )
    main()
