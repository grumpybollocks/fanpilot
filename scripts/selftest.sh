#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Hardware test for the REFERENCE board (Gigabyte B550M AORUS ELITE, IT8688E/IT8689E, AMD card on card1),
# about 90 s, PASS/FAIL per check. On any other PC use the in-app self-test: `fanpilotctl selftest`
# (Settings -> Run self-test), which works on every supported board.
#
#   sudo scripts/selftest.sh
#
# Touches only SYS_FAN1, SYS_FAN2 and the GPU fan, briefly, and puts every one of them back
# to its saved setting at the end (also if interrupted). The CPU fan is only read, never changed.
# Includes a crash test: kills the service with SIGKILL and checks that systemd hands the
# fans back to the BIOS and restarts the service.
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
command -v fanpilotctl >/dev/null || { echo "FanPilot is not installed (sudo ./install.sh)" >&2; exit 1; }
if ! grep -qE '^it86(88|89)' /sys/class/hwmon/hwmon*/name 2>/dev/null; then
    echo "This script tests the reference board only. On this PC run: fanpilotctl selftest" >&2
    exit 2
fi

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; pass=$((pass + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=$((fail + 1)); }
info() { printf '\n\033[1m%s\033[0m\n' "$*"; }
rd()   { cat "$1" 2>/dev/null || echo -1; }

saved=$(mktemp -p "$HOME" .fanpilot-selftest-backup-XXXXXX.json)
fanpilotctl config > "$saved" || { rm -f "$saved"; echo "service not reachable" >&2; exit 1; }
restore() {
    systemctl start fanpilotd 2>/dev/null
    for _ in $(seq 20); do fanpilotctl status >/dev/null 2>&1 && break; sleep 0.5; done
    if fanpilotctl import "$saved" >/dev/null 2>&1; then
        echo "  (your settings were restored)"
        rm -f "$saved"
    else
        echo "  !! could not restore your settings automatically; they are kept in $saved" >&2
        echo "     restore them with: fanpilotctl import $saved" >&2
    fi
}
trap restore EXIT

info "1. Service and hardware"
systemctl is-active --quiet fanpilotd && ok "fanpilotd is running" || bad "fanpilotd is not running"
chipfile=$(grep -lE '^it86(88|89)' /sys/class/hwmon/hwmon*/name | head -n1)
if [[ -n "$chipfile" ]]; then
    d=$(dirname "$chipfile"); ok "motherboard chip: $(cat "$chipfile")"
else
    bad "motherboard chip not found (scripts/install-driver.sh)"; d=""
fi
chip=$(fanpilotctl status | awk '/board chip:/{print $5}')
gpu=/sys/class/drm/card1/device/gpu_od/fan_ctrl
[[ -f $gpu/fan_curve ]] && ok "GPU fan curve interface present" || bad "GPU fan curve interface missing"

if [[ -n "$d" ]]; then
    for i in 2 3; do
        info "2.$((i - 1)) System fan $((i - 1)) (header $i)"
        fanpilotctl set "$chip/pwm$i" fixed 100 >/dev/null; sleep 7
        hi=$(rd "$d/fan${i}_input")
        fanpilotctl set "$chip/pwm$i" fixed 35 >/dev/null; sleep 8
        lo=$(rd "$d/fan${i}_input"); en=$(rd "$d/pwm${i}_enable")
        if (( hi <= 0 )); then
            bad "no fan detected on this header (0 RPM at 100 %) — skip if nothing is plugged in"
        elif [[ $en == 1 ]] && (( lo < hi * 85 / 100 )); then
            ok "speed control works: 100 % = $hi RPM, 35 % = $lo RPM"
        else
            bad "speed did not drop: 100 % = $hi RPM, 35 % = $lo RPM, mode $en"
        fi
        fanpilotctl set "$chip/pwm$i" auto >/dev/null; sleep 3
        [[ $(rd "$d/pwm${i}_enable") == 2 ]] && ok "handed back to BIOS (mode 2)" || bad "not handed back (mode $(rd "$d/pwm${i}_enable"))"
    done

    info "3. Crash test (kill -9 while a fan is under manual control)"
    fanpilotctl set "$chip/pwm2" fixed 40 >/dev/null; sleep 3
    [[ $(rd "$d/pwm2_enable") == 1 ]] || bad "fan was not under manual control before the kill"
    # Kill only the main process, like a real crash / OOM kill. (Plain 'systemctl kill' signals every
    # process of the unit, which also kills the ExecStopPost restore that systemd starts at that moment.)
    systemctl kill --kill-whom=main -s KILL fanpilotd
    # ExecStopPost hands the fan back at once; ~3 s later systemd restarts the service, which
    # re-applies "fixed 40". So watch closely during the first 3 seconds.
    back=no
    for _ in $(seq 15); do
        [[ $(rd "$d/pwm2_enable") == 2 ]] && { back=yes; break; }
        sleep 0.2
    done
    [[ $back == yes ]] && ok "after SIGKILL the fan went back to the BIOS (mode 2)" \
                       || bad "after SIGKILL the fan stayed in mode $(rd "$d/pwm2_enable")"
    for _ in $(seq 20); do systemctl is-active --quiet fanpilotd && break; sleep 0.5; done
    systemctl is-active --quiet fanpilotd && ok "systemd restarted the service" || bad "service did not restart"
    for _ in $(seq 10); do fanpilotctl status >/dev/null 2>&1 && break; sleep 0.5; done
    fanpilotctl set "$chip/pwm2" auto >/dev/null 2>&1
fi

if [[ -f $gpu/fan_curve ]]; then
    info "4. Graphics card fan"
    fanpilotctl set amdgpu/fan1 fixed 40 >/dev/null; sleep 10
    g=$(dirname "$(grep -l '^amdgpu' /sys/class/hwmon/hwmon*/name | head -n1)")
    rpm=$(rd "$g/fan1_input")
    grep -q '^0: 25C 40%' "$gpu/fan_curve" && ok "curve written to the card firmware" || bad "firmware table not as written"
    (( rpm > 0 )) && ok "GPU fan spinning at 40 %: $rpm RPM" || bad "GPU fan not spinning at 40 % ($rpm RPM)"
    fanpilotctl set amdgpu/fan1 auto >/dev/null; sleep 3
    grep -q '^0: 0C 0%' "$gpu/fan_curve" && ok "handed back to the card's automatic control" || bad "card table not reset"
fi

info "Result: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
