# Changelog

## Unreleased

Found on a second real machine (ASUS TUF GAMING X570-PLUS, Nuvoton NCT6798, AMD RX 9070 XT); its report is
now a test machine (`tests/reports/asus-tuf-gaming-x570-plus.json`).

* **Sensors:** on boards not in the database, channels reading exactly 0 °C are treated as unconnected
  (NCT6798 `PCH_CHIP_TEMP`, `PCH_CPU_TEMP`, `PCH_CHIP_CPU_MAX_TEMP` on AMD boards showed as live 0 °C sensors).
* **Map my fans:** headers proven empty (no fan slows, every spinning fan belongs to another header) are now
  recorded even while the CPU fan is unknown. They stay visible until you mark the CPU fan, then are hidden
  at once; no second mapping run. Nothing is hidden while any header could still be the CPU fan.
* **Find my CPU fan (new):** a guide (banner link, or right-click a fan) that pulses each spinning
  motherboard fan in turn with a countdown and asks whether the fan on the CPU cooler changed; "Yes" marks it.
  The app's "Identifying" label only says which header was *told* to pulse, not which fan is wired to it.
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
