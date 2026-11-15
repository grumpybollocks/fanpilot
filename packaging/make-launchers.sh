#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Write the three launchers for an install where the fanpilot package lives in a private directory
# (install.sh: /opt/fanpilot; .deb/.rpm: /usr/lib/fanpilot).    make-launchers.sh LIBDIR OUTDIR [SUFFIX]
# With SUFFIX (e.g. ".new") each file is written as OUTDIR/.NAME.SUFFIX, ready to be moved into place.
# The app launcher also finds PySide6 in LIBDIR/venv (Ubuntu 24.04 / Debian 12 have no PySide6 package).
set -euo pipefail
lib=$1 out=$2 suffix=${3:-}
mkdir -p "$out"
for name in fanpilotd:daemon fanpilotctl:ctl fanpilot:gui; do
    bin=${name%%:*}; mod=${name##*:}
    target="$out/$bin"
    if [[ -n $suffix ]]; then target="$out/.$bin$suffix"; fi
    {
        echo '#!/usr/bin/python3 -I'
        echo 'import sys'
        echo 'sys.dont_write_bytecode = True   # nothing written into the installed tree at run time'
        echo "sys.path.insert(0, \"$lib\")"
        if [[ $mod == gui ]]; then
            echo 'try:'
            echo '    import PySide6  # noqa: F401'
            echo 'except ImportError:  # no distro package: the private one made by "install.sh --gui-venv"'
            echo '    import glob'
            echo "    sys.path[1:1] = glob.glob(\"$lib/venv/lib/python3*/site-packages\")"
        fi
        echo "from fanpilot.$mod import main"
        echo 'sys.exit(main())'
    } > "$target"
    chmod 755 "$target"
done
