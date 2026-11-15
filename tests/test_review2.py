# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the second independent review (2026-11-14)."""
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qt import needs_qt  # noqa: E402

from fanpilot.client import Client, DaemonError  # noqa: E402
from test_daemon import Base  # noqa: E402

USER = 1000


class DaemonFixes(Base):
    def test_builtin_profile_keeps_fine_tuning(self):
        d = self.daemon({"allowed_uids": [USER], "fans": {"it8688/pwm2": {
            "mode": "fixed", "allow_stop": True, "min_duty": 0, "ramp_down": 10}}})
        d.handle({"cmd": "apply_profile", "name": "Silent"}, USER)
        f = d.cfg["fans"]["it8688/pwm2"]
        self.assertEqual((f["mode"], f["allow_stop"], f["min_duty"], f["ramp_down"]), ("curve", True, 0, 10))

    def test_calibrated_minimum_is_enforced(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 22}},
                         "calibration": {"it8688/pwm2": {"min_safe_duty": 35, "stop_duty": 20}}})
        self.ticks(d)
        self.assertEqual(self.m.pwm(2), round(35 * 2.55))

    def test_bogus_critical_sensor_ignored(self):
        d = self.daemon({"allowed_uids": [USER]})
        cfg = {**d.cfg, "critical": {**d.cfg["critical"], "cpu_sensor": "nope/temp1", "gpu_sensor": "nope/temp2"}}
        d.handle({"cmd": "set_config", "config": cfg}, USER)
        self.assertEqual(d.cfg["critical"]["cpu_sensor"], "auto")
        self.assertEqual(d.crit_sensor("cpu"), "k10temp/temp1")
        self.m.cpu(99)
        self.ticks(d)
        self.assertTrue(d.critical)
        self.assertTrue(any("Ignored emergency sensor" in e["msg"] for e in d.events))

    def _stall(self, d, i, n_spin, n_stop):
        for rpm, n in ((900, n_spin), (0, n_stop)):
            for _ in range(n):
                with open(os.path.join(self.m.board, f"fan{i}_input"), "w") as f:
                    f.write(f"{rpm}\n")
                self.ticks(d)

    def test_stall_at_25_percent(self):
        d = self.daemon({"fans": {"it8688/pwm1": {"mode": "fixed", "fixed": 25}}})
        self._stall(d, 1, 2, 12)
        self.assertIn("stalled", d.fan_status["it8688/pwm1"]["flags"])

    def test_cpu_fan_stall_under_bios(self):
        d = self.daemon()
        self._stall(d, 1, 2, 12)
        self.assertTrue(any("CPU FAN STOPPED" in e["msg"] and "BIOS" in e["msg"] for e in d.events))

    def test_sys_fan_stopped_by_bios_is_not_a_stall(self):
        d = self.daemon()
        self._stall(d, 2, 2, 20)
        self.assertFalse(any("stopped" in e["msg"] for e in d.events))

    def test_identify_refused_for_given_up_fan(self):
        d = self.daemon({"allowed_uids": [USER]})
        d.disabled["it8688/pwm2"] = "firmware keeps taking control"
        r = d.handle({"cmd": "identify", "fan": "it8688/pwm2"}, USER)
        self.assertFalse(r["ok"])

    def test_root_can_clear_calibration(self):
        d = self.daemon({"calibration": {"it8688/pwm2": {"min_safe_duty": 30}}})
        cfg = {**d.cfg, "calibration": {}}
        d.handle({"cmd": "set_config", "config": cfg}, 0)
        self.assertEqual(d.cfg["calibration"], {})
        # but a config without the key keeps it
        d.cfg["calibration"] = {"it8688/pwm2": {"min_safe_duty": 30}}
        cfg = {k: v for k, v in d.cfg.items() if k != "calibration"}
        d.handle({"cmd": "set_config", "config": cfg}, 0)
        self.assertIn("it8688/pwm2", d.cfg["calibration"])

    def test_profile_names(self):
        d = self.daemon({"allowed_uids": [USER]})
        self.assertFalse(d.handle({"cmd": "save_profile", "name": "Night\n"}, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "save_profile", "name": "silent"}, USER)["ok"])
        self.assertTrue(d.handle({"cmd": "save_profile", "name": "Night (summer)"}, USER)["ok"])

    def test_set_critical_command(self):
        d = self.daemon({"allowed_uids": [USER], "profiles": {"Keep": {}}})
        r = d.handle({"cmd": "set_critical", "cpu_temp": 85}, USER)
        self.assertTrue(r["ok"])
        self.assertEqual(d.cfg["critical"]["cpu_temp"], 85)
        self.assertIn("Keep", d.cfg["profiles"])
        self.assertFalse(d.handle({"cmd": "set_critical", "cpu_temp": 200}, USER)["ok"])


class ClientRetry(unittest.TestCase):
    def test_no_resend_after_timeout(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(5)
        got = []

        def serve():
            srv.settimeout(3)
            try:
                while True:
                    c, _ = srv.accept()
                    got.append(c.recv(100))   # read, never answer
            except OSError:
                pass
        threading.Thread(target=serve, daemon=True).start()
        with self.assertRaises(DaemonError):
            Client(path, timeout=0.5).call("calibrate", fan="x")
        time.sleep(0.3)
        self.assertEqual(len(got), 1)
        srv.close()


@needs_qt
class GuiFixes(Base):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
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

    def tearDown(self):
        self.w.quitting = True
        self.w.close()
        self.d.stop_event.set()
        self.t.join(5)
        super().tearDown()

    def test_profile_switch_with_dirty_editor(self):
        from PySide6.QtWidgets import QMessageBox
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.fan_list.setCurrentRow(0)
        w.editor.rb_fixed.setChecked(True)          # unapplied edit
        self.assertTrue(w.editor.is_dirty)
        asked = []

        def q(*a, **k):
            asked.append(a[1])
            return QMessageBox.Yes if a[1] == "Switch profile" and "unapplied" in a[2] else QMessageBox.Discard
        with mock.patch.object(QMessageBox, "question", side_effect=q):
            w._apply_profile("Performance")
        self.assertEqual(self.d.cfg["active_profile"], "Performance")
        self.assertEqual(self.d.cfg["fans"]["it8688/pwm1"]["mode"], "curve")
        self.assertNotIn("Unsaved changes", asked)

    def test_save_limits_does_not_overwrite_other_changes(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        # someone else (fanpilotctl / other window) changes things after the GUI loaded its config
        self.d.handle({"cmd": "save_profile", "name": "Night"}, 0)
        self.d.handle({"cmd": "set_fan", "fan": "it8688/pwm2", "settings": {"mode": "fixed", "fixed": 77}}, 0)
        w.crit_cpu.setValue(85)
        w._save_critical()
        self.assertIn("Night", self.d.cfg["profiles"])
        self.assertEqual(self.d.cfg["fans"]["it8688/pwm2"]["fixed"], 77)
        self.assertEqual(self.d.cfg["critical"]["cpu_temp"], 85)

    def test_failed_switch_resets_combo(self):
        from PySide6.QtWidgets import QMessageBox
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.refresh()
        self.d.handle({"cmd": "apply_profile", "name": "Balanced"}, 0)
        w.refresh()
        with mock.patch.object(w.client, "call", side_effect=DaemonError("boom")), \
                mock.patch.object(QMessageBox, "warning"):
            w._apply_profile("Performance")
        self.assertEqual(w.profile_combo.currentData(), "Balanced")

    def test_delete_disabled_for_builtin(self):
        self.start({"allowed_uids": [os.getuid()], "profiles": {"Mine": {}}})
        w = self.w
        w.refresh()
        names = [w.profile_list.item(i).data(0x0100) for i in range(w.profile_list.count())]
        w.profile_list.setCurrentRow(names.index("Silent"))
        self.assertFalse(w.p_delete.isEnabled())
        w.profile_list.setCurrentRow(names.index("Mine"))
        self.assertTrue(w.p_delete.isEnabled())

    def test_stalled_shows_warning_in_list(self):
        self.start({"fans": {"it8688/pwm1": {"mode": "fixed", "fixed": 60}}})
        w = self.w
        self.d.stalled.add("it8688/pwm1")
        self.d.seen_spinning.add("it8688/pwm1")
        with self.d.lock:
            self.d.fan_status["it8688/pwm1"]["flags"].append("stalled")
        w.status = self.d.status()
        w._update_fans()
        self.assertIn("⚠", w.fan_list.item(0).text())


@needs_qt
class SingleInstance(unittest.TestCase):
    def test_second_gui_exits_and_first_stays(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        cmd = [sys.executable, "-c", f"import sys; sys.path.insert(0,{repo!r}); from fanpilot.gui import main; sys.exit(main())",
               "--socket", "/nonexistent/fp.sock"]
        first = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2.0)
            self.assertIsNone(first.poll(), "first instance should keep running")
            second = subprocess.run(cmd, env=env, capture_output=True, timeout=10)
            self.assertEqual(second.returncode, 0)
            self.assertIsNone(first.poll())
        finally:
            first.terminate()
            first.wait(5)


if __name__ == "__main__":
    unittest.main()
