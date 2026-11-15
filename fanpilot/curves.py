# SPDX-License-Identifier: GPL-3.0-or-later
"""Curve math: interpolation, smoothing, hysteresis and ramp limiting."""
from __future__ import annotations


def interpolate(curve, temp: float) -> float:
    """Linear interpolation on sorted (temp, duty) points, flat beyond the ends."""
    pts = sorted(curve)
    if not pts:
        raise ValueError("empty curve")
    if temp <= pts[0][0]:
        return float(pts[0][1])
    if temp >= pts[-1][0]:
        return float(pts[-1][1])
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return float(max(d0, d1))
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
    return float(pts[-1][1])


class FanState:
    """Per-fan control state: EMA smoothing → deadband hysteresis → curve → ramp."""

    def __init__(self):
        self.smoothed: float | None = None
        self.effective: float | None = None
        self.duty: float | None = None

    def reset(self):
        self.smoothed = self.effective = self.duty = None

    def step_temp(self, temp: float, dt: float, smoothing_s: float, hysteresis: float) -> float:
        # exponential moving average with time constant ``smoothing_s``
        if self.smoothed is None or smoothing_s <= 0:
            self.smoothed = temp
        else:
            alpha = min(1.0, dt / smoothing_s)
            self.smoothed += alpha * (temp - self.smoothed)
        t = self.smoothed
        # deadband: rise immediately, fall only once past the band
        if self.effective is None or t > self.effective:
            self.effective = t
        elif t < self.effective - hysteresis:
            self.effective = t + hysteresis
        return self.effective

    def step_duty(self, target: float, dt: float, ramp_up: float, ramp_down: float) -> float:
        """Limit the change rate (percent per second). Returns the new duty."""
        if self.duty is None:
            self.duty = target
        elif target > self.duty:
            self.duty = min(target, self.duty + ramp_up * dt)
        else:
            self.duty = max(target, self.duty - ramp_down * dt)
        return self.duty


def duty_to_pwm(duty: float) -> int:
    return max(0, min(255, round(duty * 255 / 100)))


def pwm_to_duty(pwm: int) -> float:
    return pwm * 100 / 255
