# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI fixes from the third review: no freeze on a stuck service, no hidden zombie window,
one 'Unsaved changes' question, live About/limits, view-only profile switch, calibration Stop."""
import os
import socket
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
    from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
except ImportError:  # pragma: no cover - skipped below
    QApplication = QMessageBox = None

from test_daemon import Base  # noqa: E402
import test_gui  # noqa: E402  (module import: its tests are not collected twice)


@needs_qt
class GuiReview3(Base):
    setUpClass = classmethod(test_gui.GuiTests.setUpClass.__func__)
    start = test_gui.GuiTests.start

    def tearDown(self):
        if hasattr(self, "w"):
            self.w.quitting = True
            self.w.close()
            self.d.stop_event.set()
            self.t.join(5)
        super().tearDown()

    def pump(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            QApplication.processEvents()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_status_comes_from_the_worker_thread(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.status = {}
        w._tick()
        self.assertTrue(self.pump(lambda: w.status.get("fans")), "the poller delivered a status")

    def test_stuck_service_does_not_freeze_the_window(self):
        self.start({"allowed_uids": [os.getuid()]})
        stuck = os.path.join(self.m.root, "stuck.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(stuck)
        srv.listen(8)                 # accepts, never answers
        try:
            from fanpilot.gui import _Poller
            self.w._poller = _Poller(stuck)
            self.w._poller.done.connect(self.w._polled)
            t0 = time.monotonic()
            for _ in range(5):
                self.w._tick()
                QApplication.processEvents()
            self.assertLess(time.monotonic() - t0, 0.5, "ticks return at once while the service is stuck")
            self.assertTrue(self.w._polling, "no pile-up: later ticks wait for the first request")
            self.assertTrue(self.pump(lambda: not self.w._polling, timeout=4))
            self.assertTrue(self.w.banner.isVisible())
            self.assertIn("not reachable", self.w.banner.text())
        finally:
            self.w._poller.stop()
            srv.close()

    def test_closing_without_tray_quits_the_app(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.tray = None
        with mock.patch.object(QApplication, "quit") as quit_:
            w.close()
        quit_.assert_called_once()

    def test_rescan_does_not_ask_twice(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.fan_list.setCurrentRow(1)
        w.editor.rb_fixed.setChecked(True)
        w.editor.spin.setValue(61)
        self.assertTrue(w.editor.is_dirty)
        st = dict(w.status)
        st["fans"] = list(reversed(st["fans"]))      # a rescan changes the list
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel) as q:
            w._show_status({"status": st, "can_write": True}, False)
        self.assertEqual(q.call_count, 0, "same fan still there: nothing to ask")
        self.assertEqual(w.editor.fan_id, "it8688/pwm2")
        self.assertTrue(w.editor.is_dirty, "the edits are kept")

    def test_about_and_limits_follow_the_service(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.config = dict(w.config, critical=dict(w.config["critical"], cpu_temp=77))
        st = dict(w.status, uptime=600)
        w._show_status({"status": st, "can_write": True}, False)
        self.assertEqual(w.crit_cpu.value(), 77)
        self.assertIn("10 min", w.about.text())

    def test_view_only_profile_switch_says_so_first(self):
        self.start({"allowed_uids": []})
        w = self.w
        self.assertFalse(w.writable)
        with mock.patch.object(QMessageBox, "information") as info, \
                mock.patch.object(QMessageBox, "question") as q:
            w._apply_profile("Silent")
        info.assert_called_once()
        q.assert_not_called()

    def test_version_mismatch_banner_and_bad_status(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w._show_status({"status": dict(w.status, version="0.0.1"), "can_write": True}, False)
        self.assertIn("still", w.banner.text())
        w._show_status({"status": {"version": "9.9"}, "can_write": True}, False)   # missing keys
        self.assertIn("does not understand", w.banner.text())

    def test_calibration_stop_button(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.fan_list.setCurrentRow(1)
        self.assertTrue(self.d.handle({"cmd": "calibrate", "fan": "it8688/pwm2"}, os.getuid())["ok"])
        w.refresh()
        self.assertTrue(w.editor.cal_stop.isVisibleTo(w))
        w.editor.cal_stop.click()
        self.assertEqual(self.d.calibration.state, "aborted")

    def test_closed_windows_give_their_connections_back(self):
        self.start({"allowed_uids": [os.getuid()]})
        from fanpilot.gui import MainWindow
        for _ in range(12):                     # more windows than one user may hold connections
            win = MainWindow(self.sock)
            win.timer.stop()
            win._tick()
            self.assertTrue(self.pump(lambda: not win._polling))
            self.assertIn("fans", win.status)
            win.quitting = True
            win.close()
        self.assertTrue(self.pump(lambda: sum(self.d.conns.values()) <= 2), dict(self.d.conns))

    def test_late_poll_answer_does_not_undo_a_change(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        old = w.config
        w._poll_sent = time.monotonic()
        time.sleep(0.01)
        w.config = dict(old, critical=dict(old["critical"], cpu_temp=80))     # e.g. "Save limits"
        w._polling = True
        w._polled(("ok", {"status": w.status, "can_write": True}, old))       # answer from before
        self.assertEqual(w.config["critical"]["cpu_temp"], 80)
        self.assertEqual(w.crit_cpu.value(), 80)

    def test_updated_while_running(self):
        """The installed code changed under a running window: tray -> restart quietly as the new
        version; open window -> a button, never yanked away; opened again from the menu -> restart."""
        import fanpilot.gui as g
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        with mock.patch.object(g, "installed_version", return_value=g.__version__), \
                mock.patch.object(g.os, "execv") as ex:
            w._check_update()
            ex.assert_not_called()
        with mock.patch.object(g, "installed_version", return_value="99.0"), \
                mock.patch.object(g.os, "execv") as ex:
            w._check_update()                                  # window visible
            ex.assert_not_called()
            self.assertTrue(w.update_btn.isVisibleTo(w))
            w.hide()
            w._check_update()                                  # in the tray
            self.assertEqual(ex.call_count, 1)
            self.assertEqual(ex.call_args[0][1][-1], "--hidden")
            self.assertIn("-I", ex.call_args[0][1])
            w.show_or_update()                                 # opened from the menu
            self.assertNotIn("--hidden", ex.call_args[0][1])
            ex.reset_mock()
            w.sensors_view._log = ("f", "w", [])               # a CSV log is running
            w.hide()
            w._check_update()
            w.show_or_update()
            ex.assert_not_called()                             # never cut a log short
            self.assertTrue(w.update_btn.isVisibleTo(w))
            w.sensors_view._log = None
        self.assertIsNotNone(g.installed_version())

    def test_curve_points_kept_in_order(self):
        from fanpilot.gui import CurveEditor
        c = CurveEditor()
        c.set_points([[80, 90], [40, 30]])
        self.assertEqual(c.points, [[40.0, 30.0], [80.0, 90.0]])


if __name__ == "__main__":
    unittest.main()
