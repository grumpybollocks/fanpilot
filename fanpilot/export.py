# SPDX-License-Identifier: GPL-3.0-or-later
"""Sensor snapshot for other programs (e.g. the G510 keyboard LCD).

fanpilotd rewrites /run/fanpilot/sensors.tsv once per tick (tmpfs: no disk writes), atomically,
world-readable. Plain tab-separated text so a C program can read it without a JSON parser:

    # fanpilot-sensors 1 <unix-time>
    <key>\t<kind>\t<short>\t<label>\t<value>

kind:  temp (degrees C) | rpm | pct (0-100)
key:   t.<sensor id> | rpm.<fan id> | pct.<fan id>     (no tabs/spaces; stable across boots)
short: at most 4 characters, for tiny displays
value: number, or empty when it can't be read right now

Readers should treat a file older than a few seconds as stale (FanPilot stopped).
"""
from __future__ import annotations

import os
import re
import time

FORMAT_VERSION = 1

SHORT = {
    "k10temp/temp1": "CPU", "k10temp/temp3": "CCD1", "k10temp/temp4": "CCD2",
    "amdgpu/temp1": "GPU", "amdgpu/temp2": "GHOT", "amdgpu/temp3": "GMEM",
    "nvme/temp1": "NVME",
    "it8688/temp1": "SYS", "it8688/temp2": "SOC", "it8688/temp3": "SOCK",
    "it8688/temp5": "VRM", "it8688/temp6": "PCH",
    "it8689/temp1": "SYS", "it8689/temp2": "SOC", "it8689/temp3": "SOCK",
    "it8689/temp5": "VRM", "it8689/temp6": "PCH",
}
# any board: by the (database or driver) label
SHORT_BY_LABEL = {
    "CPU (Tctl)": "CPU", "CPU (Tdie)": "CPU", "CPU package": "CPU", "GPU edge": "GPU", "GPU hotspot": "GHOT",
    "GPU memory": "GMEM", "NVMe SSD": "NVME", "Motherboard (System)": "SYS", "SoC VRM (VSOCMOS)": "SOC",
    "CPU socket (board)": "SOCK", "VRM (VRM MOS)": "VRM", "Chipset (PCH)": "PCH",
}


def short_label(key_id: str, label: str) -> str:
    if key_id in SHORT:
        return SHORT[key_id]
    if label in SHORT_BY_LABEL:
        return SHORT_BY_LABEL[label]
    m = re.fullmatch(r"CPU die \(CCD(\d+)\)", label)
    if m:
        return f"CCD{m.group(1)}"[:4]
    letters = re.sub(r"[^A-Za-z0-9]", "", label).upper()
    return (letters[:4] or "SENS")


def fan_short(fid: str, label: str) -> tuple[str, str]:
    """(speed, duty) short names: CPUF/CPF%, SYS1/SY1%, GPUF/GPF%, else FAN<n>/FN<n>%."""
    low = label.casefold()
    if "graphics" in low or fid.startswith("amdgpu"):
        return "GPUF", "GPF%"
    if "cpu" in low:
        return "CPUF", "CPF%"
    m = re.search(r"(?:system|sys)[ _]?fan[ _]?(\d)", low)
    if m:
        return f"SYS{m.group(1)}", f"SY{m.group(1)}%"
    n = re.sub(r"\D", "", fid.split("/")[-1])[-1:] or "0"
    return f"FAN{n}", f"FN{n}%"


def _clean(s: str) -> str:
    """No tab, newline, carriage return or NUL can reach the file (readers split on them)."""
    return str(s).replace("\t", " ").replace("\n", " ").replace("\r", " ").replace("\0", "").strip()


def rows(hw, temps: dict, fan_status: dict, names=None) -> list[tuple[str, str, str, str, str]]:
    names = names or {}
    out = []
    for sid, sensor in hw.sensors.items():
        v = temps.get(sid)
        label = names.get(sid) or sensor.label
        out.append((f"t.{sid}", "temp", short_label(sid, sensor.label), label,
                    "" if v is None else f"{v:.1f}"))
    for fid, fan in hw.fans.items():
        st = fan_status.get(fid) or {}
        rshort, pshort = fan_short(fid, fan.label)
        label = names.get(fid) or fan.label
        rpm, duty = st.get("rpm"), st.get("duty")
        out.append((f"rpm.{fid}", "rpm", rshort, f"{label} speed", "" if rpm is None else str(int(rpm))))
        out.append((f"pct.{fid}", "pct", pshort, f"{label} duty",
                    "" if duty is None else f"{duty:.0f}"))
    return [tuple(_clean(x) for x in r) for r in out]


def write(path: str, hw, temps: dict, fan_status: dict, names=None) -> None:
    body = f"# fanpilot-sensors {FORMAT_VERSION} {int(time.time())}\n"
    body += "".join("\t".join(r) + "\n" for r in rows(hw, temps, fan_status, names))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read(path: str, max_age: float = 5.0) -> list[dict]:
    """Parse a snapshot (used by tests and Python readers). Returns [] if missing or stale."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if not lines or not lines[0].startswith("# fanpilot-sensors "):
        return []
    parts = lines[0].split()
    try:
        if time.time() - int(parts[3]) > max_age:
            return []
    except (IndexError, ValueError):
        return []
    out = []
    for line in lines[1:]:
        f = line.split("\t")
        if len(f) == 5:
            out.append({"key": f[0], "kind": f[1], "short": f[2], "label": f[3],
                        "value": float(f[4]) if f[4] else None})
    return out
