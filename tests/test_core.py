# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fanpilot import amdgpu, config as cfgmod  # noqa: E402
from fanpilot.curves import FanState, duty_to_pwm, interpolate  # noqa: E402
from fanpilot.hw import GPU_FAN_ID, discover  # noqa: E402
from sim import FakeGpu, FakeMachine, r  # noqa: E402
from unittest import mock  # noqa: E402


class CurveTests(unittest.TestCase):
    def test_interpolate(self):
        c = [[40, 30], [60, 50], [80, 100]]
        self.assertEqual(interpolate(c, 20), 30)
        self.assertEqual(interpolate(c, 50), 40)
        self.assertEqual(interpolate(c, 70), 75)
        self.assertEqual(interpolate(c, 99), 100)

    def test_hysteresis_deadband(self):
        s = FanState()
        self.assertEqual(s.step_temp(60, 1, 0, 3), 60)
        self.assertEqual(s.step_temp(58, 1, 0, 3), 60)   # inside band: hold
        self.assertEqual(s.step_temp(56, 1, 0, 3), 59)   # past band: follow + band
        self.assertEqual(s.step_temp(65, 1, 0, 3), 65)   # rises immediately

    def test_smoothing(self):
        s = FanState()
        s.step_temp(40, 1, 4, 0)
        self.assertAlmostEqual(s.step_temp(80, 1, 4, 0), 50)  # 1/4 of the way

    def test_ramp(self):
        s = FanState()
        self.assertEqual(s.step_duty(80, 1, 50, 4), 80)
        self.assertEqual(s.step_duty(30, 1, 50, 4), 76)
        self.assertEqual(s.step_duty(100, 1, 50, 4), 100)

    def test_pwm(self):
        self.assertEqual(duty_to_pwm(0), 0)
        self.assertEqual(duty_to_pwm(100), 255)
        self.assertEqual(duty_to_pwm(150), 255)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_auto(self):
        c = cfgmod.validate({})
        self.assertEqual(c["fans"], {})
        self.assertEqual(cfgmod.fan_defaults("it8688/pwm2")["mode"], "auto")

    def test_rejects_bad(self):
        bad = [
            {"fans": {"it8688/pwm2": {"mode": "turbo"}}},
            {"fans": {"it8688/pwm2": {"fixed": 150}}},
            {"fans": {"it8688/pwm2": {"curve": [[60, 50], [40, 60]]}}},
            {"fans": {"it8688/pwm2": {"curve": [[40, 60], [60, 50]]}}},
            {"fans": {"it8688/pwm2": {"curve": []}}},
            {"fans": {"it8688/pwm2": {"fixed": True}}},
            {"critical": {"cpu_temp": 120}},
            {"allowed_uids": ["root"]},
            {"interval": 0},
            {"version": 2},
        ]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(cfgmod.ConfigError):
                cfgmod.validate(b)

    def test_cpu_fan_floor_forced(self):
        # which fans are CPU fans comes from the hardware (board database, labels, your marks)
        hw_cpu = lambda fid, marks: fid in {"it8688/pwm1"} | set(marks)  # noqa: E731
        c = cfgmod.validate({"fans": {"it8688/pwm1": {"min_duty": 0, "allow_stop": True}}}, hw_cpu)
        self.assertEqual(c["fans"]["it8688/pwm1"]["min_duty"], cfgmod.CPU_FAN_FLOOR)
        self.assertFalse(c["fans"]["it8688/pwm1"]["allow_stop"])
        c = cfgmod.validate({"fans": {"it8688/pwm2": {"min_duty": 0, "allow_stop": True}}}, hw_cpu)
        self.assertTrue(c["fans"]["it8688/pwm2"]["allow_stop"])
        c = cfgmod.validate({"cpu_fans": ["it8688/pwm2"],
                             "fans": {"it8688/pwm2": {"min_duty": 0, "allow_stop": True}}}, hw_cpu)
        self.assertFalse(c["fans"]["it8688/pwm2"]["allow_stop"], "a fan you mark is a CPU fan")

    def test_save_load_roundtrip(self):
        m = FakeMachine()
        p = os.path.join(m.root, "etc/config.json")
        cfgmod.save(p, {"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 33}}})
        self.assertEqual(cfgmod.load(p)["fans"]["it8688/pwm2"]["fixed"], 33)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.save(p, {"fans": {"x": {"fixed": -1}}})
        self.assertEqual(cfgmod.load(p)["fans"]["it8688/pwm2"]["fixed"], 33)  # old file kept
        m.cleanup()


class DiscoveryTests(unittest.TestCase):
    def test_ids_and_labels(self):
        m = FakeMachine()
        hw = discover(m.root)
        self.assertEqual(hw.board_chip, "it8688")
        self.assertEqual(sorted(hw.fans), ["amdgpu/fan1", "it8688/pwm1", "it8688/pwm2", "it8688/pwm3"])
        self.assertEqual(hw.fans["it8688/pwm1"].label, "CPU fan (CPU_FAN)")
        self.assertEqual(hw.sensors["k10temp/temp1"].label, "CPU (Tctl)")
        self.assertEqual(hw.sensors["amdgpu/temp2"].label, "GPU hotspot")
        self.assertNotIn("it8688/temp4", hw.sensors)  # not connected on this board
        self.assertFalse(any(s.startswith("hidpp") for s in hw.sensors))
        self.assertEqual(hw.sensors["k10temp/temp1"].read(), 45.0)
        m.cleanup()

    def test_siv_suffix_and_it8689(self):
        for name, chip in (("it8688_60030507", "it8688"), ("it8689", "it8689")):
            m = FakeMachine(board_chip=name)
            hw = discover(m.root)
            self.assertEqual(hw.board_chip, chip)
            self.assertIn(f"{chip}/pwm2", hw.fans)
            m.cleanup()

    def test_no_driver(self):
        m = FakeMachine(with_board=False)
        hw = discover(m.root)
        self.assertIsNone(hw.board_chip)
        self.assertEqual(list(hw.fans), [GPU_FAN_ID])
        m.cleanup()


class GpuTests(unittest.TestCase):
    def setUp(self):
        self.m = FakeMachine()
        self.gpu = FakeGpu(self.m.fc)
        real = amdgpu._write
        self.p = mock.patch.object(amdgpu, "_write",
                                   lambda path, v: self.gpu.write(path, v) if path.startswith(self.m.fc) else real(path, v))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.m.cleanup()

    def test_read_info(self):
        info = amdgpu.read_info(self.m.fc)
        self.assertEqual(info.temp_range, (25, 100))
        self.assertEqual(info.duty_range, (20, 100))
        self.assertEqual(len(info.points), 5)
        self.assertTrue(info.zero_rpm_supported)
        self.assertTrue(info.zero_rpm)

    def test_build_points_resample_and_clamp(self):
        info = amdgpu.read_info(self.m.fc)
        pts = amdgpu.build_points(info, [[10, 0], [50, 40], [110, 100]])
        self.assertEqual(len(pts), 5)
        self.assertTrue(all(25 <= t <= 100 and 20 <= d <= 100 for t, d in pts))
        self.assertEqual(pts, sorted(pts))
        exact = [[40, 25], [60, 35], [75, 55], [85, 80], [95, 100]]
        self.assertEqual(amdgpu.build_points(info, exact), [tuple(p) for p in exact])
        padded = amdgpu.build_points(info, [[40, 30], [80, 70], [90, 100]])
        self.assertEqual(padded, [(40, 30), (60, 50), (70, 60), (80, 70), (90, 100)])
        flat = amdgpu.flat_points(info, 45)
        self.assertTrue(all(d == 45 for _, d in flat))

    def test_apply_single_commit_with_zero_rpm_staged_first(self):
        info = amdgpu.read_info(self.m.fc)
        pts = amdgpu.build_points(info, [[40, 25], [60, 35], [75, 55], [85, 80], [95, 100]])
        amdgpu.apply(self.m.fc, pts, False)
        names = [n for n, v in self.gpu.log]
        self.assertEqual(names[0], "fan_zero_rpm_enable")
        self.assertEqual([v for n, v in self.gpu.log].count("c"), 1)
        self.assertEqual(self.gpu.log[-1], ("fan_curve", "c"))
        self.assertEqual(self.gpu.committed, pts)
        self.assertEqual(self.gpu.zero, 0)
        self.assertEqual(amdgpu.read_info(self.m.fc).points, pts)  # readback

    def test_partial_failure_resets(self):
        self.gpu.fail_point = 3
        info = amdgpu.read_info(self.m.fc)
        with self.assertRaises(amdgpu.GpuError):
            amdgpu.apply(self.m.fc, amdgpu.flat_points(info, 50), None)
        self.assertEqual(self.gpu.resets, 1)
        self.assertEqual(self.gpu.commits, 0)

    def test_first_point_failure_does_not_reset(self):
        self.gpu.fail_point = 0
        info = amdgpu.read_info(self.m.fc)
        with self.assertRaises(amdgpu.GpuError):
            amdgpu.apply(self.m.fc, amdgpu.flat_points(info, 50), None)
        self.assertEqual(self.gpu.resets, 0)

    def test_wrong_point_count(self):
        with self.assertRaises(amdgpu.GpuError):
            amdgpu.apply(self.m.fc, [(40, 40)], None)

    def test_reset(self):
        info = amdgpu.read_info(self.m.fc)
        amdgpu.apply(self.m.fc, amdgpu.flat_points(info, 50), False)
        self.assertEqual(amdgpu.reset(self.m.fc), [])
        self.assertIsNone(self.gpu.committed)
        self.assertEqual(self.gpu.zero, 1)
        self.assertEqual(self.gpu.log[-2:], [("fan_curve", "r"), ("fan_zero_rpm_enable", "r")])


if __name__ == "__main__":
    unittest.main()
