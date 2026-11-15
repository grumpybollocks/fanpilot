---
name: Bug report
about: Something doesn't work, or a fan/sensor is missing or wrong
labels: bug
---

**What happened** (and what you expected):

**Your PC**
- Distribution and version:
- How FanPilot is installed (AUR / .deb / .rpm / install.sh):
- `fanpilotctl status` output:

**Hardware report** (anonymised: no serial numbers, host or user names)

Run `fanpilotctl report > fanpilot-report.json` and attach the file. It lets us add your board to the
simulator, so the fix gets a test.

**Logs**

```
journalctl -u fanpilotd -b --no-pager | tail -n 100
```
