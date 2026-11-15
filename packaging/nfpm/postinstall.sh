#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# .deb / .rpm: create the 'fanpilot' group, reload systemd; restart the service only on an upgrade.
systemd-sysusers fanpilot.conf >/dev/null 2>&1 || true
systemctl daemon-reload >/dev/null 2>&1 || true
if [ -e /etc/systemd/system/fanpilotd.service ] || [ -d /opt/fanpilot ]; then
    echo "!! FanPilot from the source installer is also present (/opt/fanpilot, /etc/systemd/system)."
    echo "   Its service file overrides this package's. Remove it, keeping your settings:"
    echo "   sudo ./uninstall.sh   (from the source folder), then: sudo systemctl daemon-reload"
fi
if systemctl is-active --quiet fanpilotd 2>/dev/null; then
    systemctl restart fanpilotd >/dev/null 2>&1 || true
else
    echo "FanPilot: every fan starts on Automatic (BIOS / graphics card)."
    echo "  1. Let your user change settings:  sudo usermod -aG fanpilot \$USER  (then log out and in)"
    echo "  2. Start the service:              sudo systemctl enable --now fanpilotd"
    echo "  No motherboard fans? See /usr/share/doc/fanpilot/SETUP.md"
fi
exit 0
