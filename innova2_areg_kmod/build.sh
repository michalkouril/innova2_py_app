#!/bin/bash

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: GPL-2.0-only

# Build (and optionally DKMS-install) innova2_areg.ko against whatever mlx5_core this machine runs.
#
#   ./build.sh            build here and say how to load it
#   ./build.sh install    register with DKMS so it rebuilds on every kernel update, and load it
#                         at boot. THIS is what you want on a machine you will reboot.
#   ./build.sh uninstall  remove both of the above
#
# THE ONE THING THAT GOES WRONG: with CONFIG_MODVERSIONS (every Ubuntu kernel) the loader checks
# the CRC of mlx5_core_access_reg, and modpost only stamps that CRC if it can see the Module.symvers
# of the build that EXPORTED it. Both OFED and the in-box kernel export the symbol, with DIFFERENT
# CRCs -- picking the wrong one produces a module that compiles cleanly and then refuses to load.
# The Makefile picks it (OFED's first, kernel's own as fallback); this script just reports it.
set -eu
cd "$(dirname "$(readlink -f "$0")")"
K=$(uname -r)
VER=1.0
NAME=innova2-areg

# The innova2-areg-dkms package registers the same DKMS name and version; leave it to dpkg.
case "${1:-build}" in install|uninstall)
  if dpkg-query -W -f='${Status}' innova2-areg-dkms 2>/dev/null | grep -q 'install ok installed'; then
    echo "the innova2-areg-dkms package manages this module -- use apt/dpkg instead (skipping '$1')"
    exit 0
  fi;;
esac

case "${1:-build}" in
uninstall)
  sudo rmmod innova2_areg 2>/dev/null || true
  sudo rm -f /etc/modules-load.d/innova2_areg.conf
  sudo dkms remove -m $NAME -v $VER --all 2>/dev/null || true
  sudo rm -rf /usr/src/$NAME-$VER
  echo "removed: DKMS registration, /etc/modules-load.d/innova2_areg.conf, loaded module"
  exit 0
  ;;
install)
  SV=$(make -s symvers KVER="$K")
  echo "Module.symvers for $K: $SV"
  grep -m1 mlx5_core_access_reg "$SV" || { echo "*** that symvers does not export mlx5_core_access_reg"; exit 1; }
  sudo rm -rf /usr/src/$NAME-$VER
  sudo mkdir -p /usr/src/$NAME-$VER
  sudo cp innova2_areg.c Makefile dkms.conf /usr/src/$NAME-$VER/
  sudo dkms remove -m $NAME -v $VER --all 2>/dev/null || true
  sudo dkms add -m $NAME -v $VER
  sudo dkms build -m $NAME -v $VER
  sudo dkms install -m $NAME -v $VER --force
  # Load at boot. mlx5_core is already up by then (it is a PCI driver bound during boot), and the
  # module simply finds no device and says so if it is not.
  echo innova2_areg | sudo tee /etc/modules-load.d/innova2_areg.conf >/dev/null
  sudo modprobe -r innova2_areg 2>/dev/null || true
  sudo modprobe innova2_areg
  echo
  dkms status -m $NAME
  ls -l /dev/*_mlx5_fpga_tools 2>/dev/null || echo "*** no node appeared -- check dmesg"
  echo
  echo "Installed. It will rebuild itself on the next kernel update and load at boot."
  exit 0
  ;;
esac

SV=$(make -s symvers KVER="$K")
echo "using Module.symvers: $SV"
grep -m1 mlx5_core_access_reg "$SV" || { echo "*** no mlx5_core_access_reg there"; exit 1; }
make clean >/dev/null 2>&1 || true
make KVER="$K" all
echo
modinfo ./innova2_areg.ko | head -5
echo
echo "load once:      sudo insmod ./innova2_areg.ko"
echo "install properly: ./build.sh install   (DKMS + load at boot)"
