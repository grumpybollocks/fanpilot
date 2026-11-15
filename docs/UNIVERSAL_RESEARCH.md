# Research: FanPilot for any Linux PC (2026-11-15)

Sourced findings that the roadmap (docs/ROADMAP.md) is built on. "Unverified" marks
points the sources did not confirm.

## 1. Name and prior art

**Name.** "FanPilot" is free on AUR, PyPI, Flathub, Debian and Repology, and no trademark or commercial
product was found. GitHub is crowded: about 20 small 2026 repositories use it, three of them Linux fan
tools (youndaimesam/FanPilot: experimental GPU fan controller; mmacity2021-hub/FanPilot: Macs running
Linux; local-inference-lab/fanpilot: GPU servers). Decision: keep the name. Checked-free
alternatives, if ever needed: FanSentry, FanShepherd, CoolWarden.

**Prior art.**

| Project | Licence | Shape | Hardware | Notes |
|---|---|---|---|---|
| CoolerControl | GPL-3.0+ | Rust daemon + web/desktop UI | hwmon, liquidctl, AMD/NVIDIA GPUs | most complete; very active |
| fan2go | AGPL-3.0 | Go daemon + CLI | hwmon, NVML, files | best documented safety: restores pwm_enable on exit, full speed on stale sensor |
| fancontrol / pwmconfig | GPL-2.0 | scripts | hwmon | config keyed by hwmonN, breaks when numbers change |
| thinkfan | GPL-3.0 | C++ daemon | ThinkPad, hwmon | level-based |
| CoreCtrl, LACT | GPL-3.0 / MIT | GUI + helper | GPUs | LACT: RDNA3 fan control "limited" |
| nbfc-linux | GPL-3.0 | service + GUI | laptop ECs | ships a per-model JSON database |
| Fan Control (Rem0o) | no OSS licence | Windows | many | the "HWiNFO-style" Windows reference |

**Cited user complaints FanPilot can answer:** fan numbers don't match board headers (no labels); config
breaks when hwmonN renumbers; board fans not detected (ACPI conflict, wrong driver); RDNA3/4 curves
ignored by firmware zero-RPM; curve measurement heating parts up; no Fan Control-like app on Linux.

Sources: gh search (2026-11-15); aur.archlinux.org RPC; pypi.org; sources.debian.org;
gitlab.com/coolercontrol/coolercontrol; github.com/markusressel/fan2go; github.com/lm-sensors/lm-sensors;
github.com/vmatare/thinkfan; gitlab.com/corectrl/corectrl; github.com/ilya-zlobintsev/LACT;
github.com/nbfc-linux/nbfc-linux; github.com/Rem0o/FanControl.Releases; launchpad.net/bugs/1217889;
forum.manjaro.org/t/108881; bugzilla.kernel.org/show_bug.cgi?id=204807; gabmus.org/posts/rdna3_fan_control_in_linux.

## 2. Motherboard fans (hwmon)

**ABI** (Documentation/ABI/testing/sysfs-class-hwmon): `pwmY` 0-255; `pwmY_enable` 0 = no control (full),
1 = manual, 2+ = automatic, *meaning is driver-specific*; `pwmY_mode` 0 = DC / 1 = PWM; `fanY_input` RPM;
labels exist only when the driver has hints. Every attribute except `name` is optional; RW attributes may
be read-only on some chips. Not guaranteed: that pwmN drives fanN, or that a written enable reads back.

| Driver | Control | Notes |
|---|---|---|
| nct6775 family | yes | 0 full, 1 manual, 2-5 smart modes; ASUS boards via WMI list (no `lax` needed when listed) |
| nct6683 | practically no | pwm writable only on MiTAC customer ID |
| nct6687d (out-of-tree, MSI) | yes | 1 manual, 2 firmware; restores itself; conflicts with nct6683 |
| it87 in-tree | yes | 0..2 only; **manual at 255 reads back 0** (`pwm_mode()`): FanPilot's known quirk; IT8688E/8686E/8665E not in-tree |
| it87 frankcrawford | yes | adds 8655/8665/8686/8688/8689/8696/8698/87952; `ignore_resource_conflict` |
| f71882fg, w83627ehf | yes | 1 manual, 2+ auto modes; EINVAL for unsupported |
| sch5627 | partial | no duty, only "full speed" per fan |
| asus_ec_sensors, asus_wmi_sensors, gigabyte_wmi, hp-wmi-sensors, sch5636 | **no** | read-only: never write |
| dell-smm-hwmon | coarse | 1 = BIOS off + max, 2 = BIOS on; whitelisted machines; SMM calls can stall ~500 ms |
| thinkpad_acpi | yes with `fan_control=1` | 0 full, 1 manual (EC levels 0-7), 2 auto; `fan_watchdog` |
| applesmc | RPM target | enable 1/2, control via `fanN_target` |
| pwm-fan (SoCs) | yes | enable has a different meaning: out of scope |

**Rules taken from this:** treat enable >= 2 as "automatic, chip-specific" and always restore the exact
original value; treat EINVAL as "mode unsupported"; never write read-only drivers; leave `pwm_mode` alone;
re-assert after reclaim and after resume; use kernel watchdogs where present.

**CPU temperature default:** AMD k10temp: Tdie (temp2) if present, else Tctl (temp1; equals the real
temperature on Zen 2+); Intel coretemp: "Package id 0"; zenpower replaces k10temp (labels unverified);
acpitz only as a last fallback.

**Detecting "chip present, no driver":** `/sys/bus/platform/devices/{it87,nct6775,nct6683}.*` without hwmon,
dmesg ACPI resource conflict lines, DMI board name, hwmon names; sensors-detect probes Super-I/O ports
(root port I/O: run it, don't reimplement). `acpi_enforce_resources=lax` is system-wide and has caused
boot failures: never suggest it silently.

**Board label databases and licences:** lm-sensors `configs/` (GPL-2.0; whether "or later" unverified; no
DMI key, board = file name); frankcrawford it87 configs (GPL-2.0, keyed by SIV id, e.g. B550M AORUS ELITE =
60030507/61030507); LibreHardwareMonitor `SuperIOHardware.cs` (MPL-2.0, combinable into GPL-3.0 per MPL
§3.3; register indices, mapping to hwmon numbering unverified). Plan: re-derive labels as facts with
attribution, never copy GPL-2.0-only files.

**Mapping headers to fans (pwmconfig):** all full speed, then one pwm at a time to 0: a fan whose RPM drops
below 5/6 of baseline belongs to it; no change = no fan; restore full speed between steps.

Sources: docs.kernel.org/hwmon/ (sysfs-interface, nct6775, nct6683, it87, f71882fg, w83627ehf, sch5627,
sch5636, asus_ec_sensors, asus_wmi_sensors, hp-wmi-sensors, coretemp, k10temp, dell-smm-hwmon, pwm-fan);
docs.kernel.org/admin-guide/laptops/thinkpad-acpi.html; kernel-parameters.txt; drivers/hwmon/{it87.c,
nct6775-platform.c, nct6683.c, k10temp.c, applesmc.c, dell-smm-hwmon.c, asus-ec-sensors.c};
drivers/platform/x86/gigabyte-wmi.c; github.com/frankcrawford/it87; github.com/Fred78290/nct6687d;
lm-sensors prog/pwm/{pwmconfig,fancontrol}, prog/detect/sensors-detect; LibreHardwareMonitor.

## 3. Packaging

* **PySide6 packages:** Arch `pyside6` 6.12; Debian 13 and Ubuntu >= 24.10 split as
  `python3-pyside6.qtcore/.qtgui/.qtwidgets`; Fedora 43+ `python3-pyside6`; **none on Ubuntu 24.04 and
  Debian 12** → GUI in a venv/pipx there (PEP 668 blocks `pip --user`). The daemon is stdlib-only and runs
  on any python3 >= 3.10.
* **Arch:** PKGBUILD with python-build/installer; unit in /usr/lib/systemd/system; never /usr/local;
  `license=('GPL-3.0-or-later')`; `.SRCINFO`; release package + optional `-git`; 0BSD licence for the PKGBUILD.
* **Debian/Fedora:** for one maintainer, `.deb`/`.rpm` on GitHub releases built with nFPM in Actions;
  proper dh/COPR packaging only if there is demand.
* **pyproject:** `[project.scripts]` fanpilotd/fanpilotctl, `[project.gui-scripts]` fanpilot, PySide6 as an
  optional `gui` extra; pip can't install system units → ship unit/.desktop as package data plus a
  `fanpilotctl install-service` (root) step.
* **Flatpak/AppImage:** not for the daemon (/sys read-only in Flatpak, no system services).
* **DKMS:** Arch `dkms` + matching headers; Debian/Ubuntu `dkms build-essential linux-headers-$(uname -r)`;
  Fedora `dkms kernel-devel`. **Secure Boot:** DKMS signs with /var/lib/dkms/mok.pub, user must
  `mokutil --import` and enrol at reboot; symptom "Key was rejected by service"; it87 upstream documents none
  of this → the helper must detect `mokutil --sb-state` and explain.
* **CI:** GitHub Actions, `QT_QPA_PLATFORM=offscreen`, `libegl1 libgl1 libxkbcommon0 libdbus-1-3`, no tray
  offscreen, sysfs mocked (FanPilot already tests on a simulated tree).

Sources: archlinux.org/packages; packages.debian.org; packages.ubuntu.com; launchpad.net/ubuntu/+source/pyside6;
packages.fedoraproject.org; wiki.archlinux.org/title/AUR_submission_guidelines; manual.archlinux.page
package-guidelines (python, dkms); dh_installsystemd(1); pybuild(1); nfpm.goreleaser.com;
packaging.python.org entry-points; docs.flatpak.org sandbox-permissions; github.com/dkms-project/dkms;
documentation.ubuntu.com secure-boot; wiki.ubuntu.com/UEFI/SecureBoot/DKMS; pytest-qt troubleshooting.

## 4. Graphics cards

**AMD GCN / RDNA1 / RDNA2** (hwmon): `pwm1` 0-255, `pwm1_enable` 0 = "no control" which on swSMU
**forces 100 %**, 1 = manual, 2 = auto. Writability must be checked (the kernel removes the write bit when the
backend has no setter; APUs/no-fan cards hide the files). **If the controller dies, the card stays manual at the
last duty** → needs the restore-on-exit/ExecStopPost path and a re-assert loop. Resume: the kernel's own
restore is unreliable (`smu_restore_dpm_user_profile` resets to AUTO), so re-apply after resume.

**AMD RDNA3 / RDNA4** (PMFW): `pwm1` is read-only on SMU14.0.2 (verified on this machine: no `pwm1_enable`);
control only through `gpu_od/fan_ctrl/` (`fan_curve` 5 points + OD_RANGE, `fan_zero_rpm_enable`, ...),
SMU13 since kernel 6.7, zero-RPM since 6.13, SMU14 files about 6.14-6.15 (unverified). Needs OverDrive:
`amdgpu.ppfeaturemask` bit 0x4000 (this machine: 0xfff7ffff). The set of files varies by PMFW (this card
lacks `fan_zero_rpm_stop_temperature`) → detect per file. The firmware keeps running a committed curve if the
controller dies (safe). LACT gates on drm family >= GC 11 and a readable `fan_curve`.

**NVIDIA proprietary:** NVML via `ctypes.CDLL("libnvidia-ml.so.1")` (no packages, works on Wayland):
temperature and fan % read without root; `nvmlDeviceSetFanSpeed_v2` needs root, Maxwell+, and leaves the
policy manual until `nvmlDeviceSetDefaultFanSpeed_v2` (reported not always to resume the curve on fan 0).
**Nouveau:** effectively read-only. **Intel Arc:** `fan*_input` read-only (xe 6.16+, i915 6.12+), no control;
DG2 can show a 0-RPM phantom channel.

**v1 matrix:** AMD RDNA3/4 with fan_ctrl = control (current code); AMD PMFW without fan_ctrl = read-only + hint
(OverDrive bit); AMD GCN-RDNA2 with writable pwm1 = control, beta, with restore + re-assert; NVIDIA, nouveau,
Intel = read-only; anything else hidden. Detect by `device/vendor`, `device/driver`, file presence and
writability; never write `pwm1_enable=0` on amdgpu.

Sources: docs.kernel.org/gpu/amdgpu/thermal.html; drivers/gpu/drm/amd/pm/{amdgpu_pm.c, swsmu/amdgpu_smu.c,
swsmu/smu11/smu_v11_0.c, swsmu/smu13/smu_v13_0*.c, swsmu/smu14/smu_v14_0_2_ppt.c}; amd_shared.h
(PP_OVERDRIVE_MASK); amd-gfx list 2023-08 (SMU13 fan OD) and 2025-03 (SMU14 zero-RPM stop temp);
phoronix Linux 6.13 zero-RPM; LACT gpu_controller/{amd,nvidia,intel}.rs; docs.nvidia.com/deploy/nvml-api
(Device Commands); forums.developer.nvidia.com/t/214430 and /t/304824; sysfs-driver-intel-{xe,i915}-hwmon;
nouveau_hwmon.c, nvkm/subdev/therm/base.c.
