#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Before files go: stopping the service hands every fan back to the BIOS / card (ExecStopPost).
# On an upgrade the service keeps running and postinstall restarts it:
#   deb: $1 = "upgrade"; rpm: $1 = number of versions left after this one (>= 1 on an upgrade).
case "$1" in
    upgrade) exit 0 ;;
esac
if [ "$1" -ge 1 ] 2>/dev/null; then exit 0; fi
systemctl disable --now fanpilotd >/dev/null 2>&1 || true
exit 0
