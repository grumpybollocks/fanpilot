# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fanpilot import amdgpu  # noqa: E402
from fanpilot.client import Client, DaemonError  # noqa: E402
from fanpilot.daemon import Daemon, restore_from_file  # noqa: E402
from sim import FakeGpu, FakeMachine  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.m = FakeMachine()
        self.gpu = FakeGpu(self.m.fc)
        real = amdgpu._write
        self.p = mock.patch.object(amdgpu, "_write",
                                   lambda path, v: self.gpu.write(path, v) if path.startswith(self.m.fc) else real(path, v))
        self.p.start()
        self.cfg_path = os.path.join(self.m.root, "etc/fanpilot/config.json")
        self.run_dir = os.path.join(self.m.root, "run/fanpilot")
        self.now = 1000.0

    def tearDown(self):
        self.p.stop()
        self.m.cleanup()

    def daemon(self, cfg=None):
        if cfg is not None:
            os.makedirs(os.path.dirname(self.cfg_path), exist_ok=True)
            with open(self.cfg_path, "w") as f:
                json.dump(cfg, f)
        return Daemon(self.m.root, self.cfg_path, self.run_dir, os.path.join(self.run_dir, "s.sock"))

    def ticks(self, d, n=1, dt=1.0):
        for _ in range(n):
            self.now += dt
            d.tick(self.now, dt)

    @property
    def restore_file(self):
        return os.path.join(self.run_dir, "restore.json")


class SafetyTests(Base):
    def test_auto_touches_nothing(self):
        d = self.daemon()
        self.ticks(d, 3)
        self.assertEqual([self.m.enable(i) for i in (1, 2, 3)], [2, 2, 2])
        self.assertFalse(os.path.exists(self.restore_file))
        self.assertEqual(self.gpu.log, [])

    def test_fixed_then_restore(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 50}}})
        self.ticks(d)
        self.assertEqual(self.m.enable(2), 1)
        self.assertEqual(self.m.pwm(2), 128)
        self.assertEqual(self.m.enable(1), 2)  # others untouched
        with open(self.restore_file) as f:
            self.assertEqual([v["orig"] for v in json.load(f)["pwm_enable"].values()], [2])
        self.assertEqual(restore_from_file(self.restore_file), [])
        self.assertEqual(self.m.enable(2), 2)
        self.assertFalse(os.path.exists(self.restore_file))

    def test_crash_keeps_original_values(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 50}}})
        self.ticks(d)
        # "crash": no restore. A new daemon must keep the original value (2), not the current (1)
        d2 = self.daemon({})
        self.assertEqual([v["orig"] for v in d2.restore["pwm_enable"].values()], [2])
        self.ticks(d2)  # config now auto -> hand back
        self.assertEqual(self.m.enable(2), 2)

    def test_manual_original_restores_to_auto(self):
        self.m.set_enable(2, 1)
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 50}}})
        self.ticks(d)
        restore_from_file(self.restore_file)
        self.assertEqual(self.m.enable(2), 2)

    def test_curve_and_slow_ramp_down(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "curve", "curve": [[40, 20], [80, 100]],
                                                  "smoothing": 0, "hysteresis": 0, "ramp_down": 5}}})
        self.m.cpu(80)
        self.ticks(d)
        self.assertEqual(self.m.pwm(2), 255)
        self.m.cpu(40)
        self.ticks(d)
        self.assertEqual(self.m.pwm(2), round(95 * 2.55))
        self.ticks(d, 30)
        self.assertEqual(self.m.pwm(2), round(20 * 2.55))

    def test_sensor_failure_forces_full(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "curve", "curve": [[40, 20], [80, 100]],
                                                  "source": ["k10temp/temp1"]}}})
        self.m.cpu(40)
        self.ticks(d)
        self.assertLess(self.m.pwm(2), 100)
        os.unlink(os.path.join(self.m.k10, "temp1_input"))
        self.ticks(d, 2)
        self.assertLess(self.m.pwm(2), 100)
        self.ticks(d)
        self.assertEqual(self.m.pwm(2), 255)
        self.assertIn("sensor_failed", d.fan_status["it8688/pwm2"]["flags"])

    def test_max_of_sources(self):
        d = self.daemon({"fans": {"it8688/pwm3": {"mode": "curve", "curve": [[40, 20], [80, 100]],
                                                  "source": ["k10temp/temp1", "amdgpu/temp2"],
                                                  "smoothing": 0}}})
        self.m.cpu(40)
        self.m.set_temp(os.path.join(self.m.root, "sys/class/hwmon/hwmon2"), 2, 80)
        self.ticks(d)
        self.assertEqual(self.m.pwm(3), 255)

    def test_critical_overrides_auto_fans_then_releases(self):
        d = self.daemon({"critical": {"cpu_temp": 90}})
        self.m.cpu(91)
        self.ticks(d)
        self.assertEqual([self.m.pwm(i) for i in (1, 2, 3)], [255] * 3)
        self.assertEqual([self.m.enable(i) for i in (1, 2, 3)], [1] * 3)
        self.m.cpu(87)  # within the 5 °C release band
        self.ticks(d)
        self.assertTrue(d.critical)
        self.m.cpu(80)
        self.ticks(d)
        self.assertFalse(d.critical)
        self.assertEqual([self.m.enable(i) for i in (1, 2, 3)], [2] * 3)

    def test_cpu_fan_never_below_floor(self):
        d = self.daemon({"fans": {"it8688/pwm1": {"mode": "fixed", "fixed": 0,
                                                  "min_duty": 0, "allow_stop": True}}})
        self.ticks(d)
        self.assertEqual(self.m.pwm(1), round(20 * 2.55))

    def test_sys_fan_can_stop_when_allowed(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 0,
                                                  "min_duty": 0, "allow_stop": True}}})
        self.ticks(d)
        self.assertEqual(self.m.pwm(2), 0)

    def test_reclaim_gives_up(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 50}}})
        self.ticks(d)
        for _ in range(5):
            self.m.set_enable(2, 2)   # BIOS takes it back
            self.ticks(d)
        self.assertIn("it8688/pwm2", d.disabled)
        self.assertEqual(self.m.enable(2), 2)
        self.ticks(d, 3)
        self.assertEqual(self.m.enable(2), 2)  # we stopped fighting

    def test_bad_config_file_runs_auto(self):
        os.makedirs(os.path.dirname(self.cfg_path), exist_ok=True)
        with open(self.cfg_path, "w") as f:
            f.write('{"fans": {"it8688/pwm2": {"fixed": 999, "mode": "fixed"}}}')
        d = self.daemon()
        self.ticks(d)
        self.assertEqual(self.m.enable(2), 2)
        self.assertTrue(any(e["level"] == "error" for e in d.events))


class GpuDaemonTests(Base):
    def test_curve_applied_once_then_auto_resets(self):
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "curve"}}})
        self.ticks(d, 5)
        self.assertEqual(self.gpu.commits, 1)
        with open(self.restore_file) as f:
            self.assertEqual(json.load(f)["gpu_fan_ctrls"], [self.m.fc])
        d.set_config({"fans": {"amdgpu/fan1": {"mode": "auto"}}})
        self.ticks(d)
        self.assertIsNone(self.gpu.committed)
        self.assertEqual(self.gpu.resets, 1)

    def test_fixed_disables_zero_rpm(self):
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "fixed", "fixed": 45}}})
        self.ticks(d)
        self.assertTrue(all(dd == 45 for _, dd in self.gpu.committed))
        self.assertEqual(self.gpu.zero, 0)

    def test_external_change_reapplied(self):
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "curve"}}})
        self.ticks(d)
        self.gpu.write(os.path.join(self.m.fc, "fan_curve"), "r")  # e.g. GPU reset by driver
        self.ticks(d, 31)
        self.assertEqual(self.gpu.commits, 2)

    def test_restore_resets_gpu(self):
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "curve"}}})
        self.ticks(d)
        self.assertEqual(restore_from_file(self.restore_file), [])
        self.assertIsNone(self.gpu.committed)


class CalibrationTests(Base):
    def test_finds_stop_and_start(self):
        d = self.daemon()
        resp = d.handle({"cmd": "calibrate", "fan": "it8688/pwm2"}, 0)
        self.assertTrue(resp["ok"], resp)
        for _ in range(200):
            self.m.spin(2, stop_below=25)
            self.ticks(d)
            if d.calibration.state != "running":
                break
        self.assertEqual(d.calibration.state, "done")
        res = d.config_path and d.cfg["calibration"]["it8688/pwm2"]
        self.assertEqual(res["stop_duty"], 20)
        self.assertEqual(res["start_duty"], 25)
        self.assertEqual(res["min_safe_duty"], 30)
        self.ticks(d)
        self.assertEqual(self.m.enable(2), 2)  # mode auto: handed back afterwards

    def test_cpu_fan_calibration_stays_above_floor(self):
        d = self.daemon()
        d.handle({"cmd": "calibrate", "fan": "it8688/pwm1"}, 0)
        lowest = 255
        for _ in range(200):
            self.m.spin(1, stop_below=10)
            self.ticks(d)
            lowest = min(lowest, self.m.pwm(1))
            if d.calibration.state != "running":
                break
        self.assertGreaterEqual(lowest, round(20 * 2.55))

    def test_aborts_when_hot(self):
        d = self.daemon()
        d.handle({"cmd": "calibrate", "fan": "it8688/pwm2"}, 0)
        self.ticks(d, 8)
        self.m.cpu(85)
        self.ticks(d)
        self.assertEqual(d.calibration.state, "aborted")
        self.assertEqual(self.m.pwm(2), 255)


class IpcTests(Base):
    def test_socket_auth_and_full_run_restores(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 60}}})
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        sock = os.path.join(self.run_dir, "s.sock")
        for _ in range(100):
            if os.path.exists(sock) and self.m.enable(2) == 1:
                break
            time.sleep(0.05)
        c = Client(sock)
        st = c.call("status")
        self.assertFalse(st["can_write"])
        self.assertEqual(len(st["status"]["fans"]), 4)
        with self.assertRaises(DaemonError):
            c.call("set_fan", fan="it8688/pwm2", settings={"mode": "auto"})
        d.cfg["allowed_uids"] = [os.getuid()]
        c.call("set_fan", fan="it8688/pwm3", settings={"mode": "fixed", "fixed": 40})
        with self.assertRaises(DaemonError):
            c.call("set_fan", fan="it8688/pwm3", settings={"fixed": 400})
        with self.assertRaises(DaemonError):
            c.call("nonsense")
        for _ in range(100):
            if self.m.enable(3) == 1:
                break
            time.sleep(0.05)
        self.assertEqual(self.m.pwm(3), round(40 * 2.55))
        c.close()
        d.stop_event.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual([self.m.enable(i) for i in (1, 2, 3)], [2, 2, 2])
        self.assertFalse(os.path.exists(sock))


if __name__ == "__main__":
    unittest.main()
