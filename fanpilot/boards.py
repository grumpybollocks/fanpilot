# SPDX-License-Identifier: GPL-3.0-or-later
"""Board database: names for the fan headers, temperature channels and voltages of known boards.

Unknown boards still work: fans are called by the driver's label or "Fan N", and nothing is assumed about
which one is the CPU fan (every motherboard fan then gets the CPU fan's protection until you mark it).

Entries are facts re-derived for each board (silkscreen names, which channels are connected, voltage
divider multipliers), with the source named. Nothing is copied from GPL-2.0-only files.
To add your board, send the output of `fanpilotctl report` (see CONTRIBUTING.md).

Fields:
  match     board_vendor / board_name (exact, case-insensitive; lists allowed) and the hwmon chip names
  title     shown as the motherboard group's heading
  fans      {pwm channel: {"label": str, "cpu": bool}}   only these channels are shown
  temps     {temp channel: label}                          only these channels are shown
  voltages  {in channel: [label, multiplier, nominal or None]}  nominal enables the +-10 % alert
  low_only  in channels where only a LOW value is a problem (e.g. the CMOS battery)
  hide_chips  other hwmon chips that only duplicate this board's readings
  source    where the facts come from
"""
from __future__ import annotations

BOARDS: list[dict] = [
    {
        "match": {"board_vendor": ["Gigabyte Technology Co., Ltd."],
                  "board_name": ["B550M AORUS ELITE"],
                  "chip": ["it8688", "it8689"]},
        "title": "Gigabyte B550M AORUS ELITE",
        "fans": {1: {"label": "CPU fan (CPU_FAN)", "cpu": True},
                 2: {"label": "System fan 1 (SYS_FAN1)"},
                 3: {"label": "System fan 2 (SYS_FAN2)"}},
        "temps": {1: "Motherboard (System)", 2: "SoC VRM (VSOCMOS)", 3: "CPU socket (board)",
                  5: "VRM (VRM MOS)", 6: "Chipset (PCH)"},
        "voltages": {0: ["CPU Vcore", 1.0, None], 1: ["+3.3V", 1.649, 3.3], 2: ["+12V", 6.0, 12.0],
                     3: ["+5V", 2.5, 5.0], 4: ["CPU VCORE SOC", 1.0, None], 5: ["CPU VDDP", 1.0, None],
                     6: ["DRAM", 1.0, None], 7: ["3VSB", 1.0, 3.3], 8: ["CMOS battery", 1.0, 3.0]},
        "low_only": [8],
        # gigabyte_wmi reads the same Super I/O (exact duplicates); acpitz is a fixed fake 16.8 °C here
        "hide_chips": ["gigabyte_wmi", "acpitz"],
        "source": "frankcrawford/it87 'Sensors configs' gigabyte-it87-amd.conf (SIV 60030507/61030507), "
                  "board manual; every value verified on a real board (2026-11-14)",
    },
]


def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def find(dmi: dict, chip: str) -> dict | None:
    """The entry for this board and chip, or None."""
    for b in BOARDS:
        m = b["match"]
        if (_norm(dmi.get("board_vendor")) in {_norm(v) for v in m["board_vendor"]}
                and _norm(dmi.get("board_name")) in {_norm(v) for v in m["board_name"]}
                and chip in m["chip"]):
            return b
    return None
