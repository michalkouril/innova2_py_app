#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""innova2-smi active tests. Each returns a result dict: name, status (PASS / FAIL / SKIP / INCONCLUSIVE), summary,
evidence (lines) and data. Tests that load xclbins or use DDR are confirmed by the CLI (or --yes) before they run;
none of them writes flash (flash-verify only reads it).

Before ANY xclbin is loaded, its feature-ROM timestamp and VBNV are compared with the running shell's: a kernel
linked for another shell wedges the host (the only guard XRT has left on these platforms is that timestamp).
"""

import glob
import json
import os
import re
import statistics
import time

import innova2_smi_hw as H
import innova2_smi_regmap as R

DSA = "/opt/xilinx/dsa"


def result(name, status, summary, evidence=None, **data):
    return dict(name=name, status=status, summary=summary, evidence=list(evidence or []), data=data)


# ----------------------------------------------------------------------------------------------- xclbin safety
def xclbin_check(sh, path):
    """Refuse an xclbin built for another shell. Returns the header."""
    hdr = R.xclbin_header(sh.host.file_head(path, 512) or b"")
    if not hdr:
        raise H.Refused("%s is missing or not an xclbin" % path)
    if hdr["rom_ts"] != sh.rom_ts or hdr["vbnv"] != sh.vbnv:
        raise H.Refused("%s was linked for ROM_TS 0x%08x / %s, the card runs ROM_TS 0x%08x / %s: loading it would "
                        "put another shell's partial bitstream into this RP (host wedge) -- refusing"
                        % (path, hdr["rom_ts"], hdr["vbnv"], sh.rom_ts, sh.vbnv))
    return hdr


def test_dir_candidates(sh):
    """(the validate dir, whether its kernels match the shell, dirs whose kernels all match the shell)."""
    h = sh.host
    d = os.path.join(DSA, sh.vbnv, "test")

    def matches(dd):
        xs = h.glob(os.path.join(dd, "*.xclbin"))
        if not xs or not h.exists(os.path.join(dd, "verify.xclbin")):
            return False
        for x in xs:
            hdr = R.xclbin_header(h.file_head(x, 512) or b"")
            if not hdr or hdr["rom_ts"] != sh.rom_ts:
                return False
        return True
    cands = [c for c in h.glob(d + ".*") if matches(c)]
    return d, matches(d), cands


def select_test_dir(sh, confirm):
    """Make the shared validate dir hold THIS shell's kernels. XRT reads /opt/xilinx/dsa/<VBNV>/test whatever shell
    is on the card, and two shells with one VBNV share it; a foreign kernel wedges the host. Returns the original
    symlink target to restore (None if nothing was changed)."""
    h = sh.host
    d, ok, cands = test_dir_candidates(sh)
    if ok:
        return None
    if not h.readlink(d):
        raise H.Refused("%s holds kernels for another shell and is not a symlink: refusing (stage this shell's "
                        "verify/bandwidth kernels in %s.<tag> and make 'test' a symlink)" % (d, d))
    if len(cands) != 1:
        raise H.Refused("%s points at kernels for another shell, and %d sibling dirs (%s) hold kernels for ROM_TS "
                        "0x%08x: cannot choose" % (d, len(cands), ", ".join(cands) or "none", sh.rom_ts))
    old = h.readlink(d)
    new = os.path.basename(cands[0])
    if not confirm("switch %s -> %s (from %s) for this run, and back afterwards?" % (d, new, old)):
        raise H.Refused("test dir switch declined")
    h.relink(d, new)
    return old


def restore_test_dir(sh, old):
    if old:
        sh.host.relink(os.path.join(DSA, sh.vbnv, "test"), old)


# ----------------------------------------------------------------------------------------------- helpers
def ecc_snap(sh):
    e = H.ecc(sh)
    return dict(ce=e["ce"], ue=e["ue_polls"], fenced=sh.r(0xA0), linkdn=sh.r(0x10) & 0xFFFF)


def ecc_delta(a, b):
    return {k: b[k] - a[k] for k in a}


def run_xrt(sh, args, timeout=900):
    exe = sh.host.which_xrt()
    if not exe:
        raise H.Refused("XRT (xrt-smi / xbutil) not found under /opt/xilinx/xrt/bin")
    rc, out = sh.host.run([exe] + args, timeout=timeout)
    return rc, re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out)


def parse_validate(out):
    tests, cur = [], None
    for line in out.splitlines():
        m = re.match(r"\s*Test \d+ \[[^\]]+\]\s*:\s*(\S+)", line)
        if m:
            cur = dict(test=m.group(1), status=None, details=[])
            tests.append(cur)
            continue
        m = re.search(r"Test Status\s*:\s*\[(\w+)\]", line)
        if m and cur:
            cur["status"] = m.group(1)
        elif cur and ("Details" in line or "bandwidth" in line.lower() or "Throughput" in line or "Error" in line):
            cur["details"].append(line.strip())
    return tests


# ----------------------------------------------------------------------------------------------- read-only tests
def t_temp_live(sh, args):
    """Every temperature source is live: E7a3 sample counter moving and fresh, SYSMON ADC converting, sources agree.
    Read-only."""
    name = "temp-live"
    sh.need("any")
    win = float(getattr(args, "window", 3.0) or 3.0)
    ev, fails = [], []
    sm0 = H.sysmon(sh)
    a = (sh.r(0x140), sh.r(0x144)) if sh.lay["e7a3"] else None
    sh.host.sleep(win)
    sm1 = H.sysmon(sh)
    ev.append("SYSMON %.2f C (max %.2f, min %.2f), word %s -> %s" % (sm1["temp_c"], sm1["temp_max_c"], sm1["temp_min_c"],
                                                                   sm0["temp_word"], sm1["temp_word"]))
    if not sm1["adc_converting"]:
        fails.append("SYSMON max/min trackers never set: the ADC never converted")
    srcs, hw = H.temperature(sh, window=0.2)
    if sh.lay["e7a3"]:
        b = (sh.r(0x140), sh.r(0x144))
        ds, dc = (b[0] - a[0]) & 0xFFFFFFFF, (b[1] - a[1]) & 0xFFFFFFFF
        exp = win / 0.1
        ev.append("TEMP_SAMPLES +%d in %.1f s (expected ~%d at 100 ms), TEMP_CHANGES +%d" % (ds, win, exp, dc))
        if ds < exp * 0.5:
            fails.append("the throttle's temperature feed sampled %d times in %.1f s (poller stalled)" % (ds, win))
        th = srcs["throttle"]
        ev.append("throttle temperature %.2f C, stale %s, age %.1f ms" % (th["temp_c"], th["stale_now"], th["age_ms"]))
        if th["stale_now"]:
            fails.append("the throttle temperature is STALE")
        if abs(th["temp_c"] - sm1["temp_c"]) > 3:
            fails.append("throttle %.2f C vs SYSMON %.2f C differ by more than 3 C" % (th["temp_c"], sm1["temp_c"]))
    elif sh.lay["e7"]:
        th = srcs["throttle"]
        ev.append("throttle temperature %.2f C (wizard temperature bus: known to freeze on E7a2)" % th["temp_c"])
        if abs(th["temp_c"] - sm1["temp_c"]) > 3:
            fails.append("throttle %.2f C vs SYSMON %.2f C: the frozen temperature bus (known E7a2 defect)"
                         % (th["temp_c"], sm1["temp_c"]))
    if "xrt_cmc" in srcs:
        x = srcs["xrt_cmc"]["temp_c"]
        ev.append("XRT cmc 'FPGA TEMP' %.1f C" % x)
        if abs(x - sm1["temp_c"]) > 4:
            fails.append("XRT cmc %.1f C vs SYSMON %.2f C differ by more than 4 C%s" % (
                x, sm1["temp_c"], "" if sh.lay["e7a3"] else " (frozen temperature bus, known on E6b/E7a2)"))
    status = "FAIL" if fails else "PASS"
    return result(name, status, "; ".join(fails) or "all temperature sources live and in agreement", ev + fails,
                  sources=srcs)


def t_ecc_live(sh, args):
    """Prove the ECC counters count. Without a marker: the poller is alive (polls advancing) -> INCONCLUSIVE (a live
    poller is necessary, not sufficient). With '--arm': fill bank0 and write a marker; after a WARM reboot (the MIG
    is reset by the link-down while the DRAM is not refreshed), the next run reads the old data back without writing
    and expects ECC events (measured: 120-226 CE + 2-6 UE per warm reboot on E6b-2400dw8)."""
    name = "ecc-live"
    sh.need("any")
    h = sh.host
    ev = []
    e0 = H.ecc(sh)
    h.sleep(1.0)
    e1 = H.ecc(sh)
    dp = (e1["polls"] - e0["polls"]) & 0xFFFFFFFF
    st = H.ddr_state(sh)
    ev.append("ECC on %s, poller alive %s, polls +%d in 1 s, CE %d, UE polls %d; DDR %s (gate %s)" % (
        e1["ecc_on"], e1["poller_alive"], dp, e1["ce"], e1["ue_polls"], st["in_force"], st["gate_open"]))
    calibrated = st["in_force"] in ("on", "hybrid") and st["gate_open"]
    if not calibrated:
        return result(name, "SKIP", "DDR is not in use (mode %s, gate %s)" % (st["in_force"], st["gate_open"]), ev)
    if not e1["ecc_on"] or not e1["poller_alive"] or dp < 100:
        return result(name, "FAIL", "the ECC poller is not running (%d polls in 1 s, expected ~1000)" % dp, ev)
    path = os.path.join(H.state_dir(), "ecc_arm_%s.json" % sh.mgmt.replace(":", "_"))
    if getattr(args, "arm", False):
        from innova2_smi_xrt import tile_bank
        xclbin = xclbin_for(sh, args)
        n = tile_bank(sh, xclbin, "fill", args.chunk_mb, args.size_mb, ev)
        up = H.uptime(sh)
        m = dict(mgmt=sh.mgmt, rom_ts=sh.rom_ts, boot_id=h.boot_id(), uptime_s=up["seconds"],
                 linkdn=sh.r(0x10) & 0xFFFF, ce=e1["ce"], ue=e1["ue_polls"], xclbin=xclbin, chunks=n,
                 chunk_mb=args.chunk_mb, size_mb=args.size_mb)
        with open(path, "w") as f:
            json.dump(m, f, indent=2)
        return result(name, "INCONCLUSIVE", "armed: %d MB of bank0 filled; WARM-reboot the host (not a power cycle), "
                      "then run 'innova2-smi test ecc-live' again" % (n * args.chunk_mb), ev, marker=path)
    if not os.path.exists(path):
        return result(name, "INCONCLUSIVE", "poller alive; counting not proven (run 'test ecc-live --arm', warm "
                      "reboot, run again)", ev)
    with open(path) as f:
        m = json.load(f)
    up = H.uptime(sh)
    ld = sh.r(0x10) & 0xFFFF
    ev.append("marker: boot %s, FPGA uptime %.0f s, link-downs %d; now boot %s, uptime %.0f s, link-downs %d" % (
        m["boot_id"][:8], m["uptime_s"], m["linkdn"], (h.boot_id() or "?")[:8], up["seconds"], ld))
    if m["rom_ts"] != sh.rom_ts or up["seconds"] < m["uptime_s"]:
        os.remove(path)
        return result(name, "INCONCLUSIVE", "the FPGA was reconfigured since --arm (cold cycle or reflash): DRAM "
                      "contents are gone; marker removed, re-arm", ev)
    if m["boot_id"] == h.boot_id() or ld <= m["linkdn"]:
        return result(name, "INCONCLUSIVE", "no warm reboot (link-down + MIG reset) since --arm; reboot the host and "
                      "run again", ev)
    from innova2_smi_xrt import tile_bank
    before = H.ecc(sh)
    mism = tile_bank(sh, m["xclbin"], "check", m["chunk_mb"], m["size_mb"], ev, count_only=True)
    after = H.ecc(sh)
    dce, due = after["ce"] - before["ce"], after["ue_polls"] - before["ue_polls"]
    ev.append("stale read of the pre-reboot fill: %d mismatching words; ECC CE +%d, UE polls +%d" % (mism, dce, due))
    os.remove(path)
    if dce + due > 0:
        return result(name, "PASS", "ECC counters move on stale DRAM data (CE +%d, UE +%d)" % (dce, due), ev)
    if mism:
        return result(name, "FAIL", "%d words read back wrong but the ECC counters did not move" % mism, ev)
    return result(name, "INCONCLUSIVE", "the DRAM kept its data across the reboot (0 mismatches, 0 ECC events)", ev)


# ----------------------------------------------------------------------------------------------- XRT tests
def xclbin_for(sh, args):
    """--xclbin, else this shell's verify.xclbin from a validate dir whose kernels match the shell."""
    if getattr(args, "xclbin", None):
        xclbin_check(sh, args.xclbin)
        return args.xclbin
    d, ok, cands = test_dir_candidates(sh)
    for dd in ([d] if ok else []) + cands:
        p = os.path.join(dd, "verify.xclbin")
        xclbin_check(sh, p)
        return p
    raise H.Refused("no verify.xclbin for ROM_TS 0x%08x under %s*; pass --xclbin" % (sh.rom_ts, d))


def t_validate(sh, args, confirm):
    name = "validate"
    sh.need("any")
    runs = getattr(args, "run", None) or "all"
    e0 = ecc_snap(sh)
    old = select_test_dir(sh, confirm)
    try:
        d, ok, _ = test_dir_candidates(sh)
        if not ok:
            raise H.Refused("the validate dir still does not hold this shell's kernels")
        rc, out = run_xrt(sh, ["validate", "-d", sh.user, "-r", runs, "--verbose"], timeout=1800)
    finally:
        restore_test_dir(sh, old)
    tests = parse_validate(out)
    e1 = ecc_snap(sh)
    dl = ecc_delta(e0, e1)
    ev = ["%-14s %s %s" % (t["test"], t["status"], " | ".join(t["details"][:3])) for t in tests]
    ev.append("ECC/fence deltas: %s" % dl)
    failed = [t["test"] for t in tests if t["status"] == "FAILED"]
    passed = [t["test"] for t in tests if t["status"] == "PASSED"]
    if failed or not passed or dl["ue"] > 0:
        return result(name, "FAIL", "failed: %s%s" % (", ".join(failed) or "no test passed",
                                                      "; UE +%d" % dl["ue"] if dl["ue"] else ""), ev, rc=rc,
                      tests=tests, output_tail=out[-2000:])
    return result(name, "PASS", "%d passed (%s), %d skipped" % (len(passed), ", ".join(passed),
                                                                  len([t for t in tests if t["status"] == "SKIPPED"])),
                  ev, tests=tests)


def t_p2p(sh, args, confirm):
    name = "p2p"
    sh.need("any")
    p = H.p2p(sh)
    if not p.get("enabled"):
        return result(name, "SKIP", "P2P is disabled (enable: xrt-smi configure -d %s --p2p enable, then a warm "
                      "reboot)" % sh.user, ["p2p: %s" % p])
    args.run = "p2p"
    r = t_validate(sh, args, confirm)
    r["name"] = name
    r["evidence"].insert(0, "P2P BAR %s bytes, config %s" % (p.get("bar4_size"), p.get("bar_config")))
    return r


def t_ddr_soak(sh, args, confirm):
    name = "ddr-soak"
    sh.need("any")
    st = H.ddr_state(sh)
    if st["in_force"] != "on" or not st["gate_open"]:
        return result(name, "SKIP", "DDR not in mode ON and calibrated (%s, gate %s)" % (st["in_force"], st["gate_open"]))
    from innova2_smi_xrt import tile_bank
    xclbin = xclbin_for(sh, args)
    ev, rows = [], []
    e0 = ecc_snap(sh)
    tile_bank(sh, xclbin, "fill", args.chunk_mb, args.size_mb, ev)
    e1 = ecc_snap(sh)
    ev.append("fill: ECC/fence delta %s" % ecc_delta(e0, e1))
    total_mis = 0
    for p in range(args.passes):
        a = ecc_snap(sh)
        mis = tile_bank(sh, xclbin, "check", args.chunk_mb, args.size_mb, ev, quiet=True)
        b = ecc_snap(sh)
        d = ecc_delta(a, b)
        total_mis += mis
        rows.append(dict(pass_=p + 1, mismatches=mis, **d))
        ev.append("pass %d: %d mismatching words, CE +%d, UE polls +%d, fenced +%d" % (p + 1, mis, d["ce"], d["ue"],
                                                                                      d["fenced"]))
    dt = ecc_delta(e1, ecc_snap(sh))
    bad = total_mis or dt["ue"] or dt["fenced"] or dt["ce"] > args.max_ce
    return result(name, "FAIL" if bad else "PASS", "%d passes: %d mismatching words, CE +%d (max %d), UE +%d, "
                  "fenced +%d" % (args.passes, total_mis, dt["ce"], args.max_ce, dt["ue"], dt["fenced"]), ev, passes=rows)


def t_pr_stress(sh, args, confirm):
    name = "pr-stress"
    sh.need("any")
    h = sh.host
    if getattr(args, "xclbins", None):
        xs = args.xclbins
    else:
        d, ok, cands = test_dir_candidates(sh)
        base = d if ok else (cands[0] if cands else None)
        if not base:
            raise H.Refused("no validate kernels for this shell: pass --xclbins a.xclbin,b.xclbin")
        xs = [p for p in (os.path.join(base, "verify.xclbin"), os.path.join(base, "bandwidth.xclbin")) if h.exists(p)]
    for x in xs:
        xclbin_check(sh, x)
    saved = H.throttle_regs(sh) if sh.lay["e7"] else None
    ev, fails = [], 0
    e0 = ecc_snap(sh)
    pr0 = sh.r(0x120) if sh.lay["e7"] else None
    try:
        for i in range(args.loads):
            if saved is not None and getattr(args, "throttled", True):
                if i >= args.loads // 2:
                    lvl = 0 if i % 2 else 4
                    H.throttle_write(sh, {0xC0: (saved[0xC0] & ~0xE) | 2 | lvl | 0x31})
                else:
                    H.throttle_write(sh, {0xC0: saved[0xC0] & ~2})
            x = xs[i % len(xs)]
            t0 = h.now()
            rc, out = run_xrt(sh, ["program", "-d", sh.user, "-u", x], timeout=300)
            dt = h.now() - t0
            ready = H.xrt_ready(h, sh.user)
            fw = [h.attr(fn, "firewall.*/status") for fn in (sh.mgmt, sh.user)]
            st = H.ddr_state(sh)
            ok = rc == 0 and "succeeded" in out.lower() and ready == "Yes" and all(f in (None, "0") for f in fw) \
                and (st["in_force"] != "on" or st["gate_open"])
            fails += not ok
            ev.append("load %2d %-18s %s %.1f s ready=%s firewall=%s ddr_gate=%s%s" % (
                i + 1, os.path.basename(x), "ok" if ok else "FAILED rc=%d" % rc, dt, ready, fw, st["gate_open"],
                " throttled" if (saved is not None and i >= args.loads // 2) else ""))
    finally:
        if saved is not None:
            H.restore_thr(sh, saved)
    e1 = ecc_snap(sh)
    d = ecc_delta(e0, e1)
    extra = ""
    if pr0 is not None:
        dpr = sh.r(0x120) - pr0
        extra = ", PR_COUNT +%d" % dpr
        if dpr < args.loads - fails:
            ev.append("PR_COUNT rose by %d for %d successful loads (XRT skips a load of the uuid already on the card)"
                      % (dpr, args.loads - fails))
    status = "FAIL" if fails or d["ue"] else "PASS"
    return result(name, status, "%d/%d loads ok%s, ECC delta %s" % (args.loads - fails, args.loads, extra, d), ev)


# ----------------------------------------------------------------------------------------------- throttle walk
def thr_steps(sh, code):
    """(label, {reg: value} to write, expected tier name or None, expected divisor). Ported from the E7a3 hardware
    test (tiers.sh) and the E7a2 force/threshold sweep."""
    d = R.THR_DEFAULTS[sh.obs_id]
    hx = lambda set_, rel: (rel & 0x3FF) << 16 | (set_ & 0x3FF)
    base = {0xC0: 0x31}
    if sh.lay["e7a3"]:
        return [
            ("FULL (defaults)", {**d, **base}, "FULL", 1),
            ("-> tier 1: SET1 below the live code", {0xC4: hx(code - 30, code - 50)}, "tier 1", 2),
            ("-> tier 2: SET2 below the live code", {0xD8: hx(code - 20, code - 40)}, "tier 2", 4),
            ("tier 2 hysteresis: SET2 above, REL2 below (stays)", {0xD8: hx(code + 30, code - 20)}, "tier 2", 4),
            ("-> tier 1: REL2 above the code (never straight to FULL)", {0xD8: d[0xD8]}, "tier 1", 2),
            ("tier 1 hysteresis: SET1 above, REL1 below (stays)", {0xC4: hx(code + 30, code - 50)}, "tier 1", 2),
            ("-> FULL: REL1 above the code", {0xC4: d[0xC4]}, "FULL", 1),
            ("FULL -> tier 2 directly (fast rise skips tier 1)", {0xC4: hx(code - 50, code - 70),
                                                                  0xD8: hx(code - 30, code - 50)}, "tier 2", 4),
            ("tier levels swapped in tier 2 (tier 2 = 1/2)", {0xDC: 0x00010001}, "tier 2", 2),
            ("-> tier 1 at level 1/4", {0xD8: d[0xD8]}, "tier 1", 4),
            ("defaults -> FULL", {**d, **base}, "FULL", 1),
            ("FORCE level 1/2 (bypasses the tiers)", {0xC0: 0x33}, None, 2),
            ("FORCE level 1/4", {0xC0: 0x37}, None, 4),
            ("FORCE off", {0xC0: 0x31}, "FULL", 1),
        ]
    return [
        ("FULL (defaults)", {**d, **base}, None, 1),
        ("FORCE level 1/2", {0xC0: 0x33}, None, 2),
        ("FORCE level 1/4", {0xC0: 0x37}, None, 4),
        ("FORCE off", {0xC0: 0x31}, None, 1),
        ("threshold below the code (alarm, level 1/2)", {0xC4: hx(code - 10, code - 20)}, None, 2),
        ("level 1/4", {0xC0: 0x35}, None, 4),
        ("enable off", {0xC0: 0x34}, None, 1),
        ("enable on", {0xC0: 0x35}, None, 4),
        ("defaults", {**d, **base}, None, 1),
    ]


def t_throttle(sh, args, confirm, measure=None):
    """Walk the throttle through every state by moving thresholds around the live temperature (and FORCE), measuring
    the delivered DATA_CLK with a lat_probe kernel each time; restore the exact previous settings at the end.
    `measure(sh) -> MHz` is injectable (unit tests); by default it runs lat_probe through pyxrt."""
    name = "throttle"
    sh.need("e7")
    if measure is None:
        from innova2_smi_xrt import LatProbe
        if not getattr(args, "xclbin", None):
            raise H.Refused("the throttle test needs an xclbin with a lat_probe CU on DATA_CLK (the build's "
                            "regress_plram_prog.xclbin, CU lp_ddr): pass --xclbin")
        xclbin_check(sh, args.xclbin)
        lp = LatProbe(sh, args.xclbin, args.cu)
        measure = lp.mhz
    tol = args.tolerance
    saved = H.throttle_regs(sh)
    ev, fails = [], 0
    code = sh.r(0xC8) >> 16 & 0x3FF
    tc0 = sh.r(0x154) if sh.lay["e7a3"] else None
    try:
        H.throttle_write(sh, {**R.THR_DEFAULTS[sh.obs_id], 0xC0: 0x31})
        sh.host.sleep(0.5)
        base = measure(sh)
        ev.append("live code %d (%.2f C); full DATA_CLK measured %.2f MHz" % (code, R.code2c(code), base))
        for label, regs, tier, div in thr_steps(sh, code):
            tcb = sh.r(0x154) if sh.lay["e7a3"] else None
            H.throttle_write(sh, regs)
            sh.host.sleep(0.5)
            mhz = measure(sh)
            st = sh.r(0xC8)
            got_tier = R.TIER_NAMES[st >> 4 & 3] if sh.lay["e7a3"] else None
            ratio = mhz / base if base else 0
            ok = abs(ratio * div - 1) <= tol and (tier is None or got_tier == tier) and \
                bool(st >> 2 & 1) == (div > 1)
            if label.startswith("FULL -> tier 2") and sh.lay["e7a3"]:
                steps = (sh.r(0x154) - tcb) & 0xFFFF
                ok = ok and steps == 1
                label += " [%d tier change%s]" % (steps, "" if steps == 1 else "s")
            fails += not ok
            ev.append("%-4s %-58s tier %-6s %7.2f MHz = 1/%.2f (want 1/%d) THR_STATUS 0x%08x" % (
                "ok" if ok else "FAIL", label, got_tier or "-", mhz, 1 / ratio if ratio else 0, div, st))
    finally:
        H.restore_thr(sh, saved)
    ev.append("restored THR registers: %s" % {"0x%x" % o: "0x%08x" % v for o, v in saved.items()})
    if sh.lay["e7a3"]:
        ev.append("tier changes during the test: %d" % ((sh.r(0x154) - tc0) & 0xFFFF))
    return result(name, "FAIL" if fails else "PASS", "%d step(s) off; tolerance %.0f%%" % (fails, tol * 100), ev)


# ----------------------------------------------------------------------------------------------- flash-verify
def mcs_image(path):
    """Intel-HEX MCS -> (flash start address, bytes). xbflash writes a non-golden MCS 0x1000 above its declared
    addresses; an MCS whose first ELA record is 0 ('golden' prefix) is written verbatim."""
    data, ela, first_ela = {}, 0, None
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln.startswith(":") or len(ln) < 11:
                continue
            n, addr, typ = int(ln[1:3], 16), int(ln[3:7], 16), int(ln[7:9], 16)
            if typ == 4:
                ela = int(ln[9:13], 16) << 16
                if first_ela is None:
                    first_ela = ela
            elif typ == 0 and n:
                data[ela + addr] = bytes.fromhex(ln[9:9 + 2 * n])
    if not data:
        raise H.Refused("%s has no data records" % path)
    shift = 0 if first_ela == 0 else 0x1000
    lo = min(data)
    hi = max(a + len(b) for a, b in data.items())
    img = bytearray(b"\xff" * (hi - lo))
    for a, b in data.items():
        img[a - lo:a - lo + len(b)] = b
    return lo + shift, bytes(img)


def t_flash_verify(sh, args, reader=None):
    """Read the User slot of both flash chips back through the shell's own quad-SPI controller (rawspi, mgmt BAR0
    + 0x40000) and compare with the given images. READ-ONLY. Exact length; a short read or any SPI stall FAILS
    (a stall shifts the rest of a dump -- re-run)."""
    name = "flash-verify"
    sh.need("any")
    imgs = []
    if args.mcs_prefix:
        for chip, suf in ((0, "_primary.mcs"), (1, "_secondary.mcs")):
            addr, b = mcs_image(args.mcs_prefix + suf)
            imgs.append((chip, args.mcs_prefix + suf, addr, b))
    else:
        if not (args.primary and args.secondary):
            raise H.Refused("give --primary and --secondary (.bin per chip) or --mcs-prefix")
        for chip, p in ((0, args.primary), (1, args.secondary)):
            with open(p, "rb") as f:
                b = f.read()
            imgs.append((chip, p, args.offset, b))
    for chip, p, addr, b in imgs:
        if not b:
            raise H.Refused("%s is empty" % p)
        if addr < 0x01000000 or addr + len(b) > 0x03000000:
            raise H.Refused("%s spans 0x%08x..0x%08x: outside the User slot (0x01000000..0x02FFFFFF)" % (
                p, addr, addr + len(b)))
    if reader is None:
        from innova2_smi_xrt import SpiReader
        rc, out = sh.host.run(["pgrep", "-a", "xbflash"], timeout=5)
        if rc == 0 and out.strip():
            raise H.Refused("xbflash is running (%s): the flash controller is busy" % out.strip())
        reader = SpiReader(sh)
    ev, fails = [], 0
    ids = reader.rdid()
    ev.append("RDID chip0 %s chip1 %s" % (ids[0], ids[1]))
    if not all("20bb20" in i for i in ids):
        return result(name, "FAIL", "both flash chips must answer the Micron JEDEC ID (20 BB 20)", ev)
    for chip, p, addr, want in imgs:
        t0 = time.time()
        got, stalls = reader.read(chip, addr, len(want))
        msg = "chip%d @0x%08x: asked 0x%x bytes, got 0x%x, %d SPI stall(s), %.0f s" % (
            chip, addr, len(want), len(got), stalls, time.time() - t0)
        if stalls:
            fails += 1
            ev.append(msg + " -- a stall can shift the dump: re-run")
            continue
        if len(got) != len(want):
            fails += 1
            ev.append(msg + " -- SHORT read")
            continue
        diff = [i for i in range(0, len(want), 1) if got[i] != want[i]] if got != want else []
        if diff:
            fails += 1
            ev.append(msg + " -- %d bytes differ from %s, first at +0x%x" % (len(diff), os.path.basename(p), diff[0]))
        else:
            ev.append(msg + " -- identical to %s" % os.path.basename(p))
    return result(name, "FAIL" if fails else "PASS", "%d of %d chip images match" % (len(imgs) - fails, len(imgs)), ev)


# ----------------------------------------------------------------------------------------------- registry
TESTS = {
    "temp-live": dict(fn=t_temp_live, writes=False, xrt=False, doc="all temperature sources live and agreeing (read-only)"),
    "ecc-live": dict(fn=t_ecc_live, writes=False, xrt=False, doc="ECC counters count (poller check; --arm + warm reboot "
                                                                 "for proof)"),
    "validate": dict(fn=t_validate, writes=True, xrt=False, doc="xrt-smi validate with the right kernels in the "
                                                                "shared test dir"),
    "p2p": dict(fn=t_p2p, writes=True, xrt=False, doc="xrt-smi validate -r p2p (P2P must be enabled)"),
    "ddr-soak": dict(fn=t_ddr_soak, writes=True, xrt=True, doc="fill bank0 + N check passes with per-pass ECC"),
    "pr-stress": dict(fn=t_pr_stress, writes=True, xrt=False, doc="N xclbin loads (half throttled on E7), health "
                                                                  "after each"),
    "throttle": dict(fn=t_throttle, writes=True, xrt=True, doc="tier walk by threshold moves, measured DATA_CLK, "
                                                               "restore"),
    "flash-verify": dict(fn=t_flash_verify, writes=False, xrt=False, doc="rawspi read-back of the User slot vs images "
                                                                         "(read-only)"),
}
ALL_ORDER = ["temp-live", "ecc-live", "validate", "p2p", "ddr-soak", "pr-stress", "throttle", "flash-verify"]
