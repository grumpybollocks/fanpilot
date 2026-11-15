# FanPilot — research notes

Everything below was checked on the reference machine (2026-11-14) or
read from primary sources (kernel docs, driver source, vendor spec pages).
Items marked **UNVERIFIED** still need a test on real hardware.

## 1. The machine

| Part | Value | How verified |
|---|---|---|
| Motherboard | Gigabyte **B550M AORUS ELITE**, BIOS F22d (2026-08-18) | `/sys/class/dmi/id/*` |
| CPU | AMD Ryzen 7 5800X3D | `/proc/cpuinfo` |
| CPU temp | `k10temp`: `Tctl`, `Tccd1` | hwmon |
| GPU | AMD Radeon RX 9060 XT (Navi 44, RDNA4, `1002:7590`) | `lspci -nn` |
| Kernel | 7.2.9-1-MANJARO (package `linux72`) | `uname -r`, `pacman -Qo` |
| Desktop | KDE Plasma 6.7.5, Python 3.14, PySide6 6.11 (system package) | `plasmashell --version`, import test |
| Other hwmon | `gigabyte_wmi` (6 temps, no fans), `nvme`, `acpitz`, `iwlwifi` | `/sys/class/hwmon/*/name` |
| USB | ITE `048d:5702` = RGB controller only (no fans), no AIO / fan hub | `lsusb` |

### Fan headers (motherboard)

Gigabyte's spec page for B550M AORUS ELITE (rev 1.0–1.2) and the manual layout:
**1× CPU_FAN, 2× system fan (SYS_FAN1, SYS_FAN2)**, all 4-pin, up to 2 A / 12 V.
Source: <https://www.gigabyte.com/Motherboard/B550M-AORUS-ELITE-rev-10-11-12/sp>

## 2. Why nothing works out of the box

* **No motherboard fan driver is loaded.** The board's fans sit behind an ITE
  Super I/O chip. The stock kernel `it87` module is never auto-loaded (ISA
  chips have no modalias), and the in-tree driver knows only these chips:
  `IT8603E IT8620E IT8622E IT8628E IT8689E IT87xx…`. It does **not** know IT8688E
  (checked with `strings` on `/lib/modules/7.2.9-1-MANJARO/kernel/drivers/hwmon/it87.ko.zst`).
* The out-of-tree driver <https://github.com/frankcrawford/it87> (active,
  last commit 2026-10-02) supports IT8688E. Its sensors config
  `Sensors configs/Gigabyte/configs/gigabyte-it87-amd.conf` lists
  **B550M AORUS ELITE under two firmware variants**: SIV `0x60030507` (it8688)
  and `0x61030507` (it8689). Both use this mapping:

  | hwmon | label |
  |---|---|
  | fan1/pwm1 | CPU_FAN |
  | fan2/pwm2 | SYS_FAN1 |
  | fan3/pwm3 | SYS_FAN2 |
  | temp1 | System |
  | temp2 | VSOCMOS |
  | temp3 | CPU |
  | temp5 | VRM MOS |
  | temp6 | PCH |
  | fan4–6, pwm4–6, temp4 | not connected |

  **UNVERIFIED: which variant this board has.** If it is IT8689E, the in-tree
  driver already works. If it is IT8688E, the DKMS driver is required. One
  `modprobe` settles it (see PLAN.md, Phase 1).
* This board is **not** in the driver's Gigabyte DMI table, which is where the driver
  turns on "ignore ACPI resource conflict" automatically. So the module option
  `ignore_resource_conflict=1` must be given explicitly. The kernel docs
  warn this can race with ACPI. In practice the driver already applies this setting
  automatically to 20+ sibling Gigabyte boards (X570/B550 AORUS etc.).
  Source: <https://docs.kernel.org/hwmon/it87.html>, `it87.c` `it87_dmi_table`.
* it87 `pwmN_enable`: **0 = full speed, 1 = manual, 2 = automatic (BIOS
  Smart Fan)** (`it87.c` `set_pwm_enable`, accepts 0–2 only).
* Community report (gist bakman2): on a B550 AORUS ELITE AX V2 the BIOS Smart
  Fan sometimes fought software PWM. **UNVERIFIED on this board.** FanPilot
  detects and logs "firmware reclaim" instead of assuming.

## 3. GPU fan (RDNA4)

The legacy hwmon `pwm1` is read-only here (`-r--r--r--`, no `pwm1_enable`).
RDNA3/4 use the firmware (PMFW) OverDrive interface, which exists on this card:

```
/sys/class/drm/card1/device/gpu_od/fan_ctrl/
  fan_curve                       5 points, hotspot 25–100 °C, speed 20–100 %
  fan_zero_rpm_enable             currently 1
  fan_minimum_pwm, fan_target_temperature,
  acoustic_limit_rpm_threshold, acoustic_target_rpm_threshold  (auto-mode knobs)
```

OverDrive is already enabled: `/etc/modprobe.d/99-amdgpu-overdrive.conf`
sets `amdgpu ppfeaturemask=0xFFF7FFFF` (bit 0x4000 set).

Kernel docs (<https://docs.kernel.org/gpu/amdgpu/thermal.html>):
* `fan_curve` write format `"<index> <temp> <speed>"`, then `c` to commit,
  `r` to reset. Writing the curve **switches the fan to manual mode**.
* The acoustic / target-temp / minimum-pwm knobs work in auto mode only and
  switch the fan back to auto.

Lessons from CoolerControl's source (`coolercontrold/.../gpu/amd.rs`):
* Stage **zero-RPM and all 5 curve points, then commit once**. PMFW resets any
  feature that wasn't flagged in the same upload, so a curve committed alone turns
  zero-RPM back on, and zero-RPM committed alone drops the curve.
* Speeds are **percent**, clamped to the advertised range (20–100).
* A half-staged table (unstaged points stay 0 °C/0 %) is rejected with `EIO`.
  Recover with `r` (restore also commits).
* `EIO` on commit means PMFW rejected the table. The reason is in the kernel log
  ("Invalid overdrive table content").
* Only write zero-RPM when its file shows the `FAN_ZERO_RPM_ENABLE:` header.
  Writing an unsupported OD feature can disable OverDrive until reboot.

Because the curve runs **inside the GPU firmware**, the GPU stays safe
even if FanPilot crashes. FanPilot does not run a software loop for the GPU.

## 4. Existing software surveyed

| Project | What it does | Why not enough here / what we take |
|---|---|---|
| [CoolerControl](https://gitlab.com/coolercontrol/coolercontrol) | Rust daemon + web/desktop UI, hwmon + liquidctl + GPUs | Best in class, and it works once the driver is loaded. It is heavy, though (daemon, web UI, auth). We take its RDNA4 commit rules and its "firmware reclaim" detection idea. |
| [LACT](https://github.com/ilya-zlobintsev/LACT) | AMD/NVIDIA/Intel GPU tool | GPU only. Confirms the RDNA4 curve path. |
| [fan2go](https://github.com/markusressel/fan2go) | Go daemon, YAML curves | No GUI. We take two ideas: fan calibration (start/stop PWM), and identifying fans by hwmon *name + channel* instead of the `hwmonN` index. |
| lm_sensors `fancontrol`/`pwmconfig` | Shell daemon | No GUI, fragile hwmonN paths, no GPU PMFW support. |
| CoreCtrl | GPU profiles | GPU only. |

The gap is a small, transparent tool made for this board and card that:
installs the right driver, labels headers correctly, gives the GPU's firmware
curve and the board fans one simple GUI, and always fails safe.

## 5. Open questions (to verify on hardware)

1. IT8688E vs IT8689E (one `modprobe`).
2. Which physical fan is on which header, and each fan's stop/start PWM
   (FanPilot calibration wizard).
3. Whether BIOS Smart Fan reclaims manual PWM (FanPilot logs reclaims).
4. Behaviour after suspend/resume. The it87 ISSUES file reports a BIOS bug on
   an older Gigabyte board; FanPilot re-applies after resume.
