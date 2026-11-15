# FanPilot — plan

## Goal

The original (version 0.1) plan, written for the reference machine: the three motherboard
headers (CPU_FAN, SYS_FAN1, SYS_FAN2) and the RX 9060 XT. The safety rules [S1]..[S9] below still apply. A KDE-friendly GUI,
a root daemon that keeps working with the GUI closed, and **fail-safe
behaviour above everything else**.

## Architecture

```
 user session                                  root (systemd)
 ┌──────────────────────────────┐   JSON over   ┌───────────────────────────────────┐
 │ fanpilot   (PySide6 GUI+tray)│◄─────────────►│ fanpilotd                         │
 │ fanpilotctl (CLI)            │  unix socket  │  ├ hwmon backend (it87, k10temp…) │
 └──────────────────────────────┘  /run/fanpilot│  ├ amdgpu PMFW backend (fan_curve)│
                                               │  ├ 1 Hz control loop + safety     │
                                               │  └ /etc/fanpilot/config.json      │
                                               └───────────────────────────────────┘
```

* **Daemon** is Python 3, standard library only (no third-party code runs as root).
* **IPC**: Unix socket, newline-delimited JSON. Anyone local may read status.
  Changes require uid 0 or a uid listed in `allowed_uids` (checked via
  `SO_PEERCRED`).
* **Identity**: fans and sensors are addressed as `chip-name/channel`
  (e.g. `it8688/pwm1`, `k10temp/temp1`), never as `hwmonN`, which can change
  between boots.
* The sysfs root is a parameter, so all logic is tested against a simulated
  sysfs tree.

## Fan modes

| Mode | Board fans | GPU |
|---|---|---|
| **Auto** | `pwm_enable` back to its original value (BIOS Smart Fan) | `fan_curve` ← `r` (firmware auto) |
| **Fixed %** | manual PWM | flat 5-point firmware curve |
| **Curve** | 2–8 points, temp source + smoothing + hysteresis + ramp | resampled to the 5 firmware points, written once |

Temperature sources: any hwmon temp, plus "max of" virtual sources
(e.g. max(CPU, GPU junction) for case fans).

## Safety rules (non-negotiable)

1. **Restore on exit.** The original `pwm_enable` values are saved to
   `/run/fanpilot/restore.json` *before* the first write. They are restored on a clean stop, and
   on a crash too (systemd `ExecStopPost=fanpilotd --restore`). The GPU gets `r`.
2. **Watchdog.** systemd `WatchdogSec`. A hung loop is killed, then the fans are restored.
3. **Sensor failure → 100 %** for any board fan whose source can't be read
   for 3 consecutive ticks.
4. **Critical temperature override**: CPU ≥ 90 °C (configurable 75–95) or
   GPU junction ≥ 100 °C → all board fans 100 % until 5 °C below the limit.
5. **Minimum duty floors**: CPU_FAN never goes below 20 % and never stops.
   Other fans can stop only if "allow stop" is enabled for them.
6. **Firmware reclaim**: if `pwm_enable` changes under us, log it and re-assert.
   After 5 reclaims in 60 s, stop fighting, fall back to Auto and warn.
7. **Resume**: detect suspend/resume (a monotonic vs boottime jump) and re-apply.
8. **Validated config**: every value is range-checked. Invalid configs are
   rejected and the old config is kept. The file is written atomically.
9. **GPU rules** from RESEARCH.md §3 (stage all, single commit, `r` on error).

## GUI (PySide6, follows KDE Breeze theme automatically)

* Left: list of fans with live RPM / duty. Right: the selected fan's editor.
* Mode selector (Auto / Fixed / Curve) in plain words.
* Curve editor: drag points, double-click adds, right-click deletes, and a live
  marker shows current temp → duty. Number fields for exact entry.
* Sensors tab: every temperature with friendly labels and a 5-minute graph.
* Presets: Silent / Balanced / Performance (one click, editable afterwards).
* Calibration wizard: per board fan, steps the PWM down, finds the stop/start
  points, aborts if the CPU gets hot. Only runs when the user starts it.
* Tray icon with the hottest temp. Closing the window sends it to the tray.
* Friendly words only (no "pwm_enable" in the main UI; a details tab shows the raw values).

## Phases

0. Research ✔ (RESEARCH.md)
1. **Driver** (needs sudo, the user runs it):
   1. `sudo modprobe it87 ignore_resource_conflict=1` with the in-tree driver.
      `dmesg` then shows either IT8689E (works) or `Unsupported chip (DEVID=0x8688)`.
   2. If 0x8688: `pacman -S dkms linux72-headers`, build frankcrawford/it87 via
      its `dkms-install.sh`, load with `ignore_resource_conflict=1`.
   3. Persist: `/etc/modprobe.d/it87.conf` + `/etc/modules-load.d/it87.conf`.
   Script: `scripts/install-driver.sh` (prints every step, has `--undo`).
2. **Core library + daemon** with simulated sysfs + unit tests.
3. **GUI** + offscreen smoke test + screenshot review.
4. **Install/uninstall** scripts, systemd unit, desktop entry.
5. **Hardware verification** (after Phase 1): read-only first. Then one fan at
   a time: fixed 40 % → 70 % → Auto. kill -9 the daemon → confirm restore. Apply a GPU curve
   → check the `fan_curve` readback → reset.
6. Review of the whole diff, docs updated, one push + draft PR.
