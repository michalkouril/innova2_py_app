#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
# SPDX-FileCopyrightText: Mellanox Technologies Ltd.
#
# SPDX-License-Identifier: Apache-2.0 AND Linux-OpenIB

"""innova2_app.py -- a working replacement for Mellanox's `innova2_flex_app` on modern drivers.

WHY THIS EXISTS
---------------
The vendor app talks to the ConnectX-5 through /dev/<bdf>_mlx5_fpga_tools, a chardev created by the
`mlx5_fpga_tools` module.  That module ships ONLY in OFED 5.2-era packages.  OFED 23.10 and every
DOCA-OFED still carry the in-kernel FPGA core (mlx5_fpga_query / mlx5_fpga_image_select /
mlx5_fpga_access_reg are all still in .../mlx5/core/fpga/cmd.c) but they DROP tools_char.c, so the
node never appears and the vendor binary dies at startup.  Measured under DOCA-OFED 3.5.0
(`OFED-internal-26.07-0.7.7`): `modinfo mlx5_fpga_tools` -> "Module not found".

WHAT IT IS
----------
Every menu item the vendor app offers is, underneath, one of three ConnectX access registers -- so
this reimplements the app on top of those registers directly, picking whichever transport is
available at run time:

  FPGA_CTRL       0x4023  read   -> image query, JTAG-grant state, ConnectX status
  FPGA_CTRL       0x4023  write  -> image select, JTAG grant/revoke, reload, reset
  FPGA_ACCESS_REG 0x4024  r/w    -> the whole CR map: identity, temperature, fan, power, BIST
  FPGA_CAP        0x4022  read   -> capabilities

THE TWO TRANSPORTS, AND WHY BOTH ARE NEEDED (all measured)
----------------------------------------------------------
  "node"   -- /dev/<bdf>_mlx5_fpga_tools.  Supplied either by the vendor mlx5_fpga_tools (OFED 5.2)
              or, on any modern kernel, by innova2_areg_kmod/innova2_areg.ko (this package), which
              re-creates the SAME node with the SAME ioctl numbers on top of the exported
              mlx5_core_access_reg().  Does everything.
  "mlxreg" -- MFT through the PCI Vendor-Specific Capability (ICMD) gateway, the FPGA registers sent raw
              (by ID and length).  Needs NO kernel module at all and survives any OFED.
              Does FPGA_CAP read, FPGA_CTRL read, and FPGA_ACCESS_REG read AND write.
              It CANNOT do FPGA_CTRL writes: image select and the JTAG grant both come back
              ME_ICMD_OPERATIONAL_ERROR, on every image, while the identical operation through the
              node succeeds seconds later in the same script.

So: read-only and CR work needs nothing installed; image select and the JTAG grant need the module.
This app reports which transport it is using and which menu items are unavailable, rather than
offering an item that will fail.

  (DEVX was tried and is a dead end: ACCESS_REG 0x805 is not on the kernel's devx_is_general_cmd()
   whitelist, so it is rejected with EINVAL before it reaches firmware.)

WHAT IS FAITHFUL TO THE VENDOR APP
----------------------------------
The register addresses, bit offsets/widths, the temperature formula, the fan-RPM formula, the
power-level ladder and the per-image menu structure are all transcribed from the vendor sources
(`app/fpga_access.c`, `app/interactive.c` in Innova_2_Flex_Open_18_12), not invented.  Like the
vendor app, the menu shown depends on which image is RUNNING.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
**Flash burning** (2026-09-27): now IN the app, exactly as the vendor does it -- `-b <file>[,<flash>[,<offset>]]`
per-chip .bin files, burned through the Flex image's BOPE endpoint (15b3:0264 BAR0, the vendor mailbox
protocol, two passes, the vendor's offset validation and confirmations), plus a sampled rawspi read-back
[ext]. The in-shell path (stock xbflash through a running shell that reaches BOTH flash chips) is
`--batch burn-inshell <mcs-prefix>`; the vendor refuses to burn in user mode, so it is not in the menus.

CR SPACE IS REFUSED WHILE THE USER IMAGE RUNS.  That is the ConnectX's behaviour, not a bug here
(measured): every CR read returns EIO through the node and
ME_ICMD_OPERATIONAL_ERROR through mlxreg.  The app detects it and says so.

  usage:  innova2_app.py [-hv] [-b <arg>] [-p <bope device>] [-d <mst device>] [-s <size>] ...
          (the innova2_flex_app 18.07.00 options; -h prints them) plus [ext] --batch <cmd>, --transport, --yes
          innova2_app.py -d 05:00 -b user_primary.bin,0 -b user_secondary.bin,1     (burn from the menu, item 6)
          innova2_app.py -d 05:00 --batch health
  run as root.
"""

import argparse
import array
import fcntl
import glob
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))

# ---------------------------------------------------------------- vendor constants
IMAGES = {0: "Innova2 User Image", 1: "Innova2 Factory Image",
          2: "Innova2 Image failover", 3: "Innova2 Flex Image"}
# THE VENDOR LABELS VALUE 1 TWO DIFFERENT WAYS, AND IT LOOKS LIKE A MIS-BOOT.
# burn_app.c has two tables:
#     g_image_str[]          [1] = "Innova2 Factory Image"   <- used for the RUNNING image
#     g_image_sheduled_str[] [1] = "Innova2 Flex Image"      <- used for the SCHEDULED image
# and the menu item "Set Innova2_Flex image active" calls set_fpga_image(MLX_ACCEL_IMAGE_FACTORY),
# i.e. VALUE 1. So scheduling "Flex" and then booting "Factory" is ONE value reported by two tables,
# not the card ignoring the request. True FLEX is enum 3 and the vendor menu can never reach it.
# The behaviour is kept (it is the vendor's), but every line that prints an image now prints the
# NUMBER too, because the names alone are what made this look like a fault.
SCHED_NAMES = {0: "Innova2 User Image", 1: "Innova2 Flex Image",
               2: "Innova2 Image failover", 3: "Innova2 Flex Image"}
STATUS = {0: "SUCCESS", 1: "FAILURE", 2: "IN_PROGRESS", 3: "DISCONNECTED"}
BIST_STATUS = {0: "not started", 1: "in progress", 2: "success", 3: "failure"}

# HOW TO TELL A GOOD BOOT FROM A FALLBACK.
# The boot chain is measured (work/docs/innova2_boot_chain.md):
#   power on -> FACTORY at flash 0x0, whose header is WBSTAR 0x03000000 + CMD IPROG, so it jumps
#   UNCONDITIONALLY to the Flex slot and NEVER stays resident -> the FLEX-SLOT image runs, and this
#   is the one the ConnectX calls index 1. The vendor's running-image table labels index 1
#   "Innova2 Factory Image", which is why oper=1 looks like the golden image and is not.
# So a card sitting at oper=1 is running the FLEX SLOT. Reaching the golden image and staying there
# is not a normal outcome; it means the jump failed, and the ConnectX says so:
#   oper=2 FACTORY_FAILOVER  -- the chain did not reach the selected image (recorded with
#                               status=1 and JTAG DONE=0, and usually NO PCIe endpoint at all)
#   status=1 FAILURE         -- the ConnectX's boot-time health check rejected what did come up;
#                               it is a SEPARATE axis from oper (oper=1 status=1 has been seen:
#                               the Flex slot ran and was rejected anyway)
VERDICTS = {
    (0, 0): ("OK", "User image running and accepted."),
    (1, 0): ("OK", "FLEX-SLOT image running and accepted. (The ConnectX calls this slot index 1 "
                   "and the vendor labels it 'Factory' when running -- it is the Flex slot, "
                   "0x03000000, not the golden image at 0x0.)"),
    (3, 0): ("OK", "FLEX image (enum 3) running and accepted."),
    (2, 0): ("FALLBACK", "FACTORY_FAILOVER: the boot chain did not reach the selected image."),
    (2, 1): ("FALLBACK", "FACTORY_FAILOVER + FAILURE: the selected image did not come up and the "
                         "ConnectX rejected the boot. This is the classic dead-Flex-slot state."),
    (0, 1): ("REJECTED", "User image running but the ConnectX's health check FAILED."),
    (1, 1): ("REJECTED", "The Flex-slot image ran but the ConnectX REJECTED it at boot "
                         "(status=1). The image configured; it did not pass the verdict."),
}

IMG_USER, IMG_FACTORY, IMG_FAILOVER, IMG_FLEX = 0, 1, 2, 3
OP_LOAD, OP_RESET, OP_FLASH_SELECT = 1, 2, 3
OP_DISCONNECT, OP_CONNECT = 9, 0xA

# CR register map, verbatim from app/fpga_access.c (addr, bit offset, width)
R_POWER        = (0x000024, 0, 16)
R_TEMPERATURE  = (0x008400, 0, 16)
R_START_BIST   = (0x020000, 0, 1)
R_CYCLIC_BIST  = (0x020004, 0, 1)
R_STOP_ON_FAIL = (0x020004, 1, 1)
R_BIST_STATUS  = (0x020004, 2, 2)
R_BIST_TYPE    = (0x020028, 0, 2)
R_CYCLIC_EN    = (0x020028, 4, 1)
R_LFSR_ADDR_EN = (0x020038, 0, 1)
R_IMAGE_VER    = (0x900000, 0, 32)
R_IMAGE_DATE   = (0x900004, 0, 32)
R_IMAGE_TIME   = (0x900008, 0, 32)
R_FPGA_DEVICE  = (0x90000C, 0, 32)
R_ADABE_VER    = (0x900010, 0, 32)
R_FPGA_TYPE    = (0x90006C, 0, 16)
R_TACHO_START  = (0x00041C, 0, 1)
R_TACHO_DONE   = (0x000420, 0, 1)
R_TACHO_CNTS   = (0x000418, 0, 32)

# ioctl numbers from uapi/linux/mlx5/fpga_tools.h -- unchanged, so the node this app drives is the
# same one the vendor binary drives.
IOCTL_FPGA_IMAGE_SEL = 0x40046D83      # _IOW('m',0x83,4)  -- image passed BY VALUE
IOCTL_FPGA_QUERY     = 0x80086D84      # _IOR('m',0x84,ptr)
IOCTL_FPGA_CAP       = 0x80086D85      # _IOR('m',0x85,ptr)
IOCTL_FPGA_CONNECT   = 0xC0086D87      # _IOWR('m',0x87,ptr)
IOCTL_FPGA_TEMPERATURE = 0xC0086D86    # _IOWR('m',0x86,ptr)

# THE TEMPERATURE TRAP. fpga_access.c has TWO temperature functions and they are NOT interchangeable:
#   fpga_get_therm()       -> CR 0x8400, the FPGA's own on-die SYSMON. Refused while the User image
#                             runs, like all of CR space.
#   fpga_get_temperature() -> IOCTL_FPGA_TEMPERATURE -> the ConnectX's MTMP register, sensor index
#                             63, which is the FPGA's thermal diode read BY THE CONNECTX. No CR
#                             space involved, so it answers on EVERY image.
# The vendor menu item "Read FPGA thermal status" calls print_temperature(), which uses the SECOND
# one -- and that is why the vendor app shows a temperature on the User image. Using the first one
# here was a real bug: it reported "CR space refused" where the vendor app reports a number.
# The ConnectX names the sensor itself: MTMP.sensor_name at index 63 reads "fpga_0".
MTMP_FPGA_SENSOR = 63          # MLX5_FPGA_SENSOR_DEVMON
MTMP_CX_SENSORS  = (0, 1, 2)   # MAXIMAL_CONNECTX_SENSOR = 2; the app takes the max of these


def boot_verdict(q):
    """Turn (oper, status) into a plain statement, and never invent one for a pair we have not
    actually seen -- an unrecognised combination is reported as unrecognised."""
    key = (q["oper"], q["status"])
    if q["status"] == 3:
        return ("JTAG", "Management path detached (JTAG access granted). Not a boot verdict.")
    return VERDICTS.get(key, ("UNKNOWN",
                              "oper=%d status=%d is a combination this tool has no recorded "
                              "meaning for -- check JTAG BOOT_STATUS/DONE before concluding."
                              % key))


def mtmp_c(raw):
    """MTMP temperature is 1/8 C, two's complement. Verbatim from fpga_get_temperature()."""
    raw &= 0xFFFF
    if (raw & 0xFF00) == 0xFF00:
        return -((0x10000 - raw) // 8)
    return raw // 8


class CrRefused(Exception):
    """The ConnectX refused CR space. Expected while oper_image == USER."""


# ---------------------------------------------------------------- transports
class NodeTransport:
    """/dev/<bdf>_mlx5_fpga_tools -- vendor mlx5_fpga_tools, or our innova2_areg.ko."""

    name = "node"
    can_ctrl_write = True

    def __init__(self, bdf):
        self.path = "/dev/%s_mlx5_fpga_tools" % bdf
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)

    def _provider(self):
        with open("/proc/modules") as f:
            mods = f.read()
        if re.search(r"^innova2_areg ", mods, re.M):
            return "innova2_areg.ko (supplemental module)"
        if re.search(r"^mlx5_fpga_tools ", mods, re.M):
            return "mlx5_fpga_tools (vendor OFED 5.2 driver)"
        return "unknown module"

    def describe(self):
        return "%s via %s" % (self.path, self._provider())

    def query(self):
        # The driver writes three 4-byte enums even though the _IOR size field says 8; the buffer is
        # poisoned so the actual write extent is visible rather than guessed (assuming three BYTES once
        # produced a whole wrong result).
        buf = array.array("B", [0xEE] * 32)
        fd = os.open(self.path, os.O_RDONLY)
        try:
            fcntl.ioctl(fd, IOCTL_FPGA_QUERY, buf, True)
        finally:
            os.close(fd)
        words = struct.unpack_from("<3I", buf, 0)
        if buf[8:12] == array.array("B", [0xEE] * 4):
            raise OSError("driver returned fewer than 3 words -- refusing to name a field it did "
                          "not write")
        return {"admin": words[0], "oper": words[1], "status": words[2]}

    def connect_query(self):
        buf = array.array("i", [0])
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, IOCTL_FPGA_CONNECT, buf, True)
        finally:
            os.close(fd)
        return buf[0]

    def connect_set(self, op):
        buf = array.array("i", [op])
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, IOCTL_FPGA_CONNECT, buf, True)
        finally:
            os.close(fd)
        return buf[0]

    def image_select(self, image):
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, IOCTL_FPGA_IMAGE_SEL, image)   # by value, as the vendor app does
        finally:
            os.close(fd)

    def mtmp(self, index):
        """struct mlx5_fpga_temperature: 8 u32 then char[16]. Field ORDER is the vendor's."""
        buf = array.array("B", struct.pack("<8I16s", 0, index, 0, 0, 0, 0, 0, 0, b""))
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, IOCTL_FPGA_TEMPERATURE, buf, True)
        finally:
            os.close(fd)
        v = struct.unpack("<8I16s", bytes(buf))
        return {"temperature": v[0], "index": v[1], "max_temperature": v[3],
                "name": v[8].split(b"\0")[0].decode("ascii", "replace").strip()}

    def cr_read(self, addr):
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.lseek(fd, addr, os.SEEK_SET)
            b = os.read(fd, 4)
        except OSError as e:
            raise CrRefused(str(e))
        finally:
            os.close(fd)
        if len(b) != 4:
            raise CrRefused("short read (%d bytes)" % len(b))
        return struct.unpack(">I", b)[0]

    def cr_write(self, addr, val):
        fd = os.open(self.path, os.O_RDWR)
        try:
            os.lseek(fd, addr, os.SEEK_SET)
            os.write(fd, struct.pack(">I", val & 0xFFFFFFFF))
        except OSError as e:
            raise CrRefused(str(e))
        finally:
            os.close(fd)


class MlxregTransport:
    """MFT's mlxreg (or mstflint's mstreg) over the PCI Vendor-Specific Capability (ICMD) gateway.

    Needs no kernel module and no register database: the three FPGA registers are sent raw, by ID and length, with
    the field positions of the kernel's include/linux/mlx5/mlx5_ifc_fpga.h (offsets below are byte.bit, bit 0 = the
    least significant bit of the big-endian dword, as mlxreg prints it). Cannot do FPGA_CTRL writes -- see the
    module docstring."""

    name = "mlxreg"
    can_ctrl_write = False
    TOOLS = ("mlxreg", "mstreg")   # MFT's mlxreg, else mstflint's mstreg (open source, same flags)
    # register id, and the length the kernel driver sends. FPGA_ACCESS_REG is always 4 + 64 dwords
    # (MLX5_ST_SZ_DW(fpga_access_reg) + MLX5_FPGA_ACCESS_REG_SIZE_MAX), whatever the payload size.
    REGS = {"FPGA_CAP": (0x4022, 0x100), "FPGA_CTRL": (0x4023, 0x10), "FPGA_ACCESS_REG": (0x4024, 0x110)}

    def __init__(self, bdf):
        self.bdf = bdf
        import shutil
        self.tool = next((t for t in self.TOOLS if shutil.which(t)), None)
        if not self.tool:
            raise FileNotFoundError("neither mlxreg (MFT) nor mstreg (mstflint) is installed")
        self.name = self.tool
        subprocess.run([self.tool, "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True)
        self.query()   # prove it actually reaches the device before claiming to be usable

    def describe(self):
        v = subprocess.run([self.tool, "-v"], capture_output=True, text=True).stdout
        m = re.search(r"mft [\d.]+-?\d*", v)
        return "%s, raw FPGA registers (PCI VSC/ICMD gateway -- no kernel module)" % (m.group(0) if m else "MFT")

    def _run(self, args):
        p = subprocess.run([self.tool, "-d", self.bdf] + args, capture_output=True, text=True)
        out = p.stdout + p.stderr
        if "-E-" in out or "Failed" in out:
            raise OSError(re.sub(r"\s+", " ", out).strip())
        return out

    def _raw(self, reg, args):
        rid, rlen = self.REGS[reg]
        return self._run(["--reg_id", "0x%X" % rid, "--reg_len", "0x%X" % rlen] + args)

    @staticmethod
    def _fields(out):
        """`name | 0x...` lines, as mlxreg prints a register it has a layout for (used for MTMP)."""
        f = {}
        for line in out.splitlines():
            if "|" in line:
                parts = [x.strip() for x in line.split("|")]
                if len(parts) >= 2 and re.fullmatch(r"0x[0-9a-fA-F]+", parts[1]):
                    f[parts[0]] = int(parts[1], 16)
        return f

    @staticmethod
    def _dwords(out):
        """`0x<byte offset> | 0x<dword>` lines, as mlxreg prints a raw register."""
        d = {}
        for m in re.finditer(r"^\s*(0x[0-9a-fA-F]+)\s*\|\s*(0x[0-9a-fA-F]+)\s*$", out, re.M):
            d[int(m.group(1), 16)] = int(m.group(2), 16)
        return d

    def query(self):
        # mlx5_ifc_fpga_ctrl_bits: status = dword 0 bits 7:0, flash_select_admin = dword 1 bits 23:16,
        # flash_select_oper = dword 1 bits 7:0
        d = self._dwords(self._raw("FPGA_CTRL", ["--get"]))
        if 0 not in d or 4 not in d:
            raise OSError("unexpected mlxreg output for FPGA_CTRL")
        return {"admin": (d[4] >> 16) & 0xFF, "oper": d[4] & 0xFF, "status": d[0] & 0xFF}

    def connect_query(self):
        # FPGA_CTRL has no separate connect field: mlx5_fpga_ctrl_connect() derives it from status,
        # and this does the same, so the two transports agree by construction.
        return OP_DISCONNECT if self.query()["status"] == 3 else OP_CONNECT

    def connect_set(self, op):
        raise OSError("the ICMD gateway refuses FPGA_CTRL writes "
                      "(ME_ICMD_OPERATIONAL_ERROR) -- load innova2_areg.ko")

    def image_select(self, image):
        raise OSError("the ICMD gateway refuses FPGA_CTRL writes "
                      "(ME_ICMD_OPERATIONAL_ERROR) -- load innova2_areg.ko")

    def mtmp(self, index):
        # MTMP is in MFT's own register database, so it is addressed by name.
        p = subprocess.run([self.tool, "-d", self.bdf, "--reg_name", "MTMP", "--indexes",
                            "sensor_index=0x%x,slot_index=0x0" % index, "--get"],
                           capture_output=True, text=True)
        out = p.stdout + p.stderr
        if "-E-" in out or "Failed" in out:
            raise OSError(re.sub(r"\s+", " ", out).strip())
        f = self._fields(out)
        name = b""
        for k in ("sensor_name_hi", "sensor_name_lo"):
            if k in f:
                name += struct.pack(">I", f[k])
        return {"temperature": f.get("temperature", 0), "index": index,
                "max_temperature": f.get("max_temperature", 0),
                "name": name.split(b"\0")[0].decode("ascii", "replace")}

    def _cr(self, addr, extra=()):
        # mlx5_ifc_fpga_access_reg_bits: size = byte 4 bits 15:0, address_hi = dword 2, address_lo = dword 3,
        # data from byte 0x10
        return self._raw("FPGA_ACCESS_REG", ["--indexes", "0x4.0:16=0x4,0x8.0:32=0x0,0xc.0:32=0x%X" % addr]
                         + list(extra))

    def cr_read(self, addr):
        try:
            out = self._cr(addr, ["--get"])
        except OSError as e:
            raise CrRefused(str(e))
        d = self._dwords(out)
        if 0x10 not in d:
            raise CrRefused("no data dword in mlxreg output")
        return d[0x10]

    def cr_write(self, addr, val):
        try:
            self._cr(addr, ["--set", "0x10.0:32=0x%X" % (val & 0xFFFFFFFF), "--yes"])
        except OSError as e:
            raise CrRefused(str(e))


# ---------------------------------------------------------------- device layer
def find_cx5_devices():
    """Every ConnectX PF, newest-style BDF first. The vendor app enumerates I2C devices; the same
    set is reachable here as the mlx5_core PCI functions."""
    devs = []
    for path in sorted(glob.glob("/sys/bus/pci/devices/*")):
        try:
            with open(os.path.join(path, "vendor")) as f:
                if f.read().strip() != "0x15b3":
                    continue
            with open(os.path.join(path, "class")) as f:
                if not f.read().strip().startswith("0x0200"):   # network controller
                    continue
        except OSError:
            continue
        devs.append(os.path.basename(path))
    return devs


class Innova2:
    def __init__(self, bdf, want="auto", cross_check=False):
        self.bdf = bdf
        self.t = None
        self.fallback = None
        errs = []
        order = {"auto": [NodeTransport, MlxregTransport],
                 "node": [NodeTransport],
                 "mlxreg": [MlxregTransport]}[want]
        for cls in order:
            try:
                self.t = cls(bdf)
                break
            except Exception as e:                      # noqa: BLE001 -- report, then try the next
                errs.append("%s: %s" % (cls.name, e))
        if self.t is None:
            raise SystemExit("*** no usable transport for %s\n    %s\n"
                             "    node   needs mlx5_fpga_tools (OFED 5.2) or innova2_areg.ko\n"
                             "    mlxreg needs MFT (or mstflint's mstreg)"
                             % (bdf, "\n    ".join(errs)))
        # A second transport, when available, is what makes a write checkable against something
        # other than itself. Opt-in (--cross-check): a normal run never starts an MFT/mstflint tool.
        if cross_check and self.t.name == "node":
            try:
                self.fallback = MlxregTransport(bdf)
            except Exception:                           # noqa: BLE001 -- optional
                self.fallback = None

    # -- field helpers, matching fpga_field_read/write in the vendor app
    def field_read(self, reg):
        addr, off, width = reg
        mask = (1 << width) - 1
        return (self.t.cr_read(addr) >> off) & mask

    def field_write(self, reg, val):
        addr, off, width = reg
        if width == 32:
            self.t.cr_write(addr, val)
            return
        mask = (1 << width) - 1
        old = self.t.cr_read(addr)
        self.t.cr_write(addr, (old & ~(mask << off)) | ((val & mask) << off))

    # -- the capabilities
    def query(self):
        return self.t.query()

    def cross_check(self, what, expect):
        """Confirm a FPGA_CTRL write on the OTHER transport. A write confirmed only by the thing
        that performed it is not confirmed."""
        if not self.fallback:
            return None
        try:
            got = self.fallback.query()
        except Exception as e:                          # noqa: BLE001
            return "    (cross-check unavailable: %s)" % e
        ok = all(got[k] == v for k, v in expect.items())
        # Name each field from ITS OWN table: "status" is a status code, not an image. Falling back
        # from one dict to the other printed "status=3(Innova2 Flex Image)" for DISCONNECTED.
        tables = {"admin": IMAGES, "oper": IMAGES, "status": STATUS}
        return "    cross-check via %s: %s %s" % (
            self.fallback.name,
            " ".join("%s=%d(%s)" % (k, got[k], tables[k].get(got[k], "?")) for k in expect),
            "OK" if ok else "*** DISAGREES with %s" % what)

    def temperature(self):
        """fpga_get_therm(): CR 0x8400, the FPGA's own SYSMON. Flex/Factory only."""
        return ((self.field_read(R_TEMPERATURE) * 508) >> 16) - 279

    def fpga_temp_mtmp(self):
        """fpga_get_temperature(): MTMP sensor 63. Works on every image, User included."""
        t = self.t.mtmp(MTMP_FPGA_SENSOR)
        return mtmp_c(t["temperature"]), t.get("name", "")

    def cx_temp_mtmp(self):
        """fpga_get_max_temperature(): the hottest of the ConnectX's own sensors."""
        best, name = None, ""
        for i in MTMP_CX_SENSORS:
            try:
                t = self.t.mtmp(i)
            except Exception:                           # noqa: BLE001 -- a missing sensor is normal
                continue
            c = mtmp_c(t["temperature"])
            if best is None or c > best:
                best, name = c, t.get("name", "")
        return best, name

    def fan_rpm(self):
        self.field_write(R_TACHO_START, 1)
        print("FAN speed measuring...")
        time.sleep(3)
        self.field_write(R_TACHO_START, 0)
        for _ in range(10):
            if self.field_read(R_TACHO_DONE):
                break
            print("Waiting for test to finish...")
            time.sleep(1)
        else:
            return None
        raw = self.field_read(R_TACHO_CNTS)
        seconds = (raw >> 24) & 0xFF
        pulses = (raw & 0x00FFFFFF) // 2
        if not seconds:
            return None
        return pulses * 60 // seconds

    def bist(self, cyclic=False, lfsr=False, timeout=200, settle=30):
        """The vendor DDR test.

        TWO WAYS THIS LIES, both seen on real cards, both handled here:

        * THE STATUS IS ALREADY 2 BEFORE YOU START. The vendor Flex/Factory image runs the BIST
          itself at power-on (585hq), so `bist_status` reads 2 = success on a card nobody has
          touched. The vendor app's loop is `while status == 1`, which on such a card exits
          IMMEDIATELY and prints the POWER-ON run's verdict as if it were the test you just asked
          for -- measured here as 'success after 0.0 s'. So this waits for the engine to actually
          enter 'in progress' before it will believe any final status, and says plainly when it
          never did.
        * A CARD WHOSE DDR NEVER CALIBRATES REPORTS NOTHING, NOT FAILURE (585hp). status stays 0
          for as long as you care to wait; 3 is effectively unreachable. Hence the timeout.

        Returns (status, seconds, started) -- `started` False means the status is NOT this run's.
        """
        entry = self.field_read(R_BIST_STATUS)
        self.field_write(R_BIST_TYPE, 0)
        self.field_write(R_STOP_ON_FAIL, 1)
        if cyclic:
            self.field_write(R_CYCLIC_EN, 1)
            self.field_write(R_LFSR_ADDR_EN, 1 if lfsr else 0)
            self.field_write(R_CYCLIC_BIST, 1)
        else:
            self.field_write(R_START_BIST, 1)
        t0 = time.time()

        # Phase 1: wait for evidence the engine actually started.
        started = False
        while time.time() - t0 < settle:
            if self.field_read(R_BIST_STATUS) == 1:
                started = True
                break
            time.sleep(0.5)
        if not started:
            return entry, time.time() - t0, False

        # Phase 2: now a final status means this run.
        print("Test is running...")
        while time.time() - t0 < timeout:
            st = self.field_read(R_BIST_STATUS)
            if st != 1:
                return st, time.time() - t0, True
            time.sleep(1)
        return self.field_read(R_BIST_STATUS), time.time() - t0, True


# ---------------------------------------------------------------- presentation
# ================================================================ vendor-faithful UI (2026-09-27)
# Everything below reproduces innova2_flex_app 18.07.00 (Innova_2_Flex_Open_18_12/app/interactive.c and
# burn_app.c): the menus, their texts and numbering, menu_get_answer(), confirm(), the start-up banner,
# the ConnectX chooser, the log file, --query, --autorun and the command-line options. Deliberate,
# documented differences are marked [ext] (extension) or [map] (mapping for our cards):
#   [map] our ConnectX reports oper=1 while the FLEX slot runs (measured; never 3). The vendor would
#         then show the Factory menu, whose offset check only allows 0x03000000 -- User burning would be
#         unreachable. When oper=1, status=SUCCESS and the BOPE endpoint (15b3:0264) is present, this
#         app treats the card as FLEX-running and shows the vendor's Burn-Diagnostics menu.
#   [ext] after a BOPE burn, a read-back check through rawspi.py (sampled; one 'Verify' line).
#   [ext] --batch commands, and 'burn-inshell' (stock xbflash through a running shell whose flash
#         controller reaches BOTH chips -- the vendor refuses to burn in user mode, so it is CLI only).
#   [ext] 'Reload User image' asks the vendor's confirmation and then declines: on these hosts an FPGA
#         reconfiguration while the host runs is reboot-class (measured). Use a cold cycle.
APP_NAME, APP_VER = "innova2_flex_app", "18.07.00"
DFL_LOGNAME = "/var/log/innova2_flex_app.log"
SYNC_WORD, DDR_OFFSET = 0xC001BABE, 0x60000000
CLEAR_PATTERN_WORD, SET_PATTERN_WORD = 0x01234567, 0xA5A5A5A5
DFL_DEFAULT_OFFSET = 0xFFFFFFFF
DFL_FLASH_FLEX_OFFSET, DFL_FLASH_OFFSET = 0x03000000, 0x01000000
MAXIMAL_SIZE_OF_FLASH, MAXIMAL_SIZE_OF_DDR = 0x02000000, 128 * 1024
FLASH_PAGE_SZ, MAX_FLASH_OFFSET = 0x100, 0x04000000
RED, NRM = "\033[0;31m", "\033[0m"
IMG_STR = {0: "User Image", 1: "Innova2 Factory Image", 2: "Innova2 Image failover", 3: "Innova2 Flex Image"}
IMG_SCHED_STR = {0: "User Image", 1: "Innova2 Flex Image"}


class Ctx:
    verbose = 0
    logfile = None
    no_logger = False
    autorun = 0
    has_autorun = False
    mlx_force = False
    flex_image = False
    developer_mode = False
    maximal_size_of_mem = MAXIMAL_SIZE_OF_DDR
    b_files = []          # list of dicts: fname, real_length, aligned_length, flash, flash_offset
    bope = None           # Bope instance or None
    bope_bdf = None
    dev = None            # Innova2
    state = None


C = Ctx()


def _log(printtime, s):
    if C.no_logger or C.logfile is None:
        return
    if printtime:
        t = time.time()
        C.logfile.write("%s:%03d - " % (time.strftime("%H:%M:%S", time.localtime(t)), int((t % 1) * 1000)))
    C.logfile.write(s)
    C.logfile.flush()


def ofprintf(s):
    _log(1, s)


def offprintf(s):
    _log(0, s)


def oprintf(level, s):
    if level <= C.verbose:
        sys.stdout.write(s)
        sys.stdout.flush()
    _log(1, s)


def menu_get_answer(menu):
    """interactive.c menu_get_answer(): separator, '[%2d ] prompt', 'Your choice: ', re-prompt on junk."""
    while True:
        print("------------------")
        for val, prompt in menu:
            if val != -1:
                print("[%2d ] %s" % (val, prompt))
        sys.stdout.write("Your choice: ")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:                     # EOF: behave like Exit rather than spin
            print()
            return 99
        try:
            answer = int(line.strip())
        except ValueError:
            continue
        for val, prompt in menu:
            if answer == val:
                print()
                if answer != 99:
                    ofprintf("Chosed menu is [%2d ] %s\n" % (val, prompt))
                return answer


def confirm(prompt):
    """burn_app.c confirm(): ' <prompt> [y/n] ' until y/Y or n/N."""
    if C.autorun:
        return True
    while True:
        sys.stdout.write(" %s [y/n] " % prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return False
        s = line.strip()
        if s[:1] in ("y", "Y"):
            return True
        if s[:1] in ("n", "N"):
            return False


# ---------------------------------------------------------------- BOPE (15b3:0264) access
def _parent(bdf):
    return os.path.basename(os.path.dirname(os.path.realpath("/sys/bus/pci/devices/%s" % bdf)))


def find_bope_for(cx5_bdf):
    """The burn endpoint shares the ConnectX's upstream switch (grandparent) -- pick that one."""
    gp = _parent(_parent(cx5_bdf))
    for d in sorted(glob.glob("/sys/bus/pci/devices/*")):
        try:
            if open(d + "/vendor").read().strip() == "0x15b3" and open(d + "/device").read().strip() == "0x0264":
                b = os.path.basename(d)
                if _parent(_parent(b)) == gp:
                    return b
        except OSError:
            pass
    return None


class Bope:
    """One 32-bit register at BAR0+0: writes push to the mailbox FIFO, reads return the status word.
    ctypes stores only -- python mmap slice stores are issued TWICE (memory: python-mmap-double-store)."""
    def __init__(self, bdf):
        import ctypes, mmap
        self._ct = ctypes
        p = "/sys/bus/pci/devices/%s" % bdf
        try:
            open(p + "/enable", "w").write("1")
        except OSError:
            pass
        # PCI COMMAND (config offset 4): make sure Memory Space Enable (bit 1) is set. Through sysfs
        # rather than setpci, so the tool needs no pciutils.
        try:
            with open(p + "/config", "r+b", buffering=0) as cfg:
                cfg.seek(4); cmd = int.from_bytes(cfg.read(2), "little")
                if not cmd & 0x2:
                    cfg.seek(4); cfg.write((cmd | 0x2).to_bytes(2, "little"))
        except OSError:
            pass
        self.fd = os.open(p + "/resource0", os.O_RDWR | os.O_SYNC)
        self.map = mmap.mmap(self.fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0)
        self.base = ctypes.addressof(ctypes.c_char.from_buffer(self.map))

    def wr(self, v):
        if C.developer_mode:
            return
        self._ct.c_uint32.from_address(self.base).value = v & 0xFFFFFFFF

    def rd(self):
        return self._ct.c_uint32.from_address(self.base).value


def st_fields(s):
    return dict(version=s & 0xFFFF, progress=(s >> 16) & 0x7F, done=(s >> 23) & 3,
                fatal=(s >> 25) & 1, busy=(s >> 26) & 1, pcie_test=(s >> 27) & 1,
                recov=(s >> 28) & 1, afull=(s >> 29) & 1)


def open_bope():
    if C.bope_bdf and C.bope is None:
        try:
            C.bope = Bope(C.bope_bdf)
        except OSError as e:
            oprintf(0, "Error opening bope_dev device %s: %s\n" % (C.bope_bdf, e))
            sys.exit(1)
        print_fpga_status()


def close_bope():
    C.bope = None


def print_fpga_status():
    st = st_fields(C.bope.rd())
    if st["busy"]:
        oprintf(0, "\t***   FPGA is BUSY  ***\n")
    else:
        oprintf(0, "\t***   FPGA image version: %#04x   ***\n" % st["version"])
        oprintf(0, "\t***   Mailbox Done counter   %d   ***\n" % st["done"])


def fpga_not_busy():
    if C.bope is None:
        return
    if st_fields(C.bope.rd())["busy"]:
        oprintf(0, "\t************************************\n")
        oprintf(0, "\t*    FPGA is in BUSY state.        *\n")
        oprintf(0, "\t*    Exiting....                   *\n")
        oprintf(0, "\t************************************\n")
        sys.exit(1)


def pci_test():
    ofprintf("pci_test\n")
    if C.developer_mode:
        return 0
    if C.bope is None:
        oprintf(0, "\n\tThis test requires installing the factory driver.\n"
                   "\tPlease install the driver and try again\n\n")
        return -1
    for val, want in ((CLEAR_PATTERN_WORD, 0), (SET_PATTERN_WORD, 1), (CLEAR_PATTERN_WORD, 0)):
        C.bope.wr(val)
        if st_fields(C.bope.rd())["pcie_test"] != want:
            return -1
    return 0


# ---------------------------------------------------------------- burning (burn_app.c)
def effective_oper(q):
    """[map] oper=1 + SUCCESS + BOPE endpoint present == the Flex slot is running (see header)."""
    if q["oper"] == IMG_FACTORY and q["status"] == 0 and C.bope_bdf:
        return IMG_FLEX
    return q["oper"]


def translate_b_option(s):
    """'<file name>[,<0|1>[,<offset>]]'"""
    parts = s.split(",")
    fname = parts[0]
    if not fname:
        oprintf(0, 'Invalid burn option "%s"\n' % s)
        return None
    if not os.path.exists(fname):
        oprintf(0, 'Can\'t access file "%s"\n' % fname)
        return None
    if not os.path.isfile(fname):
        oprintf(0, 'File "%s" is not regular\n' % fname)
        return None
    if not os.access(fname, os.R_OK):
        oprintf(0, 'No read permission for file "%s"\n' % fname)
        return None
    size = os.path.getsize(fname)
    b = dict(fname=fname, real_length=size, aligned_length=(size + 3) & ~3, flash=0,
             flash_offset=DFL_DEFAULT_OFFSET)
    if len(parts) > 1 and parts[1] != "":
        b["flash"] = int(parts[1])
        if b["flash"] not in (0, 1):
            oprintf(0, "Error: selected flash %d is out of valid range\n" % b["flash"])
            return None
    if len(parts) > 2 and parts[2] != "":
        b["flash_offset"] = int(parts[2], 16)
        if b["flash_offset"] & 0x80000000:
            oprintf(0, "Flash offset %#x is invalid\n" % b["flash_offset"])
            return None
    return b


def set_burn_default_args(b):
    if b["flash_offset"] != DFL_DEFAULT_OFFSET:
        return
    oper = effective_oper(C.state)
    if oper == IMG_FACTORY:
        b["flash_offset"] = DFL_FLASH_FLEX_OFFSET
    elif oper == IMG_FLEX:
        b["flash_offset"] = DFL_FLASH_FLEX_OFFSET if C.flex_image else DFL_FLASH_OFFSET
    oprintf(2, " Set default flash offset to %#x\n" % b["flash_offset"])


def flash_offset_validate(b):
    offset, image_sz = b["flash_offset"], b["aligned_length"]
    image_end = offset + image_sz
    message, confirmation = None, None
    oper = effective_oper(C.state)
    if oper == IMG_FACTORY:
        if offset != DFL_FLASH_FLEX_OFFSET and not C.mlx_force and offset != 0:
            message = "%sError%s: You can burn in offset %#x only\n" % (RED, NRM, DFL_FLASH_FLEX_OFFSET)
        elif offset > MAX_FLASH_OFFSET:
            message = "%sError%s: Flash offset %#x is not in the valid range" % (RED, NRM, offset)
        elif image_sz > MAX_FLASH_OFFSET or image_end > MAX_FLASH_OFFSET:
            message = ("%sError%s: image length %d(%#010x) plus offset %d(%#010x)\n\toverrides flash size"
                       % (RED, NRM, image_sz, image_sz, offset, offset))
    elif oper == IMG_FLEX:
        if image_sz > MAXIMAL_SIZE_OF_FLASH:
            message = ("%sError%s: image length %d(%#010x)\n\toverrides maximal flash size %d(%#010x)"
                       % (RED, NRM, image_sz, image_sz, MAXIMAL_SIZE_OF_FLASH, MAXIMAL_SIZE_OF_FLASH))
        elif not C.flex_image and not C.mlx_force and offset != DFL_FLASH_OFFSET:
            message = "%sError%s: You can burn in offset %#x only\n" % (RED, NRM, DFL_FLASH_OFFSET)
        elif offset > MAX_FLASH_OFFSET:
            message = "%sError%s: Flash offset %#x is not in the valid range" % (RED, NRM, offset)
        elif image_end > MAX_FLASH_OFFSET:
            message = ("%sError%s: image length %d(%#010x) plus offset %d(%#010x)\n\toverrides flash size"
                       % (RED, NRM, image_sz, image_sz, offset, offset))
        elif offset >= DFL_FLASH_FLEX_OFFSET:
            message = ("Warning: You are deleting the Innova2_Flex Image.\n"
                       "\tThis will disable the use of the burn tool in the future.\n"
                       "\tThis process is irreversible!")
            confirmation = "Are you  sure you want to delete the Innova2_Flex Image?"
        elif image_end > DFL_FLASH_FLEX_OFFSET:
            message = "%sError%s: user image going to rewrite Flex image" % (RED, NRM)
        elif offset > DFL_FLASH_OFFSET:
            message = ("Warning: You are writing the image to an offset %#x\n"
                       "\twhich is not pointed by the Innova2_Flex Image!\n"
                       "\tInnova2_Flex will not jump to this image." % offset)
            confirmation = "Are you sure you want to burn to this offset?"
        elif offset == 0:
            message = ("Warning: You are deleting the Innova2_Factory Image.\n"
                       "\tThis will disable the use of the burn tool in the future.\n"
                       "\tThis process is irreversible!")
            confirmation = "Are you  sure you want to delete the Innova2_Factory Image?"
    else:
        message = "%sError%s: Cannot write images in user mode\n" % (RED, NRM)
    if message is None and offset & (FLASH_PAGE_SZ - 1):
        message = ("%sError%s: You are trying to write to a flash offset which isn't in granularity "
                   "of flash pages %#x." % (RED, NRM, FLASH_PAGE_SZ))
    if message is None:
        return 0
    oprintf(0, "\n\t****************************************\n")
    oprintf(0, "\t\t%s\n" % b["fname"])
    oprintf(0, "\t%s" % message)
    oprintf(0, "\n\t****************************************\n")
    if confirmation:
        return 0 if confirm(confirmation) else -1
    return -1


def proc_recoverable_err(next_counter):
    time.sleep(2)
    if st_fields(C.bope.rd())["done"] != next_counter:
        oprintf(0, "Cannot recover from this error. Exiting...\n")
        sys.exit(-1)
    oprintf(0, "Error was recoverable. Try again\n")


def burning_file(b, pass_):
    """burn_app.c burning_file(): chunks of maximal_size_of_mem; in pass 0 the FIRST chunk is written
    as 0xFFFFFFFF (so a half-written image can never boot) and pass 1 writes only that chunk."""
    fan = "|/-\\"
    try:
        data = open(b["fname"], "rb").read()
    except OSError as e:
        oprintf(0, "Error while opening flash image file %s\n" % e)
        sys.exit(-1)
    data += b"\x00" * (b["aligned_length"] - len(data))
    oprintf(1, "Flash %d bytes (file %s) to DRAM...\n" % (b["real_length"], b["fname"]))
    img_size, max_size = b["aligned_length"], C.maximal_size_of_mem or b["aligned_length"]
    i = 0
    for idx in range(0, img_size, max_size):
        part = data[idx:idx + max_size]
        st = st_fields(C.bope.rd())
        want = (st["done"] + 1) & 3
        part_off = b["flash_offset"] + idx
        if b["flash"] != 0:
            part_off |= 0x80000000
        for w in (SYNC_WORD, DDR_OFFSET, part_off, len(part)):
            C.bope.wr(w)
        blank = (pass_ == 0 and idx == 0)
        for k in range(0, len(part), 4):
            guard = 0
            while st_fields(C.bope.rd())["afull"]:
                time.sleep(0.001)
                guard += 1
                if guard > 20000:
                    oprintf(0, "Non-recoverable error during FPGA burning!\n")
                    oprintf(0, "Try burning FPGA image using JTAG cable.  Exiting...\n")
                    sys.exit(-1)
            C.bope.wr(0xFFFFFFFF if blank else struct.unpack("<I", part[k:k + 4])[0])
        st = st_fields(C.bope.rd())
        j = 0
        while st["progress"] > 50 and j < 100:
            time.sleep(0.001)
            j += 1
            st = st_fields(C.bope.rd())
        while not st["progress"] and st["done"] != want:
            time.sleep(0.1)
            if pass_ == 0:
                sys.stdout.write("\rErasing flash (%2u%%) %c       " % ((100 * idx) // img_size, fan[i & 3]))
                i -= 1
            sys.stdout.flush()
            st = st_fields(C.bope.rd())
            if st["fatal"]:
                oprintf(0, "Non-recoverable error during FPGA erasing!\n")
                oprintf(0, "Try burning FPGA image using JTAG cable.  Exiting...\n")
                sys.exit(-1)
            if st["recov"]:
                oprintf(0, "Error during erasing!\n")
                proc_recoverable_err(want)
                return
        if pass_ == 0:
            sys.stdout.write("\rFlash burning ( 0%%) |          ")
        while st["done"] != want:
            time.sleep(0.1)
            st = st_fields(C.bope.rd())
            if pass_ == 0:
                sys.stdout.write("\rFlash burning (%2u%%) %c   " % ((100 * idx) // img_size, fan[i & 3]))
                i -= 1
            sys.stdout.flush()
            if st["fatal"]:
                oprintf(0, "Non-recoverable error during FPGA burning!\n")
                oprintf(0, "Try burning FPGA image using JTAG cable.  Exiting...\n")
                sys.exit(-1)
            if st["recov"]:
                oprintf(0, "Error during burning!\n")
                proc_recoverable_err(want)
                return
        if pass_ == 1:
            ofprintf("Second pass flash burned (100%)\n")
            break
    if pass_ != 1:
        sys.stdout.write("\rFlash burned  (100%) |       \n")
        ofprintf("First pass flash burned (100%)\n")


def verify_burn(b):
    """[ext] Sampled read-back through rawspi.py: first 4 KB, last 4 KB and 14 spread 4 KB blocks."""
    rawspi = os.path.join(HERE, "rawspi.py")
    if not os.path.exists(rawspi) or C.developer_mode:
        oprintf(0, "\t*** Verify: skipped (rawspi.py not found next to this app)\n")
        return
    data = open(b["fname"], "rb").read()
    n = len(data)
    offs = sorted(set([0, max(0, (n - 4096) & ~0xFFF)] + [((n * k) // 15) & ~0xFFF for k in range(1, 15)]))
    good = 0
    for off in offs:
        want = data[off:off + 4096]
        out = os.path.join(HERE, ".innova2_verify.bin")      # next to the app: persistent, not /tmp
        r = subprocess.run([sys.executable, rawspi, "dump", str(b["flash"]), "%x" % (b["flash_offset"] + off),
                            "%x" % len(want), out], env=dict(os.environ, BDF=C.bope_bdf),
                           capture_output=True, text=True)
        try:
            got = open(out, "rb").read()[:len(want)]
            os.remove(out)
        except OSError:
            got = b""
        good += (r.returncode == 0 and got == want)
    oprintf(0, "\t*** Verify: %d/%d sampled blocks match %s (flash %d @ %#x)\n"
            % (good, len(offs), os.path.basename(b["fname"]), b["flash"], b["flash_offset"]))
    return good == len(offs)


def burning_all():
    if not C.b_files:
        oprintf(0, "Files for burning are not defined. Nothing to burn\n")
    if C.bope is None and not C.developer_mode:
        oprintf(0, "\n\tYou are trying to burn an image using the Innova2_Flex image.\n"
                   "\tBurning requires installing the factory driver.\n"
                   "\tPlease install the driver and try again\n")
        return
    if not C.b_files:
        return
    fpga_not_busy()
    for b in C.b_files:
        set_burn_default_args(b)
    for b in C.b_files:
        if flash_offset_validate(b):
            sys.exit(1)
    for pass_ in (0, 1):
        for flash in (0, 1):
            for b in C.b_files:
                if b["flash"] == flash:
                    burning_file(b, pass_)
    for b in C.b_files:
        verify_burn(b)


# ---------------------------------------------------------------- menu actions
def print_scheduled_image():
    C.state = C.dev.query()
    oprintf(0, "Scheduled image:  %s\n" % IMG_SCHED_STR.get(C.state["admin"], "?"))


def set_fpga_image(img):
    if not C.dev.t.can_ctrl_write:
        oprintf(0, "Image select needs the innova2_areg module (FPGA_CTRL write).\n")
        return
    C.dev.t.image_select(img)


def print_temperature():
    try:
        c, _ = C.dev.fpga_temp_mtmp()
        oprintf(0, "\t*** FPGA Temperature: %d C\n" % c)
    except Exception as e:                              # noqa: BLE001
        oprintf(0, "\t*** FPGA Temperature: unavailable (%s)\n" % e)
    try:
        c, _ = C.dev.cx_temp_mtmp()
        oprintf(0, "\t*** ConnectX Temperature: %d C\n" % c)
    except Exception as e:                              # noqa: BLE001
        oprintf(0, "\t*** ConnectX Temperature: unavailable (%s)\n" % e)


def print_version():
    ver = C.dev.field_read(R_IMAGE_VER)
    oprintf(0, "\t*** FPGA image version:   %#-8x\n" % ver)
    d = C.dev.field_read(R_IMAGE_DATE)
    oprintf(0, "\t*** Image creation date:  %02x/%02x/%04x\n" % ((d >> 24) & 0xFF, (d >> 16) & 0xFF, d & 0xFFFF))
    t = C.dev.field_read(R_IMAGE_TIME)
    oprintf(0, "\t*** Image creation time:  %02x:%02x:%02x\n" % ((t >> 16) & 0xFF, (t >> 8) & 0xFF, t & 0xFF))


def set_connect(op):
    if not C.dev.t.can_ctrl_write:
        oprintf(0, "JTAG access needs the innova2_areg module (FPGA_CTRL write).\n")
        return
    C.dev.t.connect_set(op)
    C.state = C.dev.query()


def ddr_single_test():
    ofprintf(" fpga_ddr_single_test\n")
    C.dev.field_write(R_CYCLIC_EN, 0)
    C.dev.field_write(R_START_BIST, 1)
    oprintf(0, "Test is running...\n")
    t0 = time.time()
    while C.dev.field_read(R_BIST_STATUS) == 1 and time.time() - t0 < 300:
        print("Waiting for test to finish...")
        time.sleep(1)
    C.dev.field_write(R_START_BIST, 0)
    oprintf(0, 'Test finished with status "%s"\n' % BIST_STATUS.get(C.dev.field_read(R_BIST_STATUS), "Undefined"))


def ddr_cyclic_test(lfsr):
    ofprintf(" fpga_ddr_cyclic_test\n")
    C.dev.field_write(R_LFSR_ADDR_EN, 1 if lfsr else 0)
    C.dev.field_write(R_CYCLIC_EN, 1)
    C.dev.field_write(R_START_BIST, 1)
    oprintf(0, "Test is running. Press Enter to interrupt\n")
    import select
    while C.dev.field_read(R_BIST_STATUS) == 1:
        r, _, _ = select.select([sys.stdin], [], [], 1.0)
        if r:
            sys.stdin.readline()
            break
    C.dev.field_write(R_START_BIST, 0)
    C.dev.field_write(R_CYCLIC_EN, 0)
    oprintf(0, 'Test finished with status "%s"\n' % BIST_STATUS.get(C.dev.field_read(R_BIST_STATUS), "Undefined"))


def menu_ddr_stress_test(menuitem):
    menu = [(1, "Cyclic test LFSR address"), (2, "Cyclic test incremental address"), (3, "Single test"),
            (99, "Back"), (-1, None)]
    choice = 0
    while choice != 99:
        print("DDR stress test")
        choice = 3 if menuitem else menu_get_answer(menu)
        if choice in (1, 2):
            ddr_cyclic_test(choice == 1)
        elif choice == 3:
            ddr_single_test()
        elif choice != 99:
            print("Choice %d is not supported yet" % choice)
        if menuitem:
            break


def menu_power_test():
    menu = [(i, "Set FPGA Power level %d%s" % (i, "" if i else " - only base power without increase"))
            for i in range(11)] + [(99, "Exit"), (-1, None)]
    power = C.dev.field_read(R_POWER)
    level = next((i for i in range(11) if power == (1 << i) - 1), 11)
    choice = 0
    while choice != 99:
        oprintf(0, "\nIncrease FPGA Power (current power level %u):\n" % level)
        choice = menu_get_answer(menu)
        if 0 <= choice < 11:
            C.dev.field_write(R_POWER, (1 << choice) - 1)
            sys.stdout.write("Wait 5 seconds ")
            sys.stdout.flush()
            for _ in range(5):
                sys.stdout.write(".")
                sys.stdout.flush()
                time.sleep(1)
            print()
            print_temperature()
            break


def reload_fpga_image_with_check():
    if not confirm("%sReload feature may hang up your station if PCI is not disabled!!!%s\n"
                   "  Do you want to run this feature? " % (RED, NRM)):
        return
    oprintf(0, "Reload is disabled in this build: on these hosts an FPGA reconfiguration while the\n"
               "host runs is reboot-class. Select the image and COLD cycle instead.\n")


def _cr(fn):
    try:
        fn()
    except CrRefused as e:
        oprintf(0, "\t*** CR space refused: %s\n" % e)


def menu_jump_disconnected(menuitem):
    menu = [(1, "Disable JTAG Access - enable thermal status"), (99, "Exit"), (-1, None)]
    choice = 0
    while choice != 99:
        print("\nDisable JTAG Access menu")
        choice = menuitem if menuitem else menu_get_answer(menu)
        if choice == 1:
            before = C.state["status"]
            set_connect(OP_CONNECT)
            if menuitem:
                break
            if C.state["status"] != before:
                user_interaction(0)
                return
        elif choice != 99:
            print("Choice %d is not supported yet" % choice)
        if menuitem:
            break


def _jtag_enable(menuitem):
    before = C.state["status"]
    set_connect(OP_DISCONNECT)
    if not menuitem and C.state["status"] != before:
        menu_jump_disconnected(0)
        return True
    return False


def menu_fi_is_active(menuitem):
    menu = [(1, "Query Innova2_Flex FPGA version"), (2, "DDR stress test"), (3, "PCI test"),
            (4, "Read FPGA thermal status"), (5, "Read Fan speed"), (6, "Burn of customer User image"),
            (7, "Set User image active (reboot required)"), (8, "Set Innova2_Flex image active"),
            (9, "Increase FPGA power consumption"), (10, "Enable JTAG Access - no thermal status"),
            (99, "Exit"), (-1, None)]
    open_bope()
    choice = 0
    while choice != 99:
        print("\nBurn-Diagnostics menu")
        choice = menuitem if menuitem else menu_get_answer(menu)
        if choice == 1:
            _cr(print_version)
        elif choice == 2:
            _cr(lambda: menu_ddr_stress_test(menuitem))
        elif choice == 3:
            oprintf(0, "\t*** PCI test: %s\n" % ("failed" if pci_test() else "passed"))
        elif choice == 4:
            print_temperature()
        elif choice == 5:
            def fan():
                rpm = C.dev.fan_rpm()
                oprintf(0, "\t*** Fan speed: %d rpm\n" % (rpm or 0))
            _cr(fan)
        elif choice == 6:
            burning_all()
        elif choice == 7:
            set_fpga_image(IMG_USER)
            print_scheduled_image()
        elif choice == 8:
            set_fpga_image(IMG_FACTORY)
            print_scheduled_image()
        elif choice == 9:
            _cr(menu_power_test)
        elif choice == 10:
            if _jtag_enable(menuitem):
                return
        elif choice != 99:
            print("Choice %d is not supported yet" % choice)
        if menuitem:
            break
    close_bope()


def menu_jump_to_factory(menuitem):
    menu = [(1, "Set Innova2_Flex image active (reboot required)"), (2, "Set User image active"),
            (3, "Enable JTAG Access - no thermal status"), (4, "Read FPGA thermal status"),
            (5, "Reload User image"), (99, "Exit"), (-1, None)]
    choice = 0
    while choice != 99:
        print("\nJump-to-Innova2-User menu")
        choice = menuitem if menuitem else menu_get_answer(menu)
        if choice == 1:
            set_fpga_image(IMG_FACTORY)
            print_scheduled_image()
        elif choice == 2:
            set_fpga_image(IMG_USER)
            print_scheduled_image()
        elif choice == 3:
            if _jtag_enable(menuitem):
                return
        elif choice == 4:
            print_temperature()
        elif choice == 5:
            reload_fpga_image_with_check()
        elif choice != 99:
            print("Choice %d is not supported yet" % choice)
        if menuitem:
            break


def menu_jump_to_factory_failover(menuitem):
    menu = [(1, "Set Innova2_Flex image active (reboot required)"), (2, "Set User image active"),
            (3, "Enable JTAG Access - no thermal status"), (99, "Exit"), (-1, None)]
    choice = 0
    while choice != 99:
        print("\nJump-to-Innova2-User menu")
        choice = menuitem if menuitem else menu_get_answer(menu)
        if choice == 1:
            set_fpga_image(IMG_FACTORY)
            print_scheduled_image()
        elif choice == 2:
            set_fpga_image(IMG_USER)
            print_scheduled_image()
        elif choice == 3:
            if _jtag_enable(0):
                return
        elif choice != 99:
            print("Choice %d is not supported yet" % choice)
        if menuitem:
            break


def menu_factory_is_active(menuitem):
    menu = [(1, "Burn of Flex image"), (2, "Enable JTAG Access - no thermal status"), (99, "Exit"), (-1, None)]
    open_bope()
    choice = 0
    while choice != 99:
        print("\nJump-to-Innova2-Factory menu")
        choice = menuitem if menuitem else menu_get_answer(menu)
        if choice == 1:
            burning_all()
        elif choice == 2:
            if _jtag_enable(menuitem):
                return
        elif choice != 99:
            print("Choice %d is not supported yet" % choice)
        if menuitem:
            break
    close_bope()


def user_interaction(menuitem):
    C.state = C.dev.query()
    oper = effective_oper(C.state)
    if C.state["status"] == 3:
        menu_jump_disconnected(menuitem)
    elif oper == IMG_FACTORY:
        menu_factory_is_active(menuitem)
    elif oper == IMG_FLEX and C.state["status"] == 0:
        menu_fi_is_active(menuitem)
    elif C.state["status"] == 0:
        menu_jump_to_factory(menuitem)
    else:
        menu_jump_to_factory_failover(menuitem)


def nvconf_interaction():
    menu = [(1, "Switch to Innova2_Flex Image"), (2, "Switch to User Image"), (99, "Exit"), (-1, None)]
    choice = 0
    while choice != 99:
        print("\nPrivate debuging menu")
        choice = menu_get_answer(menu)
        if choice == 1:
            set_fpga_image(IMG_FACTORY)
            print_scheduled_image()
        elif choice == 2:
            set_fpga_image(IMG_USER)
            print_scheduled_image()
        elif choice != 99:
            print("Choice %d is not supported yet" % choice)


# ---------------------------------------------------------------- [ext] in-shell User burn (CLI only)
def mcs_range(path):
    """Lowest and highest data address in an Intel-HEX MCS (types 00 data, 04 extended linear address)."""
    ela, lo, hi = 0, None, None
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln.startswith(":") or len(ln) < 11:
                continue
            n, addr, typ = int(ln[1:3], 16), int(ln[3:7], 16), int(ln[7:9], 16)
            if typ == 4:
                ela = int(ln[9:13], 16) << 16
            elif typ == 0 and n:
                a0 = ela + addr
                lo = a0 if lo is None else min(lo, a0)
                hi = a0 + n - 1 if hi is None else max(hi, a0 + n - 1)
    if lo is None:
        raise SystemExit("*** %s has no data records" % path)
    return lo, hi


def burn_inshell(cx5_bdf, prefix):
    """Stock xbflash.qspi through the flash controller of a RUNNING User image (management PF, BAR0 + 0x40000).
    Refuses unless both flash chips answer RDID: a controller that reaches only chip 0 would write half an image."""
    P, S = prefix + "_primary.mcs", prefix + "_secondary.mcs"
    for f in (P, S):
        if not os.path.isfile(f):
            raise SystemExit("*** missing %s" % f)
    for f in (P, S):
        lo, hi = mcs_range(f)
        if lo != 0x00FFF000 or hi + 0x1000 > 0x02FFFFFF:
            raise SystemExit("*** %s spans %#010x..%#010x: not a guard-aware User-slot MCS (must start at "
                             "0x00FFF000 and stay inside the User slot) -- refusing" % (f, lo, hi))
        print("  %s: %#010x..%#010x -> payload %#010x..%#010x (User slot)"
              % (os.path.basename(f), lo, hi, lo + 0x1000, hi + 0x1000))
    xil = [os.path.basename(d) for d in glob.glob("/sys/bus/pci/devices/*")
           if open(d + "/vendor").read().strip() == "0x10ee" and os.path.basename(d).endswith(".0")
           and _parent(_parent(os.path.basename(d))) == _parent(_parent(cx5_bdf))]
    if not xil:
        raise SystemExit("*** no running Xilinx shell on this card -- use the Flex burn (menu 6) instead")
    card = xil[0]
    xb = "/opt/xilinx/xrt/bin/xbflash.qspi"
    # both chips must answer the JEDEC ID before a striped image is written (rawspi, read-only)
    r = subprocess.run([sys.executable, os.path.join(HERE, "rawspi.py"), "rdid"],
                       env=dict(os.environ, BDF=card), capture_output=True, text=True)
    ids = re.findall(r"slave(\d) RDID: 20bb20", r.stdout)
    print("  RDID: chips answering %s" % sorted(set(ids)))
    if sorted(set(ids)) != ["0", "1"]:
        raise SystemExit("*** BOTH flash chips must answer before writing a striped image -- REFUSING")
    if not confirm("Burn %s into the User slot through the running shell (%s)?" % (os.path.basename(prefix), card)):
        return 1
    r = subprocess.run([xb, "--primary", P, "--secondary", S, "--card", card, "--bar", "0",
                        "--bar-offset", "0x40000", "--force"], env=dict(os.environ, FLASH_VIA_USER="1"))
    print("xbflash rc=%d -- select the User image and COLD cycle to boot it" % r.returncode)
    return r.returncode


# ---------------------------------------------------------------- [ext] batch mode (unchanged set + burn)
def batch(dev, args, cx5_bdf):
    cmd = args[0]
    C.state = dev.query()
    if cmd == "query":
        q = C.state
        print("admin=%d(%s) oper=%d(%s) status=%d(%s)"
              % (q["admin"], IMAGES.get(q["admin"], "?"), q["oper"], IMAGES.get(q["oper"], "?"),
                 q["status"], STATUS.get(q["status"], "?")))
    elif cmd == "health":
        q = C.state
        tag, why = boot_verdict(q)
        print("admin=%d oper=%d status=%d -> %s: %s" % (q["admin"], q["oper"], q["status"], tag, why))
        n = sum(1 for v in glob.glob("/sys/bus/pci/devices/*/vendor") if open(v).read().strip() == "0x10ee")
        print("Xilinx PCIe functions present: %d%s" % (n, "   (none is EXPECTED while a Mellanox "
              "image runs; it is a RED FLAG only when oper=0)" if n == 0 else ""))
        print("BOPE burn endpoint: %s" % (C.bope_bdf or "none"))
        return 0 if tag in ("OK", "JTAG") else 1
    elif cmd == "identity":
        _cr(print_version)
    elif cmd == "temp":
        print_temperature()
    elif cmd == "fan":
        _cr(lambda: oprintf(0, "\t*** Fan speed: %d rpm\n" % (dev.fan_rpm() or 0)))
    elif cmd in ("crrd", "crwr"):
        try:
            addr = int(args[1], 16)
            if cmd == "crwr":
                dev.t.cr_write(addr, int(args[2], 16))
            print("0x%06X = 0x%08X" % (addr, dev.t.cr_read(addr)))
        except CrRefused as e:
            print("\t*** CR space refused: %s  (the ConnectX refuses CR while the User image runs)" % e)
            return 1
    elif cmd == "image-sel":
        names = {"user": IMG_USER, "factory": IMG_FACTORY, "flex": IMG_FACTORY,
                 "failover": IMG_FAILOVER, "flex3": IMG_FLEX}
        if len(args) < 2 or args[1] not in names:
            raise SystemExit("image-sel needs one of: %s" % ", ".join(names))
        set_fpga_image(names[args[1]])
        print_scheduled_image()
    elif cmd in ("jtag-on", "jtag-off"):
        set_connect(OP_DISCONNECT if cmd == "jtag-on" else OP_CONNECT)
        print(" FPGA is %s" % ("disconnected" if C.state["status"] == 3 else "connected"))
    elif cmd == "ddr":
        _cr(ddr_single_test)
    elif cmd == "pcitest":
        open_bope()
        print("\t*** PCI test: %s" % ("failed" if pci_test() else "passed"))
    elif cmd == "burn":
        open_bope()
        burning_all()
    elif cmd == "burn-inshell":
        return burn_inshell(cx5_bdf, args[1])
    else:
        raise SystemExit("unknown batch command '%s'" % cmd)
    return 0


# ---------------------------------------------------------------- command line (burn_app.c)
USAGE = """-----------------------------------------------
%s [-hv] [-b <arg>] [-p <bope device>] [-d <mst device>] [-s <size>]
  -h print this text and exit
  -v verbose output (repeat for more verbocity)
  -s size of buffer in kilobytes. Not more than %d
  --flex_image - burn flex image. Must be before -b argument
  -b image to burn (can be used multiple times)
     argument format: <file name>[,<flash>[,<offset>]]
     file name - file to burn,
     flash - 0|1 flash to burn (default - 0),
     flash offset - hex value (default %#010x)
  -p bope device for FPGA PCI access (for example 0000:07:00.0 or 07:00)
  -d mst device for FPGA ConnectX access (for example /dev/0000:08:00.0_mlx5_fpga_tools or 08:00)
  --developer_mode - developer mode, no actual read-write
  --log_name [name] - user defined file name.
       By default %s
  --no_log - log will not print to the file
 [ext] --transport auto|node|mlxreg   ConnectX register transport (default auto; mlxreg = MFT mlxreg or mstflint mstreg)
 [ext] --cross-check                  confirm image-select/JTAG writes via mlxreg/mstreg too (off by default)
 [ext] --batch <cmd> [args]           query health identity temp fan ddr pcitest crrd crwr
                                      image-sel <user|flex|flex3> jtag-on jtag-off
                                      burn (uses the -b files)   burn-inshell <mcs-prefix>
 [ext] --yes                          answer confirmations with yes (batch use)
"""


def usage(prg):
    sys.stdout.write(USAGE % (prg, MAXIMAL_SIZE_OF_DDR // 1024, DFL_FLASH_OFFSET, DFL_LOGNAME))


def parse_args(argv):
    import getopt
    longs = ["version", "mlx_force", "flex_image", "nvconf_only", "autorun=", "enable_sysfs",
             "developer_mode", "no_log", "log_name=", "query=", "transport=", "batch", "yes", "dev=", "cross-check"]
    try:
        opts, rest = getopt.getopt(argv[1:], "vhs:b:p:d:", longs)
    except getopt.GetoptError:
        usage(argv[0]); sys.exit(-1)
    a = dict(d=None, p=None, query=None, transport="auto", batch=None, nvconf=False, blist=[], cross_check=False)
    for o, v in opts:
        if o == "--version":
            print("%s version %s" % (APP_NAME, APP_VER)); sys.exit(0)
        elif o == "-v":
            C.verbose += 1
        elif o == "-h":
            usage(argv[0]); sys.exit(-1)
        elif o == "-s":
            try:
                C.maximal_size_of_mem = int(v) * 1024
            except ValueError:
                usage(argv[0]); sys.exit(-1)
            if not 0 < C.maximal_size_of_mem <= MAXIMAL_SIZE_OF_DDR:
                oprintf(0, '%sError%s: wrong parameter "-s %s" - must be more than 0 and not more than %d\n'
                        % (RED, NRM, v, MAXIMAL_SIZE_OF_DDR // 1024))
                usage(argv[0]); sys.exit(-1)
        elif o == "-b":
            a["blist"].append(v)
        elif o == "-p":
            a["p"] = v
        elif o in ("-d", "--dev"):
            a["d"] = v
        elif o == "--mlx_force":
            C.mlx_force = True
        elif o == "--flex_image":
            C.flex_image = True
        elif o == "--nvconf_only":
            a["nvconf"] = True
        elif o == "--autorun":
            C.has_autorun, C.autorun = True, int(v)
        elif o == "--developer_mode":
            C.developer_mode = True
        elif o == "--no_log":
            C.no_logger = True
        elif o == "--log_name":
            a["log_name"] = v
        elif o == "--query":
            a["query"] = v
        elif o == "--transport":
            a["transport"] = v
        elif o == "--cross-check":
            a["cross_check"] = True
        elif o == "--yes":
            C.autorun = C.autorun or -1        # confirm() auto-yes without selecting a menu item
        elif o == "--batch":
            a["batch"] = rest; rest = []
    if rest:
        oprintf(0, '%sError%s: wrong parameter "%s"\n' % (RED, NRM, rest[0]))
        usage(argv[0]); sys.exit(-1)
    return a


def _bdf_from(s):
    """'/dev/0000:08:00.0_mlx5_fpga_tools', '0000:08:00.0' or '08:00' -> the matching BDF (substring, as the vendor)."""
    return os.path.basename(s).split("_")[0] if s else None


def display_args(level, cx5):
    oprintf(level, "===============================================\n")
    oprintf(level, " Verbosity:        %d\n" % C.verbose)
    oprintf(level, " BOPE device:      %s\n" % (C.bope_bdf or "None"))
    oprintf(level, " ConnectX device:  %s\n" % (cx5 or "None"))
    if C.has_autorun:
        oprintf(level, " Autorun menu      %d\n" % C.autorun)
    if C.b_files:
        oprintf(level, " Files to burn:\n")
    for b in C.b_files:
        oprintf(level, "-------------------------------------------\n")
        oprintf(level, ' File name "%s"\n' % b["fname"])
        oprintf(level, " File length %d\n" % b["real_length"])
        oprintf(level, " Aligned length %d\n" % b["aligned_length"])
        oprintf(level, " Flash to burn %d\n" % b["flash"])
        if b["flash_offset"] == DFL_DEFAULT_OFFSET:
            oprintf(level, " Flash offset is default\n")
        else:
            oprintf(level, " Flash offset %#x\n" % b["flash_offset"])


def choose_device(devs, want):
    if not devs:
        oprintf(0, "Cannot find appropriate ConnectX device\n")
        sys.exit(1)
    if want:
        hit = [d for d in devs if want in d or d.endswith(want)]
        if not hit:
            oprintf(0, "Cannot find given %s ConnectX device\n" % want)
            sys.exit(1)
        oprintf(1, " ConnectX device: %s\n" % hit[0])
        return hit[0]
    if len(devs) == 1:
        oprintf(1, " ConnectX device: %s\n" % devs[0])
        return devs[0]
    menu = [(i + 1, d) for i, d in enumerate(devs)] + [(99, "Exit from program"), (-1, None)]
    oprintf(0, "\nChoose ConnectX device\n")
    c = menu_get_answer(menu)
    if c == 99:
        oprintf(0, "User did not select ConnectX device. Exiting...\n")
        sys.exit(1)
    oprintf(1, " ConnectX device: %s\n" % devs[c - 1])
    return devs[c - 1]


def run_query(what, devs, want):
    sel = [d for d in devs if not want or want in d]
    if want and not sel:
        oprintf(0, "Cannot find given %s ConnectX device\n" % want)
        sys.exit(1)
    if what == "number":
        oprintf(0, "Number of devices=%d\n" % len(devs))
    elif what == "address":
        for d in sel:
            oprintf(0, "%s\n" % d)
    else:
        for d in sel:
            dv = Innova2(d, "auto")
            if what == "fpga_selected":
                q = dv.query()
                oprintf(0, " Running image:    %s %s\n" % (IMG_STR.get(q["oper"], "?"),
                                                           "Success" if q["status"] == 0 else "Failure"))
            elif what == "fpga_version":
                try:
                    oprintf(0, " Version:    0x%X\n" % dv.field_read(R_IMAGE_VER))
                except CrRefused as e:
                    oprintf(0, " Version:    CR space refused (%s)\n" % e)
            elif what == "type":
                oprintf(0, " Type of board: Morse\n")
            elif what == "fpga_connection":
                q = dv.query()
                oprintf(0, " FPGA is %s\n" % ("disconnected" if q["status"] == 3 else "connected"))
            elif what == "temperature":
                c, _ = dv.cx_temp_mtmp()
                oprintf(0, "\t*** ConnectX Temperature: %d C\n" % c)
            else:
                return 1
    return 0


def main():
    a = parse_args(sys.argv)
    if os.geteuid() != 0:
        print("*** must run as root", file=sys.stderr)
        return 1
    if not C.no_logger:
        name = a.get("log_name", DFL_LOGNAME)
        try:
            C.logfile = open(name, "a")
        except OSError as e:
            print(" Cannot create log file %s.\n The error is %s.\n Exiting..." % (name, e))
            return 1
        offprintf(" **************************************************\n")
        offprintf(" * Program %s started in %s\n" % (sys.argv[0], time.strftime("%d/%m/%y %H:%M:%S")))
        offprintf(" **************************************************\n")
        offprintf(" Command line is  %s\n" % " ".join(sys.argv))
    for s in a["blist"]:
        b = translate_b_option(s)
        if b is None:
            usage(sys.argv[0]); return -1
        C.b_files.insert(0, b)           # the vendor prepends to its list
    # the vendor scans "/dev/*0_mlx5_fpga_tools" (I2C_SUFFIX): one node per card, function 0 only
    devs = [d for d in find_cx5_devices() if d.endswith(".0")]
    want = _bdf_from(a["d"])
    if a["query"]:
        return run_query(a["query"], devs, want)
    cx5 = choose_device(devs, want)
    C.bope_bdf = _bdf_from(a["p"]) or find_bope_for(cx5)
    if C.bope_bdf and ":" in C.bope_bdf and C.bope_bdf.count(":") == 1:
        C.bope_bdf = "0000:" + C.bope_bdf + (".0" if "." not in C.bope_bdf else "")
    display_args(1, cx5)
    C.dev = Innova2(cx5, a["transport"], a["cross_check"])
    C.state = C.dev.query()
    if a["batch"] is not None:
        return batch(C.dev, a["batch"], cx5) or 0
    oprintf(0, " Scheduled image:  %s\n" % IMG_SCHED_STR.get(C.state["admin"], "?"))
    run = IMG_STR.get(C.state["oper"], "?")
    ok = "Success" if C.state["status"] == 0 else "Failure"
    if C.state["oper"] == IMG_FACTORY and not C.bope_bdf:
        oprintf(0, " %sRunning image:    %s(%s)%s\n" % (RED, run, ok, NRM))
    else:
        oprintf(0, " Running image:    %s(%s)\n" % (run, ok))
    oprintf(0, " Type of board: Morse\n")
    if a["nvconf"]:
        nvconf_interaction()
    else:
        user_interaction(C.autorun if C.autorun > 0 else 0)
    if C.logfile:
        C.logfile.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
