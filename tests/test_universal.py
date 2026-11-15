# SPDX-License-Identifier: GPL-3.0-or-later
"""FanPilot on boards other than the reference one (docs/ROADMAP.md)."""
import glob
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qt import needs_qt  # noqa: E402

import boards  # noqa: E402
from fanpilot import config as cfgmod  # noqa: E402
from fanpilot.daemon import Daemon, safe_enable_value  # noqa: E402
from fanpilot.hw import discover  # noqa: E402
from sim import machine_from_report  # noqa: E402

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


class Machine(unittest.TestCase):
    def load(self, report):
        self.m = machine_from_report(report)
        self.addCleanup(self.m.cleanup)
        self.cfg_path = os.path.join(self.m.root, "etc/fanpilot/config.json")
        self.run_dir = os.path.join(self.m.root, "run/fanpilot")
        self.now = 1000.0
        return self.m

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

    def chip_dir(self, name):
        hw = discover(self.m.root)
        return next(d for c, d in hw.chips.items() if hw.names[c] == name)

    def val(self, chip_name, attr):
        with open(os.path.join(self.chip_dir(chip_name), attr)) as f:
            return f.read().strip()


class AsusNuvoton(Machine):
    def setUp(self):
        self.load(boards.asus_amd())

    def test_detection(self):
        hw = discover(self.m.root)
        self.assertEqual(hw.board_chips, ["nct6798"])
        self.assertIsNone(hw.board, "no database entry: everything generic")
        # channels with a tachometer input; generic names
        self.assertEqual([f.label for f in hw.fans.values()], [f"Fan {i}" for i in range(1, 8)])
        self.assertEqual(hw.cpu_fans, set(), "nobody knows which one is the CPU fan")
        # impossible temperatures (127 / -62 = nothing connected) are hidden on an unknown board
        self.assertNotIn("nct6798/temp3", hw.sensors)
        self.assertNotIn("nct6798/temp4", hw.sensors)
        self.assertEqual(hw.sensors["nct6798/temp2"].label, "CPU socket (CPUTIN)")
        self.assertIn("asusec/temp2", hw.sensors)
        self.assertIn("asusec/fan1", hw.fan_inputs, "EC fans are read-only readings")
        self.assertNotIn("asusec/fan1", hw.fans)
        self.assertEqual(hw.default_cpu_sensor(), "k10temp/temp1")
        self.assertIsNone(hw.default_gpu_sensor())

    def test_unconnected_pch_channels_reading_zero_hidden(self):
        """Real TUF GAMING X570-PLUS (2026-11-15): nct6798 PCH_CHIP_TEMP & co. read exactly 0 on AMD boards."""
        nct = self.chip_dir("nct6798")
        for f, v in (("temp10_input", "0"), ("temp10_label", "PCH_CHIP_TEMP"),
                     ("temp13_input", "50375"), ("temp13_label", "TSI0_TEMP")):
            with open(os.path.join(nct, f), "w") as fh:
                fh.write(v + "\n")
        hw = discover(self.m.root)
        self.assertNotIn("nct6798/temp10", hw.sensors, "0.000 °C = nothing connected")
        self.assertIn("nct6798/temp13", hw.sensors, "a real reading next to it stays")

    def test_every_fan_protected_until_the_cpu_fan_is_marked(self):
        d = self.daemon({"allowed_uids": [0]})
        self.assertTrue(all(d.protect_cpu(f) for f in d.hw.fans))
        out = d.handle({"cmd": "set_fan", "fan": "nct6798/pwm3",
                        "settings": {"mode": "fixed", "fixed": 0, "allow_stop": True, "min_duty": 0}}, 0)
        self.assertTrue(out["ok"])
        self.assertFalse(d.cfg["fans"]["nct6798/pwm3"]["allow_stop"])
        self.assertEqual(d.cfg["fans"]["nct6798/pwm3"]["min_duty"], cfgmod.CPU_FAN_FLOOR)
        self.ticks(d, 2)
        self.assertGreaterEqual(int(self.val("nct6798", "pwm3")), round(cfgmod.CPU_FAN_FLOOR * 2.55))
        self.assertTrue(d.handle({"cmd": "set_cpu_fans", "fans": ["nct6798/pwm1"]}, 0)["ok"])
        self.assertFalse(d.protect_cpu("nct6798/pwm3"))
        self.assertTrue(d.protect_cpu("nct6798/pwm1"))
        self.assertTrue(d.status()["cpu_fan_known"])

    def test_bios_mode_restored_exactly(self):
        """nct6775 'Smart Fan IV' (5) must come back as 5, not as 2 (Thermal Cruise)."""
        d = self.daemon({"fans": {"nct6798/pwm3": {"mode": "fixed", "fixed": 60}}})
        self.ticks(d, 2)
        self.assertEqual(self.val("nct6798", "pwm3_enable"), "1")
        d.handle({"cmd": "set_fan", "fan": "nct6798/pwm3", "settings": {"mode": "auto"}}, 0)
        self.ticks(d, 1)
        self.assertEqual(self.val("nct6798", "pwm3_enable"), "5")
        self.assertEqual(safe_enable_value(1), 2, "manual is never restored (it would freeze the fan)")

    def test_read_only_chips_never_written(self):
        before = {a: self.val("asusec", a) for a in ("temp1_input", "fan1_input")}
        d = self.daemon({"fans": {"nct6798/pwm1": {"mode": "fixed", "fixed": 70}}})
        self.ticks(d, 3)
        self.assertEqual(before, {a: self.val("asusec", a) for a in before})
        out = d.handle({"cmd": "set_fan", "fan": "asusec/fan1", "settings": {"mode": "fixed", "fixed": 30}}, 0)
        self.assertFalse(out["ok"])

    def test_sensors_view_and_selftest_safety(self):
        d = self.daemon()
        self.ticks(d, 2)
        groups = {g["id"]: g["title"] for g in d.status()["groups"]}
        self.assertEqual(groups["board"], "ASUSTeK COMPUTER INC. TUF GAMING X570-PLUS (Nuvoton NCT6798)")
        # unknown board: only the chip's fixed-function pins (driver-scaled), never the board-wired VIN inputs
        self.assertEqual(sorted(r["id"] for r in d.readings if r["id"].startswith("v:")),
                         sorted(f"v:nct6798/in{n}" for n in (0, 2, 3, 7, 8)), "no unscaled voltages")
        self.assertIn("fan:asusec/fan1", {r["id"] for r in d.readings})
        d.handle({"cmd": "selftest"}, 0)
        for _ in range(200):
            if d.selftest.state != "running":
                break
            self.ticks(d, 1, dt=2)
        names = {r["name"]: r for r in d.selftest.results}
        self.assertIn("skipped: mark which fan is the CPU fan first", names["Speed control"]["detail"])
        self.assertFalse(any(k.endswith("speed control") for k in names), "no fan was slowed for the test")


class FanMappingTests(Machine):
    """'Map my fans' with crossed wiring: header 2 drives tachometer 3; headers 3, 5-7 have no fan."""
    WIRING = {1: 1, 2: 3, 4: 4}             # header -> tachometer

    def setUp(self):
        self.load(boards.asus_amd())
        self.nct = self.chip_dir("nct6798")
        for i in range(1, 8):                  # the "kernel" side of the simulation updates the tachometers
            os.chmod(os.path.join(self.nct, f"fan{i}_input"), 0o644)
        os.chmod(os.path.join(self.m.k10, "temp1_input"), 0o644)

    no_tach = ()

    def physics(self):
        for i in range(1, 8):
            if i in self.no_tach:
                continue
            with open(os.path.join(self.nct, f"fan{i}_input"), "w") as f:
                f.write("0\n")
        for header, tach in self.WIRING.items():
            with open(os.path.join(self.nct, f"pwm{header}")) as f:
                duty = int(f.read()) * 100 / 255
            with open(os.path.join(self.nct, f"fan{tach}_input"), "w") as f:
                f.write(f"{int(300 + duty * 12)}\n")

    def run_mapping(self, d, max_ticks=200, before_tick=None):
        self.assertTrue(d.handle({"cmd": "map_fans"}, 0)["ok"])
        for _ in range(max_ticks):
            self.physics()
            if before_tick:
                before_tick()
            self.ticks(d)
            if d.mapping.state != "running":
                break

    def test_crossed_wiring_found_and_used(self):
        d = self.daemon({"cpu_fans": ["nct6798/pwm1"]})   # the user marked the CPU fan (Identify)
        self.run_mapping(d)
        self.assertEqual(d.mapping.state, "done", d.mapping.details)
        self.assertEqual(d.cfg["fan_map"], {"nct6798/pwm1": "fan1_input", "nct6798/pwm2": "fan3_input",
                                            "nct6798/pwm3": None, "nct6798/pwm4": "fan4_input",
                                            "nct6798/pwm5": None, "nct6798/pwm6": None, "nct6798/pwm7": None})
        self.assertTrue(d.hw.fans["nct6798/pwm2"].rpm_path.endswith("fan3_input"))
        self.assertNotIn("nct6798/pwm3", d.hw.fans, "header without a fan is hidden")
        for i in range(1, 8):
            self.assertEqual(self.val("nct6798", f"pwm{i}_enable"), "5", f"pwm{i} back to Smart Fan IV")
        self.assertEqual(d.restore["pwm_enable"], {}, "nothing left taken over")
        self.ticks(d, 2)
        d2 = self.daemon()
        self.assertTrue(d2.hw.fans["nct6798/pwm2"].rpm_path.endswith("fan3_input"), "map survives a restart")
        self.assertTrue(d2.handle({"cmd": "map_forget"}, 0)["ok"])
        self.assertIn("nct6798/pwm3", d2.hw.fans, "forgetting the map brings hidden headers back")
        for _ in range(5):                                   # no rescan spam once the map is applied
            d2.next_rescan = 0
            d2.rescan()
        self.assertEqual(sum("Hardware rescan" in e["msg"] for e in d2.events), 1)

    def test_forget_hands_back_headers_only_the_map_added(self):
        """fan2 has no tachometer of its own: pwm2 exists only through the map (pwm2 -> fan3)."""
        os.remove(os.path.join(self.nct, "fan2_input"))
        self.no_tach = (2,)
        d = self.daemon({"cpu_fans": ["nct6798/pwm1"], "allowed_uids": [0]})
        self.assertNotIn("nct6798/pwm2", d.hw.fans)
        self.run_mapping(d)
        self.assertIn("nct6798/pwm2", d.hw.fans)
        d.handle({"cmd": "set_fan", "fan": "nct6798/pwm2", "settings": {"mode": "fixed", "fixed": 50}}, 0)
        self.ticks(d, 2)
        self.assertEqual(self.val("nct6798", "pwm2_enable"), "1")
        self.assertTrue(d.handle({"cmd": "map_forget"}, 0)["ok"])
        self.assertNotIn("nct6798/pwm2", d.hw.fans)
        self.assertEqual(self.val("nct6798", "pwm2_enable"), "5", "handed back, not left in manual")

    def test_no_rescan_writes_during_mapping(self):
        d = self.daemon({"cpu_fans": ["nct6798/pwm1"]})
        self.run_mapping(d)                                  # first map hides pwm3, 5-7
        writes = []
        real = d._release
        d._release = lambda fan: (writes.append(fan.id), real(fan))
        d.cfg["fans"]["amdgpu/fan1"] = {"mode": "curve", "curve": [[40, 30], [80, 80]], "zero_rpm": True}
        self.assertTrue(d._missing_hardware(), "a configured GPU fan is missing: rescans would run")
        self.assertTrue(d.handle({"cmd": "map_fans"}, 0)["ok"])
        for _ in range(15):
            self.physics()
            self.ticks(d)
        self.assertEqual(d.mapping.state, "running")
        self.assertEqual(writes, [], "nothing handed back in the middle of a mapping run")
        d.handle({"cmd": "map_cancel"}, 0)

    def test_unknown_cpu_fan_nothing_hidden(self):
        d = self.daemon({"allowed_uids": [0]})               # CPU fan not known: every header protected
        self.run_mapping(d)
        self.assertEqual(d.mapping.state, "done")
        self.assertEqual(len(d.hw.fans), 7, "no header hidden while any could be the CPU fan")
        # ...but what the run proved is kept: 3, 5-7 drive nothing (every spinning tach is claimed)
        self.assertEqual(d.cfg["fan_map"], {"nct6798/pwm1": "fan1_input", "nct6798/pwm2": "fan3_input",
                                            "nct6798/pwm3": None, "nct6798/pwm4": "fan4_input",
                                            "nct6798/pwm5": None, "nct6798/pwm6": None, "nct6798/pwm7": None})
        self.assertTrue(any("shown until you mark the CPU fan" in x for x in d.mapping.details))
        # marking the CPU fan hides them at once: no second mapping run
        self.assertTrue(d.handle({"cmd": "set_cpu_fans", "fans": ["nct6798/pwm1"]}, 0)["ok"])
        self.assertEqual(sorted(d.hw.fans), ["nct6798/pwm1", "nct6798/pwm2", "nct6798/pwm4"])
        for i in range(1, 8):
            self.assertEqual(self.val("nct6798", f"pwm{i}_enable"), "5", f"pwm{i} still on Smart Fan IV")
        # marking an empty header as the CPU fan instead keeps that one
        d.handle({"cmd": "map_forget"}, 0)
        d.handle({"cmd": "set_cpu_fans", "fans": []}, 0)
        self.run_mapping(d)
        self.assertTrue(d.handle({"cmd": "set_cpu_fans", "fans": ["nct6798/pwm3"]}, 0)["ok"])
        self.assertIn("nct6798/pwm3", d.hw.fans, "a marked CPU fan is never hidden")

    def test_unknown_cpu_fan_unclaimed_spinning_tach_keeps_warning(self):
        """A spinning fan no header slowed (DC fan?) could be on any silent header: nothing recorded."""
        d = self.daemon()

        def dc_fan():
            with open(os.path.join(self.nct, "fan6_input"), "w") as f:
                f.write("1500\n")
        self.run_mapping(d, before_tick=dc_fan)
        self.assertEqual(d.mapping.state, "done")
        for h in (3, 5, 7):
            self.assertNotIn(f"nct6798/pwm{h}", d.cfg["fan_map"], f"pwm{h} undecided: could drive fan6")
        self.assertTrue(any("mark the CPU fan first, then map again" in x for x in d.mapping.details))

    def test_dc_fan_that_does_not_slow_is_kept(self):
        self.WIRING = {}                                     # tachometers spin, nothing reacts to PWM
        d = self.daemon({"cpu_fans": ["nct6798/pwm1"]})

        def constant():
            for i in (1, 3, 4):
                with open(os.path.join(self.nct, f"fan{i}_input"), "w") as f:
                    f.write("1500\n")
        self.run_mapping(d, before_tick=constant)
        self.assertEqual(d.mapping.state, "done")
        for fid in ("nct6798/pwm1", "nct6798/pwm3", "nct6798/pwm4"):
            self.assertIn(fid, d.hw.fans, f"{fid} spins: never hidden")

    def test_never_below_cpu_floor_and_aborts_when_hot(self):
        d = self.daemon()
        lowest = []
        pwm_files = [os.path.join(self.nct, f"pwm{i}") for i in range(1, 8)]

        def watch():
            lowest.append(min(int(open(p).read()) for p in pwm_files))
            if len(lowest) == 30:
                self.m.set_temp(self.m.k10, 1, 76)          # gets hot during mapping
        self.run_mapping(d, before_tick=watch)
        self.assertEqual(d.mapping.state, "aborted")
        self.assertGreaterEqual(min(lowest), round(30 * 2.55) - 1, "never below 30 %")
        self.ticks(d, 2)
        for i in range(1, 8):
            self.assertEqual(self.val("nct6798", f"pwm{i}_enable"), "5")
        self.assertEqual(d.cfg["fan_map"], {}, "an aborted run saves nothing")


class MsiIntel(Machine):
    def setUp(self):
        self.load(boards.msi_intel())

    def test_labelled_cpu_fan_and_intel_cpu(self):
        hw = discover(self.m.root)
        self.assertEqual((hw.cpu_fans, hw.pump_fans), ({"nct6687/pwm1"}, {"nct6687/pwm2"}))
        self.assertEqual(hw.fans["nct6687/pwm3"].label, "System Fan #1")
        self.assertEqual(hw.default_cpu_sensor(), "coretemp/temp1")
        self.assertEqual(hw.sensors["coretemp/temp2"].label, "CPU core 0")
        d = self.daemon()
        self.assertEqual(d.crit_sensor("cpu"), "coretemp/temp1")
        self.assertFalse(d.protect_cpu("nct6687/pwm3"))

    def test_two_nvme_drives_get_stable_ids(self):
        hw = discover(self.m.root)
        self.assertIn("nvme@nvme0/temp1", hw.sensors)
        self.assertIn("nvme@nvme1/temp1", hw.sensors)
        os.rename(os.path.join(self.m.root, "sys/class/hwmon/hwmon0"), os.path.join(self.m.root, "sys/class/hwmon/hwmon9"))
        hw2 = discover(self.m.root)
        self.assertEqual((hw2.sensors["nvme@nvme0/temp1"].read(), hw2.sensors["nvme@nvme1/temp1"].read()),
                         (36.85, 41.85), "renumbered hwmon: same id, same drive")

    def test_profile_without_gpu_sensor(self):
        d = self.daemon({"allowed_uids": [0]})
        self.assertTrue(d.handle({"cmd": "apply_profile", "name": "Balanced"}, 0)["ok"])
        self.assertEqual(d.cfg["fans"]["nct6687/pwm3"]["source"], ["coretemp/temp1"])
        self.ticks(d, 3)
        self.assertFalse(d.critical)


class ReviewFixesPhase1(Machine):
    def test_pump_never_stops(self):
        self.load(boards.msi_intel())
        d = self.daemon({"allowed_uids": [0]})
        d.handle({"cmd": "set_fan", "fan": "nct6687/pwm2",
                  "settings": {"mode": "fixed", "fixed": 0, "allow_stop": True, "min_duty": 0}}, 0)
        self.ticks(d, 5)
        self.assertGreaterEqual(int(self.val("nct6687", "pwm2")), round(cfgmod.CPU_FAN_FLOOR * 2.55))

    def test_saving_a_limit_never_ends_an_emergency(self):
        import shutil
        self.load(boards.asus_amd())
        d = self.daemon({"allowed_uids": [0]})
        self.ticks(d, 1)
        shutil.rmtree(os.path.dirname(discover(self.m.root).sensors["k10temp/temp1"].path))
        d.next_rescan = 0
        self.ticks(d, 4)
        self.assertTrue(d.critical)
        cfg = json.loads(json.dumps(d.cfg))
        cfg["critical"]["gpu_temp"] = 99
        d.handle({"cmd": "set_config", "config": cfg}, 0)
        self.ticks(d, 3)
        self.assertTrue(d.critical, "still blind: still an emergency")

    def test_no_false_inactive_warning(self):
        self.load(boards.asus_amd())
        d = self.daemon()
        d.stop_event.set()
        d.run()
        self.assertFalse(any("protection is inactive" in e["msg"] for e in d.events),
                         [e["msg"] for e in d.events])

    def test_pump_label_alone_keeps_the_protect_everything_fallback(self):
        r = boards.asus_amd()
        r["hwmon"][2]["attrs"]["fan4_label"] = {"v": "W_PUMP+\n", "w": False}
        self.load(r)
        d = self.daemon()
        self.assertEqual(d.hw.pump_fans, {"nct6798/pwm4"})
        self.assertTrue(all(d.protect_cpu(f) for f in d.hw.fans), "CPU fan still unknown: all protected")
        self.assertFalse(d.status()["cpu_fan_known"])

    def test_stale_cpu_fan_mark_does_not_disable_protection(self):
        self.load(boards.asus_amd())
        d = self.daemon({"cpu_fans": ["nct6799/pwm1"]})      # a fan that doesn't exist (anymore)
        self.assertTrue(all(d.protect_cpu(f) for f in d.hw.fans))

    def test_in_tree_nct6683_is_read_only_whatever_its_name(self):
        r = boards.readonly_board()
        r["hwmon"][2]["attrs"]["name"]["v"] = "nct6687\n"   # the in-tree driver names some chips so
        r["hwmon"][2]["attrs"]["pwm1_enable"] = {"v": "2\n", "w": True}
        self.load(r)
        hw = discover(self.m.root)
        self.assertEqual(hw.board_chips, [])
        self.assertEqual(hw.fans, {})


class AuditFixes(Machine):
    """Found by the end-to-end review of 0.7.1."""
    def test_selftest_on_nuvoton_passes_bios_handback_and_skips_missing_gpu(self):
        self.load(boards.asus_amd())
        d = self.daemon({"cpu_fans": ["nct6798/pwm1"]})
        nct = self.chip_dir("nct6798")
        for i in range(1, 8):
            os.chmod(os.path.join(nct, f"fan{i}_input"), 0o644)
        d.handle({"cmd": "selftest"}, 0)
        for _ in range(400):
            if d.selftest.state != "running":
                break
            for i in range(1, 8):                     # fans follow their pwm
                duty = int(open(os.path.join(nct, f"pwm{i}")).read()) / 2.55
                with open(os.path.join(nct, f"fan{i}_input"), "w") as f:
                    f.write(f"{int(300 + duty * 12) if i in (1, 3, 4) else 0}\n")
            self.ticks(d, 1, dt=2)
        res = {r["name"]: r for r in d.selftest.results}
        back = [r for n, r in res.items() if n.endswith("back to BIOS control")]
        self.assertTrue(back and all(r["ok"] for r in back), back)
        self.assertIsNone(res["Graphics card fan interface"]["ok"], "no card is a skip, not a failure")
        self.assertEqual(d.selftest.failed, 0, [r for r in d.selftest.results if r["ok"] is False])

    def test_rename_and_cpu_fan_reject_unknown_ids(self):
        self.load(boards.asus_amd())
        d = self.daemon({"allowed_uids": [0], "cpu_fans": ["nct6798/pwm1"]})
        self.ticks(d, 1)
        out = d.handle({"cmd": "rename", "id": "does/notexist", "name": "X"}, 0)
        self.assertFalse(out["ok"])
        self.assertIn("no fan or sensor", out["error"])
        out = d.handle({"cmd": "set_cpu_fans", "fans": ["nct6798/pwm22"]}, 0)
        self.assertFalse(out["ok"])
        self.assertEqual(d.cfg["cpu_fans"], ["nct6798/pwm1"], "a typo never clears the marks")
        self.assertTrue(d.handle({"cmd": "set_cpu_fans", "fans": []}, 0)["ok"], "clearing on purpose works")

    def test_renamed_fan_duty_row_follows(self):
        self.load(boards.asus_amd())
        d = self.daemon({"allowed_uids": [0]})
        self.ticks(d, 1)
        d.handle({"cmd": "rename", "id": "nct6798/pwm3", "name": "Front intake"}, 0)
        r = {x["id"]: x["label"] for x in d.status()["readings"]}
        self.assertEqual((r["fan:nct6798/pwm3"], r["duty:nct6798/pwm3"]), ("Front intake", "Front intake duty"))

    def test_second_amd_card_has_its_own_group(self):
        self.load(boards.two_rdna4_pc())
        d = self.daemon()
        self.ticks(d, 1)
        groups = {x["id"]: x["group"] for x in d.readings}
        self.assertNotEqual(groups["t:amdgpu@0000:0b:00.0/temp2"], "other")
        self.assertNotEqual(groups["t:amdgpu@0000:0b:00.0/temp2"], groups["t:amdgpu@0000:0e:00.0/temp2"])


class ReadOnlyBoard(Machine):
    def setUp(self):
        self.load(boards.readonly_board())

    def test_nothing_controllable(self):
        d = self.daemon()
        self.assertEqual([f for f in d.hw.fans], [])
        self.ticks(d, 3)
        self.assertEqual(self.val("nct6683", "pwm1"), "100")
        self.assertIn("nct6683/fan1", d.hw.fan_inputs)
        self.assertIsNone(d.status()["board_chip"])
        hints = d.status()["diagnosis"]
        self.assertTrue(any("nct6683" in h and "nct6687d" in h for h in hints), hints)
        self.assertTrue(any("sensors-detect" in h for h in hints))
        self.assertFalse(any("acpi_enforce_resources=lax" in h for h in hints), "never suggested blindly")


@needs_qt
class UnknownBoardGui(Machine):
    """The app on a board FanPilot doesn't know: CPU-fan banner, marking, renaming."""
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.load(boards.asus_amd())

    def test_mark_cpu_fan_and_rename(self):
        import threading
        import time
        from unittest import mock
        from PySide6.QtWidgets import QInputDialog
        from fanpilot.gui import MainWindow
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
            self.assertIn("Which fan is your CPU fan?", w.banner.text())
            w._mark_cpu_fan("nct6798/pwm2", True)
            self.assertEqual(d.cfg["cpu_fans"], ["nct6798/pwm2"])
            w.refresh()
            self.assertNotIn("Which fan is your CPU fan?", w.banner.text() if w.banner.isVisible() else "")
            with mock.patch.object(QInputDialog, "getText", return_value=("Front intake", True)):
                w.rename("nct6798/pwm3", "Fan 3")
            self.assertEqual(w.fan_list.item(2).text().split("\n")[0], "Front intake")
            with mock.patch.object(QInputDialog, "getText", return_value=("Water", True)):
                w.rename("nct6798/temp1", "nct6798 SYSTIN")
            w.refresh()
            labels = {r["id"]: r["label"] for r in w.status["readings"]}
            self.assertEqual(labels["t:nct6798/temp1"], "Water")
            self.assertEqual(labels["fan:nct6798/pwm3"], "Front intake")
        finally:
            w.quitting = True
            w.close()
            d.stop_event.set()
            t.join(5)


class AsusTufX570Plus(unittest.TestCase):
    """Real report, 2026-11-15: ASUS TUF GAMING X570-PLUS (nct6798, not in the board database) + RX 9070 XT."""
    def test_detection(self):
        from fanpilot import amdgpu
        m = machine_from_report(json.load(open(os.path.join(REPORTS, "asus-tuf-gaming-x570-plus.json"))))
        try:
            hw = discover(m.root)
            self.assertEqual(hw.board_chips, ["nct6798"])
            self.assertIsNone(hw.board)
            self.assertEqual(sorted(f for f in hw.fans if f.startswith("nct")), [f"nct6798/pwm{i}" for i in range(1, 8)])
            for i in (10, 11, 12):                    # PCH_* channels: exactly 0 °C on this AMD board
                self.assertNotIn(f"nct6798/temp{i}", hw.sensors)
            self.assertIn("nct6798/temp13", hw.sensors, "TSI0_TEMP is real")
            self.assertEqual(amdgpu.read_info(hw.fans["amdgpu/fan1"].fan_ctrl_dir).duty_range, (30, 100))
        finally:
            m.cleanup()

    def test_bios_setup_hints(self):
        """What the BIOS does with each header, read from the chip: the user confirmed pwm5 is the chipset fan
        (X570), 4/6/7 are empty, and pwm6's 'full speed everywhere' is the pump header default."""
        m = machine_from_report(json.load(open(os.path.join(REPORTS, "asus-tuf-gaming-x570-plus.json"))))
        try:
            hw = discover(m.root)
            guess = {fid: (f.bios or {}).get("guess") for fid, f in hw.fans.items() if fid.startswith("nct")}
            self.assertEqual(guess, {"nct6798/pwm1": "cpu", "nct6798/pwm2": "cpu", "nct6798/pwm3": "cpu",
                                     "nct6798/pwm4": "cpu", "nct6798/pwm5": "chipset", "nct6798/pwm6": "pump",
                                     "nct6798/pwm7": None})
            b5 = hw.fans["nct6798/pwm5"].bios
            self.assertEqual(b5["source"], "SMBUSMASTER 1")
            self.assertIn("stopped below 55 °C", b5["hint"])
            self.assertEqual(hw.cpu_fans, set(), "a guess never makes a CPU fan: only the user or the database")
            d = Daemon(m.root, os.path.join(m.root, "c.json"), os.path.join(m.root, "run"),
                       os.path.join(m.root, "run", "s.sock"))
            d.tick(1000.0, 1.0)
            self.assertEqual(d.fan_status["nct6798/pwm5"]["bios_guess"], "chipset")
            self.assertTrue(all(d.protect_cpu(f) for f in d.hw.fans if f.startswith("nct")),
                            "guesses change no protection")
        finally:
            m.cleanup()

    def test_sensor_names_and_fixed_voltages(self):
        """Nuvoton inputs get plain names (chip facts, any board) and the fixed-function voltage pins show up
        on a board the database doesn't know; values from this real report, no false alarm."""
        from fanpilot.readings import Readings
        m = machine_from_report(json.load(open(os.path.join(REPORTS, "asus-tuf-gaming-x570-plus.json"))))
        try:
            hw = discover(m.root)
            lab = {sid: s.label for sid, s in hw.sensors.items() if sid.startswith("nct")}
            self.assertEqual(lab["nct6798/temp1"], "Motherboard (SYSTIN)")
            self.assertEqual(lab["nct6798/temp2"], "CPU socket (CPUTIN)")
            self.assertEqual(lab["nct6798/temp3"], "Auxiliary 0 (AUXTIN0)")
            self.assertEqual(lab["nct6798/temp8"], "PECI agent 0 (calibration)")
            self.assertEqual(lab["nct6798/temp9"], "SMBus master 1 (SMBUSMASTER 1)")
            self.assertEqual(lab["nct6798/temp13"], "CPU (AMD TSI 0)")
            self.assertEqual(lab["nct6798/temp14"], "AMD TSI 1 (TSI1_TEMP)")
            self.assertFalse(any(v.startswith("nct6798 ") for v in lab.values()), "no chip-name prefix")
            r = Readings(m.root, hw)
            temps = {sid: s.read() for sid, s in hw.sensors.items()}
            volts = {x["label"]: (x["value"], x["alert"]) for x in r.read(temps, {}, {}) if x["kind"] == "volt"
                     and x["group"] == "board"}
            self.assertEqual(sorted(volts), sorted(["CPU Vcore", "AVSB (+3.3V)", "+3.3V (3VCC)",
                                                    "+3.3V standby (3VSB)", "CMOS battery (VBAT)"]))
            for label, (v, alert) in volts.items():
                self.assertFalse(alert, f"{label} = {v} V: no false alarm on a healthy board")
            self.assertAlmostEqual(volts["+3.3V (3VCC)"][0], 3.3, delta=0.2)
        finally:
            m.cleanup()

    def test_no_guess_on_it87(self):
        """it87's pwmN_auto_channels_temp bitmask differs between driver builds: nothing is guessed."""
        m = machine_from_report(json.load(open(os.path.join(REPORTS, "gigabyte-b550m-aorus-elite.json"))))
        try:
            hw = discover(m.root)
            self.assertTrue(all(getattr(f, "bios", None) is None for f in hw.fans.values()))
        finally:
            m.cleanup()


class Lightweight(Machine):
    """The service must stay light (2026-11-15: measured on a real machine, every NVMe temperature read is a
    command to the drive, 0.6-7 ms each, 5 of them a second)."""
    def setUp(self):
        self.load(boards.asus_amd())
        self.d = self.daemon()
        self.ticks(self.d, 2)

    def test_drive_temperatures_every_10_s(self):
        d = self.d
        nv = next(sid for sid in d.hw.sensors if sid.startswith("nvme"))
        path = d.hw.sensors[nv].path
        os.chmod(path, 0o644)
        first = d.temps[nv]
        with open(path, "w") as f:
            f.write("61000\n")
        cpu = d.hw.default_cpu_sensor()
        os.chmod(os.path.join(self.m.k10, "temp1_input"), 0o644)       # report files are read-only
        self.m.set_temp(self.m.k10, 1, 66)
        self.ticks(d, 3)
        self.assertEqual(d.temps[nv], first, "the drive is not asked again within 10 s")
        self.assertEqual(d.temps[cpu], 66, "CPU (and every register-read sensor) still every tick")
        d._slow_next = 0                                 # 10 s later
        self.ticks(d)
        self.assertEqual(d.temps[nv], 61)

    def test_sensors_view_built_only_when_asked(self):
        d = self.d
        calls = []
        real = d.readings_src.read
        d.readings_src.read = lambda *a: (calls.append(1), real(*a))[1]
        self.ticks(d, 5)
        self.assertEqual(calls, [], "nobody asked: no work")
        st = d.handle({"cmd": "status"}, 0)["status"]
        self.assertTrue(st["readings"])
        d.handle({"cmd": "status"}, 0)
        self.assertEqual(len(calls), 1, "at most once per tick, however many ask")
        self.ticks(d)
        light = d.handle({"cmd": "status", "readings": False}, 0)["status"]
        self.assertEqual((light["readings"], light["groups"]), ([], []))
        self.assertEqual(len(calls), 1, "the light status builds nothing")
        self.assertTrue(light["fans"] and light["sensors"], "fans and temperatures are still there")


class MoreDrivers(Machine):
    def test_ram_and_drive_sensors(self):
        """RAM module sensors (jc42 / spd5118) go to the Memory group; drivetemp gets a plain name; a dead VBAT
        on an unknown Nuvoton board alerts, the board-specific VIN pins stay hidden."""
        from fanpilot.readings import Readings
        rep = boards.asus_amd()
        rep["hwmon"].append(boards.chip("hwmon7", "jc42", "0-0018", "jc42", {"temp1_input": 41250}))
        rep["hwmon"].append(boards.chip("hwmon8", "spd5118", "1-0051", "spd5118", {"temp1_input": 43000}))
        rep["hwmon"].append(boards.chip("hwmon9", "drivetemp", "0:0:0:0", "drivetemp", {"temp1_input": 33000}))
        for h in rep["hwmon"]:
            if h["attrs"]["name"]["v"].strip() == "nct6798":
                h["attrs"]["in8_input"] = {"v": "2200\n", "w": False}         # flat CMOS battery
                h["attrs"]["in3_input"] = {"v": "3310\n", "w": False}
        self.load(rep)
        hw = discover(self.m.root)
        r = Readings(self.m.root, hw)
        out = r.read({sid: s.read() for sid, s in hw.sensors.items()}, {}, {})
        mem = [x["label"] for x in out if x["group"] == "memory" and x["kind"] == "temp"]
        self.assertEqual(mem, ["RAM module 1", "RAM module 1"])
        self.assertIn("Drive 1", [x["label"] for x in out if x["kind"] == "temp"])
        volts = {x["label"]: x["alert"] for x in out if x["kind"] == "volt"}
        self.assertTrue(volts["CMOS battery (VBAT)"], "2.2 V battery: alert")
        self.assertFalse(volts["+3.3V (3VCC)"])
        self.assertFalse(any("VIN" in k for k in volts), "board-specific inputs stay hidden")


class RealReports(unittest.TestCase):
    """Every real report: discovery works, a daemon runs and writes nothing while every fan is Automatic."""
    def test_reports(self):
        files = sorted(glob.glob(os.path.join(REPORTS, "*.json")))
        self.assertTrue(files)
        for path in files:
            with self.subTest(report=os.path.basename(path)):
                m = machine_from_report(json.load(open(path)))
                try:
                    snap = {}
                    for root, _, names in os.walk(os.path.join(m.root, "sys")):
                        for n in names:
                            p = os.path.join(root, n)
                            if not os.path.islink(p):
                                snap[p] = open(p).read()
                    run = os.path.join(m.root, "run")
                    d = Daemon(m.root, os.path.join(m.root, "c.json"), run, os.path.join(run, "s.sock"))
                    for i in range(3):
                        d.tick(1000.0 + i, 1.0)
                    self.assertTrue(d.readings)
                    after = {p: open(p).read() for p in snap}
                    self.assertEqual(snap, after, "all fans on Automatic: nothing may be written")
                finally:
                    m.cleanup()


if __name__ == "__main__":
    unittest.main()
