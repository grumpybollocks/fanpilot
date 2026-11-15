# SPDX-License-Identifier: GPL-3.0-or-later
"""Universal phase 2: graphics cards (docs/ROADMAP.md)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qt import needs_qt  # noqa: E402

import boards  # noqa: E402
from fanpilot import nvidia  # noqa: E402
from fanpilot.daemon import restore_from_file  # noqa: E402
from fanpilot.hw import AmdPwmFan, GpuFan, discover  # noqa: E402
from sim import FakeGpu, w  # noqa: E402
from test_universal import Machine  # noqa: E402


class OlderAmdCard(Machine):
    def setUp(self):
        self.load(boards.rdna2_pc())
        self.gpu = self.chip_dir("amdgpu")

    def g(self, attr):
        with open(os.path.join(self.gpu, attr)) as f:
            return f.read().strip()

    def test_detected_as_beta_pwm_card(self):
        hw = discover(self.m.root)
        fan = hw.fans["amdgpu/pwm1"]
        self.assertIsInstance(fan, AmdPwmFan)
        self.assertEqual(fan.kind, "gpu")
        self.assertIn("beta", fan.label)

    def test_curve_follows_its_own_temperature_and_hands_back(self):
        d = self.daemon({"allowed_uids": [0], "cpu_fans": ["nct6798/pwm1"],
                         "fans": {"amdgpu/pwm1": {"mode": "curve", "curve": [[40, 30], [80, 80]],
                                                  "source": ["auto:own"], "smoothing": 0}}})
        self.assertEqual(d.resolve_sources(["auto:own"], "amdgpu/pwm1"), ["amdgpu/temp2"])
        self.assertFalse(d.protect_cpu("amdgpu/pwm1"), "a GPU fan is not a CPU fan")
        self.ticks(d, 3)
        self.assertEqual(self.g("pwm1_enable"), "1")
        self.assertAlmostEqual(int(self.g("pwm1")) / 2.55, 30 + 50 * 21 / 40, delta=2)   # 61 °C junction
        self.assertIn(os.path.join(self.gpu, "pwm1_enable"), d.restore["pwm_enable"])
        # a crash: the systemd restore step hands the card back to automatic (2), never 0 (= 100 %)
        self.assertEqual(restore_from_file(d.restore_file), [])
        self.assertEqual(self.g("pwm1_enable"), "2")

    def test_never_identified_calibrated_or_mapped(self):
        d = self.daemon({"allowed_uids": [0]})
        self.assertFalse(d.handle({"cmd": "identify", "fan": "amdgpu/pwm1"}, 0)["ok"])
        self.assertFalse(d.handle({"cmd": "calibrate", "fan": "amdgpu/pwm1"}, 0)["ok"])
        d.handle({"cmd": "map_fans"}, 0)
        self.assertNotIn("amdgpu/pwm1", d.mapping.headers)
        d.handle({"cmd": "map_cancel"}, 0)

    def test_emergency_runs_the_card_at_full_speed(self):
        d = self.daemon({"fans": {"amdgpu/pwm1": {"mode": "fixed", "fixed": 30}}})
        self.ticks(d, 2)
        os.chmod(os.path.join(self.m.k10, "temp1_input"), 0o644)
        self.m.set_temp(self.m.k10, 1, 99)
        self.ticks(d, 2)
        self.assertTrue(d.critical)
        self.assertEqual(self.g("pwm1"), "255")


class Phase2ReviewFixes(Machine):
    def test_older_card_does_not_stop_the_search_for_a_board_chip(self):
        r = boards.rdna2_pc()
        r["hwmon"] = [h for h in r["hwmon"] if h["driver"] != "nct6775"]     # board driver not loaded yet
        self.load(r)
        d = self.daemon()
        self.assertEqual([f for f in d.hw.fans], ["amdgpu/pwm1"])
        self.assertTrue(d._missing_hardware())

    def test_older_card_readings_once_in_the_gpu_group(self):
        self.load(boards.rdna2_pc())
        d = self.daemon({"fans": {"amdgpu/pwm1": {"mode": "fixed", "fixed": 40}}})
        self.ticks(d, 2)
        ids = [x["id"] for x in d.readings]
        self.assertEqual(ids.count("fan:amdgpu/pwm1"), 1)
        self.assertEqual(next(x["group"] for x in d.readings if x["id"] == "fan:amdgpu/pwm1"), "gpu")

    def test_profiles_never_opt_into_the_beta(self):
        self.load(boards.rdna2_pc())
        d = self.daemon({"allowed_uids": [0]})
        d.handle({"cmd": "apply_profile", "name": "Balanced"}, 0)
        self.assertEqual(d.cfg["fans"]["amdgpu/pwm1"]["mode"], "auto")

    def test_rx7000_without_generation_info_still_not_driven_by_pwm(self):
        r = boards.rdna3_no_od_pc()
        del r["drm"][0]["attrs"]["ip_discovery/die/0/GC/0/major"]
        r["drm"][0]["attrs"]["gpu_od/marker"] = "x\n"                    # gpu_od exists (newer card)
        self.load(r)
        hw = discover(self.m.root)
        self.assertNotIn("amdgpu/pwm1", hw.fans)

    def test_nvidia_comes_back_after_an_empty_poll_during_a_rescan(self):
        self.load(boards.rdna2_pc())
        d = self.daemon()
        d.nvidia = FakeSmi({"0000:01:00.0": {"name": "RTX", "temp": 50.0, "fan": 30.0}})
        d.nvidia.updated = 1.0
        self.ticks(d, 1)
        saved = d.nvidia.data
        d.nvidia.data = {}                                                  # one bad poll ...
        d.rescan(force=True)                                                # ... exactly during a rescan
        d.nvidia.data = saved
        self.ticks(d, 1)
        self.assertIn("nvidia@0000:01:00.0/temp1", d.hw.sensors)

    def test_igpu_plus_nvidia_waits_for_the_nvidia_reading(self):
        r = boards.asus_amd()
        r["hwmon"].append(boards.chip("hwmon6", "amdgpu", "0000:10:00.0", "amdgpu",
                                      {"temp1_input": 41000, "temp1_label": "edge"}))       # APU: edge only
        self.load(r)
        d = self.daemon()
        d.nvidia = FakeSmi({})
        d._auto_sensor = {}
        self.assertEqual(d.crit_sensor("gpu"), "amdgpu/temp1")
        self.assertNotIn("gpu", d._auto_sensor, "not latched before nvidia-smi answered")
        d.nvidia.data = {"0000:01:00.0": {"name": "RTX", "temp": 60.0, "fan": 30.0}}
        d.nvidia.updated = 1.0
        self.ticks(d, 1)
        self.assertEqual(d.crit_sensor("gpu"), "nvidia@0000:01:00.0/temp1")


@needs_qt
class OlderAmdCardGui(Machine):
    def test_editor_is_the_board_style_one_following_the_card(self):
        import threading
        import time
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from fanpilot.gui import MainWindow
        self.load(boards.rdna2_pc())
        d = self.daemon({"allowed_uids": [os.getuid()]})
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        for _ in range(100):
            if os.path.exists(d.socket_path):
                break
            time.sleep(0.05)
        w = MainWindow(d.socket_path)
        w.timer.stop()
        try:
            w.refresh()
            ids = [w.fan_list.item(i).data(0x0100) for i in range(w.fan_list.count())]
            w.fan_list.setCurrentRow(ids.index("amdgpu/pwm1"))
            self.assertFalse(w.editor.is_gpu, "not the 5-point firmware editor")
            self.assertEqual(w.editor.sources, ["amdgpu/temp2"], "follows its own hotspot")
            self.assertFalse(w.editor.cpu_fan)
        finally:
            w.quitting = True
            w.close()
            d.stop_event.set()
            t.join(5)


@needs_qt
class AuditGui(Machine):
    def test_rename_title_card_layout_and_busy_buttons(self):
        import threading
        import time
        from unittest import mock
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QInputDialog
        QApplication.instance() or QApplication([])
        from fanpilot.gui import MainWindow
        self.load(boards.rdna2_pc())
        d = self.daemon({"allowed_uids": [os.getuid()], "cpu_fans": ["nct6798/pwm1"]})
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        for _ in range(100):
            if os.path.exists(d.socket_path):
                break
            time.sleep(0.05)
        w = MainWindow(d.socket_path)
        w.timer.stop()
        try:
            w.refresh()
            ids = [w.fan_list.item(i).data(0x0100) for i in range(w.fan_list.count())]
            w.fan_list.setCurrentRow(ids.index("nct6798/pwm3"))
            with mock.patch.object(QInputDialog, "getText", return_value=("Front intake", True)):
                w.rename("nct6798/pwm3", "Fan 3")
            with mock.patch.object(QInputDialog, "getText", return_value=("Water", True)):
                w.rename("nct6798/temp1", "nct6798 SYSTIN")
            time.sleep(1.2)
            w.refresh()
            self.assertEqual(w.editor.title.text(), "Front intake")
            self.assertEqual(w.sensors_view.items["t:nct6798/temp1"].text(0), "Water")
            # older AMD card: no Calibrate box and card wording, also after Apply
            w.fan_list.setCurrentRow(ids.index("amdgpu/pwm1"))
            self.assertFalse(w.editor.cal_box.isVisibleTo(w))
            self.assertIn("graphics card", w.editor.rb_auto.text())
            w.editor.rb_fixed.setChecked(True)
            w.editor.spin.setValue(40)
            w._apply("amdgpu/pwm1", w.editor.settings())
            self.assertFalse(w.editor.cal_box.isVisibleTo(w), "still hidden after Apply")
            # while fans are mapped, nothing else can be started
            self.assertTrue(d.handle({"cmd": "map_fans"}, 0)["ok"])
            w.refresh()
            self.assertFalse(w.test_btn.isEnabled())
            d.handle({"cmd": "map_cancel"}, 0)
            # an older service (no mapping in its status): Map my fans is off
            w.status.pop("mapping", None)
            w._update_mapping()
            self.assertFalse(w.map_btn.isEnabled())
        finally:
            w.quitting = True
            w.close()
            d.stop_event.set()
            t.join(5)


class Rdna3WithoutOverDrive(Machine):
    def test_read_only_with_hint(self):
        self.load(boards.rdna3_no_od_pc())
        hw = discover(self.m.root)
        self.assertFalse([f for f in hw.fans if f.startswith("amdgpu")], "pwm1 is never used on RX 7000/9000")
        self.assertIn("amdgpu/fan1", hw.fan_inputs)
        self.assertTrue(any("ppfeaturemask" in h for h in hw.gpu_hints))


class TwoCards(Machine):
    def setUp(self):
        self.load(boards.two_rdna4_pc())
        hw = discover(self.m.root)
        self.ids = sorted(f for f in hw.fans if isinstance(hw.fans[f], GpuFan))
        self.fakes = {}
        from fanpilot import amdgpu
        real = amdgpu._write
        for fid in self.ids:
            self.fakes[hw.fans[fid].fan_ctrl_dir] = FakeGpu(hw.fans[fid].fan_ctrl_dir)
        def write(path, value):
            for fc, fake in self.fakes.items():
                if path.startswith(fc):
                    return fake.write(path, value)
            return real(path, value)
        amdgpu._write = write
        self.addCleanup(setattr, amdgpu, "_write", real)

    def test_each_card_its_own_curve_and_restore(self):
        self.assertEqual(self.ids, ["amdgpu@0000:0b:00.0/fan1", "amdgpu@0000:0e:00.0/fan1"])
        d = self.daemon({"fans": {self.ids[0]: {"mode": "curve", "curve": [[40, 30], [80, 80]]},
                                  self.ids[1]: {"mode": "fixed", "fixed": 60}}})
        self.ticks(d, 2)
        a, b = (self.fakes[d.hw.fans[f].fan_ctrl_dir].committed for f in self.ids)
        self.assertNotEqual(a, b)
        self.assertEqual(set(b), {(t, 60) for t, _ in b})
        self.assertEqual(len(d.restore["gpu_fan_ctrls"]), 2)
        self.assertEqual(restore_from_file(d.restore_file), [])
        self.assertTrue(all(f.committed is None for f in self.fakes.values()), "both cards back to automatic")

    def test_old_restore_file_is_still_honoured(self):
        """A crash under an older FanPilot left the single-card key: the restore must still reset it."""
        fc = next(iter(self.fakes))
        self.fakes[fc].committed = [(40, 30)] * 5
        path = os.path.join(self.m.root, "old-restore.json")
        with open(path, "w") as f:
            json.dump({"pwm_enable": {}, "gpu_fan_ctrl": fc}, f)
        self.assertEqual(restore_from_file(path), [])
        self.assertIsNone(self.fakes[fc].committed)


class FakeSmi:
    def __init__(self, data):
        self.data = data
        self.binary = "nvidia-smi"
        self.updated = 0.0

    def snapshot(self):
        return dict(self.data)


class Nvidia(Machine):
    def test_parse(self):
        out = nvidia.parse("00000000:01:00.0, NVIDIA GeForce RTX 4070, 45, 30\n"
                           "00000000:02:00.0, NVIDIA GeForce GTX 1650, 52, [N/A]\nnonsense\n")
        self.assertEqual(out["0000:01:00.0"], {"name": "NVIDIA GeForce RTX 4070", "temp": 45.0, "fan": 30.0})
        self.assertIsNone(out["0000:02:00.0"]["fan"])

    def test_read_only_readings_and_emergency_sensor(self):
        self.load(boards.asus_amd())                        # no AMD card: the NVIDIA card is the GPU
        d = self.daemon()
        d.nvidia = FakeSmi({"0000:01:00.0": {"name": "NVIDIA GeForce RTX 4070", "temp": 47.0, "fan": 35.0}})
        d.nvidia.updated = 1.0
        d._auto_sensor = {}
        self.ticks(d, 2)
        self.assertEqual(d.crit_sensor("gpu"), "nvidia@0000:01:00.0/temp1")
        r = {x["id"]: x for x in d.readings}
        self.assertEqual(r["t:nvidia@0000:01:00.0/temp1"]["value"], 47.0)
        self.assertEqual((r["fan:nvidia@0000:01:00.0/fan1"]["value"], r["fan:nvidia@0000:01:00.0/fan1"]["group"]),
                         (35.0, "gpu:nvidia@0000:01:00.0"))
        titles = {g["id"]: g["title"] for g in d.status()["groups"]}
        self.assertEqual(titles["gpu:nvidia@0000:01:00.0"], "NVIDIA GeForce RTX 4070", "group named after the card")
        self.assertFalse([f for f in d.hw.fans if f.startswith("nvidia")], "never controlled in v1")
        for _ in range(3):                                   # rescans keep the NVIDIA readings, quietly
            d.rescan()
        self.assertIn("nvidia@0000:01:00.0/temp1", d.hw.sensors)
        self.assertFalse(any("Hardware rescan" in e["msg"] for e in d.events))
        d.nvidia.data["0000:01:00.0"]["temp"] = 101.0
        self.ticks(d, 1)
        self.assertTrue(d.critical, "a hot NVIDIA card triggers the emergency mode for the board fans")


if __name__ == "__main__":
    unittest.main()
