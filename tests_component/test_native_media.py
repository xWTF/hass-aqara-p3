# SPDX-License-Identifier: GPL-3.0-only
"""Run the actual native stream supervisor with a disposable playback process."""

import json
import os
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

TOKEN = "a" * 32


def line(proc):
    assert select.select([proc.stdout], [], [], 5)[0], "native helper stalled"
    return proc.stdout.readline().decode().strip()


@pytest.fixture
def native_audio():
    if not (compiler := shutil.which("cc")) or os.name != "posix":
        pytest.skip("Requires POSIX C compiler")
    with tempfile.TemporaryDirectory(prefix="p3-media-") as directory:
        root = Path(directory)
        source = (
            Path(__file__).resolve().parents[1]
            / "custom_components/aqara_p3/native/p3lan.c"
        )
        player, pidfile, capture = root / "player", root / "pid", root / "pcm"
        player.write_text(f"#!/bin/sh\necho $$ > '{pidfile}'\nexec cat > '{capture}'\n")
        player.chmod(0o700)
        wrapper = root / "test.c"
        wrapper.write_text(
            f"#define P3_APLAY {json.dumps(str(player))}\n"
            f"#define P3_AUDIO_LOCK {json.dumps(str(root / 'lock'))}\n"
            "#define P3_AUDIO_IDLE 1\n"
            f"#include {json.dumps(str(source))}\n"
        )
        binary = root / "helper"
        subprocess.run([compiler, "-O2", str(wrapper), "-o", str(binary)], check=True)
        processes = []

        def start():
            proc = subprocess.Popen(
                [str(binary), "audio", TOKEN], stdout=subprocess.PIPE
            )
            processes.append(proc)
            return proc

        def connect(proc):
            port = int(line(proc).split()[1])
            client = socket.create_connection(("127.0.0.1", port), timeout=3)
            client.sendall(b"P3AUDIO1" + TOKEN.encode())
            assert client.recv(100) == b"READY\n"
            return client, port

        try:
            yield SimpleNamespace(
                start=start,
                connect=connect,
                root=root,
                player=player,
                pid=pidfile,
                capture=capture,
            )
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)


def test_pcm_frames_finish_and_lock_reuse(native_audio):
    proc = native_audio.start()
    with native_audio.connect(proc)[0] as client:
        pcm = bytes(range(256)) * 4
        wire = struct.pack("!I", len(pcm)) + pcm
        for offset in range(0, len(wire), 7):
            client.sendall(wire[offset : offset + 7])
        client.sendall(bytes(4))
        assert client.recv(100) == b"DONE 0\n"
    assert proc.wait(timeout=5) == 0
    assert native_audio.capture.read_bytes() == pcm
    next_proc = native_audio.start()
    with native_audio.connect(next_proc)[0] as client:
        client.sendall(bytes(4))
        assert client.recv(100) == b"DONE 0\n"
    assert next_proc.wait(timeout=5) == 0


def test_wrong_auth_never_launches_player(native_audio):
    proc = native_audio.start()
    port = int(line(proc).split()[1])
    with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
        client.sendall(b"P3AUDIO1" + b"b" * 32)
        assert client.recv(100) == b""
    assert not native_audio.pid.exists()
    proc.terminate()
    assert proc.wait(timeout=5) != 0


@pytest.mark.parametrize(
    "failure", ["disconnect", "partial", "oversize", "unaligned", "idle", "hup", "kill"]
)
def test_interruption_reaps_own_player(native_audio, failure):
    proc = native_audio.start()
    client, _ = native_audio.connect(proc)
    deadline = time.monotonic() + 3
    while not native_audio.pid.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    pid = int(native_audio.pid.read_text())
    if failure == "partial":
        client.sendall(struct.pack("!I", 4096) + bytes(4))
        client.close()
    elif failure in ("oversize", "unaligned"):
        client.sendall(struct.pack("!I", 20000 if failure == "oversize" else 3))
    elif failure == "hup":
        proc.send_signal(__import__("signal").SIGHUP)
    elif failure == "kill":
        proc.kill()
    elif failure == "disconnect":
        client.close()
    assert proc.wait(timeout=5) != 0
    client.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Playback child survived cancellation")


def test_player_failure_is_not_success(native_audio):
    native_audio.player.write_text("#!/bin/sh\nexit 7\n")
    proc = native_audio.start()
    with native_audio.connect(proc)[0] as client:
        client.sendall(bytes(4))
        assert client.recv(100) == b"DONE 7\n"
    assert proc.wait(timeout=5) == 7


def test_second_session_cannot_interrupt_owner(native_audio):
    first = native_audio.start()
    with native_audio.connect(first)[0] as client:
        second = native_audio.start()
        assert line(second) == "P3_AUDIO_DONE 6"
        assert second.wait(timeout=3) == 6
        client.sendall(bytes(4))
        assert client.recv(100) == b"DONE 0\n"
    assert first.wait(timeout=3) == 0
