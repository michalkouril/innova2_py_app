# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0
"""innova2-smi on macOS: a Host for the XRT macOS port (DriverKit extension org.xrtmac.XrtUser).

What the Linux Host reads from sysfs and /sys/bus/pci/.../resourceN comes from here instead:
  * PCI topology, IDs, class and link state  -- the IOKit device tree (`ioreg -p IODeviceTree -c IOPCIDevice`)
  * BAR registers and PCI configuration space -- `innova2_regio` (macos/innova2_regio.cpp), a small helper that holds
    the dext's BAR mappings open and answers requests on a pipe
  * the feature ROM (VBNV, ROM timestamp)    -- read through the helper at the user function's BAR0 + 0xB0000
READ-ONLY. The dext maps BARs read-only (kIOUserClientMemoryReadOnly), so every register write is refused with a clear
reason; commands that write (recorder, ddr-mode, clear, throttle, test) need a register-write call in the dext.
Not available on macOS: PCIe AER counters, ConnectX-5 access (no mlx5 driver), VPD, hwmon, xocl sysfs reports.
"""
import os
import re
import struct
import subprocess

import innova2_smi_hw as H

REGIO_PATHS = [os.environ.get("INNOVA2_REGIO", ""),
               os.path.join(os.path.dirname(os.path.realpath(__file__)), "innova2_regio"),
               os.path.expanduser("~/innova2_kit/macos/innova2_regio"), "/usr/local/bin/innova2_regio"]
FEATURE_ROM = 0xB0000                 # user BAR0 (xrt_dext_abi.h XRT_DEXT_ROM_OFFSET)
NOT_ON_MACOS = "not available on macOS"
SPEED = {1: "2.5 GT/s PCIe", 2: "5.0 GT/s PCIe", 3: "8.0 GT/s PCIe", 4: "16.0 GT/s PCIe", 5: "32.0 GT/s PCIe"}


def parse_ioreg(text):
    """`ioreg -p IODeviceTree -r -c IOPCIDevice -l -w0` -> [dict(bdf, vendor, device, cls, link_status, link_caps,
    chain=[parent bdfs, root first])]. Nesting comes from the column of each '+-o' line."""
    nodes, stack, cur = [], [], None
    for line in text.splitlines():
        m = re.match(r"^([ |]*)\+-o ", line)
        if m:
            depth = len(m.group(1))
            while stack and stack[-1][0] >= depth:
                stack.pop()
            cur = dict(depth=depth, parent=stack[-1][1] if stack else None, props={})
            nodes.append(cur)
            stack.append((depth, cur))
            continue
        pm = re.match(r'^[ |]*"([^"]+)" = (.*)$', line)
        if cur is not None and pm:
            cur["props"].setdefault(pm.group(1), pm.group(2).strip())
    def le(v):
        h = re.fullmatch(r"<([0-9a-fA-F]*)>", v or "")
        return int.from_bytes(bytes.fromhex(h.group(1)), "little") if h and h.group(1) else None
    out = []
    for n in nodes:
        p = n["props"]
        dbg = re.match(r'"(\d+):(\d+):(\d+)', p.get("pcidebug", ""))
        if not dbg:
            continue
        b, d, f = (int(x) for x in dbg.groups())
        n["bdf"] = "0000:%02x:%02x.%x" % (b, d, f)
        vid, did, cc = le(p.get("vendor-id")), le(p.get("device-id")), le(p.get("class-code"))
        ls = p.get("IOPCIExpressLinkStatus"); lc = p.get("IOPCIExpressLinkCapabilities")
        n["rec"] = dict(bdf=n["bdf"], vendor="0x%04x" % (vid or 0), device="0x%04x" % (did or 0),
                        cls="0x%06x" % (cc or 0), link_status=int(ls) if ls and ls.isdigit() else None,
                        link_caps=int(lc) if lc and lc.isdigit() else None)
    for n in nodes:
        if "rec" not in n:
            continue
        chain, p = [], n["parent"]
        while p is not None:
            if "rec" in p:
                chain.insert(0, p["bdf"])
            p = p["parent"]
        out.append(dict(n["rec"], chain=chain))
    return out


def parse_feature_rom(words):
    """FeatureRomHeader (xclfeatures.h) from 32-bit words -> (vbnv, rom_timestamp) or (None, None)."""
    raw = b"".join(struct.pack("<I", w) for w in words)
    if raw[:4] != b"xlnx":
        return None, None
    ts = struct.unpack_from("<Q", raw, 16)[0]
    vbnv = raw[88:152].split(b"\0")[0].decode(errors="replace")
    return vbnv or None, ts


class Regio:
    """The helper process. One request, one reply line."""

    def __init__(self, path):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def ask(self, req):
        self.p.stdin.write(req + "\n"); self.p.stdin.flush()
        return self.p.stdout.readline().strip()

    def list(self):
        self.p.stdin.write("list\n"); self.p.stdin.flush()
        out = []
        for line in iter(self.p.stdout.readline, ""):
            line = line.strip()
            if line == "end":
                break
            if line.startswith("err"):
                raise H.Refused("innova2_regio: %s" % line[4:])
            f = line.split()
            if f[0] == "fn":
                b, d, fn, ven, dev, sv, sd, ls, lc = (int(x, 16) for x in f[1:10])
                out.append(dict(bdf="0000:%02x:%02x.%x" % (b, d, fn), bus=b, dev=d, func=fn, vendor=ven, device=dev,
                                link_status=ls, link_caps=lc, bars=[int(x, 16) for x in f[10:16]]))
        return out


class MacRegIO:
    """RegIO over the helper: same guard and region rules as Linux; reads only."""

    def __init__(self, host, fn, bar, regions, guard):
        self.host, self.fn, self.bar, self.regions, self.guard = host, fn, bar, regions, guard

    def _check(self, off):
        for base, size, _ in self.regions.values():
            if base <= off < base + size:
                return
        raise H.UnsafeAccess("0x%06x is outside every region innova2-smi maps" % off)

    def rd(self, off):
        self.guard.check_read(off)
        self._check(off)
        r = self.host.regio_helper().ask("r %x %x %x %x %x" % (self.fn["bus"], self.fn["dev"], self.fn["func"], self.bar, off))
        if not r.startswith("ok "):
            raise H.Refused("register read 0x%x on %s failed: %s" % (off, self.fn["bdf"], r))
        return int(r.split()[1], 16)

    # kern_return_t of XRT_DEXT_REG_WRITE -> why the write did not happen
    WRITE_ERRORS = {0xe00002c2: "the XRT macOS dext has no register-write call (XRT_DEXT_REG_WRITE, selector 36) -- "
                                "apply mac_regwrite.patch to the XRT macOS port and rebuild/activate the dext",
                    0xe00002c7: "the XRT macOS dext has no register-write call (XRT_DEXT_REG_WRITE, selector 36) -- "
                                "apply mac_regwrite.patch to the XRT macOS port and rebuild/activate the dext",
                    0xe00002e2: "the dext refused it: not the management function, not an Innova-2 shell, or outside "
                                "its register windows (0x32000-0x32FFF, 0x36000-0x36FFF)",
                    0xe00002d8: "the dext refused it: the card is asleep", 0xe00002c0: "the dext has no device"}

    def wr(self, off, val):
        """One 32-bit write through the dext's XRT_DEXT_REG_WRITE: management function, BAR0, inside innova2-smi's
        register windows. The same guard and region rules as Linux apply first."""
        self.guard.check_write(off)
        self._check(off)
        r = self.host.regio_helper().ask("w %x %x %x %x %x %x" % (self.fn["bus"], self.fn["dev"], self.fn["func"],
                                                                 self.bar, off, val & 0xFFFFFFFF))
        if r.startswith("ok "):
            return
        kr = r.rsplit(" ", 1)[-1]
        why = self.WRITE_ERRORS.get(int(kr, 16), r) if kr.startswith("0x") else r
        raise H.Refused("register write 0x%x refused: %s" % (off, why))


class MacHost(H.Host):
    platform = "macos"

    def __init__(self):
        self._tree = None; self._fns = None; self._rio = None; self._romcache = {}

    # ---- helper and topology
    def regio_helper(self):
        if self._rio is None:
            path = next((p for p in REGIO_PATHS if p and os.access(p, os.X_OK)), None)
            if not path:
                raise H.Refused("innova2_regio not found (build macos/innova2_regio.cpp against the XRT macOS port and "
                                "put it next to innova2-smi, in ~/innova2_kit/macos, or set INNOVA2_REGIO)")
            self._rio = Regio(path)
        return self._rio

    def tree(self):
        if self._tree is None:
            rc, txt = self.run(["ioreg", "-p", "IODeviceTree", "-r", "-c", "IOPCIDevice", "-l", "-w0"], timeout=30)
            self._tree = {d["bdf"]: d for d in parse_ioreg(txt)} if rc == 0 else {}
        return self._tree

    def dext_fns(self):
        if self._fns is None:
            try:
                self._fns = {f["bdf"]: f for f in self.regio_helper().list()}
            except H.Refused:
                self._fns = {}
        return self._fns

    def pci_devices(self):
        return [dict(bdf=d["bdf"], vendor=d["vendor"], device=d["device"], cls=d["cls"], chain=d["chain"])
                for d in self.tree().values()]

    # ---- sysfs emulation
    def _link(self, bdf):
        d = self.tree().get(bdf) or {}
        fn = self.dext_fns().get(bdf) or {}
        ls = d.get("link_status") if d.get("link_status") is not None else fn.get("link_status")
        lc = d.get("link_caps") if d.get("link_caps") is not None else fn.get("link_caps")
        return ls, lc

    def _rom(self, bdf):
        bus_dev = bdf.rsplit(".", 1)[0]
        if bus_dev not in self._romcache:
            fn = self.dext_fns().get(bus_dev + ".1")
            vbnv = ts = None
            if fn:
                r = self.regio_helper().ask("rb %x %x 1 0 %x 64" % (fn["bus"], fn["dev"], FEATURE_ROM))
                if r.startswith("ok "):
                    vbnv, ts = parse_feature_rom([int(x, 16) for x in r.split()[1:]])
            self._romcache[bus_dev] = (vbnv, ts)
        return self._romcache[bus_dev]

    def attr(self, bdf, name):
        d = self.tree().get(bdf)
        if name in ("vendor", "device", "class"):
            return (d or {}).get("cls" if name == "class" else name)
        if name in ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width"):
            ls, lc = self._link(bdf)
            v = ls if name.startswith("current") else lc
            if v is None:
                return None
            return SPEED.get(v & 0xF, "Unknown") if name.endswith("speed") else str((v >> 4) & 0x3F)
        if name in ("rom.*/VBNV", "rom.*/timestamp"):
            vbnv, ts = self._rom(bdf)
            return vbnv if name.endswith("VBNV") else (str(ts) if ts else None)
        if name == "xclbinuuid":                     # the dext's record of the xclbin it loaded
            fn = self._mgmt_of(bdf)
            r = self.regio_helper().ask("uuid %x %x" % (fn["bus"], fn["dev"])) if fn else ""
            if not r.startswith("ok "):
                return None
            u = r.split()[1]
            return "%s-%s-%s-%s-%s" % (u[:8], u[8:12], u[12:16], u[16:20], u[20:])
        if name == "ready":
            return "0x1" if bdf in self.dext_fns() else None
        if name == "mig_calibration" and bdf in self.dext_fns():
            fn = self.dext_fns()[bdf]
            r = self.regio_helper().ask("r %x %x %x 0 %x" % (fn["bus"], fn["dev"], fn["func"], 0x32000))
            return ("%d" % (int(r.split()[1], 16) & 1)) if r.startswith("ok ") else None
        return None                                  # AER, hwmon, xocl/xclmgmt reports: NOT_ON_MACOS

    def attrs(self, bdf, pattern):
        return {}

    SECTIONS = {"icap.*/mem_topology": 0, "icap.*/ip_layout": 1, "icap.*/connectivity": 2}

    def _mgmt_of(self, bdf):
        fn = self.dext_fns().get(bdf.rsplit(".", 1)[0] + ".0")
        return fn

    def raw(self, bdf, name, n=4096):
        """xocl's binary icap.*/{mem_topology,ip_layout,connectivity}: the same axlf sections, which the dext keeps for
        the xclbin it loaded (XRT_DEXT_GET_SECTION, slots 0/1/2)."""
        slot = self.SECTIONS.get(name)
        fn = self._mgmt_of(bdf)
        if slot is None or not fn:
            return None
        r = self.regio_helper().ask("section %x %x %x" % (fn["bus"], fn["dev"], slot))
        return bytes.fromhex(r[3:])[:n] if r.startswith("ok ") else None

    def driver(self, bdf):
        fn = self.dext_fns().get(bdf)
        if fn:                                       # the dext plays xclmgmt (function 0) and xocl (function 1)
            return "xclmgmt" if fn["func"] == 0 else "xocl"
        return None

    def bar_size(self, bdf, n):
        fn = self.dext_fns().get(bdf)
        return fn["bars"][n] if fn and n < len(fn["bars"]) and fn["bars"][n] else None

    def resource(self, bdf, n):
        s = self.bar_size(bdf, n)
        return (1, s) if s else (0, 0)

    def pci_command(self, bdf):
        fn = self.dext_fns().get(bdf)
        if not fn:
            return None
        r = self.regio_helper().ask("cfg %x %x %x 4" % (fn["bus"], fn["dev"], fn["func"]))
        return int(r.split()[1], 16) & 0xFFFF if r.startswith("ok ") else None

    def regio(self, bdf, n, regions, guard):
        fn = self.dext_fns().get(bdf)
        if not fn:
            raise H.Refused("%s: the XRT macOS dext is not attached to this function" % bdf)
        return MacRegIO(self, fn, n, regions, guard)

    def host_uptime(self):
        rc, out = self.run(["sysctl", "-n", "kern.boottime"], timeout=5)
        m = re.search(r"sec = (\d+)", out or "")
        return (self.now() - int(m.group(1))) if m else None

    def boot_id(self):
        rc, out = self.run(["sysctl", "-n", "kern.bootsessionuuid"], timeout=5)
        return out.strip() if rc == 0 and out.strip() else None

    def is_root(self):
        return True                                  # the dext's user client needs no root

    def which_xrt(self):
        for p in ("/opt/xilinx/xrt/bin/xrt-smi",):
            if os.access(p, os.X_OK):
                return p
        return None

    def cx5_query(self, cx5_bdf, transport="auto"):
        return dict(available=False, error="ConnectX-5 access: %s (no mlx5 driver)" % NOT_ON_MACOS)
