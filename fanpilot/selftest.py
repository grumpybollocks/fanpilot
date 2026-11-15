# SPDX-License-Identifier: GPL-3.0-or-later
"""In-service self-test, started from the GUI or `fanpilotctl selftest`.

Runs inside fanpilotd's control loop (so it needs no extra privileges), one step per tick,
like calibration. It holds only the fans listed in ``busy``; everything else keeps normal
control. The CPU fan is only read. Every fan goes back to its configured mode afterwards.
"""
from __future__ import annotations

import json
import os
import time

from . import amdgpu
from .curves import duty_to_pwm
from .hw import BoardFan, GpuFan

ABORT_CPU = 80.0
HIGH, LOW = 100.0, 35.0


class SelfTest:
    def __init__(self, daemon):
        self.d = daemon
        self.state = "running"
        self.step = "Starting"
        self.progress = 0.0
        self.results: list[dict] = []
        self.busy: set[str] = set()
        self.next_at = 0.0
        self.started = time.time()
        self._gen = self._steps()

    # -- helpers ------------------------------------------------------------------
    def _add(self, name: str, ok, detail: str = "") -> None:
        """ok: True = pass, False = fail, None = skipped / not applicable."""
        self.results.append({"name": name, "ok": ok, "detail": detail})

    def _board_fans(self):
        return [f for f in self.d.hw.fans.values() if isinstance(f, BoardFan) and f.kind == "board"]

    def _set(self, fan: BoardFan, duty: float) -> None:
        self.d._take(fan)              # records the original mode before the first write
        if fan.enable() != 1 or duty < 100:
            fan.set_enable(1)
        fan.set_pwm(duty_to_pwm(duty))

    # -- the test -----------------------------------------------------------------
    def _steps(self):
        d = self.d
        total = 7.0
        # 1. hardware and sensors
        self.step = "Checking hardware"
        from .hw import diagnose
        self._add("Motherboard fan chip", bool(d.hw.board_chip),
                  ", ".join(d.hw.board_chips) if d.hw.board_chips else
                  "not found: " + (diagnose(d.hw, d.root) or ["see docs/SETUP.md"])[0])
        gpu = next((f for f in d.hw.fans.values() if isinstance(f, GpuFan)), None)   # first AMD firmware-curve card
        if isinstance(gpu, GpuFan):
            self._add("Graphics card fan interface", True, "firmware fan curve available")
        else:   # no RX 7000/9000 curve on this PC: nothing to test there (not a failure)
            other = [f.label for f in d.hw.fans.values() if f.kind == "gpu"]
            self._add("Graphics card fan interface", None,
                      f"not tested ({', '.join(other)})" if other else "no controllable graphics card fan")
        for kind, what in (("cpu", "CPU temperature"), ("gpu", "GPU temperature")):
            sid = d.crit_sensor(kind)
            v = d.temps.get(sid) if sid else None
            if sid is None and kind == "gpu":
                self._add(f"{what} sensor", None, "no graphics card temperature on this PC")
                continue
            self._add(f"{what} sensor", v is not None, f"{v:.1f} °C" if v is not None else f"{sid} unreadable")
        cpu_fan = next((f for f in self._board_fans() if d.known_cpu_fan(f.id)), None)
        if cpu_fan:
            rpm = cpu_fan.rpm()
            self._add("CPU fan spinning (read only)", bool(rpm), f"{rpm} RPM" if rpm is not None else "no reading")
        self.progress = 1 / total
        yield 0.5

        # 2. speed control on each system fan
        tested = []
        if not any(d.known_cpu_fan(f.id) for f in self._board_fans()) and self._board_fans():
            # unknown board: any of these could be the CPU fan, which must never be slowed for a test
            self._add("Speed control", None, "skipped: mark which fan is the CPU fan first")
        for n, fan in enumerate(f for f in self._board_fans() if not d.protect_cpu(f.id)):
            self.busy.add(fan.id)
            self.step = f"{fan.label}: full speed"
            self._set(fan, HIGH)
            yield 7
            hi = fan.rpm() or 0
            self.step = f"{fan.label}: {LOW:.0f} %"
            self._set(fan, LOW)
            yield 8
            lo = fan.rpm() or 0
            if hi <= 0:
                self._add(f"{fan.label}: speed control", None, "no fan connected (0 RPM at 100 %)")
            else:
                ok = lo < hi * 0.85 and fan.enable() == 1
                self._add(f"{fan.label}: speed control", ok, f"100 % = {hi} RPM, {LOW:.0f} % = {lo} RPM")
                tested.append(fan)
            from .daemon import safe_enable_value
            entry = d.restore["pwm_enable"].get(fan.enable_path) or {}
            want = safe_enable_value(entry.get("orig", 2) if isinstance(entry, dict) else entry)
            d._release(fan)
            yield 1
            now_mode = fan.enable()
            # exactly the mode it had before (it87 BIOS: 2, Nuvoton Smart Fan IV: 5, ...); 0 = full is the fallback
            self._add(f"{fan.label}: back to BIOS control", now_mode in (want, 0), f"mode {now_mode} (was {want})")
            self.busy.discard(fan.id)
            d.states[fan.id].reset()
            self.progress = (2 + n) / total

        # 3. crash recovery: run the same restore routine systemd runs after a crash
        self.step = "Crash recovery"
        fan = tested[0] if tested else next((f for f in self._board_fans() if not d.protect_cpu(f.id)), None)
        if fan is None:
            self._add("Crash recovery", None, "no system fan to test with")
        else:
            from .daemon import restore_from_file, safe_enable_value
            self.busy.add(fan.id)
            self._set(fan, 40)
            entry = d.restore["pwm_enable"].get(fan.enable_path) or {}
            orig = entry.get("orig", 2) if isinstance(entry, dict) else entry
            yield 2
            path = os.path.join(d.run_dir, "selftest-restore.json")
            with open(path, "w") as f:
                json.dump({"pwm_enable": {fan.enable_path: entry}}, f)
            errs = restore_from_file(path)
            try:
                os.unlink(path)   # already gone after a successful restore
            except OSError:
                pass
            now_mode = fan.enable()
            want = safe_enable_value(orig)
            self._add("Crash recovery: fans handed back", not errs and now_mode in (want, 0),
                      f"mode {now_mode} after the restore routine" + (f"; {'; '.join(errs)}" if errs else ""))
            d._release(fan)
            self.busy.discard(fan.id)
            d.asserted.discard(fan.id)
            d.states[fan.id].reset()
            unit = next((u for u in (os.path.join(d.root, "etc/systemd/system/fanpilotd.service"),     # install.sh
                                     os.path.join(d.root, "usr/lib/systemd/system/fanpilotd.service"))  # packages
                         if os.path.exists(u)), os.path.join(d.root, "etc/systemd/system/fanpilotd.service"))
            try:
                with open(unit) as f:
                    text = f.read()
                ok = "ExecStopPost=" in text and "--restore" in text and "WatchdogSec=" in text
                self._add("Crash recovery: systemd service set up", ok,
                          "restore after every stop + watchdog" if ok else "ExecStopPost/WatchdogSec missing")
            except OSError:
                self._add("Crash recovery: systemd service set up", None, "not running as an installed service")
        self.progress = 5 / total
        yield 0.5

        # 4. graphics card
        if isinstance(gpu, GpuFan):
            self.busy.add(gpu.id)
            self.step = "Graphics card fan: 40 %"
            try:
                info = amdgpu.read_info(gpu.fan_ctrl_dir)
                d._gpu_restore(gpu.fan_ctrl_dir, True)
                pts = amdgpu.flat_points(info, 40)
                amdgpu.apply(gpu.fan_ctrl_dir, pts, False)
                written = amdgpu.read_info(gpu.fan_ctrl_dir).points == pts
                self._add("Graphics card: curve written to firmware", written,
                          "read back exactly" if written else "read-back differs")
                yield 10
                rpm = gpu.rpm() or 0
                self._add("Graphics card: fan spinning at 40 %", rpm > 0, f"{rpm} RPM")
                errs = amdgpu.reset(gpu.fan_ctrl_dir)
                back = amdgpu.read_info(gpu.fan_ctrl_dir).points
                ok = not errs and all(t == 0 and s == 0 for t, s in back)
                self._add("Graphics card: back to the card's control", ok,
                          "firmware table reset" if ok else "; ".join(errs) or str(back))
            except (OSError, amdgpu.GpuError) as e:
                self._add("Graphics card", False, str(e))
            d.gpu[gpu.id].applied = None    # normal control re-applies the configured mode
            self.busy.discard(gpu.id)
        self.progress = 1.0
        self.finish("done")

    # -- driving ------------------------------------------------------------------
    def tick(self, now: float) -> None:
        if self.state != "running":
            return
        cpu = self.d.temps.get(self.d.crit_sensor("cpu") or "")
        if self.d.critical or cpu is None or cpu >= ABORT_CPU:
            self.abort(f"CPU at {cpu} °C" if cpu is not None else "CPU temperature unreadable")
            return
        if now < self.next_at:
            return
        try:
            wait = next(self._gen)
            self.next_at = now + wait
        except StopIteration:
            pass
        except OSError as e:
            self.abort(f"hardware error: {e}")

    def finish(self, state: str) -> None:
        self.state = state
        self.step = "Finished" if state == "done" else self.step
        for fid in list(self.busy):
            fan = self.d.hw.fans.get(fid)
            if isinstance(fan, BoardFan):
                self.d.states[fid].reset()
            self.busy.discard(fid)
        for g in self.d.gpu.values():
            g.applied = None

    def abort(self, why: str) -> None:
        if self.state == "running":
            self._add("Stopped early", False, why)
            self.step = f"Stopped: {why}"
            self.finish("aborted")

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r["ok"] is True)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r["ok"] is False)

    def info(self) -> dict:
        return {"state": self.state, "step": self.step, "progress": round(self.progress, 2),
                "results": self.results, "passed": self.passed, "failed": self.failed,
                "started": self.started}
