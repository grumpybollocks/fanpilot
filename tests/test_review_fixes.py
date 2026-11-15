# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the findings of the independent review (2026-11-14)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fanpilot import amdgpu, config as cfgmod, daemon as dmod  # noqa: E402
from fanpilot.daemon import restore_from_file  # noqa: E402
from test_daemon import Base  # noqa: E402


class ReviewFixes(Base):
    def test_failed_restore_save_blocks_takeover(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 30}}})
        with mock.patch.object(dmod, "_atomic_json", side_effect=OSError(28, "No space left")):
            self.ticks(d, 3)
        self.assertEqual(self.m.enable(2), 2)          # never taken over
        self.assertEqual(d.restore["pwm_enable"], {})  # nothing pretended
        self.ticks(d)                                  # disk OK again -> works
        self.assertEqual(self.m.enable(2), 1)
        self.assertTrue(os.path.exists(self.restore_file))

    def test_no_stop_without_allow_stop(self):
        c = cfgmod.validate({"fans": {"it8688/pwm2": {"min_duty": 0, "allow_stop": False}}})
        self.assertEqual(c["fans"]["it8688/pwm2"]["min_duty"], cfgmod.NONSTOP_FLOOR)
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 0, "min_duty": 0}}})
        self.ticks(d)
        self.assertEqual(self.m.pwm(2), round(cfgmod.NONSTOP_FLOOR * 2.55))

    def test_one_broken_fan_does_not_skip_others(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 50},
                                  "it8688/pwm3": {"mode": "fixed", "fixed": 60}}})
        real = d.control_board_fan

        def boom(fan, dt, now):
            if fan.id == "it8688/pwm2":
                raise KeyError("bug")
            return real(fan, dt, now)
        d.control_board_fan = boom
        with self.assertRaises(RuntimeError):
            d.tick(1, 1)
        self.assertEqual(self.m.pwm(3), round(60 * 2.55))

    def test_repeated_tick_failure_restores_and_exits(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 50}}})
        d.cfg["interval"] = 0.5
        calls = {"n": 0}
        real = d.tick

        def tick(now, dt):
            calls["n"] += 1
            if calls["n"] == 1:
                return real(now, dt)
            raise RuntimeError("bug")
        d.tick = tick
        self.assertEqual(d.run(), 1)
        self.assertEqual(self.m.enable(2), 2)
        self.assertEqual(calls["n"], 1 + dmod.TICK_FAIL_LIMIT)

    def test_writes_refused_while_stopping(self):
        d = self.daemon()
        d.stopping = True
        self.assertFalse(d.handle({"cmd": "calibrate", "fan": "it8688/pwm2"}, 0)["ok"])
        self.assertTrue(d.handle({"cmd": "status"}, 0)["ok"])

    def test_calibration_aborts_without_cpu_sensor(self):
        d = self.daemon()
        d.handle({"cmd": "calibrate", "fan": "it8688/pwm2"}, 0)
        os.unlink(os.path.join(self.m.k10, "temp1_input"))
        self.ticks(d)
        self.assertEqual(d.calibration.state, "aborted")

    def test_missing_critical_sensor_warned(self):
        d = self.daemon({"critical": {"cpu_sensor": "k10temp/temp9"}})
        d.cfg["interval"] = 0.5
        d.stop_event.set()
        d.run()
        self.assertTrue(any("k10temp/temp9" in e["msg"] for e in d.events))

    def test_restore_skips_other_chip(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 50}}})
        self.ticks(d)
        with open(os.path.join(self.m.board, "name"), "w") as f:
            f.write("nct6798\n")   # hwmonN now belongs to a different chip
        errs = restore_from_file(self.restore_file)
        self.assertTrue(any("another chip" in e for e in errs))
        self.assertEqual(self.m.enable(2), 1)  # untouched

    def test_gpu_rejection_backs_off_then_gives_up(self):
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "fixed", "fixed": 50}}})
        orig = self.gpu.write
        tries = []

        def reject(path, v):
            if v.strip() == "c":
                tries.append(1)
                raise OSError(5, "Input/output error")
            return orig(path, v)
        self.gpu.write = reject
        self.ticks(d, 10)
        self.assertEqual(len(tries), 1)  # no retry storm
        self.ticks(d, 130, dt=1.0)
        self.assertEqual(len(tries), 3)
        self.assertIn("amdgpu/fan1", d.disabled)
        self.ticks(d, 120)
        self.assertEqual(len(tries), 3)
        self.gpu.write = orig
        d.set_config({"fans": {"amdgpu/fan1": {"mode": "fixed", "fixed": 55}}})
        self.ticks(d)
        self.assertNotIn("amdgpu/fan1", d.disabled)
        self.assertTrue(all(x == 55 for _, x in self.gpu.committed))

    def test_zero_rpm_staged_then_point0_fails_resets(self):
        gpu = self.gpu
        gpu.fail_point = 0
        info = amdgpu.read_info(self.m.fc)
        with self.assertRaises(amdgpu.GpuError):
            amdgpu.apply(self.m.fc, amdgpu.flat_points(info, 50), False)
        self.assertEqual(gpu.resets, 1)

    def test_status_is_snapshot(self):
        d = self.daemon()
        st = d.handle({"cmd": "status"}, 0)["status"]
        d.disabled["x"] = "y"
        self.assertNotIn("x", st["disabled"])
        json.dumps(st)


if __name__ == "__main__":
    unittest.main()
