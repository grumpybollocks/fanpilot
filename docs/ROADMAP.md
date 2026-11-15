# FanPilot roadmap: from "most desktops" to as universal as Linux allows

Status 0.7.4: motherboard fans on the common Super I/O drivers (it87, nct6775 family, nct6687, f71882fg,
w83627ehf), AMD RX 7000/9000 firmware curves, older AMD cards (beta), NVIDIA and laptops read-only; Arch,
.deb, .rpm and source installs. Research behind this plan (2026-11, with sources): see the end of this page and
docs/UNIVERSAL_RESEARCH.md.

## Rules that do not change

1. **Never less safe than leaving the fan alone.** Unknown or undocumented drivers are shown read-only. A
   device class is only controllable when its kernel documentation says what every value means, what happens
   on exit and on suspend, and FanPilot has a simulated tree of it with fault injection.
2. **Every controlled fan has a defined failsafe** for a clean exit, a crash (ExecStopPost) and a lost
   service: back to firmware control, or full speed where the device has no firmware fallback.
3. **Coexist, don't fight.** If another tool owns a fan (asusctl, nbfc-linux, thinkfan, fw-fanctrl,
   CoolerControl, LACT), FanPilot shows "managed by …" and leaves it alone.
4. **Every reported machine becomes a test** (`fanpilotctl report` → tests/reports → simulator).

## The one refactor that unlocks the rest: device classes + a driver table

Today the allow-list is a regular expression and the semantics live in code. Replace it with data:

| Class | Write | Hand back | Failsafe without the service | Examples |
|---|---|---|---|---|
| `pwm+enable` (today) | pwmN, enable=1 | original enable value | firmware | it87, nct6775, nct6687, f71882fg, w83627ehf, gpd-fan, oxpec, cros_ec |
| `pwm-only` | pwmN | last firmware duty, else 100 % | none: ExecStopPost sets 100 % | nzxt-smart2, corsair-cpro, arctic_fan_controller |
| `firmware-curve` | whole curve, one commit | reset to defaults | firmware keeps the curve | amdgpu OverDrive (today), asus_custom_fan_curve, nzxt-kraken3 |
| `target-rpm` | fanN_target | enable off / auto | firmware | lenovo-wmi-other, corsair-cpro |
| `levels` | level 0–7 / auto | auto | EC watchdog (thinkpad `fan_watchdog`) | thinkpad_acpi |
| `helper` | separate root process | its own restore | helper death → default/100 % | NVIDIA NVML, liquidctl devices |

Each driver entry: chip/driver name, class, enable value meanings (manual / firmware / full / **forbidden**, e.g.
pwm-fan `0` stops the fan), minimum write interval (kraken3 locks up if spammed), keep-alive period (hp-wmi:
firmware reverts after ~120 s), known quirks (it87 manual-255 reads 0), suspend behaviour, documentation link.
Tests: one simulated tree per entry built from docs.kernel.org, plus fault injection (EINVAL, EOPNOTSUPP,
EIO, write-only enable, slow writes).

## Milestones

| # | Milestone | Contents | Effort | Risk | Tested without the hardware by |
|---|---|---|---|---|---|
| 1.0 | **Publish** | clean public repo, generic README/landing page, supported-hardware table generated from tests/reports, issue templates, CHANGELOG, GitHub Pages, v1.0.0 release, AUR | S | low | CI (tests, packages, container installs) |
| 1.1 | **Safety parity** | stale-sensor policy per fan, stall detection (RPM 0 while duty > stop duty → raise floor, alarm), "someone else changed this fan" detection, conflict detection by systemd unit + process, disk temperatures (drivetemp/NVMe) as curve inputs, GNOME "no tray → install AppIndicator" hint | S–M | low | simulator with a third-party writer thread, stall model |
| 1.2 | **Driver table + new desktop devices** | the refactor above; add gpd-fan, cros_ec_hwmon (Framework), oxpec, ayaneo-ec, nzxt-smart2, corsair-cpro, asus_rog_ryujin, arctic_fan_controller | M | low–medium | per-driver simulated trees; ExecStopPost full-speed test for `pwm-only` |
| 1.3 | **Firmware curves** | generalise the amdgpu curve code: asus_custom_fan_curve (8 points, enable 1/2/3, userspace validation, profile change disables it → detect), nzxt-kraken3 (40 points, rate-limited) | M | medium | curve-validation property tests; simulated profile switch |
| 1.4 | **Laptops, opt-in** | thinkpad_acpi with `fan_control=1` and `fan_watchdog` as dead-man switch; dell-smm (writes in a worker thread, ~500 ms stalls); hp-wmi Victus/OMEN (keep-alive); lenovo-wmi-other (target RPM, -EIO models); defer to nbfc-linux where it runs | M | medium | simulated trees with latency + error injection; `/proc/acpi/ibm/fan` model |
| 1.5 | **Calibration wizard** | all fans: stop/start duty, duty→RPM curve, used by curves and stall detection (extends Map my fans) | M | low–medium | simulator fans with stall threshold, lag, noise |
| 1.6 | **NVIDIA control (beta, opt-in)** | separate root helper using NVML (`nvmlDeviceSetFanSpeed_v2`, R515+, Maxwell+); restore with `SetDefaultFanSpeed_v2` and verify (known to not always resume) → fall back to a safe fixed speed and say so; helper death = restore | M | medium–high | fake `libnvidia-ml.so.1` stub built in CI; return-code matrix; kill-the-helper test |
| 1.7 | **USB AIOs and hubs** | optional liquidctl backend in its own process (GPL-3.0 compatible), only for devices without a kernel driver | L | medium | fake liquidctl driver objects |
| 2.0 | **Reach** | Fedora COPR, openSUSE OBS (+ Debian), NixOS module, PyPI + `fanpilotctl install-service`, GUI-only Flatpak talking to the host service through `/run/fanpilot`, systemd-sysext image for immutable distros (Bazzite, Silverblue, SteamOS), translations (Qt Linguist), accessibility (names for custom widgets, keyboard editing of curve points) | M each | low | container installs per distro in CI; pseudo-locale layout test; accessibility-name test |

## Deliberately not planned

* Writing `thermal_zone*/mode` or `cooling_device*/cur_state` (the kernel governor fights it; disabling the
  zone also disables the kernel's own trip protection).
* pwm-fan control on ARM boards by default (its `pwm1_enable=0` **stops** the fan, the opposite of desktop
  chips): read-only, later opt-in only.
* Intel Arc fan control until it is in mainline (patched out-of-tree xe modules exist; FanPilot stays read-only).
* Mainline nct6683 writes (pwm is writable only for MiTAC boards); MSI boards keep using nct6687d (DKMS).

## How hardware coverage grows

1. A user runs `fanpilotctl report` and attaches it to an issue (template asks for it).
2. The report becomes `tests/reports/<board>.json`; the simulator replays it; the board's names and channels
   go into `fanpilot/boards.py`.
3. The supported-hardware table on the landing page is generated from tests/reports, so it can't claim more
   than what is tested.
4. Driver helper grows with the chips people report (today: ITE through frankcrawford/it87; next: nct6687d
   for MSI).

## Sources (2026-11)

* NVML fan commands (R515+, root): https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html ;
  restore bug: https://forums.developer.nvidia.com/t/nvmldevicesetdefaultfanspeed-v2-does-not-resume-fan-speed-algorithm-please-fix/214430
* Intel Arc read-only in mainline: https://github.com/ilya-zlobintsev/LACT/blob/master/docs/Hardware-Support.md
* Kernel driver docs: https://docs.kernel.org/hwmon/ (gpd-fan, cros_ec_hwmon, nzxt-kraken3, nzxt-smart2,
  corsair-cpro, asus_rog_ryujin, dell-smm-hwmon, pwm-fan)
* asus-wmi custom fan curves: https://lore.kernel.org/platform-driver-x86/20210904043250.134260-1-luke@ljones.dev/T/
* HP Victus fan control: https://phoronix.com/news/hp-victus-s-linux-fan-control ;
  Arctic controller: https://www.phoronix.com/review/arctic-fan-controller
* Thermal sysfs ABI: https://www.kernel.org/doc/Documentation/ABI/testing/sysfs-class-thermal
* Prior art: CoolerControl https://docs.coolercontrol.org , fan2go https://github.com/markusressel/fan2go ,
  nbfc-linux https://github.com/nbfc-linux/nbfc-linux , LACT https://github.com/ilya-zlobintsev/LACT ,
  liquidctl https://github.com/liquidctl/liquidctl
* Flatpak sandbox: https://docs.flatpak.org/en/latest/sandbox-permissions.html ; COPR example:
  https://copr.fedorainfracloud.org/coprs/codifryed/CoolerControl ; sysext: https://fedora-sysexts.github.io
