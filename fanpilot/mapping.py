# SPDX-License-Identifier: GPL-3.0-or-later
"""'Map my fans': which fan (tachometer) does each motherboard header drive?

The method of lm_sensors' pwmconfig, made gentler: every header at full speed, then one header at a time
slowed to 30 % (never stopped; above the CPU fan's 20 % floor) while the others stay at full speed. A
tachometer whose speed drops below 5/6 of its full-speed value belongs to that header; a header that moves
no tachometer has no fan (or a fan without a speed wire). Runs inside fanpilotd's control loop like the
self-test, aborts on heat, and every header goes back to exactly its original mode afterwards.
"""
from __future__ import annotations

import os
import re
import time

from .curves import duty_to_pwm
from .hw import BoardFan

ABORT_CPU = 75.0
LOW = 30.0
SETTLE, RECOVER = 8, 5


class FanMapping:
    def __init__(self, daemon):
        self.d = daemon
        self.state = "running"
        self.step = "Starting"
        self.progress = 0.0
        self.busy: set[str] = set()
        self.result: dict[str, str | None] = {}     # fan id -> "fanN_input" or None (no fan)
        self.details: list[str] = []
        self.next_at = 0.0
        self.started = time.time()
        self.headers = self._headers()
        self._gen = self._steps()

    def _headers(self) -> dict[str, BoardFan]:
        """Headers to test: the known ones, plus on boards without a database entry every PWM channel
        (also those hidden because no tachometer has their number)."""
        hw = self.d.hw
        out = {fid: f for fid, f in hw.fans.items() if isinstance(f, BoardFan) and f.kind == "board"}
        if hw.board is None:
            for chip in hw.board_chips:
                hdir = hw.chips[chip]
                for fname in sorted(os.listdir(hdir), key=lambda f: (len(f), f)):
                    m = re.fullmatch(r"pwm(\d+)_enable", fname)
                    fid = f"{chip}/pwm{m.group(1)}" if m else None
                    if m and fid not in out and os.path.exists(os.path.join(hdir, f"pwm{m.group(1)}")):
                        out[fid] = BoardFan(fid, f"Fan {m.group(1)}", os.path.join(hdir, f"pwm{m.group(1)}"),
                                            os.path.join(hdir, fname), None)
        return out

    def _tachs(self) -> dict[str, int]:
        hw = self.d.hw
        out = {}
        for chip in hw.board_chips:
            hdir = hw.chips[chip]
            for fname in os.listdir(hdir):
                if re.fullmatch(r"fan\d+_input", fname):
                    try:
                        with open(os.path.join(hdir, fname)) as f:
                            out[f"{chip}/{fname}"] = int(f.read().strip())
                    except (OSError, ValueError):
                        pass
        return out

    def _set(self, fan: BoardFan, duty: float) -> None:
        self.d._take(fan)
        if fan.enable() != 1 or duty < 100:
            fan.set_enable(1)
        fan.set_pwm(duty_to_pwm(duty))

    def _steps(self):
        fans = list(self.headers.values())
        for fan in fans:
            self.busy.add(fan.id)
            self._set(fan, 100)
        self.step = "All headers at full speed"
        yield SETTLE
        base = self._tachs()
        if not any(v > 0 for v in base.values()):
            self.details.append("No fan speed readings on this chip: nothing to map.")
            self.finish("done")
            return
        spinning_own = []
        for n, fan in enumerate(fans):
            self.step = f"Slowing {fan.label} to {LOW:.0f} %"
            self.progress = (n + 0.5) / len(fans)
            self._set(fan, LOW)
            yield SETTLE
            now = self._tachs()
            chip, ch = fan.id.split("/")
            own = f"{chip}/fan{ch[3:]}_input"          # the tachometer with the header's own number
            dropped = sorted(k for k, v in base.items() if v > 0 and now.get(k, v) < v * 5 / 6)
            same_chip = [k for k in dropped if k.split("/")[0] == chip]
            self._set(fan, 100)
            if same_chip:
                tach = own if own in same_chip else same_chip[0]
                self.result[fan.id] = tach.split("/")[-1]
                extra = f" (also {', '.join(d.split('/')[-1][:-6] for d in same_chip if d != tach)}: a splitter?)" \
                    if len(same_chip) > 1 else ""
                self.details.append(f"{fan.label} ({fan.id}) drives {tach.split('/')[-1][:-6]}{extra}")
            elif dropped:
                self.details.append(f"{fan.label} ({fan.id}) slows a fan on another chip; left as it was")
            elif self.d.protect_cpu(fan.id) or self.d.hw.board is not None:
                why = ("the board database lists it" if self.d.hw.board is not None else
                       "it may be the CPU fan: mark the CPU fan first, then map again" if not self.d.known_cpu_fan(fan.id)
                       else "a CPU fan or pump is never hidden")
                self.details.append(f"{fan.label} ({fan.id}): no fan slowed down; kept ({why})")
            elif base.get(own, 0) > 0:
                # its own tachometer spins but didn't slow: a 3-pin DC fan on a PWM header or a pump without
                # speed control -- unless another header turns out to drive that tachometer (decided below)
                spinning_own.append((fan, own))
            else:
                self.result[fan.id] = None
                self.details.append(f"{fan.label} ({fan.id}): nothing connected; hidden "
                                    "(Settings → Forget the fan map brings it back)")
            self.progress = (n + 1) / len(fans)
            yield RECOVER
        claimed = {f"{fid.split('/')[0]}/{t}" for fid, t in self.result.items() if t}
        for fan, own in spinning_own:
            if own in claimed:
                self.result[fan.id] = None
                self.details.append(f"{fan.label} ({fan.id}): nothing connected (its numbered tachometer belongs "
                                    "to another header); hidden (Settings → Forget the fan map brings it back)")
            else:
                self.details.append(f"{fan.label} ({fan.id}): its fan did not slow down at {LOW:.0f} % (DC fan or "
                                    "pump without speed control?); kept as it is")
        self.step = "Finished"
        self.finish("done")

    # -- driving (same rules as the self-test) ------------------------------------
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
            self.next_at = now + next(self._gen)
        except StopIteration:
            pass
        except OSError as e:
            self.abort(f"hardware error: {e}")

    def finish(self, state: str) -> None:
        self.state = state
        for fid in list(self.busy):
            fan = self.headers.get(fid)
            if fan is not None:
                self.d._release(fan)          # every header back to exactly its original mode; fans you set
            self.d.states[fid].reset()        # to Fixed/Curve are taken over again on the next tick
            self.busy.discard(fid)

    def abort(self, why: str) -> None:
        if self.state == "running":
            self.step = f"Stopped: {why}"
            self.details.append(f"Stopped early: {why}")
            self.result = {}
            self.finish("aborted")

    def info(self) -> dict:
        return {"state": self.state, "step": self.step, "progress": round(self.progress, 2),
                "details": self.details, "result": self.result, "started": self.started}
