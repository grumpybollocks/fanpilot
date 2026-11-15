# Sensors view (HWiNFO64 style) — plan

Request (2026-11-14): "I'm not a fan of the graph display, I'm a fan of seeing the sensors like HWiNFO64."

## What HWiNFO64's sensor window does (checked, not from memory)

* One table, **grouped by hardware component**: a bold heading per device, one row per reading under it.
* Columns **Current / Minimum / Maximum / Average**; the average covers the time since the window was opened.
* A **reset** button clears min/max (and average).
* Values past a limit are shown in **red** ("critical colours").
* Columns can be hidden; groups collapse; readings can be logged.

Sources: tech-insider HWiNFO logging guide, itigic HWiNFO64 review, HWiNFO forum ("critical colors ... wrong column").

## What the reference machine exposes (read-only)

| Group (heading) | Readings |
|---|---|
| **CPU** — AMD Ryzen 7 5800X3D | Tctl, CCD1 temperature; usage % (from /proc/stat); clock: average and highest core (cpufreq, 16 threads) |
| **Motherboard** — Gigabyte B550M AORUS ELITE (ITE IT8688E) | System, VSOCMOS, CPU socket, VRM MOS, PCH temps; **voltages** Vcore 1.27 V, +3.3 V (×1.649 → 3.34 V), +12 V (×6 → 12.24 V), +5 V (×2.5 → 5.07 V), VSOC 1.01 V, VDDP 0.92 V, DRAM 1.38 V, 3VSB 3.29 V, CMOS battery 3.0 V; **fans** CPU_FAN, SYS_FAN1, SYS_FAN2 RPM + duty |
| **GPU** — "AMD Radeon RX 9050 / 9060 XT (Navi 44), 16 GB" (pci.ids can't tell the two apart; VRAM size is measured) | edge / hotspot / memory temps; fan RPM + duty; power (PPT, W); core voltage; GPU clock and memory clock (MHz); load %; VRAM used (total is in the group title) |
| **Drive** — KINGSTON SA2000M8500G (from /sys/class/nvme) | composite temperature |
| **Memory** | RAM used (GB) and % used |

Voltage scaling factors and labels come from the it87 driver's own config for this board
(`gigabyte-it87-amd.conf`, SIV 0x60030507) — already used for the temperature labels in RESEARCH.md.
Measured values match the nominal rails, which confirms them.

## Design

* **The service (fanpilotd) collects every reading** (new `fanpilot/readings.py`), so there is one source of
  truth, the GUI stays a viewer, and nothing needs extra privileges. Cost: ~30 small sysfs/proc reads per second.
* Each reading: `id, group, label, kind (temp/fan/duty/volt/power/clock/usage/data), value, unit`.
  Status gains `readings` + `groups` (device names from DMI, /proc/cpuinfo, the GPU's PCI id, NVMe model).
* **GUI: the Temperatures tab becomes "Sensors"** — a `QTreeWidget`:
  * columns **Sensor | Current | Minimum | Maximum | Average**, numbers right-aligned in a fixed-width font,
    units like HWiNFO (`°C`, `RPM`, `%`, `V` with 3 decimals, `W`, `MHz`, `GB`);
  * bold, collapsible group rows with the device name; collapse state remembered;
  * min / max / average kept by the GUI **since the window opened** (HWiNFO semantics), **Reset** button;
  * **red** current value past a limit: temperature ≥ its hwmon `_crit` value when the driver provides a real
    one (on this PC: amdgpu edge 100 / hotspot 110 / memory 108 °C, NVMe), CPU/GPU ≥ FanPilot's emergency
    limits, a fan driven but at 0 RPM (stall), a voltage outside ±10 % of its nominal rail. The it8688
    `tempN_max` values read 127 / −125 °C here — placeholders, **not used**;
  * column chooser (right-click header) and **Copy / Export CSV** of the current table (snapshot only —
    no continuous logging, to avoid needless SSD writes);
  * **no graph** in this view. (The history graph code goes away with the old tab.)
* The `sensors.tsv` export for the G510 LCD stays as it is (its kinds are temp/rpm/pct); new kinds can be
  added later without breaking it.

## Tests

* `readings.py` on the simulated sysfs (voltages scaled, missing files → no value, CPU % from two /proc/stat samples);
* real-machine read-only run: every reading present and plausible (rails within ±10 %);
* GUI offscreen: groups, columns, min/max/avg arithmetic, reset, red thresholds, export CSV, screenshot review.

## Out of scope (for now)

Continuous CSV logging, per-core rows (HWiNFO shows them; 16 rows would swamp this view — average + highest
core instead), CPU package power (no `amd_energy`/`zenpower` driver loaded on this PC).
