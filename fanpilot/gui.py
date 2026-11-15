# SPDX-License-Identifier: GPL-3.0-or-later
"""FanPilot desktop app (PySide6). Talks to fanpilotd over its socket.

Uses only palette colours and stock widgets so it follows the KDE theme.
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import math
import os
import queue
import re
import sys
import threading
import time

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSplitter,
                               QStackedWidget, QSystemTrayIcon,
                               QTabWidget, QToolButton, QVBoxLayout, QWidget, QSpinBox,
                               QInputDialog, QTreeWidget, QTreeWidgetItem, QDialog)

from . import SOCKET_PATH, __version__, presets
from .client import Client, DaemonError
from .config import CPU_FAN_FLOOR, NONSTOP_FLOOR, fan_defaults
from .curves import interpolate
from .hw import GPU_FAN_ID



# --------------------------------------------------------------------------- icon

def fan_icon(color: QColor | None = None, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = color or QColor("#3daee9")
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    cx = cy = size / 2
    for i in range(3):
        p.save()
        p.translate(cx, cy)
        p.rotate(i * 120)
        path = QPainterPath()
        path.moveTo(0, 0)
        path.cubicTo(size * 0.10, -size * 0.18, size * 0.40, -size * 0.30, size * 0.42, -size * 0.06)
        path.cubicTo(size * 0.30, -size * 0.02, size * 0.12, 0, 0, 0)
        p.drawPath(path)
        p.restore()
    p.drawEllipse(QPointF(cx, cy), size * 0.09, size * 0.09)
    p.end()
    return QIcon(pm)


def value_icon(text: str, color: QColor, size: int = 64) -> QIcon:
    """Tray icon showing a number (like HWiNFO's tray values)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.18, size * 0.18)
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * (0.62 if len(text) <= 2 else 0.46 if len(text) == 3 else 0.36)))
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, text)
    p.end()
    return QIcon(pm)


def installed_version() -> str | None:
    """Version of the code on disk now (it changes when FanPilot is updated while this app runs)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")) as f:
            m = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


class _WheelGuard(QObject):
    """In a scrolling page the wheel must scroll the page, not change whatever value passes under the
    cursor (a preset box would replace the curve). Fields react to the wheel only after a click."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Wheel and not obj.hasFocus():
            p = obj.parentWidget()
            while p is not None and not isinstance(p, QScrollArea):
                p = p.parentWidget()
            if p is not None:
                QApplication.sendEvent(p.verticalScrollBar(), ev)
            return True
        return False


def _guard_wheel(root: QWidget, guard: QObject) -> None:
    from PySide6.QtWidgets import QAbstractSpinBox
    for w in root.findChildren(QWidget):
        if isinstance(w, (QComboBox, QAbstractSpinBox, QSlider)):
            w.setFocusPolicy(Qt.StrongFocus)
            w.installEventFilter(guard)


def _scrolled(w: QWidget) -> QScrollArea:
    """A page that scrolls when the window is small, instead of forcing the window to stay big."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidget(w)
    if not hasattr(QApplication.instance(), "_fp_wheel_guard"):
        QApplication.instance()._fp_wheel_guard = _WheelGuard(QApplication.instance())
    _guard_wheel(w, QApplication.instance()._fp_wheel_guard)
    return sa


# --------------------------------------------------------------------------- curve editor

class CurveEditor(QWidget):
    """Drag points; double-click to add; right-click to delete."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[list[float]] = []
        self.t_range = (20.0, 100.0)
        self.d_min = 0.0
        self.d_floor = 0.0          # hard floor: points can't go below it (a card's firmware minimum)
        self.max_points = 8
        self.drag = None
        self.hover = None
        self.live_temp = None
        self.live_duty = None
        self.read_only = False
        self.setMinimumSize(360, 230)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip("Drag points to shape the curve.\nDouble-click to add a point, right-click a point to remove it.")

    def set_points(self, pts, t_range=(20.0, 100.0), d_min=0.0, max_points=8, hard_floor=None):
        """hard_floor: the lowest speed the hardware accepts (graphics card firmware). Points below it are
        raised to it, so the graph shows what the card really runs; None keeps the current floor."""
        if hard_floor is not None:
            self.d_floor = float(hard_floor)
        self.points = sorted([[float(t), max(float(d), self.d_floor)] for t, d in pts])
        self.t_range, self.d_min, self.max_points = t_range, d_min, max_points
        self.update()

    # geometry
    def _plot(self) -> QRectF:
        fm = self.fontMetrics()
        left = fm.horizontalAdvance("100 %") + 10
        right = fm.horizontalAdvance("100°") // 2 + 8      # the last x label is centred on the edge
        return QRectF(left, 10, self.width() - left - right, self.height() - fm.height() - 26)

    def _to_px(self, t, d) -> QPointF:
        r = self._plot()
        t0, t1 = self.t_range
        return QPointF(r.left() + (t - t0) / (t1 - t0) * r.width(), r.bottom() - d / 100 * r.height())

    def _from_px(self, pos) -> tuple[float, float]:
        r = self._plot()
        t0, t1 = self.t_range
        t = t0 + (pos.x() - r.left()) / r.width() * (t1 - t0)
        d = (r.bottom() - pos.y()) / r.height() * 100
        return t, d

    def _hit(self, pos):
        for i, (t, d) in enumerate(self.points):
            q = self._to_px(t, d)
            if math.hypot(q.x() - pos.x(), q.y() - pos.y()) < 10:
                return i
        return None

    # painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        r = self._plot()
        p.fillRect(r, pal.color(QPalette.Base))
        grid = QColor(pal.color(QPalette.Text))
        grid.setAlpha(40)
        p.setPen(QPen(grid, 1))
        t0, t1 = self.t_range
        fm = self.fontMetrics()
        for d in range(0, 101, 20):
            y = self._to_px(t0, d).y()
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        start = int(math.ceil(t0 / 10) * 10)
        for t in range(start, int(t1) + 1, 10):
            x = self._to_px(t, 0).x()
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.setPen(pal.color(QPalette.Text))
        for d in range(0, 101, 20):
            y = self._to_px(t0, d).y()
            s = f"{d} %"
            p.drawText(QPointF(r.left() - fm.horizontalAdvance(s) - 6, y + fm.ascent() / 2 - 1), s)
        for t in range(start, int(t1) + 1, 10):
            x = self._to_px(t, 0).x()
            s = f"{t}°"
            p.drawText(QPointF(x - fm.horizontalAdvance(s) / 2, r.bottom() + fm.ascent() + 4), s)
        p.drawText(QPointF(r.right() - fm.horizontalAdvance("°C"), r.bottom() + fm.ascent() + 4 + fm.height()), "")
        if self.d_min > 0:
            band = QColor(pal.color(QPalette.Text))
            band.setAlpha(18)
            p.fillRect(QRectF(r.left(), self._to_px(t0, self.d_min).y(), r.width(),
                              r.bottom() - self._to_px(t0, self.d_min).y()), band)
        if not self.points:
            return
        accent = pal.color(QPalette.Highlight)
        pts = sorted(self.points)
        path = QPainterPath(self._to_px(t0, pts[0][1]))
        for t, d in pts:
            path.lineTo(self._to_px(t, d))
        path.lineTo(self._to_px(t1, pts[-1][1]))
        fill = QPainterPath(path)
        fill.lineTo(self._to_px(t1, 0))
        fill.lineTo(self._to_px(t0, 0))
        fill.closeSubpath()
        fc = QColor(accent)
        fc.setAlpha(40)
        p.fillPath(fill, fc)
        p.setPen(QPen(accent, 2.5))
        p.drawPath(path)
        for i, (t, d) in enumerate(pts):
            q = self._to_px(t, d)
            big = i == self.hover or i == self.drag
            p.setBrush(accent if not self.read_only else pal.color(QPalette.Mid))
            p.setPen(QPen(pal.color(QPalette.Base), 2))
            p.drawEllipse(q, 7 if big else 5.5, 7 if big else 5.5)
            if big:
                s = f"{t:.0f} °C → {d:.0f} %"
                p.setPen(pal.color(QPalette.Text))
                x = min(q.x() + 10, r.right() - fm.horizontalAdvance(s))
                p.drawText(QPointF(x, max(r.top() + fm.ascent(), q.y() - 10)), s)
        if self.live_temp is not None:
            lt = max(t0, min(t1, self.live_temp))
            ld = self.live_duty if self.live_duty is not None else interpolate(pts, lt)
            live = QColor(pal.color(QPalette.Text))
            live.setAlpha(150)
            p.setPen(QPen(live, 1, Qt.DashLine))
            x = self._to_px(lt, 0).x()
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setBrush(pal.color(QPalette.Text))
            p.setPen(Qt.NoPen)
            p.drawEllipse(self._to_px(lt, max(0, min(100, ld))), 4, 4)

    # interaction
    def mousePressEvent(self, e):
        if self.read_only:
            return
        i = self._hit(e.position())
        if e.button() == Qt.RightButton and i is not None:
            if len(self.points) > 2:
                self.points = sorted(self.points)
                del self.points[i]
                self.hover = None
                self.changed.emit()
                self.update()
            return
        if e.button() == Qt.LeftButton and i is not None:
            self.points = sorted(self.points)
            self.drag = i

    def mouseDoubleClickEvent(self, e):
        if self.read_only or e.button() != Qt.LeftButton or self._hit(e.position()) is not None:
            return
        if len(self.points) >= self.max_points:
            return
        t, _ = self._from_px(e.position())
        t = round(max(self.t_range[0], min(self.t_range[1], t)))
        pts = sorted(self.points)
        if any(abs(t - pt[0]) < 2 for pt in pts):
            return
        d = max(round(interpolate(pts, t)) if pts else 50, self.d_floor)
        self.points = sorted(pts + [[float(t), float(d)]])
        self.changed.emit()
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self.drag is None:
            h = self._hit(pos)
            if h != self.hover:
                self.hover = h
                self.update()
            self.setCursor(Qt.OpenHandCursor if h is not None and not self.read_only else Qt.ArrowCursor)
            return
        t, d = self._from_px(pos)
        i = self.drag
        pts = self.points
        lo_t = pts[i - 1][0] + 1 if i > 0 else self.t_range[0]
        hi_t = pts[i + 1][0] - 1 if i < len(pts) - 1 else self.t_range[1]
        lo_d = max(pts[i - 1][1] if i > 0 else 0, self.d_floor)
        hi_d = pts[i + 1][1] if i < len(pts) - 1 else 100
        pts[i] = [float(round(max(lo_t, min(hi_t, t)))), float(round(max(lo_d, min(hi_d, d))))]
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, _):
        self.drag = None
        self.update()

    def leaveEvent(self, _):
        self.hover = None
        self.update()


# --------------------------------------------------------------------------- sensors view

class SensorsView(QWidget):
    """HWiNFO64-style sensor table (docs/SENSORS_VIEW_PLAN.md): one bold, collapsible
    heading per device, Current / Minimum / Maximum / Average per reading, statistics
    since the window opened (Reset clears them), limits in red. No graph."""
    COLUMNS = ["Sensor", "Current", "Minimum", "Maximum", "Average"]
    ALERT = QColor("#da4453")
    alarm = Signal(str)                  # a user alarm went off (message)
    rename_requested = Signal(str, str)  # (reading id, current label)
    tray_choice_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QSettings
        from PySide6.QtGui import QFontDatabase
        self.settings = QSettings("FanPilot", "FanPilot")
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.reset_btn = QPushButton(QIcon.fromTheme("view-refresh"), "Reset min / max / average")
        self.reset_btn.clicked.connect(self.reset)
        self.copy_btn = QPushButton(QIcon.fromTheme("edit-copy"), "Copy")
        self.copy_btn.setToolTip("Copy the whole table (tab-separated) to the clipboard")
        self.copy_btn.clicked.connect(self.copy)
        self.export_btn = QPushButton(QIcon.fromTheme("document-save-as"), "Export CSV…")
        self.export_btn.clicked.connect(self.export_csv)
        self.log_btn = QPushButton(QIcon.fromTheme("media-record"), "Start logging…")
        self.log_btn.setToolTip("Write every reading to a CSV file once a second (while this app runs), "
                                "e.g. during a game or a long render")
        self.log_btn.clicked.connect(self.toggle_logging)
        self.since = QLabel()
        for w in (self.reset_btn, self.copy_btn, self.export_btn, self.log_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.since)
        lay.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLUMNS))
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLUMNS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
            self.tree.headerItem().setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._column_menu)
        for c in self._hidden_columns():
            self.tree.setColumnHidden(c, True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._row_menu)
        self.tree.setToolTip("Right-click a sensor for alarms and the tray icon")
        self.tree.itemCollapsed.connect(lambda it: self._remember(it, True))
        self.tree.itemExpanded.connect(lambda it: self._remember(it, False))
        lay.addWidget(self.tree, 1)

        self.mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.stats: dict[str, list] = {}         # id -> [min, max, sum, count]
        self.items: dict[str, QTreeWidgetItem] = {}
        self.groups: dict[str, QTreeWidgetItem] = {}
        self.last: list[dict] = []
        self.titles: dict[str, str] = {}
        self.alarms: dict[str, dict] = self._load_alarms()     # id -> {"above": x, "below": y}
        self.alarm_on: set[str] = set()
        self.tray_id = self.settings.value("sensors/tray", "") or ""
        self._log = None                   # (file, csv writer, ids) while logging
        self._log_rows = 0
        self._start = time.time()
        self._update_since()

    # -- settings
    def _hidden_columns(self):
        try:
            return [int(c) for c in (self.settings.value("sensors/hidden_columns", "") or "").split(",") if c]
        except (TypeError, ValueError):
            return []

    def _column_menu(self, pos):
        m = QMenu(self)
        for c in range(2, len(self.COLUMNS)):          # Sensor and Current always shown
            a = m.addAction(self.COLUMNS[c])
            a.setCheckable(True)
            a.setChecked(not self.tree.isColumnHidden(c))
            a.toggled.connect(lambda on, c=c: self._set_column(c, on))
        m.exec(self.tree.header().mapToGlobal(pos))

    def _set_column(self, c, shown):
        self.tree.setColumnHidden(c, not shown)
        hidden = [i for i in range(len(self.COLUMNS)) if self.tree.isColumnHidden(i)]
        self.settings.setValue("sensors/hidden_columns", ",".join(map(str, hidden)))

    def _collapsed(self):
        return set((self.settings.value("sensors/collapsed", "") or "").split("|")) - {""}

    def _remember(self, item, collapsed):
        gid = item.data(0, Qt.UserRole)
        if not gid or not str(gid).startswith("group:"):
            return
        cur = self._collapsed()
        (cur.add if collapsed else cur.discard)(gid)
        self.settings.setValue("sensors/collapsed", "|".join(sorted(cur)))

    # -- alarms / tray value (right-click a row)
    def _load_alarms(self):
        try:
            data = json.loads(self.settings.value("sensors/alarms", "") or "{}")
            return {k: {d: float(v[d]) for d in ("above", "below") if isinstance(v.get(d), (int, float))}
                    for k, v in data.items() if isinstance(v, dict)}
        except (TypeError, ValueError, AttributeError):
            return {}

    def _save_alarms(self):
        self.alarms = {k: v for k, v in self.alarms.items() if v}
        self.settings.setValue("sensors/alarms", json.dumps(self.alarms))

    def reading(self, rid):
        return next((r for r in self.last if r["id"] == rid), None)

    def _row_menu(self, pos):
        item = self.tree.itemAt(pos)
        rid = next((k for k, v in self.items.items() if v is item), None)
        r = self.reading(rid) if rid else None
        if r is None:
            return
        m = QMenu(self)
        key = rid.split(":", 1)[1] if rid.startswith(("t:", "fan:")) else rid   # name the sensor/fan itself
        if getattr(self, "writable", True):
            m.addAction("Rename…", lambda: self.rename_requested.emit(key, r["label"]))
            m.addSeparator()
        a = self.alarms.get(rid, {})
        for d, word in (("above", "above"), ("below", "below")):
            label = f"Alarm {word}…" + (f"   (now {a[d]:g} {r['unit']})" if d in a else "")
            m.addAction(label, lambda d=d: self._ask_alarm(rid, d))
        if a:
            m.addAction("Remove alarm", lambda: self.set_alarm(rid, None, None))
        m.addSeparator()
        t = m.addAction("Show on tray icon")
        t.setCheckable(True)
        t.setChecked(self.tray_id == rid)
        t.toggled.connect(lambda on: self.set_tray(rid if on else ""))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _ask_alarm(self, rid, direction):
        r = self.reading(rid)
        if r is None:
            return
        cur = self.alarms.get(rid, {}).get(direction, r["value"] if r["value"] is not None else 0.0)
        v, ok = QInputDialog.getDouble(
            self, "Sensor alarm", f"Notify me when <b>{r['label']}</b> goes {direction} ({r['unit']}):",
            float(cur), -1e6, 1e6, max(1, int(r["decimals"])))
        if ok:
            self.set_alarm(rid, direction, v)

    def set_alarm(self, rid, direction, value):
        if direction is None:
            self.alarms.pop(rid, None)
        else:
            self.alarms.setdefault(rid, {})[direction] = float(value)
        self.alarm_on.discard(rid)
        self._save_alarms()
        self.update_readings(self.last, None, count=False)

    def set_tray(self, rid):
        self.tray_id = rid
        self.settings.setValue("sensors/tray", rid)
        self.tray_choice_changed.emit()

    def _alarm_state(self, r):
        """True while a user alarm is past its limit. Fires `alarm` once per crossing;
        re-arms only after the value comes back by a small margin (no flapping)."""
        a = self.alarms.get(r["id"])
        v = r["value"]
        if not a or v is None:
            return r["id"] in self.alarm_on
        margin = lambda lim: max(abs(lim) * 0.02, 10 ** -max(0, int(r["decimals"])))
        hit = ("above" in a and v >= a["above"]) or ("below" in a and v <= a["below"])
        back = (("above" not in a or v < a["above"] - margin(a["above"]))
                and ("below" not in a or v > a["below"] + margin(a["below"])))
        if hit and r["id"] not in self.alarm_on:
            self.alarm_on.add(r["id"])
            side = "above" if "above" in a and v >= a["above"] else "below"
            self.alarm.emit(f"{r['label']}: {self.fmt(r, v)} ({side} your alarm at {a[side]:g} {r['unit']})")
        elif back:
            self.alarm_on.discard(r["id"])
        return r["id"] in self.alarm_on

    # -- logging (HWiNFO-style CSV log)
    def toggle_logging(self):
        if self._log:
            self.stop_logging()
            return
        from PySide6.QtWidgets import QFileDialog
        default = os.path.join(os.path.expanduser("~"), time.strftime("fanpilot-log-%Y%m%d-%H%M%S.csv"))
        path, _ = QFileDialog.getSaveFileName(self, "Log sensors to", default, "CSV files (*.csv)")
        if path:
            self.start_logging(path)

    def start_logging(self, path):
        import csv
        if not self.last:
            QMessageBox.warning(self, "Logging", "No readings yet (is the FanPilot service running?).")
            return
        try:
            f = open(path, "w", newline="", encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Logging", f"Could not open the file: {e}")
            return
        ids = [r["id"] for r in self.last]
        w = csv.writer(f)
        w.writerow(["Time"] + [f"{self.titles.get(r['group'], r['group'])} / {r['label']} [{r['unit']}]"
                               for r in self.last])
        self._log, self._log_rows = (f, w, ids), 0
        self.log_btn.setText("Stop logging")
        self.log_btn.setIcon(QIcon.fromTheme("media-playback-stop"))
        self.log_btn.setToolTip(f"Logging to {path}")

    def stop_logging(self):
        if self._log:
            try:
                self._log[0].close()
            except OSError:
                pass
        self._log = None
        self.log_btn.setText("Start logging…")
        self.log_btn.setIcon(QIcon.fromTheme("media-record"))

    def _log_sample(self, readings):
        f, w, ids = self._log
        vals = {r["id"]: r["value"] for r in readings}
        try:
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S")] +
                       ["" if vals.get(i) is None else vals[i] for i in ids])
            f.flush()
            self._log_rows += 1
            self.log_btn.setText(f"Stop logging ({self._log_rows})")
        except OSError as e:
            self.stop_logging()
            QMessageBox.warning(self, "Logging stopped", f"Could not write the log: {e}")

    # -- data
    @staticmethod
    def fmt(r, v):
        if v is None:
            return "—"
        return f"{v:.{r['decimals']}f} {r['unit']}"

    def _update_since(self):
        self.since.setText("Min / max / average since " + time.strftime("%H:%M:%S", time.localtime(self._start)))

    def reset(self):
        self.stats.clear()
        self._start = time.time()
        self._update_since()
        self.update_readings(self.last, None, count=True)

    def update_readings(self, readings, groups, count=True):
        if groups is not None:
            self.titles = {g["id"]: g["title"] for g in groups}
        self.last = readings
        collapsed = self._collapsed()
        for r in readings:
            v = r["value"]
            st = self.stats.setdefault(r["id"], [None, None, 0.0, 0, False, False])
            user_alarm = self._alarm_state(r)
            alert = r["alert"] or user_alarm
            if v is not None:
                # every value shown counts for min / max (Current can never lie outside them) ...
                if st[0] is None or v <= st[0]:
                    st[4] = alert          # the lowest value seen was past a limit
                if st[1] is None or v >= st[1]:
                    st[5] = alert          # the highest value seen was past a limit
                st[0] = v if st[0] is None else min(st[0], v)
                st[1] = v if st[1] is None else max(st[1], v)
                if count:                  # ... but only one sample per service tick for the average
                    st[2] += v
                    st[3] += 1
            gid = "group:" + r["group"]
            g = self.groups.get(gid)
            if g is None:
                g = QTreeWidgetItem(self.tree, [self.titles.get(r["group"], r["group"])])
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                g.setFirstColumnSpanned(True)
                g.setData(0, Qt.UserRole, gid)
                g.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.groups[gid] = g
                self.tree.blockSignals(True)          # restoring state is not a user change: don't save it
                g.setExpanded(gid not in collapsed)
                self.tree.blockSignals(False)
            else:
                g.setText(0, self.titles.get(r["group"], g.text(0)))
            it = self.items.get(r["id"])
            if it is None:
                it = QTreeWidgetItem(g, [r["label"]])
                it.setToolTip(0, r["id"])
                for c in range(1, len(self.COLUMNS)):
                    it.setFont(c, self.mono)
                    it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
                self.items[r["id"]] = it
            if it.text(0) != r["label"]:
                it.setText(0, r["label"])        # renamed meanwhile
            avg = st[2] / st[3] if st[3] else None
            values = (v, st[0], st[1], avg)
            for c, val in enumerate(values, start=1):
                it.setText(c, self.fmt(r, val))
            cur_font = QFont(self.mono)
            cur_font.setBold(alert)
            it.setFont(1, cur_font)
            normal = self.palette().color(QPalette.Text)
            it.setForeground(1, self.ALERT if alert else normal)
            it.setToolTip(1, "Past your alarm" if user_alarm else "Past its limit" if alert else "")
            for c, flag in ((2, st[4]), (3, st[5])):
                it.setForeground(c, self.ALERT if flag else normal)
                it.setToolTip(c, "Was past a limit (since the last reset)" if flag else "")
            a = self.alarms.get(r["id"])
            rules = ", ".join(f"{d} {a[d]:g} {r['unit']}" for d in ("above", "below") if a and d in a)
            it.setIcon(0, QIcon.fromTheme("notifications") if rules else QIcon())
            it.setToolTip(0, r["id"] + (f"\nAlarm: {rules}" if rules else "")
                          + ("\nShown on the tray icon" if r["id"] == self.tray_id else ""))
        if count and self._log and readings:
            self._log_sample(readings)
        seen = {r["id"] for r in readings}
        for rid, it in self.items.items():        # gone (rescan, service restart): no stale "Current"
            if rid not in seen and it.text(1) != "—":
                it.setText(1, "—")
                it.setFont(1, self.mono)
                it.setForeground(1, self.palette().color(QPalette.Text))
                it.setToolTip(1, "No longer reported")

    def rows(self):
        out = [["Device"] + self.COLUMNS]
        for gid, g in self.groups.items():
            for i in range(g.childCount()):
                it = g.child(i)
                out.append([g.text(0)] + [it.text(c) for c in range(len(self.COLUMNS))])
        return out

    def copy(self):
        QApplication.clipboard().setText("\n".join("\t".join(r) for r in self.rows()))

    def export_csv(self):
        import csv
        from PySide6.QtWidgets import QFileDialog
        default = os.path.join(os.path.expanduser("~"), time.strftime("fanpilot-sensors-%Y%m%d-%H%M%S.csv"))
        path, _ = QFileDialog.getSaveFileName(self, "Export sensors", default, "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(self.rows())
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))


# --------------------------------------------------------------------------- fan editor

class FanEditor(QWidget):
    apply_requested = Signal(str, dict)
    calibrate_requested = Signal(str)
    calibrate_cancel_requested = Signal()
    identify_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fan_id = None
        self.is_gpu = False
        self.saved: dict = {}
        self.sensor_labels: dict[str, str] = {}
        self._loading = False

        lay = QVBoxLayout(self)
        self.title = QLabel()
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.title.setFont(f)
        self.live = QLabel()
        self.live.setTextFormat(Qt.RichText)
        lay.addWidget(self.title)
        lay.addWidget(self.live)

        mode_box = QGroupBox("How should this fan be controlled?")
        ml = QHBoxLayout(mode_box)             # one row: three stacked radio buttons cost ~80 px of height
        self.mode_group = QButtonGroup(self)
        self.rb_auto = QRadioButton()
        self.rb_fixed = QRadioButton("Fixed speed")
        self.rb_fixed.setToolTip("Always the same speed")
        self.rb_curve = QRadioButton("Temperature curve")
        self.rb_curve.setToolTip("Faster as it gets hotter")
        for i, rb in enumerate((self.rb_auto, self.rb_fixed, self.rb_curve)):
            self.mode_group.addButton(rb, i)
            ml.addWidget(rb)
        ml.addStretch(1)
        self.mode_group.idToggled.connect(self._mode_changed)
        lay.addWidget(mode_box)

        self.stack = QStackedWidget()
        # auto page
        self.auto_page = QLabel()
        self.auto_page.setWordWrap(True)
        self.auto_page.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.stack.addWidget(self.auto_page)
        # fixed page
        fixed = QWidget()
        fl = QHBoxLayout(fixed)
        fl.addWidget(QLabel("Speed:"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.spin = QSpinBox()
        self.spin.setRange(0, 100)
        self.spin.setSuffix(" %")
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.spin.valueChanged.connect(self._dirty)
        fl.addWidget(self.slider, 1)
        fl.addWidget(self.spin)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.addWidget(fixed)
        self.fixed_note = QLabel()
        self.fixed_note.setWordWrap(True)
        wl.addWidget(self.fixed_note)
        wl.addStretch(1)
        self.stack.addWidget(wrap)
        # curve page
        curve = QWidget()
        cl = QVBoxLayout(curve)
        cl.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("Preset:"))
        self.preset = QComboBox()
        self.preset.addItems(["Choose…", "Silent", "Balanced", "Performance"])
        self.preset.activated.connect(self._preset)
        top.addWidget(self.preset)
        top.addStretch(1)
        self.source_label = QLabel("Follows:")
        top.addWidget(self.source_label)
        self.source_btn = QToolButton()
        self.source_btn.setPopupMode(QToolButton.InstantPopup)
        self.source_menu = QMenu(self)
        self.source_btn.setMenu(self.source_menu)
        top.addWidget(self.source_btn)
        cl.addLayout(top)
        self.curve = CurveEditor()
        self.curve.changed.connect(self._dirty)
        cl.addWidget(self.curve, 1)
        self.curve_note = QLabel()
        self.curve_note.setWordWrap(True)
        cl.addWidget(self.curve_note)

        self.adv = QGroupBox("Fine tuning")
        self.adv.setCheckable(True)
        self.adv.setChecked(False)
        self.adv_inner = QWidget()
        al = QGridLayout(self.adv_inner)       # two columns: half the height of a one-column form
        al.setColumnStretch(4, 1)
        self.min_duty = QSpinBox()
        self.min_duty.setRange(0, 100)
        self.min_duty.setSuffix(" %")
        self.allow_stop = QCheckBox("Allow this fan to stop completely when cool")
        self.hyst = QDoubleSpinBox()
        self.hyst.setRange(0, 15)
        self.hyst.setSuffix(" °C")
        self.hyst.setDecimals(1)
        self.smooth = QDoubleSpinBox()
        self.smooth.setRange(0, 30)
        self.smooth.setSuffix(" s")
        self.smooth.setDecimals(1)
        self.ramp_up = QDoubleSpinBox()
        self.ramp_up.setRange(1, 100)
        self.ramp_up.setSuffix(" %/s")
        self.ramp_down = QDoubleSpinBox()
        self.ramp_down.setRange(0.5, 100)
        self.ramp_down.setSuffix(" %/s")
        self.zero_rpm = QCheckBox("Let the fan stop when the card is cool (zero RPM)")
        self._adv_labels = {}
        for r, c, text, w in ((0, 0, "Lowest speed:", self.min_duty), (0, 2, "Ignore small drops of:", self.hyst),
                              (1, 0, "Speed up by at most:", self.ramp_up),
                              (1, 2, "Slow down by at most:", self.ramp_down),
                              (2, 0, "Smooth temperature over:", self.smooth)):
            lab = QLabel(text)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lab.setBuddy(w)
            self._adv_labels[w] = lab
            al.addWidget(lab, r, c)
            al.addWidget(w, r, c + 1)
        al.addWidget(self.allow_stop, 2, 2, 1, 3)
        al.addWidget(self.zero_rpm, 3, 0, 1, 5)
        for w in (self.min_duty, self.hyst, self.smooth, self.ramp_up, self.ramp_down):
            w.valueChanged.connect(self._dirty)
        for w in (self.allow_stop, self.zero_rpm):
            w.toggled.connect(self._dirty)
        self.allow_stop.toggled.connect(lambda _: self._update_minimums())
        self.min_duty.valueChanged.connect(lambda v: setattr(self.curve, "d_min", v) or self.curve.update())
        advl = QVBoxLayout(self.adv)
        advl.addWidget(self.adv_inner)
        self.adv_inner.setVisible(False)
        self.adv.toggled.connect(self.adv_inner.setVisible)
        cl.addWidget(self.adv)
        self.stack.addWidget(curve)
        lay.addWidget(self.stack, 1)

        # calibration
        self.cal_box = QGroupBox("Calibration")
        cal = QVBoxLayout(self.cal_box)
        self.cal_text = QLabel()
        self.cal_text.setWordWrap(True)
        cal.addWidget(self.cal_text)
        row = QHBoxLayout()
        self.cal_bar = QProgressBar()
        self.cal_bar.setRange(0, 100)
        self.cal_bar.setVisible(False)
        self.cal_btn = QPushButton("Measure this fan…")
        self.cal_btn.clicked.connect(lambda: self.calibrate_requested.emit(self.fan_id))
        self.cal_use = QPushButton("Use as lowest speed")
        self.cal_use.clicked.connect(self._use_calibration)
        self.cal_use.setVisible(False)
        self.cal_stop = QPushButton(QIcon.fromTheme("process-stop"), "Stop")
        self.cal_stop.setToolTip("Stop measuring; the fan goes back to its normal setting")
        self.cal_stop.clicked.connect(self.calibrate_cancel_requested.emit)
        self.cal_stop.setVisible(False)
        row.addWidget(self.cal_bar, 1)
        row.addStretch(0)
        row.addWidget(self.cal_use)
        row.addWidget(self.cal_stop)
        row.addWidget(self.cal_btn)
        self.id_btn = QPushButton(QIcon.fromTheme("help-whatsthis"), "Identify")
        self.id_btn.setToolTip("Runs this header slow for 3 s, then at full speed for 3 s, so you can see or hear which fan it is")
        self.id_btn.clicked.connect(lambda: self.identify_requested.emit(self.fan_id))
        row.insertWidget(0, self.id_btn)
        cal.addLayout(row)
        lay.addWidget(self.cal_box)
        self.cal_result = None

        btns = QHBoxLayout()
        self.dirty_label = QLabel()
        btns.addWidget(self.dirty_label)
        btns.addStretch(1)
        self.revert_btn = QPushButton(QIcon.fromTheme("edit-undo"), "Revert")
        self.revert_btn.clicked.connect(lambda: self.load(self.fan_id, self.saved, self.is_gpu, force=True,
                                                          cpu_fan=getattr(self, "cpu_fan", False),
                                                          header=getattr(self, "header", True)))
        self.apply_btn = QPushButton(QIcon.fromTheme("dialog-ok-apply"), "Apply")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self._apply)
        btns.addWidget(self.revert_btn)
        btns.addWidget(self.apply_btn)
        lay.addLayout(btns)
        self._set_dirty(False)

    # -- loading
    def set_sensors(self, sensors, auto=None):
        labels = {s["id"]: s["label"] for s in sensors}
        changed = labels != getattr(self, "sensor_labels", None)
        self.sensor_labels = labels
        own = getattr(self, "auto_sensors", {}).get("auto:own")   # set per fan by the window: keep it
        self.auto_sensors = {k: v for k, v in (auto or {}).items() if v}   # "auto:cpu" -> this PC's sensor
        if own and "auto:own" not in self.auto_sensors:
            self.auto_sensors["auto:own"] = own
        if changed and self.fan_id and not self.is_gpu and hasattr(self, "sources"):
            self._build_source_menu()           # a sensor was renamed: the "Follows" menu shows the new name

    def load(self, fan_id, settings, is_gpu, label="", force=False, cpu_fan=None, header=True):
        if not force and fan_id == self.fan_id and self.is_dirty:
            return
        self._loading = True
        self.fan_id, self.is_gpu = fan_id, is_gpu
        self.header = header
        card = not header and not is_gpu          # an older AMD card controlled through pwm1 (beta)
        self.saved = copy.deepcopy(settings)
        s = {**fan_defaults(fan_id), **settings}
        if label:
            self.title.setText(label)
        # the service decides which fans get the CPU fan's protection (any board)
        cpu_fan = bool(cpu_fan) and not is_gpu
        self.rb_auto.setText("Automatic (graphics card)" if is_gpu or card else "Automatic (BIOS)")
        self.rb_auto.setToolTip("The graphics card decides" if is_gpu or card else "The motherboard (BIOS) decides")
        self.auto_page.setText(
            "The card's own firmware controls the fan. This is how it behaves without FanPilot."
            if is_gpu or card else
            "The BIOS 'Smart Fan' settings control this fan, exactly as without FanPilot. "
            "FanPilot still watches temperatures and runs every fan at full speed if the CPU or GPU "
            "gets critically hot.")
        self.fixed_note.setText(
            f"The card accepts {getattr(self, 'gpu_duty', (20, 100))[0]:.0f}–{getattr(self, 'gpu_duty', (20, 100))[1]:.0f} %. "
            "Zero-RPM is switched off in this mode." if is_gpu else
            f"The CPU fan never goes below {CPU_FAN_FLOOR} %." if cpu_fan else self.fixed_note.text())
        {"auto": self.rb_auto, "fixed": self.rb_fixed, "curve": self.rb_curve}[s["mode"]].setChecked(True)
        self.stack.setCurrentIndex({"auto": 0, "fixed": 1, "curve": 2}[s["mode"]])
        self.spin.setValue(int(round(s["fixed"])))
        self.cpu_fan = cpu_fan
        if is_gpu:
            t_lo, t_hi = getattr(self, "gpu_temp_range", (25, 100))
            g_lo = float(getattr(self, "gpu_duty", (20, 100))[0])
            self.curve.set_points(s["curve"], (float(t_lo), float(t_hi)), g_lo, getattr(self, "gpu_points", 5), g_lo)
            self.curve_note.setText("Uses the GPU hotspot temperature. The curve is stored in the card's "
                                    "firmware (5 points), so it keeps working even if FanPilot stops.")
            self.zero_rpm.setChecked(bool(s["zero_rpm"]))
        else:
            self.curve.set_points(s["curve"], (20.0, 100.0), float(s["min_duty"]), 8, 0.0)
            self.curve_note.setText("")
            self.allow_stop.setChecked(bool(s["allow_stop"]) and not cpu_fan)
            self._update_minimums()
            self.min_duty.setValue(int(round(s["min_duty"])))
            self.allow_stop.setEnabled(not cpu_fan)
            self.hyst.setValue(s["hysteresis"])
            self.smooth.setValue(s["smoothing"])
            self.ramp_up.setValue(s["ramp_up"])
            self.ramp_down.setValue(s["ramp_down"])
            auto = getattr(self, "auto_sensors", {})
            self.sources = list(dict.fromkeys(auto.get(x, x) for x in s["source"]))
            self._build_source_menu()
        self.curve_note.setVisible(bool(self.curve_note.text()))   # no blank band under the graph
        for w in (self.min_duty, self.allow_stop, self.hyst, self.smooth, self.ramp_up, self.ramp_down):
            if w in self._adv_labels:
                self._adv_labels[w].setVisible(not is_gpu)
            w.setVisible(not is_gpu)
        self.zero_rpm.setVisible(is_gpu)
        self.source_label.setVisible(not is_gpu)
        self.source_btn.setVisible(not is_gpu)
        self.cal_box.setVisible(not is_gpu and header)   # measure/identify: motherboard headers only
        if is_gpu:
            self._update_minimums()
        self.spin.setValue(int(round(s["fixed"])))
        self.preset.setCurrentIndex(0)
        self._loading = False
        self._set_dirty(False)

    def _update_minimums(self):
        if self.is_gpu:
            lo = int(getattr(self, "gpu_duty", (20, 100))[0])   # this card's own minimum (RX 9070 XT: 30 %)
        elif getattr(self, "cpu_fan", False):
            lo = CPU_FAN_FLOOR
        else:
            lo = 0 if self.allow_stop.isChecked() else int(NONSTOP_FLOOR)
        for w in (self.slider, self.spin, self.min_duty):
            w.setMinimum(lo)
        if not self.is_gpu and not getattr(self, "cpu_fan", False):
            self.fixed_note.setText("" if lo == 0 else
                                    f"Never below {lo} %, unless 'Allow this fan to stop' is ticked (Temperature curve → Fine tuning).")

    def _build_source_menu(self):
        self.source_menu.clear()
        for sid, label in self.sensor_labels.items():
            a = QAction(f"{label}", self.source_menu, checkable=True)
            a.setChecked(sid in self.sources)
            a.toggled.connect(lambda on, sid=sid: self._toggle_source(sid, on))
            self.source_menu.addAction(a)
        self._source_text()

    def _toggle_source(self, sid, on):
        if on and sid not in self.sources:
            self.sources.append(sid)
        elif not on and sid in self.sources:
            if len(self.sources) == 1:
                self._build_source_menu()  # keep at least one
                return
            self.sources.remove(sid)
        self._source_text()
        self._dirty()

    def _source_text(self):
        names = [self.sensor_labels.get(s, s) for s in self.sources]
        txt = names[0] if len(names) == 1 else f"hottest of {len(names)} sensors"
        self.source_btn.setText(txt + "  ")
        self.source_btn.setToolTip("\n".join(names))

    # -- editing
    @property
    def is_dirty(self):
        return self._is_dirty

    def _set_dirty(self, d):
        self._is_dirty = d
        self.dirty_label.setText("<i>Unsaved changes</i>" if d else "")
        self.apply_btn.setEnabled(d and getattr(self, "writable", True))
        self.revert_btn.setEnabled(d)

    def _dirty(self, *_):
        if not self._loading:
            self._set_dirty(True)

    def _mode_changed(self, idx, on):
        if on:
            self.stack.setCurrentIndex(idx)
            self._dirty()

    def _preset(self, idx):
        if idx <= 0:
            return
        name = self.preset.itemText(idx)
        pts = (presets.GPU if self.is_gpu else presets.BOARD)[name]
        self.curve.set_points(pts, self.curve.t_range, self.curve.d_min, self.curve.max_points)
        self._dirty()

    def settings(self) -> dict:
        mode = ("auto", "fixed", "curve")[self.mode_group.checkedId()]
        out = {"mode": mode, "fixed": self.spin.value(),
               "curve": [[int(t), int(d)] for t, d in sorted(self.curve.points)]}
        if self.is_gpu:
            out["zero_rpm"] = self.zero_rpm.isChecked()
        else:
            out.update(source=self.sources, min_duty=self.min_duty.value(),
                       allow_stop=self.allow_stop.isChecked(), hysteresis=self.hyst.value(),
                       smoothing=self.smooth.value(), ramp_up=self.ramp_up.value(),
                       ramp_down=self.ramp_down.value())
        return out

    def _apply(self):
        self.apply_requested.emit(self.fan_id, self.settings())

    def _use_calibration(self):
        if self.cal_result and self.cal_result.get("min_safe_duty") is not None:
            self.min_duty.setValue(int(self.cal_result["min_safe_duty"]))
            self.adv.setChecked(True)

    # -- live updates
    def update_live(self, fan: dict, calibration, writable: bool):
        self.writable = writable
        self.apply_btn.setEnabled(self.is_dirty and writable)
        rpm = fan.get("rpm")
        duty = fan.get("duty")
        parts = [f"<b>{rpm} RPM</b>" if rpm is not None else "RPM unknown"]
        if duty is not None:
            parts.append(f"{duty:.0f} %")
        elif fan.get("bios"):
            parts.append("speed set by the BIOS")
        if fan.get("temp") is not None:
            parts.append(f"at {fan['temp']:.0f} °C")
        flags = fan.get("flags") or []
        nice = {"critical": "⚠ critical temperature — full speed",
                "sensor_failed": "⚠ temperature sensor unreadable — full speed",
                "gave_up": "⚠ BIOS keeps taking control — left on Automatic",
                "write_error": "⚠ could not set speed", "calibrating": "measuring…",
                "identifying": "identifying: slow, then full speed",
                "testing": "self-test running", "mapping": "being mapped (Settings → Map my fans)",
                "stalled": "⚠ fan not spinning — check it",
                "error": f"⚠ {fan.get('error') or 'error'}"}
        parts += [nice.get(f, f) for f in flags]
        self.live.setText(" · ".join(parts))
        self.curve.live_temp = fan.get("temp")
        self.curve.live_duty = fan.get("target")
        self.curve.read_only = not writable
        self.curve.update()

        cal = calibration if calibration and calibration.get("fan") == self.fan_id else None
        running = bool(cal and cal["state"] == "running")
        self.cal_bar.setVisible(running)
        self.cal_stop.setVisible(running and writable)
        self.cal_btn.setEnabled(writable and not running)
        self.id_btn.setEnabled(writable and not running and "identifying" not in flags)
        if running:
            self.cal_bar.setValue(int(cal["progress"] * 100))
            self.cal_text.setText(f"Measuring: {cal['message']}. Keep the PC idle.")
        elif cal and cal["state"] == "aborted":
            self.cal_text.setText(f"Measurement stopped: {cal['message']}")

    def show_calibration(self, result):
        self.cal_result = result
        if not result:
            self.cal_text.setText("Measure the fan to find the lowest speed at which it keeps spinning "
                                  "(takes about 1–2 minutes; FanPilot steps the fan down and stops if the CPU gets hot).")
            self.cal_use.setVisible(False)
            return
        stop = result.get("stop_duty")
        start = result.get("start_duty")
        txt = f"Full speed: {result.get('max_rpm')} RPM. "
        if stop is None:
            txt += "The fan never stopped in the tested range."
        else:
            txt += f"Stops below about {stop + 5} %, restarts at {start if start is not None else '?'} %."
        txt += f" Suggested lowest speed: {result.get('min_safe_duty')} %."
        self.cal_text.setText(txt)
        self.cal_use.setVisible(True)


# --------------------------------------------------------------------------- main window

class _Poller(QObject):
    """Fetches the status on a worker thread, so a stuck service can never freeze the window.
    (Every button still talks to the service directly: those are rare and expected to wait.)"""
    done = Signal(object)          # ("ok", response, config or None) | ("err", message, None)

    def __init__(self, socket_path):
        super().__init__()
        self.socket_path = socket_path
        self.client = None
        self._jobs = queue.Queue()
        threading.Thread(target=self._run, name="fanpilot-poll", daemon=True).start()

    def request(self, want_config: bool):
        self._jobs.put(want_config)

    def stop(self):
        self._jobs.put(None)

    def _run(self):
        while True:
            want = self._jobs.get()
            if want is None:
                if self.client is not None:
                    self.client.close()     # give the service its connection slot back
                return
            try:
                if self.client is None:
                    self.client = Client(self.socket_path, timeout=2.0)
                resp = self.client.call("status")
                cfg = self.client.call("get_config")["config"] if want else None
                res = ("ok", resp, cfg)
            except DaemonError as e:
                res = ("err", str(e), None)
            except Exception as e:  # noqa: BLE001 -- shown in the banner; the thread must live on
                res = ("err", f"{type(e).__name__}: {e}", None)
            try:
                self.done.emit(res)
            except RuntimeError:     # window already gone
                return


class CpuFanFinder(QDialog):
    """'Find my CPU fan': pulse each spinning motherboard fan in turn (Identify) and ask whether the fan on
    the CPU cooler changed. A yes marks it as a CPU fan. Only the service moves fans; this only asks."""
    PULSE = 6.0          # the service's Identify: 3 s slow, then 3 s full speed

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Find my CPU fan")
        self.setMinimumWidth(460)
        fans = win.status.get("fans", [])
        # only fans that spin can be seen changing; empty headers would waste the user's time
        self.candidates = [(f["id"], f["label"]) for f in fans if f.get("kind") == "board" and (f.get("rpm") or 0) > 0]
        self.marked = set(win.config.get("cpu_fans", [])) & {f["id"] for f in fans}
        self.i = -1
        self.t0 = None
        lay = QVBoxLayout(self)
        intro = QLabel("Take the side panel off or get close to the PC. FanPilot makes one fan at a time "
                       "<b>slow down, then run at full speed</b> (6 s; the BIOS takes it back right after). "
                       "Watch, or listen to, <b>the fan on your CPU cooler</b>.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.step = QLabel()
        f = self.step.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        self.step.setFont(f)
        lay.addWidget(self.step)
        self.phase = QLabel()
        self.phase.setWordWrap(True)
        lay.addWidget(self.phase)
        self.bar = QProgressBar()
        self.bar.setRange(0, int(self.PULSE * 10))
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        self.question = QLabel("Did the fan on your CPU cooler just slow down and speed up?")
        self.question.setWordWrap(True)
        lay.addWidget(self.question)
        row = QHBoxLayout()
        self.yes_btn = QPushButton("Yes, that's the CPU cooler")
        self.no_btn = QPushButton("No, try the next fan")
        self.again_btn = QPushButton("Show me again")
        self.close_btn = QPushButton("Close")
        for b in (self.yes_btn, self.no_btn, self.again_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.yes_btn.clicked.connect(self.answer_yes)
        self.no_btn.clicked.connect(self.next_fan)
        self.again_btn.clicked.connect(self.pulse)
        self.close_btn.clicked.connect(self.reject)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        if not self.candidates:
            self._finish("No motherboard fan is spinning right now, so there is nothing to watch.")
        else:
            QTimer.singleShot(1500, self.next_fan)    # a moment to read the text first
            self.step.setText("Get ready…")
            self._buttons(False)

    def _buttons(self, on, more=True):
        for b in (self.yes_btn, self.no_btn, self.again_btn):
            b.setEnabled(on and more)
        self.question.setVisible(more)

    def next_fan(self):
        self.i += 1
        if self.i >= len(self.candidates):
            if self.marked:
                self._finish("Done. " + self._marked_text())
            else:
                self._finish("None of these was the CPU cooler fan. It may be on a header without a speed "
                             "reading, or on a pump/fan hub: every fan keeps the CPU fan's protection.")
            return
        self.pulse()

    def pulse(self):
        if not 0 <= self.i < len(self.candidates):
            return
        fid, label = self.candidates[self.i]
        self.step.setText(f"{label}   ({self.i + 1} of {len(self.candidates)})")
        try:
            self.win.client.call("identify", fan=fid)
        except DaemonError as e:
            self.timer.stop()
            self.phase.setText(f"Could not pulse {label}: {e}. Wait a moment, then press 'Show me again'.")
            self._buttons(True)
            return
        self.t0 = time.monotonic()
        self._buttons(False)
        self.timer.start()
        self._tick()

    def _tick(self):
        el = time.monotonic() - self.t0
        self.bar.setValue(min(self.bar.maximum(), int(el * 10)))
        if el < self.PULSE / 2:
            self.phase.setText(f"Slowing down…  {self.PULSE / 2 - el:.0f} s")
        elif el < self.PULSE:
            self.phase.setText(f"Full speed…  {self.PULSE - el:.0f} s")
        else:
            self.timer.stop()
            self.phase.setText("Back to the BIOS.")
            self._buttons(True)

    def answer_yes(self):
        fid, label = self.candidates[self.i]
        self.marked.add(fid)
        try:
            self.win.config = self.win.client.call("set_cpu_fans", fans=sorted(self.marked))["config"]
        except DaemonError as e:
            self.marked.discard(fid)
            QMessageBox.warning(self, "Could not change", str(e))
            return
        self.win.refresh()
        rest = len(self.candidates) - self.i - 1
        self.phase.setText(f"<b>{label} is now marked as a CPU fan.</b> "
                           + ("If your cooler has a second fan, try the next fan; otherwise close." if rest else ""))
        if not rest:
            self._finish("Done. " + self._marked_text())

    def _marked_text(self):
        names = dict(self.candidates)
        return "CPU fan: " + ", ".join(names.get(f, f) for f in sorted(self.marked)) + \
            ". It never stops and never runs below 20 %; the other fans can now be allowed to stop."

    def _finish(self, text):
        self.timer.stop()
        self.step.setText("Finished")
        self.phase.setText(text)
        self.bar.setValue(self.bar.maximum())
        self._buttons(False, more=False)
        self.close_btn.setText("Close")


class MainWindow(QMainWindow):
    @property
    def config(self) -> dict:
        return self._config

    @config.setter
    def config(self, value):
        self._config = value
        self._config_at = time.monotonic()   # see _polled: older poll answers are dropped

    def __init__(self, socket_path=SOCKET_PATH, demo=None):
        super().__init__()
        self.client = demo or Client(socket_path, timeout=2.0)
        self.config: dict = {}
        self.status: dict = {}
        self.writable = False
        self.quitting = False
        self.setWindowTitle("FanPilot")
        self.setWindowIcon(fan_icon())
        self.resize(1000, 720)

        central = QWidget()
        root = QVBoxLayout(central)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setOpenExternalLinks(False)
        self.banner.linkActivated.connect(self._banner_link)
        self.banner.setFrameShape(QFrame.StyledPanel)
        self.banner.setMargin(8)
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(180)
        self.profile_combo.setToolTip("Switch all fans at once. Built-in: Silent, Balanced, Performance "
                                      "(they change the curves, and keep each fan's lowest speed). "
                                      "Save your own in the Settings tab.")
        self.profile_combo.activated.connect(lambda i: self._apply_profile(self.profile_combo.itemData(i)))
        bar.addWidget(self.profile_combo)
        bar.addStretch(1)
        self.summary = QLabel()
        bar.addWidget(self.summary)
        self.update_btn = QPushButton(QIcon.fromTheme("system-software-update"), "Restart to update")
        self.update_btn.setToolTip("FanPilot was updated; restart this window to use the new version "
                                   "(fan control is not interrupted)")
        self.update_btn.clicked.connect(lambda: self._restart(hidden=False))
        self.update_btn.setVisible(False)
        bar.addWidget(self.update_btn)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        # fans tab
        fans = QSplitter()
        self.fan_list = QListWidget()
        self.fan_list.setIconSize(self.fan_list.iconSize() * 1.5)
        self.fan_list.setMinimumWidth(260)
        self.fan_list.currentRowChanged.connect(self._select_fan)
        self.fan_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.fan_list.customContextMenuRequested.connect(self._fan_menu)
        fans.addWidget(self.fan_list)
        self.editor = FanEditor()
        self.editor.apply_requested.connect(self._apply)
        self.editor.calibrate_requested.connect(self._calibrate)
        self.editor.calibrate_cancel_requested.connect(lambda: self._call_quiet("calibrate_cancel"))
        self.editor.identify_requested.connect(self._identify)
        fans.addWidget(_scrolled(self.editor))
        fans.setStretchFactor(1, 1)
        fans.setSizes([280, 720])
        self.tabs.addTab(fans, fan_icon(), "Fans")

        # sensors tab (HWiNFO-style; replaced the temperatures table + graph, 2026-11-14)
        self.sensors_view = SensorsView()
        self.sensors_view.alarm.connect(self._on_alarm)
        self.sensors_view.rename_requested.connect(self.rename)
        self.sensors_view.tray_choice_changed.connect(lambda: setattr(self, "_tray_level", None))
        self.tabs.addTab(self.sensors_view, QIcon.fromTheme("temperature-normal"), "Sensors")

        # log tab
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, QIcon.fromTheme("view-list-text"), "Log")

        # settings tab
        sett = QWidget()
        sl = QVBoxLayout(sett)
        pbox = QGroupBox("Profiles")
        pl = QHBoxLayout(pbox)
        self.profile_list = QListWidget()
        self.profile_list.setMaximumHeight(self.profile_list.fontMetrics().height() * 9)   # ~6 rows, no empty slab
        self.profile_list.currentItemChanged.connect(self._profile_buttons)
        self.profile_list.itemDoubleClicked.connect(lambda it: self._apply_profile(it.data(Qt.UserRole)))
        pl.addWidget(self.profile_list, 1)
        pbtns = QVBoxLayout()
        self.p_apply = QPushButton(QIcon.fromTheme("dialog-ok-apply"), "Use this profile")
        self.p_apply.clicked.connect(lambda: self.profile_list.currentItem() and
                                     self._apply_profile(self.profile_list.currentItem().data(Qt.UserRole)))
        self.p_save = QPushButton(QIcon.fromTheme("document-save-as"), "Save current settings as…")
        self.p_save.clicked.connect(self._save_profile)
        self.p_delete = QPushButton(QIcon.fromTheme("edit-delete"), "Delete")
        self.p_delete.clicked.connect(self._delete_profile)
        for b in (self.p_apply, self.p_save, self.p_delete):
            pbtns.addWidget(b)
        pbtns.addStretch(1)
        pl.addLayout(pbtns)
        sl.addWidget(pbox)
        cbox = QGroupBox("Emergency limits — above these, every motherboard fan runs at full speed")
        cf = QHBoxLayout(cbox)                 # one row
        self.crit_cpu = QSpinBox()
        self.crit_cpu.setRange(70, 95)
        self.crit_cpu.setSuffix(" °C")
        self.crit_gpu = QSpinBox()
        self.crit_gpu.setRange(80, 105)
        self.crit_gpu.setSuffix(" °C")
        self.crit_save = QPushButton(QIcon.fromTheme("document-save"), "Save limits")
        self.crit_save.clicked.connect(self._save_critical)
        for text, w_ in (("CPU:", self.crit_cpu), ("GPU hotspot:", self.crit_gpu)):
            lab = QLabel(text)
            lab.setBuddy(w_)
            cf.addWidget(lab)
            cf.addWidget(w_)
            cf.addSpacing(12)
        cf.addWidget(self.crit_save)
        cf.addStretch(1)
        sl.addWidget(cbox)
        tbox = QGroupBox("Self-test")
        tl2 = QVBoxLayout(tbox)
        intro = QLabel("Checks that everything works on this PC: the fan chip and sensors, speed control of each "
                       "system fan, that fans are handed back to the BIOS after a crash, and an RX 7000/9000 "
                       "graphics card's fan curve if there is one. Takes about 45 seconds; those fans change speed "
                       "for a moment. CPU fans and pumps are only read. Stops by itself if the CPU gets hot.")
        intro.setWordWrap(True)
        tl2.addWidget(intro)
        trow = QHBoxLayout()
        self.test_btn = QPushButton(QIcon.fromTheme("system-run"), "Run self-test")
        self.test_btn.clicked.connect(self._run_selftest)
        self.test_cancel = QPushButton(QIcon.fromTheme("process-stop"), "Stop")
        self.test_cancel.clicked.connect(lambda: self._call_quiet("selftest_cancel"))
        self.test_bar = QProgressBar()
        self.test_bar.setRange(0, 100)
        self.test_step = QLabel()
        trow.addWidget(self.test_btn)
        trow.addWidget(self.test_cancel)
        trow.addWidget(self.test_bar, 1)
        tl2.addLayout(trow)
        tl2.addWidget(self.test_step)
        self.test_results = QListWidget()
        self.test_results.setMinimumHeight(200)
        self.test_results.setSelectionMode(QAbstractItemView.NoSelection)
        tl2.addWidget(self.test_results)
        sl.addWidget(tbox, 1)
        self._test_shown = None
        mbox = QGroupBox("Which header drives which fan?")
        ml = QVBoxLayout(mbox)
        mrow = QHBoxLayout()
        self.map_btn = QPushButton(QIcon.fromTheme("view-list-tree"), "Map my fans…")
        self.map_btn.setToolTip("Slows one header at a time to 30 % (never stops a fan) and watches which fan "
                                "reacts. Fixes speed readings on boards where the numbers don't match, and hides "
                                "headers with nothing connected. About 15 seconds per header.")
        self.map_btn.clicked.connect(self._run_mapping)
        self.map_cancel = QPushButton(QIcon.fromTheme("process-stop"), "Stop")
        self.map_cancel.clicked.connect(lambda: self._call_quiet("map_cancel"))
        self.map_bar = QProgressBar()
        self.map_bar.setRange(0, 100)
        self.map_text = QLabel()
        self.map_text.setWordWrap(True)
        self.map_forget = QPushButton(QIcon.fromTheme("edit-clear-history"), "Forget the fan map")
        self.map_forget.setToolTip("Back to the headers exactly as the driver reports them (hidden ones return)")
        self.map_forget.clicked.connect(self._forget_map)
        for w_ in (self.map_btn, self.map_cancel, self.map_forget):
            mrow.addWidget(w_)
        mrow.addWidget(self.map_bar, 1)
        ml.addLayout(mrow)
        ml.addWidget(self.map_text)
        sl.addWidget(mbox)

        self.about = QLabel()
        self.about.setWordWrap(True)
        self.about.setTextInteractionFlags(Qt.TextSelectableByMouse)
        abox = QGroupBox("About")
        QVBoxLayout(abox).addWidget(self.about)
        sl.addWidget(abox)
        self.tabs.addTab(_scrolled(sett), QIcon.fromTheme("configure"), "Settings")

        # tray (at login the panel may come up after us: keep trying for 2 minutes)
        self.tray = None
        self._profiles_shown = None
        self._tray_level = None
        self._make_tray()
        if self.tray is None:
            self._tray_tries = 0
            self._tray_timer = QTimer(self)
            self._tray_timer.timeout.connect(self._retry_tray)
            self._tray_timer.start(5000)

        self._last_log = 0
        self._last_sample = 0.0
        self._polling = False
        self._poll_sent = 0.0
        self._poller = None
        if demo is None:
            self._poller = _Poller(socket_path)
            self._poller.done.connect(self._polled)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._check_update)
        self._update_timer.start(10000)
        try:                                   # the size and place the window had last time
            g = self.sensors_view.settings.value("window/geometry")
            if g:
                self.restoreGeometry(g)
        except Exception:
            pass
        self.refresh(first=True)

    def _save_geometry(self):
        try:
            self.sensors_view.settings.setValue("window/geometry", self.saveGeometry())
        except Exception:
            pass

    # -- updates: a running window keeps its old code until it restarts
    def _update_pending(self) -> bool:
        v = installed_version()
        return v is not None and v != __version__

    def _check_update(self):
        if not self._update_pending():
            return
        if self.isVisible() or self.sensors_view._log:
            self.update_btn.setVisible(True)     # don't yank a window the user is looking at, or a log
        else:
            self._restart(hidden=True)           # in the tray: just come back as the new version

    def show_or_update(self):
        """Opened again from the menu: show this window, or come back as the new version."""
        if self._update_pending() and not self.sensors_view._log:
            self._restart(hidden=False)
        else:
            self._show()
            self.update_btn.setVisible(self._update_pending())

    def _restart(self, hidden: bool):
        self._save_geometry()
        self.quitting = True
        self.sensors_view.stop_logging()
        if self._poller:
            self._poller.stop()
        if hasattr(self.client, "close"):
            self.client.close()
        args = [a for a in sys.argv[1:] if a != "--hidden"] + (["--hidden"] if hidden else [])
        os.execv(sys.executable, [sys.executable, "-I", os.path.abspath(sys.argv[0])] + args)

    def _make_tray(self):
        if self.tray is not None or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(fan_icon(), self)
        m = QMenu(self)
        m.addAction("Show FanPilot", self._show)
        self.tray_profiles = m.addMenu("Profile")
        m.addSeparator()
        m.addAction("Quit (fans keep running)", self._quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self._show() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self._profiles_shown = None          # fill its Profile menu on the next update
        self._tray_level = None

    def _retry_tray(self):
        self._tray_tries += 1
        self._make_tray()
        if self.tray is not None or self._tray_tries >= 24:
            self._tray_timer.stop()

    # -- data
    def _tick(self):
        if self._poller is None:
            self.refresh()
            return
        if self._polling:
            return          # the last request is still waiting on a stuck service: don't pile up more
        self._ticks = getattr(self, "_ticks", 0) + 1
        self._polling = True
        self._poll_sent = time.monotonic()
        # other windows or fanpilotctl may change settings too
        self._poller.request(not self.config or self._ticks % 5 == 0)

    def _polled(self, res):
        self._polling = False
        kind, data, cfg = res
        if kind == "err":
            self._unreachable(data)
            return
        if cfg is not None and self._poll_sent >= self._config_at:
            self.config = cfg          # (an answer sent before the last change would undo it)
        self._show_status(data, False)

    def _unreachable(self, msg):
        self._banner("error", f"<b>FanPilot service is not reachable.</b> {msg}<br>"
                     "Start it with <code>sudo systemctl enable --now fanpilotd</code>.")

    def refresh(self, first=False):
        """Fetch now (on this thread) and show it. Used at start, after actions, and by tests."""
        try:
            resp = self.client.call("status")
            self._ticks = getattr(self, "_ticks", 0) + 1
            if first or not self.config or self._ticks % 5 == 0:
                self.config = self.client.call("get_config")["config"]
        except DaemonError as e:
            self._unreachable(str(e))
            return
        self._show_status(resp, first)

    def _show_status(self, resp, first):
        try:
            self._show_status_inner(resp, first)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            ver = (resp.get("status") or {}).get("version", "?") if isinstance(resp, dict) else "?"
            self._banner("error", f"<b>This window (FanPilot {__version__}) does not understand the running "
                         f"service (FanPilot {ver}).</b> Restart the service "
                         "(<code>sudo systemctl restart fanpilotd</code>) and reopen this window. "
                         f"<small>({type(e).__name__}: {e})</small>")

    def _show_status_inner(self, resp, first):
        self.status = resp["status"]
        self.writable = resp.get("can_write", False)
        st = self.status
        self._update_banner()
        cpu_id, gpu_id = self._crit_ids()
        self.editor.set_sensors(st["sensors"], {"auto:cpu": cpu_id, "auto:gpu": gpu_id})
        self.sensors_view.writable = self.writable
        self.editor.setEnabled(bool(st["fans"]))      # nothing to edit on a board without controllable fans
        self._update_fans()
        self._update_profiles(first)
        self._update_selftest()
        self._update_mapping()
        # one sample per service tick: refresh() right after a button press must not count twice
        now = time.monotonic()
        sample = now - self._last_sample >= 0.9
        if sample:
            self._last_sample = now
        self.sensors_view.update_readings(st.get("readings", []), st.get("groups", []), count=sample)
        new = [e for e in st["events"] if e["t"] > self._last_log]
        for e in new:
            self.log.appendPlainText(f"{time.strftime('%H:%M:%S', time.localtime(e['t']))}  "
                                     f"{e['level'].upper():5}  {e['msg']}")
            if self.tray and e["level"] in ("warn", "error") and not first:
                self.tray.showMessage("FanPilot", e["msg"],
                                      QSystemTrayIcon.Warning if e["level"] == "warn" else QSystemTrayIcon.Critical)
        if new:
            self._last_log = new[-1]["t"]
        if self.tray:
            hot = {s["id"]: s["value"] for s in st["sensors"]}
            cpu, gpu = (hot.get(x or "") for x in self._crit_ids())
            sv = self.sensors_view
            chosen = sv.reading(sv.tray_id) if sv.tray_id else None
            tip = "FanPilot\n" + (f"CPU {cpu:.0f} °C" if cpu is not None else "") \
                + (f"   GPU {gpu:.0f} °C" if gpu is not None else "")
            if chosen is not None:
                tip += f"\n{chosen['label']}: {sv.fmt(chosen, chosen['value'])}"
            self.tray.setToolTip(tip)
            level = 2 if st["critical"] else 1 if (cpu or 0) >= 80 or (gpu or 0) >= 90 else 0
            text = None
            if chosen is not None and chosen["value"] is not None:
                text = f"{chosen['value']:.0f}" if abs(chosen["value"]) < 1000 else f"{chosen['value'] / 1000:.1f}k"
                if chosen["alert"] or chosen["id"] in sv.alarm_on:
                    level = 2
            key = (level, text)
            if key != self._tray_level:
                self._tray_level = key
                color = QColor(("#3daee9", "#f67400", "#da4453")[level])
                self.tray.setIcon(value_icon(text, color) if text is not None else fan_icon(color))

    def _banner(self, kind, html):
        self.banner.setVisible(bool(html))
        self.banner.setText(html)
        pal = self.banner.palette()
        color = {"error": QColor("#da4453"), "warn": QColor("#f67400"), "info": QColor("#3daee9")}[kind]
        bg = QColor(color)
        bg.setAlpha(45)
        pal.setColor(QPalette.Window, bg)
        self.banner.setAutoFillBackground(True)
        self.banner.setPalette(pal)

    def _banner_link(self, href):
        if href == "fanpilot:find-cpu":
            self.find_cpu_fan()
        else:
            QDesktopServices.openUrl(QUrl(href))

    def find_cpu_fan(self):
        dlg = CpuFanFinder(self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()
        return dlg

    def _update_banner(self):
        st = self.status
        if st["critical"]:
            self._banner("error", "<b>Critical temperature.</b> All motherboard fans are running at full speed.")
        elif st.get("version") != __version__:
            self._banner("warn", f"<b>Updated:</b> this window is FanPilot {__version__} but the service is still "
                                 f"{st.get('version')}. Restart it: <code>sudo systemctl restart fanpilotd</code>.")
        elif not st["board_chip"]:
            self._banner("warn", "<b>Motherboard fans can't be controlled.</b> No supported fan chip driver is "
                                 "loaded (or the board's chip is read-only), so FanPilot only shows readings"
                                 + (" and controls the graphics card" if any(f.get("kind") == "gpu" for f in st["fans"])
                                    else "") + ". See SETUP.md → 'My motherboard fans are missing' (<i>/usr/share/doc/fanpilot/</i> or the source "
                                    "folder's <i>docs/</i>).")
        elif not st.get("cpu_fan_known", True) and self.writable:
            self._banner("info", "<b>Which fan is your CPU fan?</b> FanPilot doesn't know this board yet, so every "
                                 "motherboard fan keeps the CPU fan's protection (never stops, at least 20 %). "
                                 "<a href=\"fanpilot:find-cpu\">Find my CPU fan…</a> pulses each fan in turn "
                                 "while you watch the cooler (or use <i>Identify</i>, then right-click the fan → "
                                 "<i>This is a CPU fan</i>).")
        elif not self.writable:
            self._banner("info", f"<b>View only.</b> Your user (uid {os.getuid()}) may not change settings. "
                                 "Add it to the <code>fanpilot</code> group "
                                 "(<code>sudo usermod -aG fanpilot $USER</code>, then log out and in), or install "
                                 "with <code>sudo ./install.sh</code>, which allows the user who runs it.")
        else:
            self.banner.setVisible(False)

    def _update_fans(self):
        fans = self.status["fans"]
        ids = [f["id"] for f in fans]
        if [self.fan_list.item(i).data(Qt.UserRole) for i in range(self.fan_list.count())] != ids:
            cur = self.editor.fan_id
            self.fan_list.blockSignals(True)
            self.fan_list.clear()
            for f in fans:
                it = QListWidgetItem(fan_icon(QColor("#f67400") if f["kind"] == "gpu" else None), f["label"])
                it.setData(Qt.UserRole, f["id"])
                self.fan_list.addItem(it)
            if cur in ids:
                # same fan still there: just keep it selected; nothing to load or ask
                self.fan_list.setCurrentRow(ids.index(cur))
                self.fan_list.blockSignals(False)
            else:
                if ids:
                    self.fan_list.setCurrentRow(0)
                self.fan_list.blockSignals(False)
                if ids:
                    self.editor._set_dirty(False)        # the fan being edited is gone
                    self._select_fan(0)
        modes = {"auto": "Automatic", "fixed": "Fixed", "curve": "Curve"}
        for i, f in enumerate(fans):
            rpm = f.get("rpm")
            mode = modes.get(f.get("mode"), "")
            flags = f.get("flags") or []
            warn = " ⚠" if any(x in flags for x in ("critical", "sensor_failed", "gave_up", "write_error", "error", "stalled")) else ""
            self.fan_list.item(i).setText(f"{f['label']}\n{rpm if rpm is not None else '—'} RPM · {mode}{warn}")
        cur = self.editor.fan_id
        jobs = any((self.status.get(k) or {}).get("state") == "running" for k in ("selftest", "mapping"))
        for f in fans:
            if f["id"] == cur:
                self.editor.update_live(f, self.status.get("calibration"), self.writable)
                if self.editor.title.text() != f["label"]:
                    self.editor.title.setText(f["label"])      # renamed
                if jobs:                                       # identify / measure would be refused now
                    self.editor.id_btn.setEnabled(False)
                    self.editor.cal_btn.setEnabled(False)

    def _select_fan(self, row):
        if row < 0 or row >= len(self.status.get("fans", [])):
            return
        if self.editor.is_dirty and self.editor.fan_id:
            ans = QMessageBox.question(self, "Unsaved changes",
                                       "Apply the changes you made to this fan first?",
                                       QMessageBox.Apply | QMessageBox.Discard | QMessageBox.Cancel)
            stay = ans == QMessageBox.Cancel
            if ans == QMessageBox.Apply and not self._apply(self.editor.fan_id, self.editor.settings()):
                stay = True
            if stay:
                ids = [f["id"] for f in self.status["fans"]]
                if self.editor.fan_id in ids:
                    self.fan_list.blockSignals(True)
                    self.fan_list.setCurrentRow(ids.index(self.editor.fan_id))
                    self.fan_list.blockSignals(False)
                    return
        try:
            self.config = self.client.call("get_config")["config"]  # may have changed via fanpilotctl
        except DaemonError:
            pass
        f = self.status["fans"][row]
        firmware_curve = f.get("control", "pmfw" if f["kind"] == "gpu" else "pwm") == "pmfw"   # RX 7000/9000
        rng = f.get("ranges") or {}                    # this card's own limits, as its firmware reports them
        self.editor.gpu_temp_range = tuple(rng.get("temp", (25, 100)))
        self.editor.gpu_duty = tuple(rng.get("duty", (20, 100)))
        self.editor.gpu_points = int(rng.get("points", 5))
        if f.get("own_sensor"):
            self.editor.auto_sensors["auto:own"] = f["own_sensor"]        # an older card's own temperature
        else:
            self.editor.auto_sensors.pop("auto:own", None)
        self.editor.load(f["id"], self.config.get("fans", {}).get(f["id"], {}), firmware_curve,
                         f["label"], force=True, cpu_fan=self._cpu_protected(f["id"]),
                         header=f.get("kind", "board") == "board")
        self.editor.show_calibration(self.config.get("calibration", {}).get(f["id"]))
        self.editor.update_live(f, self.status.get("calibration"), self.writable)

    # -- names and CPU fans (any board)
    def _fan_menu(self, pos):
        it = self.fan_list.itemAt(pos)
        if it is None or not self.writable:
            return
        fid = it.data(Qt.UserRole)
        fan = next((f for f in self.status.get("fans", []) if f["id"] == fid), None)
        if fan is None:
            return
        m = QMenu(self)
        m.addAction("Rename…", lambda: self.rename(fid, fan["label"]))
        if fan.get("kind") == "board":
            marks = set(self.config.get("cpu_fans", []))
            a = m.addAction("This is a CPU fan (never stops, at least 20 %)")
            a.setCheckable(True)
            a.setChecked(fid in marks)
            a.toggled.connect(lambda on: self._mark_cpu_fan(fid, on))
            m.addAction("Find my CPU fan…", self.find_cpu_fan)
        m.exec(self.fan_list.viewport().mapToGlobal(pos))

    def rename(self, item_id, current):
        name, ok = QInputDialog.getText(self, "Rename", "New name (empty = back to the default):", text=current)
        if not ok:
            return
        try:
            self.config = self.client.call("rename", id=item_id, name=name.strip())["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "Could not rename", str(e))
            return
        self.refresh()

    def _mark_cpu_fan(self, fid, on):
        present = {f["id"] for f in self.status.get("fans", [])}
        marks = set(self.config.get("cpu_fans", [])) & present     # stale ids (fan no longer there) dropped
        (marks.add if on else marks.discard)(fid)
        try:
            self.config = self.client.call("set_cpu_fans", fans=sorted(marks))["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "Could not change", str(e))
        self.refresh()
        self._select_fan(self.fan_list.currentRow())

    # -- actions
    def _apply(self, fan_id, settings) -> bool:
        try:
            self.config = self.client.call("set_fan", fan=fan_id, settings=settings)["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "Could not apply", str(e))
            return False
        if self.editor.fan_id == fan_id:
            self.editor.load(fan_id, self.config["fans"].get(fan_id, {}), self.editor.is_gpu, force=True,
                             cpu_fan=self._cpu_protected(fan_id), header=getattr(self.editor, "header", True))
        return True

    def _calibrate(self, fan_id):
        label = next((f["label"] for f in self.status["fans"] if f["id"] == fan_id), fan_id)
        ok = QMessageBox.question(
            self, "Measure fan",
            f"FanPilot will slowly lower <b>{label}</b> from full speed and may stop it for a few "
            "seconds to find its lowest working speed.<br><br>Leave the PC idle for about two minutes. "
            "The test stops by itself if the CPU reaches 80 °C.<br><br>Start now?")
        if ok != QMessageBox.Yes:
            return
        try:
            self.client.call("calibrate", fan=fan_id)
        except DaemonError as e:
            QMessageBox.warning(self, "Could not start", str(e))
            return
        self._watch_cal = fan_id
        QTimer.singleShot(1500, self._poll_cal)

    def _poll_cal(self):
        cal = self.status.get("calibration")
        if cal and cal.get("state") == "running":
            QTimer.singleShot(1500, self._poll_cal)
            return
        try:
            self.config = self.client.call("get_config")["config"]
        except DaemonError:
            return
        if self.editor.fan_id == getattr(self, "_watch_cal", None):
            self.editor.show_calibration(self.config.get("calibration", {}).get(self.editor.fan_id))

    def _update_profiles(self, first):
        st = self.status
        names, active = st.get("profiles", []), st.get("active_profile")
        rpms = [f.get("rpm") for f in st["fans"] if f.get("rpm") is not None]
        hot = {x["id"]: x["value"] for x in st["sensors"]}
        cpu, gpu = (hot.get(x or "") for x in self._crit_ids())
        self.summary.setText(" · ".join(x for x in (
            f"CPU {cpu:.0f} °C" if cpu is not None else "", f"GPU {gpu:.0f} °C" if gpu is not None else "",
            f"{len(rpms)} fans") if x))
        self._load_limits()
        self.about.setText(f"FanPilot {st['version']} · service running for {st['uptime'] // 60} min · "
                           f"motherboard chip: {st['board_chip'] or 'not found'} · your uid {os.getuid()} "
                           f"{'can' if self.writable else 'can NOT'} change settings"
                           + "".join(f"\n\nNote: {h}" for h in st.get("diagnosis") or []))
        key = (tuple(names), active, self.writable)
        if key == self._profiles_shown:
            return
        self._profiles_shown = key
        builtin = {"Silent", "Balanced", "Performance"}
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if active is None:
            self.profile_combo.addItem("Custom (hand-tuned)", None)
        for n in names:
            self.profile_combo.addItem(n, n)
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(active)) if active else 0)
        self.profile_combo.setEnabled(self.writable and bool(st["fans"]))
        self.profile_combo.blockSignals(False)
        cur = self.profile_list.currentItem().data(Qt.UserRole) if self.profile_list.currentItem() else active
        self.profile_list.clear()
        for n in names:
            it = QListWidgetItem(("✓ " if n == active else "    ") + n + ("   (built-in)" if n in builtin else ""))
            it.setData(Qt.UserRole, n)
            self.profile_list.addItem(it)
            if n == cur:
                self.profile_list.setCurrentItem(it)
        self.p_save.setEnabled(self.writable)
        self.crit_save.setEnabled(self.writable)
        self._profile_buttons()
        if self.tray:
            self.tray_profiles.clear()
            for n in names:
                a = self.tray_profiles.addAction(n, lambda n=n: self._apply_profile(n))
                a.setCheckable(True)
                a.setChecked(n == active)
                a.setEnabled(self.writable)

    def _profile_buttons(self, *_):
        it = self.profile_list.currentItem()
        name = it.data(Qt.UserRole) if it else None
        self.p_apply.setEnabled(self.writable and name is not None and bool(self.status.get("fans")))
        self.p_delete.setEnabled(self.writable and name is not None
                                 and name not in ("Silent", "Balanced", "Performance"))

    def _load_limits(self, force=False):
        crit = self.config.get("critical", {})
        key = (crit.get("cpu_temp"), crit.get("gpu_temp"))
        if crit and (force or key != getattr(self, "_limits_loaded", None)):
            self._limits_loaded = key
            self.crit_cpu.setValue(int(key[0]))
            self.crit_gpu.setValue(int(key[1]))

    def _reset_profile_widgets(self):
        self._profiles_shown = None
        self._update_profiles(False)

    def _apply_profile(self, name):
        if not name:
            return
        if not self.writable:
            QMessageBox.information(self, "View only", "Your user may not change settings (see the banner).")
            self._reset_profile_widgets()
            return
        if self.editor.is_dirty:
            ans = QMessageBox.question(
                self, "Switch profile",
                f"You have unapplied changes to <b>{self.title_of(self.editor.fan_id)}</b>. "
                "They will be lost when the profile is switched. Continue?")
            if ans != QMessageBox.Yes:
                self._reset_profile_widgets()
                return
            self.editor._set_dirty(False)
        if self.status.get("active_profile") is None:
            ans = QMessageBox.question(
                self, "Switch profile",
                f"Switching to <b>{name}</b> replaces your current hand-tuned fan settings.<br>"
                "Save them as a profile first?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
            if ans == QMessageBox.Cancel or (ans == QMessageBox.Save and not self._save_profile()):
                self._reset_profile_widgets()
                return
        try:
            self.config = self.client.call("apply_profile", name=name)["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "Could not switch profile", str(e))
            self._reset_profile_widgets()
            return
        self.refresh()
        self.editor._set_dirty(False)
        self._select_fan(self.fan_list.currentRow())

    def title_of(self, fan_id):
        return next((f["label"] for f in self.status.get("fans", []) if f["id"] == fan_id), fan_id)

    def _save_profile(self) -> bool:
        name, ok = QInputDialog.getText(
            self, "Save profile", "Name for the current fan settings\n"
            "(up to 32 letters, digits, spaces, . ( ) + -; not Silent / Balanced / Performance):")
        name = name.strip()
        if not ok or not name:
            return False
        if name in self.status.get("profiles", []) and QMessageBox.question(
                self, "Save profile", f"A profile named <b>{name}</b> already exists. Replace it?") != QMessageBox.Yes:
            return False
        try:
            self.config = self.client.call("save_profile", name=name.strip())["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "Could not save profile", str(e))
            return False
        self.refresh()
        return True

    def _delete_profile(self):
        it = self.profile_list.currentItem()
        if not it:
            return
        name = it.data(Qt.UserRole)
        if QMessageBox.question(self, "Delete profile", f"Delete the profile <b>{name}</b>?") != QMessageBox.Yes:
            return
        try:
            self.config = self.client.call("delete_profile", name=name)["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "Could not delete", str(e))
            return
        self.refresh()

    def _save_critical(self):
        try:
            self.config = self.client.call("set_critical", cpu_temp=self.crit_cpu.value(),
                                           gpu_temp=self.crit_gpu.value())["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "Could not save", str(e))
            return
        self._load_limits(force=True)
        self.crit_save.setText("Saved ✓")
        QTimer.singleShot(2500, lambda: self.crit_save.setText("Save limits"))

    def _update_selftest(self):
        t = self.status.get("selftest")
        running = bool(t and t["state"] == "running")
        busy_cal = bool((self.status.get("calibration") or {}).get("state") == "running")
        busy_map = bool((self.status.get("mapping") or {}).get("state") == "running")
        self.test_btn.setEnabled(self.writable and not running and not busy_cal and not busy_map
                                 and not self.status.get("critical"))
        self.test_cancel.setVisible(running)
        self.test_bar.setVisible(running)
        if not t:
            self.test_step.setText("Not run yet in this session.")
            return
        self.test_bar.setValue(int(t["progress"] * 100))
        if running:
            self.test_step.setText(f"Running: {t['step']}…")
        else:
            when = time.strftime("%H:%M", time.localtime(t["started"]))
            verdict = "all checks passed ✔" if t["failed"] == 0 else f"{t['failed']} check(s) FAILED ✖"
            self.test_step.setText(f"<b>Self-test at {when}: {t['passed']} passed, {verdict}</b>")
        key = (t["state"], len(t["results"]))
        if key == self._test_shown:
            return
        self._test_shown = key
        self.test_results.clear()
        for r in t["results"]:
            mark, color = {True: ("✔", "#27ae60"), False: ("✖", "#da4453"), None: ("–", None)}[r["ok"]]
            it = QListWidgetItem(f"{mark}  {r['name']}" + (f"  —  {r['detail']}" if r["detail"] else ""))
            if color:
                it.setForeground(QColor(color))
            self.test_results.addItem(it)
        self.test_results.scrollToBottom()

    def _update_mapping(self):
        mp = self.status.get("mapping")
        running = bool(mp and mp["state"] == "running")
        if getattr(self, "_map_was_running", False) and not running:
            try:                                  # the finished map is in the config now: show Forget at once
                self.config = self.client.call("get_config")["config"]
            except DaemonError:
                pass
        self._map_was_running = running
        if mp and mp.get("started") == getattr(self, "_map_forgotten", None):
            mp = None                             # results of a map that was forgotten since
        busy = any((self.status.get(k) or {}).get("state") == "running" for k in ("selftest", "calibration"))
        self.map_btn.setEnabled(self.writable and not running and not busy and bool(self.status.get("board_chip"))
                                and not self.status.get("critical") and "mapping" in self.status)
        self.map_cancel.setVisible(running)
        self.map_bar.setVisible(running)
        self.map_forget.setVisible(bool(self.config.get("fan_map")) and not running)
        self.map_forget.setEnabled(self.writable)
        if not mp:
            self.map_text.setText("Not needed on boards FanPilot knows; useful when fan numbers don't match "
                                  "the board's headers.")
            return
        self.map_bar.setValue(int(mp["progress"] * 100))
        self.map_text.setText(f"{mp['step']}…" if running else "<br>".join(mp["details"]) or mp["step"])

    def _forget_map(self):
        try:
            self.config = self.client.call("map_forget")["config"]
        except DaemonError as e:
            QMessageBox.warning(self, "FanPilot", str(e))
            return
        self._map_forgotten = (self.status.get("mapping") or {}).get("started")
        self.refresh()

    def _crit_ids(self):
        st = self.status
        if "cpu_sensor" in st:
            return st.get("cpu_sensor"), st.get("gpu_sensor")
        return "k10temp/temp1", "amdgpu/temp2"    # a service older than 0.6 used these fixed ids

    def _cpu_protected(self, fid) -> bool:
        if "cpu_protected" in self.status:
            return fid in self.status["cpu_protected"]
        return fid.endswith("/pwm1")              # a service older than 0.6 (its own rule)

    def _run_mapping(self):
        if QMessageBox.question(
                self, "Map my fans",
                "For about 15 seconds per header, all motherboard fans run at full speed while one at a time "
                "is slowed to 30 % (no fan is stopped). It stops by itself if the CPU gets warm. Leave the PC "
                "idle. Every fan goes back to its setting afterwards.<br><br>Start now?") != QMessageBox.Yes:
            return
        try:
            self.client.call("map_fans")
        except DaemonError as e:
            QMessageBox.warning(self, "Could not start", str(e))
            return
        self.refresh()

    def _run_selftest(self):
        if QMessageBox.question(
                self, "Self-test",
                "Run the self-test now?<br><br>For about 45 seconds the system fans and the graphics card fan "
                "will change speed (you will hear them). The CPU fan is not touched. Your settings are "
                "restored afterwards.") != QMessageBox.Yes:
            return
        try:
            self.client.call("selftest")
        except DaemonError as e:
            QMessageBox.warning(self, "Could not start the self-test", str(e))
            return
        self.refresh()

    def _call_quiet(self, cmd, **kw):
        try:
            self.client.call(cmd, **kw)
        except DaemonError as e:
            QMessageBox.warning(self, "FanPilot", str(e))
        self.refresh()

    def _identify(self, fan_id):
        try:
            self.client.call("identify", fan=fan_id)
        except DaemonError as e:
            QMessageBox.warning(self, "Could not identify", str(e))

    def _on_alarm(self, msg):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  ALARM  {msg}")
        if self.tray:
            self.tray.showMessage("FanPilot alarm", msg, QSystemTrayIcon.Warning)

    # -- window / tray
    def _show(self):
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)   # keeps "maximized" if it was
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._save_geometry()
        self.quitting = True
        self.sensors_view.stop_logging()
        if self._poller:
            self._poller.stop()
        QApplication.quit()

    def closeEvent(self, e):
        self._save_geometry()
        if self.tray and not self.quitting:
            e.ignore()
            self.hide()
            if not getattr(self, "_told", False):
                self._told = True
                self.tray.showMessage("FanPilot", "Still running in the tray. Fan control continues "
                                      "in the background service either way.", QSystemTrayIcon.Information)
        else:
            e.accept()
            self.timer.stop()
            self.sensors_view.stop_logging()
            if self._poller:
                self._poller.stop()
            if hasattr(self.client, "close"):
                self.client.close()
            if not self.tray and not self.quitting:
                QApplication.quit()       # no tray to come back from: closing the window ends the app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fanpilot")
    ap.add_argument("--socket", default=SOCKET_PATH)
    ap.add_argument("--hidden", action="store_true", help="start in the tray")
    ap.add_argument("--screenshot", help="render the window to this PNG and exit (testing)")
    ap.add_argument("--tab", type=int, default=0)
    ap.add_argument("--select", type=int, default=None, help="fan row to select (testing)")
    a, qt_args = ap.parse_known_args(argv)
    app = QApplication(sys.argv[:1] + qt_args)
    app.setApplicationName("FanPilot")
    app.setDesktopFileName("fanpilot")
    app.setQuitOnLastWindowClosed(False)
    server = None
    if not a.screenshot:
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        import hashlib
        # one window per user per service socket (tests / a second service don't collide)
        key = f"fanpilot-gui-{os.getuid()}-{hashlib.sha1(a.socket.encode()).hexdigest()[:8]}"
        rt = os.environ.get("XDG_RUNTIME_DIR")
        if rt and os.path.isdir(rt):
            key = os.path.join(rt, key)  # private to this user; a bare name would live in /tmp
        probe = QLocalSocket()
        probe.connectToServer(key)
        if probe.waitForConnected(300):
            probe.write(b"show")          # already running: bring that window up instead
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            return 0
        QLocalServer.removeServer(key)   # stale socket from a crashed instance
        server = QLocalServer()
        server.listen(key)
        if rt and os.path.isdir(rt):
            try:   # tells install.sh "this window updates itself, don't kill it" (same pid after execv)
                with open(os.path.join(rt, "fanpilot-gui.pid"), "w") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass
    import PySide6
    if sys.version_info < (3, 12) and PySide6.__version__ == "6.12.0" and not a.screenshot:
        # measured: every Qt call returning None drops a reference to None; Python < 3.12 then aborts
        msg = (f"PySide6 6.12.0 has a bug that crashes apps on Python {sys.version_info.major}."
               f"{sys.version_info.minor} after a while. Install PySide6 6.11 (sudo ./install.sh --gui-venv "
               "does). Fan control is not affected: it runs in the service.")
        print("FanPilot: " + msg, file=sys.stderr)
        if not a.hidden:                       # not at every login: the log line is enough then
            QMessageBox.warning(None, "FanPilot", msg)
    w = MainWindow(a.socket)
    if server is not None:
        server.newConnection.connect(lambda: (server.nextPendingConnection(), w.show_or_update()))
    if a.select is not None:
        w.fan_list.setCurrentRow(a.select)
    w.tabs.setCurrentIndex(a.tab)
    if a.screenshot:
        app.setQuitOnLastWindowClosed(True)
        w.show()

        def shot():
            w.refresh()
            w.grab().save(a.screenshot)
            app.quit()
        QTimer.singleShot(2500, shot)
    elif not a.hidden or not w.tray:
        w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
