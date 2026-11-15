# SPDX-License-Identifier: GPL-3.0-or-later
"""In-service self-test on the simulated machine (fans follow PWM, it87 full-speed quirk, GPU firmware)."""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qt import needs_qt  # noqa: E402

from sim import w  # noqa: E402
from test_daemon import Base  # noqa: E402

USER = 1000
UNIT = "[Service]\nExecStart=/usr/local/bin/fanpilotd\nExecStopPost=/usr/local/bin/fanpilotd --restore\nWatchdogSec=15\n"


class SelfTestTests(Base):
    def physics(self, empty_header=None):
        for i in (1, 2, 3):
            if i == empty_header:
                w(f"{self.m.board}/fan{i}_input", "0\n")
            elif self.m.enable(i) == 2:
                w(f"{self.m.board}/fan{i}_input", "1200\n")     # BIOS runs them near full (reference machine)
            else:
                self.m.spin(i, stop_below=15)
        pts = self.gpu.committed
        w(os.path.join(self.m.root, "sys/class/hwmon/hwmon2/fan1_input"), f"{(pts[0][1] * 45) if pts else 0}\n")

    def run_test(self, d, empty_header=None, max_ticks=150, hot_at=None):
        self.physics(empty_header)   # fans are already spinning when the user presses the button
        self.assertTrue(d.handle({"cmd": "selftest"}, USER)["ok"])
        for n in range(max_ticks):
            if hot_at is not None and n == hot_at:
                self.m.cpu(85)
            self.now += 1
            d.tick(self.now, 1)
            self.m.it87_report()
            self.physics(empty_header)
            if d.selftest.state != "running":
                break
        return d.selftest

    def install_unit(self):
        w(os.path.join(self.m.root, "etc/systemd/system/fanpilotd.service"), UNIT)

    def test_all_pass_and_everything_restored(self):
        self.install_unit()
        d = self.daemon({"allowed_uids": [USER]})
        t = self.run_test(d)
        self.assertEqual(t.state, "done", t.results)
        bad = [r for r in t.results if r["ok"] is not True]
        self.assertEqual(bad, [], bad)
        self.assertGreaterEqual(t.passed, 12)
        self.ticks(d, 2)
        self.assertEqual([self.m.enable(i) for i in (1, 2, 3)], [2, 2, 2])   # config is Automatic
        self.assertIsNone(self.gpu.committed)
        self.assertFalse(d.restore.get("gpu_fan_ctrls"))
        self.assertTrue(any("Self-test finished: " in e["msg"] for e in d.events))
        self.assertFalse(any("taken back" in e["msg"] for e in d.events))   # it87 quirk handled
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "selftest-restore.json")))

    def test_cpu_fan_is_never_written(self):
        d = self.daemon({"allowed_uids": [USER]})
        writes = []
        fan = d.hw.fans["it8688/pwm1"]
        orig_pwm, orig_en = fan.set_pwm, fan.set_enable
        fan.set_pwm = lambda v: (writes.append(("pwm", v)), orig_pwm(v))
        fan.set_enable = lambda v: (writes.append(("en", v)), orig_en(v))
        self.run_test(d)
        self.assertEqual(writes, [])

    def test_configured_modes_come_back(self):
        d = self.daemon({"allowed_uids": [USER], "fans": {
            "it8688/pwm2": {"mode": "fixed", "fixed": 45},
            "amdgpu/fan1": {"mode": "curve"}}})
        self.ticks(d, 2)
        commits_before = self.gpu.commits
        t = self.run_test(d)
        self.assertEqual(t.state, "done")
        self.ticks(d, 3)
        self.assertEqual((self.m.enable(2), self.m.pwm(2)), (1, round(45 * 2.55)))
        self.assertEqual(self.gpu.committed, [(40, 25), (60, 35), (75, 55), (85, 80), (95, 100)])
        self.assertGreater(self.gpu.commits, commits_before)

    def test_empty_header_is_skipped_not_failed(self):
        d = self.daemon({"allowed_uids": [USER]})
        t = self.run_test(d, empty_header=3)
        r = [x for x in t.results if "SYS_FAN2" in x["name"] and "speed" in x["name"]][0]
        self.assertIsNone(r["ok"])
        self.assertEqual(t.failed, 0, t.results)

    def test_aborts_when_hot_and_hands_back(self):
        d = self.daemon({"allowed_uids": [USER]})
        t = self.run_test(d, hot_at=5)
        self.assertEqual(t.state, "aborted")
        self.assertTrue(any(r["name"] == "Stopped early" for r in t.results))
        self.assertTrue(d.critical is False or self.m.cpu)  # 85 °C is below the 90 °C critical limit
        self.m.cpu(50)
        self.ticks(d, 2)
        self.assertEqual([self.m.enable(i) for i in (2, 3)], [2, 2])

    def test_cancel(self):
        d = self.daemon({"allowed_uids": [USER]})
        d.handle({"cmd": "selftest"}, USER)
        self.ticks(d, 3)
        self.assertTrue(d.handle({"cmd": "selftest_cancel"}, USER)["ok"])
        self.assertEqual(d.selftest.state, "aborted")
        self.ticks(d, 2)
        self.assertEqual(self.m.enable(2), 2)

    def test_exclusive_with_calibration_and_identify(self):
        d = self.daemon({"allowed_uids": [USER]})
        d.handle({"cmd": "selftest"}, USER)
        self.assertFalse(d.handle({"cmd": "selftest"}, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "calibrate", "fan": "it8688/pwm2"}, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "identify", "fan": "it8688/pwm3"}, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "selftest"}, 4321)["ok"])  # not allowed user

    def test_no_board_chip_reports_failure_not_crash(self):
        from sim import FakeMachine
        self.m.cleanup()
        self.m = FakeMachine(with_board=False)
        from fanpilot import amdgpu
        from sim import FakeGpu
        self.gpu = FakeGpu(self.m.fc)
        self.p.stop()
        from unittest import mock
        real = amdgpu._write
        self.p = mock.patch.object(amdgpu, "_write",
                                   lambda path, v: self.gpu.write(path, v) if path.startswith(self.m.fc) else real(path, v))
        self.p.start()
        self.cfg_path = os.path.join(self.m.root, "etc/fanpilot/config.json")
        self.run_dir = os.path.join(self.m.root, "run/fanpilot")
        d = self.daemon({"allowed_uids": [USER]})
        t = d.handle({"cmd": "selftest"}, USER)
        self.assertTrue(t["ok"])
        for _ in range(60):
            self.now += 1
            d.tick(self.now, 1)
            pts = self.gpu.committed
            w(os.path.join(self.m.root, "sys/class/hwmon/hwmon2/fan1_input"), f"{(pts[0][1] * 45) if pts else 0}\n")
            if d.selftest.state != "running":
                break
        self.assertEqual(d.selftest.state, "done")
        chip = [r for r in d.selftest.results if r["name"] == "Motherboard fan chip"][0]
        self.assertFalse(chip["ok"])


@needs_qt
class SelfTestGui(Base):
    def test_results_rendered(self):
        import threading
        import time
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        d = self.daemon({"allowed_uids": [os.getuid()]})
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        sock = os.path.join(self.run_dir, "s.sock")
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        from fanpilot.gui import MainWindow
        win = MainWindow(sock)
        win.timer.stop()
        self.assertTrue(win.test_btn.isEnabled())
        self.assertIn("Not run yet", win.test_step.text())
        with d.lock:
            from fanpilot.selftest import SelfTest
            st = SelfTest(d)
            st._add("Motherboard fan chip", True, "it8688")
            st._add("SYS_FAN1 speed", False, "speed did not drop")
            st._add("SYS_FAN2 speed", None, "no fan connected")
            st.finish("done")
            d.selftest = st
        win.refresh()
        texts = [win.test_results.item(i).text() for i in range(win.test_results.count())]
        self.assertEqual(len(texts), 3)
        self.assertTrue(texts[0].startswith("✔") and texts[1].startswith("✖") and texts[2].startswith("–"))
        self.assertIn("1 check(s) FAILED", win.test_step.text())
        with d.lock:
            d.selftest.state = "running"
        win.refresh()
        self.assertFalse(win.test_btn.isEnabled())
        self.assertFalse(win.test_cancel.isHidden())
        win.quitting = True
        win.close()
        d.stop_event.set()
        t.join(5)


if __name__ == "__main__":
    unittest.main()
