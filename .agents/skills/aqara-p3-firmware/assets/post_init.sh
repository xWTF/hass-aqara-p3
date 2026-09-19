#!/bin/sh
# User startup hook, called after the factory application startup.
echo "Starting Aqara P3 user services"
echo enable > /sys/class/tty/tty/enable
if [ -z "$(pgrep telnetd)" ]; then
    /bin/busybox telnetd
fi
