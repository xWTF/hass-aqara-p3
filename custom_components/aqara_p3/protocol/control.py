# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Independent persistent data/control sessions with bounded commands."""

import asyncio
import base64
import bz2
import hashlib
import json
import re
import shlex
import uuid
from pathlib import Path

from .capture import CaptureDecoder
from .errors import InvalidData, P3Error
from .heatshrink import encode_pulses
from .native_info import SHA256
from .telnet import ANSI, PROMPT, Resource, TelnetReader

REMOTE = "/tmp/p3lan-" + SHA256[:12]


class CommandError(P3Error):
    """Do not retry: device may have acted before the response was lost."""


class LocalModeError(CommandError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Local mode operation failed: {code}")


def native_payload():
    raw = (Path(__file__).resolve().parents[1] / "native/p3lan-helper").read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise InvalidData("Native helper checksum mismatch")
    return base64.b64encode(bz2.compress(raw)).decode("ascii")


class CommandSession(TelnetReader):
    async def run(self, cmd, *, timeout=8, cap=262144):
        async with self._lock:
            try:
                await self._connect()
                marker = "__P3_CMD_" + uuid.uuid4().hex + "__"
                trailer = "printf '\\n" + marker + ':%s\\n\' "$?"'
                done = re.compile(r"(?:^|\n)" + marker + r":([0-9]+)\n")
                async with asyncio.timeout(timeout):
                    await self._send_line("printf '\\n'")
                    for line in cmd.splitlines():
                        await self._send_line(line)
                    await self._send_line(trailer)
                    _, result = await self._until([done], limit=cap)
                    await self._until([PROMPT], limit=8192)
                match = done.search(result)
                if int(match[1]):
                    raise CommandError(
                        f"Local adapter failed ({match[1]}); not retried"
                    )
                body = ANSI.sub("", result[: match.start()])
                return "\n".join(
                    line
                    for line in (item.removeprefix("# ") for item in body.splitlines())
                    if line
                    not in (cmd, trailer, "# " + trailer, "# ", "", "printf '\\n'")
                )
            except BaseException:
                await self.close()
                raise


class P3Control:
    def __init__(self, config, expected_uid):
        self.config = config
        self.expected_uid = expected_uid
        self.session = CommandSession(config)
        self.data_session = CommandSession(config)
        self.data_lock = asyncio.Lock()
        self.local_led = False
        self._led_state = None
        self.capture_session = None
        self.capture_pid = None
        self.last_capture_result = None
        self._lock = asyncio.Lock()

    async def _verify_device(self, session):
        # Identity verification precedes every possible upload or write.
        mac = (await session.read(Resource.MAC)).strip().lower()
        if "p3_" + mac.replace(":", "") != self.expected_uid:
            raise InvalidData("Device identity changed; refusing control")
        if (await session.read(Resource.MODEL)).strip() != "lumi.aircondition.acn05":
            raise InvalidData("Unsupported device")
        if "4.0.4" not in (await session.read(Resource.FIRMWARE)).strip():
            raise InvalidData("Control currently requires verified firmware 4.0.4")

    async def _prepare(self, session):
        if (
            getattr(session, "connected", False)
            and session.connection_id is not None
            and getattr(session, "_prepared_connection", None) == session.connection_id
        ):
            return
        await self._verify_device(session)
        result = await session.run(f"busybox sha256sum {REMOTE} 2>/dev/null || true")
        if not re.search(r"(?m)^" + SHA256 + r"\s", result):
            payload = await asyncio.to_thread(native_payload)
            # BusyBox's interactive line editor loses pasted data while echoing
            # large heredocs. Disable echo on this session only.
            # stty flushes queued input, so send no next command until it settles.
            await session._send_line("busybox stty -echo")
            await asyncio.sleep(0.3)
            async with asyncio.timeout(2):
                await session._until([PROMPT], limit=8192)
            lines = "\n".join(payload[i : i + 768] for i in range(0, len(payload), 768))
            # Fixed filenames and generated base64 only; no user shell text.
            temporary = REMOTE + "." + uuid.uuid4().hex + ".new"
            cmd = f"busybox base64 -d <<'P3_NATIVE_EOF' | busybox bzcat > {temporary}\n{lines}\nP3_NATIVE_EOF"
            await session.run(cmd, timeout=15)
            result = await session.run(f"busybox sha256sum {temporary}")
            if not re.search(r"(?m)^" + SHA256 + r"\s", result):
                raise InvalidData("Upload checksum mismatch")
            await session.run(f"chmod 700 {temporary} && mv {temporary} {REMOTE}")
        result = await session.run(f"{REMOTE} version")
        if "p3lan-native-1" not in result:
            raise InvalidData("Native helper version mismatch")
        session._prepared_connection = session.connection_id

    async def sync_data_led(self, enabled=None):
        """Bind ordinary status lighting to the authenticated data login shell."""
        async with self.data_lock:
            if enabled is not None:
                self.local_led = enabled
            session = self.data_session
            if not self.local_led and enabled is None:
                return
            if session.connected and self._led_state == (
                session.connection_id,
                self.local_led,
            ):
                return
            try:
                async with asyncio.timeout(30):
                    await self._prepare(session)
                    fingerprint = await session.run("busybox sha256sum /bin/ha_basis")
                    if not re.search(
                        r"(?m)^331b5e32feae71fc2f68ee6328ab86a428481841caf510ff8373875a90202687\s",
                        fingerprint,
                    ):
                        raise InvalidData(
                            "Status lighting requires verified basis firmware"
                        )
                    token = session.connection_id
                    if self.local_led:
                        # Register before claim: even a lost response is cleaned up.
                        await session.run(
                            f"trap '{REMOTE} led-session release {token} >/dev/null 2>&1' EXIT; "
                            "trap 'exit 0' HUP; "
                            f"{REMOTE} led-session claim {token}",
                            timeout=14,
                        )
                    else:
                        # Reset also invalidates an older half-open session's lease.
                        await session.run(
                            f"{REMOTE} led-session reset {token} && trap - EXIT HUP",
                            timeout=14,
                        )
                    self._led_state = (token, self.local_led)
            except BaseException:
                await session.close()
                raise

    async def local_mode(self, action):
        if action not in ("status", "enable", "disable"):
            raise InvalidData("Invalid local mode action")
        if self.capture_session is not None:
            raise LocalModeError("busy")
        async with self._lock:
            try:
                async with asyncio.timeout(50):
                    await self._prepare(self.session)
                    path = Path(__file__).resolve().parents[1] / "native/local_mode.sh"
                    raw = await asyncio.to_thread(path.read_bytes)
                    # Normalize checkout line endings before uploading a shell script.
                    raw = raw.replace(b"\r\n", b"\n")
                    digest = hashlib.sha256(raw).hexdigest()
                    remote = "/tmp/aqara-p3-local-mode-" + digest[:12] + ".sh"
                    result = await self.session.run(
                        f"busybox sha256sum {remote} 2>/dev/null || true"
                    )
                    if not re.search(r"(?m)^" + digest + r"\s", result):
                        payload = base64.b64encode(bz2.compress(raw)).decode("ascii")
                        await self.session._send_line("busybox stty -echo")
                        await asyncio.sleep(0.3)
                        async with asyncio.timeout(2):
                            await self.session._until([PROMPT], limit=8192)
                        lines = "\n".join(
                            payload[i : i + 768] for i in range(0, len(payload), 768)
                        )
                        await self.session.run(
                            f"busybox base64 -d <<'P3_MODE_EOF' | busybox bzcat > {remote}.new\n"
                            + lines
                            + "\nP3_MODE_EOF",
                            timeout=10,
                        )
                        result = await self.session.run(
                            f"busybox sha256sum {remote}.new"
                        )
                        if not re.search(r"(?m)^" + digest + r"\s", result):
                            raise InvalidData("Local mode script checksum mismatch")
                        await self.session.run(
                            f"chmod 700 {remote}.new && mv {remote}.new {remote}"
                        )
                    result = await self.session.run(
                        f"{REMOTE} local-mode {remote} {action}; true",
                        timeout=35,
                        cap=16384,
                    )
                    errors = re.findall(r"(?m)^P3_LOCAL_ERROR ([a-z_]+)$", result)
                    if errors:
                        raise LocalModeError(errors[-1])
                    states = re.findall(r"(?m)^P3_LOCAL_STATE (\{[^\n]+\})$", result)
                    if len(states) != 1:
                        raise InvalidData("Missing local mode result")
                    state = json.loads(states[0])
                    if (
                        not isinstance(state, dict)
                        or set(state)
                        != {"mode", "backup", "cloud_running", "monitor_running"}
                        or state.get("mode") not in ("local", "cloud")
                        or state.get("backup") not in ("ready", "absent", "conflict")
                        or type(state.get("cloud_running")) is not bool
                        or type(state.get("monitor_running")) is not bool
                    ):
                        raise InvalidData("Invalid local mode result")
                    if action == "enable" and (
                        state["mode"] != "local"
                        or state["cloud_running"]
                        or not state["monitor_running"]
                        or state["backup"] != "ready"
                    ):
                        raise LocalModeError("verification_failed")
                    if action == "disable" and (
                        state["mode"] != "cloud"
                        or not state["cloud_running"]
                        or not state["monitor_running"]
                    ):
                        raise LocalModeError("verification_failed")
                    return state
            except BaseException:
                await self.session.close()
                raise

    async def _ipc(self, method, params, *, target=512):
        if self.capture_session is not None:
            raise CommandError("Stop IR capture before controlling")
        async with self._lock:
            try:
                async with asyncio.timeout(35):
                    await self._prepare(self.session)
                    if target == 32:
                        did = (
                            await self.session.run("getprop persist.sys.miio_did")
                        ).strip()
                        if not re.fullmatch(r"[0-9]{1,20}", did):
                            raise InvalidData("Invalid local device ID")
                        params = [{**item, "did": did} for item in params]
                    request_id = uuid.uuid4().int & 0x7FFFFFFF
                    request = {
                        "id": request_id,
                        "_to": target,
                        "method": method,
                        "params": params,
                    }
                    wire = json.dumps(request, separators=(",", ":"))
                    if len(wire.encode()) > 1000:
                        raise InvalidData("Request exceeds factory IPC capacity")
                    response = await self.session.run(
                        f"{REMOTE} ipc {shlex.quote(wire)}"
                    )
                    replies = []
                    for line in response.splitlines():
                        if not line.startswith("{"):
                            continue
                        try:
                            candidate = json.loads(line)
                        except ValueError:
                            continue
                        if (
                            isinstance(candidate, dict)
                            and candidate.get("id") == request_id
                        ):
                            replies.append(candidate)
                    if (
                        len(replies) != 1
                        or not isinstance(replies[0], dict)
                        or replies[0].get("id") != request_id
                    ):
                        raise CommandError("Missing acknowledgement; not retried")
                    reply = replies[0]
                    if (
                        not isinstance(reply, dict)
                        or "error" in reply
                        or reply.get("_from") != target
                    ):
                        raise CommandError("Device rejected command; not retried")
                    if target == 512:
                        if reply.get("result") != ["ok"]:
                            raise CommandError("Device rejected command; not retried")
                    else:
                        result = reply.get("result")
                        expected = {(p["siid"], p["piid"]) for p in params}
                        seen = set()
                        if not isinstance(result, list) or len(result) != len(params):
                            raise CommandError("Incomplete property reply; not retried")
                        for item in result:
                            if not isinstance(item, dict):
                                raise CommandError(
                                    "Invalid property reply; not retried"
                                )
                            if (
                                type(item.get("siid")) is not int
                                or type(item.get("piid")) is not int
                            ):
                                raise CommandError("Invalid property identifier")
                            key = (item.get("siid"), item.get("piid"))
                            if (
                                key not in expected
                                or key in seen
                                or item.get("did") != did
                                or type(item.get("code")) is not int
                                or item["code"] != 0
                            ):
                                raise CommandError("Property rejected; not retried")
                            if method == "get_properties" and "value" not in item:
                                raise CommandError("Missing property value")
                            seen.add(key)
                    return reply
            except BaseException:
                await self.session.close()
                raise

    async def audio_read(self):
        keys = ((5, 2), (9, 5))
        reply = await self._ipc(
            "get_properties", [{"siid": s, "piid": p} for s, p in keys], target=32
        )
        return {(p["siid"], p["piid"]): p["value"] for p in reply["result"]}

    async def read_energy(self):
        """Read the native integrator without invoking or stopping the IR service."""
        async with self.data_lock:
            try:
                await self._prepare(self.data_session)
                hashes = await self.data_session.run(
                    "sha256sum /bin/mha_ir /lib/libha_ir.so"
                )
                expected = {
                    "/bin/mha_ir": "8ebc70f32f973265533129f02de7d791da518a4911eb2f26a4c76fed59ed6ffd",
                    "/lib/libha_ir.so": "d6cd8c88a4cb94438fce22f94e20cdce504d56508f6a02d94fc9475ac7e595e7",
                }
                found = {}
                for line in hashes.splitlines():
                    fields = line.split()
                    if len(fields) == 2:
                        found[fields[1]] = fields[0]
                if found != expected:
                    raise InvalidData("电量读取不支持当前原厂程序版本")
                return json.loads(await self.data_session.run(f"{REMOTE} energy"))
            except BaseException:
                await self.data_session.close()
                raise

    async def audio_write(self, siid, piid, value):
        # Only expose investigated operations; never arbitrary MIOT writes.
        if (siid, piid) == (5, 2):
            valid = type(value) is int and 0 <= value <= 100
        elif (siid, piid) == (9, 4):
            valid = type(value) is int and value == 1
        elif (siid, piid) == (9, 1):
            try:
                command = json.loads(value)
                valid = (
                    set(command) == {"name", "volume"}
                    and isinstance(command["name"], str)
                    and re.fullmatch(r"[A-Za-z0-9_]{1,32}", command["name"])
                    and type(command["volume"]) is int
                    and 0 <= command["volume"] <= 100
                )
            except (TypeError, ValueError, KeyError):
                valid = False
        else:
            valid = False
        if not valid:
            raise InvalidData("Unsupported audio property or value")
        return await self._ipc(
            "set_properties", [{"siid": siid, "piid": piid, "value": value}], target=32
        )

    async def send(self, state):
        code, length = await asyncio.to_thread(encode_pulses, state.pulses())
        return await self._ipc(
            "miIO.ir_play", {"code": code, "freq": 38000, "length": length}
        )

    async def relay(self, on):
        if type(on) is not bool:
            raise InvalidData("Invalid relay state")
        return await self._ipc(
            "local.spec_ir", {"cmd": 2, "key": "4.1.85", "value": "1" if on else "0"}
        )

    async def capture(self, seconds, on_ready=None):
        if type(seconds) is not int or not 10 <= seconds <= 300:
            raise InvalidData("Capture must last 10–300 seconds")
        if self.capture_session is not None:
            raise CommandError("Capture already running")
        async with self._lock:
            session = CommandSession(self.config)
            self.capture_session = session
            decoder = CaptureDecoder()
            self.last_capture_result = None
            complete = False
            try:
                async with asyncio.timeout(seconds + 35):
                    await self._prepare(session)
                    result = await session.run("busybox pgrep -x mha_ir")
                    pids = re.findall(r"(?m)^([0-9]+)$", result)
                    if len(pids) != 1:
                        raise InvalidData("IR service PID unavailable")
                    await session._send_line(
                        f"printf '\\n'; {REMOTE} capture {int(pids[0])} {seconds}"
                    )
                    idx, result = await session._until(
                        [re.compile(r"(?:^|\n)READY ([0-9]+)\n"), PROMPT]
                    )
                    if idx:
                        raise CommandError("Capture could not start")
                    self.capture_pid = int(re.search(r"READY ([0-9]+)", result)[1])
                    if on_ready:
                        on_ready()
                    total = 0
                    while True:
                        _, line = await session._until([re.compile(r"\n")], limit=20000)
                        total += len(line)
                        if total > 1_100_000:
                            raise InvalidData("Capture output limit exceeded")
                        line = line.strip()
                        if line.startswith("RX "):
                            try:
                                decoder.feed(bytes.fromhex(line[3:]))
                            except ValueError:
                                raise InvalidData("Invalid capture data") from None
                        elif line.startswith("DONE "):
                            if line != "DONE 0":
                                raise CommandError("Capture adapter failed")
                            complete = True
                            return {**decoder.result(), "complete": True}
            finally:
                self.last_capture_result = {**decoder.result(), "complete": complete}
                if self.capture_pid is not None:
                    await self._stop_capture_process()
                self.capture_pid = None
                await session.close()
                self.capture_session = None

    async def _stop_capture_process(self):
        # Separate short-lived session allows cancellation of a streaming shell.
        if self.capture_pid is None:
            return
        session = CommandSession(self.config)
        try:
            async with asyncio.timeout(5):
                expected = REMOTE.replace("/tmp/", "/var/tmp/")
                cmd = f'if [ "$(busybox readlink /proc/{self.capture_pid}/exe)" = {shlex.quote(expected)} ]; then kill -TERM {self.capture_pid}; fi'
                await session.run(cmd, timeout=4)
        except (P3Error, OSError, TimeoutError):
            pass
        finally:
            await session.close()

    async def close(self):
        if self.capture_pid:
            await self._stop_capture_process()
        await self.session.close()
        await self.data_session.close()
