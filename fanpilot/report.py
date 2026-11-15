# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware report: an anonymised, read-only snapshot of everything FanPilot looks at.

    fanpilotctl report > my-pc.json        (or: python3 report.py > my-pc.json, no install needed)

It only READS files under /sys and /proc and needs no root. It contains: hwmon chips (names, drivers,
attribute values and whether they are writable), graphics cards (PCI ids, fan-control files), motherboard
vendor and model, CPU model, kernel version, and related kernel modules and boot parameters.
It does not contain: host name, user name, serial numbers, UUIDs, MAC or IP addresses.
The same file can be loaded into FanPilot's simulator, so every report becomes a test machine.
Standard library only, so this file also runs on its own.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time

FORMAT = 1
MAX_VALUE = 400
SKIP = {"uevent", "power", "subsystem", "device", "driver", "of_node", "firmware_node", "modalias"}
MODULE_RE = re.compile(r"^(it87|nct\w*|asus\w*|gigabyte\w*|k10temp|zenpower\w*|coretemp|amdgpu|nvidia\w*|nouveau|"
                       r"xe|i915|thinkpad_acpi|dell_smm\w*|applesmc|f71882fg|w83627ehf|sch56\w+|hp_wmi\w*)$")
CMDLINE_RE = re.compile(r"^(amdgpu\.\S+|acpi_enforce_resources=\S+|it87\.\S+|nct6775\.\S+|nct6687\.\S+)$")


def _read(path: str, limit: int = MAX_VALUE) -> str | None:
    try:
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
    except OSError:
        return None
    try:
        txt = raw.decode()
    except UnicodeDecodeError:
        return None
    return txt[:limit]


def _attrs(d: str) -> dict:
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if n in SKIP:
            continue
        p = os.path.join(d, n)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        out[n] = {"v": _read(p) if st.st_mode & stat.S_IRUSR else None,
                  "w": bool(st.st_mode & stat.S_IWUSR)}
    return out


def _base(path: str) -> str | None:
    return os.path.basename(os.path.realpath(path)) if os.path.exists(path) else None


def build(root: str = "/") -> dict:
    j = lambda *p: os.path.join(root, *p)  # noqa: E731
    rep = {"fanpilot_report": FORMAT, "date": time.strftime("%Y-%m-%d"),
           "kernel": os.uname().release if root == "/" else None,
           "dmi": {}, "cpu": None, "hwmon": [], "drm": [], "platform": [], "modules": [], "cmdline": []}
    for k in ("board_vendor", "board_name", "board_version", "sys_vendor", "product_name", "bios_version"):
        v = _read(j("sys/class/dmi/id", k))
        if v is not None:
            rep["dmi"][k] = v.strip()
    m = re.search(r"^model name\s*:\s*(.+)$", _read(j("proc/cpuinfo"), 1 << 20) or "", re.M)
    rep["cpu"] = m.group(1).strip() if m else None

    hw = j("sys/class/hwmon")
    for e in sorted(os.listdir(hw) if os.path.isdir(hw) else [], key=lambda n: int(re.sub(r"\D", "", n) or 0)):
        d = os.path.join(hw, e)
        rep["hwmon"].append({"dir": e, "device": _base(os.path.join(d, "device")),
                             "driver": _base(os.path.join(d, "device", "driver")), "attrs": _attrs(d)})

    drm = j("sys/class/drm")
    for e in sorted(os.listdir(drm) if os.path.isdir(drm) else []):
        if not re.fullmatch(r"card\d+", e):
            continue
        dev = os.path.join(drm, e, "device")
        card = {"card": e, "device": _base(dev), "driver": _base(os.path.join(dev, "driver")), "attrs": {}}
        for k in ("vendor", "device", "subsystem_vendor", "subsystem_device", "mem_info_vram_total",
                  "gpu_busy_percent", "pp_features"):
            v = _read(os.path.join(dev, k))
            if v is not None:
                card["attrs"][k] = v
        fc = os.path.join(dev, "gpu_od", "fan_ctrl")
        if os.path.isdir(fc):
            card["fan_ctrl"] = _attrs(fc)
        rep["drm"].append(card)

    plat = j("sys/bus/platform/devices")
    for e in sorted(os.listdir(plat) if os.path.isdir(plat) else []):
        if re.match(r"^(it87|nct|f71|w83|asus|gigabyte|dell|thinkpad|applesmc)", e, re.I):
            rep["platform"].append({"name": e, "hwmon": os.path.isdir(os.path.join(plat, e, "hwmon"))})
    for line in (_read(j("proc/modules"), 1 << 20) or "").splitlines():
        name = line.split(" ", 1)[0]
        if MODULE_RE.match(name):
            rep["modules"].append(name)
    rep["cmdline"] = [w for w in (_read(j("proc/cmdline"), 1 << 16) or "").split() if CMDLINE_RE.match(w)]
    v = _read(j("sys/module/amdgpu/parameters/ppfeaturemask"))
    if v is not None:
        rep["amdgpu_ppfeaturemask"] = v.strip()
    return rep


def main(argv=None) -> int:
    json.dump(build(), sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
