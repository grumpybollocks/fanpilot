# SPDX-License-Identifier: GPL-3.0-or-later
"""fanpilotd — the root daemon. Standard library only.

Safety rules are listed in docs/PLAN.md; the comments tag them as [S1]..[S9].
"""
from __future__ import annotations

import argparse
import collections
import grp
import pwd
import json
import logging
import os
import signal
import socket
import socketserver
import struct
import threading
import time

from . import CONFIG_PATH, RUN_DIR, SOCKET_PATH, __version__, amdgpu, export, nvidia, presets
from . import config as cfgmod
from .curves import FanState, duty_to_pwm, interpolate, pwm_to_duty
from .hw import GPU_FAN_ID, BoardFan, GpuFan, Sensor, diagnose, discover
from .readings import Readings
from .mapping import FanMapping
from .selftest import SelfTest

log = logging.getLogger("fanpilotd")

FAIL_TICKS = 3            # [S3] unreadable source for this many ticks -> 100 %
CRIT_RELEASE = 5.0        # [S4] critical override releases this far below the limit
RECLAIM_LIMIT = 5         # [S6] reclaims within RECLAIM_WINDOW -> give up, back to auto
RECLAIM_WINDOW = 60.0
GPU_VERIFY_EVERY = 30.0
RESCAN_EVERY = 10.0       # look again for hardware that is missing (late driver, module reload)
CAL_ABORT_CPU = 80.0      # calibration aborts above this CPU temperature
GPU_RETRY_AFTER = 60.0    # back-off after the GPU firmware rejected a curve
GPU_MAX_FAILS = 3         # then give the GPU fan back to the card
TICK_FAIL_LIMIT = 3       # consecutive broken ticks -> restore + exit so systemd restarts us
MAX_CLIENTS = 64          # socket connections in total ...
PER_UID_CLIENTS = 16      # ... and per user, so one user can't lock everyone else out
IDLE_TIMEOUT = 30.0       # an idle connection is closed after this long
GPU_REPAIR_LIMIT = 3      # re-apply a GPU curve changed from outside at most this often ...
GPU_REPAIR_WINDOW = 3600.0  # ... per hour
IDENTIFY_SECONDS = 6.0   # 'identify': first half slow, second half full speed
IDENTIFY_LOW = 25.0      # BIOS often already runs fans near 100 % (reference machine), so a pulse is what you notice
STALL_SECONDS = 10.0     # fan driven >= STALL_DUTY but reading 0 rpm this long -> stalled
STALL_DUTY = 15.0        # real SYS fan: 25 % = 391 rpm, so a 0 above this is suspicious


# --------------------------------------------------------------------------- restore file

def _chip_name(enable_path: str) -> str | None:
    try:
        with open(os.path.join(os.path.dirname(enable_path), "name")) as f:
            return f.read().strip()
    except OSError:
        return None


def _atomic_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write(path: str, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)


def safe_enable_value(orig) -> int:
    """Value to restore into pwmN_enable: exactly what was there. 0 = full speed, 2 and up = the chip's
    own automatic modes (nct6775: 5 = Smart Fan IV, the usual BIOS mode; writing 2 there would switch the
    BIOS to Thermal Cruise). Only 1 (manual) is not restored, since it would freeze the fan at FanPilot's
    last duty: it becomes 2 (automatic)."""
    if isinstance(orig, int) and not isinstance(orig, bool) and 0 <= orig <= 9 and orig != 1:
        return orig
    return 2


def restore_enable(write, orig) -> None:
    """Write the restore value; a chip that refuses it (EINVAL: e.g. in-tree it87 without automatic
    mode) gets 0 = full speed. Never leaves a fan in manual mode. Raises only if both writes fail."""
    try:
        write(str(safe_enable_value(orig)))
    except OSError:
        write("0")


ADMIN_GROUP = "fanpilot"     # members may change settings (distribution packages create it); see in_group


def in_group(uid: int, group: str) -> bool:
    """Is this user a member of ``group`` (primary or supplementary)? Looked up per request, so adding
    someone to the group works without restarting the service. False if the group doesn't exist."""
    try:
        g = grp.getgrnam(group)
        p = pwd.getpwuid(uid)
    except KeyError:
        return False
    return p.pw_gid == g.gr_gid or p.pw_name in g.gr_mem


def gpu_dirs(data: dict) -> list[str]:
    """Firmware-curve dirs FanPilot changed: "gpu_fan_ctrls" (any number of cards) and the single
    "gpu_fan_ctrl" key older versions wrote."""
    dirs = [d for d in (data.get("gpu_fan_ctrls") or []) if isinstance(d, str)]
    old = data.get("gpu_fan_ctrl")
    if isinstance(old, str) and old not in dirs:
        dirs.append(old)
    return dirs


class GpuState:
    """Per graphics card: what was applied, repairs, failures."""
    def __init__(self):
        self.applied = None
        self.repairs: collections.deque = collections.deque()
        self.next_verify = 0.0
        self.error: str | None = None
        self.fails = 0
        self.retry_at = 0.0
        self.logged = None
        self.repair_paused = False
        self.ranges = None


class NvSensor(Sensor):
    """A temperature read from the nvidia-smi snapshot instead of a file."""
    def __init__(self, sid, label, getter):
        super().__init__(sid, label, "")
        self._get = getter

    def read(self):
        return self._get()


def restore_from_file(restore_file: str) -> list[str]:
    """[S1] Give every fan we touched back to the firmware. Used on clean exit
    and by systemd ExecStopPost after a crash. Never raises."""
    errors: list[str] = []
    try:
        with open(restore_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        return errors
    except (OSError, ValueError) as e:
        return [f"restore file unreadable: {e}"]
    for path, entry in (data.get("pwm_enable") or {}).items():
        orig, name = (entry.get("orig"), entry.get("name")) if isinstance(entry, dict) else (entry, None)
        if name is not None:
            # hwmonN numbers can change if the driver was reloaded: only write to the same chip
            try:
                with open(os.path.join(os.path.dirname(path), "name")) as f:
                    if f.read().strip() != name:
                        errors.append(f"{path}: now belongs to another chip, skipped")
                        continue
            except OSError:
                continue  # chip gone: nothing to restore
        try:
            restore_enable(lambda v, p=path: _write(p, v), orig)
        except OSError as e:
            errors.append(f"{path}: {e}")
    for gpu in gpu_dirs(data):
        errors += amdgpu.reset(gpu)
    if not errors:
        try:
            if data.get("vanished"):
                # a fan's chip is gone right now (driver reload): keep its BIOS mode for when it returns
                _atomic_json(restore_file, {"pwm_enable": {}, "gpu_fan_ctrls": [], "vanished": data["vanished"]})
            else:
                os.unlink(restore_file)
        except OSError:
            pass
    return errors


# --------------------------------------------------------------------------- other fan tools

# process name -> what it is; each of these can also write fan settings (seen on the reference machine: LACT's daemon
# re-applies its GPU settings on every drm 'change' event, which reset FanPilot's GPU curve once)
OTHER_FAN_TOOLS = {"lact": "LACT", "corectrl": "CoreCtrl", "corectrl_helper": "CoreCtrl",
                   "fancontrol": "lm_sensors fancontrol", "amdgpu-fan": "amdgpu-fan",
                   "coolercontrold": "CoolerControl", "fan2go": "fan2go"}


def other_fan_tools(proc="/proc") -> list[str]:
    found = set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name in OTHER_FAN_TOOLS:
            found.add(OTHER_FAN_TOOLS[name])
    return sorted(found)


# --------------------------------------------------------------------------- systemd

def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


def _boottime() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


# --------------------------------------------------------------------------- calibration

class Calibration:
    """Steps one board fan down to find where it stops and where it restarts."""

    def __init__(self, fan: BoardFan, is_cpu_fan: bool):
        self.fan = fan
        self.floor = cfgmod.CPU_FAN_FLOOR if is_cpu_fan else 0
        self.state = "running"
        self.message = "Spinning up to full speed"
        self.progress = 0.0
        self.table: list[list[float]] = []
        self.result: dict | None = None
        self.next_at = 0.0
        self.duty = 100.0
        self._gen = self._steps()

    def _set(self, duty: float):
        self.duty = duty
        self.fan.set_pwm(duty_to_pwm(duty))

    def _steps(self):
        self._set(100)
        yield 6
        max_rpm = self.fan.rpm() or 0
        if max_rpm <= 0:            # nothing to measure: don't store a 100 % floor for an empty header
            self.abort("No speed reading at full speed: nothing connected, or the fan has no speed wire")
            return
        self.table.append([100, max_rpm])
        duties = list(range(95, self.floor - 1, -5))
        stop = None
        for i, d in enumerate(duties):
            self._set(d)
            self.message = f"Testing {d} %"
            self.progress = 0.1 + 0.7 * i / max(1, len(duties))
            yield 3
            rpm = self.fan.rpm() or 0
            self.table.append([d, rpm])
            if rpm == 0:
                stop = d
                break
        start = None
        if stop is not None:
            for d in range(stop + 5, 101, 5):
                self._set(d)
                self.message = f"Restarting from stop at {d} %"
                yield 4
                if (self.fan.rpm() or 0) > 0:
                    start = d
                    break
        self.result = {
            "max_rpm": max_rpm,
            "stop_duty": stop,
            "start_duty": start,
            # stopped but never restarted (even at 100 %): no low speed is safe
            "min_safe_duty": min(100, (start + 5) if start is not None else 100 if stop is not None else
                                 (self.table[-1][0] if self.table else 20)),
            "table": self.table,
            "time": int(time.time()),
        }
        self.progress = 1.0
        self.state = "done"
        self.message = "Finished"

    def tick(self, now: float) -> None:
        if self.state != "running" or now < self.next_at:
            return
        try:
            wait = next(self._gen)
            self.next_at = now + wait
        except StopIteration:
            pass

    def abort(self, why: str) -> None:
        if self.state == "running":
            self.state = "aborted"
            self.message = why
            try:
                self._set(100)
            except OSError:
                pass

    def info(self) -> dict:
        return {"fan": self.fan.id, "state": self.state, "message": self.message,
                "progress": round(self.progress, 2), "duty": self.duty,
                "table": self.table, "result": self.result}


# --------------------------------------------------------------------------- daemon

class Daemon:
    def __init__(self, root="/", config_path=CONFIG_PATH, run_dir=RUN_DIR, socket_path=SOCKET_PATH):
        self.root = root
        self.config_path = config_path
        self.run_dir = run_dir
        self.socket_path = socket_path
        self.restore_file = os.path.join(run_dir, "restore.json")
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.events: collections.deque = collections.deque(maxlen=200)
        self.hw = discover(root)
        self.readings_src = Readings(root, self.hw)   # HWiNFO-style Sensors view
        self.readings: list = []
        try:
            self.cfg = cfgmod.load(config_path, self.protect_cpu)
        except (cfgmod.ConfigError, ValueError) as e:
            # [S8] a broken config must not stop the daemon: run everything on Auto
            self.cfg = cfgmod.validate({})
            self.event("error", f"Config file invalid, using Auto for all fans: {e}")
        self.states: dict[str, FanState] = collections.defaultdict(FanState)
        self.fail_counts: dict[str, int] = collections.defaultdict(int)
        self.reclaims: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.asserted: set[str] = set()
        self.disabled: dict[str, str] = {}
        self.fan_status: dict[str, dict] = {}
        self.temps: dict[str, float | None] = {}
        self.critical = False
        self.gpu: dict[str, GpuState] = collections.defaultdict(GpuState)
        self.nvidia = nvidia.NvidiaSmi() if root == "/" else None   # tests inject a fake
        self._nv_seen: tuple = ()
        self.stopping = False
        self.conn_lock = threading.Lock()
        self.conns: dict[int, int] = collections.defaultdict(int)   # uid -> open connections
        self.crit_unreadable = 0
        self.crit_seen: set[str] = set()   # emergency sensors that existed at some point
        self.gpu_crit_warned = False
        self.release_failed: set[str] = set()
        self.identify: dict[str, float | None] = {}   # fan id -> end time (None = start next tick)
        self.seen_spinning: set[str] = set()
        self.stall_since: dict[str, float] = {}
        self.stalled: set[str] = set()
        self.calibration: Calibration | None = None
        self.selftest: SelfTest | None = None
        self.mapping: FanMapping | None = None
        self.next_rescan = 0.0
        self.started = time.time()
        self.restore = self._load_restore()
        self._apply_fan_map()
        self._auto_sensor: dict[str, str] = {}
        for kind in ("cpu", "gpu"):        # pick the "auto" emergency sensors now, while all are readable
            self.crit_sensor(kind)

    # -- helpers ---------------------------------------------------------------
    def event(self, level: str, msg: str) -> None:
        self.events.append({"t": time.time(), "level": level, "msg": msg})
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(msg)

    def _load_restore(self) -> dict:
        # [S1] after a crash the file still holds the *original* values
        try:
            with open(self.restore_file) as f:
                data = json.load(f)
            data.setdefault("pwm_enable", {})
            data["gpu_fan_ctrls"] = gpu_dirs(data)       # older single-card files are read too
            data.pop("gpu_fan_ctrl", None)
            return data
        except (OSError, ValueError):
            return {"pwm_enable": {}, "gpu_fan_ctrls": []}

    def _gpu_restore(self, d: str, held: bool) -> None:
        """Record (or drop) a firmware-curve dir in the restore file before (after) changing it."""
        dirs = list(self.restore.get("gpu_fan_ctrls") or [])
        if held and d not in dirs:
            self._save_restore({**self.restore, "gpu_fan_ctrls": dirs + [d]})
        elif not held and d in dirs:
            self._save_restore({**self.restore, "gpu_fan_ctrls": [x for x in dirs if x != d]})

    def _save_restore(self, data: dict | None = None) -> None:
        """Persist ``data`` (default: current state). Only on success does it become
        the in-memory state, so a failed save never leaves an unrecorded takeover."""
        data = self.restore if data is None else data
        os.makedirs(self.run_dir, exist_ok=True)
        _atomic_json(self.restore_file, data)
        self.restore = data

    def _take(self, fan: BoardFan) -> None:
        """Record the original pwm_enable BEFORE the first write to this fan.
        Raises OSError if it cannot be recorded; the caller must then not write."""
        if fan.enable_path not in self.restore["pwm_enable"]:
            vanished = self.restore.get("vanished") or {}
            # a driver reload keeps the chip's registers: what we read now may be OUR manual mode.
            # The BIOS's mode was recorded before the fan's path vanished; reuse it.
            orig = vanished[fan.id] if fan.id in vanished else fan.enable()
            name = None
            try:
                with open(os.path.join(os.path.dirname(fan.enable_path), "name")) as f:
                    name = f.read().strip()
            except OSError:
                pass
            new = json.loads(json.dumps(self.restore))
            new["pwm_enable"][fan.enable_path] = {"orig": orig if orig is not None else 2, "name": name,
                                                  "id": fan.id}
            new.get("vanished", {}).pop(fan.id, None)
            self._save_restore(new)

    def _release(self, fan: BoardFan) -> None:
        entry = self.restore["pwm_enable"].get(fan.enable_path)
        if entry is None:
            return
        orig = entry.get("orig") if isinstance(entry, dict) else entry
        try:
            restore_enable(lambda v: fan.set_enable(int(v)), orig)
            new = json.loads(json.dumps(self.restore))
            del new["pwm_enable"][fan.enable_path]
            self._save_restore(new)
            self.release_failed.discard(fan.enable_path)
        except OSError as e:
            if fan.enable_path not in self.release_failed:
                self.release_failed.add(fan.enable_path)
                self.event("error", f"{fan.label}: could not hand back to BIOS: {e}")
        self.asserted.discard(fan.id)
        self.states[fan.id].reset()

    def fan_cfg(self, fid: str) -> dict:
        return self.cfg["fans"].get(fid) or cfgmod.fan_defaults(fid)

    # -- which fan is the CPU fan, which sensors to watch (any board) --------------
    def protect_cpu(self, fid: str, marks=None) -> bool:
        """Give this fan the CPU fan's protection (never stops, >= 20 %): known CPU fans, and every
        motherboard fan while nobody knows which one is the CPU fan."""
        marks = set(self.cfg.get("cpu_fans", []) if marks is None else marks) & set(self.hw.fans)
        known = self.hw.cpu_fans | marks       # a mark for a fan that no longer exists counts for nothing
        if fid in self.hw.pump_fans:
            return True                        # pumps never stop
        if known:
            return fid in known
        fan = self.hw.fans.get(fid)
        return isinstance(fan, BoardFan) and fan.kind == "board"

    def known_cpu_fan(self, fid: str) -> bool:
        return fid in (self.hw.cpu_fans | self.hw.pump_fans | (set(self.cfg.get("cpu_fans", [])) & set(self.hw.fans)))

    def crit_sensor(self, kind: str) -> str | None:
        """The emergency sensor. "auto" picks one from the hardware ONCE and keeps it: if that sensor
        later vanishes it counts as unreadable (emergency mode), never silently swapped for another."""
        want = self.cfg["critical"][f"{kind}_sensor"]
        if want != "auto":
            return want
        latched = getattr(self, "_auto_sensor", {})
        if latched.get(kind):
            return latched[kind]
        found = self.hw.default_cpu_sensor() if kind == "cpu" else self.hw.default_gpu_sensor()
        nv = getattr(self, "nvidia", None)
        waiting = kind == "gpu" and nv is not None and (nv.pending() if hasattr(nv, "pending") else not nv.updated)
        if found and not waiting:           # an NVIDIA card may still be the real GPU: wait for its first reading
            self._auto_sensor = {**latched, kind: found}
        return found

    def own_sensor(self, fid: str) -> str | None:
        """A graphics card fan's own temperature: its hotspot, else its edge, else any of its sensors."""
        chip = fid.split("/")[0]
        mine = {s.label: sid for sid, s in self.hw.sensors.items() if sid.split("/")[0] == chip}
        for want in ("GPU hotspot", "GPU edge"):
            if want in mine:
                return mine[want]
        return next(iter(mine.values()), None)

    def resolve_sources(self, sources, fid: str | None = None) -> list[str]:
        out = []
        for s in sources:
            r = self.crit_sensor(s[5:]) if s in ("auto:cpu", "auto:gpu") else \
                (self.own_sensor(fid) if s == "auto:own" and fid else None if s == "auto:own" else s)
            if r and r not in out:
                out.append(r)
        return out

    def _apply_fan_map(self, hw=None, release: bool = True) -> None:
        """Use the 'Map my fans' result: each header's real tachometer; headers found empty are hidden,
        but never a CPU fan or pump, never a header the board database lists, never one whose own
        tachometer reads a speed, and never one you set to something other than Automatic."""
        hw = hw or self.hw
        fmap = self.cfg.get("fan_map") or {}
        claimed = {os.path.join(hw.chips.get(f.split("/")[0], ""), t) for f, t in fmap.items() if t}
        for fid, tach in fmap.items():
            chip, ch = fid.split("/")
            hdir = hw.chips.get(chip)
            if hdir is None or chip not in hw.board_chips:
                continue
            fan = hw.fans.get(fid)
            if tach is None:
                own_spins = bool(fan.rpm()) and fan.rpm_path not in claimed if isinstance(fan, BoardFan) else True
                if (isinstance(fan, BoardFan) and self.fan_cfg(fid)["mode"] == "auto" and hw.board is None
                        and not self.protect_cpu(fid) and not own_spins):
                    if release and fan.enable_path in self.restore["pwm_enable"]:
                        self._release(fan)    # never hide a header FanPilot still holds
                    del hw.fans[fid]
                continue
            rpm = os.path.join(hdir, tach)
            if isinstance(fan, BoardFan):
                fan.rpm_path = rpm
            elif os.path.exists(os.path.join(hdir, ch)) and os.path.exists(os.path.join(hdir, f"{ch}_enable")):
                n = ch[3:]
                hw.fans[fid] = BoardFan(fid, f"Fan {n}", os.path.join(hdir, ch),
                                        os.path.join(hdir, f"{ch}_enable"), rpm)
        hw.fans = dict(sorted(hw.fans.items(), key=lambda kv: (kv[1].kind == "gpu", kv[0])))

    def busy_with(self, fid: str) -> str | None:
        for job, flag in ((self.selftest, "testing"), (self.mapping, "mapping")):
            if job and job.state == "running" and fid in job.busy:
                return flag
        return None

    def _named_readings(self) -> list[dict]:
        names = self.cfg.get("names", {})
        if not names:
            return self.readings
        out = []
        for r in self.readings:
            key = r["id"].split(":", 1)[1] if r["id"].startswith(("t:", "fan:", "duty:")) else r["id"]
            name = names.get(r["id"]) or names.get(key)
            if name and r["id"].startswith("duty:") and r["id"] not in names:
                name += " duty"                    # a renamed fan's duty row follows its name
            out.append({**r, "label": name} if name else r)
        return out

    def label(self, item_id: str, default: str) -> str:
        return self.cfg.get("names", {}).get(item_id) or default

    # -- control ----------------------------------------------------------------
    def read_temps(self) -> None:
        self.temps = {sid: s.read() for sid, s in self.hw.sensors.items()}

    def check_critical(self) -> None:
        c = self.cfg["critical"]
        cpu_id, gpu_id = self.crit_sensor("cpu"), self.crit_sensor("gpu")
        cpu = self.temps.get(cpu_id) if cpu_id else None
        gpu = self.temps.get(gpu_id) if gpu_id else None
        self.crit_seen.update(k for k in (cpu_id, gpu_id) if k and k in self.hw.sensors)
        # once seen, a sensor that vanishes (driver gone, rescanned away) is unreadable, not absent
        cpu_here = cpu_id in self.crit_seen
        gpu_here = gpu_id in self.crit_seen
        # fail safe: a CPU sensor that exists but can't be read counts as hot after a few ticks
        self.crit_unreadable = self.crit_unreadable + 1 if cpu is None and cpu_here else 0
        blind = self.crit_unreadable >= FAIL_TICKS
        if gpu is None and gpu_here and not self.gpu_crit_warned:
            self.gpu_crit_warned = True
            self.event("warn", "GPU temperature unreadable: the GPU emergency limit is not active until it reads again")
        elif gpu is not None:
            self.gpu_crit_warned = False
        hot = (cpu is not None and cpu >= c["cpu_temp"]) or (gpu is not None and gpu >= c["gpu_temp"])
        # leave only on real readings: an unreadable sensor while critical keeps it critical
        cool = ((cpu < c["cpu_temp"] - CRIT_RELEASE) if cpu is not None else not cpu_here) and \
               ((gpu < c["gpu_temp"] - CRIT_RELEASE) if gpu is not None else not gpu_here)
        if blind and not self.critical:
            self.critical = True
            self.event("error", "CPU temperature unreadable: all board fans 100 % until it reads again")
        elif hot and not self.critical:
            self.critical = True
            self.event("warn", f"Critical temperature (CPU {cpu} °C, GPU {gpu} °C): all board fans 100 %")
        elif self.critical and cool and not blind:
            self.critical = False
            self.event("info", "Temperatures back to normal, leaving critical mode")

    def control_board_fan(self, fan: BoardFan, dt: float, now: float) -> None:
        fid = fan.id
        fc = self.fan_cfg(fid)
        st = {"id": fid, "label": fan.label, "kind": fan.kind, "control": "pwm", "mode": fc["mode"],
              "own_sensor": self.own_sensor(fid) if fan.kind == "gpu" else None,
              "rpm": fan.rpm(), "duty": None, "target": None, "temp": None, "flags": []}
        if not os.path.exists(fan.enable_path):
            st["flags"].append("missing")
            self.fan_status[fid] = st
            self.next_rescan = 0.0          # driver reloaded / hwmon renumbered: find it again
            return
        p = fan.pwm()
        en = fan.enable()
        # under BIOS control the pwm register is stale (verified on the reference machine): don't report it as a duty
        # mode 0 = full speed (also how it87 shows a manual fan at 255)
        st["duty"] = 100.0 if en == 0 else round(pwm_to_duty(p), 1) if p is not None and en == 1 else None
        st["bios"] = en not in (0, 1)
        self.fan_status[fid] = st

        busy = self.busy_with(fid)
        if busy:
            st["flags"].append(busy)
            self.stall_since.pop(fid, None)
            return
        if self.calibration and self.calibration.fan.id == fid and self.calibration.state == "running":
            st["flags"].append("calibrating")
            self.stall_since.pop(fid, None)
            return
        self._check_stall(fan, st, now)
        identifying = False
        if fid in self.identify:
            if self.identify[fid] is None:
                self.identify[fid] = now + IDENTIFY_SECONDS
            if now < self.identify[fid]:
                identifying = True
                st["flags"].append("identifying")
            else:
                del self.identify[fid]
                self.states[fid].reset()
        if fid in self.disabled:
            st["flags"].append("gave_up")
            st["mode"] = "auto"
            if fan.enable_path in self.restore["pwm_enable"]:
                self._release(fan)
            return
        if fc["mode"] == "auto" and not self.critical and not identifying:
            if fan.enable_path in self.restore["pwm_enable"]:
                self._release(fan)
                self.event("info", f"{fan.label}: handed back to BIOS automatic control")
            return

        state = self.states[fid]
        forced = None
        if self.critical:
            forced = 100.0
            st["flags"].append("critical")
        elif identifying:
            # slow first, then full speed: an obvious change even if the fan was already at 100 %
            low_phase = now < self.identify[fid] - IDENTIFY_SECONDS / 2
            cal_id = self.cfg.get("calibration", {}).get(fid) or {}
            safe_id = cal_id.get("min_safe_duty") if isinstance(cal_id.get("min_safe_duty"), (int, float)) else 0
            forced = max(IDENTIFY_LOW, float(fc["min_duty"]), float(safe_id)) if low_phase else 100.0
        if fc["mode"] == "fixed":
            target = float(fc["fixed"])
        elif fc["mode"] == "curve":
            sources = self.resolve_sources(fc["source"], fid)
            vals = [self.temps.get(s) for s in sources] or [None]   # nothing to follow = unreadable
            if any(v is None for v in vals):
                self.fail_counts[fid] += 1
                if self.fail_counts[fid] == FAIL_TICKS:
                    missing = [s for s, v in zip(sources, vals) if v is None] or list(fc["source"])
                    self.event("warn", f"{fan.label}: sensor {', '.join(missing)} unreadable, fan forced to 100 %")
                if self.fail_counts[fid] >= FAIL_TICKS:
                    forced = 100.0
                    st["flags"].append("sensor_failed")
                target = state.duty if state.duty is not None else 100.0
            else:
                if self.fail_counts[fid] >= FAIL_TICKS:
                    self.event("info", f"{fan.label}: sensor readable again")
                self.fail_counts[fid] = 0
                temp = max(vals)
                st["temp"] = round(temp, 1)
                eff = state.step_temp(temp, dt, fc["smoothing"], fc["hysteresis"])
                target = interpolate(fc["curve"], eff)
        else:  # auto but critical
            target = 100.0

        cpu_fan = self.protect_cpu(fid)
        if target <= 0 and fc["allow_stop"] and not cpu_fan:
            target = 0.0
        else:
            cal = self.cfg.get("calibration", {}).get(fid) or {}
            safe = cal.get("min_safe_duty") if isinstance(cal.get("min_safe_duty"), (int, float)) else 0
            # never ask for less than the measured lowest working speed
            floor = max(float(fc["min_duty"]), cfgmod.NONSTOP_FLOOR, float(safe),
                        cfgmod.CPU_FAN_FLOOR if cpu_fan else 0)
            target = max(target, floor)
            if state.duty is not None and state.duty < floor:
                state.duty = floor   # restarting from a stop: crawling up from 0 % would stay stopped
        if forced is not None:
            duty = forced
            state.duty = forced
        else:
            duty = state.step_duty(target, dt, fc["ramp_up"], fc["ramp_down"])
        st["target"] = round(duty, 1)

        try:
            self._take(fan)
            en = fan.enable()
            want = duty_to_pwm(duty)
            # it87 reports a manual fan whose duty register is 255 as mode 0 "full speed"
            # (it87.c pwm_mode(); seen on the reference machine). That is still our setting, not a reclaim.
            ours_full = en == 0 and fid in self.asserted and fan.pwm() == 255
            if ours_full:
                if want != 255:
                    fan.set_enable(1)
            elif en != 1:
                if fid in self.asserted:
                    # [S6] something (BIOS/SMM/another tool) took the fan back
                    q = self.reclaims[fid]
                    q.append(now)
                    while q and now - q[0] > RECLAIM_WINDOW:
                        q.popleft()
                    self.event("warn", f"{fan.label}: control was taken back (mode {en}), re-asserting")
                    if len(q) >= RECLAIM_LIMIT:
                        self.disabled[fid] = "firmware keeps taking control"
                        self.event("error", f"{fan.label}: firmware keeps reclaiming control; "
                                            "giving it back to BIOS. Check the BIOS Smart Fan setting.")
                        self._release(fan)
                        return
                fan.set_enable(1)
                self.asserted.add(fid)
            fan.set_pwm(want)  # every tick: it87 caches readings
        except OSError as e:
            st["flags"].append("write_error")
            self.event("error", f"{fan.label}: write failed: {e}")

    def _check_stall(self, fan: BoardFan, st: dict, now: float) -> None:
        """Warn when a fan that has been seen spinning reads 0 rpm while driven.
        (An empty header never spins, so it never warns.)"""
        fid, rpm, duty = fan.id, st["rpm"], st["duty"]
        if rpm is None:
            return
        cal = self.cfg.get("calibration", {}).get(fid) or {}
        threshold = STALL_DUTY
        if isinstance(cal.get("stop_duty"), (int, float)):
            threshold = max(threshold, cal["stop_duty"] + 5)
        bios_cpu = st.get("bios") and self.known_cpu_fan(fid)
        if rpm > 0:
            self.seen_spinning.add(fid)
            self.stall_since.pop(fid, None)
            if fid in self.stalled:
                self.stalled.discard(fid)
                self.event("info", f"{fan.label} is spinning again")
            return
        # under BIOS control the duty is unknown; a system fan may legitimately be stopped by the
        # BIOS 'fan stop' feature, but the CPU fan never is
        driven = bios_cpu or (duty is not None and duty >= threshold)
        if fid not in self.seen_spinning or not driven:
            self.stall_since.pop(fid, None)
            if fid in self.stalled and not driven:
                self.stalled.discard(fid)
            return
        since = self.stall_since.setdefault(fid, now)
        if now - since >= STALL_SECONDS and fid not in self.stalled:
            self.stalled.add(fid)
            what = "CPU FAN STOPPED: check it now" if self.known_cpu_fan(fid) else "has stopped"
            at = f"{duty:.0f} %" if duty is not None else "BIOS control"
            self.event("error", f"{fan.label} {what} (0 RPM at {at}); the fan may be blocked, "
                                "unplugged or broken")
        if fid in self.stalled:
            st["flags"].append("stalled")

    def control_gpu(self, now: float, fan: GpuFan | None = None, force: bool = False) -> None:
        """One AMD card's firmware fan curve (RX 7000/9000)."""
        fan = fan if fan is not None else self.hw.fans.get(GPU_FAN_ID)
        if not isinstance(fan, GpuFan):
            return
        fid, g = fan.id, self.gpu[fan.id]
        temp = self.temps.get(self.own_sensor(fid) or "")
        if self.busy_with(fid):
            p = fan.pwm()
            self.fan_status[fid] = {
                "id": fid, "label": fan.label, "kind": "gpu", "control": "pmfw", "mode": self.fan_cfg(fid)["mode"],
                "rpm": fan.rpm(), "duty": round(pwm_to_duty(p), 1) if p is not None else None,
                "target": None, "temp": temp, "flags": [self.busy_with(fid)]}
            return
        fc = self.fan_cfg(fid)
        p = fan.pwm()
        self.fan_status[fid] = {
            "id": fid, "label": fan.label, "kind": "gpu", "control": "pmfw", "mode": fc["mode"],
            "rpm": fan.rpm(), "duty": round(pwm_to_duty(p), 1) if p is not None else None,
            "target": None, "temp": temp, "flags": ["error"] if g.error else [], "error": g.error,
        }
        if g.ranges is None:                   # the card's own limits, for the app's editor (read once)
            try:
                i0 = amdgpu.read_info(fan.fan_ctrl_dir)
                g.ranges = {"temp": list(i0.temp_range), "duty": list(i0.duty_range), "points": len(i0.points)}
            except (OSError, amdgpu.GpuError):
                pass
        if g.ranges:
            self.fan_status[fid]["ranges"] = g.ranges
        held = fan.fan_ctrl_dir in (self.restore.get("gpu_fan_ctrls") or [])
        if fc["mode"] == "auto":
            if held and now >= g.retry_at:
                errs = amdgpu.reset(fan.fan_ctrl_dir)
                if errs:
                    g.error = "; ".join(errs)
                    g.retry_at = now + GPU_RETRY_AFTER
                    self.event("error", f"{fan.label} reset failed: {g.error}")
                else:
                    g.error = None
                    self._gpu_restore(fan.fan_ctrl_dir, False)
                    self.event("info", f"{fan.label} handed back to the card's automatic control")
            g.applied = None
            g.fails = 0
            return
        if fid in self.disabled:
            self.fan_status[fid]["flags"].append("gave_up")
            return
        try:
            info = amdgpu.read_info(fan.fan_ctrl_dir)
        except (OSError, amdgpu.GpuError) as e:
            g.error = str(e)
            return
        if fc["mode"] == "fixed":
            want = (tuple(amdgpu.flat_points(info, fc["fixed"])), False)
        else:
            want = (tuple(amdgpu.build_points(info, fc["curve"])), bool(fc["zero_rpm"]))
        need = force or want != g.applied
        if not need and now >= g.next_verify:
            g.next_verify = now + GPU_VERIFY_EVERY
            if tuple(info.points) != want[0]:
                q = g.repairs
                while q and now - q[0] > GPU_REPAIR_WINDOW:
                    q.popleft()
                if len(q) < GPU_REPAIR_LIMIT:
                    q.append(now)
                    others = other_fan_tools(os.path.join(self.root, "proc"))
                    self.event("warn", f"{fan.label} curve was changed outside FanPilot"
                               + (f" (also running: {', '.join(others)})" if others else "") + ", re-applying")
                    need = True
                elif not g.repair_paused:
                    g.repair_paused = True
                    self.event("warn", f"{fan.label} curve keeps being changed outside FanPilot (another tool?); "
                                       "leaving it alone for up to an hour")
            else:
                g.repair_paused = False
        if not need or now < g.retry_at:
            return
        try:
            self._gpu_restore(fan.fan_ctrl_dir, True)
            amdgpu.apply(fan.fan_ctrl_dir, list(want[0]), want[1])
            g.applied = want
            g.error = None
            g.fails = 0
            g.next_verify = now + GPU_VERIFY_EVERY
            if want != g.logged:
                g.logged = want
                self.event("info", f"{fan.label} curve applied: " + ", ".join(f"{t}°C→{d}%" for t, d in want[0]))
        except (OSError, amdgpu.GpuError) as e:
            g.error = str(e)
            g.applied = None
            g.fails += 1
            g.retry_at = now + GPU_RETRY_AFTER
            self.event("error", f"{fan.label}: {e}")
            if g.fails >= GPU_MAX_FAILS:
                self.disabled[fid] = "the card rejected the curve"
                self.event("error", f"{fan.label}: giving control back to the card after repeated failures; "
                                    "change its settings to try again")
                if fan.fan_ctrl_dir in (self.restore.get("gpu_fan_ctrls") or []) and not amdgpu.reset(fan.fan_ctrl_dir):
                    try:
                        self._gpu_restore(fan.fan_ctrl_dir, False)
                    except OSError:
                        pass

    def _sync_nvidia(self, hw=None) -> None:
        """NVIDIA cards (read-only): add/remove their temperature and fan readings as nvidia-smi reports
        them. With ``hw`` (a fresh rescan) they are always added, so the rescan compares like with like."""
        snap = self.nvidia.snapshot() if self.nvidia else {}
        key = tuple(sorted(snap))
        if hw is None and key == self._nv_seen:
            return
        target = hw or self.hw
        if hw is None:
            self._nv_seen = key
        for sid in [s for s in target.sensors if s.startswith("nvidia")]:
            del target.sensors[sid]
        for fid in [f for f in target.fan_inputs if f.startswith("nvidia")]:
            del target.fan_inputs[fid]
        for bus, card in snap.items():
            chip = f"nvidia@{bus}"              # stable, whatever the number of cards

            def getter(field, bus=bus):
                return (self.nvidia.snapshot().get(bus) or {}).get(field) if self.nvidia else None
            target.sensors[f"{chip}/temp1"] = NvSensor(f"{chip}/temp1", "GPU temperature", lambda g=getter: g("temp"))
            target.fan_inputs[f"{chip}/fan1"] = (f"{card['name']} fan %", lambda g=getter: g("fan"))
        if hw is None:
            self.readings_src = Readings(self.root, self.hw)
            for bus, card in snap.items():
                self.readings_src.groups[f"gpu:nvidia@{bus}"] = card["name"]

    def tick(self, now: float, dt: float) -> None:
        self._sync_nvidia()
        # while the CPU sensor is unreadable, look for it every tick so emergency mode ends promptly
        if (time.monotonic() >= self.next_rescan or self.crit_unreadable) and self._missing_hardware() \
                and not (self.mapping and self.mapping.state == "running"):    # never re-bind mid-mapping
            self.rescan()
        self.read_temps()
        self.check_critical()
        cal = self.calibration
        if cal and cal.state == "running":
            cpu = self.temps.get(self.crit_sensor("cpu") or "")
            if self.critical or cpu is None or cpu >= CAL_ABORT_CPU:
                cal.abort(f"Stopped: CPU at {cpu} °C")
                self.event("warn", f"Calibration of {cal.fan.label} aborted (CPU {cpu} °C)")
            else:
                try:
                    cal.tick(now)
                except Exception as e:  # noqa: BLE001 -- any bug: stop measuring, fan back to 100 %
                    cal.abort(f"Stopped: {e}")
                if cal.state == "done":
                    self.cfg["calibration"][cal.fan.id] = cal.result
                    self.states[cal.fan.id].reset()
                    try:
                        cfgmod.save(self.config_path, self.cfg, self.protect_cpu)
                    except (OSError, cfgmod.ConfigError) as e:
                        self.event("error", f"Could not save calibration: {e}")
                    self.event("info", f"Calibration of {cal.fan.label} finished: {cal.result}")
            if cal.state != "running":
                self.states[cal.fan.id].reset()
        st = self.selftest
        if st and st.state == "running":
            try:
                st.tick(now)
            except Exception as e:  # noqa: BLE001 -- any bug: stop the test, settings come back
                log.exception("self-test step failed")
                st.abort(f"internal error: {e}")
            if st.state != "running":
                self.event("info" if st.failed == 0 else "warn",
                           f"Self-test {'finished' if st.state == 'done' else 'stopped'}: "
                           f"{st.passed} passed, {st.failed} failed")
        mp = self.mapping
        if mp and mp.state == "running":
            try:
                mp.tick(now)
            except Exception as e:  # noqa: BLE001 -- any bug: stop mapping, fans go back
                log.exception("fan mapping step failed")
                mp.abort(f"internal error: {e}")
            if mp.state == "done":
                cfg = json.loads(json.dumps(self.cfg))
                # this run's answer replaces every earlier entry for the headers it tested
                cfg["fan_map"] = {**{k: v for k, v in cfg.get("fan_map", {}).items() if k not in mp.headers},
                                  **mp.result}
                try:
                    self.set_config(cfg)
                    self.rescan(force=True)
                except (OSError, cfgmod.ConfigError) as e:
                    self.event("error", f"Could not save the fan map: {e}")
                self.event("info", "Fan mapping finished: " + "; ".join(mp.details))
            elif mp.state != "running":
                self.event("warn", f"Fan mapping {mp.step.lower()}")
        broken = []
        for fan in list(self.hw.fans.values()):
            try:
                if isinstance(fan, BoardFan):
                    self.control_board_fan(fan, dt, now)
                elif isinstance(fan, GpuFan):
                    self.control_gpu(now, fan)
            except Exception as e:  # one fan's bug must not skip the others
                log.exception("controlling %s failed", fan.id)
                broken.append(f"{fan.label}: {e}")
        try:
            # every reading for the Sensors view; a failure here never touches fan control
            crit = dict(self.cfg["critical"], cpu_sensor=self.crit_sensor("cpu"), gpu_sensor=self.crit_sensor("gpu"))
            self.readings = self.readings_src.read(self.temps, self.fan_status, crit)
        except Exception as e:      # noqa: BLE001 -- display only
            self.readings = []      # never show an old snapshot as current
            if not getattr(self, "_readings_warned", False):
                self._readings_warned = True
                self.event("warn", f"Some sensor readings are unavailable: {e}")
        try:
            # snapshot for other programs (G510 LCD); tmpfs, so no disk writes
            export.write(os.path.join(self.run_dir, "sensors.tsv"), self.hw, self.temps, self.fan_status,
                         self.cfg.get("names"))
        except OSError as e:
            if not getattr(self, "_export_warned", False):
                self._export_warned = True
                self.event("warn", f"Could not write the sensor snapshot: {e}")
        if broken:
            raise RuntimeError("; ".join(broken))

    def _missing_hardware(self) -> bool:
        """Something we need is not (or no longer) there: worth looking again."""
        fans = self.hw.fans
        if not any(isinstance(f, BoardFan) and f.kind == "board" for f in fans.values()):
            return True                    # (an older AMD card's pwm1 is not a motherboard header)
        for f in fans.values():
            if not os.path.exists(self._fan_path(f)):
                return True
        if any(s.path and not os.path.exists(s.path) for s in self.hw.sensors.values()):   # (NVIDIA: no file)
            return True
        if self.crit_sensor("cpu") is None:
            return True                    # no CPU temperature yet (driver loads late?): keep looking
        want = {s for s in (self.crit_sensor("cpu"), self.crit_sensor("gpu")) if s}
        for fid, fc in self.cfg["fans"].items():
            if fc.get("mode") != "auto":
                if fid not in fans:
                    return True
                if fc.get("mode") == "curve":
                    want.update(self.resolve_sources(fc.get("source") or [], fid))
        return any(s not in self.hw.sensors for s in want)

    @staticmethod
    def _fan_path(f):
        return f.enable_path if isinstance(f, BoardFan) else f.fan_ctrl_dir

    def _hw_signature(self, hw):
        return ({k: self._fan_path(f) for k, f in hw.fans.items()}, {k: s.path for k, s in hw.sensors.items()})

    def rescan(self, force: bool = False) -> None:
        self.next_rescan = time.monotonic() + RESCAN_EVERY
        hw = discover(self.root)
        self._apply_fan_map(hw, release=False)   # compare like with like; no hardware writes on a trial
        self._sync_nvidia(hw)
        if not force and self._hw_signature(hw) == self._hw_signature(self.hw):
            return
        for fid, old in self.hw.fans.items():
            # a fan that leaves the list (map forgotten, header hidden, ...) is handed back first if
            # FanPilot holds it: nothing may stay in manual mode without being controlled
            if fid not in hw.fans and isinstance(old, BoardFan) and old.enable_path in self.restore["pwm_enable"] \
                    and os.path.exists(old.enable_path):
                self._release(old)
        self.event("info", f"Hardware rescan: {len(hw.fans)} fans, {len(hw.sensors)} sensors")
        for fid, f in hw.fans.items():
            old = self.hw.fans.get(fid)
            if old is None or self._fan_path(old) != self._fan_path(f):
                # same fan, new sysfs path (module reload): take it over again from scratch
                self.asserted.discard(fid)
                self.states[fid].reset()
                self.reclaims[fid].clear()
                if isinstance(f, GpuFan):
                    self.gpu[fid].applied = None
        gone = [p for p in self.restore["pwm_enable"] if not os.path.exists(p)]
        if gone:
            new = json.loads(json.dumps(self.restore))
            for p in gone:                 # nothing left to hand back at a path that no longer exists
                e = new["pwm_enable"].pop(p)
                if isinstance(e, dict) and e.get("id"):   # ...but remember the BIOS mode for its return
                    new.setdefault("vanished", {})[e["id"]] = e.get("orig")
            try:
                self._save_restore(new)
            except OSError:
                pass
        back = [f for f in hw.fans.values() if isinstance(f, BoardFan)
                and f.id in (self.restore.get("vanished") or {}) and f.enable_path not in self.restore["pwm_enable"]]
        if back:
            # a fan FanPilot held is back after a driver reload. The chip kept OUR manual mode, so it must be
            # handed back with the BIOS's mode even if nothing takes it again (it is on Automatic now, or
            # the service stops): record it now; release and shutdown then restore it as usual.
            new = json.loads(json.dumps(self.restore))
            for f in back:
                new["pwm_enable"][f.enable_path] = {"orig": new["vanished"].pop(f.id), "id": f.id,
                                                    "name": _chip_name(f.enable_path)}
            try:
                self._save_restore(new)
            except OSError:
                pass
        self.hw = hw
        self._nv_seen = None                    # the next sync adds whatever nvidia-smi reports now
        self.readings_src = Readings(self.root, hw)

    def on_resume(self) -> None:
        # [S7] the BIOS may have rewritten the fan registers while we slept
        self.event("info", "Resumed from sleep, re-applying fan settings")
        self.asserted.clear()
        for s in self.states.values():
            s.reset()
        for g in self.gpu.values():
            g.applied = None

    # -- API --------------------------------------------------------------------
    def status(self) -> dict:
        sensors = [{"id": sid, "label": self.label(sid, s.label), "value": self.temps.get(sid)}
                   for sid, s in self.hw.sensors.items()]
        fans = []
        for fid, f in self.hw.fans.items():
            st = dict(self.fan_status.get(fid, {"id": fid, "label": f.label, "kind": f.kind}))
            st["label"] = self.label(fid, f.label)
            fans.append(st)
        return {
            "version": __version__,
            "uptime": round(time.time() - self.started),
            "board_chip": self.hw.board_chip,
            "critical": self.critical,
            "sensors": sensors,
            "fans": fans,
            "calibration": self.calibration.info() if self.calibration else None,
            "selftest": self.selftest.info() if self.selftest else None,
            "mapping": self.mapping.info() if self.mapping else None,
            "events": list(self.events)[-50:],
            "disabled": dict(self.disabled),
            "active_profile": self.cfg.get("active_profile"),
            "readings": self._named_readings(),
            "groups": self.readings_src.group_list(self.readings),
            "profiles": list(presets.BUILTIN_PROFILES) + sorted(self.cfg.get("profiles", {})),
            "cpu_sensor": self.crit_sensor("cpu"),
            "gpu_sensor": self.crit_sensor("gpu"),
            "cpu_protected": [fid for fid in self.hw.fans if self.protect_cpu(fid)],
            "cpu_fan_known": bool(self.hw.cpu_fans | set(self.cfg.get("cpu_fans", []))),
            "names": dict(self.cfg.get("names", {})),
            "board": (self.hw.board or {}).get("title"),
            "diagnosis": diagnose(self.hw, self.root),
            "dmi": {k: self.hw.dmi.get(k) for k in ("board_vendor", "board_name")},
            "read_only_fans": [{"id": fid, "label": self.label(fid, lab)}
                               for fid, (lab, _p) in self.hw.fan_inputs.items()],
        }

    def set_config(self, new: dict, uid: int = 0) -> dict:
        if not isinstance(new, dict):
            raise cfgmod.ConfigError("config must be an object")
        cfg = cfgmod.validate(new, self.protect_cpu)
        if uid != 0:
            # only root may change who is allowed in; calibration is written by the daemon itself
            cfg["allowed_uids"] = list(self.cfg["allowed_uids"])
            cfg["calibration"] = dict(self.cfg.get("calibration", {}))
        elif "calibration" not in new:
            cfg["calibration"] = dict(self.cfg.get("calibration", {}))
        # [S4] the emergency override must watch sensors that exist on this machine
        for key in ("cpu_sensor", "gpu_sensor"):
            want = cfg["critical"][key]
            if want != self.cfg["critical"][key] and want != "auto" and want not in self.hw.sensors:
                cfg["critical"][key] = self.cfg["critical"][key]
                self.event("warn", f"Ignored emergency sensor {want!r}: not present on this machine")
        cfgmod.save(self.config_path, cfg, self.protect_cpu)
        old = self.cfg
        self.cfg = cfg
        for kind in ("cpu", "gpu"):
            if old["critical"][f"{kind}_sensor"] != cfg["critical"][f"{kind}_sensor"]:
                # only a changed sensor SETTING chooses again; limits and anything else keep the choice,
                # so an emergency caused by a vanished sensor is never ended by saving a setting
                self._auto_sensor = {k: v for k, v in self._auto_sensor.items() if k != kind}
                if self.crit_sensor(kind) is None and kind == "cpu":
                    self.event("warn", "No CPU temperature sensor found: the CPU emergency limit is inactive")
        for fid in list(self.disabled):
            # changing a fan's settings lets it try again
            if old["fans"].get(fid) != cfg["fans"].get(fid):
                del self.disabled[fid]
                self.reclaims[fid].clear()
        for fid, f in cfg["fans"].items():
            if old["fans"].get(fid) != f:
                self.states[fid].reset()
        for fid, g in self.gpu.items():
            g.repairs.clear()
            if old["fans"].get(fid) != cfg["fans"].get(fid):
                g.fails = 0
                g.retry_at = 0.0
        self.event("info", "Settings saved")
        return cfg

    def handle(self, req: dict, uid: int) -> dict:
        cmd = req.get("cmd")
        writable = uid == 0 or uid in self.cfg["allowed_uids"] or in_group(uid, ADMIN_GROUP)
        if self.stopping and cmd not in ("hello", "status", "get_config"):
            return {"ok": False, "error": "FanPilot is shutting down"}
        with self.lock:
            if cmd == "hello":
                return {"ok": True, "version": __version__, "can_write": writable}
            if cmd == "status":
                return {"ok": True, "status": self.status(), "can_write": writable}
            if cmd == "get_config":
                return {"ok": True, "config": self.cfg}
            if not writable:
                return {"ok": False, "error": "Not allowed: add your user to the 'fanpilot' group "
                                              "(sudo usermod -aG fanpilot $USER, then log in again)"}
            try:
                if cmd == "set_config":
                    return {"ok": True, "config": self.set_config(req.get("config"), uid)}
                if cmd == "set_fan":
                    fid = req.get("fan")
                    if fid not in self.hw.fans:
                        return {"ok": False, "error": f"unknown fan {fid}"}
                    settings = req.get("settings") or {}
                    if not isinstance(settings, dict):
                        return {"ok": False, "error": "settings must be an object"}
                    if fid in self.disabled:
                        self.event("info", f"{self.hw.fans[fid].label}: trying again with new settings")
                    cfg = json.loads(json.dumps(self.cfg))
                    cfg["fans"][fid] = {**self.fan_cfg(fid), **settings}
                    cfg["active_profile"] = None   # hand-tuned now
                    return {"ok": True, "config": self.set_config(cfg, uid)}
                if cmd == "apply_profile":
                    name = req.get("name")
                    if name in presets.BUILTIN_PROFILES:
                        # keep each fan's fine tuning (lowest speed, allow stop, ramps); only curves change
                        fans = {fid: {**self.fan_cfg(fid), **pf}
                                for fid, pf in presets.builtin_profile(
                                    name, list(self.hw.fans), self.crit_sensor("cpu") or "auto:cpu",
                                    self.crit_sensor("gpu"), self.protect_cpu).items()}
                    elif name in self.cfg["profiles"]:
                        fans = {k: v for k, v in self.cfg["profiles"][name].items() if k in self.hw.fans}
                    else:
                        return {"ok": False, "error": f"no profile named {name!r}"}
                    cfg = json.loads(json.dumps(self.cfg))
                    cfg["fans"].update(fans)
                    cfg["active_profile"] = name
                    out = self.set_config(cfg, uid)
                    self.event("info", f"Profile '{name}' applied")
                    return {"ok": True, "config": out}
                if cmd == "save_profile":
                    name = req.get("name")
                    if isinstance(name, str) and name.casefold() in {b.casefold() for b in presets.BUILTIN_PROFILES}:
                        return {"ok": False, "error": f"'{name}' is a built-in profile, choose another name"}
                    cfg = json.loads(json.dumps(self.cfg))
                    cfg["profiles"][name] = {fid: self.fan_cfg(fid) for fid in self.hw.fans}
                    cfg["active_profile"] = name
                    return {"ok": True, "config": self.set_config(cfg, uid)}
                if cmd == "delete_profile":
                    name = req.get("name")
                    if name not in self.cfg["profiles"]:
                        return {"ok": False, "error": f"no saved profile named {name!r}"}
                    cfg = json.loads(json.dumps(self.cfg))
                    del cfg["profiles"][name]
                    if cfg["active_profile"] == name:
                        cfg["active_profile"] = None
                    return {"ok": True, "config": self.set_config(cfg, uid)}
                if cmd == "identify":
                    fid = req.get("fan")
                    fan = self.hw.fans.get(fid)
                    if not isinstance(fan, BoardFan) or fan.kind != "board":
                        return {"ok": False, "error": "only motherboard fans can be identified"}
                    if fid in self.disabled:
                        return {"ok": False, "error": f"{fan.label} was handed back to the BIOS "
                                                      f"({self.disabled[fid]}); change its settings first"}
                    if self.calibration and self.calibration.state == "running" and self.calibration.fan.id == fid:
                        return {"ok": False, "error": "this fan is being measured"}
                    if self.selftest and self.selftest.state == "running":
                        return {"ok": False, "error": "the self-test is running"}
                    if self.mapping and self.mapping.state == "running":
                        return {"ok": False, "error": "fans are being mapped"}
                    self.identify[fid] = None
                    self.event("info", f"Identify: {fan.label} slow, then full speed ({IDENTIFY_SECONDS:.0f} s)")
                    return {"ok": True, "seconds": IDENTIFY_SECONDS}
                if cmd == "set_critical":
                    cfg = json.loads(json.dumps(self.cfg))
                    for k in ("cpu_temp", "gpu_temp"):
                        if k in req:
                            cfg["critical"][k] = req[k]
                    out = self.set_config(cfg, uid)
                    self.event("info", f"Emergency limits: CPU {out['critical']['cpu_temp']} °C, "
                                       f"GPU {out['critical']['gpu_temp']} °C")
                    return {"ok": True, "config": out}
                if cmd == "calibrate":
                    fid = req.get("fan")
                    fan = self.hw.fans.get(fid)
                    if not isinstance(fan, BoardFan) or fan.kind != "board":
                        return {"ok": False, "error": "only motherboard fans can be calibrated"}
                    if self.calibration and self.calibration.state == "running":
                        return {"ok": False, "error": "a calibration is already running"}
                    if self.selftest and self.selftest.state == "running":
                        return {"ok": False, "error": "the self-test is running"}
                    if self.mapping and self.mapping.state == "running":
                        return {"ok": False, "error": "fans are being mapped"}
                    if self.critical:
                        return {"ok": False, "error": "temperatures are critical"}
                    self._take(fan)
                    fan.set_enable(1)
                    fan.set_pwm(255)       # not whatever stale value the BIOS left in the register
                    self.asserted.add(fid)
                    self.calibration = Calibration(fan, self.protect_cpu(fid))
                    self.event("info", f"Calibration of {fan.label} started")
                    return {"ok": True}
                if cmd == "selftest":
                    if self.selftest and self.selftest.state == "running":
                        return {"ok": False, "error": "the self-test is already running"}
                    if self.calibration and self.calibration.state == "running":
                        return {"ok": False, "error": "a fan is being measured; try again when it is done"}
                    if self.mapping and self.mapping.state == "running":
                        return {"ok": False, "error": "fans are being mapped; try again when it is done"}
                    if self.critical:
                        return {"ok": False, "error": "temperatures are critical"}
                    self.identify.clear()
                    self.selftest = SelfTest(self)
                    self.event("info", "Self-test started")
                    return {"ok": True}
                if cmd == "map_fans":
                    if any(j and j.state == "running" for j in (self.selftest, self.calibration, self.mapping)):
                        return {"ok": False, "error": "a test or measurement is already running"}
                    if self.critical:
                        return {"ok": False, "error": "temperatures are critical"}
                    if not self.hw.board_chips:
                        return {"ok": False, "error": "no controllable motherboard fan chip"}
                    self.identify.clear()
                    self.mapping = FanMapping(self)
                    self.event("info", f"Fan mapping started ({len(self.mapping.headers)} headers)")
                    return {"ok": True}
                if cmd == "map_forget":
                    if self.mapping and self.mapping.state == "running":
                        return {"ok": False, "error": "fans are being mapped right now"}
                    cfg = json.loads(json.dumps(self.cfg))
                    cfg["fan_map"] = {}
                    out = self.set_config(cfg, uid)
                    self.rescan(force=True)
                    self.event("info", "Fan map forgotten: headers as the driver reports them")
                    return {"ok": True, "config": out}
                if cmd == "map_cancel":
                    if self.mapping:
                        self.mapping.abort("cancelled")
                    return {"ok": True}
                if cmd == "selftest_cancel":
                    if self.selftest:
                        self.selftest.abort("cancelled")
                    return {"ok": True}
                if cmd == "calibrate_cancel":
                    if self.calibration:
                        self.calibration.abort("Cancelled")
                    return {"ok": True}
                if cmd == "rename":
                    item, name = req.get("id"), req.get("name") or ""
                    if not isinstance(name, str):
                        raise ValueError("name must be text")
                    name = name.strip()
                    known = set(self.hw.fans) | set(self.hw.sensors) | set(self.hw.fan_inputs) \
                        | {r["id"] for r in self.readings} | set(self.cfg.get("names", {}))
                    if item not in known:
                        return {"ok": False, "error": f"no fan or sensor with the id {item!r} "
                                                      "(see 'fanpilotctl status' / 'fanpilotctl sensors')"}
                    cfg = json.loads(json.dumps(self.cfg))
                    if name:
                        cfg["names"][item] = name
                    else:
                        cfg["names"].pop(item, None)
                    return {"ok": True, "config": self.set_config(cfg, uid)}
                if cmd == "set_cpu_fans":
                    fans = req.get("fans")
                    if not isinstance(fans, list) or not all(isinstance(f, str) for f in fans):
                        return {"ok": False, "error": "cpu fans must be a list of fan ids"}
                    bad = [f for f in fans if not (isinstance(self.hw.fans.get(f), BoardFan)
                                                    and self.hw.fans[f].kind == "board")]
                    if bad:
                        return {"ok": False, "error": f"not a motherboard fan on this PC: {', '.join(bad)}"}
                    cfg = json.loads(json.dumps(self.cfg))
                    cfg["cpu_fans"] = fans
                    saved = self.set_config(cfg, uid)
                    self.rescan()       # headers the map found empty may be hidden now that the CPU fan is known
                    return {"ok": True, "config": saved}
                if cmd == "rescan":
                    self.rescan()
                    return {"ok": True}
            except (cfgmod.ConfigError, OSError, ValueError, TypeError) as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    # -- server / loop ------------------------------------------------------------
    def serve(self) -> socketserver.BaseServer:
        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", creds)
                with daemon.conn_lock:
                    if sum(daemon.conns.values()) >= MAX_CLIENTS or daemon.conns[uid] >= PER_UID_CLIENTS:
                        return  # too many connections (from this user)
                    daemon.conns[uid] += 1
                try:
                    self.request.settimeout(IDLE_TIMEOUT)
                    self._serve(uid)
                except OSError:
                    pass        # client went away or idled out: not an error worth a traceback
                finally:
                    with daemon.conn_lock:
                        daemon.conns[uid] -= 1

            def _serve(self, uid):
                for _ in range(1000):
                    line = self.rfile.readline(1 << 20)
                    if not line:
                        return
                    try:
                        req = json.loads(line)
                        if not isinstance(req, dict):
                            raise ValueError("request must be an object")
                        with daemon.lock:  # serialise while nothing can change underneath
                            out = json.dumps(daemon.handle(req, uid))
                    except ValueError as e:
                        out = json.dumps({"ok": False, "error": f"bad request: {e}"})
                    self.wfile.write((out + "\n").encode())

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        srv = Server(self.socket_path, Handler)
        os.chmod(self.socket_path, 0o666)  # reads for everyone; writes checked by uid
        threading.Thread(target=srv.serve_forever, name="ipc", daemon=True).start()
        return srv

    def run(self) -> int:
        srv = self.serve()
        self.event("info", f"FanPilot {__version__} started: {len(self.hw.fans)} fans, "
                           f"{len(self.hw.sensors)} sensors, board chip {self.hw.board_chip or 'NOT FOUND'}")
        if self.hw.board_chip is None:
            hints = diagnose(self.hw, self.root)
            self.event("warn", "No controllable motherboard fan chip found" + (f": {hints[0]}" if hints else ""))
        for kind in ("cpu", "gpu"):
            sid = self.crit_sensor(kind)
            if sid is None and kind == "gpu":
                continue                       # a PC without a GPU temperature has nothing to watch there
            if sid not in self.hw.sensors:
                self.event("warn", f"{kind.upper()} emergency sensor {sid or '(none found)'} not found: "
                                   "that protection is inactive")
        others = other_fan_tools(os.path.join(self.root, "proc"))
        if others:
            self.event("warn", f"Other fan tools are running ({', '.join(others)}): if they control the same "
                               "fans, the last one to write wins. FanPilot re-applies its settings when it notices.")
        sd_notify("READY=1")
        last_m, last_b = time.monotonic(), _boottime()
        failed_ticks = 0
        exit_code = 0
        try:
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                b0 = _boottime()
                dt = t0 - last_m
                if (b0 - last_b) - dt > 5:
                    with self.lock:
                        self.on_resume()
                last_m, last_b = t0, b0
                with self.lock:
                    try:
                        self.tick(t0, max(0.0, min(dt, 5.0)))
                        failed_ticks = 0
                    except Exception as e:
                        failed_ticks += 1
                        log.exception("tick failed")
                        self.event("error", f"internal error: {e}")
                if failed_ticks >= TICK_FAIL_LIMIT:
                    # [S2] keeps failing -> hand everything back and let systemd restart us
                    log.error("control loop keeps failing; restoring fans and exiting")
                    exit_code = 1
                    break
                sd_notify("WATCHDOG=1")
                self.stop_event.wait(max(0.05, self.cfg["interval"] - (time.monotonic() - t0)))
        finally:
            self.stopping = True
            srv.shutdown()
            srv.server_close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            try:
                os.unlink(os.path.join(self.run_dir, "sensors.tsv"))  # readers see 'no FanPilot' at once
            except OSError:
                pass
            with self.lock:
                errs = restore_from_file(self.restore_file)
            for e in errs:
                log.error("restore: %s", e)
            log.info("stopped; fans handed back to firmware")
        return exit_code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fanpilotd", description="FanPilot fan control daemon")
    ap.add_argument("--root", default="/", help="sysfs root (for testing)")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--run-dir", default=RUN_DIR)
    ap.add_argument("--socket", default=None)
    ap.add_argument("--restore", action="store_true",
                    help="only hand every fan back to firmware control, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    restore_file = os.path.join(args.run_dir, "restore.json")
    if args.restore:
        errs = restore_from_file(restore_file)
        for e in errs:
            log.error("restore: %s", e)
        return 1 if errs else 0
    d = Daemon(args.root, args.config, args.run_dir, args.socket or os.path.join(args.run_dir, "fanpilot.sock"))
    signal.signal(signal.SIGTERM, lambda *_: d.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: d.stop_event.set())
    return d.run()


if __name__ == "__main__":
    raise SystemExit(main())
