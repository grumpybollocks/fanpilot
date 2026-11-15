# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI tests need PySide6. On a machine without it (Ubuntu 24.04, Debian 12) the service still installs:
install.sh runs the suite with FANPILOT_NO_QT=1 and these tests are skipped."""
import importlib.util
import os
import unittest

HAVE_QT = importlib.util.find_spec("PySide6") is not None and os.environ.get("FANPILOT_NO_QT") != "1"
needs_qt = unittest.skipUnless(HAVE_QT, "PySide6 not available")
