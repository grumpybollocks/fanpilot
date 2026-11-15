# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware discovery and access through sysfs.

Every path is resolved under ``root`` so tests can point it at a fake tree.
Fans and sensors are addressed by stable ids ("<chip>/<channel>"), never by
the hwmonN index, which can change between boots.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import boards

# --- which chips may be controlled ----------------------------------------

# Desktop Super-I/O drivers whose pwmN / pwmN_enable work as the hwmon ABI says (0 full, 1 manual,
# 2+ automatic): see docs/UNIVERSAL_RESEARCH.md. Every other chip is read-only for FanPilot: drivers that are
# read-only by design (asus_ec_sensors, asus_wmi_sensors, gigabyte_wmi, hp-wmi-sensors, nct6683, ...),
# GPUs (own backends), and laptops (dell_smm stalls on every write, thinkpad needs fan_control=1).
CONTROL_CHIP_RE = re.compile(
    r"^(it8\d{3}"                                 # it87 (in-tree and frankcrawford/it87)
    r"|nct6106|nct6116|nct6126|nct5585|nct67(?:7[569]|9[1-9])"  # nct6775 family
    r"|nct6687"                                    # nct6687d (MSI, out-of-tree)
    r"|f71[0-9]{3}[a-z]*|f81[0-9]{3}[a-z]*|f8000"  # f71882fg family
    r"|w83[0-9]{3}[a-z]*(?:-[a-z])?)$")            # w83627ehf family
IT87_SIV_RE = re.compile(r"^(it8\d{3})_[0-9a-fA-F]{8}$")   # some it87 builds append the chip's SIV
BOARD_CHIP_RE = CONTROL_CHIP_RE                   # kept for older imports

KNOWN_LABELS = {
    ("k10temp", "Tctl"): "CPU (Tctl)",
    ("k10temp", "Tdie"): "CPU (Tdie)",
    ("zenpower", "Tctl"): "CPU (Tctl)",
    ("zenpower", "Tdie"): "CPU (Tdie)",
    ("coretemp", "Package id 0"): "CPU package",
    ("amdgpu", "edge"): "GPU edge",
    ("amdgpu", "junction"): "GPU hotspot",
    ("amdgpu", "mem"): "GPU memory",
    ("nvme", "Composite"): "NVMe SSD",
}
CPU_CHIPS = ("k10temp", "zenpower", "coretemp")
# headers that must never stop, from driver labels (nct6687 "CPU Fan" / "Pump Fan", ASUS "CPU_OPT",
# "W_PUMP+", "AIO_PUMP"). Pumps are protected but do not tell us which fan is the CPU fan.
CPU_LABEL_RE = re.compile(r"cpu", re.I)
PUMP_LABEL_RE = re.compile(r"pump|aio", re.I)

# chips whose temps are not useful for fan control
IGNORED_CHIPS = {"iwlwifi", "iwlwifi_1"}
IGNORED_PREFIXES = ("hidpp", "BAT", "ucsi", "mt79", "ath1")

GPU_FAN_ID = "amdgpu/fan1"          # the (first) AMD card's firmware-curve fan; more cards: "amdgpu@<pci>/fan1"
PMFW_FAN_RE = re.compile(r"^amdgpu(?:@[^/]+)?/fan\d+$")   # RX 7000/9000 firmware curve
AMD_PWM_RE = re.compile(r"^amdgpu(?:@[^/]+)?/pwm\d+$")    # older AMD cards, manual pwm1 (beta)
GPU_CHIPS = ("amdgpu", "nouveau", "i915", "xe", "radeon")


def gc_major(device_dir: str) -> int | None:
    """Graphics core generation from amdgpu IP discovery (10 = RDNA1/2, 11 = RDNA3, 12 = RDNA4)."""
    v = _read_int(os.path.join(device_dir, "ip_discovery", "die", "0", "GC", "0", "major"))
    return v


def _writable(path: str) -> bool:
    """The kernel removes the write bit when a driver has no setter (amdgpu does this for pwm1)."""
    try:
        return bool(os.stat(path).st_mode & 0o200)
    except OSError:
        return False


def _read(path: str) -> str | None:
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    txt = _read(path)
    if txt is None:
        return None
    try:
        return int(txt.strip())
    except ValueError:
        return None


def _write(path: str, value: str) -> None:
    # sysfs wants a single write() call per value
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)


@dataclass
class Sensor:
    id: str
    label: str
    path: str  # tempN_input, millidegrees

    def read(self) -> float | None:
        v = _read_int(self.path)
        if v is None or not -40000 <= v <= 150000:
            return None   # e.g. it87 reports -128 / 127 for an open or shorted thermistor
        return v / 1000.0


# Temperature inputs of the nct6775 family that carry the CPU's temperature (Intel PECI, AMD TSI / SB-TSI,
# and the CPUTIN thermistor under the socket). SMBUSMASTER 0 is the CPU on boards that wire it; other
# SMBUSMASTER numbers vary, so they are never counted.
CPU_SOURCE_RE = re.compile(r"^(CPUTIN|PECI|TSI\d|AMD TSI|SMBUSMASTER 0\b)", re.I)


def bios_setup(hdir: str, idx: int, board_name: str = "") -> dict | None:
    """What the BIOS itself does with header idx, read from the chip (read only): the temperature it
    follows and its curve, plus a guess at what kind of header it is. Only for the nct6775 family, whose
    pwmN_temp_sel names one input; the it87 bitmask means different things in different driver builds, so
    nothing is guessed there. Guesses never change protection: they are hints and an order for 'Find my
    CPU fan'.

    guess: "cpu"      follows the CPU temperature (the CPU fan, or a case fan set to follow the CPU)
           "pump"     full speed at every temperature (the BIOS default for AIO_PUMP / W_PUMP headers)
           "chipset"  an X570 board, a non-CPU sensor, and the fan stops when cool (the chipset fan)
           None       nothing to go on"""
    sel = _read_int(os.path.join(hdir, f"pwm{idx}_temp_sel"))
    if not sel:
        return None
    source = (_read(os.path.join(hdir, f"temp{sel}_label")) or "").strip() or f"temp{sel}"
    points = []
    for k in range(1, 8):
        t = _read_int(os.path.join(hdir, f"pwm{idx}_auto_point{k}_temp"))
        p = _read_int(os.path.join(hdir, f"pwm{idx}_auto_point{k}_pwm"))
        if t is None or p is None:
            break
        points.append((round(t / 1000), round(p * 100 / 255)))
    cpu = bool(CPU_SOURCE_RE.match(source))
    full = bool(points) and all(p == 100 for _, p in points)
    stop_below = points[0][0] if points and points[0][1] == 0 else None
    guess = ("pump" if full else "cpu" if cpu else
             "chipset" if stop_below is not None and "X570" in (board_name or "").upper() else None)
    if full:
        hint = "The BIOS runs this header at full speed at every temperature, the usual setting for a pump header."
    else:
        hint = (f"The BIOS makes it follow the CPU temperature ({source})" if cpu else
                f"The BIOS makes it follow {source}, not the CPU")
        if points:
            lo = next(((t, p) for t, p in points if p > 0), points[0])
            hi = next(((t, p) for t, p in points if p == 100), points[-1])
            hint += f": {lo[1]} % at {lo[0]} °C, {hi[1]} % at {hi[0]} °C"
        if stop_below is not None:
            hint += f", stopped below {stop_below} °C"
        hint += "."
        if guess == "chipset":
            hint += " On X570 boards that is usually the chipset fan: leave it on Automatic."
        elif cpu:
            hint += " The CPU fan does this, but case fans often do too: 'Find my CPU fan' tells them apart."
    return {"source": source, "points": points, "guess": guess, "hint": hint}


@dataclass
class BoardFan:
    """A motherboard PWM header (it87)."""
    id: str
    label: str
    pwm_path: str
    enable_path: str
    rpm_path: str | None
    kind: str = "board"
    bios: dict | None = None      # the BIOS's own Smart Fan setup for this header (bios_setup), read once

    def rpm(self) -> int | None:
        return _read_int(self.rpm_path) if self.rpm_path else None

    def pwm(self) -> int | None:
        return _read_int(self.pwm_path)

    def enable(self) -> int | None:
        return _read_int(self.enable_path)

    def set_enable(self, value: int) -> None:
        _write(self.enable_path, str(int(value)))

    def set_pwm(self, value: int) -> None:
        _write(self.pwm_path, str(max(0, min(255, int(value)))))


@dataclass
class AmdPwmFan(BoardFan):
    """Older AMD card (GCN up to RDNA2): pwm1 / pwm1_enable like a motherboard header (1 manual, 2 auto).
    The card stays at the last speed if the controller dies, so it is handled exactly like a board fan:
    restore file, ExecStopPost and re-assertion. Never write pwm1_enable=0: on these cards it forces 100 %."""
    kind: str = "gpu"


@dataclass
class GpuFan:
    """RDNA3/4 GPU fan driven through the PMFW OverDrive fan curve."""
    id: str
    label: str
    hwmon_dir: str
    fan_ctrl_dir: str
    kind: str = "gpu"

    def rpm(self) -> int | None:
        return _read_int(os.path.join(self.hwmon_dir, "fan1_input"))

    def pwm(self) -> int | None:
        return _read_int(os.path.join(self.hwmon_dir, "pwm1"))


@dataclass
class Hardware:
    sensors: dict[str, Sensor] = field(default_factory=dict)
    fans: dict[str, BoardFan | GpuFan] = field(default_factory=dict)
    chips: dict[str, str] = field(default_factory=dict)      # chip id -> hwmon dir
    names: dict[str, str] = field(default_factory=dict)      # chip id -> hwmon name
    drivers: dict[str, str] = field(default_factory=dict)    # chip id -> kernel driver
    board_chips: list[str] = field(default_factory=list)     # controllable motherboard chips
    board_chip: str | None = None                            # the first of them (older callers)
    board: dict | None = None                                # board database entry, if known
    dmi: dict[str, str] = field(default_factory=dict)
    gpu_hints: list[str] = field(default_factory=list)       # why a graphics card fan can't be controlled
    cpu_fans: set[str] = field(default_factory=set)          # fans known to be CPU fans
    pump_fans: set[str] = field(default_factory=set)         # pump headers: always protected
    fan_inputs: dict[str, tuple[str, str]] = field(default_factory=dict)  # read-only fans: id -> (label, path)

    def default_cpu_sensor(self) -> str | None:
        """AMD: Tdie if the chip reports an offset, else Tctl (real on Zen 2+); Intel: package."""
        by_label = {s.label: sid for sid, s in self.sensors.items()}
        for want in ("CPU (Tdie)", "CPU (Tctl)", "CPU package"):
            if want in by_label:
                return by_label[want]
        return next((sid for sid in self.sensors if sid.split("@")[0].split("/")[0] in CPU_CHIPS), None)

    def default_gpu_sensor(self) -> str | None:
        by_label = {s.label: sid for sid, s in self.sensors.items()}
        if "GPU hotspot" in by_label:          # a discrete AMD card (an APU only reports "edge")
            return by_label["GPU hotspot"]
        nv = next((sid for sid in self.sensors if sid.startswith("nvidia")), None)
        if nv:
            return nv
        if "GPU edge" in by_label:
            return by_label["GPU edge"]
        return next((sid for sid in self.sensors if sid.split("@")[0].split("/")[0] in GPU_CHIPS), None)

    def is_cpu_fan(self, fid: str) -> bool:
        """True for a known CPU fan, and for every motherboard fan while nobody knows which one it is."""
        if fid in self.cpu_fans or fid in self.pump_fans:
            return True
        fan = self.fans.get(fid)
        return isinstance(fan, BoardFan) and fan.kind == "board" and not self.cpu_fans


def _chip_base(name: str) -> tuple[str, bool]:
    """Return (chip id, controllable board chip). Strips the SIV suffix some it87 builds add."""
    m = IT87_SIV_RE.match(name)
    base = m.group(1) if m else name
    return base, bool(CONTROL_CHIP_RE.match(base))


def _dmi(root: str) -> dict[str, str]:
    out = {}
    for k in ("board_vendor", "board_name", "sys_vendor", "product_name"):
        v = _read(os.path.join(root, "sys/class/dmi/id", k))
        if v is not None:
            out[k] = v.strip()
    return out


def _device_name(hdir: str) -> str | None:
    dev = os.path.join(hdir, "device")
    if not os.path.exists(dev):
        return None
    return re.sub(r"[^A-Za-z0-9:._-]", "_", os.path.basename(os.path.realpath(dev))) or None


def _driver(hdir: str) -> str:
    drv = os.path.join(hdir, "device", "driver")
    return os.path.basename(os.path.realpath(drv)) if os.path.exists(drv) else ""


def discover(root: str = "/") -> Hardware:
    hw = Hardware()
    hw.dmi = _dmi(root)
    base = os.path.join(root, "sys/class/hwmon")
    try:
        entries = sorted(os.listdir(base), key=lambda n: int(re.sub(r"\D", "", n) or 0))
    except OSError:
        return hw

    found = []                         # (hwmon dir, hwmon name, chip base, controllable)
    for entry in entries:
        hdir = os.path.join(base, entry)
        name = (_read(os.path.join(hdir, "name")) or "").strip()
        if not name or name in IGNORED_CHIPS or name.startswith(IGNORED_PREFIXES):
            continue
        chip, board = _chip_base(name)
        if board and _driver(hdir) == "nct6683":
            board = False                  # the in-tree nct6683 driver is read-only, whatever the name says
        found.append((hdir, name, chip, board))
    # stable ids: a chip name seen once keeps its plain name (e.g. "it8688"); names seen twice get the
    # device they belong to ("nvme@nvme1"), never the discovery order, which can change between boots
    count: dict[str, int] = {}
    for _, _, chip, _ in found:
        count[chip] = count.get(chip, 0) + 1
    ids = []
    for hdir, name, chip, board in found:
        if count[chip] > 1:
            chip = f"{chip}@{_device_name(hdir) or os.path.basename(hdir)}"
        ids.append((hdir, name, chip, board))

    for hdir, name, chip, board in ids:
        hw.chips[chip] = hdir
        hw.names[chip] = name
        hw.drivers[chip] = _driver(hdir)
        base_name = chip.split("@")[0]
        try:
            files = set(os.listdir(hdir))
        except OSError:
            continue
        entry = boards.find(hw.dmi, base_name) if board else None
        if board:
            hw.board_chips.append(chip)
            if hw.board_chip is None:
                hw.board_chip = chip
            if entry and hw.board is None:
                hw.board = entry

        # temperatures
        for fname in sorted(files, key=lambda f: (len(f), f)):
            m = re.fullmatch(r"temp(\d+)_input", fname)
            if not m:
                continue
            idx = int(m.group(1))
            raw_label = (_read(os.path.join(hdir, f"temp{idx}_label")) or "").strip()
            sen = Sensor(f"{chip}/temp{idx}", "", os.path.join(hdir, fname))
            if board and entry:
                if idx not in entry["temps"]:
                    continue                   # not connected on this board
                sen.label = entry["temps"][idx]
            else:
                if board:
                    v = sen.read()
                    if v is None or not 0 < v <= 120:
                        continue               # unknown board: -128 / 127 / -62 / exactly 0 = nothing connected
                                               # (nct6798 PCH_* channels on AMD boards read 0.000 °C)
                label = KNOWN_LABELS.get((base_name, raw_label))
                if label is None and base_name in ("coretemp",) and raw_label.startswith("Core "):
                    label = f"CPU core {raw_label[5:]}"
                if label is None and base_name in ("k10temp", "zenpower") and raw_label.startswith("Tccd"):
                    label = f"CPU die (CCD{raw_label[4:]})"
                if label is None:
                    pretty = {"gigabyte_wmi": "Board (WMI)", "acpitz": "ACPI zone", "nvme": "NVMe SSD",
                              "asusec": "Board (EC)", "asus_wmi_sensors": "Board (WMI)", "nouveau": "GPU",
                              "i915": "GPU", "xe": "GPU", "radeon": "GPU"}.get(base_name, base_name)
                    suffix = chip.split("@")[1] if "@" in chip else ""
                    label = f"{pretty}{' ' + suffix if suffix and base_name == 'nvme' else ''} " \
                            f"{raw_label or idx}".strip()
                sen.label = label
            hw.sensors[sen.id] = sen

        # motherboard PWM headers
        if board:
            for fname in sorted(files, key=lambda f: (len(f), f)):
                m = re.fullmatch(r"pwm(\d+)_enable", fname)
                if not m or f"pwm{m.group(1)}" not in files:
                    continue
                idx = int(m.group(1))
                fid = f"{chip}/pwm{idx}"
                rpm = os.path.join(hdir, f"fan{idx}_input") if f"fan{idx}_input" in files else None
                if entry:
                    if idx not in entry["fans"]:
                        continue               # header not on this board
                    label = entry["fans"][idx]["label"]
                    if entry["fans"][idx].get("cpu"):
                        hw.cpu_fans.add(fid)
                else:
                    if rpm is None:
                        continue               # no tachometer: nothing to show or check (mapping may add it)
                    fl = (_read(os.path.join(hdir, f"fan{idx}_label")) or "").strip()
                    label = fl or f"Fan {idx}"
                    if CPU_LABEL_RE.search(fl):
                        hw.cpu_fans.add(fid)
                    elif PUMP_LABEL_RE.search(fl):
                        hw.pump_fans.add(fid)
                hw.fans[fid] = BoardFan(fid, label, os.path.join(hdir, f"pwm{idx}"),
                                        os.path.join(hdir, f"pwm{idx}_enable"), rpm,
                                        bios=bios_setup(hdir, idx, hw.dmi.get("board_name", "")))
        else:
            # fans this program can only read (EC/WMI board sensors, laptops, ...)
            for fname in sorted(files, key=lambda f: (len(f), f)):
                m = re.fullmatch(r"fan(\d+)_input", fname)
                if not m or base_name == "amdgpu":
                    continue                       # decided with the card below
                idx = int(m.group(1))
                fl = (_read(os.path.join(hdir, f"fan{idx}_label")) or "").strip()
                hw.fan_inputs[f"{chip}/fan{idx}"] = (fl or f"{base_name} fan {idx}", os.path.join(hdir, fname))

        # AMD graphics card fan: firmware curve (RX 7000/9000), pwm1 (older, beta) or read-only
        if base_name == "amdgpu":
            dev = os.path.realpath(os.path.join(hdir, "device"))
            fc = os.path.join(dev, "gpu_od", "fan_ctrl")
            gen = gc_major(dev)
            pwm, en = os.path.join(hdir, "pwm1"), os.path.join(hdir, "pwm1_enable")
            label = "Graphics card fan" if "@" not in chip else f"Graphics card fan ({chip.split('@')[1]})"
            if os.path.isfile(os.path.join(fc, "fan_curve")):
                hw.fans[f"{chip}/fan1"] = GpuFan(f"{chip}/fan1", label, hdir, fc)
            elif (gen is not None and gen >= 11) or (gen is None and os.path.isdir(os.path.join(dev, "gpu_od"))):
                # RDNA3/4: the firmware ignores manual pwm1; only the OverDrive curve works (LACT does the same).
                # Only for a card that HAS a fan: an APU's built-in graphics (also GC 11+) has none.
                if "fan1_input" in files or "pwm1" in files:
                    hw.gpu_hints.append(f"{label}: RX 7000/9000 fan control needs OverDrive. Add the OverDrive bit "
                                        "(0x4000) to the amdgpu.ppfeaturemask kernel parameter, reboot, and FanPilot "
                                        "will find the firmware fan curve. Shown read-only until then.")
            elif "fan1_input" in files and _writable(pwm) and _writable(en):
                hw.fans[f"{chip}/pwm1"] = AmdPwmFan(f"{chip}/pwm1", label + " (beta)", pwm, en,
                                                    os.path.join(hdir, "fan1_input"))
            if not any(f.startswith(chip + "/") for f in hw.fans) and "fan1_input" in files:
                hw.fan_inputs[f"{chip}/fan1"] = (label, os.path.join(hdir, "fan1_input"))

    # readings that only duplicate others
    hide = set(hw.board["hide_chips"]) if hw.board else set()
    if any(c.startswith("it8") for c in hw.board_chips):
        hide.add("gigabyte_wmi")           # reads the same ITE Super I/O chip: exact duplicates
    if any(c.split("@")[0] in CPU_CHIPS for c in hw.chips):
        hide.add("acpitz")                 # a real CPU sensor exists; ACPI zones are often fake
    if hide:
        hw.sensors = {sid: sen for sid, sen in hw.sensors.items() if sid.split("/")[0].split("@")[0] not in hide}
        hw.fan_inputs = {fid: v for fid, v in hw.fan_inputs.items() if fid.split("/")[0].split("@")[0] not in hide}
    # motherboard headers first, graphics card last
    hw.fans = dict(sorted(hw.fans.items(), key=lambda kv: (kv[1].kind == "gpu", kv[0])))
    return hw


# --- why are the motherboard fans missing? ------------------------------------

READ_ONLY_BOARD_CHIPS = {"asusec": "asus-ec-sensors", "asus_wmi_sensors": "asus_wmi_sensors",
                         "gigabyte_wmi": "gigabyte-wmi", "hp_wmi_sensors": "hp-wmi-sensors",
                         "nct6683": "nct6683"}


def diagnose(hw: Hardware, root: str = "/") -> list[str]:
    """Board hints first (the self-test shows the first one when no fan chip was found), then
    graphics-card hints, which apply whatever the motherboard has."""
    return _board_diagnose(hw, root) + list(hw.gpu_hints)


def _board_diagnose(hw: Hardware, root: str = "/") -> list[str]:
    """Plain-language hints when no controllable fan chip was found. Reads /sys only; no guessing about
    kernel parameters (acpi_enforce_resources=lax can break booting, so it is never suggested silently)."""
    if hw.board_chips:
        return []
    hints = []
    plat = os.path.join(root, "sys/bus/platform/devices")
    try:
        devices = os.listdir(plat)
    except OSError:
        devices = []
    names = {hw.names[c] for c in hw.chips} | ({"nct6683"} if "nct6683" in hw.drivers.values() else set())
    vendor = (hw.dmi.get("board_vendor") or hw.dmi.get("sys_vendor") or "").casefold()
    if "nct6683" in names:
        hints.append("The fan chip is handled by the nct6683 driver, which can only read fans on most boards. "
                     "MSI boards: the out-of-tree nct6687d driver (github.com/Fred78290/nct6687d) adds control.")
    for n in sorted(names & set(READ_ONLY_BOARD_CHIPS) - {"nct6683"}):
        hints.append(f"'{n}' gives readings only (by design); the fan chip itself needs its own driver.")
    loaded_no_hwmon = [d for d in devices if re.match(r"^(it87|nct6775|nct6687|f71882fg|w83627ehf)\.", d)
                       and not os.path.isdir(os.path.join(plat, d, "hwmon"))]
    if loaded_no_hwmon:
        hints.append(f"The fan chip was found ({', '.join(loaded_no_hwmon)}) but its driver did not start; the "
                     "kernel log ('sudo dmesg | grep -i -e it87 -e nct6') usually says why (often an ACPI "
                     "resource conflict).")
    if "gigabyte" in vendor:
        hints.append("Gigabyte boards use ITE chips (IT8686E/IT8688E/IT8689E...), which need the out-of-tree it87 "
                     "driver: run 'sudo fanpilot-install-driver' (package) or 'sudo scripts/install-driver.sh' "
                     "(source folder).")
    elif "asus" in vendor:
        hints.append("ASUS boards usually use a Nuvoton chip: try 'sudo modprobe nct6775'. If the kernel log then "
                     "mentions an ACPI resource conflict, see SETUP.md (docs/ in the source, /usr/share/doc/fanpilot/ for "
                     "packages; don't change kernel parameters blindly).")
    elif "micro-star" in vendor or vendor.startswith("msi"):
        hints.append("MSI boards: newer ones need the out-of-tree nct6687d driver; older ones work with "
                     "'sudo modprobe nct6775'.")
    hints.append("To find the chip: install lm_sensors and run 'sudo sensors-detect', then send "
                 "'fanpilotctl report' with a bug report.")
    return hints
