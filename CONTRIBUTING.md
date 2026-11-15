# Contributing

## Your board isn't known, or something looks wrong

Send a hardware report with your issue. It is anonymised (no host or user name, serial numbers, UUIDs, MAC or
IP addresses) and only reads `/sys` and `/proc`; no root needed:

```bash
fanpilotctl report > my-pc.json        # or, without installing:  python3 fanpilot/report.py > my-pc.json
```

Every report becomes a test machine (`tests/reports/`, loaded by `tests/sim.py: machine_from_report`), so the
fix is tested against your exact hardware layout.

## Adding a board to the database

`fanpilot/boards.py`: the header names as printed on the board (CPU_FAN, SYS_FAN1, ...), which temperature
channels are connected, and the voltage multipliers. Re-derive the facts from your board's manual and your
own readings, and name the source in `source`. Do not copy files from GPL-2.0-only projects (lm-sensors
configs, it87 configs): FanPilot is GPL-3.0-or-later.

## Code

* The service (`fanpilot/daemon.py` and what it imports) uses only Python's standard library and runs as
  root: keep it that way. The app (`fanpilot/gui.py`) uses PySide6.
* Every fan must go back to the BIOS / card on any exit, and FanPilot never writes to a driver it doesn't
  know to be controllable. A change that weakens this won't be merged.
* Tests run without hardware or root: `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -t .`
  (`FANPILOT_NO_QT=1` skips the app's tests where PySide6 isn't installed). Add a test that fails without
  your change.

By contributing you agree that your contribution is licensed under GPL-3.0-or-later.
