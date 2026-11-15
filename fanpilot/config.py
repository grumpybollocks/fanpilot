# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration: defaults, strict validation and atomic save."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile

from .hw import AMD_PWM_RE, PMFW_FAN_RE

MODES = ("auto", "fixed", "curve")

DEFAULT_FAN = {
    "mode": "auto",
    "fixed": 50,
    "curve": [[40, 30], [60, 45], [75, 70], [85, 100]],
    "source": ["auto:cpu"],       # "auto:cpu" / "auto:gpu" = this machine's CPU / GPU temperature
    "min_duty": 20,
    "allow_stop": False,
    "hysteresis": 2.0,
    "smoothing": 3.0,
    "ramp_up": 50.0,
    "ramp_down": 4.0,
}

DEFAULT_GPU = {
    "mode": "auto",
    "fixed": 40,
    "curve": [[40, 25], [60, 35], [75, 55], [85, 80], [95, 100]],
    "zero_rpm": True,
}

DEFAULT = {
    "version": 1,
    "interval": 1.0,
    "allowed_uids": [],
    "critical": {
        "cpu_sensor": "auto",       # "auto" = chosen from the hardware (hw.default_cpu_sensor)
        "cpu_temp": 90,
        "gpu_sensor": "auto",
        "gpu_temp": 100,
    },
    "fans": {},
    "calibration": {},
    "profiles": {},
    "active_profile": None,
    "cpu_fans": [],               # fans you marked as CPU fans (adds to the board database)
    "names": {},                  # your own names for fans and sensors: id -> name
    "fan_map": {},                # 'Map my fans' result: header -> "fanN_input" (its tachometer) or None
}

FAN_ID_RE = re.compile(r"^[a-z0-9_]+(?:@[A-Za-z0-9:._-]{1,40})?(?:#\d+)?/(pwm|fan)\d+$")
ITEM_ID_RE = re.compile(r"^[a-z0-9_:]+(?:@[A-Za-z0-9:._-]{1,40})?(?:/[a-z]+\d+)?$")
MAX_NAME = 40
PROFILE_NAME_RE = re.compile(r"^[\w][\w .()+-]{0,31}$")
MAX_PROFILES = 20

CPU_FAN_FLOOR = 20  # a CPU fan never stops and never goes below this (which fans: hw.is_cpu_fan)
NONSTOP_FLOOR = 10  # a fan not allowed to stop never gets less than this


class ConfigError(ValueError):
    pass


def _num(d, key, lo, hi, what):
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{what}.{key} must be a number")
    if not lo <= v <= hi:
        raise ConfigError(f"{what}.{key}={v} outside {lo}..{hi}")
    return float(v) if isinstance(v, float) else v


def _curve(c, what, max_points=8):
    if not isinstance(c, list) or not 1 <= len(c) <= max_points:
        raise ConfigError(f"{what}.curve needs 1..{max_points} points")
    pts = []
    for p in c:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in p)):
            raise ConfigError(f"{what}.curve points must be [temp, duty]")
        t, d = p
        if not 0 <= t <= 110 or not 0 <= d <= 100:
            raise ConfigError(f"{what}.curve point {p} out of range (0-110 °C, 0-100 %)")
        pts.append([t, d])
    for a, b in zip(pts, pts[1:]):
        if b[0] <= a[0]:
            raise ConfigError(f"{what}.curve temperatures must increase")
        if b[1] < a[1]:
            raise ConfigError(f"{what}.curve speeds must not go down as temperature rises")
    return pts


def fan_defaults(fan_id: str) -> dict:
    if PMFW_FAN_RE.match(fan_id or ""):
        return copy.deepcopy(DEFAULT_GPU)
    d = copy.deepcopy(DEFAULT_FAN)
    if AMD_PWM_RE.match(fan_id or ""):
        d["source"] = ["auto:own"]            # an older AMD card follows its own temperature
        d["curve"] = copy.deepcopy(DEFAULT_GPU["curve"])
    return d


def validate_fan(fid: str, f, cpu_fan: bool = False) -> dict:
    """Validate one fan's settings and return the normalised copy."""
    if not isinstance(fid, str) or not FAN_ID_RE.fullmatch(fid):
        raise ConfigError(f"invalid fan id {fid!r}")
    if not isinstance(f, dict):
        raise ConfigError(f"fans.{fid} must be an object")
    what = f"fans.{fid}"
    nf = {**fan_defaults(fid), **f}
    if nf["mode"] not in MODES:
        raise ConfigError(f"{what}.mode must be one of {MODES}")
    nf["fixed"] = _num(nf, "fixed", 0, 100, what)
    nf["curve"] = _curve(nf["curve"], what)
    if PMFW_FAN_RE.match(fid):
        if not isinstance(nf["zero_rpm"], bool):
            raise ConfigError(f"{what}.zero_rpm must be true/false")
        return {k: nf[k] for k in DEFAULT_GPU}
    src = nf["source"]
    if isinstance(src, str):
        src = [src]
    if (not isinstance(src, list) or not 1 <= len(src) <= 8
            or not all(isinstance(x, str) and len(x) < 64 for x in src)):
        raise ConfigError(f"{what}.source must be a list of 1-8 sensor ids")
    nf["source"] = src
    nf["min_duty"] = _num(nf, "min_duty", 0, 100, what)
    if not isinstance(nf["allow_stop"], bool):
        raise ConfigError(f"{what}.allow_stop must be true/false")
    nf["hysteresis"] = _num(nf, "hysteresis", 0, 15, what)
    nf["smoothing"] = _num(nf, "smoothing", 0, 30, what)
    nf["ramp_up"] = _num(nf, "ramp_up", 1, 100, what)
    nf["ramp_down"] = _num(nf, "ramp_down", 0.5, 100, what)
    if cpu_fan:
        # the CPU fan must never stop and never drop below the floor
        nf["allow_stop"] = False
        nf["min_duty"] = max(nf["min_duty"], CPU_FAN_FLOOR)
    elif not nf["allow_stop"]:
        nf["min_duty"] = max(nf["min_duty"], NONSTOP_FLOOR)
    return {k: nf[k] for k in DEFAULT_FAN}


def validate_fans(fans, what="fans", is_cpu_fan=None) -> dict:
    if not isinstance(fans, dict) or len(fans) > 32:
        raise ConfigError(f"{what} must be an object with at most 32 fans")
    return {fid: validate_fan(fid, f, bool(is_cpu_fan and is_cpu_fan(fid))) for fid, f in fans.items()}


def validate(cfg: dict, is_cpu_fan=None) -> dict:
    """Return a normalised copy of ``cfg`` or raise ConfigError. ``is_cpu_fan(fid)`` (from the hardware)
    decides which fans get the CPU fan's floor; fans marked in ``cpu_fans`` always do."""
    if not isinstance(cfg, dict):
        raise ConfigError("config must be an object")
    out = copy.deepcopy(DEFAULT)
    if cfg.get("version", 1) != 1:
        raise ConfigError("unsupported config version")
    if "interval" in cfg:
        out["interval"] = _num(cfg, "interval", 0.5, 5, "config")
    uids = cfg.get("allowed_uids", [])
    if not isinstance(uids, list) or not all(isinstance(u, int) and not isinstance(u, bool) and u >= 0 for u in uids):
        raise ConfigError("allowed_uids must be a list of user ids")
    out["allowed_uids"] = uids

    crit = cfg.get("critical", {})
    if not isinstance(crit, dict):
        raise ConfigError("critical must be an object")
    merged = {**out["critical"], **{k: v for k, v in crit.items() if k in out["critical"]}}
    for k in ("cpu_sensor", "gpu_sensor"):
        if not isinstance(merged[k], str):
            raise ConfigError(f"critical.{k} must be a sensor id")
    merged["cpu_temp"] = _num(merged, "cpu_temp", 70, 95, "critical")
    merged["gpu_temp"] = _num(merged, "gpu_temp", 80, 105, "critical")
    out["critical"] = merged

    marks = cfg.get("cpu_fans", [])
    if not isinstance(marks, list) or len(marks) > 32 or not all(isinstance(x, str) and FAN_ID_RE.fullmatch(x)
                                                                 for x in marks):
        raise ConfigError("cpu_fans must be a list of fan ids")
    out["cpu_fans"] = sorted(set(marks))
    names = cfg.get("names", {})
    if not isinstance(names, dict) or len(names) > 200:
        raise ConfigError("names must be an object (id -> name)")
    for k, v in names.items():
        if not (isinstance(k, str) and ITEM_ID_RE.fullmatch(k) and isinstance(v, str)
                and 1 <= len(v.strip()) <= MAX_NAME and v.isprintable()):
            raise ConfigError(f"invalid name for {k!r}: 1-{MAX_NAME} printable characters")
    out["names"] = {k: v.strip() for k, v in names.items()}
    fmap = cfg.get("fan_map", {})
    if not isinstance(fmap, dict) or len(fmap) > 32 or not all(
            isinstance(k, str) and FAN_ID_RE.fullmatch(k) and (v is None or (isinstance(v, str)
                                                                             and re.fullmatch(r"fan\d+_input", v)))
            for k, v in fmap.items()):
        raise ConfigError("fan_map must map fan ids to 'fanN_input' or null")
    out["fan_map"] = dict(fmap)
    # is_cpu_fan(fid, marks): the hardware's answer given the fans marked here (marks narrow "unknown")
    cpu = (lambda fid: bool(is_cpu_fan(fid, out["cpu_fans"]))) if is_cpu_fan else \
        (lambda fid: fid in out["cpu_fans"])  # noqa: E731
    out["fans"] = validate_fans(cfg.get("fans", {}), is_cpu_fan=cpu)

    cal = cfg.get("calibration", {})
    if not isinstance(cal, dict) or not all(isinstance(k, str) and FAN_ID_RE.fullmatch(k) and isinstance(v, dict)
                                            for k, v in cal.items()):
        raise ConfigError("calibration must map fan ids to results")
    out["calibration"] = cal

    profiles = cfg.get("profiles", {})
    if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
        raise ConfigError(f"profiles must be an object with at most {MAX_PROFILES} entries")
    for name, fans in profiles.items():
        if not isinstance(name, str) or not PROFILE_NAME_RE.fullmatch(name):
            raise ConfigError(f"invalid profile name {name!r}")
        out["profiles"][name] = validate_fans(fans, f"profiles.{name}", is_cpu_fan=cpu)
    active = cfg.get("active_profile")
    if active is not None and not isinstance(active, str):
        raise ConfigError("active_profile must be a name or null")
    out["active_profile"] = active
    return out


def load(path: str, is_cpu_fan=None) -> dict:
    try:
        with open(path) as f:
            return validate(json.load(f), is_cpu_fan)
    except FileNotFoundError:
        return validate({}, is_cpu_fan)


def save(path: str, cfg: dict, is_cpu_fan=None) -> None:
    cfg = validate(cfg, is_cpu_fan)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
