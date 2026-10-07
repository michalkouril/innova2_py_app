#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""innova2-smi host layer: card discovery, shell identification, guarded MMIO, and the read-only collectors.

Everything that touches the machine goes through a Host object (sysfs, BAR mappings, subprocesses), so the unit
tests can substitute a mock host with a mock register model (tests/test_innova2_smi.py).

MMIO RULES (measured on these cards, see the shell notes):
  * ctypes 32-bit loads and stores only. A python mmap slice goes through memcpy, which issues the access twice;
    the recorder index register and every FIFO would see it twice.
  * only the decoded windows listed in innova2_smi_regmap are mapped, each one page, never the whole 32 MB BAR;
  * inside a window, only offsets in the running layout's read/write sets are touched. The Guard below enforces it
    for every access, and it starts out allowing nothing but OBS_ID, so an unknown shell is never read further.
"""

import ctypes
import glob
import json
import mmap
import os
import re
import subprocess
import time

import innova2_smi_regmap as R

SYS_PCI = "/sys/bus/pci/devices"
XRT_BIN = "/opt/xilinx/xrt/bin"


class UnsafeAccess(Exception):
    """An access the running shell's register map does not allow. Raised BEFORE the bus is touched."""


class Refused(Exception):
    """innova2-smi refuses to continue (unknown shell, card not responding, missing prerequisite)."""


class Guard:
    def __init__(self, reads=(), writes=()):
        self.reads, self.writes = set(reads), set(writes)

    def allow(self, reads=(), writes=()):
        self.reads |= set(reads)
        self.writes |= set(writes)

    def check_read(self, off):
        if off not in self.reads:
            raise UnsafeAccess("read of 0x%06x refused: not in the running shell's register map" % off)

    def check_write(self, off):
        if off not in self.writes:
            raise UnsafeAccess("write of 0x%06x refused: not a writable register of the running shell" % off)


class RegIO:
    """One PCI BAR, with only the listed regions mapped (one mmap per region, created on first use)."""

    def __init__(self, path, regions, guard):
        self.path, self.regions, self.guard = path, regions, guard
        self.fd, self.maps = None, {}

    def _addr(self, off):
        for name, (base, size, _) in self.regions.items():
            if base <= off < base + size:
                if name not in self.maps:
                    if self.fd is None:
                        self.fd = os.open(self.path, os.O_RDWR | os.O_SYNC)
                    m = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
                    self.maps[name] = (m, ctypes.addressof(ctypes.c_char.from_buffer(m)))
                return self.maps[name][1] + off - base
        raise UnsafeAccess("0x%06x is outside every region innova2-smi maps" % off)

    def rd(self, off):
        self.guard.check_read(off)
        return ctypes.c_uint32.from_address(self._addr(off)).value

    def wr(self, off, val):
        self.guard.check_write(off)
        ctypes.c_uint32.from_address(self._addr(off)).value = val & 0xFFFFFFFF


class Host:
    """The real machine."""

    def pci_devices(self):
        out = []
        for p in sorted(glob.glob(SYS_PCI + "/*")):
            b = os.path.basename(p)
            v, d, c = self.attr(b, "vendor"), self.attr(b, "device"), self.attr(b, "class")
            if v is None:
                continue
            real = os.path.realpath(p).split("/")
            chain = [x for x in real if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", x)]
            out.append(dict(bdf=b, vendor=v, device=d, cls=c or "", chain=chain[:-1]))
        return out

    def attr(self, bdf, name):
        """A sysfs attribute of the device (name may contain a glob, e.g. 'rom.m.*/VBNV'); None if unreadable."""
        paths = glob.glob(os.path.join(SYS_PCI, bdf, name))
        if not paths:
            return None
        try:
            with open(paths[0]) as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None

    def attrs(self, bdf, pattern):
        """{basename: value} for every readable file matching the glob under the device."""
        out = {}
        for p in glob.glob(os.path.join(SYS_PCI, bdf, pattern)):
            try:
                with open(p) as f:
                    out[os.path.basename(p)] = f.read().strip()
            except (OSError, UnicodeDecodeError):
                pass
        return out

    def raw(self, bdf, name, n=4096):
        paths = glob.glob(os.path.join(SYS_PCI, bdf, name))
        if not paths:
            return None
        try:
            with open(paths[0], "rb") as f:
                return f.read(n)
        except OSError:
            return None

    def bar_size(self, bdf, n):
        try:
            return os.path.getsize(os.path.join(SYS_PCI, bdf, "resource%d" % n))
        except OSError:
            return None

    def regio(self, bdf, n, regions, guard):
        return RegIO(os.path.join(SYS_PCI, bdf, "resource%d" % n), regions, guard)

    def run(self, cmd, timeout=60, env=None):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            return p.returncode, p.stdout + p.stderr
        except FileNotFoundError as e:
            return 127, str(e)
        except subprocess.TimeoutExpired as e:
            return 124, "timed out after %d s: %s" % (timeout, " ".join(cmd))

    def driver(self, bdf):
        p = os.path.join(SYS_PCI, bdf, "driver")
        return os.path.basename(os.path.realpath(p)) if os.path.exists(p) else None

    def resource(self, bdf, n):
        """(start, size) of BAR n from sysfs 'resource'; (0, 0) when unassigned or unreadable."""
        try:
            line = open(os.path.join(SYS_PCI, bdf, "resource")).read().splitlines()[n].split()
            st, en = int(line[0], 16), int(line[1], 16)
            return (st, en - st + 1) if st else (0, 0)
        except (OSError, IndexError, ValueError):
            return 0, 0

    def pci_command(self, bdf):
        try:
            with open(os.path.join(SYS_PCI, bdf, "config"), "rb") as f:
                f.seek(4)
                b = f.read(2)
            return int.from_bytes(b, "little") if len(b) == 2 else None
        except OSError:
            return None

    def exists(self, path):
        return os.path.exists(path)

    def glob(self, pattern):
        return sorted(glob.glob(pattern))

    def read_file(self, path):
        try:
            with open(path) as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None

    def file_head(self, path, n):
        try:
            with open(path, "rb") as f:
                return f.read(n)
        except OSError:
            return None

    def readlink(self, path):
        return os.readlink(path) if os.path.islink(path) else None

    def relink(self, link, target):
        """Point the symlink `link` at `target` atomically (new link + rename)."""
        tmp = link + ".innova2-smi-new"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, link)

    def host_uptime(self):
        try:
            return float(open("/proc/uptime").read().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def bounded(self, fn, timeout, default):
        """Run fn() in a daemon thread; give up after `timeout` s (a half-present card must not hang discovery)."""
        import threading
        box = {}

        def run():
            try:
                box["v"] = fn()
            except BaseException as e:            # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive() or "e" in box:
            return default
        return box.get("v", default)

    def boot_id(self):
        try:
            return open("/proc/sys/kernel/random/boot_id").read().strip()
        except OSError:
            return None

    def sleep(self, s):
        time.sleep(s)

    def now(self):
        return time.time()

    def is_root(self):
        return os.geteuid() == 0

    def which_xrt(self):
        for n in ("xrt-smi", "xbutil"):
            p = os.path.join(XRT_BIN, n)
            if os.access(p, os.X_OK):
                return p
        return None

    def cx5_query(self, cx5_bdf, transport="auto"):
        """FPGA_CTRL read through innova2_app's transports: running/scheduled image, status. Read-only."""
        import innova2_app as ia
        try:
            dev = ia.Innova2(cx5_bdf, transport)
            q = dev.query()
            out = dict(q, transport=dev.t.name)
            try:
                c, _ = dev.fpga_temp_mtmp()
                out["fpga_mtmp_c"] = c
            except Exception:                             # noqa: BLE001 -- optional sensor
                pass
            return out
        except BaseException as e:                       # noqa: BLE001 -- Innova2() raises SystemExit
            return dict(error=str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__)


# ----------------------------------------------------------------------------------------------- discovery
# Cards are found from the ConnectX-5 side (the Mellanox functions are present whatever the FPGA runs), then the
# FPGA functions are looked up behind the same on-card PCIe switch. The FPGA state is classified without touching
# the FPGA beyond sysfs and, for a shell that passes every check, the guarded register reads.
SHELL_DEV_IDS = {"0x788f", "0x6a90", "0x6a9f", "0x7890", "0x6aa0"}     # the Innova-2 XDMA shell family
MGMT_DEV_IDS, USER_DEV_IDS = {"0x788f", "0x6a9f"}, {"0x6a90", "0x6aa0", "0x7890"}
STATES = {
    "OUR_SHELL": "an fp8c XRT shell with a known OBS_ID: full status",
    "OUR_SHELL_OLD": "a pre-fp8c Innova-2 XRT shell: identity only (no ddr_health map)",
    "OUR_SHELL_UNREADABLE": "a shell device ID, but a safety check failed: nothing further is read",
    "FLEX": "the Flex/Factory image (burn endpoint 15b3:0264, or the ConnectX reports it): not a User shell",
    "FPGA_MISSING": "the ConnectX-5 is present but no FPGA function is behind its switch",
    "OTHER_XILINX": "a Xilinx function that is not an Innova-2 XRT shell (e.g. the DDR tester 10ee:9038)",
}
CX5_IMAGES = {0: "User", 1: "Flex slot (vendor label 'Factory')", 2: "Factory failover", 3: "Flex"}


def _is(d, vendor, device=None, cls_prefix=None):
    return (d["vendor"] == vendor and (device is None or d["device"] == device)
            and (cls_prefix is None or d["cls"].startswith(cls_prefix)))


def discover(host):
    """Every Innova-2 card, keyed by the upstream port of the switch its ConnectX-5 sits behind."""
    devs = host.pci_devices()
    cards = {}
    for d in devs:
        if _is(d, R.CX5_VENDOR, cls_prefix="0x02") and len(d["chain"]) >= 2:
            sw = d["chain"][-2]
            c = cards.setdefault(sw, dict(switch=sw, root_port=d["chain"][-3] if len(d["chain"]) >= 3 else None,
                                          cx5=[], fpga=[], mgmt=None, user=None, bope=None))
            c["cx5"].append(d["bdf"])
    for d in devs:
        for sw, c in cards.items():
            if sw in d["chain"] and d["bdf"] not in c["cx5"]:
                if _is(d, R.CX5_VENDOR, R.BOPE_DEV):
                    c["bope"] = d["bdf"]
                elif d["vendor"] == R.FPGA_VENDOR:
                    c["fpga"].append(dict(bdf=d["bdf"], device=d["device"], driver=host.driver(d["bdf"])))
    res = []
    for sw in sorted(cards):
        c = cards[sw]
        # an Innova-2 card: the switch is the ConnectX-5's PCIe bridge (15b3:1974), or FPGA/burn functions are behind it
        if host.attr(sw, "device") != "0x1974" and not (c["fpga"] or c["bope"]):
            continue
        c["cx5"].sort()
        for f in c["fpga"]:
            if f["device"] in MGMT_DEV_IDS and f["bdf"].endswith(".0"):
                c["mgmt"] = f["bdf"]
            elif f["device"] in USER_DEV_IDS:
                c["user"] = f["bdf"]
        res.append(c)
    for i, c in enumerate(res):
        c["index"] = i
    return res


def cx5_health(host, card):
    """The ConnectX-5 side: driver, firmware, the mlx5_fpga_tools/innova2_areg node, devlink health. Bounded."""
    nic = card["cx5"][0]
    out = dict(bdf=nic, driver=host.driver(nic), node=None, fw_ver=None, devlink=None, notes=[])
    node = "/dev/%s_mlx5_fpga_tools" % nic
    out["node"] = node if host.exists(node) else None
    for p in host.glob("/sys/bus/pci/devices/%s/infiniband/*/fw_ver" % nic):
        out["fw_ver"] = host.read_file(p)
    if out["driver"] != "mlx5_core":
        out["notes"].append("ConnectX-5 %s has no mlx5_core driver bound (%s)" % (nic, out["driver"]))
    if not out["node"]:
        out["notes"].append("no %s: the FPGA image query needs 'modprobe mlx5_fpga_tools' (MLNX_OFED 5.2) or "
                            "'modprobe innova2_areg' (this package); MFT mlxreg works without a module" % node)
    rc, txt = host.run(["devlink", "health", "show", "pci/" + nic], timeout=5)
    if rc == 0:
        bad = re.findall(r"reporter (\S+)\s+state (\S+)\s+error (\d+)", txt)
        out["devlink"] = [dict(reporter=a, state=b, errors=int(e)) for a, b, e in bad]
        for a, b, e in bad:
            if b != "healthy":
                out["notes"].append("devlink health reporter %s is '%s' (%s errors)" % (a, b, e))
    return out


def classify(host, card, query_cx5=True, transport="auto"):
    """Fill card['state'], card['cx5_query'], card['flags'] (each flag: severity, text). Never raises."""
    flags = []
    q = None
    if query_cx5:
        q = host.bounded(lambda: host.cx5_query(card["cx5"][0], transport), 15,
                         dict(error="ConnectX-5 query timed out after 15 s"))
    card["cx5_query"] = q
    oper = q.get("oper") if q and "error" not in q else None
    status = q.get("status") if q and "error" not in q else None
    shell_fns = [f for f in card["fpga"] if f["device"] in SHELL_DEV_IDS]
    other = [f for f in card["fpga"] if f["device"] not in SHELL_DEV_IDS]
    if shell_fns:
        card["state"] = "OUR_SHELL?"            # refined by Shell.identify()
    elif other:
        card["state"] = "OTHER_XILINX"
    elif card["bope"] or oper in (1, 3) or (oper == 2):
        card["state"] = "FLEX"
    else:
        card["state"] = "FPGA_MISSING"
    if oper == 0 and (card["state"] in ("FPGA_MISSING", "OTHER_XILINX")):
        flags.append(("FAIL", "CX5 inconsistent: the ConnectX reports the User image running, but %s" % (
            "no FPGA function is present" if card["state"] == "FPGA_MISSING"
            else "the FPGA function is not an Innova-2 XRT shell (%s)" % ", ".join(
                "%s 10ee:%s" % (f["bdf"], f["device"][2:]) for f in other))))
    if status not in (None, 0, 3):
        flags.append(("FAIL", "CX5 inconsistent: the ConnectX reports a failed image load (status %d, oper %s)" % (
            status, oper)))
    if q and "error" in q:
        flags.append(("INFO", "ConnectX-5 FPGA query unavailable: %s" % q["error"]))
    card["flags"] = flags
    return card


def cx5_fw_ident(host, nic):
    """PSID and firmware version from the RDMA device's sysfs (board_id / fw_ver): present even on a card whose VPD is
    blank. Read-only, no firmware command."""
    out = {}
    for p in host.glob("/sys/bus/pci/devices/%s/infiniband/*/board_id" % nic):
        out["psid"] = host.read_file(p)
        out["fw_ver"] = host.read_file(os.path.join(os.path.dirname(p), "fw_ver"))
        break
    return out


def board_id(host, card):
    """The board identity from the ConnectX-5 VPD (PN, EC, SN), plus the PSID / firmware from sysfs.
    Some cards carry an EMPTY VPD (only the end tag; measured on hpz440's card, whose base GUID and MAC are blank
    too): then vpd_empty=True and validated=None (unknown -- not 'not validated')."""
    if not card["cx5"]:
        return dict(error="no ConnectX-5 on this card")
    nic = card["cx5"][0]
    raw = host.bounded(lambda: host.raw(nic, "vpd"), 10, None)
    fw = cx5_fw_ident(host, nic)
    if not raw:
        return dict(error="cannot read the VPD of %s (root needed, or it timed out)" % nic, nic=nic, **fw)
    v = R.parse_vpd(raw)
    v["nic"] = nic
    v.update(fw)
    pn, ec = v.get("PN"), v.get("EC")
    v["vpd_empty"] = not any(v.get(k) for k in ("PN", "EC", "SN", "name"))
    v["validated"] = None if v["vpd_empty"] else (pn, ec) in R.VALIDATED_BOARDS
    return v


def board_summary(b):
    """One line for list/status: what the VPD holds, never a bare '?'."""
    fw = (", PSID %s, fw %s" % (b["psid"], b.get("fw_ver"))) if b.get("psid") else ""
    if "error" in b:
        return "unknown (%s)%s" % (b["error"], fw)
    if b.get("vpd_empty"):
        return "VPD empty (no PN/EC/SN in this ConnectX-5's VPD)%s -- board not identifiable, cannot be validated" % fw
    missing = [k for k in ("PN", "EC", "SN") if not b.get(k)]
    return "%s rev %s, serial %s%s -- %s" % (
        b.get("PN") or "(no PN)", b.get("EC") or "(no EC)", b.get("SN") or "(no SN)",
        (" (the VPD has no %s)" % "/".join(missing)) if missing else "",
        "validated board" if b.get("validated") else "*** NOT a validated board: the DDR4 part may differ from " + R.DDR_PART)


def select_card(cards, want):
    """-d: index, any BDF of the card (ConnectX or FPGA; short forms like 08:00 accepted) or the board serial."""
    if want is None:
        if len(cards) == 1:
            return cards[0]
        raise Refused("%d Innova-2 cards found; choose one with -d <index|BDF|serial> (or --all where offered)"
                      % len(cards))
    if re.fullmatch(r"\d+", want) and int(want) < len(cards):
        return cards[int(want)]
    for c in cards:
        bdfs = [x for x in [c["mgmt"], c["user"], c["bope"]] + c["cx5"] + [f["bdf"] for f in c["fpga"]] if x]
        if any(b == want or b.endswith(want) or b[5:].startswith(want) for b in bdfs):
            return c
        if c.get("serial") and c["serial"] == want:
            return c
    raise Refused("no Innova-2 card matches '%s'" % want)


# ----------------------------------------------------------------------------------------------- identification
class Shell:
    """A running fp8c shell, identified step by step. `io` is the mgmt BAR0, `uio` the user BAR0, both guarded.
    The first failing step stops identification: nothing past it is read (an unmapped read can hang the host)."""

    def __init__(self, host, card):
        self.host, self.card = host, card
        self.mgmt, self.user = card.get("mgmt"), card.get("user")
        self.ok, self.reason, self.warnings, self.steps = False, None, [], []
        self.state = None
        self.vbnv = self.rom_ts = self.obs_id = self.lay = self.name = None
        self.guard, self.uguard = Guard(), Guard()
        self.io = self.uio = None

    def _step(self, name, ok, detail=""):
        self.steps.append(dict(step=name, ok=bool(ok), detail=detail))
        if not ok:
            self.reason = "%s: %s" % (name, detail)
        return ok

    def _bar_ok(self, bdf, minsize):
        st, size = self.host.resource(bdf, 0)
        cmd = self.host.pci_command(bdf)
        if not st or not size:
            return False, "BAR0 not assigned"
        if size < minsize:
            return False, "BAR0 is 0x%x bytes, expected >= 0x%x" % (size, minsize)
        if cmd is None or not cmd & 2:
            return False, "memory decoding disabled (PCI COMMAND %s)" % ("unreadable" if cmd is None else "0x%04x" % cmd)
        return True, "BAR0 0x%x, %d MB, memory decoding on" % (st, size >> 20)

    def identify(self):
        h = self.host
        self.state = "OUR_SHELL_UNREADABLE"
        if not self._step("management function", self.mgmt, "no 10ee:788f/6a9f function .0 behind the switch "
                          "(functions: %s)" % ", ".join("%s %s" % (f["bdf"], f["device"]) for f in self.card.get("fpga", []))):
            return self
        ok, det = self._bar_ok(self.mgmt, 0x200000)
        if not self._step("mgmt BAR0", ok, det):
            return self
        drv = h.driver(self.mgmt)
        if not self._step("xclmgmt bound", drv == "xclmgmt", "driver is %s" % (drv or "none")):
            return self
        vbnv, ts = h.attr(self.mgmt, "rom.*/VBNV"), h.attr(self.mgmt, "rom.*/timestamp")
        if not self._step("feature ROM (xclmgmt sysfs)", vbnv and ts and ts.isdigit(),
                          "VBNV=%r timestamp=%r" % (vbnv, ts)):
            return self
        self.vbnv, self.rom_ts = vbnv, int(ts)
        known = R.SHELLS.get(self.rom_ts)
        self.name = known["name"] if known else None
        if self.vbnv == R.VBNV_OLD:
            self.state = "OUR_SHELL_OLD"
            self._step("platform", False, "pre-fp8c shell %s: no ddr_health map; identity only" % self.vbnv)
            return self
        if not self._step("platform", self.vbnv in R.VBNVS, "VBNV %s" % self.vbnv):
            return self
        self.io = h.regio(self.mgmt, 0, R.MGMT_REGIONS, self.guard)
        self.guard.allow(reads=[R.STATUS + R.OBS_ID_OFF])
        oid = self.io.rd(R.STATUS + R.OBS_ID_OFF)
        self.obs_id = oid
        if not self._step("OBS_ID", oid != 0xFFFFFFFF, "mgmt BAR reads all-ones: card not responding"):
            return self
        lay = R.layout(oid)
        if not self._step("OBS_ID", lay is not None, "0x%08X is not a layout this tool knows: refusing every other "
                          "register access" % oid):
            return self
        if known and known["obs_id"] != oid:
            self.warnings.append("ROM_TS %d is %s, which should report OBS_ID 0x%08X, but the card reports 0x%08X "
                                 "-- using the register map of the OBS_ID" % (self.rom_ts, known["name"],
                                                                              known["obs_id"], oid))
        if not known:
            self.name = "unlisted build (ROM_TS %d), %s layout" % (self.rom_ts, lay["name"])
        if R.is_routev(lay) != (self.vbnv == R.VBNV_ROUTEV):
            self.warnings.append("VBNV %s with OBS_ID 0x%08X (%s): the platform and the shell's register layout "
                                 "disagree -- using the register map of the OBS_ID" % (self.vbnv, oid, lay["name"]))
        self.guard.allow(reads=R.read_set(oid), writes=R.write_set(oid))
        if not self._step("status register", self.io.rd(R.STATUS) != 0xFFFFFFFF, "mgmt_status reads all-ones"):
            return self
        self.lay = lay
        if self.user:
            ok, det = self._bar_ok(self.user, 0x200000)
            self.steps.append(dict(step="user BAR0", ok=ok, detail=det + ("" if ok else " (user-PF registers skipped)")))
            if ok:
                self.uio = h.regio(self.user, 0, R.USER_REGIONS, self.uguard)
                self.uguard.allow(reads=R.USER_READS)
        self.ok, self.state, self.reason = True, "OUR_SHELL", None
        return self

    def need(self, feature):
        if not self.ok:
            raise Refused(self.reason or "shell not identified")
        if feature == "e7" and not self.lay["e7"]:
            raise Refused("this shell (%s) has no throttle / PCIe / PR / flight recorder (E7a2 and later)" % self.name)
        if feature == "e7a3" and not self.lay["e7a3"]:
            raise Refused("this shell (%s) has no live temperature / two-tier throttle (E7a3 and later)" % self.name)

    def r(self, off):
        return self.io.rd(R.STATUS + off)

    def ident(self):
        return dict(state=self.state, vbnv=self.vbnv, rom_ts=self.rom_ts,
                    rom_ts_hex="0x%08x" % self.rom_ts if self.rom_ts else None, build=self.name,
                    obs_id="0x%08X" % self.obs_id if self.obs_id is not None else None,
                    layout=self.lay["name"] if self.lay else None,
                    interface_uuid=(R.SHELLS.get(self.rom_ts) or {}).get("uuid"),
                    supported=self.ok, refused=self.reason, warnings=self.warnings, steps=self.steps)


# ----------------------------------------------------------------------------------------------- collectors
def ddr_state(sh):
    s, c, m = sh.io.rd(R.STATUS), sh.io.rd(R.MEMCALIB), sh.io.rd(R.MODE_REG)
    d = dict(requested=R.MODE_NAMES[m & 3], in_force=R.MODE_NAMES[s >> 29 & 3], mig_held=bool(s >> 31 & 1),
             gate_open=bool(s >> 28 & 1), fenced_seen=bool(s >> 27 & 1), xrt_calibration_bit=c & 1,
             raw_status="0x%08x" % s)
    if sh.lay and R.is_routev(sh.lay):
        rv = sh.r(0x1C0)
        d["xclbin_ddr"] = "KEPT" if rv & 1 else "PRUNED"       # route V: does the loaded xclbin have DDR?
    return d


def routev(sh):
    """Route V (OBS_ID 0x0B5E0008): the DDR state of the loaded xclbin -- KEPT (MIG in the dynamic region) or PRUNED
    (no DDR) -- calibration, load / calibration counts, per-load ECC, fences while KEPT. None on other shells."""
    if not (sh.lay and R.is_routev(sh.lay)):
        return None
    i, ld, cal = sh.r(0x1C0), sh.r(0x1C4), sh.r(0x1C8)
    uel = sh.r(0x1D4)
    out = dict(signature_ok=(i >> 16) == R.RV_SIGNATURE, xclbin_ddr="KEPT" if i & 1 else "PRUNED",
               ddr_present=bool(i & 1), calibrated=bool(i >> 1 & 1), rp_calib=bool(i >> 2 & 1),
               ecc_poller=bool(i >> 3 & 1), rp_released=bool(i >> 4 & 1), gate_open=bool(i >> 5 & 1),
               ecc_accepting=bool(i >> 6 & 1), loads_kept=ld & 0xFFFF, loads_pruned=ld >> 16,
               calibrations=cal & 0xFFFF, calibrations_lost=cal >> 16,
               last_calibration_ms=round(sh.r(0x1CC) / R.ACLK_HZ * 1e3, 3),
               ecc_this_load=dict(ce=sh.r(0x1D0), ue_polls=uel & 0xFFFF, polled=bool(uel >> 29 & 1),
                                  ue_seen=bool(uel >> 31 & 1), polls=sh.r(0x1D8)),
               fenced_while_kept=sh.r(0x1EC), raw_info="0x%08x" % i)
    out["ddr_info"] = R.decode_ddr_info(sh.r(0x200), sh.r(0x204), sh.r(0x208), sh.r(0x20C), sh.r(0x210), sh.r(0x214))
    for name, lo, hi in (("first_ce", 0x1DC, 0x1E0), ("first_ue", 0x1E4, 0x1E8)):
        h = sh.r(hi)
        out["ecc_this_load"][name] = R.decode_ffa((h & 0x7FFFFFFF) << 32 | sh.r(lo)) if h >> 31 & 1 else None
    return out


def poll_sample(sh):
    """(polls completed, uptime cycles): uptime read before and after the poll count, the midpoint kept (the reads are a
    few microseconds apart). Loads only."""
    def up():
        lo = sh.r(0xB0)
        return sh.r(0xB4) << 32 | lo                   # UPTIME_HI is latched by the UPTIME_LO read
    a = up()
    p = sh.r(0x2C)
    b = up()
    return p, (a + b) // 2


def ddr_ready_for_rate(sh, regs=None):
    """(ok, why): the ECC poller polls only while the MIG is calibrated, so the rate is measurable only then."""
    rd = (lambda o: regs[o]) if regs is not None else sh.r
    if R.is_routev(sh.lay):
        i = rd(0x1C0)
        if not (i & 1):
            return False, "no DDR in the loaded xclbin (PRUNED)"
        if not (i >> 1 & 1 and i >> 4 & 1):
            return False, "DDR KEPT but not calibrated / RP isolated"
        return True, ""
    if not rd(0x00) >> 28 & 1:
        return False, "DDR gate closed (not calibrated or mode off)"
    if not rd(0x18) >> 29 & 1:
        return False, "the ECC poller has never polled"
    return True, ""


def rate_result(cfg, ui, dp, dt, routev=False):
    """The measured rate next to the table value."""
    out = dict(ui_clk_mhz=round(ui, 3) if ui else None, mts=round(8 * ui, 1) if ui else None, polls=dp,
               window_s=round(dt, 3), precision_pct=R.ui_clk_precision_pct(dp),
               formula="ui_clk = POLL_CYC x polls / (uptime cycles / 250 MHz); MT/s = 8 x ui_clk (4:1 PHY, DDR)")
    if ui and cfg:
        out["vs_table_pct"] = round(100.0 * (ui - cfg["ui_clk_mhz"]) / cfg["ui_clk_mhz"], 2)
        out["poll_cyc"] = cfg["poll_cyc"]
        if routev:
            out["note"] = "route V: POLL_CYC and the MIG come from the KEPT xclbin; the template's values are assumed"
    return out


def ddr_speed(sh, window=1.5):
    """The DDR configuration of this build (R.MIG_BUILDS by ROM_TS, or DDR_INFO when the layout has it) and the MIG
    ui_clk measured passively from the ECC poller's poll count over `window` seconds of the 250 MHz uptime counter."""
    cfg = R.mig_config(sh.rom_ts)
    out = dict(table=cfg, table_source="build table (ROM_TS %d)" % sh.rom_ts if cfg else
               "unknown build (ROM_TS %s): no MIG table" % sh.rom_ts)
    if sh.obs_id in R.DDR_INFO_MAPS:                   # hook: route V's DDR_INFO block, once its map exists
        out["ddr_info"] = {n: dec(sh.r(o)) for o, (n, dec) in R.DDR_INFO_MAPS[sh.obs_id]["regs"].items()}
    ok, why = ddr_ready_for_rate(sh)
    if not ok:
        out["measured"] = dict(ui_clk_mhz=None, reason=why)
        return out
    if not cfg:
        out["measured"] = dict(ui_clk_mhz=None, reason="POLL_CYC unknown for an unlisted build")
        return out
    ld0 = sh.r(0x10) & 0xFFFF
    p0, u0 = poll_sample(sh)
    sh.host.sleep(window)
    p1, u1 = poll_sample(sh)
    if (sh.r(0x10) & 0xFFFF) != ld0:
        out["measured"] = dict(ui_clk_mhz=None, reason="a link-down (MIG reset) during the window; run again")
        return out
    ui, dp, dt = R.ui_clk_from_polls(p0, u0, p1, u1, cfg["poll_cyc"])
    out["measured"] = rate_result(cfg, ui, dp, dt, routev=R.is_routev(sh.lay)) if ui else \
        dict(ui_clk_mhz=None, reason="no poll completed in %.2f s" % dt)
    return out


def uptime(sh):
    lo = sh.r(0xB0)
    hi = sh.r(0xB4)
    hz = sh.r(0xB8) or 250000000
    cyc = hi << 32 | lo
    return dict(cycles=cyc, hz=hz, seconds=cyc / hz)


def ecc(sh):
    ue = sh.r(0x18)
    out = dict(ce=sh.r(0x14), ue_polls=ue & 0xFFFF, ue_seen=bool(ue >> 31 & 1), ecc_on=bool(ue >> 30 & 1),
               poller_alive=bool(ue >> 29 & 1), polls=sh.r(0x2C))
    for name, lo, hi in (("first_ce", 0x1C, 0x20), ("first_ue", 0x24, 0x28)):
        h = sh.r(hi)
        out[name] = R.decode_ffa((h & 0x7FFFFFFF) << 32 | sh.r(lo)) if h >> 31 & 1 else None
    return out


def health(sh):
    ld = sh.r(0x10)
    lk, c1, c2 = sh.r(0x34), sh.r(0x38), sh.r(0x3C)
    cnts = [c1 & 0xFF, c1 >> 8 & 0xFF, c1 >> 16 & 0xFF, c1 >> 24 & 0xFF, c2 & 0xFF]
    fi = sh.r(0xA4)
    return dict(
        link_down=dict(count=ld & 0xFFFF, recalibrated=bool(ld >> 16 & 1), mig_reset_active=bool(ld >> 17 & 1),
                       link_up=bool(ld >> 18 & 1), waiting_recal=bool(ld >> 19 & 1)),
        locks=[dict(name=n, locked=bool(lk >> i & 1), lost=bool(lk >> (8 + i) & 1), ever=bool(lk >> (16 + i) & 1),
                    losses=cnts[i]) for i, n in enumerate(R.LOCK_NAMES)],
        fenced=dict(count=sh.r(0xA0), first=(dict(read=bool(fi >> 30 & 1), addr=sh.r(0xAC) << 32 | sh.r(0xA8))
                                             if fi >> 31 & 1 else None)))


def throttle(sh):
    sh.need("e7")
    c, th, st = sh.r(0xC0), sh.r(0xC4), sh.r(0xC8)
    out = dict(ctrl="0x%02x" % c, enabled=bool(c & 1), force=bool(c >> 1 & 1), force_level=R.lvl(c >> 2),
               gate_data=bool(c >> 4 & 1), gate_kernel=bool(c >> 5 & 1),
               tier1=dict(on_code=th & 0x3FF, off_code=th >> 16 & 0x3FF, on_c=round(R.code2c(th & 0x3FF), 2),
                          off_c=round(R.code2c(th >> 16 & 0x3FF), 2)),
               status="0x%08x" % st, code=st >> 16 & 0x3FF, temp_c=round(R.code2c(st >> 16 & 0x3FF), 2),
               alarm=bool(st & 1), requested=bool(st >> 1 & 1), data_clk_throttled=bool(st >> 2 & 1),
               kernel_clk_throttled=bool(st >> 3 & 1), events=sh.r(0xCC), time_throttled_s=sh.r(0xD0) * R.TS_S,
               last_start_ts=sh.r(0xD4) * R.TS_S)
    if sh.lay["e7a3"]:
        t2, tl = sh.r(0xD8), sh.r(0xDC)
        out.update(
            tier2=dict(on_code=t2 & 0x3FF, off_code=t2 >> 16 & 0x3FF, on_c=round(R.code2c(t2 & 0x3FF), 2),
                       off_c=round(R.code2c(t2 >> 16 & 0x3FF), 2)),
            tier_ctrl="0x%08x" % tl, tier1_level=R.lvl(tl), tier2_level=R.lvl(tl >> 8), failsafe_enabled=bool(tl >> 16 & 1),
            tier_in_effect=R.TIER_NAMES[st >> 4 & 3], temperature_tier=R.TIER_NAMES[st >> 12 & 3],
            stale=bool(st >> 6 & 1), failsafe_engaged=bool(st >> 7 & 1), level_now=R.lvl(st >> 8),
            tier_changes=sh.r(0x154) & 0xFFFF)
    return out


def hwmon_all(host, bdf):
    """Every XRT hwmon sensor, zeros included: [(label, file, raw, value, unit)]."""
    labels = host.attrs(bdf, "hwmon/hwmon*/*_label")
    inputs = host.attrs(bdf, "hwmon/hwmon*/*_input")
    out = []
    for k in sorted(labels, key=lambda x: (re.sub(r"\d+", "", x), int(re.sub(r"\D", "", x) or 0))):
        stem = k[:-len("_label")]
        raw = inputs.get(stem + "_input")
        if raw is None or not raw.lstrip("-").isdigit():
            continue
        v = int(raw)
        if stem.startswith("temp"):
            out.append((labels[k], stem + "_input", v, v / 1000.0, "C"))
        elif stem.startswith("in"):
            out.append((labels[k], stem + "_input", v, v / 1000.0, "V"))
        elif stem.startswith("curr"):
            out.append((labels[k], stem + "_input", v, v / 1000.0, "A"))
        elif stem.startswith("power"):
            out.append((labels[k], stem + "_input", v, v / 1e6, "W"))
        else:
            out.append((labels[k], stem + "_input", v, v, "RPM" if stem.startswith("fan") else ""))
    return out


def _hwmon(host, bdf):
    """XRT's sensors (the cmc_emul behind xclmgmt), by label: {label: value in C / V / RPM / W}."""
    out = {}
    labels = host.attrs(bdf, "hwmon/hwmon*/*_label")
    inputs = host.attrs(bdf, "hwmon/hwmon*/*_input")
    for k, lab in labels.items():
        stem = k[:-len("_label")]
        v = inputs.get(stem + "_input")
        if v is None or not v.lstrip("-").isdigit():
            continue
        v = int(v)
        if v == 0:
            continue
        if stem.startswith("temp"):
            out[lab] = v / 1000.0
        elif stem.startswith("in") or stem.startswith("curr"):
            out[lab] = v / 1000.0
        elif stem.startswith("power"):
            out[lab] = v / 1e6
        else:
            out[lab] = v
    return out


def sysmon(sh):
    v = {k: sh.io.rd(R.SYSMON_DRP + 4 * d) & 0xFFFF for k, d in R.SYSMON_REGS.items()}
    adc_ran = not (v["temp_max"] == 0 and v["temp_min"] == 0xFFFF)
    out = dict(temp_c=round(R.code2c(v["temp"] >> 6), 2), temp_max_c=round(R.code2c(v["temp_max"] >> 6), 2),
               temp_min_c=round(R.code2c(v["temp_min"] >> 6), 2), temp_word="0x%04x" % v["temp"],
               adc_converting=adc_ran)
    for rail in ("vccint", "vccaux", "vccbram"):
        out[rail] = dict(v=round(R.sysmon_volts(v[rail]), 4), max=round(R.sysmon_volts(v[rail + "_max"]), 4),
                         min=round(R.sysmon_volts(v[rail + "_min"]), 4))
    return out


def temperature(sh, window=0.35):
    """Every temperature source, each marked LIVE / STALE / UNRELIABLE with the evidence."""
    h = sh.host
    srcs = {}
    s0 = sysmon(sh)
    e7a3 = sh.lay["e7a3"]
    if e7a3:
        a = (sh.r(0x140), sh.r(0x144))
    h.sleep(window)
    s1 = sysmon(sh)
    srcs["sysmon"] = dict(s1, path="mgmt BAR0 0xA0400 (SYSMONE4 TEMP via the wizard's AXI view)",
                          state="LIVE" if s1["adc_converting"] else "STALE",
                          evidence="max/min trackers %s; word %s -> %s in %.2f s" % (
                              "moving (ADC converts)" if s1["adc_converting"] else "never set (ADC never converted)",
                              s0["temp_word"], s1["temp_word"], window))
    if sh.lay["e7"]:
        t = throttle(sh)
        d = dict(temp_c=t["temp_c"], code=t["code"], path="THR_STATUS[25:16] (the throttle's own temperature)")
        if e7a3:
            b = (sh.r(0x140), sh.r(0x144))
            raw, sd, mm = sh.r(0x148), sh.r(0x14C), sh.r(0x150)
            ds = (b[0] - a[0]) & 0xFFFFFFFF
            d.update(samples=b[0], samples_in_window=ds, word_changes=b[1], last_word="0x%04x" % (raw & 0xFFFF),
                     age_ms=round((raw >> 16) * R.TS_S * 1e3, 1), stale_now=bool(sd >> 16 & 1),
                     stale_episodes=sd & 0xFFFF, stale_seen=bool(sd >> 18 & 1),
                     min_c=round(R.code2c(mm & 0x3FF), 2) if (mm & 0x3FF) != 0x3FF else None,
                     max_c=round(R.code2c(mm >> 16 & 0x3FF), 2) if mm >> 16 & 0x3FF else None)
            d["state"] = "STALE" if (ds == 0 or d["stale_now"]) else "LIVE"
            d["evidence"] = "%d samples in %.2f s (sysmon_reader AXI poll, 100 ms), staleness flag %s" % (
                ds, window, "SET" if d["stale_now"] else "clear")
        else:
            d["state"] = "UNRELIABLE"
            d["evidence"] = ("E7a2 feeds the throttle from the wizard's temperature bus, which freezes after one value "
                             "per reset (fixed in E7a3); compare with SYSMON")
        srcs["throttle"] = d
    hw = _hwmon(h, sh.mgmt)
    if "FPGA TEMP" in hw:
        srcs["xrt_cmc"] = dict(temp_c=hw["FPGA TEMP"], path="xclmgmt hwmon 'FPGA TEMP' (cmc_emul)",
                               state="LIVE" if e7a3 else "UNRELIABLE",
                               evidence=("cmc_emul takes sysmon_reader's poll (E7a3)" if e7a3 else
                                         "cmc_emul takes the wizard's temperature bus, which freezes (E6b/E7a2)"))
    ref = srcs["sysmon"]["temp_c"]
    for k, d in srcs.items():
        if k != "sysmon" and d.get("temp_c") is not None and srcs["sysmon"]["state"] == "LIVE":
            d["delta_vs_sysmon_c"] = round(d["temp_c"] - ref, 2)
    return srcs, hw


def pcie(sh):
    h, c = sh.host, sh.card
    out = dict(fpga_link=dict(speed=h.attr(sh.mgmt, "current_link_speed"), width=h.attr(sh.mgmt, "current_link_width")),
               card_slot_link=dict(port=c["switch"], speed=h.attr(c["switch"], "current_link_speed"),
                                   width=h.attr(c["switch"], "current_link_width")))
    aer = {}
    for fn in (sh.mgmt, sh.user):
        if not fn:
            continue
        for kind in ("correctable", "nonfatal", "fatal"):
            v = h.attr(fn, "aer_dev_" + kind)
            if v:
                m = re.search(r"TOTAL_ERR_\w+\s+(\d+)", v)
                if m:
                    aer["%s %s" % (fn, kind)] = int(m.group(1))
    out["aer"] = aer
    out["slot_aer"] = slot_aer(h, c)
    if sh.lay["e7"]:
        lk = sh.r(0x10C)
        out["ddr_health"] = dict(correctable=sh.r(0x100), nonfatal=sh.r(0x104), fatal=sh.r(0x108), ltssm="0x%02x" % (lk & 0x3F),
                                 speed_code=lk >> 8 & 7, width_code=lk >> 16 & 0xF, changes=sh.r(0x110))
    return out


def pr(sh):
    sh.need("e7")
    return dict(count=sh.r(0x120), last_isolation_ms=sh.r(0x124) / 250e3, rp_released=bool(sh.r(0x128) & 1))


def last_events(sh, now_s):
    sh.need("e7")
    out = {}
    for o, n in ((0xE0, "link_down"), (0xE4, "lock_loss"), (0xE8, "ecc_ce"), (0xEC, "ecc_ue"), (0xF0, "fenced"),
                 (0xF4, "pr_load"), (0xF8, "pcie_error"), (0xD4, "throttle_start")):
        t = sh.r(o)
        out[n] = None if t == 0 else dict(at_s=round(t * R.TS_S, 3), ago_s=round(now_s - t * R.TS_S, 1))
    return out


def recorder_info(sh):
    sh.need("e7")
    info = sh.r(0x180)
    aw = info & 0x1F
    return dict(entries=1 << aw, format=info >> 8 & 0xFF, wptr=sh.r(0x184), wraps=sh.r(0x188),
                written=sh.r(0x184) + (sh.r(0x188) << aw))


def read_record(sh, idx, timeout=0.1):
    sh.io.wr(R.STATUS + 0x18C, idx)
    t0 = sh.host.now()
    while not sh.r(0x198) & 1:
        if sh.host.now() - t0 > timeout:
            raise Refused("flight-recorder read of %d never became ready" % idx)
    v = sh.r(0x194) << 32 | sh.r(0x190)
    t, p, ts = R.decode_record(v)
    return dict(index=idx, raw="0x%016x" % v, type=R.REC_TYPES.get(t, "type%d" % t), payload=p, time_s=round(ts, 6),
                detail=R.record_detail(t, p))


def recorder(sh, last=None):
    inf = recorder_info(sh)
    n, wp = inf["entries"], inf["wptr"]
    idxs = list(range(wp, n)) + list(range(wp)) if inf["wraps"] else list(range(wp))
    if last is not None:
        idxs = idxs[-last:] if last > 0 else []
    return inf, [read_record(sh, i) for i in idxs]


def p2p(sh):
    h = sh.host
    out = {}
    if sh.user:
        cfg = h.attr(sh.user, "p2p.*/config") or ""
        m = dict(re.findall(r"(\w+):(\d+)", cfg))
        en = h.attr(sh.user, "p2p.*/p2p_enable")
        out.update(enabled=None if en is None else en.strip() == "1", bar4_size=h.bar_size(sh.user, 4),
                   bar_config={k: int(v) for k, v in m.items()},
                   host_mem_size=h.attr(sh.user, "host_mem_size"))
    if sh.uio:
        pid = sh.uio.rd(R.PEER_BASE)
        if pid == R.PEER_ID:
            u = sh.uio.rd
            nwin = u(R.PEER_BASE + 0x20)
            bases = [u(R.PEER_BASE + lo) | u(R.PEER_BASE + lo + 4) << 32 for lo in (0x08, 0x28, 0x30, 0x38)][:max(1, nwin)]
            out["peer_window"] = dict(enabled=bool(u(R.PEER_BASE + 4) & 1), nwin=nwin,
                                      win_log2=u(R.PEER_BASE + 0x10), bases=["0x%x" % b for b in bases],
                                      writes=u(R.PEER_BASE + 0x14), reads=u(R.PEER_BASE + 0x18),
                                      refused=u(R.PEER_BASE + 0x1C))
        else:
            out["peer_window"] = dict(absent=True, id="0x%08x" % pid)
        aid = sh.uio.rd(R.ALIAS_BASE)
        if aid == R.ALIAS_ID:
            u = sh.uio.rd
            ctl = u(R.ALIAS_BASE + 4)
            out["plram_alias"] = dict(enabled=bool(ctl & 1), in_force=bool(ctl >> 8 & 1),
                                      alias="0x%x" % (u(R.ALIAS_BASE + 8) | u(R.ALIAS_BASE + 0xC) << 32),
                                      win_log2=u(R.ALIAS_BASE + 0x10),
                                      target="0x%x" % (u(R.ALIAS_BASE + 0x18) | u(R.ALIAS_BASE + 0x1C) << 32),
                                      writes=u(R.ALIAS_BASE + 0x20), reads=u(R.ALIAS_BASE + 0x24))
        else:
            out["plram_alias"] = dict(absent=True, id="0x%08x" % aid)
    return out


def clocks(sh):
    h = sh.host
    w = (h.attr(sh.mgmt, "clock_wizard.*/clock_wiz_freqs") or "").split()
    c = (h.attr(sh.mgmt, "clock_freq_counter.*/clock_counter_freqs") or "").split()
    num = lambda l, i: int(l[i]) if len(l) > i and l[i].isdigit() else None
    return dict(data_clk=dict(wizard_mhz=num(w, 0), counter_mhz=num(c, 0)),
                kernel_clk=dict(wizard_mhz=num(w, 1), counter_mhz=num(c, 1)))


def xrt_ready(host, user):
    """XRT's 'Device Ready' for a user PF, from xocl's sysfs 'ready' attribute (what xrt-smi examine reports).
    Deliberately NOT `xrt-smi examine`: measured on x440, one examine adds ~400 correctable AER errors on a card
    behind a marginal link, because it queries every device. Returns 'Yes', 'No' or None (xocl not bound)."""
    if not user:
        return None
    v = host.attr(user, "ready")
    if v is None:
        return None
    try:
        return "Yes" if int(v, 0) == 1 else "No"
    except ValueError:
        return "No"


def slot_aer(host, card):
    """Correctable / non-fatal / fatal AER totals of the card's slot link: the root port and the on-card switch's
    upstream port (the FPGA's own counters miss errors on this link)."""
    out = {}
    for port in (card.get("root_port"), card.get("switch")):
        if not port:
            continue
        for kind in ("correctable", "nonfatal", "fatal"):
            m = re.search(r"TOTAL_ERR_\w+\s+(\d+)", host.attr(port, "aer_dev_" + kind) or "")
            if m:
                out["%s %s" % (port, kind)] = int(m.group(1))
    return out


def xrt_state(sh):
    h = sh.host
    return dict(device_ready=xrt_ready(h, sh.user),
                mgmt_ready=h.attr(sh.mgmt, "ready"), mig_calibration=h.attr(sh.mgmt, "mig_calibration"),
                user_ready=h.attr(sh.user, "ready") if sh.user else None,
                xclbin_uuid=h.attr(sh.user, "xclbinuuid") if sh.user else None,
                xrt_tool=h.which_xrt())


# ----------------------------------------------------------------------------------------------- memory banks
# The loaded xclbin's memory banks, from xocl's sysfs copies of the xclbin sections under the user PF
# (<user>/icap.u.*/mem_topology, ip_layout, connectivity -- XRT 2.19; binary, the axlf structs of xclbin.h).
# PASSIVE: no XRT context, no xrt-smi/xbutil (one examine adds ~400 correctable AER errors on x440's weak link), no
# xclbin load. Reading mem_topology makes xocl do ONE register load of its own: the HMA address translator's range
# register (xocl reports the HOST bank's size from it), the same load xocl does for every memstat / xbutil read.
MEM_TYPES = ("DDR3", "DDR4", "DRAM", "STREAMING", "PREALLOCATED_GLOB", "ARE", "HBM", "BRAM", "URAM",
             "STREAMING_CONNECTION", "HOST", "PS_KERNEL")
NO_MARKER = ("used=0 in the xclbin's mem_topology; the prune hooks embed no marker in the xclbin, so whether the path "
             "was replaced by a zero slave cannot be verified from the host")


def parse_mem_topology(b):
    """struct mem_topology (int32 m_count, then 40-byte mem_data from offset 8). xocl concatenates one copy per slot;
    every copy is returned with its slot number."""
    import struct
    out, pos, slot = [], 0, 0
    while b and pos + 8 <= len(b):
        n = struct.unpack_from("<i", b, pos)[0]
        if n <= 0 or pos + 8 + 40 * n > len(b):
            break
        for i in range(n):
            t, used, size_kb, base = struct.unpack_from("<BB6xQQ", b, pos + 8 + 40 * i)
            tag = b[pos + 8 + 40 * i + 24:pos + 8 + 40 * i + 40].split(b"\0")[0].decode("ascii", "replace")
            out.append(dict(slot=slot, index=i, type=MEM_TYPES[t] if t < len(MEM_TYPES) else "type%d" % t,
                            used=bool(used), size_bytes=size_kb * 1024, base=base, tag=tag))
        pos += 8 + 40 * n
        slot += 1
    return out


def parse_ip_layout(b):
    """struct ip_layout (int32 m_count, then 80-byte ip_data from offset 8): [(type, base, name)]."""
    import struct
    if not b or len(b) < 8:
        return []
    n = struct.unpack_from("<i", b, 0)[0]
    out = []
    for i in range(max(0, n)):
        o = 8 + 80 * i
        if o + 80 > len(b):
            break
        t, _, base = struct.unpack_from("<IIQ", b, o)
        out.append((t, base, b[o + 16:o + 80].split(b"\0")[0].decode("ascii", "replace")))
    return out


def parse_connectivity(b):
    """struct connectivity (int32 m_count, then 12-byte connection from offset 4): [(arg, ip index, mem index)]."""
    import struct
    if not b or len(b) < 4:
        return []
    n = struct.unpack_from("<i", b, 0)[0]
    return [struct.unpack_from("<iii", b, 4 + 12 * i) for i in range(max(0, n)) if 4 + 12 * i + 12 <= len(b)]


def bank_kind(tag, mtype):
    t = tag.upper()
    if mtype == "HOST" or t.startswith("HOST"):
        return "HOST"
    for k, pre in (("DDR", ("BANK", "DDR")), ("PLRAM", ("PLRAM",)), ("PEER", ("PEER",)), ("HBM", ("HBM",))):
        if t.startswith(pre):
            return k
    return "OTHER"


def mem_context(host, card, sh=None, regs=None, memcalib=None):
    """What makes a bank ready, from loads the running shell's map allows (or XRT's sysfs on a shell this tool does not
    map). regs/memcalib: a check snapshot, so check reads nothing twice."""
    user, mgmt = card.get("user"), card.get("mgmt")
    ctx = dict(routev=False, ddr_ready=None, ddr_ready_source=None, rp_released=None, ddr_kept=None,
               platform=sh.vbnv if sh else None)
    if sh is not None and sh.ok:
        rd = (lambda o: regs[o]) if regs is not None else sh.r
        status = rd(0x00)
        mc = memcalib if memcalib is not None else sh.io.rd(R.MEMCALIB)
        if R.is_routev(sh.lay):
            info = rd(0x1C0)
            ctx.update(routev=True, ddr_kept=bool(info & 1), ddr_ready=bool(info >> 1 & 1), rp_released=bool(info >> 4 & 1),
                       ddr_ready_source="RV_INFO 0x%08x: [0] KEPT %d, [1] ddr_ready AND RP released %d, [4] RP released %d"
                                        % (info, info & 1, info >> 1 & 1, info >> 4 & 1))
        else:
            gate = status >> 28 & 1
            mode = R.MODE_NAMES[status >> 29 & 3]
            ctx["ddr_mode"] = mode
            if mode in ("emu", "hybrid"):
                # EMU/HYBRID: the gate is closed and the MIG held BY DESIGN; DDR traffic is served from the 1 MB URAM
                # emulation, and the gate reports calibration to XRT. Ready = XRT's calibration bit.
                ctx.update(ddr_ready=bool(mc & 1),
                           ddr_ready_source="DDR mode %s in force (1 MB URAM emulation, gate closed by design), XRT "
                                            "calibration bit %d" % (mode, mc & 1))
            else:
                ctx.update(ddr_ready=bool(gate and mc & 1),
                           ddr_ready_source="ddr_health gate %s, XRT calibration bit %d" % ("open" if gate else "CLOSED",
                                                                                           mc & 1))
            if sh.lay["e7"]:
                ctx["rp_released"] = bool(rd(0x128) & 1)
    elif mgmt and host.driver(mgmt) == "xclmgmt":
        v = (host.attr(mgmt, "mig_calibration") or "").strip()
        if v in ("0", "1"):
            ctx.update(ddr_ready=v == "1", ddr_ready_source="xclmgmt sysfs mig_calibration=%s" % v)
    if user:
        hm = host.attr(user, "host_mem_size")
        ctx["host_mem_size"] = int(hm) if hm and hm.isdigit() else None
        en = host.attr(user, "p2p.*/p2p_enable")
        ctx["p2p_enabled"] = None if en is None else en.strip() == "1"
    return ctx


def _bank_state(b, ctx):
    """(ready, state, pruned, evidence) of one bank. pruned: True (verified), False (verified kept), None (unknown)."""
    k, used = b["kind"], b["used"]
    if k == "DDR" and ctx.get("routev"):
        if ctx.get("rp_released") is False:
            return (False if used else None), "RP ISOLATED (an xclbin load in progress?): KEPT/PRUNED unknown", None, \
                ctx["ddr_ready_source"]
        if ctx.get("ddr_kept"):
            ev = "the loaded RM carries the MIG (%s)" % ctx["ddr_ready_source"]
            if used:
                return ctx["ddr_ready"], "KEPT, %s" % ("calibrated" if ctx["ddr_ready"] else "NOT READY (ddr_ready=0)"), \
                    False, ev
            return None, "KEPT but unused by any kernel (hook/xclbin mismatch?)", False, ev
        ev = "used=%d in mem_topology and RV_INFO[0]=0: the loaded RM has no MIG (prune_ddr.tcl's filler)" % used
        if used:
            return False, "PRUNED but used by kernels (no MIG behind it)", True, ev
        return None, "PRUNED", True, ev
    if not used:
        if k == "DDR" and ctx.get("platform") == R.VBNV_FP8C:
            return None, "unused (static DDR: never pruned)", False, "the fp8c MIG is in the static region"
        if k in ("PLRAM", "HOST", "PEER"):
            return None, "unused (may be pruned)", None, NO_MARKER
        return None, "unused", None, "used=0 in the xclbin's mem_topology"
    if k == "DDR":
        r = ctx.get("ddr_ready")
        if ctx.get("ddr_mode") in ("emu", "hybrid"):
            return r, "in use, %s" % ("EMULATED (DDR mode %s: only the first 1 MB is backed, by URAM)" % ctx["ddr_mode"]
                                      if r else "NOT READY (DDR mode %s, XRT calibration bit 0)" % ctx["ddr_mode"]), \
                False, ctx["ddr_ready_source"]
        return r, "in use, %s" % {True: "calibrated", False: "NOT CALIBRATED", None: "calibration unknown"}[r], False, \
            ctx.get("ddr_ready_source") or "no calibration source"
    if k == "PLRAM":
        r = ctx.get("rp_released")
        return r, "in use%s" % {True: ", RP released", False: ", NOT READY (RP isolated)", None: ""}[r], False, \
            "PLRAM is on-chip RAM in the dynamic region: ready once the RP is released"
    if k == "HOST":
        hm = ctx.get("host_mem_size")
        r = None if hm is None else hm > 0
        return r, "in use%s" % {True: ", host memory %d MB reserved" % ((hm or 0) >> 20), False:
                                ", NOT READY (no host memory reserved: xrt-smi configure --host-mem)", None: ""}[r], False, \
            "xocl host_mem_size=%s" % hm
    if k == "PEER":
        r = ctx.get("p2p_enabled")
        return r, "in use%s" % {True: ", P2P enabled", False: ", NOT READY (P2P disabled)", None: ""}[r], False, \
            "xocl p2p_enable=%s" % r
    return None, "in use", None, ""


def xrt_memory(host, card, sh=None, ctx=None):
    """Every memory bank of the loaded xclbin: tag, kind, type, base, size, used, the CU arguments connected to it,
    ready, and the prune state (PRUNED only where it is verifiable). See the block comment above for what is read."""
    user = card.get("user")
    if not user:
        return dict(available=False, reason="no user PF (10ee:6a90) on this card")
    drv = host.driver(user)
    if drv != "xocl":
        return dict(available=False, reason="the user PF %s is not bound to xocl (driver %s)" % (user, drv or "none"))
    uuid = (host.attr(user, "xclbinuuid") or "").strip()
    mt = host.raw(user, "icap.*/mem_topology", 1 << 20)
    src = "/sys/bus/pci/devices/%s/icap.*/{mem_topology,ip_layout,connectivity}" % user
    if mt is None:
        return dict(available=False, reason="no icap mem_topology under %s (xocl too old or icap not probed)" % user,
                    xclbin_uuid=uuid or None)
    banks = parse_mem_topology(mt)
    loaded = bool(uuid.replace("-", "").strip("0")) and bool(banks)
    out = dict(available=True, source=src, xclbin_uuid=uuid or None, xclbin_loaded=loaded, banks=[],
               prune_marker=dict(found=False, note="no prune marker reaches the xclbin (prune_unused.tcl writes none; "
                                                   "prune_ddr.tcl writes prune_ddr_marker.json in the build tree only); "
                                                   "route V's DDR state is read from the shell instead (RV_INFO)"))
    if not loaded:
        out["reason"] = "no xclbin loaded" if not banks else "xclbinuuid is %r" % uuid
        return out
    ips = parse_ip_layout(host.raw(user, "icap.*/ip_layout", 1 << 20) or b"")
    conns = parse_connectivity(host.raw(user, "icap.*/connectivity", 1 << 20) or b"")
    ctx = ctx if ctx is not None else mem_context(host, card, sh)
    out["context"] = ctx
    for b in banks:
        b["kind"] = bank_kind(b["tag"], b["type"])
        b["connections"] = ["%s.arg%d" % (ips[ip][2].split(":")[-1], arg) for arg, ip, mi in conns
                            if mi == b["index"] and 0 <= ip < len(ips)] if b["slot"] == 0 else []
        b["ready"], b["state"], b["pruned"], b["evidence"] = _bank_state(b, ctx)
        b["base"] = "0x%x" % b["base"]
        out["banks"].append(b)
    return out


def counters(sh):
    """EVERY counter / register the running shell exposes, built from its OBS_ID register map, plus the SYSMON
    registers, the XRT frequency counters and wizards, the cmc_emul sensors (hwmon), the user-PF PEER / alias
    counters and the kernel's AER totals. One entry per value: region, offset, register, field, name, raw, value,
    unit, description."""
    h = sh.host
    sh.need("any")
    regs = sh.lay["regs"]
    raw = {o: sh.r(o) for o in sorted(regs)}           # sorted: UPTIME_LO (0xB0) is read before UPTIME_HI (0xB4)
    out = []
    fields = {}
    for (o, lsb, w, name, kind, desc) in R.COUNTER_FIELDS:
        if o in regs:
            fields.setdefault(o, []).append((lsb, w, name, kind, desc))
    for o in sorted(regs):
        rname, acc, rdesc = regs[o]
        v = raw[o]
        if o in fields:
            for lsb, w, name, kind, desc in fields[o]:
                f = v >> lsb & ((1 << w) - 1)
                val, unit = R.decode_field(kind, f)
                out.append(dict(region="ddr_health", offset="0x%05x" % (R.STATUS + o), register=rname,
                                field="[%d:%d]" % (lsb + w - 1, lsb) if w < 32 else "", name=name,
                                raw="0x%08x" % v, value=val, unit=unit, description=desc))
        else:
            out.append(dict(region="ddr_health", offset="0x%05x" % (R.STATUS + o), register=rname, field="",
                            name=rname.lower(), raw="0x%08x" % v, value="0x%08x" % v, unit="", description=rdesc))
    cyc = raw[0xB4] << 32 | raw[0xB0]
    hz = raw[0xB8] or 250000000
    out.append(dict(region="ddr_health", offset="0x360b0", register="UPTIME_LO/HI", field="", name="uptime",
                    raw="0x%012x" % cyc, value=round(cyc / hz, 3), unit="s",
                    description="time since configuration (aclk cycles / UPTIME_HZ; never reset)"))
    for k, d in R.SYSMON_REGS.items():
        off = R.SYSMON_DRP + 4 * d
        v = sh.io.rd(off) & 0xFFFF
        if k.startswith("temp"):
            val, unit = round(R.code2c(v >> 6), 2), "C"
        else:
            val, unit = round(R.sysmon_volts(v), 4), "V"
        out.append(dict(region="sysmon", offset="0x%05x" % off, register="DRP 0x%02X" % d, field="[15:6]",
                        name="sysmon_" + k, raw="0x%04x" % v, value=val, unit=unit,
                        description="SYSMONE4 %s%s" % (k.replace("_", " "),
                                                       " (hardware tracker)" if "_m" in k else "")))
    c, m = sh.io.rd(R.MEMCALIB), sh.io.rd(R.MODE_REG)
    out.append(dict(region="memcalib", offset="0x%05x" % R.MEMCALIB, register="GPIO", field="[0]",
                    name="xrt_calibration_bit", raw="0x%08x" % c, value=c & 1, unit="", description="what XRT reads as MIG calibration"))
    out.append(dict(region="memcalib", offset="0x%05x" % R.MODE_REG, register="GPIO2", field="[1:0]",
                    name="ddr_mode_requested", raw="0x%08x" % m, value=R.MODE_NAMES[m & 3], unit="",
                    description="requested DDR mode"))
    for src, attr in (("wizard", "clock_wizard.*/clock_wiz_freqs"), ("counter", "clock_freq_counter.*/clock_counter_freqs")):
        vals = (h.attr(sh.mgmt, attr) or "").split()
        for i, v in enumerate(vals):
            if v.isdigit():
                out.append(dict(region="xrt", offset="sysfs " + attr.split("/")[0], register=attr.split("/")[1],
                                field="[%d]" % i, name="clk%d_%s" % (i, src), raw=v, value=int(v), unit="MHz",
                                description="%s %s (%s)" % (["DATA_CLK", "KERNEL_CLK"][i] if i < 2 else "clock %d" % i,
                                                            "as programmed" if src == "wizard" else "as measured",
                                                            "XRT clock wizard" if src == "wizard" else "XRT frequency counter")))
    for label, f, rv, val, unit in hwmon_all(h, sh.mgmt):
        out.append(dict(region="cmc_sensors", offset="hwmon " + f, register="", field="", name=label,
                        raw=str(rv), value=val, unit=unit,
                        description="XRT sensor from cmc_emul%s" % (" (0 = not provided by this board)" if rv == 0 else "")))
    if sh.uio:
        for base, ident, nm, items in ((R.PEER_BASE, R.PEER_ID, "peer_window",
                                        ((0x14, "writes_forwarded"), (0x18, "reads_forwarded"), (0x1C, "refused"))),
                                       (R.ALIAS_BASE, R.ALIAS_ID, "plram_alias",
                                        ((0x20, "writes_redirected"), (0x24, "reads_redirected"),
                                         (0x28, "writes_outstanding")))):
            if sh.uio.rd(base) != ident:
                continue
            for o, n in items:
                v = sh.uio.rd(base + o)
                out.append(dict(region="user_bar0", offset="0x%06x" % (base + o), register=nm, field="", name="%s_%s" % (nm, n),
                                raw="0x%08x" % v, value=v, unit="", description="%s %s" % (nm, n.replace("_", " "))))
    for fn in (sh.mgmt, sh.user):
        if not fn:
            continue
        for kind in ("correctable", "nonfatal", "fatal"):
            v = h.attr(fn, "aer_dev_" + kind)
            for line in (v or "").splitlines():
                p = line.split()
                if len(p) == 2 and p[1].isdigit():
                    out.append(dict(region="aer", offset="sysfs %s aer_dev_%s" % (fn, kind), register=p[0], field="",
                                    name="aer_%s_%s" % (fn[-1], p[0].lower()), raw=p[1], value=int(p[1]), unit="",
                                    description="kernel AER count, %s PF (%s)" % ("mgmt" if fn == sh.mgmt else "user", kind)))
    return out


def counter_key(e):
    return (e["region"], e["offset"], e["name"])


def counter_deltas(prev, cur):
    """{key: delta} for numeric values that changed."""
    p = {counter_key(e): e["value"] for e in prev}
    out = {}
    for e in cur:
        k = counter_key(e)
        a, b = p.get(k), e["value"]
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(b, bool) and a != b:
            out[k] = round(b - a, 6)
    return out


# ----------------------------------------------------------------------------------------------- simulate-hot
def state_dir():
    """Persistent per-user state (~/.cache/innova2-smi): survives a crash of innova2-smi; never under /tmp."""
    d = os.path.join(os.path.expanduser("~"), ".cache", "innova2-smi")
    os.makedirs(d, exist_ok=True)
    return d


def thr_state_path(sh):
    return os.path.join(state_dir(), "throttle_saved_%s.json" % sh.mgmt.replace(":", "_"))


def sim_hot_plan(sh, tier, margin=20):
    """Threshold registers that make the AUTOMATIC state machine enter `tier` at the live temperature.
    Returns (code, {offset: value}). Thresholds are temperature codes (0.5 C per code)."""
    sh.need("e7")
    code = sh.r(0xC8) >> 16 & 0x3FF
    if code < 2 * margin + 2:
        raise Refused("live temperature code %d is implausibly low; refusing to move thresholds" % code)
    if not sh.lay["e7a3"]:
        if tier != "tier1":
            raise Refused("this shell (%s) has ONE automatic tier; use 'simulate-hot tier1'" % sh.name)
        return code, {0xC4: (code - 2 * margin) << 16 | (code - margin)}
    if tier == "tier1":
        # SET1/REL1 below the code; tier 2 stays where it is (above the code) so the machine stops at tier 1
        t2 = sh.r(0xD8)
        if (t2 & 0x3FF) <= code:
            raise Refused("tier 2 SET (%d) is already at or below the live code %d" % (t2 & 0x3FF, code))
        return code, {0xC4: (code - 2 * margin) << 16 | (code - margin)}
    if tier == "tier2":
        # SET2/REL2 below the code: FULL -> tier 2 directly (a fast rise skips tier 1) or tier 1 -> tier 2
        return code, {0xD8: (code - 2 * margin) << 16 | (code - margin)}
    raise Refused("tier must be tier1 or tier2")


def save_thr_state(sh, saved, extra=None):
    path = thr_state_path(sh)
    if os.path.exists(path):
        raise Refused("%s exists: an earlier simulate-hot was not restored. Run 'innova2-smi -d %s throttle restore' "
                      "first" % (path, sh.mgmt))
    d = dict(mgmt=sh.mgmt, rom_ts=sh.rom_ts, obs_id=sh.obs_id, saved={"0x%x" % o: v for o, v in saved.items()},
             written_at=time.time(), pid=os.getpid())
    d.update(extra or {})
    tmp = path + ".new"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def load_thr_state(sh):
    path = thr_state_path(sh)
    if not os.path.exists(path):
        return path, None
    with open(path) as f:
        d = json.load(f)
    return path, d


def restore_thr(sh, saved):
    """Write the saved throttle registers back and verify every one reads back exactly."""
    throttle_write(sh, saved)
    bad = {o: (v, sh.r(o)) for o, v in saved.items() if sh.r(o) != v}
    if bad:
        raise Refused("throttle restore did not read back: " + ", ".join(
            "0x%x wrote 0x%08x read 0x%08x" % (o, a, b) for o, (a, b) in bad.items()))


# ----------------------------------------------------------------------------------------------- controls (writes)
def set_ddr_mode(sh, want, timeout=5.0):
    """Ported from innova2_ddr_mode: write the mode register, wait for the mode to take effect; restore a v1.2
    gate's previous mode when 'hybrid' maps to OFF there."""
    if sh.lay and R.is_routev(sh.lay) and R.MODE_NAMES[want] in R.ROUTEV_DEFERRED_MODES:
        raise Refused("DDR mode '%s' is not available on route V (deferred): the DDR is built into each xclbin in the "
                      "dynamic region and the static gate runs on/off only; nothing was written" % R.MODE_NAMES[want])
    prev = sh.io.rd(R.MODE_REG) & 3
    sh.io.wr(R.MODE_REG, want)
    if (sh.io.rd(R.MODE_REG) & 3) != want:
        raise Refused("the mode register did not take the write (read back %d)" % (sh.io.rd(R.MODE_REG) & 3))
    t0 = sh.host.now()
    while sh.host.now() - t0 < timeout:
        st = ddr_state(sh)
        if st["in_force"] == R.MODE_NAMES[want]:
            break
        sh.host.sleep(0.05)
    else:
        if want == 3 and st["in_force"] == "off":
            sh.io.wr(R.MODE_REG, prev)
            raise Refused("this shell's calibration gate has no hybrid mode (v1.2: 3 = off); restored '%s'"
                          % R.MODE_NAMES[prev])
        raise Refused("mode not applied within %.0f s (DDR traffic still in flight?)" % timeout)
    if want in (0, 3):
        t0 = sh.host.now()
        while sh.host.now() - t0 < 10 and not ddr_state(sh)["gate_open"]:
            sh.host.sleep(0.1)
    return ddr_state(sh)


def clear_counters(sh):
    sh.need("any")
    sh.io.wr(R.STATUS + R.OBS_ID_OFF, R.OBS_CLEAR)
    return sh.r(R.OBS_ID_OFF) == sh.obs_id


def throttle_regs(sh):
    sh.need("e7")
    regs = [0xC0, 0xC4] + ([0xD8, 0xDC] if sh.lay["e7a3"] else [])
    return {o: sh.r(o) for o in regs}


def throttle_write(sh, regs):
    """Write throttle control registers {offset: value} (offsets relative to ddr_health)."""
    sh.need("e7")
    for o, v in regs.items():
        sh.io.wr(R.STATUS + o, v)


def throttle_change(sh, args):
    """Parse a 'throttle ...' control request into {offset: new value}. Raises Refused on a bad request."""
    sh.need("e7")
    e7a3 = sh.lay["e7a3"]
    c = sh.r(0xC0)
    a = list(args)
    if a == ["defaults"]:
        return dict(R.THR_DEFAULTS[sh.obs_id])
    if len(a) == 2 and a[0] == "force" and a[1] in ("on", "off"):
        return {0xC0: (c & ~2) | (2 if a[1] == "on" else 0)}
    if len(a) == 2 and a[0] == "level" and a[1] in ("2", "4"):
        return {0xC0: (c & ~0xC) | (4 if a[1] == "4" else 0)}
    if len(a) == 2 and a[0] == "enable" and a[1] in ("on", "off"):
        return {0xC0: (c & ~1) | (1 if a[1] == "on" else 0)}
    if len(a) == 2 and a[0] == "gate" and a[1] in ("data", "kernel", "both"):
        return {0xC0: (c & ~0x30) | {"data": 0x10, "kernel": 0x20, "both": 0x30}[a[1]]}
    if len(a) == 3 and a[0] in ("thresholds", "thresholds2"):
        on, off = float(a[1]), float(a[2])
        if off >= on:
            raise Refused("the off temperature must be below the on temperature")
        if a[0] == "thresholds2" and not e7a3:
            raise Refused("tier 2 exists on E7a3 shells only")
        return {(0xD8 if a[0] == "thresholds2" else 0xC4): R.c2code(off) << 16 | R.c2code(on)}
    if len(a) == 3 and a[0] == "tierlevel" and a[1] in ("1", "2") and a[2] in ("2", "4"):
        if not e7a3:
            raise Refused("per-tier levels exist on E7a3 shells only")
        tl, s = sh.r(0xDC), (0 if a[1] == "1" else 8)
        return {0xDC: (tl & ~(3 << s)) | ((1 if a[2] == "4" else 0) << s)}
    if len(a) == 2 and a[0] == "failsafe" and a[1] in ("on", "off"):
        if not e7a3:
            raise Refused("the stale-temperature fail-safe exists on E7a3 shells only")
        tl = sh.r(0xDC)
        return {0xDC: (tl & ~(1 << 16)) | ((1 if a[1] == "on" else 0) << 16)}
    raise Refused("bad throttle request: %s" % " ".join(a))


def jdump(o):
    return json.dumps(o, indent=2, sort_keys=False, default=str)
