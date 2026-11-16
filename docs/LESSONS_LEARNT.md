# Lessons learnt

Mistakes made while building and testing FanPilot, and the rule each one taught. Kept so the same
mistake isn't made twice. Newest first.

## Second real machine (ASUS TUF GAMING X570-PLUS, NCT6798, Ryzen 7 5700X3D, RX 9070 XT), 1.0.1 → next

| What went wrong | Rule |
|---|---|
| A new test passed even **without** the fix it was written for (it used made-up fan ids the service rejected, so "nothing happened" either way). | Run every new test against the old code (`git stash push fanpilot/`) and see it **fail** before trusting it. |
| Restoring a file with `cp` silently did nothing: the shell aliases `cp` to `cp -i`, which waited for an answer that never came, leaving a safety guard deleted. | In scripts use `command cp -f`; after restoring, grep for the line you expect. |
| An automated edit of `CHANGELOG.md` dropped the `## 1.0.1` heading; only the release-notes test noticed. | Run the **full** suite after editing docs too, not only after code. |
| The user took the app's "Identifying" label as proof of which fan was on the CPU cooler. It only says which header was *told* to pulse. | The UI must never look like proof of something it can't know. Hence *Find my CPU fan*: pulse, then ask about the cooler. |
| CPU use was first measured with `/proc/PID/stat` (10 ms timer-tick sampling): it read 1.1 % one minute and 1.8 % the next for the same code. | Measure a bursty process with `/proc/PID/schedstat` (exact nanoseconds). Compare before/after only with the same method. |
| The simulator showed ~1 ms per tick, the real machine 11 ms: the cost is inside kernel drivers, which the simulator doesn't model. | Profile real sysfs reads on real hardware (read-only scripts). Measured: an NVMe temperature read is a SMART command to the drive (0.6–7 ms); an amdgpu reading costs ~1 ms when its firmware data is older than ~1 ms; an NCT6798 `pwmN` read 0.35 ms. |
| The graphics card measurement checked the card's temperature only between its 10 s steps. | Safety checks run every tick, never behind a step timer. |
| A type check meant for "RX 7000/9000 cards" also let older AMD cards (pwm1 beta) into the motherboard-fan measurement. | Gate on the exact class and kind, and test the case that must be refused. |
| Mapping proved headers empty but threw the proof away while the CPU fan was unknown, forcing a second 100 s run. | Keep what a run proved; apply it once the safety condition (CPU fan known) is met. |
| Unconnected NCT6798 `PCH_*` channels read exactly 0 °C and were shown as real sensors. | On an unknown board, exactly 0 °C (like -128 / 127) means nothing connected. |
| The curve editor let a GPU point be dragged to 6 % while the card's firmware minimum is 30 %. | The editor enforces the hardware's own reported limits (`OD_RANGE`), and shows what the hardware really runs. |
| BIOS-setup guesses could have been used to mark the CPU fan automatically. | Guesses are hints and an order, never protection changes: three headers followed the CPU temperature on this board. |

## Working on the repo

| What went wrong | Rule |
|---|---|
| `sudo` from inside the assistant's shell can't ask for a password. | Ask the user to run `sudo ./install.sh` in their own terminal. |
| A second machine had no git identity and git couldn't push (no credential helper). | Repo-local identity (the noreply address); push with `git -c credential.helper='!gh auth git-credential' push`. |
| A combined `git push origin <tag> <branch> --delete` was malformed and pushed nothing. | One push per purpose; check `git ls-remote` afterwards. |
| An unrequested PR merge was (rightly) refused. | Merge and release only when asked; open the PR and report CI. |
| Machine names must never appear publicly. | Before pushing: `git log -p` and the release packages are searched for machine names; describe machines by hardware only. |
