#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Install FanPilot (daemon + GUI + CLI).
#
#   sudo ./install.sh               install / update, enable the service
#   sudo ./install.sh --autostart   also start the tray app at login
#   sudo ./install.sh --gui-venv    put the app's toolkit (PySide6, ~200 MB download) in /opt/fanpilot/venv,
#                                   for distributions without a PySide6 package (Ubuntu 24.04, Debian 12)
# Any distribution with systemd and Python 3.10+. Distribution packages (AUR, .deb, .rpm) install the
# same files under /usr instead; don't mix the two (uninstall one first).
#
# Files: /opt/fanpilot, /usr/local/bin/fanpilot{,d,ctl}, /etc/systemd/system/fanpilotd.service,
#        /usr/share/applications/fanpilot.desktop, /usr/share/icons/hicolor/scalable/apps/fanpilot.svg,
#        /etc/fanpilot/config.json (kept on update). Undo with: sudo ./uninstall.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
command -v python3 >/dev/null || die "python3 missing"
python3 -c 'import sys; assert sys.version_info >= (3, 10)' || die "python 3.10+ needed"
uid=${SUDO_UID:-}
[[ -n "$uid" && "$uid" != 0 ]] || die "run via sudo from your normal user, so that user can change settings"
user=$(id -nu "$uid")
if [[ -e /usr/lib/systemd/system/fanpilotd.service ]]; then
    die "FanPilot is installed as a distribution package (AUR / .deb / .rpm). Update it with your package
       manager instead, or remove the package first; a second copy here would silently override it."
fi
if [[ -L /etc/systemd/system/fanpilotd.service ]] \
        && [[ $(readlink -f /etc/systemd/system/fanpilotd.service) == /dev/null ]]; then
    die "fanpilotd is masked: 'sudo systemctl unmask fanpilotd' first if you really want it back"
fi
autostart=0 gui_venv=0
for a in "$@"; do
    case "$a" in
        --autostart) autostart=1 ;;
        --gui-venv) gui_venv=1 ;;
        *) die "unknown option $a" ;;
    esac
done

pyside_hint() {
    if command -v pacman >/dev/null; then echo "sudo pacman -S pyside6"
    elif command -v dnf >/dev/null; then echo "sudo dnf install python3-pyside6"
    elif command -v zypper >/dev/null; then echo "sudo zypper install python3-PySide6"
    elif command -v apt-get >/dev/null; then
        echo "sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtnetwork (Debian 13 / Ubuntu 24.10+;"
        echo "    Ubuntu 24.04 and Debian 12 have none: sudo ./install.sh --gui-venv)"
    else echo "install PySide6 for python3, or: sudo ./install.sh --gui-venv"; fi
}
have_qt=1                                   # 1 = distribution package, 2 = /opt/fanpilot/venv, 0 = none
python3 -c 'import PySide6.QtWidgets, PySide6.QtNetwork' 2>/dev/null || have_qt=0
if (( have_qt == 0 )) && [[ -d /opt/fanpilot/venv ]]; then have_qt=2; fi

say "Running tests (simulated hardware, no real fans touched)"
noqt=1
if (( have_qt == 1 )); then noqt=0; fi     # without PySide6 the app's tests are skipped; the service is fully tested
runuser -u "$user" -- env QT_QPA_PLATFORM=offscreen FANPILOT_NO_QT=$noqt python3 -I -m unittest discover -s tests -t . -q 2>&1 \
    | tail -n 3 || die "tests failed, not installing"

say "Copying files"
# copy to a new folder, then swap it in: a running app never sees a half-copied tree
install -d -m 755 /opt/fanpilot
stage=$(mktemp -d /opt/fanpilot/.new.XXXXXX)
install -m 644 fanpilot/*.py "$stage/"
chmod 755 "$stage"
rm -rf /opt/fanpilot/fanpilot.old
if [[ -d /opt/fanpilot/fanpilot ]]; then mv /opt/fanpilot/fanpilot /opt/fanpilot/fanpilot.old; fi
mv "$stage" /opt/fanpilot/fanpilot
rm -rf /opt/fanpilot/fanpilot.old
bash packaging/make-launchers.sh /opt/fanpilot /usr/local/bin .new
for bin in fanpilotd fanpilotctl fanpilot; do
    mv -f "/usr/local/bin/.$bin.new" "/usr/local/bin/$bin"
    echo "    /usr/local/bin/$bin"
done
# a new file moved into place (never written through a symlink that may sit at the destination)
sed 's|@BINDIR@|/usr/local/bin|g' packaging/fanpilotd.service > /etc/systemd/system/.fanpilotd.service.new
chmod 644 /etc/systemd/system/.fanpilotd.service.new
mv -f /etc/systemd/system/.fanpilotd.service.new /etc/systemd/system/fanpilotd.service
install -D -m 644 packaging/fanpilot.desktop /usr/share/applications/fanpilot.desktop
install -D -m 644 packaging/fanpilot.svg /usr/share/icons/hicolor/scalable/apps/fanpilot.svg
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true

say "Config (/etc/fanpilot/config.json): allowing user '$user' (uid $uid) to change settings"
install -d -m 755 /etc/fanpilot
python3 -I - "$uid" <<'EOF'
import sys
sys.path.insert(0, "/opt/fanpilot")
from fanpilot import config
p = "/etc/fanpilot/config.json"
import os, time
try:
    cfg = config.load(p)
except (config.ConfigError, ValueError) as e:
    # don't stop half-way (files already copied): keep the broken file, start from defaults
    bad = f"{p}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
    os.replace(p, bad)
    print(f"    !! the existing config was invalid ({e});\n       it is kept as {bad} and a fresh one is used")
    cfg = config.load(p)
uid = int(sys.argv[1])
if uid not in cfg["allowed_uids"]:
    cfg["allowed_uids"].append(uid)
config.save(p, cfg)
EOF

if (( gui_venv )); then
    say "The app's toolkit (PySide6) in /opt/fanpilot/venv"
    python3 -m venv /opt/fanpilot/venv || die "python3 -m venv failed (Debian/Ubuntu: sudo apt install python3-venv)"
    # PySide6 6.12.0 loses a reference to None on every call that returns None: on Python < 3.12 (None is
    # not immortal there, e.g. Debian 12) the app crashes after a while. Measured in CI; 6.9-6.11 are fine.
    spec=$(/opt/fanpilot/venv/bin/python -c 'import sys; print("PySide6>=6.5,<6.12" if sys.version_info < (3, 12) else "PySide6>=6.5")')
    /opt/fanpilot/venv/bin/pip install --quiet --upgrade "$spec" || die "could not download PySide6"
    have_qt=2
fi
if (( have_qt == 0 )); then
    say "Note: the desktop app needs PySide6, which is not installed"
    echo "    The service and fanpilotctl work without it. For the app: $(pyside_hint)"
fi

if (( autostart )); then
    home=$(getent passwd "$uid" | cut -d: -f6)
    # written as the user (not root), so a symlink there can't redirect a root write
    runuser -u "$user" -- mkdir -p "$home/.config/autostart"
    sed 's|^Exec=.*|Exec=/usr/local/bin/fanpilot --hidden|' packaging/fanpilot.desktop \
        | runuser -u "$user" -- tee "$home/.config/autostart/fanpilot.desktop" >/dev/null
    echo "    tray app starts at login"
fi

say "Starting the service"
systemctl daemon-reload
systemctl enable fanpilotd >/dev/null
systemctl restart fanpilotd
sleep 2

# A FanPilot window older than 0.5.1 keeps running its old code (and one older than 0.5 can't see the new
# single-instance lock: two tray icons). Restart it in its own desktop session, with its own environment.
# Newer windows update themselves (they leave $XDG_RUNTIME_DIR/fanpilot-gui.pid) and are left alone.
gui_re='^/usr/bin/python3 -I /usr/local/bin/fanpilot( |$)'
selfupdating=$(cat "/run/user/$uid/fanpilot-gui.pid" 2>/dev/null || true)
restart_env=()
for pid in $(pgrep -u "$uid" -f "$gui_re" || true); do
    if [[ "$pid" == "$selfupdating" ]]; then continue; fi
    if (( ${#restart_env[@]} == 0 )); then
        envs=()
        mapfile -d '' -t envs 2>/dev/null < "/proc/$pid/environ" || envs=()
        for e in "${envs[@]}"; do
            case "$e" in
                INVOCATION_ID=*|JOURNAL_STREAM=*|SYSTEMD_EXEC_PID=*|MANAGERPID=*|NOTIFY_SOCKET=*|LISTEN_*) ;;
                *) restart_env+=("$e") ;;
            esac
        done
    fi
    kill "$pid" 2>/dev/null || true
done
if (( ${#restart_env[@]} )); then
    sleep 1
    runuser -u "$user" -- env -i "${restart_env[@]}" setsid -f /usr/local/bin/fanpilot --hidden \
        >/dev/null 2>&1 || true
    sleep 3             # setsid -f always succeeds: check that the app really came back
    if pgrep -u "$uid" -f "$gui_re" >/dev/null; then
        echo "    the FanPilot tray app was restarted with the new version"
    else
        echo "    !! open FanPilot from the app menu again to use the new version"
    fi
fi
systemctl --no-pager --lines=5 status fanpilotd || true
/usr/local/bin/fanpilotctl status || true
say "Installed. Open 'FanPilot' from the app menu. Every fan starts on Automatic (BIOS/card)."
