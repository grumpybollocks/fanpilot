# SPDX-License-Identifier: GPL-3.0-or-later
"""Sensor snapshot (/run/fanpilot/sensors.tsv) for other programs such as the G510 LCD."""
import os
import stat
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fanpilot import export  # noqa: E402
from test_daemon import Base  # noqa: E402


class ExportTests(Base):
    @property
    def path(self):
        return os.path.join(self.run_dir, "sensors.tsv")

    def test_written_every_tick_with_all_rows(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 40}}})
        self.m.cpu(55.5)
        self.ticks(d, 2)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        rows = {r["key"]: r for r in export.read(self.path)}
        self.assertEqual(rows["t.k10temp/temp1"]["value"], 55.5)
        self.assertEqual(rows["t.k10temp/temp1"]["short"], "CPU")
        self.assertEqual(rows["t.k10temp/temp1"]["label"], "CPU (Tctl)")
        self.assertEqual(rows["t.it8688/temp5"]["short"], "VRM")
        self.assertEqual(rows["pct.it8688/pwm2"]["value"], 40)
        self.assertEqual(rows["pct.it8688/pwm2"]["short"], "SY1%")
        self.assertIn("rpm.it8688/pwm2", rows)
        self.assertEqual(rows["rpm.amdgpu/fan1"]["short"], "GPUF")
        self.assertIsNone(rows["pct.it8688/pwm1"]["value"])   # BIOS-controlled: no honest duty
        for r in rows.values():
            self.assertLessEqual(len(r["short"]), 4)
            self.assertNotIn(" ", r["key"])
            self.assertIn(r["kind"], ("temp", "rpm", "pct"))

    def test_unreadable_value_is_empty(self):
        d = self.daemon()
        with open(os.path.join(self.m.k10, "temp1_input"), "w") as f:
            f.write("garbage\n")              # present but unreadable (a vanished file is rescanned away)
        self.ticks(d)
        rows = {r["key"]: r for r in export.read(self.path)}
        self.assertIsNone(rows["t.k10temp/temp1"]["value"])

    def test_stale_and_missing(self):
        self.assertEqual(export.read(self.path), [])
        os.makedirs(self.run_dir, exist_ok=True)
        with open(self.path, "w") as f:
            f.write(f"# fanpilot-sensors 1 {int(time.time()) - 60}\nt.x\ttemp\tX\tX\t40.0\n")
        self.assertEqual(export.read(self.path), [])

    def test_removed_on_clean_stop(self):
        d = self.daemon()
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        for _ in range(100):
            if os.path.exists(self.path):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.path))
        d.stop_event.set()
        t.join(5)
        self.assertFalse(os.path.exists(self.path))

    def test_labels_cannot_break_the_format(self):
        class S:
            label = "weird\tname\nwith\r\x00breaks"

        class HW:
            sensors = {"odd/temp1": S()}
            fans = {}
        r = export.rows(HW(), {"odd/temp1": 30.0}, {})[0]
        self.assertNotIn("\t", r[3])
        self.assertNotIn("\n", r[3])
        self.assertNotIn("\r", r[3])
        self.assertNotIn("\x00", r[3])
        self.assertEqual(r[2], "WEIR")


if __name__ == "__main__":
    unittest.main()
