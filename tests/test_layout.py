# SPDX-License-Identifier: GPL-3.0-or-later
"""Window sizing (2026-11-15): the window could not be made shorter than ~850 px, and opening
'Fine tuning' grew it to ~980 px (almost a whole 1080p screen). Pages now scroll instead."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qt import needs_qt  # noqa: E402

try:
    from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
    from PySide6.QtGui import QWheelEvent  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402
except ImportError:  # pragma: no cover - skipped below
    QApplication = None

from test_daemon import Base  # noqa: E402
import test_gui  # noqa: E402


@needs_qt
class Layout(Base):
    setUpClass = classmethod(test_gui.GuiTests.setUpClass.__func__)
    start = test_gui.GuiTests.start

    def tearDown(self):
        if hasattr(self, "w"):
            self.w.quitting = True
            self.w.close()
            self.d.stop_event.set()
            self.t.join(5)
        super().tearDown()

    def settle(self):
        for _ in range(10):
            QApplication.processEvents()
            time.sleep(0.02)

    def test_window_can_be_small_on_every_tab(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.show()
        self.settle()
        for t in range(w.tabs.count()):
            w.tabs.setCurrentIndex(t)
            self.settle()
            self.assertLess(w.minimumSizeHint().height(), 500, w.tabs.tabText(t))
            self.assertLess(w.minimumSizeHint().width(), 900, w.tabs.tabText(t))

    def test_fine_tuning_does_not_grow_the_window(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.resize(1000, 700)
        w.show()
        w.tabs.setCurrentIndex(0)
        w.fan_list.setCurrentRow(0)
        self.settle()
        before = w.size()
        w.editor.adv.setChecked(True)
        self.settle()
        self.assertEqual(w.size(), before)
        self.assertLess(w.minimumSizeHint().height(), 500)

    def test_wheel_scrolls_the_page_not_the_values(self):
        self.start({"allowed_uids": [os.getuid()]})
        w = self.w
        w.resize(1000, 560)
        w.show()
        w.tabs.setCurrentIndex(0)
        w.fan_list.setCurrentRow(0)
        self.settle()
        ed = w.editor
        ed.rb_curve.setChecked(True)
        self.settle()

        def wheel(target):
            p = QPointF(target.width() / 2, target.height() / 2)
            QApplication.sendEvent(target, QWheelEvent(p, target.mapToGlobal(p), QPoint(0, 0), QPoint(0, -120),
                                                       Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
            self.settle()
        before = ed.preset.currentIndex()
        wheel(ed.preset)
        self.assertEqual(ed.preset.currentIndex(), before, "passing over the preset box changed nothing")
        ed.rb_fixed.setChecked(True)
        self.settle()
        v = ed.spin.value()
        wheel(ed.slider)
        self.assertEqual(ed.spin.value(), v)
        ed.slider.setFocus()
        self.settle()
        wheel(ed.slider)
        self.assertNotEqual(ed.spin.value(), v, "after a click (focus) the wheel works as usual")

    def test_gpu_hides_board_only_fine_tuning_labels(self):
        self.start({"allowed_uids": [os.getuid()]})
        ed = self.w.editor
        ed.load("amdgpu/fan1", {"mode": "curve"}, True)
        self.assertTrue(all(lab.isHidden() for lab in ed._adv_labels.values()))
        ed.load("it8688/pwm2", {"mode": "curve"}, False)
        self.assertFalse(any(lab.isHidden() for lab in ed._adv_labels.values()))
