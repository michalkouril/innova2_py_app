# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0
"""innova2-smi ddr-assess: measure which DDR configuration a card's memory really supports, and settle on a route-V
per-card DDR profile that a v++ link config can carry.

The measurement runs on the DDR TESTER image (g2x2 static, PCI 10ee:9038) in the card's User slot: every candidate DDR
configuration is a partial bitstream for the tester's reconfigurable partition (a MIG on a chosen set of byte lanes,
speed and CA drive, plus the ddr_bist engine). Each candidate is loaded over PCIe (axi_hwicap, no JTAG), and judged by
a FULL read/write of its whole capacity with the engine's own self-check armed -- never by calibration alone
(2026-09-07: every config of a damaged card calibrated, two survived a read/write).

Rules carried over from the tools this replaces (pr_load.py, bist_run.py), each paid for earlier:
  * ctypes 32-bit stores only (RegIO); hwicap DONE must be 1; the write FIFO is cleared AND released, canary-tested;
    a kick on an empty FIFO deadlocks the IP; the RM is held decoupled + in reset across the load (0x3 -> 0x2 -> 0x0);
    success is read back from the RM's identity registers, never a status bit.
  * BIST STATUS bit 0 is BUSY, bit 1 DONE: wait for BUSY then DONE; MODE 1 runs the size asked for (MODE 0 caps at
    64 MB); the self-check needs CTRL[17] and a non-zero ST_SIZE; SELFFAIL / CFG_REFUSED / NO_CAL / AXI_TIMEOUT are
    INVALID (no verdict), RESPERR and UI_STALL are FAIL even with 0 counted errors.

Settling: the widest configuration that is clean, then the fastest, then the CA drive; it is confirmed by reloading it
and testing again with other seeds. The result is a profile file for the route-V link hook (ddr_profile.tcl):
  set ::innova2_ddr_profile {name <n> lanes {..} variant <dw8_833|e6b750|mwrnd833> ca <RDRV_..>}
and the link-config line that carries it:  [advanced] param=compiler.userPreSysLinkOverlayTcl=<profile file>
"""
import os
import re
import struct
import time

import innova2_smi_hw as H

TESTER_VENDOR, TESTER_DEVICE = "0x10ee", "0x9038"
GPIO, RP, ICAP, DNA = 0x00000, 0x10000, 0x20000, 0x30000
WF, CR, SR, WFV, ASR = 0x100, 0x10C, 0x110, 0x114, 0x11C
B = dict(MAGIC=0x000, VERSION=0x004, WIDTH=0x008, LMAP=0x00C, TCK=0x014, MEMMB=0x024, CAL=0x040,
         CTRL=0x100, STATUS=0x104, BASE_LO=0x108, BASE_HI=0x10C, SIZE_LO=0x110, SIZE_HI=0x114, SEED=0x118,
         REREADS=0x11C, ST_BASE_LO=0x128, ST_BASE_HI=0x12C, ST_SIZE=0x130, ERR_LO=0x140, ERR_HI=0x144,
         EADDR_LO=0x148, EADDR_HI=0x14C)
BUSY, DONE = 1 << 0, 1 << 1
CTRL_SELF = (1 << 2) | (0x3F << 8) | (1 << 16) | (1 << 17) | (1 << 18) | 1      # MODE 1, all patterns, self-check
CTRL_NOSELF = (CTRL_SELF & ~(1 << 17)) | (1 << 19)                              # + ACK_NO_SELFCHECK
ST_SIZE_B = 1 << 20
CAL_DONE = 0x35
FLAGS = ((24, "SELFSKIP"), (6, "SELFFAIL"), (13, "CFG_REFUSED"), (5, "NO_CAL"), (4, "CANARY"), (3, "AXI_TIMEOUT"),
         (25, "RESPERR"), (27, "UI_STALL"))
CA_SUFFIX = {"ca60": "RDRV_60_60", "ca48": "RDRV_48_48", "ca40dflt": "RDRV_40_40"}
CA_PREF = {"RDRV_60_60": 0, "RDRV_48_48": 1, "RDRV_40_40": 2}           # tie-break: the drive measured best on these cards
# lane -> I/O bank (tester_pfm/src/ddr4_pins_9lane.xdc); CA is in bank 66. The MIG needs consecutive banks (Mig 66-99).
LANE_BANK = {0: 66, 1: 68, 2: 68, 3: 67, 4: 67, 5: 67, 6: 67, 7: 68, 8: 68}


def verdict(st, err):
    """bist_run.py's rules: an aborted / refused / uncalibrated run is INVALID (errors=0 is NOT a result)."""
    if st & (1 << 6) or st & (1 << 13) or st & (1 << 5) or st & (1 << 3):
        return "INVALID"
    if st & (1 << 25) or st & (1 << 27):
        return "FAIL"
    return "PASS" if err == 0 else "FAIL"


def flags(st):
    return [n for b, n in FLAGS if st >> b & 1]


def lanes_of(lmap, width):
    """LANEMAP nibble k = the board lane MIG byte k drives (0xF = unused). A 72-bit MIG is the natural order with the
    ECC byte on lane 8 (LANEMAP has eight nibbles; the ninth lane is implicit)."""
    if width == 72:
        return list(range(9))
    return [lmap >> (4 * k) & 0xF for k in range(width // 8)]


def ca_of(name):
    m = re.search(r"_(ca60|ca48|ca40dflt)$", name)
    return CA_SUFFIX[m.group(1)] if m else "RDRV_40_40"                 # tester RMs without the suffix: MIG default


def variant_of(name, tck):
    """The route-V MIG variant (build/mig_variant.tcl) with this tCK and part. RMs built from the dw8 custom-parts CSV
    are named *dw8* or carry a CA suffix (the 2026-10-08 convention); others use the MIG's built-in part."""
    dw8 = "dw8" in name or re.search(r"_(ca60|ca48|ca40dflt)$", name)
    if tck == 750:
        return "e6b750" if dw8 else None
    if tck == 833:
        return "dw8_833" if dw8 else "mwrnd833"
    return None


def profile_problem(lanes, tck, variant):
    """Why a measured configuration cannot become a route-V profile (None = it can)."""
    w = 8 * len(lanes)
    if w == 72:
        return "72-bit: the standard platform DDR (no profile needed)"
    if w not in (8, 16, 32, 64):
        return "%d-bit: the MIG takes 8/16/32/64 without ECC" % w
    banks = sorted({66} | {LANE_BANK[l] for l in lanes})
    if banks[-1] - banks[0] + 1 != len(banks):
        return "lanes %s + CA use banks %s: not consecutive" % (lanes, banks)
    if not variant:
        return "tCK %d ps: no route-V MIG variant (750 = e6b750, 833 = dw8_833 / mwrnd833)" % tck
    return None


class Tester:
    """The tester static's BAR0: axi_gpio, the RP window (ddr_bist), axi_hwicap. Every access is listed in the guard."""

    def __init__(self, host, bdf):
        self.host, self.bdf = host, bdf
        rp = [RP + o for o in B.values()]
        g = H.Guard(reads=[GPIO + 0x00, GPIO + 0x08] + rp + [ICAP + o for o in (CR, SR, WFV, ASR)] + [DNA, DNA + 4, DNA + 8],
                    writes=[GPIO + 0x00] + [RP + B[k] for k in ("CTRL", "BASE_LO", "BASE_HI", "SIZE_LO", "SIZE_HI", "SEED",
                                                                  "REREADS", "ST_BASE_LO", "ST_BASE_HI", "ST_SIZE")]
                    + [ICAP + WF, ICAP + CR])
        self.io = host.regio(bdf, 0, dict(gpio=(GPIO, 0x1000, "rw"), rp=(RP, 0x1000, "rw"), icap=(ICAP, 0x1000, "rw"),
                                          dna=(DNA, 0x1000, "r")), g)

    def rd(self, o):
        return self.io.rd(o)

    def wr(self, o, v):
        self.io.wr(o, v)

    def dna(self):
        return "0x%08X%08X%08X" % (self.rd(DNA + 8), self.rd(DNA + 4), self.rd(DNA))

    def identity(self):
        r = lambda k: self.rd(RP + B[k])
        w = r("WIDTH")
        return dict(magic=r("MAGIC"), version=r("VERSION"), width=w, lanemap=r("LMAP"), tck_ps=r("TCK"),
                    mem_mb=r("MEMMB"), cal=r("CAL"), lanes=lanes_of(r("LMAP"), w) if w in (8, 16, 32, 64, 72) else [])

    # ------------------------------------------------------------------------------------------------- PR load
    def pr_load(self, path):
        d = open(path, "rb").read()
        cfg = d
        if d[0:2] == b"\x00\x09":                    # .bit TLV header: the configuration data is the 'e' section
            i = 2 + 9 + 2
            cfg = None
            while i < len(d):
                k = d[i]; i += 1
                if k == 0x65:
                    n = struct.unpack_from(">I", d, i)[0]; i += 4
                    cfg = d[i:i + n]; break
                ln = struct.unpack_from(">H", d, i)[0]; i += 2 + ln
            if cfg is None:
                raise H.Refused("%s: no 'e' section in the .bit header" % path)
        if cfg.find(b"\xaa\x99\x55\x66") < 0 or len(cfg) % 4:
            raise H.Refused("%s: no sync word, or not whole 32-bit words" % path)
        words = [int.from_bytes(cfg[j:j + 4], "big") for j in range(0, len(cfg), 4)]
        # the write FIFO: clear AND release (CR bit2 is a level), then prove it accepts data
        self.wr(ICAP + CR, 0x4); self.wr(ICAP + CR, 0x0)
        v0 = self.rd(ICAP + WFV)
        for _ in range(4):
            self.wr(ICAP + WF, 0x20000000)
        if self.rd(ICAP + WFV) != v0 - 4:
            raise H.Refused("hwicap write FIFO not accepting data (held in reset?) -- nothing loaded")
        self.wr(ICAP + CR, 0x4); self.wr(ICAP + CR, 0x0)
        if not self.rd(ICAP + SR) & 1:
            raise H.Refused("hwicap DONE=0: the ICAP would discard everything -- nothing loaded")
        before, cal0 = self.identity(), self.rd(GPIO + 0x08) & 1
        self.wr(GPIO + 0x00, 0x3)                        # decouple + RM reset across the load
        if self.rd(GPIO + 0x00) & 0x3 != 0x3:
            raise H.Refused("gpio 0x3 did not take -- nothing loaded")
        n, t0 = 0, self.host.now()
        try:
            while n < len(words):
                vac = self.rd(ICAP + WFV)
                if vac == 0:
                    self._kick()
                    continue
                for _ in range(min(vac, len(words) - n)):
                    self.wr(ICAP + WF, words[n]); n += 1
            self._kick(10.0)
        finally:
            if self.rd(ICAP + CR) & 1:                   # FSM deadlock: only a CR write clears it
                self.wr(ICAP + CR, 0x8); self.wr(ICAP + CR, 0x0)
            self.wr(GPIO + 0x00, 0x2); self.host.sleep(0.05)
            self.wr(GPIO + 0x00, 0x0); self.host.sleep(0.5)
        after = self.identity()
        took = (before["magic"], before["version"], before["tck_ps"]) != (after["magic"], after["version"], after["tck_ps"]) \
            or (cal0 and not self.rd(GPIO + 0x08) & 1)
        return dict(took=bool(took), words=n, seconds=round(self.host.now() - t0, 1), before=before, after=after)

    def _kick(self, tmo=5.0):
        if self.rd(ICAP + WFV) >= 1023:                  # empty: a kick would deadlock the IP
            return
        self.wr(ICAP + CR, 0x1)
        t1 = self.host.now()
        while self.rd(ICAP + CR) & 1:
            if self.host.now() - t1 > tmo:
                raise H.Refused("hwicap CR bit0 stuck -- FSM deadlock")

    def wait_cal(self, timeout=5.0):
        t0 = self.host.now()
        while self.host.now() - t0 < timeout:
            c = self.rd(RP + B["CAL"])
            if c == CAL_DONE:
                return c
            self.host.sleep(0.1)
        return self.rd(RP + B["CAL"])

    # ------------------------------------------------------------------------------------------------- BIST
    def bist(self, base, size, selfcheck=True, seed=0x12345678, timeout=3600):
        for k, v in (("BASE_LO", base), ("BASE_HI", base >> 32), ("SIZE_LO", size), ("SIZE_HI", size >> 32),
                     ("SEED", seed), ("REREADS", 1), ("ST_BASE_LO", 0), ("ST_BASE_HI", 0),
                     ("ST_SIZE", ST_SIZE_B if selfcheck else 0)):
            self.wr(RP + B[k], v & 0xFFFFFFFF)
        self.wr(RP + B["CTRL"], CTRL_SELF if selfcheck else CTRL_NOSELF)
        t0, busy = self.host.now(), False
        while self.host.now() - t0 < timeout:
            s = self.rd(RP + B["STATUS"])
            busy |= bool(s & BUSY)
            if (busy and s & DONE and not s & BUSY) or (not busy and s & DONE and self.host.now() - t0 > 0.5):
                break
            self.host.sleep(0.002)
        else:
            return dict(verdict="INVALID", flags=["TIMEOUT"], errors=None, seconds=timeout)
        st = self.rd(RP + B["STATUS"])
        err = self.rd(RP + B["ERR_LO"]) | self.rd(RP + B["ERR_HI"]) << 32
        return dict(verdict=verdict(st, err), flags=flags(st), errors=err, status="0x%08X" % st,
                    seconds=round(self.host.now() - t0, 2), mb=size >> 20)


# ----------------------------------------------------------------------------------------------------- assessment
def candidates(partial_dir, only=None):
    out = []
    for f in sorted(os.listdir(partial_dir)):
        m = re.fullmatch(r"g2x2_rm_(.+)_partial\.bit", f)
        if m and (not only or re.search(only, m.group(1))):
            out.append(dict(name=m.group(1), path=os.path.join(partial_dir, f), ca=ca_of(m.group(1))))
    return out


def rank_key(r):
    """Widest first, then fastest (smaller tCK), then the preferred CA drive."""
    return (-(r.get("width") or 0), r.get("tck_ps") or 99999, CA_PREF.get(r.get("ca"), 9), r["name"])


def assess_one(t, c, reps=2, reloads=2, seeds=(0x12345678, 0x9E3779B9, 0x2545F491, 0x6A09E667)):
    """Load one candidate and judge it: calibration, then `reps` self-checked full passes; a clean candidate is reloaded
    `reloads` times and re-tested with other seeds. On failure one 64 MB no-self-check pass measures how bad it is."""
    r = dict(name=c["name"], ca=c["ca"], runs=[])
    ld = t.pr_load(c["path"])
    if not ld["took"]:
        return dict(r, result="LOAD-FAILED", why="the RM identity did not change and calibration did not restart")
    idn = ld["after"]
    r.update(width=idn["width"], lanes=idn["lanes"], tck_ps=idn["tck_ps"], mem_mb=idn["mem_mb"])
    r["variant"] = variant_of(c["name"], idn["tck_ps"])
    for load in range(1 + reloads):
        if load:
            if not t.pr_load(c["path"])["took"]:
                return dict(r, result="LOAD-FAILED", why="reload %d did not take" % load)
        cal = t.wait_cal()
        if cal != CAL_DONE:
            return dict(r, result="NO-CAL", why="calibration status 0x%X (want 0x%X)%s" % (
                cal, CAL_DONE, " on reload %d" % load if load else ""))
        for k in range(reps if not load else 1):
            x = t.bist(0, idn["mem_mb"] << 20, True, seeds[(load + k) % len(seeds)])
            x["load"] = load
            r["runs"].append(x)
            if x["verdict"] != "PASS":
                probe = t.bist(0, 64 << 20, False)
                r["probe_64mb"] = dict(verdict=probe["verdict"], errors=probe["errors"], flags=probe["flags"])
                return dict(r, result="FAIL" if x["verdict"] == "FAIL" else "INVALID",
                            why="%s on %s, pass %d: %s errors %s" % (
                                x["verdict"], "reload %d" % load if load else "first load", k, x["errors"],
                                ",".join(x["flags"]) or ""))
    return dict(r, result="CLEAN", why="%d self-checked full passes over %d MB across %d loads, 0 errors" % (
        len(r["runs"]), idn["mem_mb"], 1 + reloads))


def settle(results):
    """The widest CLEAN configuration, then the fastest, then the CA drive, among those a route-V profile can use;
    the first clean 72-bit result short-circuits (the standard platform DDR works)."""
    clean = sorted([r for r in results if r.get("result") == "CLEAN"], key=rank_key)
    for r in clean:
        why = profile_problem(r["lanes"], r["tck_ps"], r.get("variant"))
        if r["width"] == 72:
            return dict(choice=r, standard=True, why=why)
        if why is None:
            return dict(choice=r, standard=False, why=None)
    return dict(choice=None, standard=False,
                why="no clean configuration a route-V profile can use" + (
                    " (clean but unusable: %s)" % "; ".join("%s: %s" % (r["name"], profile_problem(
                        r["lanes"], r["tck_ps"], r.get("variant"))) for r in clean) if clean else ""))


def profile_text(choice, name, evidence):
    lanes = " ".join(str(l) for l in choice["lanes"])
    return "\n".join([
        "# route-V per-card DDR profile, settled by innova2-smi ddr-assess %s" % time.strftime("%Y-%m-%d %H:%M"),
        "# %s" % evidence,
        "# Use from a v++ link config:  [advanced] param=compiler.userPreSysLinkOverlayTcl=<this file>",
        "# (next to the platform's prune_unused.tcl / ddr_profile.tcl; the lane adapter IP is packaged per lane set by",
        "#  build/pkg/ddr_lanes_package.tcl)",
        "set ::innova2_ddr_profile {name %s lanes {%s} variant %s ca %s}" % (name, lanes, choice["variant"], choice["ca"]),
        "source [file join [file dirname [file normalize [info script]]] prune_unused.tcl]", ""])


def default_name(choice, card="card"):
    return "%s_x%d_%s" % (card, choice["width"], "".join(str(l) for l in choice["lanes"]))


# ----------------------------------------------------------------------------------------------------- offline
def parse_run_log(text, name):
    """One ca_lane campaign log (campaign.sh: identity line, 'rep N' self-checked full passes, a no-self ladder) ->
    the same record assess_one returns. Logs carry no reloads: such a result is CLEAN only with reloads unconfirmed."""
    r = dict(name=name, ca=ca_of(name), runs=[], source="log")
    m = re.search(r"WIDTH=0x([0-9A-F]+) LANEMAP=([0-9A-F]+) TCK_PS=0x([0-9A-F]+) MEM_MB=(\d+) CAL_DONE=0x([0-9A-F]+)", text)
    if not m:
        return dict(r, result="NO-DATA", why="no identity line")
    w, lm, tck, mb, cal = int(m[1], 16), int(m[2], 16), int(m[3], 16), int(m[4]), int(m[5], 16)
    r.update(width=w, lanes=lanes_of(lm, w), tck_ps=tck, mem_mb=mb, variant=variant_of(name, tck))
    if cal != CAL_DONE:
        return dict(r, result="NO-CAL", why="calibration status 0x%X" % cal)
    reps = []
    for l in text.splitlines():
        mm = re.match(r"\s*(rep \d+|\d+MB\[\d+\])\s+(PASS|FAIL|INVALID)\s+errors=(\d+)", l)
        if mm:
            reps.append(dict(tag=mm[1], verdict=mm[2], errors=int(mm[3]), selfcheck=mm[1].startswith("rep")))
    r["runs"] = reps
    full = [x for x in reps if x["selfcheck"]]
    if not full:
        return dict(r, result="NO-DATA", why="no self-checked full pass in the log")
    bad = [x for x in reps if x["verdict"] != "PASS"]
    if bad:
        return dict(r, result="FAIL" if any(x["verdict"] == "FAIL" for x in bad) else "INVALID",
                    why="%s %s, %d errors%s" % (bad[0]["tag"], bad[0]["verdict"], bad[0]["errors"],
                                                 " (+%d more failing passes)" % (len(bad) - 1) if len(bad) > 1 else ""))
    return dict(r, result="CLEAN", reloads_confirmed=False,
                why="%d self-checked full + %d ladder passes, 0 errors; one load only" % (
                    len(full), len(reps) - len(full)))


def from_logs(d, only=None):
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".log") and not f.startswith("campaign") and (not only or re.search(only, f[:-4])):
            out.append(parse_run_log(open(os.path.join(d, f), errors="replace").read(), f[:-4]))
    return out
