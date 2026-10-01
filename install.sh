#!/bin/bash

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

# Install innova2_app on a host that has an Innova-2 card.   Run as root from the unpacked folder.
#
#   sudo ./install.sh                  copy to /opt/innova2_app, link the commands, DKMS-install the module
#   sudo ./install.sh --no-kmod        same, without the kernel module (read-only / CR use through MFT only)
#   sudo ./install.sh --prefix <dir>   install somewhere else
#   sudo ./install.sh --uninstall      remove the links, the copy and the module
#
# The app finds its sidecar file (rawspi.py) next to its REAL path,
# so the /usr/local/bin links work from anywhere.
set -eu
PREFIX=/opt/innova2_app; KMOD=1; MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX=${2:?}; shift 2;;
    --no-kmod) KMOD=0; shift;;
    --uninstall) MODE=uninstall; shift;;
    *) sed -n '2,10p' "$0"; exit 1;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "*** run as root"; exit 1; }
SRC=$(dirname "$(readlink -f "$0")")
LINKS="innova2_app innova2_areg"

if [ $MODE = uninstall ]; then
  for l in $LINKS; do rm -f /usr/local/bin/$l; done
  [ -x "$PREFIX/innova2_areg_kmod/build.sh" ] && "$PREFIX/innova2_areg_kmod/build.sh" uninstall || true
  rm -rf "$PREFIX"
  echo "uninstalled from $PREFIX"; exit 0
fi

command -v python3 >/dev/null || { echo "*** python3 is required"; exit 1; }
mkdir -p "$PREFIX"
( cd "$SRC" && tar --exclude=.git -cf - . ) | ( cd "$PREFIX" && tar -xf - )
ln -sf "$PREFIX/innova2_app.py"      /usr/local/bin/innova2_app
ln -sf "$PREFIX/innova2_areg.sh"     /usr/local/bin/innova2_areg
echo "installed to $PREFIX; commands: innova2_app, innova2_areg"

command -v mlxreg >/dev/null && echo "MFT mlxreg: $(command -v mlxreg)" \
  || echo "note: MFT (mlxreg) not found -- the 'mlxreg' transport is unavailable; the module path still works"
[ -x /opt/xilinx/xrt/bin/xbflash.qspi ] && echo "XRT xbflash.qspi: present (burn-inshell available)" \
  || echo "note: /opt/xilinx/xrt/bin/xbflash.qspi not found -- burn-inshell unavailable (BOPE burning still works)"

if [ $KMOD = 1 ]; then
  if ls /dev/*_mlx5_fpga_tools >/dev/null 2>&1 && ! lsmod | grep -q '^innova2_areg '; then
    echo "a vendor mlx5_fpga_tools node already exists -- not installing innova2_areg (not needed)"
  else
    command -v dkms >/dev/null || { echo "*** dkms is required for the module (apt install dkms), or use --no-kmod"; exit 1; }
    "$PREFIX/innova2_areg_kmod/build.sh" install
  fi
fi
echo "done. Try:  sudo innova2_app --batch query"
