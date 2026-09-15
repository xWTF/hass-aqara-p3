#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors
# Runtime-only overlay for the verified 4.0.4 MIoT monitor. No flash edits.

DATA_DIR=/data/aqara_p3
BASE=$DATA_DIR/local-mode-v1
BACKUP=$BASE/app_monitor.original.sh
TARGET=/bin/app_monitor.sh
CLOUD_EXE=/bin/miio_client
RUNDIR=/tmp/aqara-p3-local-mode
PATCH=$RUNDIR/app_monitor.sh
HELPER=$2
ORIGINAL_SHA=619bae0b8b18d338c6942f000f3320a53dfb125bb555e17c02b989512a5532ee
PATCH_SHA=bf9692eff0550d4074ca80071714e24044f5cb1d610045a0a184832dd0f9df85
rollback_to=

fail() { printf 'P3_LOCAL_ERROR %s\n' "$1"; exit 1; }
hash() { busybox sha256sum "$1" 2>/dev/null | busybox awk '{print $1}'; }
mount_count() { busybox awk '$2=="/bin/app_monitor.sh" {n++} END {print n+0}' /proc/mounts; }
inode() { busybox stat -t "$1" 2>/dev/null | busybox awk '{print $7 ":" $8}'; }

secure_dir() {
    [ ! -L "$1" ] || fail unsafe_path
    if [ ! -d "$1" ]; then busybox mkdir -m 700 "$1" || fail storage_failed; fi
}

check_backup() {
    [ ! -L "$BACKUP" ] && [ -f "$BACKUP" ] || fail backup_conflict
    [ "$(hash "$BACKUP")" = "$ORIGINAL_SHA" ] || fail backup_conflict
}

save_backup() {
    secure_dir "$DATA_DIR"
    secure_dir "$BASE"
    if [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; then check_backup; return; fi
    [ "$(hash "$TARGET")" = "$ORIGINAL_SHA" ] || fail system_changed
    temporary=$(busybox mktemp "$BASE/.backup.XXXXXX") || fail storage_failed
    busybox cp -p "$TARGET" "$temporary" || fail storage_failed
    [ "$(hash "$temporary")" = "$ORIGINAL_SHA" ] || fail backup_conflict
    busybox chmod 400 "$temporary" || fail storage_failed
    # Publishing by hard link is exclusive: an existing backup is never replaced.
    if ! busybox ln "$temporary" "$BACKUP"; then
        busybox rm -f "$temporary"
        check_backup
        return
    fi
    busybox rm -f "$temporary"
    busybox sync
    check_backup
}

make_patch() {
    secure_dir "$RUNDIR"
    [ ! -L "$PATCH" ] || fail system_changed
    if [ -e "$PATCH" ]; then
        [ "$(hash "$PATCH")" = "$PATCH_SHA" ] || fail system_changed
        return
    fi
    temporary=$(busybox mktemp "$RUNDIR/.monitor.XXXXXX") || fail storage_failed
    busybox awk '$0=="\tmiio_client -l4 -d /data/miio/ -D" {$0="\t: # Aqara P3 local mode"} {print}' "$BACKUP" > "$temporary" || fail storage_failed
    [ "$(hash "$temporary")" = "$PATCH_SHA" ] || fail system_changed
    busybox chmod 755 "$temporary" || fail storage_failed
    busybox ln "$temporary" "$PATCH" || fail system_changed
    busybox rm -f "$temporary"
}

owned_mount() {
    [ "$(mount_count)" = 1 ] && [ ! -L "$PATCH" ] &&
    [ "$(hash "$TARGET")" = "$PATCH_SHA" ] &&
    [ "$(inode "$TARGET")" = "$(inode "$PATCH")" ]
}

check_system() {
    [ "$(getprop persist.sys.cloud)" = miot ] || fail unsupported_mode
    [ ! -L "$TARGET" ] || fail system_changed
    case "$(mount_count)" in
        0) [ "$(hash "$TARGET")" = "$ORIGINAL_SHA" ] || fail system_changed ;;
        1) owned_mount || fail system_changed; check_backup ;;
        *) fail system_changed ;;
    esac
}

monitor_pids() {
    for entry in /proc/[0-9]*/comm; do
        read -r name < "$entry" 2>/dev/null || continue
        [ "$name" = app_monitor.sh ] || continue
        entry=${entry%/comm}/cmdline
        command=$(busybox tr '\000' '|' < "$entry" 2>/dev/null)
        case "$command" in
            '/bin/sh|/bin/app_monitor.sh|'|'sh|/bin/app_monitor.sh|')
                pid=${entry#/proc/}; printf '%s\n' "${pid%/cmdline}" ;;
        esac
    done
}

cloud_pids() {
    for entry in /proc/[0-9]*/comm; do
        read -r name < "$entry" 2>/dev/null || continue
        [ "$name" = miio_client ] || continue
        pid=${entry#/proc/}; pid=${pid%/comm}
        [ "$(busybox readlink "/proc/$pid/exe" 2>/dev/null)" = "$CLOUD_EXE" ] || continue
        printf '%s\n' "$pid"
    done
}

stop_group() {
    for pid in $($1); do kill -TERM "$pid" 2>/dev/null || true; done
    busybox sleep 0.3
    for pid in $($1); do kill -KILL "$pid" 2>/dev/null || true; done
    busybox sleep 0.1
    [ -z "$($1)" ] || fail stop_failed
}

start_monitor() {
    [ -z "$(monitor_pids)" ] || return
    busybox nohup /bin/app_monitor.sh </dev/null >/dev/null 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$RUNDIR/monitor.pid"
    busybox sleep 0.2
    [ "$(monitor_pids)" = "$pid" ] || fail start_failed
}

runtime_local() {
    stop_group monitor_pids
    if [ "$(mount_count)" = 0 ]; then
        "$HELPER" mode-bind || fail mount_failed
    fi
    owned_mount || fail system_changed
    stop_group cloud_pids
    start_monitor
}

runtime_cloud() {
    stop_group monitor_pids
    if [ "$(mount_count)" != 0 ]; then
        owned_mount || fail system_changed
        "$HELPER" mode-unbind || fail mount_failed
    fi
    [ "$(hash "$TARGET")" = "$ORIGINAL_SHA" ] || fail system_changed
    if [ -z "$(cloud_pids)" ]; then
        busybox nohup /bin/miio_client -l4 -d /data/miio/ -D </dev/null >/dev/null 2>&1 &
        busybox sleep 0.5
    fi
    start_monitor
    [ -n "$(cloud_pids)" ] || fail start_failed
}

report() {
    backup=absent
    if [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; then
        backup=conflict
        if [ ! -L "$BACKUP" ] && [ "$(hash "$BACKUP")" = "$ORIGINAL_SHA" ]; then backup=ready; fi
    fi
    cloud=false; [ -z "$(cloud_pids)" ] || cloud=true
    monitor=false; [ -z "$(monitor_pids)" ] || monitor=true
    mode=cloud
    if [ "$(mount_count)" != 0 ]; then mode=local; fi
    printf 'P3_LOCAL_STATE {"mode":"%s","backup":"%s","cloud_running":%s,"monitor_running":%s}\n' "$mode" "$backup" "$cloud" "$monitor"
}

cleanup() {
    result=$?
    trap - 0 HUP INT TERM
    if [ "$result" != 0 ] && [ -n "$rollback_to" ]; then
        if ! ( "$rollback_to" ); then printf 'P3_LOCAL_ERROR rollback_failed\n'; fi
    fi
    exit "$result"
}

# Entry point (the tests exercise the functions above in an isolated filesystem).
case "$1" in status|enable|disable) ;; *) fail invalid_action;; esac
# The native helper holds flock while this script runs and enforces a timeout.
trap cleanup 0
trap 'exit 1' HUP INT TERM
check_system
case "$1" in
    enable)
        save_backup
        make_patch
        if owned_mount; then
            # Reuse an existing overlay; only recover a missing/stale monitor.
            if [ "$(monitor_pids)" != "$(cat "$RUNDIR/monitor.pid" 2>/dev/null)" ]; then
                rollback_to=runtime_local
                runtime_local
            else
                stop_group cloud_pids
            fi
        else
            rollback_to=runtime_cloud
            runtime_local
        fi
        ;;
    disable)
        secure_dir "$RUNDIR"
        if owned_mount; then
            check_backup
            rollback_to=runtime_local
            runtime_cloud
        elif [ -z "$(monitor_pids)" ] || [ -z "$(cloud_pids)" ]; then
            runtime_cloud
        fi
        ;;
esac
rollback_to=
report
