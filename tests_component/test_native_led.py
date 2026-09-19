# SPDX-License-Identifier: GPL-3.0-only
"""Run the production lease/IPC code against a temporary local agent socket."""

import json
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def lamp():
    if not (compiler := shutil.which("cc")) or not hasattr(socket, "AF_UNIX"):
        pytest.skip("Native session tests require a POSIX C compiler")
    with tempfile.TemporaryDirectory(prefix="p3-led-") as directory:
        root = Path(directory)
        native = (
            Path(__file__).resolve().parents[1]
            / "custom_components/aqara_p3/native/p3lan.c"
        )
        source, binary = root / "test.c", root / "helper"
        owner, wire, lock = root / "owner", root / "agent", root / "ipc-lock"
        source.write_text(
            f"#define P3_LED_OWNER {json.dumps(str(owner))}\n"
            f"#define P3_AGENT_SOCKET {json.dumps(str(wire))}\n"
            f"#define P3_IPC_LOCK {json.dumps(str(lock))}\n"
            "#define main p3_main\n"
            f"#include {json.dumps(str(native))}\n"
            "#undef main\n"
            "int main(int argc,char **argv) {alarm(12); return argc==3 ? led_session(argv[1],argv[2]) : 2;}\n"
        )
        subprocess.run([compiler, "-O2", str(source), "-o", str(binary)], check=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        server.bind(str(wire))
        server.listen()
        server.settimeout(0.1)
        stopped = threading.Event()
        seen, errors = [], []

        def receive():
            while not stopped.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                try:
                    with connection:
                        connection.settimeout(3)
                        assert json.loads(connection.recv(1024)) == {
                            "method": "bind",
                            "address": 8192,
                        }
                        request = json.loads(connection.recv(1024))
                        assert request["_to"] == 1
                        assert request["method"] == "basis.system"
                        assert request["params"]["name"] == "system_lampoff"
                        seen.append(request["params"]["value"])
                        connection.send(b'{"result":["ok"]}')
                except (AssertionError, OSError, ValueError, KeyError) as error:
                    errors.append(error)

        worker = threading.Thread(target=receive)
        worker.start()

        def run(action, token):
            return subprocess.run(
                [str(binary), action, token], timeout=15, check=False
            ).returncode

        try:
            yield SimpleNamespace(
                run=run, owner=owner, seen=seen, binary=binary, root=root
            )
        finally:
            stopped.set()
            worker.join(timeout=4)
            server.close()
            assert not worker.is_alive()
            assert not errors


def test_old_shell_cannot_restore_new_shell_lamp(lamp):
    first, second = "a" * 32, "b" * 32
    assert lamp.run("claim", first) == 0
    inode = lamp.owner.stat().st_ino
    assert lamp.run("claim", second) == 0
    assert lamp.run("release", first) == 0
    assert lamp.seen == ["1", "1"]
    assert lamp.owner.read_text() == second
    assert lamp.run("release", second) == 0
    assert lamp.seen == ["1", "1", "0"]
    assert lamp.owner.read_bytes() == b""
    assert lamp.owner.stat().st_ino == inode
    assert lamp.run("release", second) == 0
    assert lamp.seen == ["1", "1", "0"]


def test_reset_invalidates_half_open_shell(lamp):
    assert lamp.run("claim", "a" * 32) == 0
    assert lamp.run("reset", "b" * 32) == 0
    assert lamp.run("release", "a" * 32) == 0
    assert lamp.seen == ["1", "0"]


def test_invalid_token_and_symlink_never_touch_lamp(lamp):
    assert lamp.run("claim", "$(reboot)") == 2
    target = lamp.root / "keep"
    target.write_text("unchanged")
    lamp.owner.symlink_to(target)
    assert lamp.run("claim", "a" * 32) != 0
    assert target.read_text() == "unchanged"
    assert not lamp.seen


@pytest.mark.parametrize("ending", ["exit", "kill -HUP $$"])
def test_shell_cleanup_restores_lamp(lamp, ending):
    token = "a" * 32
    command = (
        f"trap '{lamp.binary} release {token} >/dev/null 2>&1' EXIT; "
        f"trap 'exit 0' HUP; {lamp.binary} claim {token}; {ending}"
    )
    subprocess.run(["sh", "-c", command], check=True, timeout=15)
    assert lamp.seen == ["1", "0"]
    assert lamp.owner.read_bytes() == b""
