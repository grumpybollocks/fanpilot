#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Remove FanPilot. Stopping the service hands every fan back to the BIOS / GPU firmware first.
#
#   sudo ./uninstall.sh           keep /etc/fanpilot/config.json
#   sudo ./uninstall.sh --purge   remove the config too
# The it87 driver setup is separate: sudo scripts/install-driver.sh --undo
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

echo "==> stopping fanpilotd (fans go back to automatic)"
systemctl disable --now fanpilotd 2>/dev/null || true
/usr/local/bin/fanpilotd --restore 2>/dev/null || true

echo "==> removing files"
rm -f /etc/systemd/system/fanpilotd.service \
      /usr/local/bin/fanpilot /usr/local/bin/fanpilotd /usr/local/bin/fanpilotctl
if [[ ! -e /usr/lib/systemd/system/fanpilotd.service ]]; then
    # the menu entry and icon belong to a distribution package if one is installed: leave them then
    rm -f /usr/share/applications/fanpilot.desktop /usr/share/icons/hicolor/scalable/apps/fanpilot.svg
fi
rm -rf /opt/fanpilot /run/fanpilot
if [[ -n "${SUDO_UID:-}" ]]; then
    home=$(getent passwd "$SUDO_UID" | cut -d: -f6)
    rm -f "$home/.config/autostart/fanpilot.desktop"
fi
systemctl daemon-reload
if [[ "${1:-}" == "--purge" ]]; then
    rm -rf /etc/fanpilot
    echo "    config removed"
else
    echo "    kept /etc/fanpilot/config.json (use --purge to remove)"
fi
if [[ -e /usr/lib/systemd/system/fanpilotd.service ]]; then
    # a FanPilot package is installed too: its service was hidden behind this one and the
    # 'disable --now' above stopped it as well. Hand control to the package's service
    # (after --purge, so it never starts with a config that is about to be deleted).
    systemctl enable --now fanpilotd && echo "==> the packaged FanPilot service is now running"
fi
echo "==> done"
