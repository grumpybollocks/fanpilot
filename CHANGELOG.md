# Changelog

## 1.0.1

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
