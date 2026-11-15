#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# After the files are gone: Python caches written at run time are not part of the package.
case "$1" in
    remove|purge|0) rm -rf /usr/lib/fanpilot/fanpilot/__pycache__ ;;
esac
rmdir /usr/lib/fanpilot/fanpilot /usr/lib/fanpilot 2>/dev/null || true
exit 0
