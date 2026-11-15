# SPDX-License-Identifier: GPL-3.0-or-later
"""NVIDIA graphics cards, read-only: temperature and fan speed through nvidia-smi.

nvidia-smi ships with the proprietary driver and has a documented CSV output. It runs in its own thread
with a timeout, so a missing driver, a hung GPU or a crash can never block or take down fan control (a
direct NVML call could). Fan *control* for NVIDIA is not in v1 (docs/ROADMAP.md): NVML's setter
needs root and leaves the card on manual until it is explicitly reset.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading
import time

QUERY = "pci.bus_id,name,temperature.gpu,fan.speed"


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def parse(text: str) -> dict[str, dict]:
    """'00000000:01:00.0, NVIDIA GeForce RTX 4070, 45, 30' lines -> {bus id: {name, temp, fan}}."""
    out = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4 or not parts[0]:
            continue
        bus = parts[0].lower()
        if bus.startswith("00000000:"):
            bus = bus[4:]                       # 0000:01:00.0, like the kernel's PCI addresses

        def num(s):
            try:
                return float(s)
            except ValueError:                  # "[N/A]", "[Not Supported]"
                return None
        out[bus] = {"name": parts[1], "temp": num(parts[2]), "fan": num(parts[3])}
    return out


class NvidiaSmi:
    def __init__(self, interval: float = 2.0, binary: str | None = None):
        self.binary = binary or shutil.which("nvidia-smi")
        self.interval = interval
        self.data: dict[str, dict] = {}
        self.updated = 0.0
        self.started = time.monotonic()
        self._lock = threading.Lock()
        if self.binary:
            threading.Thread(target=self._loop, name="nvidia-smi", daemon=True).start()

    @staticmethod
    def cards(root: str = "/") -> list[str]:
        """PCI directories of NVIDIA display controllers."""
        return [d for d in glob.glob(os.path.join(root, "sys/bus/pci/devices/*"))
                if _read(os.path.join(d, "vendor")) == "0x10de" and (_read(os.path.join(d, "class")) or "").startswith("0x03")]

    @staticmethod
    def all_asleep(root: str = "/") -> bool:
        """Every NVIDIA card is runtime-suspended (hybrid laptops): asking would wake it and keep it awake."""
        cards = NvidiaSmi.cards(root)
        return bool(cards) and all(_read(os.path.join(d, "power/runtime_status")) == "suspended" for d in cards)

    def pending(self, root: str = "/") -> bool:
        """Worth waiting for the first answer: nvidia-smi exists, an NVIDIA card exists, and it has had
        30 s to answer. (Installed tools without a card, or a driver that never answers, must not keep
        the GPU emergency sensor unchosen forever.)"""
        return (bool(self.binary) and not self.updated and time.monotonic() - self.started < 30
                and bool(self.cards(root)))

    def _loop(self) -> None:
        while True:
            if self.cards() and not self.all_asleep():     # no NVIDIA card: never spawn nvidia-smi
                try:
                    r = subprocess.run([self.binary, f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
                                       capture_output=True, text=True, errors="replace", timeout=5)
                    if r.returncode == 0:
                        with self._lock:
                            self.data, self.updated = parse(r.stdout), time.monotonic()
                    # a failed run keeps the last answer; it turns stale (= no readings) after 15 s
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            time.sleep(self.interval)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            if time.monotonic() - self.updated > 15:
                return {}                       # stale: nvidia-smi hangs or stopped answering
            return dict(self.data)
