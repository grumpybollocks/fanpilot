# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate the README screenshots from the real GUI on a simulated machine.

    QT_QPA_PLATFORM=offscreen python3 tests/screenshots.py docs/images
"""
import math
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fanpilot import amdgpu  # noqa: E402
from fanpilot.daemon import Daemon  # noqa: E402
from sim import FakeGpu, FakeMachine, w  # noqa: E402
from test_sensors_view import build_extras  # noqa: E402


def breeze_dark(app):
    p = QPalette()
    c = {QPalette.Window: "#202326", QPalette.WindowText: "#fcfcfc", QPalette.Base: "#141618",
         QPalette.AlternateBase: "#1d1f22", QPalette.Text: "#fcfcfc", QPalette.Button: "#292c30",
         QPalette.ButtonText: "#fcfcfc", QPalette.Highlight: "#3daee9", QPalette.HighlightedText: "#fcfcfc",
         QPalette.Mid: "#4d4d4d", QPalette.ToolTipBase: "#292c30", QPalette.ToolTipText: "#fcfcfc"}
    for role, col in c.items():
        p.setColor(role, QColor(col))
    app.setPalette(p)


def breeze_light(app):
    p = QPalette()
    c = {QPalette.Window: "#eff0f1", QPalette.WindowText: "#232629", QPalette.Base: "#fcfcfc",
         QPalette.AlternateBase: "#eff0f1", QPalette.Text: "#232629", QPalette.Button: "#fcfcfc",
         QPalette.ButtonText: "#232629", QPalette.Highlight: "#3daee9", QPalette.HighlightedText: "#fcfcfc",
         QPalette.Mid: "#bdc3c7", QPalette.ToolTipBase: "#f7f7f7", QPalette.ToolTipText: "#232629"}
    for role, col in c.items():
        p.setColor(role, QColor(col))
    app.setPalette(p)


def main(out):
    os.makedirs(out, exist_ok=True)
    app = QApplication(sys.argv[:1] + ["-style", "breeze"])
    m = FakeMachine()
    build_extras(m)            # voltages, GPU power/clocks, CPU/RAM load, NVMe ... for the Sensors view
    gpu = FakeGpu(m.fc)
    real = amdgpu._write
    amdgpu._write = lambda p, v: gpu.write(p, v) if p.startswith(m.fc) else real(p, v)
    cfg = os.path.join(m.root, "etc/config.json")
    d = Daemon(m.root, cfg, os.path.join(m.root, "run"), os.path.join(m.root, "run/s.sock"))
    d.cfg["allowed_uids"] = [os.getuid()]
    d.handle({"cmd": "set_fan", "fan": "it8688/pwm2", "settings": {"mode": "fixed", "fixed": 45}}, 0)
    d.handle({"cmd": "save_profile", "name": "Night"}, 0)
    d.handle({"cmd": "apply_profile", "name": "Balanced"}, 0)
    d.cfg["calibration"]["it8688/pwm2"] = {"max_rpm": 1210, "stop_duty": 15, "start_duty": 25,
                                           "min_safe_duty": 30, "table": []}
    gh = os.path.join(m.root, "sys/class/hwmon/hwmon2")
    # run a real self-test on the simulated machine (fast-forwarded) so the Settings shot shows real output
    w(os.path.join(m.root, "etc/systemd/system/fanpilotd.service"),
      "ExecStopPost=/usr/local/bin/fanpilotd --restore\nWatchdogSec=15\n")
    d.read_temps()
    d.handle({"cmd": "selftest"}, 0)
    now = 1000.0
    for _ in range(200):
        for i in (1, 2, 3):
            duty = m.pwm(i) * 100 / 255 if m.enable(i) in (0, 1) else 100
            w(f"{m.board}/fan{i}_input", f"{int(300 + duty * 9)}\n")
        pts = gpu.committed
        w(os.path.join(gh, "fan1_input"), f"{(pts[0][1] * 45) if pts else 0}\n")
        now += 1
        d.tick(now, 1)
        if d.selftest.state != "running":
            break
    d.handle({"cmd": "apply_profile", "name": "Balanced"}, 0)
    stop = threading.Event()

    def physics():
        t0 = time.time()
        while not stop.is_set():
            t = time.time() - t0 + 200
            m.cpu(54 + 9 * math.sin(t / 25) + 1.5 * math.sin(t * 1.7))
            m.set_temp(gh, 2, 61 + 7 * math.sin(t / 33))
            m.set_temp(gh, 1, 50 + 5 * math.sin(t / 33))
            for i in (1, 2, 3):
                duty = m.pwm(i) * 100 / 255 if m.enable(i) == 1 else 100
                w(f"{m.board}/fan{i}_input", f"{int(300 + duty * 9)}\n")
            pts = gpu.committed
            w(os.path.join(gh, "fan1_input"), f"{int((pts[2][1] if pts else 0) * 38)}\n")
            w(os.path.join(gh, "pwm1"), f"{int((pts[2][1] if pts else 0) * 2.55)}\n")
            ticks = int(t * 20)       # CPU counters move, so "CPU usage" has a value
            w(os.path.join(m.root, "proc/stat"), f"cpu  {ticks * 3} 0 {ticks} {ticks * 16} 0 0 0 0 0 0\n")
            time.sleep(0.05)

    threading.Thread(target=physics, daemon=True).start()
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(1.5)
    from fanpilot.gui import MainWindow
    shots = [("dark", "fans-curve", 0, 0), ("light", "fans-curve-light", 0, 0), ("dark", "gpu", 0, 3),
             ("dark", "fixed", 0, 1), ("dark", "sensors", 1, 0), ("light", "sensors-light", 1, 0), ("dark", "settings", 3, 0)]
    for theme, name, tab, row in shots:
        if theme == "dark":
            breeze_dark(app)
            QIcon.setThemeName("breeze-dark")
        else:
            breeze_light(app)
            QIcon.setThemeName("breeze")
        win = MainWindow(d.socket_path)
        win.resize(1060, 740)
        win.show()
        win.fan_list.setCurrentRow(row)
        win.tabs.setCurrentIndex(tab)
        for _ in range(12):
            app.processEvents()
            win.refresh()
            time.sleep(0.12)
        win.grab().save(os.path.join(out, f"{name}.png"))
        win.quitting = True
        win.close()
        win.deleteLater()
        app.processEvents()
        print("saved", name)
    stop.set()
    d.stop_event.set()
    time.sleep(1.2)
    m.cleanup()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/images")
