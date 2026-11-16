# SPDX-License-Identifier: GPL-3.0-or-later
"""Every reading for the HWiNFO-style Sensors view, grouped by device.

Collected by fanpilotd once per tick (plain sysfs/proc reads, no privileges needed) and sent
to the GUI in the status; see docs/SENSORS_VIEW_PLAN.md. Everything is read relative to
``root`` so the simulated sysfs in the tests works the same way.
"""
from __future__ import annotations

import os
import re

from .hw import GPU_FAN_ID, RAM_CHIPS, BoardFan


# Nuvoton NCT6779 and later: inputs with a fixed function and internal scaling (the chip's pins CPUVCORE,
# AVSB, 3VCC, 3VSB, VBAT), so their readings are true voltages on every board. VIN* pins go through each
# board's own resistors and stay hidden unless the board database knows them. {in: (label, nominal, low_only)}
NUVOTON_FIXED_RE = re.compile(r"^nct(6779|679\d)$")
NUVOTON_FIXED_VOLTS = {0: ("CPU Vcore", None, False), 2: ("AVSB (+3.3V)", 3.3, False),
                       3: ("+3.3V (3VCC)", 3.3, False), 7: ("+3.3V standby (3VSB)", 3.3, False),
                       8: ("CMOS battery (VBAT)", 3.0, True)}
GPU_GROUP_CHIPS = ("amdgpu", "nvidia", "nouveau", "i915", "xe", "radeon")


def _chip_title(chip: str) -> str:
    base = chip.split("@")[0]
    if re.fullmatch(r"it8\d{3}", base):
        return f"ITE {base.upper()}E"
    if base.startswith("nct"):
        return f"Nuvoton {base.upper()}"
    if base.startswith(("f71", "f81", "f8000")):
        return f"Fintek {base.upper()}"
    if base.startswith("w83"):
        return f"Winbond {base.upper()}"
    return base

UNITS = {"temp": "°C", "fan": "RPM", "duty": "%", "volt": "V", "power": "W",
         "clock": "MHz", "usage": "%", "data": "GB"}
DECIMALS = {"temp": 1, "fan": 0, "duty": 0, "volt": 3, "power": 1, "clock": 0, "usage": 1, "data": 2}


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _num(path: str) -> float | None:
    v = _read(path)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _pci_name(root: str, vendor: str, device: str) -> str | None:
    """Device name from the system's pci.ids (read once at start)."""
    path = os.path.join(root, "usr/share/hwdata/pci.ids")
    v, d = vendor.lower().removeprefix("0x"), device.lower().removeprefix("0x")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            in_vendor = False
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    in_vendor = line.startswith(v + " ")
                elif in_vendor and line.startswith("\t" + d + " "):
                    return line.strip()[len(d):].strip()
    except OSError:
        pass
    return None


class Readings:
    def __init__(self, root: str, hw):
        self.root = root
        self.hw = hw
        self._prev_stat = None
        self.groups: dict[str, str] = {}
        self._crit: dict[str, float] = {}
        self._describe()

    # -- static facts (names, limits), read once ----------------------------------------------
    def _p(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    def _describe(self) -> None:
        cpu = "CPU"
        info = _read(self._p("proc/cpuinfo")) or ""
        m = re.search(r"^model name\s*:\s*(.+)$", info, re.M)
        if m:
            cpu = re.sub(r"\s+\d+-Core Processor$", "", m.group(1).strip())
        self.groups["cpu"] = cpu

        vendor = _read(self._p("sys/class/dmi/id/board_vendor")) or ""
        board = _read(self._p("sys/class/dmi/id/board_name")) or "Motherboard"
        vendor = vendor.replace("Gigabyte Technology Co., Ltd.", "Gigabyte")
        chip = f" ({', '.join(_chip_title(c) for c in self.hw.board_chips)})" if self.hw.board_chips else ""
        self.groups["board"] = f"{vendor} {board}{chip}".strip()

        self.gpu_dev = None
        gname = "GPU"
        gpu_hwmon = self.hw.chips.get("amdgpu")
        if gpu_hwmon:
            dev = os.path.realpath(os.path.join(gpu_hwmon, "device"))
            self.gpu_dev = dev
            name = _pci_name(self.root, _read(os.path.join(dev, "vendor")) or "",
                             _read(os.path.join(dev, "device")) or "")
            if name:
                m = re.match(r"(.*?)\s*\[(.*)\]$", name)
                gname = f"AMD {m.group(2)} ({m.group(1)})" if m else f"AMD {name}"
            total = _num(os.path.join(dev, "mem_info_vram_total"))
            if total:
                gname += f", {total / 2 ** 30:.0f} GB"      # GiB, as VRAM is sold
        self.groups["gpu"] = gname

        drives = []
        for chip, d in self.hw.chips.items():
            if chip.split("@")[0] == "nvme":
                model = _read(os.path.join(os.path.realpath(os.path.join(d, "device")), "model"))
                drives.append((chip, model or "NVMe drive"))
        self._drives = drives
        for chip, model in drives:
            self.groups["drive:" + chip] = model
        self.groups["memory"] = "Memory"
        self.groups["other"] = "Other sensors"

        # real critical limits, only where the driver provides one (not the it87 placeholders)
        for sid, s in self.hw.sensors.items():
            chip = sid.split("/")[0].split("@")[0]
            if chip in ("amdgpu", "nvme"):
                c = _num(s.path.replace("_input", "_crit"))
                if c and 0 < c < 200000:
                    self._crit[sid] = c / 1000

    def _gpu_group(self, chip: str) -> str:
        if chip == "amdgpu":
            return "gpu"
        g = "gpu:" + chip
        if g not in self.groups:              # an extra card: its PCI name, else its driver and address
            d = self.hw.chips.get(chip)
            title = None
            if d:
                dev = os.path.realpath(os.path.join(d, "device"))
                name = _pci_name(self.root, _read(os.path.join(dev, "vendor")) or "",
                                 _read(os.path.join(dev, "device")) or "")
                if name:
                    m = re.match(r"(.*?)\s*\[(.*)\]$", name)
                    title = m.group(2) if m else name
            base, _, where = chip.partition("@")
            self.groups[g] = f"{title or base.upper()}" + (f" ({where})" if where else "")
        return g

    # -- one tick ------------------------------------------------------------------------------
    def _cpu_usage(self) -> float | None:
        line = (_read(self._p("proc/stat")) or "").split("\n", 1)[0].split()
        if not line or line[0] != "cpu":
            return None
        vals = [int(x) for x in line[1:]]
        # user..steal only: guest/guest_nice are already counted inside user/nice
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals[:8])
        prev, self._prev_stat = self._prev_stat, (idle, total)
        if not prev or total <= prev[1]:
            return None
        # iowait can go backwards (documented in proc(5)), so keep the result in 0..100
        busy = 1 - max(0, idle - prev[0]) / (total - prev[1])
        return 100.0 * min(1.0, max(0.0, busy))

    def read(self, temps: dict, fan_status: dict, critical: dict) -> list[dict]:
        out: list[dict] = []

        def add(group, rid, label, kind, value, alert=False):
            out.append({"id": rid, "group": group, "label": label, "kind": kind,
                        "value": None if value is None else round(float(value), 4),
                        "unit": UNITS[kind], "decimals": DECIMALS[kind], "alert": bool(alert)})

        cpu_limit = critical.get("cpu_temp", 90)
        gpu_limit = critical.get("gpu_temp", 100)
        used_sensors = set()

        # CPU
        for sid, s in self.hw.sensors.items():
            if sid.split("/")[0].split("@")[0] in ("k10temp", "coretemp", "zenpower"):
                v = temps.get(sid)
                add("cpu", "t:" + sid, s.label, "temp", v, v is not None and v >= cpu_limit)
                used_sensors.add(sid)
        add("cpu", "cpu:usage", "CPU usage", "usage", self._cpu_usage())
        freqs = []
        base = self._p("sys/devices/system/cpu/cpufreq")
        try:
            for pol in sorted(os.listdir(base)):
                f = _num(os.path.join(base, pol, "scaling_cur_freq"))
                if f:
                    freqs.append(f / 1000)
        except OSError:
            pass
        add("cpu", "cpu:clock_avg", "Core clock (average)", "clock", sum(freqs) / len(freqs) if freqs else None)
        add("cpu", "cpu:clock_max", "Core clock (highest)", "clock", max(freqs) if freqs else None)

        # Motherboard
        for sid, s in self.hw.sensors.items():
            if sid.split("/")[0] in self.hw.board_chips:
                add("board", "t:" + sid, s.label, "temp", temps.get(sid))
                used_sensors.add(sid)
        # voltages only where the board database knows the dividers (raw values would mislead)
        board = self.hw.board
        if board is None:
            # unknown board: only the pins whose meaning and scaling the chip itself fixes
            for chip in self.hw.board_chips:
                pins = NUVOTON_FIXED_VOLTS if NUVOTON_FIXED_RE.match(self.hw.names.get(chip, chip)) else {}
                for n, (label, nominal, low_only) in pins.items():
                    v = _num(os.path.join(self.hw.chips[chip], f"in{n}_input"))
                    v = None if v is None or v <= 0 else v / 1000
                    if v is None:
                        continue                   # pin not wired / not reported on this board
                    alert = nominal is not None and ((nominal - v) if low_only else abs(v - nominal)) > 0.1 * nominal
                    add("board", f"v:{chip}/in{n}", label, "volt", v, alert)
        bchip = next((c for c in self.hw.board_chips if board and c.split("@")[0] in board["match"]["chip"]), None)
        if board and bchip:
            bdir = self.hw.chips[bchip]
            low_only = set(board.get("low_only", []))
            for n, (label, scale, nominal) in board.get("voltages", {}).items():
                raw = _num(os.path.join(bdir, f"in{n}_input"))
                v = None if raw is None else raw / 1000 * scale
                alert = v is not None and nominal is not None and (
                    (nominal - v) if n in low_only else abs(v - nominal)) > 0.1 * nominal
                add("board", f"v:in{n}", label, "volt", v, alert)
        for fid, fan in self.hw.fans.items():
            if isinstance(fan, BoardFan) and fan.kind == "board":   # graphics card fans: GPU group below
                st = fan_status.get(fid) or {}
                stalled = "stalled" in (st.get("flags") or [])
                name = fan.label.split(" (")[-1].rstrip(")") if "(" in fan.label else fan.label
                add("board", "fan:" + fid, name, "fan", st.get("rpm"), stalled)
                add("board", "duty:" + fid, name + " duty", "duty", st.get("duty"))
        for fid, (label, path) in self.hw.fan_inputs.items():     # fans FanPilot can only read
            chip = fid.split("/")[0]
            group = self._gpu_group(chip) if chip.split("@")[0] in GPU_GROUP_CHIPS else "board"
            kind = "duty" if label.endswith(" %") else "fan"
            add(group, "fan:" + fid, label, kind, path() if callable(path) else _num(path))

        # GPU (every card: the first AMD card is "gpu", others get their own group named after the card)
        for sid, s in self.hw.sensors.items():
            chip = sid.split("/")[0]
            if chip.split("@")[0] in GPU_GROUP_CHIPS:
                v = temps.get(sid)
                limit = self._crit.get(sid)
                if sid == critical.get("gpu_sensor"):
                    limit = min(limit, gpu_limit) if limit else gpu_limit
                add(self._gpu_group(chip), "t:" + sid, s.label, "temp", v,
                    v is not None and limit is not None and v >= limit)
                used_sensors.add(sid)
        for fid, gst in fan_status.items():          # every graphics card fan FanPilot controls
            if gst.get("kind") == "gpu":
                name = "GPU fan" if fid == GPU_FAN_ID else gst.get("label", "GPU fan")
                add("gpu", "fan:" + fid, name, "fan", gst.get("rpm"))
                add("gpu", "duty:" + fid, name + " duty", "duty", gst.get("duty"))
        gh = self.hw.chips.get("amdgpu")
        if gh:
            p = _num(os.path.join(gh, "power1_average"))
            if p is None:
                p = _num(os.path.join(gh, "power1_input"))
            add("gpu", "gpu:power", "GPU power (PPT)", "power", None if p is None else p / 1e6)
            v = _num(os.path.join(gh, "in0_input"))
            add("gpu", "gpu:vcore", "GPU core voltage", "volt", None if v is None else v / 1000)
            for n, label in ((1, "GPU clock"), (2, "Memory clock")):
                f = _num(os.path.join(gh, f"freq{n}_input"))
                add("gpu", f"gpu:freq{n}", label, "clock", None if f is None else f / 1e6)
        if self.gpu_dev:
            add("gpu", "gpu:load", "GPU load", "usage", _num(os.path.join(self.gpu_dev, "gpu_busy_percent")))
            used = _num(os.path.join(self.gpu_dev, "mem_info_vram_used"))
            add("gpu", "gpu:vram", "VRAM used", "data", None if used is None else used / 2 ** 30)

        # Drives
        for chip, _model in self._drives:
            for sid, s in self.hw.sensors.items():
                if sid.split("/")[0] == chip:
                    v = temps.get(sid)
                    limit = self._crit.get(sid)
                    add("drive:" + chip, "t:" + sid, s.label.replace("NVMe SSD", "").strip() or "Temperature",
                        "temp", v, v is not None and limit is not None and v >= limit)
                    used_sensors.add(sid)

        # Memory (RAM module temperature sensors, when their driver is loaded)
        for sid, s in self.hw.sensors.items():
            if sid.split("/")[0].split("@")[0] in RAM_CHIPS:
                add("memory", "t:" + sid, s.label, "temp", temps.get(sid))
                used_sensors.add(sid)
        mi = _read(self._p("proc/meminfo")) or ""
        tot = re.search(r"^MemTotal:\s+(\d+)", mi, re.M)
        avail = re.search(r"^MemAvailable:\s+(\d+)", mi, re.M)
        if tot and avail:
            t, a = int(tot.group(1)), int(avail.group(1))
            add("memory", "mem:used", "Memory used", "data", (t - a) / 1024 ** 2)
            add("memory", "mem:pct", "Memory load", "usage", 100.0 * (t - a) / t if t else None)

        # anything else the service knows (e.g. gigabyte_wmi on boards without it87)
        for sid, s in self.hw.sensors.items():
            if sid not in used_sensors:
                add("other", "t:" + sid, s.label, "temp", temps.get(sid))
        return out

    def group_list(self, readings: list[dict]) -> list[dict]:
        seen = []
        for r in readings:
            if r["group"] not in seen:
                seen.append(r["group"])
        return [{"id": g, "title": self.groups.get(g, g)} for g in seen]
