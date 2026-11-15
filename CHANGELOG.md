# Changelog

## Unreleased

* **Sensors, any board with a Nuvoton chip (most ASUS, MSI, ASRock):** plain names for the chip's inputs, raw
  name kept in brackets (`TSI0_TEMP` → *CPU (AMD TSI 0)*, `SYSTIN` → *Motherboard (SYSTIN)*, `CPUTIN` → *CPU
  socket*, `AUXTINn`, `SMBUSMASTER n`, `PCH_*`), no more chip-name prefix.
* **Voltages on boards not in the database:** the fixed-function pins of NCT6779 and later (CPU Vcore, AVSB,
  +3.3V, +3.3V standby, CMOS battery), which the driver scales itself, with the +-10 % / low-battery alert.
  Board-wired inputs (+12V, +5V, ...) still need the board database: raw values would mislead.
* **More drivers:** RAM module temperatures (`jc42`, `spd5118`) in the Memory group; `drivetemp` drives and
  Intel chipset (`pch_*`) sensors get plain names.


* **Emergency mode covers the graphics card too:** above a CPU or GPU emergency limit, an AMD RX 7000/9000
  card's fan now also runs at full speed (before, only motherboard fans did); it returns to its setting after.
* Writes only to the Fintek and Winbond chips their drivers really cover (f71882fg / w83627ehf chip lists);
  other chips of those makers are shown read-only.
* The app launcher explains a missing PySide6 (message + desktop notification) instead of failing silently.
* A malformed (deeply nested) request no longer writes a traceback to the journal; `fanpilotctl selftest` and
  `fanpilotctl map` stop cleanly if the service restarts meanwhile.
* Docs: download links and step-by-step install per distribution, the AUR package marked as coming, the
  OverDrive fix includes rebuilding the initramfs, corrected claims (the GPU curve returns to the card's
  control when FanPilot stops), SECURITY.md; release notes come from this file.

**Found on a second real machine** (ASUS TUF GAMING X570-PLUS, Nuvoton NCT6798, AMD RX 9070 XT); its report is
now a test machine (`tests/reports/asus-tuf-gaming-x570-plus.json`).

* **Sensors:** on boards not in the database, channels reading exactly 0 °C are treated as unconnected
  (NCT6798 `PCH_CHIP_TEMP`, `PCH_CPU_TEMP`, `PCH_CHIP_CPU_MAX_TEMP` on AMD boards showed as live 0 °C sensors).
* **Map my fans:** headers proven empty (no fan slows, every spinning fan belongs to another header) are now
  recorded even while the CPU fan is unknown. They stay visible until you mark the CPU fan, then are hidden
  at once; no second mapping run. Nothing is hidden while any header could still be the CPU fan.
* **Find my CPU fan (new):** a guide (banner link, or right-click a fan) that pulses each spinning
  motherboard fan in turn with a countdown and asks whether the fan on the CPU cooler changed; "Yes" marks it.
  The app's "Identifying" label only says which header was *told* to pulse, not which fan is wired to it.
* **What the BIOS does with each header (new, Nuvoton NCT67xx boards):** read from the chip, shown under the
  fan's name and as its tooltip: the temperature it follows and its curve. From that, a cautious guess:
  *pump header?* (full speed at every temperature), *chipset fan?* (X570, non-CPU sensor, stops when cool), or
  "follows the CPU" (Find my CPU fan tries those first). A guess never makes a CPU fan or changes any
  protection. ITE (it87) boards get no guess: their source bitmask differs between driver builds.
* **Graphics card curve / fixed speed:** points can no longer be set below the card's own firmware minimum
  (RX 9070 XT: 30 %), and saved points below it are shown at the speed the card really runs. The fixed-speed
  minimum is the card's reported one instead of a fixed 20 %.

## 1.0.0 — first public release

* **Motherboard fans** on any board with a supported Super I/O driver (it87, nct6775 family, nct6687,
  f71882fg, w83627ehf): Automatic (BIOS), fixed speed, or a temperature curve per fan, following one or
  several sensors. Other drivers (nct6683, asus-ec-sensors, asus_wmi_sensors, gigabyte-wmi, hp-wmi-sensors,
  laptops) are shown read-only.
* **Graphics cards:** AMD RX 7000/9000 fan curves written to the card's firmware (OverDrive); older AMD cards
  through pwm1 (beta, opt-in); NVIDIA and Intel read-only.
* **Safety:** the original BIOS mode of every fan is recorded before the first write and restored on exit,
  on a crash (systemd `ExecStopPost`) and after a driver reload; CPU fans and pumps never stop; full speed on
  critical CPU/GPU temperature or an unreadable sensor; systemd watchdog.
* **App:** curve editor, calibration, "Map my fans", HWiNFO-style Sensors view with min/max/average, alarms,
  CSV logging, profiles, tray, self-test; follows the desktop theme.
* **CLI:** `fanpilotctl` (status, set, profiles, sensors, calibrate, map, selftest, report, …).
* **Install:** Arch (PKGBUILD), .deb and .rpm (nFPM), or `install.sh` from source (with `--gui-venv` for
  distributions without PySide6); `fanpilot-install-driver` for ITE boards that need the out-of-tree it87.
* Versions 0.1–0.7 were developed and tested on the reference machine (Gigabyte B550M AORUS ELITE, ITE
  IT8688E, AMD RX 9060 XT); the test history is in docs/TESTING.md.
