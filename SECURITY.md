# Security

FanPilot's service (`fanpilotd`) runs as root and writes fan settings to the kernel, so security reports are
welcome and taken seriously.

**Please report vulnerabilities privately** through GitHub: *Security* tab → *Report a vulnerability*
(https://github.com/grumpybollocks/fanpilot/security/advisories/new). Don't open a public issue for them.

Useful to include: the version (`fanpilotctl status`), how FanPilot is installed, and the steps to reproduce.

What is in scope, for example: a user who is not root, not in `allowed_uids` and not in the `fanpilot` group
changing fan settings through `/run/fanpilot/fanpilot.sock`; making the service write outside the fan files
it manages; anything that leaves a fan stopped or stops FanPilot from handing fans back to the BIOS.

Only the latest release gets security fixes.
