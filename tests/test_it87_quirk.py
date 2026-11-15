# SPDX-License-Identifier: GPL-3.0-or-later
"""it87 reports a manual fan at duty 255 as pwm_enable 0 (seen on the real IT8688E, it87.c pwm_mode()).
That must never be mistaken for the BIOS taking the fan back."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_daemon import Base  # noqa: E402


class It87FullSpeedQuirk(Base):
    def ticks(self, d, n=1, dt=1.0):
        for _ in range(n):
            self.now += dt
            d.tick(self.now, dt)
            self.m.it87_report()

    def reclaims(self, d):
        return [e for e in d.events if "taken back" in e["msg"] or "keeps reclaiming" in e["msg"]]

    def test_fixed_100_is_not_a_reclaim(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 100}}})
        self.ticks(d, 20)
        self.assertEqual(self.reclaims(d), [])
        self.assertNotIn("it8688/pwm2", d.disabled)
        self.assertEqual((self.m.enable(2), self.m.pwm(2)), (0, 255))
        st = {f["id"]: f for f in d.status()["fans"]}["it8688/pwm2"]
        self.assertEqual((st["duty"], st["bios"]), (100.0, False))
        # lowering works from the mode-0 state
        d.set_config({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 40, "ramp_down": 100}}})
        self.ticks(d, 2)
        self.assertEqual((self.m.enable(2), self.m.pwm(2)), (1, round(40 * 2.55)))
        self.assertEqual(self.reclaims(d), [])

    def test_critical_full_speed_never_gives_up(self):
        d = self.daemon()
        self.m.cpu(95)
        self.ticks(d, 30)
        self.assertEqual(self.reclaims(d), [])
        self.assertEqual(d.disabled, {})
        self.assertEqual([self.m.pwm(i) for i in (1, 2, 3)], [255] * 3)
        self.m.cpu(60)
        self.ticks(d, 2)
        self.assertEqual([self.m.enable(i) for i in (1, 2, 3)], [2] * 3)  # back to BIOS

    def test_identify_with_quirk(self):
        d = self.daemon({"allowed_uids": [1000], "fans": {"it8688/pwm3": {"mode": "fixed", "fixed": 40}}})
        self.ticks(d)
        d.handle({"cmd": "identify", "fan": "it8688/pwm3"}, 1000)
        self.ticks(d, 9)
        self.assertEqual(self.reclaims(d), [])
        self.assertEqual((self.m.enable(3), self.m.pwm(3)), (1, round(40 * 2.55)))

    def test_real_reclaim_to_mode_0_still_detected(self):
        d = self.daemon({"fans": {"it8688/pwm2": {"mode": "fixed", "fixed": 40}}})
        self.ticks(d)
        for _ in range(5):
            self.m.set_enable(2, 0)          # something switched it to full speed; duty is not 255
            self.ticks(d)
        self.assertIn("it8688/pwm2", d.disabled)


if __name__ == "__main__":
    unittest.main()
