# Local adapter

`p3lan-helper` is a statically linked MIPS32r2 little-endian, soft-float executable,
built with Zig 0.14.1 / musl for Linux 3.10. Source: `p3lan.c`.

```sh
zig cc -target mipsel-linux.3.10-musleabi -mcpu=mips32r2 -msoft-float -Os -static -s p3lan.c -o p3lan-helper
```

After rebuilding, update `protocol/native_info.py` with the binary's SHA-256.
The repository's `scripts/build-native.ps1 -Zig <zig.exe>` compiles this source
and updates the checksum. musl's copyright
notice is included in `LICENSE.musl`; this project's native source is licensed
under GPL-3.0-only (see `../LICENSE`).

The executable exposes only `version`, one bounded `ipc` request and a bounded
`capture PID SECONDS`. It uses the existing factory IR process and IPC socket;
it does not open the UART or install/start a service. Capture uses temporary
ptrace syscall observation and must not be used for continuous production
monitoring. A flock serializes adapter instances. Capture exits on timeout,
SIGTERM, SIGHUP or parent death and detaches without EXITKILL.

The integration verifies identity and firmware, copies the bundled binary to a
content-addressed `/tmp/p3lan-*` path on demand and verifies its checksum.
No device-side network download, flash write or boot-script modification is
involved. The cached binary disappears on reboot.
