# SPDX-License-Identifier: GPL-3.0-or-later
"""Run fanpilotd against a simulated reference machine (no root, no real hardware).

    python3 tests/simdaemon.py /tmp/fp.sock      # then: python3 -m fanpilot.gui --socket /tmp/fp.sock
"""
import json
import math
import os
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fanpilot import amdgpu  # noqa: E402
from fanpilot.daemon import Daemon  # noqa: E402
from sim import FakeGpu, FakeMachine, w  # noqa: E402


def main():
    sock = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fanpilot-sim.sock"
    no_board = "--no-board" in sys.argv
    m = FakeMachine(with_board=not no_board)
    gpu = FakeGpu(m.fc)
    real = amdgpu._write
    amdgpu._write = lambda p, v: gpu.write(p, v) if p.startswith(m.fc) else real(p, v)
    cfg_path = os.path.join(m.root, "etc/config.json")
    os.makedirs(os.path.dirname(cfg_path))
    cfg = {"allowed_uids": [] if "--readonly" in sys.argv else [os.getuid()],
           "fans": {"it8688/pwm1": {"mode": "curve"}, "it8688/pwm2": {"mode": "fixed", "fixed": 40},
                    "amdgpu/fan1": {"mode": "curve"}},
           "calibration": {"it8688/pwm3": {"max_rpm": 1500, "stop_duty": 20, "start_duty": 30,
                                           "min_safe_duty": 35, "table": []}}}
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    d = Daemon(m.root, cfg_path, os.path.join(m.root, "run"), sock)

    def physics():
        t0 = time.time()
        gh = os.path.join(m.root, "sys/class/hwmon/hwmon2")
        while True:
            t = time.time() - t0
            m.cpu(52 + 8 * math.sin(t / 20) + 2 * math.sin(t * 1.3))
            m.set_temp(gh, 2, 58 + 6 * math.sin(t / 30))
            if m.board:
                for i in (1, 2, 3):
                    m.spin(i)
            pts = gpu.committed
            w(os.path.join(gh, "fan1_input"), f"{int((pts[2][1] if pts else 0) * 30)}\n")
            w(os.path.join(gh, "pwm1"), f"{int((pts[2][1] if pts else 0) * 2.55)}\n")
            time.sleep(0.5)
    threading.Thread(target=physics, daemon=True).start()
    print("simulated daemon on", sock, "root", m.root, flush=True)
    try:
        d.run()
    finally:
        m.cleanup()


if __name__ == "__main__":
    main()
