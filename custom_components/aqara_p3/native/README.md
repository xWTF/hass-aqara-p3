# Local adapter

`p3lan-helper` is a statically linked MIPS32r2 little-endian, soft-float executable,
built with Zig 0.14.1 / musl for Linux 3.10. Source: `p3lan.c` in the repository
and the release's Source code downloads. The install ZIP contains the executable
and license notices.

```sh
zig cc -target mipsel-linux.3.10-musleabi -mcpu=mips32r2 -msoft-float -Os -static -s p3lan.c -o p3lan-helper
```

After rebuilding, update `protocol/native_info.py` with the binary's SHA-256.
The repository's `scripts/build-native.ps1 -Zig <zig.exe>` compiles this source
and updates the checksum. musl's copyright
notice is included in `LICENSE.musl`; this project's native source is licensed
under GPL-3.0-only (see `../LICENSE`).

The executable provides `version`, bounded `ipc` requests, bounded
`capture PID SECONDS`, and `local-mode SCRIPT ACTION`. It uses the existing
factory IR process and IPC socket. Capture uses temporary
ptrace syscall observation and must not be used for continuous production
monitoring. A flock serializes adapter instances. Capture exits on timeout,
SIGTERM, SIGHUP or parent death and detaches without EXITKILL.

The integration verifies identity and firmware, copies the bundled binary to a
content-addressed `/tmp/p3lan-*` path on demand and verifies its checksum.
No device-side network download, flash write or boot-script modification is
involved. The cached binary disappears on reboot.

## Local mode

`local_mode.sh` is runtime code and is included in the install ZIP. It saves the
verified factory monitor once at `/data/aqara_p3/local-mode-v1/app_monitor.original.sh`
using exclusive hard-link publication, retaining a read-only backup. Existing
backups are validated and never replaced. Its runtime patch skips the MIoT cloud
client's restart handler; the rest of the monitor remains active.

The adapter holds flock throughout a mode transaction, closes the lock in its
child, and gives interrupted transactions time to roll back. Internal
`mode-bind` / `mode-unbind` commands use fixed-path mount syscalls because the
device's BusyBox lacks bind-mount support. Restore requires matching inode and
hash checks, removes the owned mount and restarts the factory cloud client.

The overlay lives in RAM and expires on reboot. HA reapplies an explicitly saved
choice in the background every 60 seconds, so the cloud client can run between a
device reboot and HA reconnecting. Power cycling itself has not been part of the
verification; enable, repeat enable, restore and original backup integrity have
been checked on firmware 4.0.4.

## Media playback

`audio TOKEN` owns a temporary IPv4 TCP listener and one original `aplay`
child. A separate RAM flock serializes media sessions without holding the IR
or IPC lock. The login shell execs the helper; HUP, parent death, connection
loss and bounded timeouts clean up the playback child. No audio files are
created on the device.

The 40-byte authentication header is `P3AUDIO1` followed by a per-session
32-character lowercase hex token. Each frame has a four-byte big-endian length
and up to 16 KiB of aligned S32_LE / 32000 Hz / mono PCM. A zero length is the
explicit normal end marker; an incomplete frame or unmarked EOF cancels the
player. The helper replies `READY` after spawning and `DONE 0` after a successful
drain. These report process state, not independent acoustic confirmation.

Authentication/connection setup is bounded, input stalls time out after 15
seconds and sessions last at most an hour. Only the owned player PID is
terminated. Its parent-death signal also covers forced helper termination.
The factory player needs its undocumented `-x 1` option to avoid its default
three-pass behavior. HA verifies the player fingerprint before launching.
