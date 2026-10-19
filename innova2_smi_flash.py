# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""innova2-smi setup / flash: the shells installed on this host, what flashing a card needs, and writing a shell into
the User slot.

innova2-smi writes nothing itself. 'flash' runs the tool that already does it for the card's state:
  * a card running an innova2 XRT shell: the shell package's own flash tool (innova2_flash_user,
    innova2_noddr_flash_user), which probes both flash chips, scans Factory, writes with xbflash.qspi and scans
    Factory again;
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


def plan(host, card, sh, s):
    """The commands 'flash' runs, in order, with what each needs. Refused when a precondition fails."""
    how, why = route(host, card, sh)
    cx5 = card["cx5"][0]
    steps, needs = [], []
    if how == "xrt":
        needs += [s["flash_tool"], XBFLASH]
        steps.append(dict(what="write the User slot (%s)" % os.path.basename(s["flash_tool"]),
                          cmd=[s["flash_tool"], "--card", card["mgmt"], "--yes"]))
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
    return dict(route=how, why=why, steps=steps)


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


def shell_rows(rows, shells, problems):
    for p in problems:
        _item(rows, "WARN", "manifest %s" % p)
    good = [s for s in shells if s["installed"] and not s["bad"]]
    for s in shells:
        if s["installed"] and not s["bad"]:
            _item(rows, "ok", "shell %s: images verified (sha256)%s" % (
                shell_label(s), "" if s["source"] == "built-in" else ", manifest %s" % s["source"]))
        elif s["partial"]:
            _item(rows, "WARN", "shell %s: only partly on this host (%d of 5 files, not from the package): not "
                  "flashable" % (shell_label(s), 5 - len(s["bad"])), "to flash it: %s" % install_hint(s))
        elif s["bad"]:
            _item(rows, "FAIL", "shell %s: %s" % (shell_label(s), "; ".join(s["bad"])),
                  "reinstall: %s" % install_hint(s))
        else:
            _item(rows, "MISSING" if not good else "info", "shell %s: not installed" % shell_label(s),
                  install_hint(s))
    return good


def card_rows(host, rows, cs, shells, good):
    nxt = []
    for c in cs:
        sh = H.Shell(host, c)
        if c["state"] == "OUR_SHELL?":
            sh.identify()
            c["state"] = sh.state
        ts, run = running_shell(c, sh, shells)
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


def cmd_setup(host, args, app_ver, cards_fn):
    """Read-only: what this host has and what flashing a card needs, with the command that fixes each gap."""
    shells, problems = known_shells(host)
    for s in shells:
        inspect_shell(host, s)
    rows = []
    xrt_rows(host, rows, shells)
    areg_rows(host, rows, app_ver)
    good = shell_rows(rows, shells, problems)
    cs = cards_fn()
    if not cs:
        _item(rows, "WARN", "no Innova-2 card found (no ConnectX-5 behind an Innova-2 switch)")
    for c in cs:
        if host.driver(c["cx5"][0]) != "mlx5_core":
            _item(rows, "MISSING", "ConnectX-5 %s has no mlx5_core driver" % c["cx5"][0],
                  "install the inbox mlx5 driver (linux-modules-extra) or MLNX/DOCA-OFED")
    nxt = card_rows(host, rows, cs, shells, good)
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
            print("%-*s    -> %s" % (w, "", r["fix"]))
    if not getattr(args, "verbose", False) and any(r["level"] == "info" for r in rows):
        print("(-v: also the shells that are available but not installed)")
    fixes = [r["fix"] for r in bad if r["fix"]]
    if fixes:
        print("\nto do first:")
        for f in dict.fromkeys(fixes):
            print("  " + f)
    elif nxt:
        print("\nnext:")
        for n in dict.fromkeys(nxt):
            print("  " + n)
    return 1 if bad else 0


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
    p = plan(host, card, sh, s)
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
    for i, st in enumerate(p["steps"], 1):
        print("=== %d. %s" % (i, st["what"]))
        rc = host.run_live(st["cmd"])
        if rc:
            print("*** step %d failed (rc %d)%s" % (i, rc, " -- do NOT power cycle into this image; run it again, or "
                                                            "recover from the Flex image" if i == 1 else ""))
            return 1
        if st.get("image_sel"):
            q = host.cx5_query(card["cx5"][0]) or {}
            if q.get("admin") != 0:
                print("*** the ConnectX does not report the User image scheduled after image-sel (%s)" % q)
                return 1
    print("FLASH-DONE: %s holds %s. COLD cycle the host, then check: innova2-smi list (ROM_TS %d), "
          "innova2-smi check, xrt-smi validate -r all" % (head, s["name"], s["rom_ts"]))
    return 0
