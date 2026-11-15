# SPDX-License-Identifier: GPL-3.0-or-later
"""1.0.1: emergency mode includes the graphics card fan, exact Fintek/Winbond chip lists, deeply nested
requests, CLI waits that survive a service restart, docs whose download links match the version."""
import io
import json
import os
import re
import socket
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fanpilot  # noqa: E402
from fanpilot import ctl  # noqa: E402
from fanpilot.hw import CONTROL_CHIP_RE  # noqa: E402
from test_daemon import Base  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Release101(Base):
    def test_emergency_runs_the_graphics_card_fan_at_full_speed(self):
        d = self.daemon({"fans": {"amdgpu/fan1": {"mode": "fixed", "fixed": 30}}})
        self.ticks(d, 2)
        self.assertTrue(self.gpu.committed and all(p[1] < 100 for p in self.gpu.committed), self.gpu.committed)
        d.critical = True
        d.control_gpu(self.now)
        self.assertTrue(all(p[1] == 100 for p in self.gpu.committed), self.gpu.committed)
        d.critical = False
        d.control_gpu(self.now + 1)
        self.assertTrue(all(p[1] < 100 for p in self.gpu.committed), "its own setting again after the emergency")

    def test_deeply_nested_request_gets_an_error_reply(self):
        d = self.daemon()
        srv = d.serve()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(d.socket_path)
            s.sendall(b"[" * 200000 + b"\n")
            with s.makefile() as f:
                reply = json.loads(f.readline())
            self.assertFalse(reply["ok"])
            s.close()
        finally:
            srv.shutdown()


class Chips(unittest.TestCase):
    def test_only_chips_the_supported_drivers_cover(self):
        for name in ("it8688", "nct6798", "nct6687", "f71882fg", "f71869a", "f81866a", "f8000",
                     "w83627ehf", "w83627dhg", "w83667hg"):
            self.assertTrue(CONTROL_CHIP_RE.match(name), name)
        for name in ("f71805f", "w83627hf", "w83781d", "w83791d", "w83795g", "nct6683", "gigabyte_wmi"):
            self.assertFalse(CONTROL_CHIP_RE.match(name), name)


class CliRestart(unittest.TestCase):
    def _run(self, cmd, key):
        class Fake:
            def __init__(self, *a, **k):
                pass

            def call(self, c, **k):
                return {"ok": True, "status": {key: None}} if c == "status" else {"ok": True}
        out = io.StringIO()
        with mock.patch.object(ctl, "Client", Fake), mock.patch("time.sleep"), redirect_stdout(out):
            rc = ctl.main([cmd])
        return rc, out.getvalue()

    def test_selftest_wait_survives_a_service_restart(self):
        rc, out = self._run("selftest", "selftest")
        self.assertEqual(rc, 1)
        self.assertIn("service restarted", out)

    def test_map_wait_survives_a_service_restart(self):
        rc, out = self._run("map", "mapping")
        self.assertEqual(rc, 1)
        self.assertIn("service restarted", out)


class Docs(unittest.TestCase):
    def test_download_links_match_the_version(self):
        v = fanpilot.__version__
        for doc in ("README.md", "docs/index.html"):
            with open(os.path.join(ROOT, doc)) as f:
                text = f.read()
            self.assertIn(f"download/v{v}/fanpilot_{v}_all.deb", text, doc)
            self.assertIn(f"download/v{v}/fanpilot-{v}-1.noarch.rpm", text, doc)
            stale = set(re.findall(r"fanpilot_(\d+\.\d+\.\d+)_all\.deb", text)) - {v}
            self.assertFalse(stale, f"{doc} still names {stale}")
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertRegex(f.read(), rf"(?m)^## {re.escape(v)}( |$)", "CHANGELOG has a section for this version")


if __name__ == "__main__":
    unittest.main()
