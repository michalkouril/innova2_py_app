# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""innova2-smi setup / flash: the shells installed on this host, what flashing a card needs, and writing a shell into
the User slot.

innova2-smi writes nothing itself. 'flash' runs the tool that already does it for the card's state:
  * a card running an innova2 XRT shell: the steps of the shell package's own flash tool (innova2_flash_user,
    innova2_noddr_flash_user) -- probe both flash chips with the package's xbflash.probe, scan Factory, write with
    stock xbflash.qspi, scan Factory again -- run here so the long ones show progress (dots, 'x' for a failure, ETA);
    a package without xbflash.probe has its flash tool run as is;
  * a card running the Flex image: innova2_app's BOPE burn (-b <chip0>.bin,0 -b <chip1>.bin,1, sampled read-back);
then selects the User image through the ConnectX and asks for a cold power cycle. Only the User slot is ever written.

A shell is found from its package's manifest, /opt/xilinx/dsa/<vbnv>/innova2_flash.json (MANIFEST_DOC), or from the
built-in table of released shells below. Every image is checked against its sha256 before anything is written.
"""

import hashlib
import json
import os
import re
import sys
import time

import innova2_smi_hw as H

HERE = os.path.dirname(os.path.realpath(__file__))
APP = os.path.join(HERE, "innova2_app.py")
FW = "/lib/firmware/xilinx/"
MANIFEST_GLOB = "/opt/xilinx/dsa/*/innova2_flash.json"
XBFLASH = "/opt/xilinx/xrt/bin/xbflash.qspi"
IMAGE_ROLES = ("mcs_primary", "mcs_secondary", "bope_primary", "bope_secondary")
APP_RELEASES = "https://github.com/michalkouril/innova2_py_app/releases/download/v%s/"

MANIFEST_DOC = """\
/opt/xilinx/dsa/<vbnv>/innova2_flash.json (format 1), shipped by a shell package:
  {"format": 1, "name": "noddr r1", "package": "innova2-noddr", "version": "202320.3.0-3",
   "vbnv": "innova2_innova2_noddr_202320_3", "rom_ts": 1792486800, "interface_uuid": "9975da59...",
   "flash_tool": "/usr/bin/innova2_noddr_flash_user",
   "images": {"mcs_primary":    {"path": "/lib/firmware/xilinx/<vbnv>_primary.mcs",        "sha256": "..."},
              "mcs_secondary":  {"path": "/lib/firmware/xilinx/<vbnv>_secondary.mcs",      "sha256": "..."},
              "bope_primary":   {"path": "/lib/firmware/xilinx/<vbnv>_bope_primary.bin",   "sha256": "..."},
              "bope_secondary": {"path": "/lib/firmware/xilinx/<vbnv>_bope_secondary.bin", "sha256": "..."}},
   "boot_xsabin": {"path": "/lib/firmware/xilinx/10ee-788f-4351-<ROM_TS, 16 hex digits>.xsabin", "sha256": "..."},
   "download": "<URL of the package>"}
Images and the boot xsabin must be under /lib/firmware/xilinx/, the flash tool under /usr/bin/ or /opt/xilinx/dsa/.
The flash tool takes --card <mgmt BDF> --yes and writes only the User slot. A manifest replaces the built-in entry
with the same vbnv and rom_ts."""


def _builtin(name, package, version, vbnv, rom_ts, uuid, tool, sums, url):
    p = FW + vbnv
    paths = (p + "_primary.mcs", p + "_secondary.mcs", p + "_bope_primary.bin", p + "_bope_secondary.bin")
    return dict(format=1, name=name, package=package, version=version, vbnv=vbnv, rom_ts=rom_ts, interface_uuid=uuid,
                flash_tool=tool, images={r: dict(path=f, sha256=s) for r, f, s in zip(IMAGE_ROLES, paths, sums)},
                boot_xsabin=dict(path=FW + "10ee-788f-4351-%016x.xsabin" % rom_ts, sha256=sums[4]),
                download=url, source="built-in")


# The released shells (sha256 of the files their packages install; the images of noddr 3.0-1 and 3.0-2 are identical).
BUILTIN = (
    _builtin("noddr r1", "innova2-noddr", "202320.3.0-2", "innova2_innova2_noddr_202320_3", 1792486800,
             "9975da591e3d49aab449ab760829ddd0", "/usr/bin/innova2_noddr_flash_user",
             ("f4b264aebf5daf407e45de320a734c91d3f5189e4d1b47d4eaf56c567d177c26",
              "e24ab942f5eb853789c6e5ceff34bac4520586f92ab763e753793de78417bbbc",
              "5568703589056f90ce6184901d0ff2eab7f3abadd2dea4cee6592a8ae1e9a857",
              "6ea44753f2252b67830ceb9163b4eb1d65aa1c12a335b943b6557d58a23d2a1d",
              "4edba4511b1a4a326cbad4ef1a387ae0d207c73ac37b28fff3d635413dc39340"),
             "https://github.com/michalkouril/innova2_noddr_shell/releases/download/noddr-r1-2/"
             "innova2-noddr_202320.3.0-2_all.deb"),
    _builtin("inn2x r10", "innova2-xdma", "202320.4.0-1", "innova2_innova2_xdma_202320_4", 1792512000,
             None, "/usr/bin/innova2_flash_user",
             ("fc9a8125c6828e00cf1966b55808a3726287b4fd9483c4930993095b14f6408f",
              "55209093a4e63281e91d68447c5464596e1200f3338ae4c8d4ee83702b886745",
              "7087ac92f7f0e3fdc3947170204729349b59bb7560243c213b256c2c0baa1445",
              "3e0c83cbd00188451c9d4ae26225275db3ea02a53588749d5cf9660f4c033aa4",
              "41710ea322207cca3c045939274258571a8053ab5ef29e74a7a9efd46e0fe36c"),
             "https://github.com/michalkouril/innova2_shell/releases/download/inn2x-r10/"
             "innova2-xdma_202320.4.0-1_all.deb"),
)


def boot_xsabin_path(rom_ts):
    return FW + "10ee-788f-4351-%016x.xsabin" % rom_ts


# ----------------------------------------------------------------------------------------------- shells
def check_manifest(m):
    """None when the manifest is usable, else why not."""
    if not isinstance(m, dict) or m.get("format") != 1:
        return "not a format-1 manifest"
    for k in ("name", "package", "vbnv", "rom_ts", "flash_tool", "images", "boot_xsabin"):
        if k not in m:
            return "no '%s'" % k
    if not isinstance(m["rom_ts"], int):
        return "rom_ts is not an integer"
    if not re.fullmatch(r"/(usr/bin|opt/xilinx/dsa)/[^\0]+", m["flash_tool"]) or ".." in m["flash_tool"]:
        return "flash_tool %s is not under /usr/bin/ or /opt/xilinx/dsa/" % m["flash_tool"]
    files = [m["images"].get(r) for r in IMAGE_ROLES] + [m["boot_xsabin"]]
    for r, f in zip(IMAGE_ROLES + ("boot_xsabin",), files):
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}",
                                                                                           str(f.get("sha256"))):
            return "%s needs a path and a sha256" % r
        if not f["path"].startswith(FW) or ".." in f["path"]:
            return "%s %s is not under %s" % (r, f["path"], FW)
    return None


def known_shells(host):
    """(shells, problems): the manifests on this host, then each built-in shell no manifest replaces."""
    out, problems = [], []
    for p in host.glob(MANIFEST_GLOB):
        try:
            m = json.loads(host.read_file(p) or "")
        except ValueError as e:
            problems.append("%s: not JSON (%s)" % (p, e))
            continue
        why = check_manifest(m)
        if why:
            problems.append("%s: %s -- ignored" % (p, why))
            continue
        out.append(dict(m, source=p))
    for b in BUILTIN:
        if not any(m["vbnv"] == b["vbnv"] and m["rom_ts"] == b["rom_ts"] for m in out):
            out.append(dict(b))
    return out, problems


def package_version(host, pkg):
    rc, o = host.run(["dpkg-query", "-W", "-f", "${Status} ${Version}", pkg], timeout=10)
    if rc == 0 and o.startswith("install ok installed "):
        return o.split()[-1]
    rc, o = host.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", pkg], timeout=10)
    if rc == 0 and o.strip() and "not installed" not in o:
        return o.strip()
    return None


def inspect_shell(host, s, verify=True):
    """Fill s['installed'] (every image and the boot xsabin present), s['bad'] (files missing or not matching their
    sha256) and s['installed_version'] (from dpkg/rpm, None for a tarball install)."""
    files = [(r, s["images"][r]) for r in IMAGE_ROLES] + [("boot_xsabin", s["boot_xsabin"])]
    present = [host.exists(f["path"]) for _, f in files]
    s["installed"] = all(present)
    s["bad"] = [] if not any(present) else ["%s missing" % f["path"] for (_, f), ok in zip(files, present) if not ok]
    if s["installed"] and verify:
        for _, f in files:
            got = host.sha256(f["path"])
            if got != f["sha256"]:
                s["bad"].append("%s: sha256 %s, expected %s" % (f["path"], (got or "unreadable")[:16] + "...",
                                                                 f["sha256"][:16] + "..."))
    s["tool_ok"] = host.exists(s["flash_tool"])
    if s["installed"] and not s["tool_ok"]:
        s["bad"].append("flash tool %s missing" % s["flash_tool"])
    s["installed_version"] = package_version(host, s["package"]) if any(present) else None
    # some files but no package: copied by hand (a development host), not a damaged install
    s["partial"] = not s["installed"] and any(present) and not s["installed_version"]
    return s


def shell_label(s):
    return "%s (%s %s, %s, ROM_TS %d)" % (s["name"], s["package"], s.get("installed_version") or s["version"],
                                          s["vbnv"], s["rom_ts"])


def select_shell(shells, want):
    ok = [s for s in shells if s["installed"] and not s["bad"]]
    if want:
        w = want.lower()
        hit = [s for s in shells if w in (s["name"].lower(), s["package"].lower(), s["vbnv"].lower())]
        if not hit:
            raise H.Refused("no shell '%s'; known: %s" % (want, ", ".join(s["name"] for s in shells)))
        s = hit[0]
        if not s["installed"]:
            raise H.Refused("%s is not installed on this host: %s" % (s["name"], install_hint(s)))
        if s["bad"]:
            raise H.Refused("%s is damaged (%s): reinstall %s" % (s["name"], "; ".join(s["bad"]), s["package"]))
        return s
    if not ok:
        raise H.Refused("no shell package is installed (or every one is damaged): run 'innova2-smi setup'")
    if len(ok) > 1:
        raise H.Refused("several shells are installed (%s): choose one with --shell <name>" % ", ".join(
            "'%s'" % s["name"] for s in ok))
    return ok[0]


def install_hint(s):
    f = os.path.basename(s["download"]) if s.get("download") else None
    if not f:
        return "install the %s package" % s["package"]
    return "wget %s && sudo apt install ./%s" % (s["download"], f)


# ----------------------------------------------------------------------------------------------- the card
def running_shell(card, sh, shells):
    """(rom_ts or None, the matching known shell or None)."""
    ts = sh.rom_ts
    return ts, next((s for s in shells if ts is not None and s["rom_ts"] == ts), None)


def route(host, card, sh):
    """('xrt' | 'bope', why) for the card's current state, or Refused with what to do instead."""
    cx5 = card["cx5"][0]
    mgmt = card.get("mgmt")
    dev = next((f["device"] for f in card["fpga"] if f["bdf"] == mgmt), None)
    if mgmt and dev == "0x788f":
        if host.driver(mgmt) != "xclmgmt":
            raise H.Refused("card %d runs an XRT shell (%s) but xclmgmt is not bound (driver %s): install or load XRT "
                            "(modprobe xclmgmt) first" % (card["index"], mgmt, host.driver(mgmt) or "none"))
        return "xrt", "the card runs an XRT shell (%s): the shell package's flash tool writes through the shell's " \
                      "own quad-SPI controller" % mgmt
    if card.get("bope"):
        return "bope", "the card runs the Flex image (burn endpoint %s): innova2_app burns through it" % card["bope"]
    sel = "innova2_app -d %s --batch image-sel flex" % cx5
    if card["state"] == "FLEX":
        raise H.Refused("card %d: the ConnectX reports a Flex-slot image but its burn endpoint (15b3:0264) is not on "
                        "PCIe -- cold cycle and try again" % card["index"])
    raise H.Refused("card %d runs neither an XRT shell with a 10ee:788f management PF nor the Flex image (%s): "
                    "select the Flex image ('%s'), cold cycle, then run this again" % (card["index"], card["state"], sel))


def cx5_node(host, card):
    return "/dev/%s_mlx5_fpga_tools" % card["cx5"][0]


# ----------------------------------------------------------------------------------------------- progress
def _mmss(s):
    s = int(round(max(s, 0)))
    return "%d:%02d" % (s // 60, s % 60)


class Progress:
    """Dots for a long step: one character per unit ('.' good, 'x' a failure found), a row of `row` units closed with
    done/total, elapsed time and ETA. label() starts a new row with a name (the write's erase/program phases).
    quiet: nothing is drawn."""

    def __init__(self, total, quiet=False, row=64, out=None, now=time.time, indent="    "):
        self.total, self.quiet, self.row, self.now, self.indent = max(total, 1), quiet, row, now, indent
        self.out = out or sys.stdout
        self.t0, self.done, self.col, self.bad, self.lab = now(), 0, 0, 0, None

    def _w(self, s):
        if not self.quiet:
            self.out.write(s)
            self.out.flush()

    def _close(self):
        if self.col:
            el = self.now() - self.t0
            eta = el / self.done * (self.total - self.done) if self.done else 0
            pad = " " * (self.row - self.col) if self.lab is None else " " * max(0, 12 - self.col)
            self._w("%s %4d/%d  %s elapsed%s\n" % (pad, self.done, self.total, _mmss(el),
                                                     ", ETA %s" % _mmss(eta) if self.done < self.total else ""))
            self.col = 0

    def label(self, text):
        self._close()
        self.lab = text
        self._w("%s%-16s " % (self.indent, text))
        self.col = 0

    def tick(self, ch="."):
        if self.col == 0 and self.lab is None:
            self._w(self.indent)
        self.done += 1
        self.total = max(self.total, self.done)
        self.bad += ch != "."
        self._w(ch)
        self.col += 1
        if self.lab is None and self.col >= self.row:
            self._close()

    def finish(self):
        self._close()
        return self.now() - self.t0


# ----------------------------------------------------------------------------------------------- XRT route, native
FLASH_WORK = "/var/tmp/innova2_flash"      # persistent scratch, as the shell packages' flash tools (never /tmp)
FACTORY_END, SCAN_BLOCK = 0x1000000, 0x10000
# The sampled Factory check (default): every 8th 64 KB block of each chip (one per 512 KB), the last block, and the
# block holding XRT's default dual-QSPI bitstream guard (0x00801000, inside Factory on this card). The ways a User-slot
# write can land in Factory damage it contiguously -- a dropped address bit 24 wraps the whole image onto Factory's
# start, a wrong MCS base address shifts it -- so any misplaced write of 512 KB or more hits a sampled block; the one
# single-block risk, the guard, is sampled by address. --full-factory-check reads all 256 blocks per chip.
SAMPLE_STRIDE, GUARD_ADDR = 8, 0x00801000


def factory_blocks(mode):
    """The block offsets (per chip) the Factory check reads."""
    n = FACTORY_END // SCAN_BLOCK
    if mode == "full":
        return [i * SCAN_BLOCK for i in range(n)]
    return sorted({i * SCAN_BLOCK for i in range(0, n, SAMPLE_STRIDE)} | {(n - 1) * SCAN_BLOCK,
                                                                         GUARD_ADDR & ~(SCAN_BLOCK - 1)})


def tools_dir(s):
    return "/opt/xilinx/dsa/%s/tools" % s["vbnv"]


def probe_tool(s):
    return s.get("probe") or os.path.join(tools_dir(s), "xbflash.probe")


def mcs_records(host, path):
    """The extended-linear-address records in an MCS file: xbflash prints one dot per 20 of them per phase."""
    return host.count_lines(path, ":02000004")


def probe_chips(host, ctx, s):
    """Read-only RDID of both flash chips with the package's xbflash.probe (as its flash tool): both must answer with a
    Micron ID, or a shell without the second chip wired would get half an image."""
    P, S = s["images"]["mcs_primary"]["path"], s["images"]["mcs_secondary"]["path"]
    rc, o = host.run([probe_tool(s), "--primary", P, "--secondary", S, "--card", ctx["mgmt"], "--bar", "0",
                      "--bar-offset", "0x40000", "--force"], timeout=120,
                     env=dict(os.environ, XBFLASH_PROBE_BOTH="1", FLASH_VIA_USER="1"), cwd=tools_dir(s))
    ctx["log"](o)
    for l in o.splitlines():
        if "RDID raw" in l:
            print("    " + l.strip())
    ok = [bool(re.search(r"slave=%d RDID raw = [0-9A-F]{2} 20 BB 20" % i, o)) for i in (0, 1)]
    if not all(ok):
        print("*** both flash chips must answer with a Micron ID (chip0 %s, chip1 %s). A shell without the second "
              "chip wired (drp ... rbdf) must be flashed from the Flex image instead. NOTHING was written." % tuple(
                  "yes" if x else "no" for x in ok))
        return 1
    return 0


def scan_factory(host, ctx, name, ref=None):
    """READ-ONLY sha1 of 64 KB blocks of the Factory region (0x0..0xFFFFFF) of both chips, as qspi_scan.py: all of
    them (ctx['factory'] == 'full') or the sampled set (factory_blocks). ref: an earlier scan of the same blocks; a
    block that differs from it is drawn 'x'. A short read is an 'x' too."""
    blocks = factory_blocks(ctx["factory"])
    n = 2 * len(blocks)
    pr = Progress(n, ctx["quiet"])
    res, fails = [], 0
    try:
        f = host.open_flash(ctx["fdev"])
    except OSError as e:
        print("*** cannot open %s: %s" % (ctx["fdev"], e))
        return 1, None
    try:
        for slave in (0, 1):
            for off in blocks:
                b = f.read_at((slave << 56) | off, SCAN_BLOCK)
                h = hashlib.sha1(b).hexdigest()
                res.append((slave, off, h, len(b)))
                bad = len(b) != SCAN_BLOCK or (ref is not None and ref[len(res) - 1][2] != h)
                fails += bad
                pr.tick("x" if bad else ".")
    except OSError as e:
        pr.finish()
        print("*** reading %s: %s" % (ctx["fdev"], e))
        return 1, None
    finally:
        f.close()
    el = pr.finish()
    host.write_text(os.path.join(ctx["work"], name), "".join("%d 0x%08X %s %d\n" % r for r in res))
    short = sum(1 for r in res if r[3] != SCAN_BLOCK)
    print("    read %d blocks of 64 KB (%s) in %s%s" % (n, "all of Factory" if ctx["factory"] == "full" else
                                                        "sampled: every %dth, the last, the guard's" % SAMPLE_STRIDE,
                                                        _mmss(el), "; %d short reads" % short if short else ""))
    if short:
        return 1, None
    if ref is not None:
        if fails:
            print("*** Factory CHANGED: %d of %d blocks differ (x above) -- stop and inspect before power cycling" % (
                fails, n))
            return 1, res
        print("    Factory unchanged: 0 of %d blocks differ" % n)
    return 0, res


def write_chips(host, ctx, s):
    """xbflash.qspi (stock XRT) through the shell's quad-SPI controller, as the package's flash tool runs it. Its
    'Erasing flash....' / 'Programming flash....' dots are read unbuffered and drawn per phase with an ETA."""
    P, S = s["images"]["mcs_primary"]["path"], s["images"]["mcs_secondary"]["path"]
    per = [mcs_records(host, f) // 20 for f in (P, S)]
    pr = Progress(2 * sum(per), ctx["quiet"])
    st = dict(line="", dots=False, chip=0)

    def chunk(b):
        txt = b.decode("utf-8", "replace")
        ctx["log"](txt)
        for ch in txt:
            if st["dots"] and ch == ".":
                pr.tick()
                continue
            st["dots"] = False
            if ch == "\n":
                st["line"] = ""
                continue
            st["line"] += ch
            m = re.search(r"Preparing flash chip (\d)$", st["line"])
            if m:
                st["chip"] = int(m.group(1))
            for k, lab in (("Erasing flash", "erase"), ("Programming flash", "program")):
                if st["line"].endswith(k):
                    st["dots"] = True
                    pr.label("chip %d %s" % (st["chip"], lab))

    d = os.path.dirname(P)
    rc = host.stream([XBFLASH, "--primary", os.path.basename(P), "--secondary", os.path.basename(S), "--card",
                      ctx["mgmt"], "--bar", "0", "--bar-offset", "0x40000", "--force"], chunk,
                     env=dict(os.environ, FLASH_VIA_USER="1"), cwd=d)
    el = pr.finish()
    errs = [l for l in ctx["logged"]().splitlines() if "ERROR" in l]
    print("    xbflash.qspi rc %d after %s%s" % (rc, _mmss(el), "".join("\n    " + e.strip() for e in errs[-3:])))
    return 1 if rc or errs else 0


def factory_what(mode):
    n = 2 * len(factory_blocks(mode))
    return "%d blocks%s" % (n, "" if mode == "full" else ", sampled (--full-factory-check: all 512)")


def xrt_native_steps(host, card, s, factory="sampled"):
    """The package flash tool's steps, run here so the long ones can show progress. None when the package has no
    xbflash.probe (then its flash tool runs as is)."""
    if not host.exists(probe_tool(s)):
        return None
    mgmt = card["mgmt"]
    inst = host.attr(mgmt, "instance")
    fdev = "/dev/xfpga/flash.m%s.0" % inst
    bo = host.attr(mgmt, "flash.m.*/bar_off")
    if bo is not None and bo != "262144":
        raise H.Refused("card %d: flash controller at BAR0+%s, expected 0x40000 -- not an innova2 shell?" % (
            card["index"], bo))
    if inst is None or not host.exists(fdev):
        raise H.Refused("card %d: no %s for the Factory check (xclmgmt not bound?)" % (card["index"], fdev))
    probe = dict(what="probe both flash chips (read-only, %s)" % os.path.basename(probe_tool(s)), native="probe",
                 cmd=["(%s --card %s, XBFLASH_PROBE_BOTH=1)" % (probe_tool(s), mgmt)])
    write = dict(what="write both chips (xbflash.qspi via the shell's quad-SPI controller)", native="write",
                 cmd=["FLASH_VIA_USER=1 %s --primary %s --secondary %s --card %s --bar 0 --bar-offset 0x40000 --force"
                      % (XBFLASH, os.path.basename(s["images"]["mcs_primary"]["path"]),
                         os.path.basename(s["images"]["mcs_secondary"]["path"]), mgmt)])
    if factory == "skip":
        return [probe, write], fdev
    return [probe,
            dict(what="Factory region scan before writing (%s, both chips, %s)" % (fdev, factory_what(factory)),
                 native="scan0", cmd=["(read-only sha1 of 64 KB blocks of 0x0..0xFFFFFF)"]),
            write,
            dict(what="Factory region scan after writing (every block read must be unchanged)", native="scan1",
                 cmd=["(the same blocks, compared one by one)"])], fdev


def run_native(host, st, ctx, s):
    k = st["native"]
    if k == "probe":
        return probe_chips(host, ctx, s)
    if k == "scan0":
        rc, ctx["before"] = scan_factory(host, ctx, "factory_before.txt")
        return rc
    if k == "write":
        return write_chips(host, ctx, s)
    if k == "scan1":
        return scan_factory(host, ctx, "factory_after.txt", ref=ctx["before"])[0]
    raise AssertionError(k)


def plan(host, card, sh, s, factory="sampled"):
    """The commands 'flash' runs, in order, with what each needs. Refused when a precondition fails."""
    how, why = route(host, card, sh)
    cx5 = card["cx5"][0]
    steps, needs, fdev = [], [], None
    if how == "xrt":
        needs += [XBFLASH] + [s["images"][r]["path"] for r in ("mcs_primary", "mcs_secondary")]
        nat = xrt_native_steps(host, card, s, factory)
        if nat:
            steps, fdev = nat
            why = why.replace("the shell package's flash tool writes", "xbflash.qspi writes") + \
                ", with the package flash tool's checks (%s's steps)" % os.path.basename(s["flash_tool"])
        else:
            needs.append(s["flash_tool"])
            steps.append(dict(what="write the User slot (%s)" % os.path.basename(s["flash_tool"]),
                              cmd=[s["flash_tool"], "--card", card["mgmt"], "--yes"] +
                              (["--skip-factory-check"] if factory == "skip" else []), first_write=True))
    else:
        needs += [APP, os.path.join(HERE, "rawspi.py"), cx5_node(host, card)]
        steps.append(dict(what="burn both chips through the Flex image's burn endpoint (innova2_app)",
                          cmd=[sys.executable, APP, "-d", cx5, "-p", card["bope"], "--yes",
                               "-b", s["images"]["bope_primary"]["path"] + ",0",
                               "-b", s["images"]["bope_secondary"]["path"] + ",1", "--batch", "burn"]))
    q = card.get("cx5_query") or {}
    if q.get("admin") != 0:
        needs += [APP, cx5_node(host, card), "/sys/module/innova2_areg"]
        steps.append(dict(what="schedule the User image (now %s)" % (
            "admin %s" % q["admin"] if "admin" in q else "unknown: the ConnectX query failed"),
            cmd=[sys.executable, APP, "-d", cx5, "--yes", "--batch", "image-sel", "user"], image_sel=True))
    missing = []
    for n in dict.fromkeys(needs):
        if not host.exists(n):
            missing.append(n)
    if missing:
        fix = []
        if cx5_node(host, card) in missing or "/sys/module/innova2_areg" in missing:
            fix.append("sudo modprobe innova2_areg (package innova2-areg-dkms)")
        if XBFLASH in missing:
            fix.append("install XRT (xbflash.qspi)")
        raise H.Refused("card %d (%s route) needs %s%s" % (card["index"], how, ", ".join(missing),
                                                           (" -- " + "; ".join(fix)) if fix else ""))
    return dict(route=how, why=why, steps=steps, fdev=fdev)


# ----------------------------------------------------------------------------------------------- setup
def _item(rows, level, what, fix=None):
    rows.append(dict(level=level, what=what, fix=fix))


def xrt_rows(host, rows, shells):
    doc = next((s for s in shells if s["installed"]), None)
    where = ("/opt/xilinx/dsa/%s/doc/README.md (Requirements) and the xocl patches in /opt/xilinx/dsa/%s/tools/xrt/"
             % (doc["vbnv"], doc["vbnv"])) if doc else "the shell README (Requirements)"
    xs = host.which_xrt()
    if not xs:
        _item(rows, "MISSING", "XRT (no xrt-smi)", "build and install XRT 2.27: see %s" % where)
        return
    v = package_version(host, "xrt")
    _item(rows, "ok", "XRT %s(%s)" % ("%s " % v if v else "", xs))
    _item(rows, "ok" if host.exists(XBFLASH) else "MISSING", "xbflash.qspi (flashing from a running shell)",
          None if host.exists(XBFLASH) else "reinstall XRT")
    for mod in ("xclmgmt", "xocl"):
        rc, _ = host.run(["modinfo", "-F", "version", mod], timeout=10)
        if rc:
            _item(rows, "MISSING", "kernel module %s" % mod, "XRT's DKMS modules did not build for this kernel: "
                  "dkms status; see %s" % where)


def areg_rows(host, rows, app_ver):
    url = APP_RELEASES % app_ver
    if host.exists("/sys/module/innova2_areg"):
        _item(rows, "ok", "innova2_areg loaded (ConnectX FPGA control: image select, Flex burn)")
        return
    rc, _ = host.run(["modinfo", "-F", "version", "innova2_areg"], timeout=10)
    if rc == 0:
        _item(rows, "WARN", "innova2_areg is installed but not loaded", "sudo modprobe innova2_areg")
    else:
        _item(rows, "MISSING", "innova2_areg (image select and the Flex burn need it)",
              "wget %sinnova2-areg-dkms_%s_all.deb && sudo apt install ./innova2-areg-dkms_%s_all.deb" % (
                  url, app_ver, app_ver))


def shell_rows(rows, shells, problems, runners):
    """runners: {rom_ts: [card index, ...]} of the cards running each build. A shell a card runs but whose package is
    missing is worth installing (WARN); one no card runs is only listed with -v. With no usable shell at all, the fix
    is the package of a shell the cards run, else any one of the released shells."""
    for p in problems:
        _item(rows, "WARN", "manifest %s" % p)
    good = [s for s in shells if s["installed"] and not s["bad"]]
    for s in shells:
        by = runners.get(s["rom_ts"])
        runs = "; card %s runs it" % ", ".join(str(i) for i in by) if by else ""
        if s["installed"] and not s["bad"]:
            _item(rows, "ok", "shell %s: images verified (sha256)%s" % (
                shell_label(s), "" if s["source"] == "built-in" else ", manifest %s" % s["source"]))
        elif s["partial"]:
            _item(rows, "WARN", "shell %s: only partly on this host (%d of 5 files, not from the package): not "
                  "flashable%s" % (shell_label(s), 5 - len(s["bad"]), runs), "install its package: %s" % install_hint(s))
        elif s["bad"]:
            _item(rows, "FAIL", "shell %s: %s" % (shell_label(s), "; ".join(s["bad"])),
                  "reinstall: %s" % install_hint(s))
        elif by:
            _item(rows, "WARN", "shell %s: not installed%s" % (shell_label(s), runs),
                  "install its package: %s" % install_hint(s))
        else:
            _item(rows, "info", "shell %s: not installed" % shell_label(s), install_hint(s))
    if not good:
        run = [s for s in shells if runners.get(s["rom_ts"])]
        _item(rows, "MISSING", "no shell package is installed, so no card can be flashed%s" % (
            ": install the package of the shell your cards run" if run else
            ": install the package of the shell you want (any one)"), [install_hint(s) for s in (run or shells)])
        rows[-1]["any"] = not run and len(shells) > 1
    return good


def identify_cards(host, cs, shells):
    """[(card, shell, rom_ts, the known shell it runs or None)]; identifies each card once."""
    out = []
    for c in cs:
        sh = H.Shell(host, c)
        if c["state"] == "OUR_SHELL?":
            sh.identify()
            c["state"] = sh.state
        ts, run = running_shell(c, sh, shells)
        out.append((c, sh, ts, run))
    return out


def card_rows(host, rows, ids, good):
    nxt = []
    cs = [i[0] for i in ids]
    for c, sh, ts, run in ids:
        head = "card %d (ConnectX %s)" % (c["index"], c["cx5"][0])
        now = ("runs %s" % run["name"]) if run else ("runs ROM_TS %d (%s)" % (ts, sh.name or sh.vbnv or "?")
                                                      if ts is not None else "state %s" % c["state"])
        if ts is not None and not run and not host.exists(boot_xsabin_path(ts)):
            _item(rows, "WARN", "%s %s, and no boot xsabin for it is installed (%s): xclmgmt cannot bring the "
                  "card up" % (head, now, os.path.basename(boot_xsabin_path(ts))), "flash an installed shell")
        try:
            how, why = route(host, c, sh)
        except H.Refused as e:
            _item(rows, "WARN", "%s %s; cannot be flashed now: %s" % (head, now, e))
            continue
        dflag = " -d %d" % c["index"] if len(cs) > 1 else ""
        other = [s for s in good if not (run and s["rom_ts"] == run["rom_ts"])]
        if run:     # a released shell already: never suggest replacing it, only say how
            _item(rows, "ok", "%s %s%s; flashable (%s route)%s" % (
                head, now, "" if run["installed"] and not run["bad"] else " (its package is not installed)", how,
                "; to write another shell: %s" % ", ".join("sudo innova2-smi flash%s --shell '%s'" % (dflag, s["name"])
                                                           for s in other) if other else ""))
            continue
        _item(rows, "ok", "%s %s; flashable (%s route)" % (head, now, how))
        for s in good:
            nxt.append("sudo innova2-smi flash%s%s" % (dflag, " --shell '%s'" % s["name"] if len(good) > 1 else ""))
    return nxt


def _fixes(r):
    return r["fix"] if isinstance(r["fix"], list) else [r["fix"]] if r["fix"] else []


def cmd_setup(host, args, app_ver, cards_fn):
    """Read-only: what this host has and what flashing a card needs, with the command that fixes each gap."""
    shells, problems = known_shells(host)
    for s in shells:
        inspect_shell(host, s)
    rows = []
    xrt_rows(host, rows, shells)
    areg_rows(host, rows, app_ver)
    cs = cards_fn()
    ids = identify_cards(host, cs, shells)
    runners = {}
    for c, _, ts, _ in ids:
        if ts is not None:
            runners.setdefault(ts, []).append(c["index"])
    good = shell_rows(rows, shells, problems, runners)
    if not cs:
        _item(rows, "WARN", "no Innova-2 card found (no ConnectX-5 behind an Innova-2 switch)")
    for c in cs:
        if host.driver(c["cx5"][0]) != "mlx5_core":
            _item(rows, "MISSING", "ConnectX-5 %s has no mlx5_core driver" % c["cx5"][0],
                  "install the inbox mlx5 driver (linux-modules-extra) or MLNX/DOCA-OFED")
    nxt = card_rows(host, rows, ids, good)
    bad = [r for r in rows if r["level"] in ("MISSING", "FAIL")]
    if getattr(args, "json", False):
        print(H.jdump(dict(items=rows, shells=shells, next=nxt, ready=not bad)))
        return 1 if bad else 0
    w = 7                                   # len("MISSING")
    for r in rows:
        if r["level"] == "info" and not getattr(args, "verbose", False):
            continue
        print("%-*s  %s" % (w, r["level"], r["what"]))
        if r["fix"] and r["level"] != "ok":
            for f in _fixes(r):
                print("%-*s    -> %s" % (w, "", f))
    if not getattr(args, "verbose", False) and any(r["level"] == "info" for r in rows):
        print("(-v: also the shells that are available but not installed)")
    fixes = [("any one of:", _fixes(r)) if r.get("any") else (f, None) for r in bad for f in
             (_fixes(r)[:1] if r.get("any") else _fixes(r))]
    if fixes:
        print("\nto do first:")
        for f, alt in dict.fromkeys((f, tuple(alt) if alt else None) for f, alt in fixes):
            print("  " + f)
            for a in alt or ():
                print("    " + a)
    elif nxt:
        print("\nnext:")
        for n in dict.fromkeys(nxt):
            print("  " + n)
    return 1 if bad else 0


# ----------------------------------------------------------------------------------------------- shells
def shell_state(s):
    if s["installed"] and not s["bad"]:
        return "flashable"
    if s.get("partial"):
        return "partial"
    if s["bad"]:
        return "damaged"
    return "not installed"


def flash_commands(s, state, cs, running):
    """The flash command for each card that does not already run the shell (-d only when the host has several)."""
    if state != "flashable":
        return []
    todo = [c["index"] for c in cs if c["index"] not in running]
    if not cs:
        return ["sudo innova2-smi flash --shell '%s'" % s["name"]]
    if len(cs) == 1:
        return ["sudo innova2-smi flash --shell '%s'" % s["name"]] if todo else []
    return ["sudo innova2-smi flash -d %d --shell '%s'" % (i, s["name"]) for i in todo]


def cmd_shells(host, args, cards_fn):
    """Every shell this tool knows (manifests on the host, then the built-in released shells): flashable, partial
    (files copied by hand, no package), damaged or not installed, and which cards run it. Exit 0 when at least one is
    flashable."""
    shells, problems = known_shells(host)
    for s in shells:
        inspect_shell(host, s)
    cs = cards_fn()
    ids = identify_cards(host, cs, shells)
    rows = []
    for s in shells:
        st = shell_state(s)
        by = [c["index"] for c, _, ts, _ in ids if ts == s["rom_ts"]]
        rows.append(dict(name=s["name"], package=s["package"], version=s.get("installed_version") or s["version"],
                         installed_version=s.get("installed_version"), vbnv=s["vbnv"], rom_ts=s["rom_ts"], state=st,
                         problems=s["bad"], cards=by, source=s["source"], flash_tool=s["flash_tool"],
                         images={r: s["images"][r]["path"] for r in IMAGE_ROLES},
                         flash_commands=flash_commands(s, st, cs, by),
                         install=None if st == "flashable" else install_hint(s)))
        rows[-1]["flash"] = (rows[-1]["flash_commands"] or [None])[0]
    ok = any(r["state"] == "flashable" for r in rows)
    if getattr(args, "json", False):
        print(H.jdump(rows))
        return 0 if ok else 1
    for p in problems:
        print("note: manifest %s" % p)
    head = ("shell", "package", "version", "ROM_TS", "state", "cards")
    tab = [head] + [(r["name"], r["package"], r["version"] + ("" if r["installed_version"] or r["state"] != "flashable"
                                                               else " (no package)"),
                     str(r["rom_ts"]), r["state"], ",".join(str(i) for i in r["cards"]) or "-") for r in rows]
    w = [max(len(x[i]) for x in tab) for i in range(len(head))]
    for x in tab:
        print("  ".join(v.ljust(w[i]) for i, v in enumerate(x)).rstrip())
    for r in rows:
        if r["state"] == "flashable" and r["flash_commands"]:
            print("%s: %s" % (r["name"], ", ".join(r["flash_commands"])))
        elif r["state"] == "flashable":
            print("%s: card %s already runs it: nothing to flash (flash --force writes it again)" % (
                r["name"], ", ".join(str(i) for i in r["cards"])))
        elif r["state"] != "not installed" or r["cards"] or getattr(args, "verbose", False):
            why = "; ".join(r["problems"]) if r["state"] == "damaged" else ""
            print("%s: %s%s -> %s" % (r["name"], r["state"], " (%s)" % why if why else
                                       " (files copied by hand, no package)" if r["state"] == "partial" else "",
                                       r["install"]))
    if any(r["state"] == "not installed" and not r["cards"] for r in rows) and not getattr(args, "verbose", False):
        print("(-v: also how to install the shells no card runs)")
    return 0 if ok else 1


# ----------------------------------------------------------------------------------------------- flash
def cmd_flash(host, args, cards_fn, confirm):
    """Write an installed shell into the User slot of one card, schedule the User image, ask for a cold cycle."""
    shells, problems = known_shells(host)
    for p in problems:
        print("note: manifest %s" % p)
    for s in shells:
        inspect_shell(host, s)
    s = select_shell(shells, args.shell)
    cs = cards_fn()
    if not cs:
        raise H.Refused("no Innova-2 card found (no ConnectX-5 behind an Innova-2 switch)")
    if args.device is None and len(cs) > 1:
        raise H.Refused("%d cards on this host: choose one with -d (innova2-smi list)" % len(cs))
    card = H.select_card(cs, args.device)
    sh = H.Shell(host, card)
    if card["state"] == "OUR_SHELL?":
        sh.identify()
        card["state"] = sh.state
    ts, run = running_shell(card, sh, shells)
    serial = H.board_id(host, card).get("SN") or "no serial"
    head = "card %d (ConnectX %s, %s)" % (card["index"], card["cx5"][0], serial)
    print("%s runs %s" % (head, run["name"] if run else ("ROM_TS %d (%s)" % (ts, sh.name or sh.vbnv))
                          if ts is not None else "state %s" % card["state"]))
    print("shell to write: %s, images verified (sha256)" % shell_label(s))
    if ts == s["rom_ts"] and not args.force:
        print("the card already runs this shell: nothing to do (--force writes it again)")
        return 0
    factory = "skip" if args.skip_factory_check else "full" if args.full_factory_check else "sampled"
    p = plan(host, card, sh, s, factory)
    if factory == "skip" and p["route"] == "xrt":
        print("note: --skip-factory-check: Factory is not checked; a misplaced write would go unnoticed until the "
              "card falls back")
    print("route: %s" % p["why"])
    for i, st in enumerate(p["steps"], 1):
        print("  %d. %s\n     %s" % (i, st["what"], " ".join(st["cmd"])))
    print("  %d. you: COLD power cycle the host (power off, then on); the FPGA reads flash only at power-on" % (
        len(p["steps"]) + 1))
    if args.dry_run:
        print("dry run: nothing was written")
        return 0
    if not confirm(args, "write %s into the User slot of %s?" % (s["name"], head)):
        print("nothing written")
        return 1
    work = os.path.join(FLASH_WORK, "smi_%s_%s" % ((card.get("mgmt") or card["cx5"][0]).replace(":", "_"),
                                                     time.strftime("%Y%m%d_%H%M%S", time.localtime(host.now()))))
    host.mkdirs(work)
    logp = os.path.join(work, "flash.log")
    ctx = dict(mgmt=card.get("mgmt"), fdev=p["fdev"], work=work, quiet=args.batch, before=None, buf=[],
               factory=factory)
    ctx["log"] = lambda txt: (ctx["buf"].append(txt), host.write_text(logp, txt, append=True))
    ctx["logged"] = lambda: "".join(ctx["buf"])
    print("log and scans: %s" % work)
    wrote = False
    for i, st in enumerate(p["steps"], 1):
        print("=== %d. %s" % (i, st["what"]))
        t0 = host.now()
        if st.get("native"):
            ctx["buf"] = []
            rc = run_native(host, st, ctx, s)
        elif args.batch:
            rc, o = host.run(st["cmd"], timeout=3600)
            ctx["log"](o)
        else:
            rc = host.run_live(st["cmd"])
        writes = st.get("native") == "write" or st.get("first_write") or "burn" in st["cmd"]
        if rc:
            print("*** step %d failed (rc %d)%s" % (i, rc, " -- do NOT power cycle into this image; run it again, or "
                                                            "recover from the Flex image" if writes or wrote else
                                                            " -- nothing was written"))
            return 1
        wrote = wrote or writes
        if args.batch:
            print("    ok (%s)" % _mmss(host.now() - t0))
        if st.get("image_sel"):
            q = host.cx5_query(card["cx5"][0]) or {}
            if q.get("admin") != 0:
                print("*** the ConnectX does not report the User image scheduled after image-sel (%s)" % q)
                return 1
    print("FLASH-DONE: %s holds %s. COLD cycle the host, then check: innova2-smi list (ROM_TS %d), "
          "innova2-smi check, xrt-smi validate -r all" % (head, s["name"], s["rom_ts"]))
    return 0
