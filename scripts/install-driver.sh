#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Load the motherboard fan driver for boards with an ITE Super I/O chip (it87), on any distribution.
# Most other boards don't need this (nct6775 is in every kernel); see docs/SETUP.md, "My motherboard fans
# are missing". Installed by the packages as `fanpilot-install-driver`.
#
#   sudo fanpilot-install-driver             detect + install + make persistent
#   sudo fanpilot-install-driver --force     also on boards that are not known to have an ITE chip
#                                            (check first: `sudo sensors-detect` must report an ITE IT8xxx chip)
#   sudo fanpilot-install-driver --undo      remove everything this script added
#   fanpilot-install-driver --dry-run [...]  print every step, change nothing (no root needed)
#
# Steps (each one is printed before it runs):
#   1. /etc/modprobe.d/fanpilot-it87.conf: ignore_resource_conflict=1 (it87's own per-driver option; the
#      system-wide acpi_enforce_resources=lax is never touched, it has stopped some PCs from booting)
#   2. try the kernel's own it87 driver
#   3. otherwise build frankcrawford/it87 (pinned commit) with DKMS: it rebuilds on kernel updates and adds
#      IT8655/8665/8686/8688/8689/8696/8698/87952. With Secure Boot the module must be signed and the key
#      enrolled once (the script explains how)
#   4. /etc/modules-load.d/fanpilot-it87.conf so it loads at boot
set -euo pipefail

PIN=bd7a566c8a60fe0308d67e26761e16260368191a   # frankcrawford/it87, 2026-10-02
REPO=https://github.com/frankcrawford/it87.git
SRC=/var/lib/fanpilot/it87                      # directory name must be "it87" (dkms-install.sh uses it)
MODCONF=/etc/modprobe.d/fanpilot-it87.conf
LOADCONF=/etc/modules-load.d/fanpilot-it87.conf
SYSFS=${FANPILOT_SYSFS:-/sys}                   # tests point this at a simulated tree
# board vendors whose desktop boards commonly use ITE chips (others: --force after sensors-detect)
ITE_VENDORS_RE='^(Gigabyte Technology|ASRock|BIOSTAR|Shuttle|ECS|Elitegroup)'

DRY=0 FORCE=0 UNDO=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --force) FORCE=1 ;;
        --undo) UNDO=1 ;;
        *) printf 'unknown option %s\n' "$a" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
run()  { printf '    $ %s\n' "$*"; if (( ! DRY )); then "$@"; fi; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
writefile() {   # writefile PATH CONTENT
    printf '    write %s\n' "$1"
    if (( ! DRY )); then printf '%s\n' "$2" > "$1"; fi
}

ite_chip() {
    local f
    for f in "$SYSFS"/class/hwmon/hwmon*/name; do
        if grep -qE '^it8[0-9]{3}' "$f" 2>/dev/null; then cat "$f"; return 0; fi
    done
    return 1
}

# a DKMS tree installed by a distribution package (e.g. it87-dkms-git) belongs to that package
pkg_owned() {
    local owner=""
    if command -v pacman >/dev/null; then owner=$(pacman -Qqo "/usr/src/it87-$1" 2>/dev/null || true)
    elif command -v dpkg >/dev/null; then owner=$(dpkg -S "/usr/src/it87-$1" 2>/dev/null | cut -d: -f1 || true)
    elif command -v rpm >/dev/null; then owner=$(rpm -qf "/usr/src/it87-$1" 2>/dev/null | grep -v 'not owned' || true)
    fi
    [[ -n "$owner" ]] || return 1
    echo "    it87 $1 belongs to the package '$owner'; leaving it to the package manager"
}

secure_boot_on() { command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; }

secure_boot_help() {
    local key=/var/lib/dkms/mok.pub                          # DKMS's own key (Arch, Fedora, Debian 12+)
    if [[ -e /var/lib/shim-signed/mok/MOK.der ]]; then key=/var/lib/shim-signed/mok/MOK.der; fi   # Ubuntu
    cat <<EOF
    Secure Boot is on: the kernel only loads signed modules. DKMS signs the module with the key
    $key. Enrol that key once:
        sudo mokutil --import $key      (choose a one-time password)
    then reboot and pick "Enroll MOK" -> "Continue" -> enter that password. After that run this script
    again. Symptom without it: "Key was rejected by service" when the module loads.
EOF
}

# it87 versions DKMS knows, one per line ("it87/v2.0-6-gbd7a566.20261002, 7.2.9, x86_64: installed" and
# "it87/v1.9: added" both give the version)
dkms_versions() { dkms status -m it87 2>/dev/null | sed -nE 's|^it87/([^,: ]+).*|\1|p' | sort -u; }
# only versions built from this script's pinned commit are this script's to remove
ours() { [[ "$1" == *"g${PIN:0:7}"* ]]; }

# packages to build a module for the running kernel, per distribution
build_deps() {
    if command -v pacman >/dev/null; then
        local kpkg kver hver pkgs=(dkms git make gcc)
        kpkg=$(pacman -Qqo "/usr/lib/modules/$(uname -r)/vmlinuz" 2>/dev/null | head -n1) \
            || die "cannot find the package that owns the running kernel"
        kver=$(pacman -Q "$kpkg" | awk '{print $2}')
        if pacman -Q "${kpkg}-headers" 2>/dev/null | grep -q " $kver\$"; then
            echo "    ${kpkg}-headers $kver already installed (matches the running kernel)"
        else
            # never let pacman pull headers for a different kernel version than the one running
            hver=$(pacman -Si "${kpkg}-headers" 2>/dev/null | awk -F': ' '/^Version/{print $2; exit}')
            [[ -n "$hver" ]] || die "package ${kpkg}-headers not found in the repositories"
            if [[ "$hver" != "$kver" ]]; then
                die "running kernel is $kpkg $kver but the repo has ${kpkg}-headers $hver.
       Update the system first (sudo pacman -Syu), reboot, then run this script again."
            fi
            pkgs+=("${kpkg}-headers")
        fi
        run pacman -S --needed --noconfirm "${pkgs[@]}"
    elif command -v apt-get >/dev/null; then
        run apt-get install -y dkms build-essential git "linux-headers-$(uname -r)"
    elif command -v dnf >/dev/null; then
        run dnf install -y dkms git make gcc "kernel-devel-$(uname -r)"
    elif command -v zypper >/dev/null; then
        run zypper --non-interactive install dkms git make gcc kernel-default-devel
    else
        die "unknown distribution: install dkms, git, make, gcc and the headers of the running kernel, then retry"
    fi
}

if (( ! DRY )); then [[ $EUID -eq 0 ]] || die "run with sudo (or try --dry-run)"; fi
if (( DRY )); then say "Dry run: nothing is changed"; fi

if (( UNDO )); then
    say "Removing the it87 driver setup"
    if systemctl is-active --quiet fanpilotd 2>/dev/null; then
        run systemctl stop fanpilotd      # hands every fan back to the BIOS first
    fi
    run modprobe -r it87 || true
    run rm -f "$MODCONF" "$LOADCONF"
    if command -v dkms >/dev/null; then
        for v in $(dkms_versions); do
            if pkg_owned "$v"; then continue; fi
            if ! ours "$v"; then echo "    it87 $v was not installed by FanPilot: left alone"; continue; fi
            run dkms remove -m it87 -v "$v" --all || true
            run rm -rf "/usr/src/it87-$v"
        done
    fi
    run rm -rf /var/lib/fanpilot/it87
    say "Done. Reboot or 'sudo systemctl start fanpilotd' (graphics card control keeps working without it87)."
    exit 0
fi

vendor=$(cat "$SYSFS/class/dmi/id/board_vendor" 2>/dev/null || true)
board=$(cat "$SYSFS/class/dmi/id/board_name" 2>/dev/null || true)
visible=0
if chip=$(ite_chip); then
    say "Fan chip already visible as '$chip': nothing to build, only making it load at boot"
    visible=1
elif ! [[ "$vendor" =~ $ITE_VENDORS_RE ]] && (( ! FORCE )); then
    die "this helper is for ITE fan chips; '$vendor $board' usually has another chip.
       Check with 'sudo sensors-detect' (package lm_sensors / lm-sensors). If it reports an ITE IT8xxx chip,
       run again with --force. Otherwise see docs/SETUP.md, 'My motherboard fans are missing'."
fi
echo "    board: $vendor $board"

if (( ! visible )); then
say "Step 1: module option ignore_resource_conflict=1"
writefile "$MODCONF" "# FanPilot: many boards mark the Super I/O chip as ACPI-owned; this lets it87 use it anyway
options it87 ignore_resource_conflict=1"

say "Step 2: try the kernel's built-in it87 driver"
if (( ! DRY )) && modprobe it87 2>/dev/null && chip=$(ite_chip); then
    echo "    built-in driver works: chip '$chip'"
else
    if (( DRY )); then echo "    (dry run: would try 'modprobe it87' and continue below if no chip appears)"; fi
    if (( ! DRY )); then
        modprobe -r it87 2>/dev/null || true
        dmesg | grep -i 'it87' | tail -n 5 | sed 's/^/    dmesg: /' || true
    fi
    say "Step 3: building the out-of-tree driver (frankcrawford/it87, pinned)"
    if command -v dkms >/dev/null; then
        for v in $(dkms_versions); do
            if pkg_owned "$v"; then
                die "an it87 driver from a distribution package is installed ($v). Use that one: make sure it
       is built for the running kernel ('dkms status'), then 'sudo modprobe it87'. FanPilot won't add a second."
            fi
        done
    fi
    if secure_boot_on; then secure_boot_help; fi
    build_deps
    # a previous half-finished run of this script would make 'dkms add' fail
    for v in $(dkms_versions); do
        if ! ours "$v"; then continue; fi
        run dkms remove -m it87 -v "$v" --all || true
        run rm -rf "/usr/src/it87-$v"
    done
    if [[ -d "$SRC/.git" ]]; then
        run git -C "$SRC" fetch --quiet origin
    else
        run mkdir -p "$(dirname "$SRC")"
        run git clone --quiet "$REPO" "$SRC"
    fi
    run git -C "$SRC" checkout --quiet "$PIN"
    if (( ! DRY )); then
        (cd "$SRC" && run ./dkms-install.sh) || die "DKMS build/install failed (see output above)"
        err=$(mktemp)
        if ! modprobe it87 2>"$err"; then
            cat "$err" >&2
            if grep -qi 'key was rejected' "$err"; then secure_boot_help; fi
            rm -f "$err"
            die "the module was built but could not be loaded"
        fi
        rm -f "$err"
        sleep 1
        chip=$(ite_chip) || die "driver loaded but no ITE fan chip appeared; see 'sudo dmesg | grep it87'"
        echo "    out-of-tree driver works: chip '$chip'"
    else
        echo "    \$ (cd $SRC && ./dkms-install.sh); modprobe it87"
    fi
fi
fi   # not visible yet

say "Step 4: load it87 at every boot"
if [[ ! -f "$MODCONF" ]]; then   # also when the chip was made visible by hand (modprobe with the option)
    writefile "$MODCONF" "# FanPilot: many boards mark the Super I/O chip as ACPI-owned; this lets it87 use it anyway
options it87 ignore_resource_conflict=1"
fi
writefile "$LOADCONF" "it87"

if (( ! DRY )); then
    say "Fan readings now:"
    d=$(dirname "$(grep -lE '^it8[0-9]{3}' "$SYSFS"/class/hwmon/hwmon*/name | head -n1)")
    for f in "$d"/pwm[0-9]; do
        i=${f##*pwm}
        printf '    header %s: %s rpm, pwm %s, mode %s\n' "$i" \
            "$(cat "$d/fan${i}_input" 2>/dev/null || echo ?)" "$(cat "$d/pwm$i" 2>/dev/null || echo ?)" \
            "$(cat "$d/pwm${i}_enable" 2>/dev/null || echo ?)"
    done
    if systemctl is-active --quiet fanpilotd 2>/dev/null; then
        run systemctl restart fanpilotd
    fi
fi
say "Done."
