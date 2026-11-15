# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for profiles, identify, stall detection, privilege rules and the CLI."""
import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fanpilot import config as cfgmod, ctl, daemon as dmod, presets  # noqa: E402
from test_daemon import Base  # noqa: E402

USER = 1000


class ProfileTests(Base):
    def test_builtin_profile_sets_every_fan(self):
        d = self.daemon({"allowed_uids": [USER]})
        r = d.handle({"cmd": "apply_profile", "name": "Silent"}, USER)
        self.assertTrue(r["ok"], r)
        fans = d.cfg["fans"]
        self.assertEqual(set(fans), set(d.hw.fans))
        self.assertTrue(all(f["mode"] == "curve" for f in fans.values()))
        self.assertEqual(fans["it8688/pwm1"]["source"], ["k10temp/temp1"])
        self.assertEqual(fans["it8688/pwm2"]["source"], ["k10temp/temp1", "amdgpu/temp2"])
        self.assertEqual(fans["amdgpu/fan1"]["curve"], presets.GPU["Silent"])
        self.assertEqual(d.cfg["active_profile"], "Silent")
        self.ticks(d, 2)
        self.assertEqual(self.m.enable(2), 1)
        self.assertEqual(self.gpu.commits, 1)

    def test_every_builtin_profile_validates(self):
        ids = ["it8688/pwm1", "it8688/pwm2", "it8688/pwm3", "amdgpu/fan1"]
        for name in presets.BUILTIN_PROFILES:
            cfgmod.validate({"fans": presets.builtin_profile(name, ids)})

    def test_save_apply_delete_user_profile(self):
        d = self.daemon({"allowed_uids": [USER], "fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 33}}})
        self.assertTrue(d.handle({"cmd": "save_profile", "name": "Night"}, USER)["ok"])
        self.assertEqual(d.cfg["profiles"]["Night"]["it8688/pwm2"]["fixed"], 33)
        d.handle({"cmd": "apply_profile", "name": "Performance"}, USER)
        self.assertEqual(d.cfg["fans"]["it8688/pwm2"]["mode"], "curve")
        d.handle({"cmd": "apply_profile", "name": "Night"}, USER)
        self.assertEqual(d.cfg["fans"]["it8688/pwm2"], d.cfg["profiles"]["Night"]["it8688/pwm2"])
        self.assertEqual(d.status()["active_profile"], "Night")
        self.assertIn("Night", d.status()["profiles"])
        self.assertTrue(d.handle({"cmd": "delete_profile", "name": "Night"}, USER)["ok"])
        self.assertNotIn("Night", d.cfg["profiles"])
        self.assertIsNone(d.cfg["active_profile"])
        # persisted
        self.assertEqual(cfgmod.load(self.cfg_path)["profiles"], {})

    def test_profile_name_rules(self):
        d = self.daemon({"allowed_uids": [USER]})
        for bad in ("", " x", "a" * 40, "../etc", None, 5):
            with self.subTest(bad=bad):
                self.assertFalse(d.handle({"cmd": "save_profile", "name": bad}, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "save_profile", "name": "Silent"}, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "apply_profile", "name": "Nope"}, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "delete_profile", "name": "Silent"}, USER)["ok"])

    def test_hand_tuning_clears_active_profile(self):
        d = self.daemon({"allowed_uids": [USER]})
        d.handle({"cmd": "apply_profile", "name": "Balanced"}, USER)
        d.handle({"cmd": "set_fan", "fan": "it8688/pwm3", "settings": {"mode": "fixed", "fixed": 50}}, USER)
        self.assertIsNone(d.cfg["active_profile"])

    def test_profile_for_missing_fan_is_ignored(self):
        d = self.daemon({"allowed_uids": [USER],
                         "profiles": {"Old": {"nct6798/pwm2": {"mode": "fixed", "fixed": 50},
                                              "it8688/pwm2": {"mode": "fixed", "fixed": 44}}}})
        self.assertTrue(d.handle({"cmd": "apply_profile", "name": "Old"}, USER)["ok"])
        self.assertNotIn("nct6798/pwm2", d.cfg["fans"])
        self.assertEqual(d.cfg["fans"]["it8688/pwm2"]["fixed"], 44)


class PrivilegeTests(Base):
    def test_user_cannot_change_allowed_uids_or_calibration(self):
        d = self.daemon({"allowed_uids": [USER]})
        new = json.loads(json.dumps(d.cfg))
        new["allowed_uids"] = [USER, 1234]
        new["calibration"] = {"it8688/pwm2": {"min_safe_duty": 1}}
        self.assertTrue(d.handle({"cmd": "set_config", "config": new}, USER)["ok"])
        self.assertEqual(d.cfg["allowed_uids"], [USER])
        self.assertEqual(d.cfg["calibration"], {})
        # and leaving the key out does not lock the user out
        del new["allowed_uids"]
        d.handle({"cmd": "set_config", "config": new}, USER)
        self.assertEqual(d.cfg["allowed_uids"], [USER])

    def test_root_can(self):
        d = self.daemon({"allowed_uids": [USER]})
        new = json.loads(json.dumps(d.cfg))
        new["allowed_uids"] = [USER, 1234]
        d.handle({"cmd": "set_config", "config": new}, 0)
        self.assertEqual(d.cfg["allowed_uids"], [USER, 1234])

    def test_bad_inputs_rejected(self):
        d = self.daemon({"allowed_uids": [USER]})
        for req in ({"cmd": "set_config", "config": "x"},
                    {"cmd": "set_config", "config": {"fans": {"../x": {}}}},
                    {"cmd": "set_fan", "fan": "it8688/pwm2", "settings": [1]},
                    {"cmd": "identify", "fan": "amdgpu/fan1"},
                    {"cmd": "identify", "fan": "nope"}):
            with self.subTest(req=req):
                self.assertFalse(d.handle(req, USER)["ok"])
        self.assertFalse(d.handle({"cmd": "identify", "fan": "it8688/pwm2"}, 4321)["ok"])  # not allowed


class IdentifyTests(Base):
    def test_identify_auto_fan_then_hand_back(self):
        d = self.daemon({"allowed_uids": [USER]})
        self.ticks(d)
        self.assertEqual(self.m.enable(2), 2)
        self.assertTrue(d.handle({"cmd": "identify", "fan": "it8688/pwm2"}, USER)["ok"])
        seen = []
        for _ in range(7):
            self.ticks(d)
            seen.append(self.m.pwm(2))
            if len(seen) == 1:
                self.assertIn("identifying", d.fan_status["it8688/pwm2"]["flags"])
        # slow first (25 %), then full speed, then handed back
        self.assertEqual(seen[:3], [64, 64, 64])
        self.assertEqual(seen[3:6], [255, 255, 255])
        self.ticks(d)
        self.assertEqual(self.m.enable(2), 2)  # back to BIOS
        self.assertNotIn("it8688/pwm2", d.identify)

    def test_identify_fixed_fan_returns_to_setting(self):
        d = self.daemon({"allowed_uids": [USER], "fans": {"it8688/pwm3": {"mode": "fixed", "fixed": 40}}})
        self.ticks(d)
        d.handle({"cmd": "identify", "fan": "it8688/pwm3"}, USER)
        self.ticks(d, 4)
        self.assertEqual(self.m.pwm(3), 255)
        self.ticks(d, 8)
        self.assertEqual(self.m.pwm(3), round(40 * 2.55))


class StallTests(Base):
    def run_fan(self, d, n, rpm):
        for _ in range(n):
            with open(os.path.join(self.m.board, "fan2_input"), "w") as f:
                f.write(f"{rpm}\n")
            self.ticks(d)

    def test_stall_warns_once_and_recovers(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 60}}})
        self.run_fan(d, 3, 900)
        self.run_fan(d, 9, 0)
        self.assertNotIn("stalled", d.fan_status["it8688/pwm2"]["flags"])
        self.run_fan(d, 3, 0)
        self.assertIn("stalled", d.fan_status["it8688/pwm2"]["flags"])
        errs = [e for e in d.events if e["level"] == "error" and "stopped" in e["msg"]]
        self.assertEqual(len(errs), 1)
        self.run_fan(d, 5, 0)
        self.assertEqual(len([e for e in d.events if "stopped" in e["msg"]]), 1)  # no spam
        self.run_fan(d, 1, 800)
        self.assertNotIn("stalled", d.fan_status["it8688/pwm2"]["flags"])
        self.assertTrue(any("spinning again" in e["msg"] for e in d.events))

    def test_empty_header_never_warns(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 60}}})
        self.run_fan(d, 30, 0)
        self.assertFalse(any("stopped" in e["msg"] for e in d.events))

    def test_low_duty_zero_rpm_is_not_a_stall(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 0, "min_duty": 0, "allow_stop": True}}})
        self.run_fan(d, 2, 900)
        self.run_fan(d, 20, 0)
        self.assertFalse(any("stopped" in e["msg"] for e in d.events))

    def test_cpu_fan_message(self):
        d = self.daemon({"fans": {"it8688/pwm1": {"mode": "fixed", "fixed": 60}}})
        for rpm, n in ((900, 2), (0, 12)):
            for _ in range(n):
                with open(os.path.join(self.m.board, "fan1_input"), "w") as f:
                    f.write(f"{rpm}\n")
                self.ticks(d)
        self.assertTrue(any("CPU FAN STOPPED" in e["msg"] for e in d.events))


class RealDataFixes(Base):
    def test_wmi_duplicates_hidden_only_with_board_chip(self):
        from fanpilot.hw import discover
        hw = discover(self.m.root)
        self.assertFalse(any(s.startswith("gigabyte_wmi") for s in hw.sensors))
        from sim import FakeMachine
        m2 = FakeMachine(with_board=False)
        self.assertTrue(any(s.startswith("gigabyte_wmi") for s in discover(m2.root).sensors))
        m2.cleanup()

    def test_bios_fan_reports_no_duty(self):
        d = self.daemon({"fans": {"it8688/pwm3": {"mode": "fixed", "fixed": 40}}})
        self.ticks(d)
        st = {f["id"]: f for f in d.status()["fans"]}
        self.assertIsNone(st["it8688/pwm2"]["duty"])      # BIOS: pwm register is stale
        self.assertTrue(st["it8688/pwm2"]["bios"])
        self.ticks(d)
        st = {f["id"]: f for f in d.status()["fans"]}
        self.assertAlmostEqual(st["it8688/pwm3"]["duty"], 40, delta=0.5)
        self.assertFalse(st["it8688/pwm3"]["bios"])


class CliTests(Base):
    def setUp(self):
        super().setUp()
        self.d = self.daemon({"allowed_uids": [os.getuid()]})
        self.t = threading.Thread(target=self.d.run, daemon=True)
        self.t.start()
        self.sock = os.path.join(self.run_dir, "s.sock")
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)

    def tearDown(self):
        self.d.stop_event.set()
        self.t.join(5)
        super().tearDown()

    def ctl(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ctl.main(["--socket", self.sock, *args])
        return rc, out.getvalue()

    def test_status_and_profiles(self):
        rc, out = self.ctl("status")
        self.assertEqual(rc, 0)
        self.assertIn("CPU fan (CPU_FAN)", out)
        self.assertIn("profile: custom", out)
        self.assertEqual(self.ctl("profile", "apply", "Balanced")[0], 0)
        rc, out = self.ctl("profile", "list")
        self.assertIn("* Balanced", out)
        self.assertEqual(self.ctl("profile", "save", "Mine")[0], 0)
        self.assertEqual(self.ctl("profile", "delete", "Mine")[0], 0)
        self.assertEqual(self.ctl("profile", "apply", "Nope")[0], 1)

    def test_set_identify_import(self):
        self.assertEqual(self.ctl("set", "it8688/pwm2", "fixed", "45")[0], 0)
        self.assertEqual(self.d.cfg["fans"]["it8688/pwm2"]["fixed"], 45)
        self.assertEqual(self.ctl("set", "it8688/pwm2", "curve", "40:30,70:80")[0], 0)
        self.assertEqual(self.d.cfg["fans"]["it8688/pwm2"]["curve"], [[40, 30], [70, 80]])
        self.assertEqual(self.ctl("set", "it8688/pwm2", "curve", "40:90,70:20")[0], 1)  # rejected
        self.assertEqual(self.ctl("set", "it8688/pwm2", "curve", "garbage")[0], 2)
        self.assertEqual(self.ctl("identify", "it8688/pwm3")[0], 0)
        rc, out = self.ctl("config")
        cfg = json.loads(out)
        cfg["fans"]["it8688/pwm3"] = {"mode": "fixed", "fixed": 66}
        p = os.path.join(self.m.root, "exported.json")
        with open(p, "w") as f:
            json.dump(cfg, f)
        self.assertEqual(self.ctl("import", p)[0], 0)
        self.assertEqual(self.d.cfg["fans"]["it8688/pwm3"]["fixed"], 66)
        self.assertEqual(self.ctl("import", "/nonexistent.json")[0], 2)


if __name__ == "__main__":
    unittest.main()
