#!/bin/bash

# SPDX-FileCopyrightText: 2026 the innova2 contributors
# SPDX-FileCopyrightText: Mellanox Technologies Ltd.
#
# SPDX-License-Identifier: Apache-2.0 AND Linux-OpenIB

# innova2_areg.sh -- the Mellanox "innova2 flex app" capabilities WITHOUT mlx5_fpga_tools.
#
# WHAT THIS REPLACES
# ------------------
# The vendor app talks to the ConnectX-5 through /dev/<bdf>_mlx5_fpga_tools, a chardev created by the
# `mlx5_fpga_tools` module. That module ships only in OFED 5.2-era packages: OFED 23.10 and every DOCA-OFED still
# carry the in-kernel FPGA core (drivers/.../mlx5/core/fpga/{cmd,core,conn,sdk}.c) but DROP tools_char.c, so the
# node is gone and every ioctl/lseek-based tool stops working.
#
# WHY THIS WORKS ANYWAY
# ---------------------
# Everything those ioctls did reduces to ConnectX *access registers* (OFED 5.2 source,
# drivers/net/ethernet/mellanox/mlx5/core/fpga/cmd.c):
#
#   IOCTL_FPGA_QUERY      0x84  -> mlx5_fpga_query           -> FPGA_CTRL 0x4023 GET
#   IOCTL_FPGA_IMAGE_SEL  0x83  -> mlx5_fpga_flash_select    -> FPGA_CTRL 0x4023 SET op=FLASH_SELECT(3)
#   IOCTL_FPGA_CONNECT    0x87  -> mlx5_fpga_connectdisconnect-> FPGA_CTRL 0x4023 SET op=9/0xA
#   read()/write() on node     -> mlx5_fpga_access_reg       -> FPGA_ACCESS_REG 0x4024 {size,address,data}
#   IOCTL_FPGA_CAP        0x85  -> cached FPGA_CAP           -> FPGA_CAP  0x4022 GET
#
# and access registers are reachable from user space with NO FPGA-aware driver at all: MFT's `mlxreg` gets to
# them through the PCI Vendor-Specific Capability gateway in config space (`Capabilities: [c0] Vendor Specific
# Information`), with the MST PCI modules not loaded, and on kernels where mlx5_fpga_tools does not exist.
#
# MFT's register database has no FPGA registers, so they are sent RAW: by ID and length, with the {size, address}
# index fields given as byte.bit:width (positions from include/linux/mlx5/mlx5_ifc_fpga.h; bit 0 is the least
# significant bit of the big-endian dword). FPGA_ACCESS_REG is always sent at 0x110 bytes, the length the kernel
# driver uses (4 + 64 dwords).
#
# REQUIREMENTS: MFT's mlxreg (shipped with MLNX_OFED / DOCA-OFED) or, failing that, mstflint's mstreg
#               (open source, in the distro repos, same flags; untested on the card) -- and root.
#               NOT mlx5_fpga_tools, NOT mst, NOT the mlx5 FPGA driver.
#
# WHAT WORKS AND WHAT DOES NOT (measured, cross-checked against the old ioctl on the same boot):
#   query / cap / crrd / crwr  -- WORK.
#   image-sel / jtag-on / jtag-off -- DO NOT WORK. Every FPGA_CTRL *write* comes back ME_ICMD_OPERATIONAL_ERROR,
#                                 on both the User and the Factory image, while the same operation through
#                                 /dev/<bdf>_mlx5_fpga_tools succeeds seconds later. So the ICMD gateway carries
#                                 FPGA reads and CR writes but not FPGA_CTRL writes. Use innova2_areg_kmod/ for
#                                 those. The subcommands are kept so the failure is reproducible rather than absent.
#
# NOTE ON CR SPACE: the ConnectX refuses FPGA_ACCESS_REG while the User image runs (oper_image == USER). That
# refusal surfaces here as ME_ICMD_OPERATIONAL_ERROR and as EIO through the old driver -- same card state, two
# transports. Put the card on the Flex (or Factory) image before concluding anything about a CR read.
#
#   usage: innova2_areg.sh <cx5-bdf> query
#          innova2_areg.sh <cx5-bdf> cap
#          (image-sel / jtag-on / jtag-off: refused here -- use innova2_app, see below)
#          innova2_areg.sh <cx5-bdf> crrd <hexaddr>
#          innova2_areg.sh <cx5-bdf> crwr <hexaddr> <hexval>
set -u
D=${1:?usage: $0 <cx5-bdf> <cmd> ...}; shift
CMD=${1:?missing command}; shift
TOOL=$(command -v mlxreg || command -v mstreg) || { echo "*** neither mlxreg (MFT) nor mstreg (mstflint) is installed"; exit 1; }
MR="$TOOL -d $D"
# raw register access: id, length (bytes)
CTRL="--reg_id 0x4023 --reg_len 0x10"; CAP="--reg_id 0x4022 --reg_len 0x100"; ACC="--reg_id 0x4024 --reg_len 0x110"
dw(){ echo "$1" | awk -v o="$2" -F'|' '{gsub(/ /,"",$1); gsub(/ /,"",$2)} $1==o {print $2}'; }   # dword at byte offset

case "$CMD" in
query)
  # FPGA_CTRL GET == IOCTL_FPGA_QUERY. Three fields, same three the app's menu header prints.
  # mlx5_ifc_fpga_ctrl_bits: status = dword 0 bits 7:0; flash_select_admin = dword 1 bits 23:16, oper = bits 7:0
  out=$($MR $CTRL --get 2>&1) || { echo "$out"; exit 1; }
  echo "$out" | grep -qiE "Failed|-E-" && { echo "$out"; exit 1; }
  d0=$(dw "$out" 0x00000000); d1=$(dw "$out" 0x00000004)
  [ -n "$d0" ] && [ -n "$d1" ] || { echo "*** unexpected $(basename "$TOOL") output:"; echo "$out"; exit 1; }
  st=$(printf "0x%x" $(( d0 & 0xFF ))); op=$(printf "0x%x" $(( d1 & 0xFF ))); ad=$(printf "0x%x" $(( (d1 >> 16) & 0xFF )))
  nm(){ case $((16#${1#0x})) in 0) echo USER;; 1) echo FACTORY;; 2) echo FACTORY_FAILOVER;; 3) echo FLEX;; *) echo "?";; esac; }
  sn(){ case $((16#${1#0x})) in 0) echo SUCCESS;; 1) echo FAILURE;; 2) echo IN_PROGRESS;; 3) echo DISCONNECTED;; *) echo "?";; esac; }
  echo "FPGA-QUERY admin=$((16#${ad#0x}))($(nm $ad)) oper=$((16#${op#0x}))($(nm $op)) status=$((16#${st#0x}))($(sn $st))   [via FPGA_CTRL 0x4023]"
  ;;
cap)
  $MR $CAP --get
  ;;
image-sel|jtag-on|jtag-off)
  # The firmware refuses every FPGA_CTRL *write* through this path (ME_ICMD_OPERATIONAL_ERROR; measured,
  # see the header). Image select and the JTAG grant need the device node of our kernel module.
  echo "*** $CMD is not possible over $(basename "$TOOL"): the firmware refuses FPGA_CTRL writes on this path."
  echo "    Use: innova2_app -d $D --batch $CMD ${1:-}   (needs innova2_areg.ko or mlx5_fpga_tools)"
  exit 2
  ;;
crrd)
  a=${1:?hex address}
  # mlx5_ifc_fpga_access_reg_bits: size = byte 4 bits 15:0, address_hi = dword 2, address_lo = dword 3, data at 0x10
  out=$($MR $ACC --indexes "0x4.0:16=0x4,0x8.0:32=0x0,0xc.0:32=0x$a" --get 2>&1)
  echo "$out" | grep -qiE "Failed|-E-" && { echo "$out"; exit 1; }
  v=$(dw "$out" 0x00000010); [ -n "$v" ] || { echo "$out"; exit 1; }
  echo "CR 0x$a = $v"
  ;;
crwr)
  a=${1:?hex address}; v=${2:?hex value}
  $MR $ACC --indexes "0x4.0:16=0x4,0x8.0:32=0x0,0xc.0:32=0x$a" --set "0x10.0:32=0x$v" --yes
  ;;
*) echo "*** unknown command '$CMD'"; exit 1;;
esac
