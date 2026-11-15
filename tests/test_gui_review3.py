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

    def test_gpu_curve_never_below_card_minimum(self):
        """Real RX 9070 XT (2026-11-15): firmware accepts 30-100 %, but a point could be dragged to 6 % and the
        graph kept showing 6 % while the card ran 30 %."""
        from PySide6.QtCore import QPointF
        from fanpilot.gui import CurveEditor, FanEditor
        c = CurveEditor()
        c.resize(400, 260)
        c.set_points([[48, 6], [56, 42], [95, 100]], (25.0, 100.0), 30.0, 5, 30.0)
        self.assertEqual(c.points[0], [48.0, 30.0], "loaded below the card's minimum: shown as what it runs")
        c.drag = 0

        class Ev:
            def __init__(self, p):
                self._p = p

            def position(self):
                return self._p
        c.mouseMoveEvent(Ev(QPointF(c._to_px(48, 0).x(), c._plot().bottom())))   # drag to the floor
        self.assertEqual(c.points[0][1], 30.0, "can't be dragged under the card's minimum")
        # a motherboard curve keeps 0 % points (fan stop)
        c.set_points([[30, 0], [80, 100]], (20.0, 100.0), 20.0, 8, 0.0)
        self.assertEqual(c.points[0], [30.0, 0.0])
        # fixed speed: the slider's minimum is the card's, not a guess
        e = FanEditor()
        e.gpu_duty = (30, 100)
        e.load("amdgpu/fan1", {"mode": "fixed", "fixed": 20, "curve": [[48, 6], [95, 100]], "zero_rpm": False},
               True, "Graphics card fan", force=True, header=False)
        self.assertEqual(e.spin.minimum(), 30)
        self.assertEqual(e.curve.points[0], [48.0, 30.0])


    def test_find_my_cpu_fan_guide(self):
        """Second real machine (2026-11-15): the user read 'Identifying' next to the fan the app was told to
        pulse and took it as proof. The guide pulses each spinning fan and asks about the cooler instead."""
        from fanpilot.hw import discover
        hw = discover(self.m.root)
        it = next(d for c, d in hw.chips.items() if hw.names[c].startswith("it8"))
        for i, rpm in ((1, 900), (2, 1100), (3, 0)):                         # header 3: nothing to watch
            with open(os.path.join(it, f"fan{i}_input"), "w") as f:
                f.write(f"{rpm}\n")
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.refresh()
        dlg = w.find_cpu_fan()
        spinning = [f["id"] for f in w.status["fans"] if f.get("kind") == "board" and f.get("rpm")]
        self.assertEqual([c[0] for c in dlg.candidates], spinning, "only fans that spin are shown")
        self.assertEqual(len(spinning), 2)
        self.pump(lambda: dlg.i == 0 and self.d.identify, timeout=5)        # starts by itself
        self.assertIn(spinning[0], self.d.identify, "the service pulses the first fan")
        self.assertFalse(dlg.yes_btn.isEnabled(), "no answer before the pulse has been seen")
        dlg.t0 -= dlg.PULSE                                                  # the 6 s are over
        dlg._tick()
        self.assertTrue(dlg.yes_btn.isEnabled())
        self.pump(lambda: not self.d.identify, timeout=10)                   # service finished that pulse
        dlg.next_fan()                                                       # "No": the second fan
        self.pump(lambda: spinning[1] in self.d.identify, timeout=5)
        dlg.answer_yes()
        self.assertEqual(self.d.cfg["cpu_fans"], [spinning[1]])
        self.assertIn(spinning[1], dlg.marked)
        dlg.reject()
        # most likely first: fans the BIOS drives from the CPU temperature; chipset fans and pumps last
        from fanpilot.gui import CpuFanFinder
        w.status = dict(w.status, fans=[
            {"id": "n/pwm1", "label": "Fan 1", "kind": "board", "rpm": 700},
            {"id": "n/pwm5", "label": "Fan 5", "kind": "board", "rpm": 1500, "bios_guess": "chipset"},
            {"id": "n/pwm2", "label": "Fan 2", "kind": "board", "rpm": 720, "bios_guess": "cpu"},
            {"id": "n/pwm3", "label": "Fan 3", "kind": "board", "rpm": 800, "bios_guess": "cpu"}])
        d2 = CpuFanFinder(w)
        self.assertEqual([c[0] for c in d2.candidates], ["n/pwm2", "n/pwm3", "n/pwm1", "n/pwm5"])
        d2.reject()
        self.d.identify.clear()
        self.pump(lambda: False, timeout=1.8)                                # its delayed start passes
        self.assertEqual(d2.i, -1, "closed before it started: no fan tried, nothing pulsed")
        # the banner links to it while the CPU fan is unknown
        w.status["cpu_fan_known"] = False
        w._update_banner()
        self.assertIn("fanpilot:find-cpu", w.banner.text())


if __name__ == "__main__":
    unittest.main()
