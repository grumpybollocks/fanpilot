# SPDX-License-Identifier: GPL-3.0-or-later
"""HWiNFO-style Sensors view: readings collected by the service + the GUI table."""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qt import needs_qt  # noqa: E402

from fanpilot.readings import Readings  # noqa: E402
from fanpilot.hw import discover  # noqa: E402
from sim import w  # noqa: E402
from test_daemon import Base  # noqa: E402

CRIT = {"cpu_temp": 90, "gpu_temp": 100, "cpu_sensor": "k10temp/temp1", "gpu_sensor": "amdgpu/temp2"}


def build_extras(m, rail12_mv=2040):
    """Everything the Sensors view reads beyond the fan-control basics."""
    r = m.root
    for n, mv in enumerate([1272, 2028, rail12_mv, 2028, 1008, 924, 1380, 3288, 3000]):
        w(f"{m.board}/in{n}_input", f"{mv}\n")
    gh = os.path.join(r, "sys/class/hwmon/hwmon2")
    w(f"{gh}/power1_average", "14000000\n")
    w(f"{gh}/in0_input", "780\n")
    w(f"{gh}/freq1_input", "2400000000\n")
    w(f"{gh}/freq2_input", "96000000\n")
    w(f"{gh}/temp2_crit", "110000\n")
    w(f"{m.gpu_dev}/vendor", "0x1002\n")
    w(f"{m.gpu_dev}/device", "0x7590\n")
    w(f"{m.gpu_dev}/gpu_busy_percent", "32\n")
    w(f"{m.gpu_dev}/mem_info_vram_used", f"{2 * 2 ** 30}\n")
    w(f"{m.gpu_dev}/mem_info_vram_total", "17095983104\n")
    w(f"{r}/usr/share/hwdata/pci.ids", "1002  Advanced Micro Devices, Inc. [AMD/ATI]\n"
                                       "\t7590  Navi 44 [Radeon RX 9050 / 9060 XT]\n")
    w(f"{r}/sys/class/dmi/id/board_vendor", "Gigabyte Technology Co., Ltd.\n")
    w(f"{r}/sys/class/dmi/id/board_name", "B550M AORUS ELITE\n")
    w(f"{r}/proc/cpuinfo", "processor\t: 0\nmodel name\t: AMD Ryzen 7 5800X3D 8-Core Processor\n")
    w(f"{r}/proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
    w(f"{r}/proc/meminfo", "MemTotal:       33554432 kB\nMemAvailable:   16777216 kB\n")
    for i, f in enumerate((3600000, 4400000)):
        w(f"{r}/sys/devices/system/cpu/cpufreq/policy{i}/scaling_cur_freq", f"{f}\n")
    nv = os.path.join(r, "sys/class/hwmon/hwmon1")
    ndev = os.path.join(r, "sys/devices/nvme0")
    w(f"{nv}/name", "nvme\n")
    w(f"{nv}/temp1_input", "75000\n")
    w(f"{nv}/temp1_label", "Composite\n")
    w(f"{nv}/temp1_crit", "70000\n")
    w(f"{ndev}/model", "KINGSTON SA2000M8500G   \n")
    if not os.path.exists(f"{nv}/device"):
        os.symlink(ndev, f"{nv}/device")


class ReadingsTests(Base):
    def rows(self, readings):
        return {r["id"]: r for r in readings}

    def test_groups_values_and_alerts(self):
        build_extras(self.m)
        hw = discover(self.m.root)
        rd = Readings(self.m.root, hw)
        temps = {k: s.read() for k, s in hw.sensors.items()}
        fans = {"it8688/pwm2": {"rpm": 900, "duty": 40.0, "flags": []},
                "it8688/pwm3": {"rpm": 0, "duty": 60.0, "flags": ["stalled"]}}
        rd.read(temps, fans, CRIT)
        w(f"{self.m.root}/proc/stat", "cpu  150 0 150 900 0 0 0 0 0 0\n")   # +100 busy, +100 idle -> 50 %
        out = rd.read(temps, fans, CRIT)
        r = self.rows(out)
        titles = {g["id"]: g["title"] for g in rd.group_list(out)}
        self.assertEqual(titles["cpu"], "AMD Ryzen 7 5800X3D")
        self.assertEqual(titles["board"], "Gigabyte B550M AORUS ELITE (ITE IT8688E)")
        self.assertEqual(titles["gpu"], "AMD Radeon RX 9050 / 9060 XT (Navi 44), 16 GB")
        self.assertEqual(titles["drive:nvme"], "KINGSTON SA2000M8500G")
        self.assertAlmostEqual(r["v:in2"]["value"], 12.24, places=2)        # +12V x6
        self.assertAlmostEqual(r["v:in1"]["value"], 3.344, places=3)        # +3.3V x1.649
        self.assertFalse(r["v:in2"]["alert"])
        self.assertAlmostEqual(r["cpu:usage"]["value"], 50.0, places=1)
        self.assertEqual((r["cpu:clock_avg"]["value"], r["cpu:clock_max"]["value"]), (4000.0, 4400.0))
        self.assertAlmostEqual(r["mem:pct"]["value"], 50.0)
        self.assertAlmostEqual(r["mem:used"]["value"], 16.0)
        self.assertEqual(r["gpu:power"]["value"], 14.0)
        self.assertEqual(r["gpu:freq1"]["value"], 2400.0)
        self.assertEqual(r["gpu:load"]["value"], 32.0)
        self.assertAlmostEqual(r["gpu:vram"]["value"], 2.0)
        self.assertTrue(r["t:nvme/temp1"]["alert"])                          # 75 >= its crit 70
        self.assertTrue(r["fan:it8688/pwm3"]["alert"])                       # stalled fan
        self.assertFalse(r["fan:it8688/pwm2"]["alert"])
        self.assertFalse(any(x["alert"] for x in out if x["id"].startswith("t:it8688")),
                         "it87 _max placeholders are never used as limits")
        self.assertEqual(r["duty:it8688/pwm2"]["value"], 40.0)

    def test_rail_out_of_range_and_hot_gpu(self):
        build_extras(self.m, rail12_mv=1700)                                 # 10.2 V: >10 % low
        self.m.set_temp(os.path.join(self.m.root, "sys/class/hwmon/hwmon2"), 2, 101)
        hw = discover(self.m.root)
        rd = Readings(self.m.root, hw)
        r = self.rows(rd.read({k: s.read() for k, s in hw.sensors.items()}, {}, CRIT))
        self.assertTrue(r["v:in2"]["alert"])
        self.assertTrue(r["t:amdgpu/temp2"]["alert"])                        # >= emergency limit 100

    def test_cpu_usage_ignores_guest_and_backwards_iowait(self):
        build_extras(self.m)
        hw = discover(self.m.root)
        rd = Readings(self.m.root, hw)
        stat = f"{self.m.root}/proc/stat"
        w(stat, "cpu  100 0 0 900 0 0 0 0 0 0\n")
        rd.read({}, {}, CRIT)
        w(stat, "cpu  200 0 0 1800 0 0 0 0 100 0\n")       # 100 guest ticks are inside user's 100
        self.assertAlmostEqual(self.rows(rd.read({}, {}, CRIT))["cpu:usage"]["value"], 10.0, places=3)
        w(stat, "cpu  200 0 0 1800 0 0 0 0 100 0\n")
        w(stat, "cpu  210 0 0 1800 50 0 0 0 100 0\n")
        rd.read({}, {}, CRIT)
        w(stat, "cpu  300 0 0 1810 0 0 0 0 100 0\n")       # iowait went 50 -> 0 (old code: 180 %)
        v = self.rows(rd.read({}, {}, CRIT))["cpu:usage"]["value"]
        self.assertTrue(0.0 <= v <= 100.0, v)

    def test_battery_high_is_fine_zero_watts_kept_nvme_labels(self):
        build_extras(self.m)
        w(f"{self.m.board}/in8_input", "3400\n")                           # fresh cell: 3.4 V
        gh = os.path.join(self.m.root, "sys/class/hwmon/hwmon2")
        w(f"{gh}/power1_average", "0\n")
        nv = os.path.join(self.m.root, "sys/class/hwmon/hwmon1")
        w(f"{nv}/temp2_input", "40000\n")
        w(f"{nv}/temp2_label", "Sensor 1\n")
        hw = discover(self.m.root)
        rd = Readings(self.m.root, hw)
        r = self.rows(rd.read({k: s.read() for k, s in hw.sensors.items()}, {}, CRIT))
        self.assertFalse(r["v:in8"]["alert"])
        self.assertEqual(r["gpu:power"]["value"], 0.0)
        self.assertEqual((r["t:nvme/temp1"]["label"], r["t:nvme/temp2"]["label"]), ("Temperature", "Sensor 1"))
        w(f"{self.m.board}/in8_input", "2500\n")                           # flat cell
        self.assertTrue(self.rows(rd.read({}, {}, CRIT))["v:in8"]["alert"])

    def test_daemon_clears_readings_when_reading_fails(self):
        build_extras(self.m)
        d = self.daemon()
        self.ticks(d, 2)
        self.assertTrue(d.status()["readings"])
        d.readings_src.read = lambda *a: 1 / 0
        self.ticks(d, 1)
        self.assertEqual(d.status()["readings"], [])

    def test_cli_sensors_table(self):
        import contextlib
        import io
        from fanpilot import ctl
        build_extras(self.m)
        d = self.daemon()
        self.ticks(d, 2)
        srv = d.serve()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(ctl.main(["--socket", d.socket_path, "sensors"]), 0)
        finally:
            srv.shutdown()
            srv.server_close()
        out = buf.getvalue()
        self.assertIn("Gigabyte B550M AORUS ELITE (ITE IT8688E)", out)
        self.assertRegex(out, r"\+12V\s+12\.240 V")
        self.assertRegex(out, r"Temperature\s+75\.0 °C  !")      # NVMe past its own limit

    def test_missing_files_give_no_value(self):
        hw = discover(self.m.root)                                           # basic sim: no extras
        rd = Readings(self.m.root, hw)
        r = self.rows(rd.read({}, {}, CRIT))
        self.assertIsNone(r["cpu:usage"]["value"])
        self.assertIsNone(r["v:in2"]["value"])
        self.assertEqual(rd.groups["gpu"], "GPU")

    def test_daemon_status_carries_readings(self):
        build_extras(self.m)
        d = self.daemon()
        self.ticks(d, 2)
        st = d.status()
        ids = {r["id"] for r in st["readings"]}
        self.assertIn("v:in2", ids)
        self.assertIn("gpu:power", ids)
        self.assertEqual([g["id"] for g in st["groups"]][:3], ["cpu", "board", "gpu"])


@needs_qt
class SensorsViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def reading(self, v, alert=False):
        return [{"id": "t:k10temp/temp1", "group": "cpu", "label": "CPU (Tctl)", "kind": "temp",
                 "value": v, "unit": "°C", "decimals": 1, "alert": alert}]

    def test_current_min_max_average_reset_and_alert(self):
        from fanpilot.gui import SensorsView
        view = SensorsView()
        groups = [{"id": "cpu", "title": "AMD Ryzen 7 5800X3D"}]
        for v in (40.0, 60.0, 50.0):
            view.update_readings(self.reading(v), groups)
        it = view.items["t:k10temp/temp1"]
        self.assertEqual([it.text(c) for c in range(5)],
                         ["CPU (Tctl)", "50.0 °C", "40.0 °C", "60.0 °C", "50.0 °C"])
        self.assertEqual(view.groups["group:cpu"].text(0), "AMD Ryzen 7 5800X3D")
        self.assertTrue(view.groups["group:cpu"].font(0).bold())
        view.reset()
        self.assertEqual([it.text(c) for c in range(1, 5)], ["50.0 °C"] * 4)
        view.update_readings(self.reading(95.0, alert=True), groups)
        self.assertEqual(it.foreground(1).color().name(), "#da4453")
        self.assertTrue(it.font(1).bold())
        view.update_readings(self.reading(None), groups)
        self.assertEqual(it.text(1), "—")
        self.assertEqual(it.text(3), "95.0 °C")                              # max kept

    def test_reading_that_disappears_shows_dash(self):
        from fanpilot.gui import SensorsView
        view = SensorsView()
        g = [{"id": "cpu", "title": "CPU"}]
        view.update_readings(self.reading(42.0, alert=True), g)
        view.update_readings([], g)
        it = view.items["t:k10temp/temp1"]
        self.assertEqual((it.text(1), it.text(3)), ("—", "42.0 °C"))
        self.assertFalse(it.font(1).bold())

    def test_current_never_outside_min_max(self):
        from fanpilot.gui import SensorsView
        view = SensorsView()
        g = [{"id": "cpu", "title": "CPU"}]
        view.update_readings(self.reading(62.0), g)
        view.update_readings(self.reading(61.0), g, count=False)     # quick refresh after a button press
        it = view.items["t:k10temp/temp1"]
        self.assertEqual([it.text(c) for c in range(1, 5)], ["61.0 °C", "61.0 °C", "62.0 °C", "62.0 °C"])

    def test_rows_for_copy_and_columns(self):
        from fanpilot.gui import SensorsView
        view = SensorsView()
        view.update_readings(self.reading(42.0), [{"id": "cpu", "title": "CPU X"}])
        rows = view.rows()
        self.assertEqual(rows[0], ["Device", "Sensor", "Current", "Minimum", "Maximum", "Average"])
        self.assertEqual(rows[1][:3], ["CPU X", "CPU (Tctl)", "42.0 °C"])
        view._set_column(4, False)
        self.assertTrue(view.tree.isColumnHidden(4))
        self.assertTrue(SensorsView().tree.isColumnHidden(4), "hidden column remembered")
        view._set_column(4, True)

    def test_user_alarm_fires_once_and_rearms(self):
        from fanpilot.gui import SensorsView
        view = SensorsView()
        g = [{"id": "cpu", "title": "CPU"}]
        got = []
        view.alarm.connect(got.append)
        view.update_readings(self.reading(50.0), g)
        view.set_alarm("t:k10temp/temp1", "above", 70)
        for v in (71.0, 75.0, 69.5):              # 69.5: still within the 2 % margin -> no re-arm
            view.update_readings(self.reading(v), g)
        self.assertEqual(len(got), 1, got)
        self.assertIn("above your alarm at 70", got[0])
        it = view.items["t:k10temp/temp1"]
        self.assertEqual(it.foreground(1).color().name(), "#da4453")
        view.update_readings(self.reading(60.0), g)   # back to normal
        self.assertNotEqual(it.foreground(1).color().name(), "#da4453")
        self.assertEqual(it.foreground(3).color().name(), "#da4453", "Maximum remembers it went past")
        view.update_readings(self.reading(72.0), g)
        self.assertEqual(len(got), 2)
        self.assertEqual(SensorsView().alarms, {"t:k10temp/temp1": {"above": 70.0}}, "alarms are remembered")
        view.set_alarm("t:k10temp/temp1", None, None)
        self.assertEqual(SensorsView().alarms, {})

    def test_below_alarm_and_minimum_red(self):
        from fanpilot.gui import SensorsView
        view = SensorsView()
        g = [{"id": "cpu", "title": "CPU"}]
        got = []
        view.alarm.connect(got.append)
        view.set_alarm("t:k10temp/temp1", "below", 10)
        view.update_readings(self.reading(20.0), g)
        view.update_readings(self.reading(5.0), g)
        self.assertEqual(len(got), 1)
        it = view.items["t:k10temp/temp1"]
        self.assertEqual(it.foreground(2).color().name(), "#da4453")
        self.assertNotEqual(it.foreground(3).color().name(), "#da4453")
        view.set_alarm("t:k10temp/temp1", None, None)

    def test_csv_logging(self):
        import csv
        import tempfile
        from fanpilot.gui import SensorsView
        view = SensorsView()
        g = [{"id": "cpu", "title": "CPU X"}]
        view.update_readings(self.reading(40.0), g)
        path = os.path.join(tempfile.mkdtemp(), "log.csv")
        view.start_logging(path)
        view.update_readings(self.reading(41.0), g)
        view.update_readings(self.reading(None), g)
        view.update_readings(self.reading(43.0), g, count=False)   # not a new sample: not logged
        view.stop_logging()
        rows = list(csv.reader(open(path)))
        self.assertEqual(rows[0], ["Time", "CPU X / CPU (Tctl) [°C]"])
        self.assertEqual([r[1] for r in rows[1:]], ["41.0", ""])

    def test_tray_value_icon(self):
        from fanpilot.gui import SensorsView, value_icon
        from PySide6.QtGui import QColor
        self.assertFalse(value_icon("52", QColor("#3daee9")).isNull())
        view = SensorsView()
        view.set_tray("t:k10temp/temp1")
        self.assertEqual(SensorsView().tray_id, "t:k10temp/temp1")
        view.set_tray("")

    def test_settings_go_to_scratch_not_home(self):
        from PySide6.QtCore import QSettings
        path = QSettings("FanPilot", "FanPilot").fileName()
        self.assertFalse(path.startswith(os.path.expanduser("~/.config")), path)


if __name__ == "__main__":
    unittest.main()
