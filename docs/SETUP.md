# FanPilot — setup guide

## 1. Motherboard fan driver (only some boards)

Most boards work out of the box (Nuvoton `nct6775` is in every kernel). Boards with an **ITE** chip
(most Gigabyte, some ASRock and others) need the `it87` driver, often a newer one than the kernel's:

```bash
sudo fanpilot-install-driver          # from a package; from the source folder: sudo scripts/install-driver.sh
fanpilot-install-driver --dry-run     # see every step first, nothing is changed
```

What it does (every command is printed as it runs):

1. Writes `/etc/modprobe.d/fanpilot-it87.conf` with `options it87 ignore_resource_conflict=1` (many boards
   mark the chip as ACPI-owned although ACPI doesn't use it). The system-wide
   `acpi_enforce_resources=lax` is never touched.
2. Tries the kernel's built-in `it87` (enough for e.g. the IT8689E).
3. Otherwise installs `dkms` and the running kernel's headers (pacman, apt, dnf or zypper) and builds
   [frankcrawford/it87](https://github.com/frankcrawford/it87) at a pinned commit; DKMS rebuilds it on
   kernel updates. On Arch/Manjaro it refuses if the repository's headers don't match the running kernel
   (update, reboot, run again). **Secure Boot:** it detects it and shows how to enrol the DKMS key once
   (`mokutil --import`, then "Enroll MOK" at the next boot).
4. Writes `/etc/modules-load.d/fanpilot-it87.conf` so the driver loads at boot.

Boards from vendors that rarely use ITE chips need `--force`, after `sudo sensors-detect` confirmed an ITE
chip. Undo everything: `--undo`.

## 2. Install FanPilot

| Distribution | How |
|---|---|
| Arch, Manjaro, EndeavourOS | AUR package coming soon; until then the source install below. App: `sudo pacman -S pyside6` |
| Debian 13+, Ubuntu 24.10+ | `sudo apt install ./fanpilot_1.0.1_all.deb` from the [latest release](https://github.com/grumpybollocks/fanpilot/releases/latest) |
| Fedora 43+ | `sudo dnf install ./fanpilot-1.0.1-1.noarch.rpm` from the [latest release](https://github.com/grumpybollocks/fanpilot/releases/latest) |
| Ubuntu 24.04, Debian 12, anything else | from the source folder: `sudo ./install.sh --gui-venv --autostart` (the .deb installs there too, but those releases have no PySide6 package, so the app would not start) |

After a **package** install: `sudo usermod -aG fanpilot $USER` (log out and in, so your user may change
settings), then `sudo systemctl enable --now fanpilotd`.

The **source installer** (`sudo ./install.sh [--autostart] [--gui-venv]`) runs the test suite first (on
simulated hardware, as your user), copies the app to `/opt/fanpilot`, allows the user who ran it to change
settings, and enables the service. `--gui-venv` downloads the app's toolkit (PySide6) into
`/opt/fanpilot/venv` for distributions without a PySide6 package. Don't mix a package and the source
installer: `sudo ./uninstall.sh` first (your settings in `/etc/fanpilot` are kept).

Every fan starts on **Automatic**: nothing changes until you choose.

## 3. First use (recommended order)

1. Open **FanPilot** from the app menu (for a desktop icon: `cp /usr/share/applications/fanpilot.desktop ~/Desktop/`).
   Check that the *Sensors* tab shows sensible values. Right-click a sensor for alarms or to show it on the tray icon.
2. **Find out which fan is on which header**: select *System fan 1* and click **Identify**.
   That header runs slow for 3 seconds, then at full speed for 3 seconds. Repeat for *System fan 2*.
3. **Measure** each system fan (*Measure this fan…*, about 1–2 minutes, PC idle). Then click *Use as lowest
   speed* so FanPilot never asks for a speed at which the fan stalls.
4. Quickest start: pick **Silent**, **Balanced** or **Performance** in the *Profile* box at the top.
   For finer control, choose a curve per fan: pick a **Preset**, then drag points. Double-click adds a point; right-click
   removes one. The dashed line shows the current temperature.
5. Case fans: under *Follows*, tick **CPU (Tctl)** and **GPU hotspot** so the case fans react to
   whichever is hotter (useful while gaming or running AI models on the GPU).
6. Happy with it? *Settings → Save current settings as…* stores it as your own profile
   (switch profiles from the tray icon too).
7. Graphics card: *Temperature curve* uses the GPU hotspot. Untick *zero RPM* under *Fine tuning* if
   you want the fan to always spin.

## Command line

```bash
fanpilotctl status
fanpilotctl set it8688/pwm2 fixed 45
fanpilotctl set it8688/pwm2 curve 40:30,60:45,75:70,85:100
fanpilotctl set amdgpu/fan1 auto
fanpilotctl calibrate it8688/pwm3          # stop it with: fanpilotctl calibrate --stop
fanpilotctl sensors -w                     # every reading, refreshed each second
fanpilotctl identify it8688/pwm2           # slow 3 s, then full speed 3 s
fanpilotctl profile list | apply Silent | save Night | delete Night
fanpilotctl config > my-fans.json          # backup
fanpilotctl import my-fans.json            # restore
fanpilotctl events
journalctl -u fanpilotd -f
```

(Use `it8689/…` instead if your chip turned out to be an IT8689E. `fanpilotctl status` shows the ids.)

## Self-test on the real hardware

Easiest: **Settings → Self-test → Run self-test** in the app (or `fanpilotctl selftest`). No password
needed, about 45 s, with a ✔/✖ per check. It runs inside the service and tests the crash recovery by running
the same restore routine systemd runs after a crash.

On the **reference board** (Gigabyte B550M AORUS ELITE) a fuller version, which really kills the service
with SIGKILL, needs root (on other boards it stops and points to `fanpilotctl selftest`):

```bash
sudo scripts/selftest.sh
```

About 90 seconds. It checks the driver and the service, speed control on SYS_FAN1 and SYS_FAN2
(100 % vs 35 %), hand-back to the BIOS, a **crash test** (kills the service with SIGKILL and checks that
the fans go back to the BIOS and the service restarts), and the GPU firmware curve. The CPU fan
is only read, never changed, and your settings are put back at the end. Run it again after BIOS
updates or kernel-series changes.

## Sensors for other programs (G510 keyboard LCD)

While running, FanPilot rewrites `/run/fanpilot/sensors.tsv` once a second (in RAM, readable by
everyone). Every line is `key, kind (temp/rpm/pct), short label, label, value`, separated by tabs,
after a `# fanpilot-sensors 1 <unix time>` header. The G510 LCD app lists these in its Custom
Screens dropdown as "FanPilot: …". The file is removed when FanPilot stops, so readers show N/A.

## Any other PC

FanPilot works on any desktop whose motherboard fan chip has a Linux driver that allows control:
ITE `it87` (many Gigabyte, ASRock and older boards), Nuvoton `nct6775` family (most ASUS and many MSI,
ASRock boards), Nuvoton `nct6687` (newer MSI boards, out-of-tree driver), Fintek `f71882fg`, Winbond
`w83627ehf`. Other chips are shown read-only, never written to.

* **Boards FanPilot knows** (see `fanpilot/boards.py`) get the headers' real names (CPU_FAN, SYS_FAN1...),
  the connected temperature channels and the voltages.
* **Any other board** works generically: fans are called "Fan 1", "Fan 2"... (or the driver's label), and
  every motherboard fan gets the CPU fan's protection (never stops, never below 20 %) until you tell
  FanPilot which one is the CPU fan: use *Identify*, then right-click that fan → *This is a CPU fan*
  (or `fanpilotctl cpu-fan <id>`). Right-click any fan or sensor to rename it.
* **Map my fans** (*Settings*, or `fanpilotctl map`): if speeds look wrong or a header shows a fan that
  isn't there, this finds which header drives which fan (about 15 s per header; no fan is stopped).
* Send `fanpilotctl report` with any bug report: it is anonymised and becomes a test machine.

### Graphics cards

| Card | What FanPilot does |
|---|---|
| AMD RX 7000 / 9000 with OverDrive | Temperature curve stored in the card's firmware (5 points), zero-RPM optional |
| AMD RX 7000 / 9000 without OverDrive | Shown read-only, with a hint: add the OverDrive bit (0x4000) to the `amdgpu.ppfeaturemask` kernel parameter |
| AMD up to RX 6000 | **Beta:** controlled like a motherboard header (it follows its own temperature by default); handed back to the card on every exit |
| NVIDIA (proprietary driver) | Read-only temperature and fan % through `nvidia-smi`; it also counts for the GPU emergency limit |
| NVIDIA nouveau, Intel Arc | Read-only, as far as the kernel reports anything |

Several cards are supported; each gets its own id (e.g. `amdgpu@0000:0b:00.0/fan1`).

### My motherboard fans are missing

FanPilot shows hints in the window, in `fanpilotctl status` and in the self-test. In short:

1. `sudo sensors-detect` (package lm_sensors / lm-sensors) tells you which chip the board has and which
   driver it needs. Load that driver (`sudo modprobe nct6775`, `it87`...).
2. **Gigabyte** (ITE IT8686E/IT8688E/IT8689E...): needs the out-of-tree it87 driver:
   `sudo fanpilot-install-driver` (package) or `sudo scripts/install-driver.sh` (source folder).
3. **MSI** with nct6683 in `fanpilotctl report`: that driver only reads fans; the out-of-tree
   [nct6687d](https://github.com/Fred78290/nct6687d) driver adds control.
4. **"ACPI resource conflict"** in `sudo dmesg`: the board's firmware also uses the chip. For it87 there is
   the per-driver option `ignore_resource_conflict=1`. The system-wide `acpi_enforce_resources=lax`
   affects every driver and has stopped some PCs from booting: only try it if you can undo it from the
   boot menu.
5. Laptops (Dell, ThinkPad...) are read-only in FanPilot; use thinkfan or nbfc-linux for their fans.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Motherboard fans not found" | Run `sudo fanpilot-install-driver` (package) or `sudo scripts/install-driver.sh`; check `sudo dmesg \| grep it87` |
| "View only" banner | Your user may not change settings: `sudo usermod -aG fanpilot $USER` and log out and in (packages), or re-run `sudo ./install.sh` (source install) |
| Log says "control was taken back" | BIOS Smart Fan is fighting FanPilot. In BIOS → Smart Fan 5, set that header to *Manual* or *Full speed*, then let FanPilot control it |
| GPU: "commit failed … firmware rejected the table" | `journalctl -k \| grep -i overdrive`; reboot if it mentions `OD_UNSUPPORTED_FEATURE` |
| GPU fan controls missing | OverDrive must be on: `options amdgpu ppfeaturemask=0xFFF7FFFF` in `/etc/modprobe.d/99-amdgpu-overdrive.conf`, then rebuild the initramfs (`sudo mkinitcpio -P` / `sudo update-initramfs -u` / `sudo dracut -f`) and reboot — or add `amdgpu.ppfeaturemask=0xFFF7FFFF` to the kernel command line |
| "… has stopped (0 RPM)" warning | the fan is blocked, unplugged or broken; check it. The CPU fan warning is urgent |
| Fans loud after suspend | FanPilot re-applies settings after resume within a second; check the Log tab |

## Uninstall

Stopping the service hands every fan back to the BIOS / card first, on every route.

```bash
sudo fanpilot-install-driver --undo      # first: the it87 driver setup, if you used it (the helper goes with the package)
sudo apt remove fanpilot                 # package install (dnf: sudo dnf remove fanpilot / pacman: sudo pacman -R fanpilot)

sudo ./uninstall.sh                      # source install; add --purge to delete /etc/fanpilot too
sudo scripts/install-driver.sh --undo
```
