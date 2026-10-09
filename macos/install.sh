#!/bin/sh
# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Install innova2-smi for the current user on macOS (XRT macOS port): ~/.local/lib/innova2-smi + ~/.local/bin wrapper.
#   macos/install.sh            (run macos/build.sh first)
set -e
here=$(cd "$(dirname "$0")" && pwd); src=$(cd "$here/.." && pwd)
lib=$HOME/.local/lib/innova2-smi; bin=$HOME/.local/bin
[ -x "$here/innova2_regio" ] || { echo "*** build the helper first: macos/build.sh"; exit 1; }
mkdir -p "$lib" "$bin"
cp "$src"/innova2_smi*.py "$src"/innova2_app.py "$src"/VERSION "$lib/"
cp "$here/innova2_regio" "$lib/"
printf '#!/bin/sh\nexec /usr/bin/python3 "%s/innova2_smi.py" "$@"\n' "$lib" > "$bin/innova2-smi"
chmod +x "$bin/innova2-smi"
echo "installed: $bin/innova2-smi ($(cat "$src/VERSION")); helper $lib/innova2_regio"
case ":$PATH:" in *":$bin:"*) ;; *) echo "add $bin to PATH";; esac
