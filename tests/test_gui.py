# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI tests: real MainWindow (offscreen) against an in-process simulated daemon."""
import os
import sys
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qt import needs_qt  # noqa: E402

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover
    QApplication = None

from test_daemon import Base  # noqa: E402


@needs_qt
@unittest.skipIf(QApplication is None, "PySide6 not installed")
class GuiTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def start(self, cfg):
        self.d = self.daemon(cfg)
        self.t = threading.Thread(target=self.d.run, daemon=True)
        self.t.start()
        self.sock = os.path.join(self.run_dir, "s.sock")
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)
        from fanpilot.gui import MainWindow
        self.w = MainWindow(self.sock)
        self.w.timer.stop()
        self.w.show()
        QTest.qWaitForWindowExposed(self.w)

    def tearDown(self):
        self.w.quitting = True
        self.w.close()
        self.d.stop_event.set()
        self.t.join(5)
        super().tearDown()

    def wait_for(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_fixed_speed_apply(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        ids = [w.fan_list.item(i).data(Qt.UserRole) for i in range(w.fan_list.count())]
        self.assertEqual(ids, ["it8688/pwm1", "it8688/pwm2", "it8688/pwm3", "amdgpu/fan1"])
        w.fan_list.setCurrentRow(1)
        ed = w.editor
        self.assertEqual(ed.fan_id, "it8688/pwm2")
        self.assertFalse(ed.apply_btn.isEnabled())
        ed.rb_fixed.setChecked(True)
        ed.spin.setValue(55)
        self.assertTrue(ed.apply_btn.isEnabled())
        QTest.mouseClick(ed.apply_btn, Qt.LeftButton)
        self.assertEqual(self.d.cfg["fans"]["it8688/pwm2"]["mode"], "fixed")
        self.assertTrue(self.wait_for(lambda: self.m.pwm(2) == round(55 * 2.55)))
        self.assertFalse(ed.apply_btn.isEnabled())

    def test_cpu_fan_floor_in_ui(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.fan_list.setCurrentRow(0)
        self.assertEqual(w.editor.spin.minimum(), 20)
        self.assertFalse(w.editor.allow_stop.isEnabled())

    def test_sys_fan_minimum_follows_allow_stop(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.fan_list.setCurrentRow(1)
        ed = w.editor
        self.assertEqual(ed.spin.minimum(), 10)
        ed.allow_stop.setChecked(True)
        self.assertEqual(ed.spin.minimum(), 0)
        self.assertEqual(ed.min_duty.minimum(), 0)

    def test_failed_apply_keeps_edits(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.fan_list.setCurrentRow(1)
        ed = w.editor
        ed.rb_fixed.setChecked(True)
        from fanpilot.client import DaemonError
        with mock.patch.object(w.client, "call", side_effect=DaemonError("nope")), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Apply), \
                mock.patch.object(QMessageBox, "warning"):
            w.fan_list.setCurrentRow(2)
        self.assertEqual(ed.fan_id, "it8688/pwm2")
        self.assertTrue(ed.is_dirty)

    def test_drag_curve_point(self):
        self.start({"allowed_uids": [os.getuid()],
                    "fans": {"it8688/pwm3": {"mode": "curve", "curve": [[40, 30], [60, 45], [80, 100]]}}})
        w = self.w
        w.fan_list.setCurrentRow(2)
        ed = w.editor
        cv = ed.curve
        cv.resize(600, 300)
        p = cv._to_px(60, 45).toPoint()
        target = cv._to_px(60, 70).toPoint()
        QTest.mousePress(cv, Qt.LeftButton, Qt.NoModifier, p)
        QTest.mouseMove(cv, target)
        QTest.mouseRelease(cv, Qt.LeftButton, Qt.NoModifier, target)
        pts = sorted(cv.points)
        self.assertAlmostEqual(pts[1][1], 70, delta=2)
        self.assertEqual(pts[1][0], 60)
        ed.apply_btn.click()
        saved = self.d.cfg["fans"]["it8688/pwm3"]["curve"]
        self.assertAlmostEqual(saved[1][1], 70, delta=2)

    def test_drag_cannot_cross_neighbours(self):
        self.start({"allowed_uids": [os.getuid()],
                    "fans": {"it8688/pwm3": {"mode": "curve", "curve": [[40, 30], [60, 45], [80, 100]]}}})
        w = self.w
        w.fan_list.setCurrentRow(2)
        cv = w.editor.curve
        cv.resize(600, 300)
        p = cv._to_px(60, 45).toPoint()
        QTest.mousePress(cv, Qt.LeftButton, Qt.NoModifier, p)
        QTest.mouseMove(cv, cv._to_px(95, 5).toPoint())
        QTest.mouseRelease(cv, Qt.LeftButton, Qt.NoModifier, cv._to_px(95, 5).toPoint())
        t, d = sorted(cv.points)[1]
        self.assertEqual(t, 79)    # stops 1 °C before the next point
        self.assertEqual(d, 30)    # never below the previous point's speed

    def test_read_only_user(self):
        self.start({})
        w = self.w
        self.assertFalse(w.writable)
        self.assertTrue(w.banner.isVisible())
        self.assertIn("View only", w.banner.text())
        w.fan_list.setCurrentRow(1)
        w.editor.rb_fixed.setChecked(True)
        self.assertFalse(w.editor.apply_btn.isEnabled())

    def test_gpu_editor(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.fan_list.setCurrentRow(3)
        ed = w.editor
        self.assertTrue(ed.is_gpu)
        self.assertEqual(ed.curve.max_points, 5)
        self.assertFalse(ed.cal_box.isVisibleTo(w))
        ed.rb_curve.setChecked(True)
        ed.apply_btn.click()
        self.assertTrue(self.wait_for(lambda: self.gpu.commits == 1))
        self.assertEqual(self.gpu.committed, [(40, 25), (60, 35), (75, 55), (85, 80), (95, 100)])


if __name__ == "__main__":
    unittest.main()
