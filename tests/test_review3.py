# SPDX-License-Identifier: GPL-3.0-or-later
"""Third review (whole core, 2026-11-14): driver reloads, unreadable emergency sensor,
late sensors, socket lock-out, restart from a stop, GPU repair limit, calibration."""
import os
import shutil
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fanpilot import daemon as dmod  # noqa: E402
from fanpilot.daemon import Calibration  # noqa: E402
from fanpilot.hw import Sensor  # noqa: E402
from sim import w  # noqa: E402
from test_daemon import Base  # noqa: E402


class CoreReview3(Base):
    def test_driver_reload_rebinds_fans(self):
        """hwmon7 -> hwmon8 (modprobe -r it87; modprobe it87): control must follow the chip."""
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 40}}})
        self.ticks(d, 2)
        self.assertEqual(self.m.enable(2), 1)
        new = self.m.board.replace("hwmon7", "hwmon8")
        os.rename(self.m.board, new)
        self.m.board = new
        self.m.set_enable(2, 2)                      # a reloaded chip starts in BIOS mode
        self.m.cpu(95)                               # and it gets hot meanwhile
        self.ticks(d, 3)
        self.assertTrue(d.critical)
        self.assertEqual((self.m.enable(2), self.m.pwm(2)), (1, 255), "critical must reach the new chip")
        self.assertNotIn("it8688/pwm2", d.disabled)
        self.assertTrue(all(os.path.exists(p) for p in d.restore["pwm_enable"]), "no dead paths kept")
        self.assertIn(os.path.join(new, "pwm2_enable"), d.restore["pwm_enable"])

    def test_unreadable_cpu_sensor_keeps_critical(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 20}}})
        self.m.cpu(96)
        self.ticks(d, 1)
        self.assertTrue(d.critical)
        os.remove(os.path.join(self.m.k10, "temp1_input"))  # sensor fails while critical
        self.ticks(d, 5)
        self.assertTrue(d.critical, "an unreadable sensor is not a cool one")
        self.assertEqual(self.m.pwm(2), 255)

    def test_cpu_sensor_unreadable_enters_critical(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 30}}})
        self.ticks(d, 2)
        self.assertFalse(d.critical)
        os.remove(os.path.join(self.m.k10, "temp1_input"))
        self.ticks(d, dmod.FAIL_TICKS)
        self.assertTrue(d.critical)
        self.assertTrue(any("unreadable" in e["msg"] for e in list(d.events)[-3:]), list(d.events)[-3:])   # (the GPU fan's switch to 100 % may follow)
        self.m.cpu(50)                                       # reads again, and cool
        self.ticks(d, 1)
        self.assertFalse(d.critical)

    def test_late_cpu_driver_is_picked_up(self):
        saved = os.path.join(self.m.root, "k10-saved")
        shutil.move(self.m.k10, saved)                      # k10temp not loaded yet at start
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 30}}})
        self.ticks(d, 2)
        self.assertNotIn("k10temp/temp1", d.hw.sensors)
        shutil.move(saved, self.m.k10)                      # udev loads it a moment later
        d.next_rescan = 0.0
        self.m.cpu(99)
        self.ticks(d, 1)
        self.assertIn("k10temp/temp1", d.hw.sensors)
        self.assertTrue(d.critical, "emergency protection must work once the sensor appears")

    def test_no_rescan_when_nothing_is_missing(self):
        d = self.daemon()
        self.ticks(d, 1)
        before = d.readings_src
        d.next_rescan = 0.0
        self.ticks(d, 1)
        self.assertIs(d.readings_src, before, "pci.ids etc. are not re-read every 10 s for nothing")

    def test_rescan_after_cpu_driver_vanished_keeps_critical(self):
        """The reviewer's case: k10temp goes away, a rescan drops it -> must NOT read as 'cool'."""
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 20}}})
        self.m.cpu(97)
        self.ticks(d, 1)
        self.assertTrue(d.critical)
        shutil.rmtree(self.m.k10)
        self.ticks(d, 3)
        d.next_rescan = 0.0
        self.ticks(d, 2)                                     # sensor path gone -> rescan drops it
        self.assertNotIn("k10temp/temp1", d.hw.sensors)
        self.assertTrue(d.critical, "a vanished CPU sensor is unreadable, not cool")
        self.assertEqual(self.m.pwm(2), 255)

    def test_cpu_driver_vanishing_while_cool_starts_critical(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 20}}})
        self.ticks(d, 2)
        shutil.rmtree(self.m.k10)
        d.next_rescan = 0.0
        self.ticks(d, dmod.FAIL_TICKS + 1)
        self.assertTrue(d.critical)

    def test_fan_gone_and_back_at_same_path_is_retaken(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 40}}})
        self.ticks(d, 2)
        away = os.path.join(self.m.root, "hwmon7-away")       # outside sys/class/hwmon
        os.rename(self.m.board, away)
        d.next_rescan = 0.0
        self.ticks(d, 2)                                     # rescanned while the driver is unloaded
        self.assertNotIn("it8688/pwm2", d.hw.fans)
        os.rename(away, self.m.board)
        self.m.set_enable(2, 2)
        d.next_rescan = 0.0
        self.ticks(d, 2)
        self.assertEqual(self.m.enable(2), 1)
        self.assertEqual(len(d.reclaims["it8688/pwm2"]), 0, "coming back is not a 'reclaim'")

    def test_identify_respects_measured_minimum(self):
        d = self.daemon({"calibration": {"it8688/pwm2": {"min_safe_duty": 100, "stop_duty": 60}}})
        d.cfg["allowed_uids"] = [os.getuid()]
        self.ticks(d, 1)
        self.assertTrue(d.handle({"cmd": "identify", "fan": "it8688/pwm2"}, os.getuid())["ok"])
        self.ticks(d, 1)
        self.assertEqual(self.m.pwm(2), 255, "a fan that never restarts is not slowed for Identify")

    def test_restart_from_stop_starts_at_safe_speed(self):
        d = self.daemon({"fans": {"it8688/pwm2": {
            "mode": "curve", "curve": [[40, 0], [60, 100]], "allow_stop": True, "ramp_up": 2,
            "smoothing": 0, "hysteresis": 0, "source": ["k10temp/temp1"]}},
            "calibration": {"it8688/pwm2": {"min_safe_duty": 35}}})
        self.m.cpu(30)
        self.ticks(d, 3)
        self.assertEqual(self.m.pwm(2), 0)
        self.m.cpu(55)
        self.ticks(d, 1)
        self.assertGreaterEqual(self.m.pwm(2) * 100 / 255, 35 - 0.5, "not 2 %, 4 %, 6 %...")

    def test_other_fan_tool_is_named(self):
        for pid, comm in (("1241", "lact"), ("77", "bash")):
            w(os.path.join(self.m.root, "proc", pid, "comm"), comm + "\n")
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "curve", "curve": [[40, 30], [80, 80]]}}})
        self.ticks(d, 1)
        self.gpu.write(os.path.join(self.m.fc, "fan_curve"), "r")
        self.ticks(d, 1, dt=dmod.GPU_VERIFY_EVERY + 1)
        msgs = [e["msg"] for e in d.events]
        self.assertTrue(any("also running: LACT" in m for m in msgs), msgs)
        self.assertEqual(dmod.other_fan_tools("/nonexistent"), [])

    def test_gpu_repairs_limited_per_hour_not_forever(self):
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "curve", "curve": [[40, 30], [80, 80]]}}})
        self.ticks(d, 1)
        self.assertIsNotNone(self.gpu.committed)
        for _ in range(dmod.GPU_REPAIR_LIMIT):
            self.gpu.write(os.path.join(self.m.fc, "fan_curve"), "r")       # someone resets the card
            self.ticks(d, 1, dt=dmod.GPU_VERIFY_EVERY + 1)
            self.assertIsNotNone(self.gpu.committed)
        self.gpu.write(os.path.join(self.m.fc, "fan_curve"), "r")
        self.ticks(d, 1, dt=dmod.GPU_VERIFY_EVERY + 1)
        self.assertIsNone(self.gpu.committed, "4th change within the hour is left alone")
        self.assertIn("leaving it alone", d.events[-1]["msg"])
        self.ticks(d, 1, dt=dmod.GPU_REPAIR_WINDOW)
        self.ticks(d, 1, dt=dmod.GPU_VERIFY_EVERY + 1)
        self.assertIsNotNone(self.gpu.committed, "repaired again an hour later")

    def test_calibration_without_restart_is_not_safe_low(self):
        class Fan:
            id = "it8688/pwm2"
            p, stopped = 255, False

            def set_pwm(self, v):
                self.p = v

            def rpm(self):
                self.stopped = self.stopped or self.p < 200   # stops below ~78 % and never restarts
                return 0 if self.stopped else 900

        cal = Calibration(Fan(), False)
        now = 0.0
        while cal.state == "running":
            now += 10
            cal.tick(now)
        self.assertIsNotNone(cal.result["stop_duty"])
        self.assertIsNone(cal.result["start_duty"])
        self.assertEqual(cal.result["min_safe_duty"], 100)

    def test_calibration_starts_at_full_speed(self):
        d = self.daemon()
        self.ticks(d, 1)
        w(f"{self.m.board}/pwm2", "40\n")                   # stale BIOS-mode register value
        d.cfg["allowed_uids"] = [os.getuid()]
        self.assertTrue(d.handle({"cmd": "calibrate", "fan": "it8688/pwm2"}, os.getuid())["ok"])
        self.assertEqual(self.m.pwm(2), 255)

    def test_gpu_points_never_share_a_temperature(self):
        from fanpilot.amdgpu import CurveInfo, build_points
        info = CurveInfo([(0, 0)] * 5, (25, 100), (20, 100), True, True)
        live = [[45, 7], [60, 35], [69, 61], [81, 84], [95, 100]]          # the real card's curve
        self.assertEqual(build_points(info, live), [(45, 20), (60, 35), (69, 61), (81, 84), (95, 100)])
        for curve in ([[0, 30], [10, 40], [20, 50]], [[90, 50], [110, 80], [130, 100]],
                      [[30, 30], [30.4, 35], [31, 40], [80, 90], [100, 100]]):
            ts = [t for t, _ in build_points(info, curve)]
            self.assertTrue(all(a < b for a, b in zip(ts, ts[1:])) and 25 <= ts[0] and ts[-1] <= 100, ts)

    def test_impossible_temperature_is_unreadable(self):
        p = os.path.join(self.m.root, "t")
        for raw, want in (("-128000", None), ("127500", 127.5), ("200000", None), ("45000", 45.0)):
            w(p, raw + "\n")
            self.assertEqual(Sensor("x", "x", p).read(), want, raw)

    def test_one_user_cannot_lock_everyone_out(self):
        d = self.daemon()
        srv = d.serve()
        try:
            hogs = []
            for _ in range(dmod.PER_UID_CLIENTS + 4):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(d.socket_path)
                hogs.append(s)
            time.sleep(0.3)
            with d.conn_lock:
                self.assertLessEqual(sum(d.conns.values()), dmod.PER_UID_CLIENTS)
            for s in hogs:
                s.close()
            time.sleep(0.3)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(3)
            c.connect(d.socket_path)
            c.sendall(b'{"cmd": "hello"}\n')
            self.assertIn(b'"ok": true', c.recv(4096))
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_idle_connection_is_closed(self):
        old = dmod.IDLE_TIMEOUT
        dmod.IDLE_TIMEOUT = 0.3
        d = self.daemon()
        srv = d.serve()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(d.socket_path)
            self.assertEqual(s.recv(10), b"", "server hung up on the idle client")
            s.close()
            time.sleep(0.1)
            with d.conn_lock:
                self.assertEqual(sum(d.conns.values()), 0)
        finally:
            dmod.IDLE_TIMEOUT = old
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
