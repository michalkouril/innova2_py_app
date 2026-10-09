#!/bin/sh
# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Build innova2_regio (innova2-smi's register helper on macOS) against the XRT macOS port's dext ABI header.
#   macos/build.sh [<path to the port's XrtUser dir, containing xrt_dext_abi.h>]
# Default: ~/src/XRT/src/runtime_src/core/pcie/driver/macos/XrtUser. Rebuild after every dext ABI change: the helper
# refuses to run against a dext whose ABI version differs from the header it was built with.
set -e
here=$(cd "$(dirname "$0")" && pwd)
abi=${1:-$HOME/src/XRT/src/runtime_src/core/pcie/driver/macos/XrtUser}
[ -f "$abi/xrt_dext_abi.h" ] || { echo "*** no xrt_dext_abi.h in $abi"; exit 1; }
clang++ -std=c++17 -O2 -Wall -I"$abi" -framework IOKit -framework CoreFoundation \
  -o "$here/innova2_regio.new" "$here/innova2_regio.cpp"
mv "$here/innova2_regio.new" "$here/innova2_regio"
echo "built $here/innova2_regio (dext ABI $(sed -n 's/^#define XRT_DEXT_ABI_VERSION *\([0-9]*\).*/\1/p' "$abi/xrt_dext_abi.h"))"
