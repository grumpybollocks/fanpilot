# FanPilot — testing

## Automated (no root, no real hardware)

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -t . -v
```

`tests/sim.py` builds a fake sysfs tree shaped like the reference machine with it87 loaded. It also emulates
the RDNA4 `fan_curve` firmware: point writes are staged, `c` commits the whole table (EIO if
any point is out of range), `r` restores automatic mode.

Covered:
* curve math, smoothing, hysteresis deadband, ramp limits
* config validation (ranges, increasing temps, non-decreasing speeds, CPU fan floor)
* discovery: stable ids, board labels, it8688 / it8689 / SIV-suffixed names, no-driver case
* GPU: single commit with zero-RPM staged first, exact 5-point pass-through, padding,
  clamping, partial-write reset, readback, reset sequence
* daemon safety: Auto touches nothing; restore file written before the first write; restore
  after a clean stop; crash recovery keeps the *original* values; manual-mode original → auto;
  sensor failure → 100 %; critical override, including fans on Automatic, with release band;
  CPU fan floor; reclaim → give up; broken config → all Automatic; GPU re-apply after external reset
* calibration: stop/start detection, CPU fan never below 20 %, abort when hot
* IPC: read for everyone, writes only for allowed uids, bad requests rejected
* GUI (offscreen, real widgets): fixed-speed apply reaches the fan, curve dragging and
  neighbour constraints, CPU-fan limits in the UI, read-only mode, GPU editor commit

Process-level checks (run manually 2026-11-14, see commit message):
* SIGTERM → `READY=1`/`WATCHDOG=1` sent to `NOTIFY_SOCKET`, fans back to mode 2, restore file removed
* `kill -9` → fans stay manual, `fanpilotd --restore` (the systemd `ExecStopPost`) puts them back to mode 2

Independent code review (2026-11-14) found 7 real defects, all fixed with regression tests in
`tests/test_review_fixes.py`: takeover without a saved restore record if the save failed; a fan
stopping although stop was not allowed; a broken loop still feeding the watchdog (now: restore +
exit after 3 failed ticks, systemd restarts); GPU retry storm on a rejected curve (60 s back-off,
then back to card control); GPU reset order; IPC reading state outside the lock; headers being
upgraded past the running kernel in the driver script.

Second independent review (2026-11-14, after profiles/identify/stall were added) found 10 more
defects, nine of them reproduced with probes; all are fixed with regression tests in `tests/test_review2.py`:
built-in profiles wiped each fan's fine tuning (now merged, and the calibrated minimum speed is always
enforced); an import could silently disable the emergency override with non-existent sensors; the stall
warning missed 20-30 % and fans under BIOS control (now 15 %, and the CPU fan is also watched under BIOS);
switching profile with unsaved edits undid the profile; "Save limits" sent a stale copy of the whole config
(now a dedicated `set_critical` command); Identify on a given-up fan; stalled fans had no ⚠ in the list;
root could not clear calibration; no single-instance guard (two tray icons); profile names with a trailing
newline / clashing with built-ins; the client could resend a request after a timeout.

Third review (0.4.0 diff, 2026-11-14) found 8 display/value issues (CPU % counted VM time twice, stale
rows, 0 W, NVMe labels, CMOS battery band, stale snapshot on failure, test isolation); fixed in
`tests/test_sensors_view.py`.

Fourth review (whole code base, two reviewers, 2026-11-14) found 25 more; all fixed with regression tests in
`tests/test_review3.py` and `tests/test_gui_review3.py` (11 of the 12 core tests fail on the old code):

* **Safety core:** an unreadable CPU sensor ended emergency mode (now it keeps it, and after 3 s starts it);
  a fan-driver reload or a late sensor at boot left FanPilot writing to dead paths (now it rescans every
  10 s while anything is missing and re-binds); any local user could hold every socket slot (now 16 per
  user, idle connections closed after 30 s); restarting from a stop crawled up from 0 % (now starts at the
  lowest working speed); the GPU curve was repaired only 3 times per run (now 3 per hour, and it says when
  it pauses); calibration started at the stale BIOS-mode duty and could store a stop speed as "safe";
  impossible temperatures (-128 / 200 °C) were used; log spam and BrokenPipe tracebacks; a failing service
  could restart forever (`StartLimitBurst`); `scripts/selftest.sh` deleted its backup even when the restore
  failed; `install.sh` stopped half-way on a broken config file.
* **App:** a stuck service froze the window up to 20 s (status now comes from a worker thread); without a
  tray icon, closing the window left an invisible app running; calibration had no Stop button; "Unsaved
  changes" could pop up twice; About text and emergency limits went stale; view-only users got a confusing
  profile dialog; restoring from the tray un-maximized the window; a fast refresh counted a sample twice in
  the averages; Ctrl+C in `fanpilotctl selftest` left the test running; the single-instance lock lived in /tmp;
  a window newer than the running service silently stopped updating (now a banner says to restart it);
  right-clicking a curve point could remove a different one if points were out of order.
* **Noted, not changed:** the GPU firmware may reject two curve points at the same temperature (only when
  points are outside 25-100 °C; it then falls back to the card, which is safe; later fixed: points are spread 1 °C apart);
  `install-driver.sh` would remove a pacman-owned it87 DKMS tree (later fixed: it never does); a client resend after a dropped connection could
  report "already running" for a selftest that did start (rare, harmless).

Fifth review (the 0.5.0 diff itself, 2026-11-14) found 5 more in the new code, all fixed with tests:
a CPU sensor whose driver vanished and was rescanned away counted as "cool" and ended emergency mode
(now a sensor once seen counts as unreadable, and is looked for every second); a fan that went away and
came back at the same path was not taken over from scratch; Identify could slow a fan below its measured
minimum; a late status answer could put old emergency limits back on screen; Ctrl+C mid-request in the CLI.
The screenshots also caught two bugs of mine: closed windows kept their service connections, and
"Current" could show below "Minimum".

Evidence audit on the live machine (2026-11-15) — things only the running system could show:

* **The tray app never picked up updates.** `ps` showed it started at 15:25 (0.3.0) while the code on disk
  was 0.4.0 from 22:02; opening FanPilot from the menu only showed that old window. A pre-0.5 app also can't
  see 0.5's single-instance lock (two tray icons). Fix: the app checks the installed version every 10 s and
  restarts itself (silently in the tray; with a button when its window is open; when opened from the menu),
  and `install.sh` restarts an open app in its own desktop session. The captured session variables were
  proven to start a Qt app on the real KDE Wayland desktop with a tray (`platform=wayland tray=True`).
* **"GPU fan curve was changed outside FanPilot" at 23:15:44** was traced through the journal: removing a
  package made systemd reload, the kernel sent a drm `change` event, and the LACT daemon (installed, running,
  but with no GPU settings in `/etc/lact/config.yaml`) "reapplied its configuration" at 23:15:30, which put
  the card back on its stock fan behaviour. FanPilot repaired it at its next 30 s check. Now FanPilot names
  such tools (LACT, CoreCtrl, fancontrol, CoolerControl, fan2go, amdgpu-fan) at start and in that warning.
* GPU curve points clamped onto the same temperature are now spread 1 °C apart (the card is proven to take
  distinct temperatures; your live curve gives identical points before and after). `install-driver.sh` no
  longer deletes an it87 DKMS tree that a package owns (here it is ours: `pacman -Qo` says no package).
* An independent review of these changes found the installer's "restarted" message could lie (`setsid -f`
  always succeeds; now it checks the app really runs), a vanished window could wipe the captured session,
  some session variables were dropped (now the whole environment minus systemd's bookkeeping is kept), a
  tray app could restart mid-copy (files now go to a new folder that is swapped in), the installer could
  close a window you were using (windows from 0.5.1 on leave a marker and update themselves), and an
  automatic restart could cut a CSV log short (it now waits). One more leak of my own: a test started the
  real app and left its lock in `/run/user/<uid>` and `/tmp`; removed, and tests now use a scratch runtime dir.
* Checked and fine: G510 LCD service 0.15 % of one core (measured over 20 s); fanpilotd 0.3 % and 12 MB;
  the two `SIGKILL` journal entries at 15:09 / 15:14 were the self-test's own crash tests.
* **Never tested on hardware yet:** suspend/resume and a reboot with FanPilot enabled (no suspend and no
  reboot since it was installed). Both are covered in the simulator; a real test needs you (see AUDIT).

### Universal phase 1 (0.6.0): any hwmon board

Generic detection, board database, stable ids, automatic CPU/GPU emergency sensors, CPU-fan/pump protection
from the hardware, exact BIOS-mode restore, Map my fans, missing-driver hints, hardware report. Tested on
simulated ASUS/Nuvoton nct6798 + asus_ec, MSI/Intel nct6687 + coretemp + two NVMe, a read-only board, and
this PC's own report (`tests/reports/`), which reproduces the real machine exactly.

* **Reference PC unchanged:** same fan/sensor ids, names, paths, CPU fan, emergency sensors, `sensors.tsv`
  (byte for byte) and restore values; the real `/etc/fanpilot/config.json` validates unchanged (only three
  empty sections added).
* **Found while building:** nct6775 boards would have been handed back in a different BIOS mode (5 "Smart Fan
  IV" turned into 2 "Thermal Cruise"); an unconnected Nuvoton channel at 127 °C was shown; the automatic
  CPU sensor could silently swap to another sensor when the chosen one vanished.
* **Two review rounds** found 15 more, all fixed with tests: mapping could hide a spinning fan (even the CPU
  fan) and had no undo; saving a limit could end an emergency; pump headers lost protection; "Forget the fan
  map" and rescans during mapping could leave or put a header in the wrong mode; false "inactive" warning;
  rescan log spam; stale CPU-fan marks; in-tree nct6683 named like a controllable chip; two-chip mapping;
  Revert in the editor. 182 tests.

### Universal phase 2 (0.7.0): graphics cards

Per-card state and restore entries (any number of AMD cards; old single-card restore files still honoured),
older AMD cards through pwm1 (beta: never `pwm1_enable=0`, which forces 100 %; handed back on exit), RX
7000/9000 without OverDrive read-only with a hint (generation read from the kernel's IP discovery: this PC's
card reports GC 12 = RDNA4), NVIDIA read-only via `nvidia-smi` in its own thread (a hung or missing driver
can't block control). Found while testing: a rescan dropped the NVIDIA readings (and would have rescanned
forever); fixed. This PC: same ids, readings and `sensors.tsv` rows as before. An independent review then
found: an older AMD card counted as a motherboard header (a late board driver was never looked for), its
readings appeared twice, and one empty nvidia-smi answer during a rescan lost NVIDIA readings for good. Also
changed after the review: built-in profiles leave older AMD cards on Automatic (the beta is opt-in), no
NVIDIA polling while a laptop's card sleeps, a failed nvidia-smi keeps the last answer, a second guard keeps
RX 7000/9000 off pwm1, the GPU emergency sensor waits for NVIDIA's first reading on iGPU+NVIDIA PCs, and
NVIDIA ids are always `nvidia@<pci>`. 198 tests.

### Universal phase 3 (packaging)

Built and inspected here without root: the Arch package with `makepkg` (its `check()` runs the suite), the
.deb and .rpm with nFPM 2.49.0 (checksum-verified download); the suite passes in a Python without PySide6;
`install-driver.sh --dry-run` against simulated boards with stub pacman/dkms/mokutil. Installing the .deb
under systemd (Ubuntu) and the .rpm (Fedora) is done by the CI workflow (.github/workflows/ci.yml, runs on
every push). An independent review found: package and source installs could coexist with the
source unit silently overriding the package's (now refused / warned); the Arch package lived under a versioned
python3.X path, so after a Python update without a rebuild even the restore step could fail (now
/usr/lib/fanpilot like the .deb/.rpm); the driver helper's `dkms status` parsing broke on "added" versions
and could remove versions FanPilot didn't install (now only its pinned build), didn't keep the module option
for an already-visible chip, used a fixed /tmp file as root, ignored a package-owned driver, showed the wrong
Secure Boot key path on Ubuntu; plus unit written through a symlink/masked unit, runtime caches left in
/usr/lib, Arch upgrade restart order, stale build tree, rpm upgrade case, setuptools minimum.

### Audit round (0.7.2)

An end-to-end review of the app and CLI on the simulated boards found, all fixed with tests: the self-test
reported false failures on other PCs ("back to BIOS" expected mode 2 where a Nuvoton board saves mode 5; the
graphics-card check failed without an RX 7000/9000 card; package-installed units were not recognised); renames
did not reach the Sensors rows, editor title, "Follows" menu and duty rows; `rename` / `cpu-fan` accepted
unknown ids (a typo cleared the CPU-fan marks); older AMD cards showed motherboard wording and a Calibrate box
after Apply; second GPUs and NVIDIA cards had no named group; actions could start during the self-test or
mapping; read-only boards and view-only users were offered actions that fail; a newer app on an older service
showed broken menus. The GPU curve editor uses each card's own limits from the service. A docs audit corrected
about a dozen claims, and `scripts/selftest.sh` now refuses to run on boards other than the reference one.
203 tests.

### Window layout (0.7.3)

The window could not be made shorter than ~850 px on any tab, and opening "Fine tuning" grew it to ~980 px (a
1080p screen minus the panel). Measured on the simulated machine at 820×560, 1000×720 and 1400×1040: the fan
editor and Settings page now scroll instead of holding the window open (minimum height 225 px), the control
modes sit on one row (explanations in tooltips), Fine tuning is two columns, empty notes leave no blank band,
emergency limits are one row, the profile list no longer takes an empty slab, the graph's last label is no
longer cut, and the window remembers its size and place. tests/test_layout.py fails on the old layout (802 px
minimum) and passes now.

### Fourth bug-check (0.7.4)

An independent review with reproductions found, all fixed with tests that fail on the old code
(tests/test_bugcheck4.py): a driver reload during a rescan lost the BIOS's original fan mode (a Nuvoton fan in
Smart Fan IV, mode 5, was handed back in mode 2); nvidia-smi installed without an NVIDIA card was run every 2 s
forever and kept the GPU emergency sensor unchosen; with an NVIDIA card a full hardware rescan ran every 10 s;
the RX 7000/9000 OverDrive hint was never shown (and would have fired for an APU's fanless graphics);
calibrating an empty header stored a 100 % floor, and a floor could exceed 100 %; a non-text rename crashed the
request; an older AMD card's "Follows" button lost its own sensor after a poll; uninstall.sh left a packaged
service disabled. CI found that PySide6 6.12.0 drops a reference to None on every Qt call returning None, so on
Python < 3.12 the app aborts ("deallocating None"); measured per version in CI (6.9-6.11: 0, 6.12.0: -1 per
call). install.sh --gui-venv and the `gui` extra now use PySide6 < 6.12 on Python < 3.12, and the app warns on
that combination. An independent review of these fixes then found: a fan set to Automatic during a driver reload stayed in manual mode, and a service restart during the gap lost the saved BIOS mode (both fixed: a returning fan is recorded again with the BIOS mode at once, and the restore file keeps it across a restart); with the pages now scrolling, the mouse wheel changed presets, sliders and spin boxes while scrolling (they react to the wheel only after a click now); uninstall.sh --purge started a packaged service before deleting its config. 216 tests.

Screenshots in the README are generated from the real GUI on the simulated machine:
`QT_QPA_PLATFORM=offscreen python3 tests/screenshots.py docs/images`.

Visual check: `python3 tests/simdaemon.py /tmp/fp.sock` + `python3 -m fanpilot.gui --socket /tmp/fp.sock`.

## On the real machine

Done 2026-11-14 on the reference machine (unprivileged, through `fanpilotctl`):

| Check | Result |
|---|---|
| driver | IT8688E (`it8688_60030507`), DKMS build on 7.2.9 OK |
| discovery | 3 headers + GPU; 11 sensors after hiding WMI duplicates and the fake ACPI zone |
| SYS_FAN1 fixed 70/40/25 % | 964 / 632 / 391 RPM; hand-back → mode 2, 1157 RPM; no BIOS reclaim |
| BIOS mode | the pwm register is stale under BIOS control → FanPilot now shows "set by BIOS" |
| GPU fixed 35 % | 1568 RPM, zero-RPM off; curve readback exact; auto → `0C 0%` table; no kernel errors |

First real `sudo scripts/selftest.sh` run (0.2.0): 11 passed, 1 failed.

* PASS: speed control SYS_FAN1 (100 % = 1201 RPM, 35 % = 602), SYS_FAN2 (1186 / 520), hand-back to
  BIOS for both, GPU 40 % = 1869 RPM with the firmware table written and reset, service restart.
* FAIL: crash test. This was a **test bug**: plain `systemctl kill` signals every process of the unit,
  and the journal shows it also killed the `ExecStopPost` restore systemd had just started
  (`Control process exited, code=killed`). A real crash/OOM/watchdog kill only hits the main process.
  The self-test now uses `systemctl kill --kill-whom=main`.
* Found in the same run: **it87 reports a manual fan at duty 255 as `pwm_enable` 0** (`it87.c`
  `pwm_mode()`). FanPilot took that for the BIOS reclaiming the fan and gave up after 5 seconds,
  which would also have happened during a critical-temperature event. Fixed; `tests/test_it87_quirk.py`
  emulates the driver (3 of its 4 tests fail on the previous code).

### Manual checklist

1. `sudo scripts/install-driver.sh`: note IT8688E or IT8689E (update RESEARCH.md).
2. `fanpilotctl status` with every fan on Automatic: RPMs plausible, nothing changes.
3. One header at a time: fixed 40 % → 70 % → Automatic. RPM follows, and BIOS takes over again.
4. `sudo systemctl kill -s KILL fanpilotd` while a fan is fixed: it must return to BIOS control.
5. GPU: curve mode → `cat /sys/class/drm/card1/device/gpu_od/fan_ctrl/fan_curve` shows the
   points; Automatic → shows `0C 0%` again (firmware auto).
6. Suspend/resume with a fixed fan: setting re-applied (Log tab).
7. Calibrate SYS_FAN1/2.

### Second real run (0.2.1)

`sudo scripts/selftest.sh`: **12 passed, 0 failed**, including the crash test (journal: main process
SIGKILLed, no killed control process, fan back to mode 2, service restarted after 3 s). No false
"taken back" warnings while fans ran at 100 %.

Features on real fans (as the normal user): Silent profile 665 / 434 / 492 RPM (from ~1200-1300 under BIOS),
Performance 1049 / 760 / 778 RPM, no warnings. Identify at 100 % was not noticeable because the BIOS already
runs the system fans near full speed, so Identify now pulses: 3 s at 25 %, then 3 s at 100 %.

### Sensors view on the real machine (0.4.0)

New service code run read-only on the real hardware (no user allowed to change anything) with the new GUI:
**39 readings**, none missing, **no false red values** (the it87 `_max` placeholders 127 / -125 °C are not
used as limits), board rails +12 V / +5 V / +3.3 V within 2 % of nominal, every `pwm*_enable` identical
before and after. Tests: `tests/test_sensors_view.py` (readings, alerts, missing files, min/max/avg/reset,
columns; GUI settings go to a scratch folder, never `~/.config`).

### In-app self-test on the real machine (0.3.0)

`fanpilotctl selftest` (same as the Settings button), run as the normal user: **14 passed, 0 failed**.
SYS_FAN1 100 % = 1207 RPM / 35 % = 579, SYS_FAN2 1125 / 404, CPU fan 1569 RPM (read only), crash-recovery
restore routine → mode 2, systemd unit has ExecStopPost + WatchdogSec, GPU curve read back exactly,
GPU 40 % = 1861 RPM, card table reset afterwards (checked 30 s later: `0C 0%`, zero-RPM on, fan at 0 RPM).
No warnings in the journal.

