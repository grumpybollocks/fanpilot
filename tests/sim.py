# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds a fake sysfs tree that looks like the reference machine (Gigabyte B550M AORUS ELITE) with the it87 driver loaded.

Regular files cannot run code on write, so the GPU's stage/commit firmware
semantics are emulated by ``FakeGpu.process()``, which tests call after the
code under test has written to ``fan_curve`` (via a write-log hook).
"""
from __future__ import annotations

import os
import tempfile

# GUI tests keep QSettings in a scratch folder, never the real ~/.config (also when a test file is
# run directly instead of through `unittest discover`, where tests/__init__.py does the same)
if not os.environ.get("XDG_CONFIG_HOME", "").startswith(tempfile.gettempdir()):
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="fanpilot-test-config-")
if not os.environ.get("XDG_RUNTIME_DIR", "").startswith(tempfile.gettempdir()):
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="fanpilot-test-run-")

GPU_CURVE_EMPTY = """OD_FAN_CURVE:
0: 0C 0%
1: 0C 0%
2: 0C 0%
3: 0C 0%
4: 0C 0%
OD_RANGE:
FAN_CURVE(hotspot temp): 25C 100C
FAN_CURVE(fan speed): 20% 100%
"""

ZERO_RPM = "FAN_ZERO_RPM_ENABLE:\n{v}\nOD_RANGE:\nZERO_RPM_ENABLE: 0 1\n"


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text))


def r(path):
    with open(path) as f:
        return f.read()


class FakeMachine:
    def __init__(self, board_chip="it8688", with_board=True, with_gpu=True):
        self.root = tempfile.mkdtemp(prefix="fanpilot-sim-")
        hw = os.path.join(self.root, "sys/class/hwmon")
        os.makedirs(hw)
        self.k10 = os.path.join(hw, "hwmon3")
        w(f"{self.k10}/name", "k10temp\n")
        w(f"{self.k10}/temp1_input", "45000\n")
        w(f"{self.k10}/temp1_label", "Tctl\n")
        w(f"{self.k10}/temp3_input", "44500\n")
        w(f"{self.k10}/temp3_label", "Tccd1\n")
        wmi = os.path.join(hw, "hwmon4")
        w(f"{wmi}/name", "gigabyte_wmi\n")
        for i in range(1, 7):
            w(f"{wmi}/temp{i}_input", f"{30 + i}000\n")
        bat = os.path.join(hw, "hwmon6")
        w(f"{bat}/name", "hidpp_battery_0\n")
        self.board = None
        # the simulated machine is the reference board (an unknown board is simulated by changing these)
        w(f"{self.root}/sys/class/dmi/id/board_vendor", "Gigabyte Technology Co., Ltd.\n")
        w(f"{self.root}/sys/class/dmi/id/board_name", "B550M AORUS ELITE\n")
        if with_board:
            self.board = os.path.join(hw, "hwmon7")
            w(f"{self.board}/name", f"{board_chip}\n")
            for i in range(1, 7):
                w(f"{self.board}/temp{i}_input", f"{35 + i}000\n")
            for i in range(1, 6):
                w(f"{self.board}/fan{i}_input", "0\n")
                w(f"{self.board}/pwm{i}", "128\n")
                w(f"{self.board}/pwm{i}_enable", "2\n")
        self.gpu_dev = None
        if with_gpu:
            self.gpu_dev = os.path.join(self.root, "sys/devices/pci0000:00/0000:08:00.0")
            self.fc = os.path.join(self.gpu_dev, "gpu_od/fan_ctrl")
            w(f"{self.fc}/fan_curve", GPU_CURVE_EMPTY)
            w(f"{self.fc}/fan_zero_rpm_enable", ZERO_RPM.format(v=1))
            gh = os.path.join(hw, "hwmon2")
            w(f"{gh}/name", "amdgpu\n")
            w(f"{gh}/fan1_input", "0\n")
            w(f"{gh}/pwm1", "0\n")
            for i, (lab, v) in enumerate([("edge", 31000), ("junction", 33000), ("mem", 52000)], 1):
                w(f"{gh}/temp{i}_input", f"{v}\n")
                w(f"{gh}/temp{i}_label", f"{lab}\n")
            os.symlink(self.gpu_dev, f"{gh}/device")

    # -- board helpers
    def set_temp(self, chip_dir, idx, celsius):
        w(f"{chip_dir}/temp{idx}_input", f"{int(celsius * 1000)}\n")

    def cpu(self, celsius):
        self.set_temp(self.k10, 1, celsius)

    def pwm(self, i):
        return int(r(f"{self.board}/pwm{i}"))

    def enable(self, i):
        return int(r(f"{self.board}/pwm{i}_enable"))

    def set_enable(self, i, v):
        w(f"{self.board}/pwm{i}_enable", f"{v}\n")

    def it87_report(self):
        """Emulate it87.c pwm_mode(): a manual fan whose duty is 255 reads back as mode 0."""
        for i in (1, 2, 3):
            if self.enable(i) == 1 and self.pwm(i) == 255:
                self.set_enable(i, 0)

    def spin(self, i, stop_below=25):
        """Crude fan model: rpm follows the pwm, stops below ``stop_below`` %."""
        duty = self.pwm(i) * 100 / 255
        w(f"{self.board}/fan{i}_input", f"{0 if duty < stop_below else int(duty * 15)}\n")

    def cleanup(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


class FakeGpu:
    """Emulates the kernel/PMFW behaviour of gpu_od/fan_ctrl on plain files.

    Replaces amdgpu._write so every write is interpreted like the kernel does:
    point writes are staged, 'c' commits all staged points (EIO if any point
    is outside the range), 'r' restores the boot table.
    """

    def __init__(self, fc_dir, fail_point=None):
        self.fc = fc_dir
        self.staged = {}
        self.staged_zero = None
        self.committed = None   # list of (t, d) or None = auto
        self.zero = 1
        self.commits = 0
        self.resets = 0
        self.fail_point = fail_point
        self.log = []

    def write(self, path, value):
        name = os.path.basename(path)
        v = value.strip()
        self.log.append((name, v))
        if name == "fan_curve":
            if v == "c":
                pts = [self.staged.get(i, (0, 0)) for i in range(5)]
                if any(not (25 <= t <= 100 and 20 <= d <= 100) for t, d in pts):
                    raise OSError(5, "Input/output error")
                self.committed = pts
                if self.staged_zero is not None:
                    self.zero = self.staged_zero
                self.commits += 1
                self.staged, self.staged_zero = {}, None
            elif v == "r":
                self.committed, self.staged = None, {}
                self.resets += 1
            else:
                i, t, d = (int(x) for x in v.split())
                if self.fail_point == i:
                    raise OSError(22, "Invalid argument")
                self.staged[i] = (t, d)
            self._render()
        elif name == "fan_zero_rpm_enable":
            if v == "r":
                self.zero, self.staged_zero = 1, None
            else:
                self.staged_zero = int(v)
            w(path, ZERO_RPM.format(v=self.zero))
        else:
            raise OSError(13, "Permission denied")

    def _render(self):
        pts = self.committed or [(0, 0)] * 5
        body = "OD_FAN_CURVE:\n" + "".join(f"{i}: {t}C {d}%\n" for i, (t, d) in enumerate(pts))
        body += "OD_RANGE:\nFAN_CURVE(hotspot temp): 25C 100C\nFAN_CURVE(fan speed): 20% 100%\n"
        w(os.path.join(self.fc, "fan_curve"), body)


def machine_from_report(report: dict) -> "FakeMachine":
    """Rebuild a simulated machine from a `fanpilotctl report` (fanpilot/report.py) file.

    hwmon attributes become plain files (read-only ones get mode 0444), each chip's `device` points at a
    device directory with a `driver` link, graphics cards share that directory with their hwmon chip, and
    the fan_ctrl files are written as reported. Everything else about FakeMachine (GPU emulation hooks
    aside) works the same, so the daemon, readings and GUI run against the reported hardware."""
    m = FakeMachine.__new__(FakeMachine)
    m.root = tempfile.mkdtemp(prefix="fanpilot-report-")
    m.board = m.gpu_dev = m.fc = m.k10 = None
    r = m.root
    for k, v in (report.get("dmi") or {}).items():
        w(f"{r}/sys/class/dmi/id/{k}", v + "\n")
    if report.get("cpu"):
        w(f"{r}/proc/cpuinfo", f"processor\t: 0\nmodel name\t: {report['cpu']}\n")

    def device_dir(name, driver):
        d = os.path.join(r, "sys/devices", name or "unknown")
        os.makedirs(d, exist_ok=True)
        if driver and not os.path.exists(os.path.join(d, "driver")):
            drv = os.path.join(r, "sys/bus/drivers", driver)
            os.makedirs(drv, exist_ok=True)
            os.symlink(drv, os.path.join(d, "driver"))
        return d

    for h in report.get("hwmon", []):
        hd = os.path.join(r, "sys/class/hwmon", h["dir"])
        os.makedirs(hd, exist_ok=True)
        for name, a in (h.get("attrs") or {}).items():
            p = os.path.join(hd, name)
            w(p, a.get("v") if a.get("v") is not None else "")
            os.chmod(p, 0o644 if a.get("w") else 0o444)
        if h.get("device"):
            os.symlink(device_dir(h["device"], h.get("driver")), os.path.join(hd, "device"))
        name = (h.get("attrs", {}).get("name", {}).get("v") or "").strip()
        if name.startswith(("it8", "nct", "f71", "w83")) and m.board is None:
            m.board = hd
        if name == "k10temp":
            m.k10 = hd
    for c in report.get("drm", []):
        dev = device_dir(c.get("device"), c.get("driver"))
        cd = os.path.join(r, "sys/class/drm", c["card"])
        os.makedirs(cd, exist_ok=True)
        if not os.path.exists(os.path.join(cd, "device")):
            os.symlink(dev, os.path.join(cd, "device"))
        for k, v in (c.get("attrs") or {}).items():
            w(os.path.join(dev, k), v)
        if c.get("fan_ctrl"):
            m.gpu_dev = dev
            m.fc = os.path.join(dev, "gpu_od", "fan_ctrl")
            for name, a in c["fan_ctrl"].items():
                p = os.path.join(m.fc, name)
                w(p, a.get("v") or "")
                os.chmod(p, 0o644 if a.get("w") else 0o444)
    return m
