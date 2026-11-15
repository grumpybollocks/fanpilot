# SPDX-License-Identifier: GPL-3.0-or-later
"""Test package setup: nothing a test does may touch the real user's settings.

The GUI keeps small preferences with QSettings (~/.config/FanPilot/...). Point the
config dir at a scratch folder before any test creates a window.
"""
import os
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="fanpilot-test-config-")
# ... and the single-instance lock / update marker go to a scratch runtime dir, not /run/user/<uid>
os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="fanpilot-test-run-")
