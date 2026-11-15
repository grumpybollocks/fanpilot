#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build dist/fanpilot_<version>_all.deb and dist/fanpilot-<version>-1.noarch.rpm with nFPM.
#   packaging/build-packages.sh [path/to/nfpm]
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
nfpm=${1:-nfpm}
version=$(python3 -c 'import re; print(re.search(r"__version__ = \"(.+?)\"", open("fanpilot/__init__.py").read()).group(1))')
rm -rf build/pkg                      # a clean tree every time: a removed module must not ship
mkdir -p build/pkg/lib/fanpilot build/pkg/bin dist
install -m 644 fanpilot/*.py build/pkg/lib/fanpilot/
bash packaging/make-launchers.sh /usr/lib/fanpilot build/pkg/bin
sed 's|@BINDIR@|/usr/bin|g' packaging/fanpilotd.service > build/pkg/fanpilotd.service
for fmt in deb rpm; do
    FANPILOT_VERSION=$version "$nfpm" package -f packaging/nfpm/nfpm.yaml -p "$fmt" -t dist/
done
ls -l dist/
