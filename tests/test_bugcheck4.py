# SPDX-License-Identifier: GPL-3.0-or-later
"""Fourth bug-check (2026-11-15): driver reload keeps the BIOS mode, NVIDIA polling only with a card,
no endless rescans with NVIDIA, the OverDrive hint (only for cards with a fan), calibration of an
empty header, a non-text rename."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boards  # noqa: E402
from fanpilot import daemon as dmod  # noqa: E402
from fanpilot import nvidia  # noqa: E402
from fanpilot.daemon import Calibration, Daemon  # noqa: E402
from fanpilot.hw import diagnose, discover  # noqa: E402
from sim import machine_from_report  # noqa: E402


class FakeSmi:
    binary = "nvidia-smi"

    def __init__(self, data, updated=1.0):
        self.data, self.updated = data, updated

    def snapshot(self):
        return dict(self.data)

    def pending(self, root="/"):
        return False


class Reports(unittest.TestCase):
    def mk(self, report, cfg=None):
        m = machine_from_report(report)
        self.addCleanup(m.cleanup)
        cp = os.path.join(m.root, "etc/fanpilot/config.json")
        rd = os.path.join(m.root, "run/fanpilot")
        if cfg is not None:
            os.makedirs(os.path.dirname(cp), exist_ok=True)
            with open(cp, "w") as f:
                json.dump(cfg, f)
        return m, Daemon(m.root, cp, rd, os.path.join(rd, "s.sock"))

    def test_driver_reload_keeps_the_bios_mode(self):
        m, d = self.mk(boards.asus_amd(), {"fans": {"nct6798/pwm3": {"mode": "fixed", "fixed": 60}},
                                           "cpu_fans": ["nct6798/pwm1"]})
        now = 1000.0
        for _ in range(2):
            now += 1
            d.tick(now, 1)
        hd = d.hw.chips["nct6798"]
        self.assertEqual(d.restore["pwm_enable"][os.path.join(hd, "pwm3_enable")]["orig"], 5)
        gone = os.path.join(m.root, "gone")
        os.rename(hd, gone)                               # driver unloaded: a rescan runs in the gap
        for _ in range(2):
            now += 1
            d.next_rescan = 0
            d.tick(now, 1)
        new = os.path.join(os.path.dirname(hd), "hwmon9")
        os.rename(gone, new)                              # reloaded; the chip kept our manual mode (1)
        for _ in range(2):
            now += 1
            d.next_rescan = 0
            d.tick(now, 1)
        self.assertEqual(d.restore["pwm_enable"][os.path.join(new, "pwm3_enable")]["orig"], 5)
        d.stop_event.set()
        self.assertEqual(dmod.restore_from_file(d.restore_file), [])
        with open(os.path.join(new, "pwm3_enable")) as f:
            self.assertEqual(f.read().strip(), "5", "handed back to Smart Fan IV, as the BIOS had it")

    def _reload(self, d, m, now, between=None):
        hd = d.hw.chips["nct6798"]
        gone = os.path.join(m.root, "gone")
        os.rename(hd, gone)
        for _ in range(2):
            now += 1
            d.next_rescan = 0
            d.tick(now, 1)
        if between:
            d = between(d) or d
        new = os.path.join(os.path.dirname(hd), "hwmon9")
        os.rename(gone, new)
        for _ in range(3):
            now += 1
            d.next_rescan = 0
            d.tick(now, 1)
        return d, new

    def test_fan_set_to_auto_during_a_reload_gets_the_bios_mode_back(self):
        m, d = self.mk(boards.asus_amd(), {"fans": {"nct6798/pwm3": {"mode": "fixed", "fixed": 30}},
                                           "cpu_fans": ["nct6798/pwm1"]})
        now = 1000.0
        for _ in range(2):
            now += 1
            d.tick(now, 1)

        def to_auto(d):
            d.cfg["fans"]["nct6798/pwm3"] = {"mode": "auto"}
        d, new = self._reload(d, m, now, to_auto)
        with open(os.path.join(new, "pwm3_enable")) as f:
            self.assertEqual(f.read().strip(), "5", "not left in FanPilot's manual mode")

    def test_service_restart_during_a_reload_keeps_the_bios_mode(self):
        cfg = {"fans": {"nct6798/pwm3": {"mode": "fixed", "fixed": 30}}, "cpu_fans": ["nct6798/pwm1"]}
        m, d = self.mk(boards.asus_amd(), cfg)
        now = 1000.0
        for _ in range(2):
            now += 1
            d.tick(now, 1)

        def restart(d):
            d.stop_event.set()
            self.assertEqual(dmod.restore_from_file(d.restore_file), [])
            return Daemon(m.root, d.config_path, d.run_dir, d.socket_path)
        d, new = self._reload(d, m, now, restart)
        self.assertEqual(d.restore["pwm_enable"][os.path.join(new, "pwm3_enable")]["orig"], 5)

    def test_nvidia_sensor_does_not_cause_endless_rescans(self):
        m, d = self.mk(boards.asus_amd())
        d.nvidia = FakeSmi({"0000:01:00.0": {"name": "RTX", "temp": 47.0, "fan": 35.0}})
        now = 1000.0
        for _ in range(3):
            now += 1
            d.next_rescan = 0
            d.tick(now, 1)
        self.assertTrue(any(not s.path for s in d.hw.sensors.values()), "an NVIDIA sensor is there")
        self.assertFalse(d._missing_hardware())

    def test_nvidia_smi_without_a_card_does_not_block_the_gpu_limit(self):
        m = machine_from_report(boards.asus_amd())
        self.addCleanup(m.cleanup)
        smi = nvidia.NvidiaSmi.__new__(nvidia.NvidiaSmi)  # no polling thread in a test
        smi.binary, smi.updated, smi.started = "nvidia-smi", 0.0, time.monotonic()
        self.assertEqual(nvidia.NvidiaSmi.cards(m.root), [])
        self.assertFalse(smi.pending(m.root), "installed tool, no card: nothing to wait for")
        smi.started -= 31
        self.assertFalse(smi.pending("/"), "never answered in 30 s: stop waiting")

    def test_overdrive_hint_only_for_a_card_with_a_fan(self):
        r = boards.asus_amd()                             # APU graphics: GC 11, no fan
        r["hwmon"].append(boards.chip("hwmon6", "amdgpu", "0000:0e:00.0", "amdgpu",
                                      {"temp1_input": 45000, "temp1_label": "edge"}))
        r["drm"] = [boards.amd_card("0000:0e:00.0", "card0", 11)]
        m = machine_from_report(r)
        self.addCleanup(m.cleanup)
        self.assertEqual(discover(m.root).gpu_hints, [])

        r = boards.asus_amd()                             # RX 7000 without OverDrive: has a fan
        r["hwmon"].append(boards.chip("hwmon6", "amdgpu", "0000:0e:00.0", "amdgpu",
                                      {"temp1_input": 45000, "temp1_label": "edge", "fan1_input": 900,
                                       "pwm1": 100, "pwm1_enable": 2}))
        r["drm"] = [boards.amd_card("0000:0e:00.0", "card0", 11)]
        m2 = machine_from_report(r)
        self.addCleanup(m2.cleanup)
        hw = discover(m2.root)
        self.assertEqual(len(hw.gpu_hints), 1)
        self.assertIn("OverDrive", diagnose(hw, m2.root)[-1], "shown, after any board hints")

    def test_rename_with_a_non_text_name_is_an_error_reply(self):
        m, d = self.mk(boards.asus_amd())
        d.cfg["allowed_uids"] = [os.getuid()]
        r = d.handle({"cmd": "rename", "id": "nct6798/pwm3", "name": 5}, os.getuid())
        self.assertFalse(r["ok"])


class Calibrating(unittest.TestCase):
    def test_empty_header_is_not_calibrated(self):
        class Fan:
            id = "it8688/pwm3"
            p = 255

            def set_pwm(self, v):
                self.p = v

            def rpm(self):
                return 0                                  # nothing connected

        cal = Calibration(Fan(), False)
        now = 0.0
        while cal.state == "running":
            now += 10
            cal.tick(now)
        self.assertEqual(cal.state, "aborted")
        self.assertIsNone(cal.result)
        self.assertIn("No speed reading", cal.message)

    def test_lowest_speed_never_above_100(self):
        class Fan:
            id = "it8688/pwm2"
            p = 255

            def set_pwm(self, v):
                self.p = v

            def rpm(self):
                return 900 if self.p >= 250 else 0        # stops at 95 %, restarts only at 100 %

        cal = Calibration(Fan(), False)
        now = 0.0
        while cal.state == "running":
            now += 10
            cal.tick(now)
        self.assertEqual(cal.result["min_safe_duty"], 100)


if __name__ == "__main__":
    unittest.main()
