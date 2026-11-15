# SPDX-License-Identifier: GPL-3.0-or-later
"""RDNA3/RDNA4 GPU fan control via the PMFW OverDrive ``gpu_od/fan_ctrl`` files.

Rules (docs/RESEARCH.md §3, kernel docs + CoolerControl source):
* stage zero-RPM and *every* curve point, then commit once with ``c``;
* speeds are percent, clamped to the advertised range;
* if staging fails after the first point, write ``r`` so no half table stays staged;
* only touch zero-RPM when its file advertises ``FAN_ZERO_RPM_ENABLE:``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .curves import interpolate

_POINT = re.compile(r"^\s*(\d+):\s+(\d+)C\s+(\d+)%")
_T_RANGE = re.compile(r"FAN_CURVE\(hotspot temp\):\s+(\d+)C\s+(\d+)C")
_D_RANGE = re.compile(r"FAN_CURVE\(fan speed\):\s+(\d+)%\s+(\d+)%")


class GpuError(Exception):
    pass


@dataclass
class CurveInfo:
    points: list[tuple[int, int]]
    temp_range: tuple[int, int]
    duty_range: tuple[int, int]
    zero_rpm_supported: bool
    zero_rpm: bool | None


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _write(path: str, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)


def read_info(fan_ctrl_dir: str) -> CurveInfo:
    txt = _read(os.path.join(fan_ctrl_dir, "fan_curve"))
    points, tr, dr = [], None, None
    for line in txt.splitlines():
        if m := _POINT.match(line):
            points.append((int(m.group(2)), int(m.group(3))))
        elif m := _T_RANGE.search(line):
            tr = (int(m.group(1)), int(m.group(2)))
        elif m := _D_RANGE.search(line):
            dr = (int(m.group(1)), int(m.group(2)))
    if not points or tr is None or dr is None:
        raise GpuError("fan_curve has no points or no OD_RANGE (is OverDrive enabled?)")
    zr_path = os.path.join(fan_ctrl_dir, "fan_zero_rpm_enable")
    zr_sup, zr = False, None
    try:
        ztxt = _read(zr_path)
        if "FAN_ZERO_RPM_ENABLE:" in ztxt:
            zr_sup = True
            lines = [l.strip() for l in ztxt.splitlines()]
            i = lines.index("FAN_ZERO_RPM_ENABLE:")
            zr = lines[i + 1] == "1"
    except (OSError, ValueError, IndexError):
        pass
    return CurveInfo(points, tr, dr, zr_sup, zr)


def build_points(info: CurveInfo, curve: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """Map a user curve (any number of points) onto the firmware's N points."""
    n = len(info.points)
    tmin, tmax = info.temp_range
    dmin, dmax = info.duty_range
    clamp_t = lambda t: int(max(tmin, min(tmax, round(t))))
    clamp_d = lambda d: int(max(dmin, min(dmax, round(d))))
    curve = [(float(t), float(d)) for t, d in sorted(curve)]
    if len(curve) == 1:
        curve = [(tmin, curve[0][1]), (tmax, curve[0][1])]
    if len(curve) <= n:
        # keep the user's points exactly; pad by splitting the widest gap
        pts = list(curve)
        while len(pts) < n:
            gaps = [(pts[i + 1][0] - pts[i][0], i) for i in range(len(pts) - 1)]
            _, i = max(gaps)
            t = (pts[i][0] + pts[i + 1][0]) / 2
            pts.insert(i + 1, (t, interpolate(curve, t)))
    else:
        t0, t1 = curve[0][0], curve[-1][0]
        pts = [(t0 + (t1 - t0) * i / (n - 1),) for i in range(n)]
        pts = [(t, interpolate(curve, t)) for (t,) in pts]
    out = [(clamp_t(t), clamp_d(d)) for t, d in pts]
    # speeds never fall; temperatures strictly rise (points clamped onto the same temperature are
    # spread 1 °C apart: distinct temperatures are what the card is proven to accept)
    for i in range(1, n):
        out[i] = (max(out[i][0], out[i - 1][0] + 1), max(out[i][1], out[i - 1][1]))
    for i in range(n - 1, -1, -1):
        cap = tmax if i == n - 1 else out[i + 1][0] - 1
        if out[i][0] > cap:
            out[i] = (cap, out[i][1])
    return out


def flat_points(info: CurveInfo, duty: float) -> list[tuple[int, int]]:
    tmin, tmax = info.temp_range
    return build_points(info, [(tmin, duty), (tmax, duty)])


def apply(fan_ctrl_dir: str, points: list[tuple[int, int]], zero_rpm: bool | None) -> None:
    """Stage zero-RPM (if supported and requested) and all points, then commit once."""
    info = read_info(fan_ctrl_dir)
    if len(points) != len(info.points):
        raise GpuError(f"need exactly {len(info.points)} points, got {len(points)}")
    curve_path = os.path.join(fan_ctrl_dir, "fan_curve")
    staged = False
    if zero_rpm is not None and info.zero_rpm_supported:
        try:
            _write(os.path.join(fan_ctrl_dir, "fan_zero_rpm_enable"), "1\n" if zero_rpm else "0\n")
            staged = True
        except OSError as e:
            if zero_rpm:
                raise GpuError(f"could not enable zero-RPM: {e}") from e
            # a spinning fan is the safe side: carry on with the curve
    for i, (t, d) in enumerate(points):
        try:
            _write(curve_path, f"{i} {t} {d}\n")
        except OSError as e:
            if i > 0 or staged:
                reset(fan_ctrl_dir)  # never leave a half-staged table behind
            raise GpuError(f"writing point '{i} {t} {d}' failed: {e}") from e
    try:
        _write(curve_path, "c\n")
    except OSError as e:
        reset(fan_ctrl_dir)
        hint = " (firmware rejected the table, see 'journalctl -k')" if e.errno == 5 else ""
        raise GpuError(f"commit failed: {e}{hint}") from e


def reset(fan_ctrl_dir: str) -> list[str]:
    """Hand the fan back to firmware automatic mode. Returns errors (never raises)."""
    errors = []
    # curve first: its restore also commits, which clears any half-staged table
    try:
        _write(os.path.join(fan_ctrl_dir, "fan_curve"), "r\n")
    except OSError as e:
        errors.append(f"curve reset: {e}")
    zr = os.path.join(fan_ctrl_dir, "fan_zero_rpm_enable")
    try:
        if "FAN_ZERO_RPM_ENABLE:" in _read(zr):
            _write(zr, "r\n")
    except OSError as e:
        errors.append(f"zero-RPM reset: {e}")
    return errors
