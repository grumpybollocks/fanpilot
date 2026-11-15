# SPDX-License-Identifier: GPL-3.0-or-later
"""fanpilotctl — command line access to fanpilotd."""
from __future__ import annotations

import argparse
import json
import sys

from . import SOCKET_PATH
from .client import Client, DaemonError


def _fmt(v, unit=""):
    return "—" if v is None else f"{v:g}{unit}" if isinstance(v, (int, float)) else str(v)


def _sensors_loop(cl, watch: bool) -> None:
    import time
    while True:
        st = cl.call("status")["status"]
        titles = {g["id"]: g["title"] for g in st.get("groups", [])}
        out, group = [], None
        for r in st.get("readings", []):
            if r["group"] != group:
                group = r["group"]
                out.append(("\n" if out else "") + titles.get(group, group))
            v = "—" if r["value"] is None else f"{r['value']:.{r['decimals']}f} {r['unit']}"
            out.append(f"  {r['label']:<30} {v:>12}{'  !' if r['alert'] else ''}")
        if not out:
            out.append("no readings (FanPilot older than 0.4.0?)")
        if watch:
            print("\033[H\033[2J" + time.strftime("%H:%M:%S") + "   (! = past a limit; Ctrl+C to stop)\n")
        print("\n".join(out), flush=True)
        if not watch:
            return
        time.sleep(1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fanpilotctl", description="Control the FanPilot service")
    ap.add_argument("--socket", default=SOCKET_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show fans and temperatures")
    se = sub.add_parser("sensors", help="every reading, grouped like the app's Sensors tab")
    se.add_argument("-w", "--watch", action="store_true", help="refresh every second (Ctrl+C to stop)")
    sub.add_parser("config", help="print the current config as JSON")
    s = sub.add_parser("set", help="change one fan")
    s.add_argument("fan", help="fan id, e.g. it8688/pwm2 or amdgpu/fan1")
    s.add_argument("mode", choices=["auto", "fixed", "curve"])
    s.add_argument("value", nargs="?", help="percent for fixed, or curve as 40:30,60:45,80:100")
    c = sub.add_parser("calibrate", help="measure a motherboard fan's stop/start speed, or a graphics card fan's lowest speed")
    c.add_argument("fan", nargs="?")
    c.add_argument("--stop", action="store_true", help="stop a running measurement")
    sub.add_parser("events", help="show recent events")
    mp = sub.add_parser("map", help="find which header drives which fan (about 15 s per header)")
    mp.add_argument("--stop", action="store_true")
    mp.add_argument("--forget", action="store_true", help="drop the saved map (hidden headers come back)")
    sub.add_parser("report", help="print an anonymised hardware report (JSON) for bug reports; no root needed")
    rn = sub.add_parser("rename", help="give a fan or sensor your own name (empty name = back to default)")
    rn.add_argument("id", help="fan or sensor id, e.g. nct6798/pwm2 or nvme/temp1")
    rn.add_argument("name", nargs="?", default="")
    cf = sub.add_parser("cpu-fan", help="mark which motherboard fans are CPU fans (they never stop)")
    cf.add_argument("fans", nargs="*", help="fan ids; none = show the current ones")
    i = sub.add_parser("identify", help="run one motherboard fan slow for 3 s, then at full speed for 3 s")
    i.add_argument("fan")
    pr = sub.add_parser("profile", help="list, apply, save or delete whole-machine profiles")
    pr.add_argument("action", choices=["list", "apply", "save", "delete"])
    pr.add_argument("name", nargs="?")
    sub.add_parser("selftest", help="test fans, crash recovery and GPU control (about 45 s)")
    im = sub.add_parser("import", help="load settings from a JSON file (as saved by 'config')")
    im.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "report":               # reads /sys only: works without the service
        from . import report
        return report.main()
    cl = Client(a.socket)
    try:
        if a.cmd == "status":
            st = cl.call("status")["status"]
            print(f"FanPilot {st['version']}  board chip: {st['board_chip'] or 'not found'}"
                  f"  profile: {st.get('active_profile') or 'custom'}"
                  f"{'  ** CRITICAL **' if st['critical'] else ''}")
            for h in st.get("diagnosis") or []:
                print(f"  hint: {h}")
            print("\nFans:")
            for f in st["fans"]:
                print(f"  {f['id']:<16} {f['label']:<34} {f.get('mode', '?'):<6} "
                      f"{_fmt(f.get('rpm'), ' rpm'):>9} {('BIOS' if f.get('bios') else _fmt(f.get('duty'), ' %')):>8} "
                      f"{' '.join(f.get('flags') or [])}")
            print("\nTemperatures:")
            for s_ in st["sensors"]:
                print(f"  {s_['id']:<20} {s_['label']:<26} {_fmt(s_['value'], ' °C'):>9}")
        elif a.cmd == "sensors":
            try:
                _sensors_loop(cl, a.watch)
            except KeyboardInterrupt:
                pass
        elif a.cmd == "config":
            print(json.dumps(cl.call("get_config")["config"], indent=2))
        elif a.cmd == "set":
            settings = {"mode": a.mode}
            if a.mode == "fixed":
                if a.value is None:
                    ap.error("fixed needs a percent")
                settings["fixed"] = float(a.value)
            elif a.mode == "curve" and a.value:
                settings["curve"] = [[float(x) for x in p.split(":")] for p in a.value.split(",")]
            cl.call("set_fan", fan=a.fan, settings=settings)
            print("ok")
        elif a.cmd == "calibrate":
            if a.stop:
                cl.call("calibrate_cancel")
                print("stopped")
            elif not a.fan:
                ap.error("calibrate needs a fan id (or --stop)")
            else:
                cl.call("calibrate", fan=a.fan)
                print("calibration started; watch it with: fanpilotctl status   (stop: fanpilotctl calibrate --stop)")
        elif a.cmd == "identify":
            r = cl.call("identify", fan=a.fan)
            half = r.get("seconds", 6) / 2
            print(f"slow for {half:g} s, then full speed for {half:g} s")
        elif a.cmd == "profile":
            if a.action == "list":
                st = cl.call("status")["status"]
                for n in st["profiles"]:
                    print(("* " if n == st.get("active_profile") else "  ") + n)
            else:
                if not a.name:
                    ap.error(f"profile {a.action} needs a name")
                cl.call(f"{a.action}_profile", name=a.name)
                print("ok")
        elif a.cmd == "selftest":
            import time
            cl.call("selftest")
            print("self-test running (about 45 s)...")
            shown = 0
            try:
                while True:
                    time.sleep(1)
                    st = cl.call("status")["status"]["selftest"]
                    if not st:                         # the service restarted: the test was stopped
                        print("stopped: the service restarted; fans are back to their settings")
                        return 1
                    for r in st["results"][shown:]:
                        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[r["ok"]]
                        print(f"  {mark}  {r['name']}: {r['detail']}")
                    shown = len(st["results"])
                    if st["state"] != "running":
                        print(f"result: {st['passed']} passed, {st['failed']} failed")
                        return 0 if st["failed"] == 0 else 1
            except KeyboardInterrupt:
                Client(a.socket).call("selftest_cancel")   # fresh connection: the old one may be mid-call
                print("\nstopped; your settings are restored")
                return 130
        elif a.cmd == "import":
            with open(a.file) as f:
                cfg = json.load(f)
            cl.call("set_config", config=cfg)
            print("ok (allowed_uids and calibration are only changed when run as root)")
        elif a.cmd == "map":
            import time
            if a.stop:
                cl.call("map_cancel")
                print("stopped")
                return 0
            if a.forget:
                cl.call("map_forget")
                print("fan map forgotten")
                return 0
            cl.call("map_fans")
            print("mapping (every fan at full speed, one at a time slowed to 30 %)...")
            try:
                while True:
                    time.sleep(2)
                    mpi = cl.call("status")["status"]["mapping"]
                    if not mpi:                        # the service restarted: mapping was stopped
                        print("stopped: the service restarted; fans are back to their settings")
                        return 1
                    if mpi["state"] != "running":
                        print("\n".join(mpi["details"]) or mpi["step"])
                        return 0 if mpi["state"] == "done" else 1
                    print(f"  {mpi['step']}")
            except KeyboardInterrupt:
                Client(a.socket).call("map_cancel")
                print("\nstopped; every fan is back to its setting")
                return 130
        elif a.cmd == "rename":
            cl.call("rename", id=a.id, name=a.name)
            print("ok")
        elif a.cmd == "cpu-fan":
            if a.fans:
                cl.call("set_cpu_fans", fans=a.fans)
            st = cl.call("status")["status"]
            if "cpu_protected" not in st:
                print(f"the running service is FanPilot {st.get('version')}: update it to use this")
                return 1
            prot = st.get("cpu_protected", [])
            print("CPU fan protection (never stops, at least 20 %): " + (", ".join(prot) or "none"))
            if not st.get("cpu_fan_known", True):
                print("Nobody knows which fan is the CPU fan yet, so every motherboard fan is protected.\n"
                      "Mark it with: fanpilotctl cpu-fan <fan id>")
        elif a.cmd == "events":
            import time
            for e in cl.call("status")["status"]["events"]:
                print(time.strftime("%H:%M:%S", time.localtime(e["t"])), e["level"].upper(), e["msg"])
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
