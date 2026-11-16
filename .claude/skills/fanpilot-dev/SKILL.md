---
name: fanpilot-dev
description: Working on FanPilot (this repo) - testing, verifying on real hardware, measuring resource use, releasing, and the privacy rule. Use for any change to fanpilot/, tests/, packaging or docs.
---

# FanPilot development

Read `docs/LESSONS_LEARNT.md` first: every rule there came from a real mistake.

## Rules
- **Safety first** (CONTRIBUTING.md): every fan goes back to its BIOS/card mode on any exit; never write to
  a driver not known to be controllable; CPU fans and pumps never stop. Safety checks run every tick.
- **Privacy:** never write machine host names, user names or email addresses into commits, PR text, docs,
  reports or screenshots. Describe machines by hardware (board, chip, CPU, GPU).
- **Guesses never change protection** (CPU fan, pumps): only the user or the board database does.
- Merge, tag and release only when the user asks.

## Test
```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -t .   # all
FANPILOT_NO_QT=1 python3 -m unittest discover -s tests -t .           # like CI's no-Qt job
```
- Every fix gets a test that **fails on the old code**: `git stash push -q fanpilot/`, run it, `git stash pop -q`.
- Real hardware layouts live in `tests/reports/*.json` (`fanpilotctl report`); load with
  `tests/sim.py: machine_from_report`. Add a report for every new machine.

## Verify on the real machine
- Install: the user runs `sudo ./install.sh` in their own terminal (the assistant's shell can't prompt).
- Check: `diff -rq fanpilot /opt/fanpilot/fanpilot -x __pycache__`, `journalctl -u fanpilotd`,
  `fanpilotctl status|sensors|events`, `/run/fanpilot/restore.json` empty after any job.
- Never claim "fixed" without seeing it work on the hardware (or say plainly that it wasn't).

## Measure resource use
- CPU: `/proc/PID/schedstat` field 1 (ns) over 60 s, **not** `/proc/PID/stat` (tick-sampled, misleading).
- Kernel cost dominates: profile sysfs reads read-only on the real machine (NVMe, amdgpu, Super I/O).

## Release
1. Version in `fanpilot/__init__.py` and `packaging/arch/PKGBUILD`; `## X.Y.Z` section in CHANGELOG.md
   (release notes are cut from it; `tests/test_release101.py` checks it).
2. Merge to `main`, `git tag -a vX.Y.Z`, push the tag: `.github/workflows/release.yml` builds .deb/.rpm.
3. Download the assets and search them for machine names before announcing.

## Git on a fresh machine
Repo-local identity (the noreply address from earlier commits); push with
`git -c credential.helper= -c credential.helper='!gh auth git-credential' push`.
