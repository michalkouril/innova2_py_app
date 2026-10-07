#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""innova2-smi check: the passive "concerns only" health check.

STRICTLY PASSIVE. It may run at any time, also while kernels or DMA are running:
  * MMIO: 32-bit LOADS only, through the running shell's OBS_ID register map (an unknown shell is refused before
    anything but OBS_ID is read). Never a store -- not even the flight recorder's index register, which is why the
    "events since boot" items use the last-event timestamp registers instead of the recorder.
  * sysfs reads, the xrt-smi device table and file headers (the boot xsabin, the validate kernels). No xclbin load,
    no DMA, no DDR access, no counter clear, no throttle write, no reset, no `xbutil validate`, no XRT context.
  * at most two snapshots (--interval S apart), each ~100 register reads.

Output: one line per item needing attention (WARN / FAIL) with its evidence and a hint, or "OK: no concerns".
INFO items (known benign) are shown with -v and are always in --json. Exit code 0 OK, 1 WARN, 2 FAIL.

Every threshold and every "known benign" rule is in RULES below.
"""

import os
import time

import innova2_smi_hw as H
import innova2_smi_regmap as R

SEV_ORDER = {"OK": 0, "INFO": 0, "WARN": 1, "FAIL": 2}

# ----------------------------------------------------------------------------------------------- the rules table
# id -> severity, threshold (if any), hint. Tune here; `innova2-smi check --rules` prints this table.
RULES = {
    # card / FPGA state (discovery from the ConnectX-5 side)
    "fpga.missing":           dict(sev="FAIL", hint="no FPGA function behind the card's switch: the FPGA did not "
                                                    "configure or enumerate; check the ConnectX's image status"),
    "fpga.flex":              dict(sev="WARN", hint="the card runs the Flex/Factory image, not a User shell: select "
                                                    "the User image (innova2_app --batch image-sel user) and cold-cycle"),
    "fpga.other":             dict(sev="WARN", hint="the FPGA runs a design that is not an Innova-2 XRT shell"),
    "cx5.inconsistent":       dict(sev="FAIL", hint="the ConnectX's view of the FPGA disagrees with what is on the bus"),
    "cx5.health":             dict(sev="WARN", hint="the ConnectX-5 itself needs attention"),
    "cx5.no_node":            dict(sev="INFO", hint="load mlx5_fpga_tools (OFED 5.2) or innova2_areg (this package) "
                                                    "for the image query: innova2-smi list --load-module --yes"),
    "cx5.query":              dict(sev="INFO", hint="the ConnectX-5 FPGA query could not run"),
    # identity / platform
    "identity.refused":       dict(sev="FAIL", hint="a safety check failed before any register access past it; "
                                                    "nothing further was read"),
    "identity.old":           dict(sev="WARN", hint="pre-fp8c shell: innova2-smi reports its identity only"),
    "identity.board":         dict(sev="WARN", hint="board PN/EC not validated for this DDR controller; report the "
                                                    "DDR4 chip marking"),
    "identity.vpd_empty":     dict(sev="WARN", hint="the ConnectX-5 VPD is empty (no PN/EC/SN): the board cannot be "
                                                    "checked against the validated list; identify it by the FPGA DNA "
                                                    "(JTAG) and report the DDR4 chip marking"),
    "identity.obs_mismatch":  dict(sev="WARN", hint="ROM_TS names a build with another OBS_ID: flash/platform mix-up?"),
    "identity.uuid_mismatch": dict(sev="WARN", hint="the interface uuid XRT read from the installed boot xsabin is "
                                                    "not the one this tool lists for the flashed build: platform/flash "
                                                    "mix-up, or a stale table entry"),
    "platform.no_xsabin":     dict(sev="FAIL", hint="no boot xsabin installed for this card's ROM_TS: install the "
                                                    "platform that matches the flashed shell"),
    "platform.test_dir":      dict(sev="WARN", hint="the validate kernels belong to another shell (validate would wedge "
                                                    "the host); use 'innova2-smi test validate', which switches dirs"),
    "xrt.not_ready":          dict(sev="FAIL", hint="XRT Device Ready is not Yes: check dmesg for xclmgmt/xocl errors"),
    "xrt.firewall":           dict(sev="FAIL", hint="an AXI firewall tripped: the RP or a kernel hung an AXI access; "
                                                    "a reset or reboot is needed"),
    "xrt.mailbox":            dict(sev="WARN", hint="the mgmt<->user mailbox is not connected"),
    # DDR
    "ddr.not_calibrated":     dict(sev="FAIL", hint="DDR fenced: XRT refuses DDR and PLRAM xclbins; cold-cycle"),
    "ddr.mode":               dict(sev="WARN", expect="on", hint="DDR mode is not the expected one (--expect-mode)"),
    "ddr.ue":                 dict(sev="FAIL", hint="uncorrectable ECC errors on data written after the clean point "
                                                    "(configuration or 'innova2-smi ecc-baseline'): DMA reads of that "
                                                    "data fail (SLVERR); soak the card ('innova2-smi test ddr-soak')"),
    "ddr.ue_stale":           dict(sev="WARN", hint="likely stale-data reads after a warm reboot: the link-down MIG "
                                                    "reset loses the DDR contents, so reading buffers filled before the "
                                                    "reboot gives UEs -- expected after a warm reboot. The counters are "
                                                    "not host-clearable: re-fill, then 'innova2-smi ecc-baseline' (check "
                                                    "then FAILs only on new UEs), or cold-boot to clear. The shell "
                                                    "cannot see host fills, so this stays a WARN, never OK"),
    "ddr.ue_unclassified":    dict(sev="WARN", hint="uncorrectable ECC errors with warm reboots since the clean point: "
                                                    "stale-data reads after a reboot or real errors -- the shell cannot "
                                                    "tell ('innova2-smi recorder' shows each ECC_UE / LINK_DOWN record "
                                                    "time); re-fill and 'innova2-smi ecc-baseline', or cold-boot"),
    "ddr.ue_baselined":       dict(sev="INFO", hint="uncorrectable ECC errors counted before the ecc-baseline; none since"),
    "ddr.ce":                 dict(sev="WARN", hint="corrected ECC errors: watch the rate; a soak tells more (after a "
                                                    "warm reboot, reads of pre-reboot data count here too: "
                                                    "'innova2-smi ecc-baseline' after a re-fill counts only new ones)"),
    "ddr.ce_baselined":       dict(sev="INFO", hint="corrected ECC errors counted before the ecc-baseline; none since"),
    "ddr.baseline_ignored":   dict(sev="INFO", hint="the ecc-baseline file is not for this configuration of this card "
                                                    "(cold boot / reflash since): judged on the totals; take a new one"),
    "ddr.baseline":           dict(sev="INFO", hint="ECC judged against the host-side ecc-baseline "
                                                    "(~/.cache/innova2-smi/ecc_baseline_<mgmt BDF>.json)"),
    "ddr.fenced":             dict(sev="WARN", hint="something accessed DDR while it was fenced (often one write in "
                                                    "the link-down window of a warm reboot)"),
    "ddr.link_down_rising":   dict(sev="WARN", hint="the PCIe link dropped during the check interval"),
    "ddr.link_down_count":    dict(sev="INFO", hint="link-downs since configuration (each warm reboot adds one)"),
    "ddr.rate_mismatch":      dict(sev="WARN", threshold=1.0, hint="the MIG ui_clk measured from the ECC poll count "
                                                                   "differs from this build's table by more than this "
                                                                   "(%): a wrong table entry or a mis-built shell"),
    "ddr.rate_unverified":    dict(sev="INFO", hint="the DDR rate could not be compared with a table (unlisted build, "
                                                    "or route V, whose MIG comes with each xclbin)"),
    "ddr.poller":             dict(sev="FAIL", hint="the ECC poller is not running while DDR is calibrated: the ECC "
                                                    "counters cannot be trusted"),
    # route V (OBS_ID 0x0B5E0008): DDR exists only in xclbins whose kernels use it (KEPT)
    "ddr.pruned":             dict(sev="INFO", hint="no DDR in this xclbin (PRUNED): the DDR window is fenced by design; "
                                                    "load a bank0 (KEPT) xclbin to use DDR"),
    "ddr.kept_not_calibrated": dict(sev="FAIL", hint="the loaded xclbin has DDR (KEPT) but calibration / ddr_ready is not "
                                                     "up: kernels and host DMA cannot use bank0 (bad DDR card? reload)"),
    "ddr.fenced_kept":        dict(sev="FAIL", hint="a host/P2P DDR access was fenced while a KEPT xclbin was loaded: "
                                                    "DDR was not calibrated when it was used; 'innova2-smi clear' after "
                                                    "diagnosis"),
    "ddr.fenced_pruned":      dict(sev="INFO", hint="DDR accesses fenced while no DDR was loaded (PRUNED or during a "
                                                    "load): expected, the fence answered instead of hanging"),
    "ddr.cal_lost":           dict(sev="WARN", hint="DDR calibration was lost while a KEPT xclbin was loaded (a "
                                                    "link-down reset or a MIG fault); see the recorder (CAL_LOST)"),
    "ddr.mode_deferred":      dict(sev="WARN", hint="EMU/HYBRID are deferred on route V: the requested mode is not in "
                                                    "force ('innova2-smi ddr-mode on')"),
    "ddr.rp_isolated":        dict(sev="WARN", hint="the dynamic region is isolated (an xclbin load in progress or "
                                                    "stuck): DDR state not meaningful"),
    # memory banks of the loaded xclbin (xocl sysfs mem_topology + the shell's registers; see innova2-smi memory)
    "mem.no_xclbin":          dict(sev="INFO", hint="no xclbin is loaded: nothing to judge per bank"),
    "mem.unavailable":        dict(sev="INFO", hint="xocl's mem_topology is not readable (xocl not bound, or no icap "
                                                    "subdevice): per-bank checks skipped"),
    "mem.bank_not_ready":     dict(sev="FAIL", hint="a bank the loaded xclbin USES is not ready (DDR not calibrated, "
                                                    "RP isolated, no host memory, P2P off): kernels using it fail or hang"),
    "mem.ddr_kept_not_ready": dict(sev="WARN", hint="route V: the loaded RM carries the MIG (KEPT) but ddr_ready=0: "
                                                    "calibration pending or failed (bad DDR card? reload the xclbin)"),
    # temperature
    "temp.hot_warn":          dict(sev="WARN", threshold=80.0, hint="FPGA hotter than the warning level; check "
                                                                   "airflow / fan"),
    "temp.hot_fail":          dict(sev="FAIL", threshold=95.0, hint="FPGA near its junction limit"),
    "temp.disagree":          dict(sev="WARN", threshold=3.0, hint="temperature sources disagree (C): one of them "
                                                                  "is frozen or stale"),
    "temp.feed_stale":        dict(sev="FAIL", hint="the throttle's temperature feed is not updating: the throttle "
                                                    "is blind (the fail-safe holds tier 2 if enabled)"),
    "temp.failsafe":          dict(sev="WARN", hint="stale fail-safe engaged: kernels run at tier-2 clock"),
    "temp.adc_never":         dict(sev="FAIL", hint="SYSMON max/min trackers never set: the ADC never converted"),
    "temp.known_frozen":      dict(sev="INFO", hint="known defect of this build: the wizard temperature bus freezes "
                                                    "(fixed in E7a3); SYSMON 0xA0400 is the true value"),
    # throttle
    "thr.auto":               dict(sev="WARN", hint="throttled by temperature now"),
    "thr.force":              dict(sev="WARN", hint="forced throttle active: 'innova2-smi throttle off'"),
    "thr.disabled":           dict(sev="WARN", hint="the automatic throttle is disabled: 'innova2-smi throttle enable on'"),
    "thr.not_default":        dict(sev="WARN", hint="throttle settings differ from the power-on defaults (a missed "
                                                    "simulate-hot restore?): 'innova2-smi throttle defaults'"),
    "thr.state_file":         dict(sev="WARN", hint="a simulate-hot was not restored: 'innova2-smi throttle restore'"),
    # clocks
    "clk.unlocked":           dict(sev="FAIL", hint="a clock wizard is not locked now"),
    "clk.icap_benign":        dict(sev="INFO", threshold=4, hint="known benign: clk_wiz_icap loses lock up to 4 times "
                                                                 "in the first 0.7 ms after configuration (every card)"),
    "clk.icap":               dict(sev="WARN", hint="clk_wiz_icap is never reset: lock losses beyond the benign "
                                                    "configuration ones are a fault (ICAP/PR at risk)"),
    "clk.kernel_benign":      dict(sev="INFO", hint="known benign: the kernel-clock wizards relock on every XRT clock "
                                                    "reprogramming (xclbin load)"),
    "clk.xdma_benign":        dict(sev="INFO", hint="known benign: clk_wiz_spi / clk_wiz_0 relock on every XDMA reset "
                                                    "(warm reboot, link-down)"),
    "clk.xdma":               dict(sev="WARN", hint="spi/freerun wizard lost lock more often than the XDMA was reset"),
    "clk.rising":             dict(sev="WARN", hint="a clock wizard lost lock during the check interval"),
    "clk.freq":               dict(sev="WARN", threshold=2.0, hint="frequency counter differs from the programmed "
                                                                  "rate by more than this (%)"),
    # PCIe
    "pcie.fpga_link":         dict(sev="WARN", hint="FPGA link to the on-card switch is below its capability"),
    "pcie.slot_link":         dict(sev="WARN", hint="the card's slot link is below what card and slot support "
                                                    "(riser? dirty contacts?)"),
    "pcie.fatal":             dict(sev="FAIL", hint="PCIe fatal errors were seen"),
    "pcie.nonfatal":          dict(sev="WARN", hint="PCIe non-fatal errors were seen"),
    "pcie.cor_rate":          dict(sev="WARN", threshold=10.0, hint="correctable PCIe errors rising faster than this "
                                                                   "per second"),
    "pcie.cor_count":         dict(sev="INFO", hint="correctable PCIe errors since configuration"),
    "pcie.aer":               dict(sev="WARN", hint="the kernel's AER logged non-fatal/fatal errors on the FPGA"),
    "pcie.slot_aer":          dict(sev="WARN", threshold=100, hint="the card's slot link (root port / on-card switch) "
                                                                  "logs correctable errors: a marginal link (riser, "
                                                                  "contacts); heavy XRT queries make it worse"),
    "pcie.slot_aer_rate":     dict(sev="WARN", threshold=5.0, hint="slot-link correctable errors rising faster than "
                                                                  "this per second during the check"),
    "pcie.slot_aer_fatal":    dict(sev="FAIL", hint="the card's slot link logged non-fatal/fatal errors"),
    # AXI monitors
    "axi.monitor":            dict(sev="WARN", hint="AXI error responses on a monitored path (see 'counters')"),
    # events since host boot (E7 timestamps)
    "event.since_boot":       dict(sev="WARN", hint="an error-class event happened after the host booted; "
                                                    "'innova2-smi recorder' has the details"),
}


def _item(rule, msg, evidence=None, sev=None, **kw):
    r = RULES[rule]
    d = dict(rule=rule, severity=sev or r["sev"], message=msg, evidence=evidence or {}, hint=r["hint"])
    d.update(kw)
    return d


def snapshot(sh):
    """One cheap, read-only snapshot: the layout's ddr_health registers (no recorder data port), SYSMON, memcalib,
    clocks, AER totals and hwmon FPGA temperature."""
    h = sh.host
    regs = {}
    for o in sorted(sh.lay["regs"]):
        if 0x18C <= o <= 0x198:                     # recorder data port: needs an index write, never touched here
            continue
        regs[o] = sh.r(o)
    s = dict(t=h.now(), regs=regs, sysmon=H.sysmon(sh), memcalib=sh.io.rd(R.MEMCALIB), mode=sh.io.rd(R.MODE_REG),
             clocks=H.clocks(sh), host_uptime=h.host_uptime())
    aer = {}
    for fn in (sh.mgmt, sh.user):
        for kind in ("correctable", "nonfatal", "fatal"):
            v = h.attr(fn, "aer_dev_" + kind) if fn else None
            for line in (v or "").splitlines():
                p = line.split()
                if len(p) == 2 and p[0].startswith("TOTAL_") and p[1].isdigit():
                    aer["%s %s" % (fn, kind)] = int(p[1])
    s["aer"] = aer
    s["slot_aer"] = H.slot_aer(h, sh.card)
    hw = H._hwmon(h, sh.mgmt)
    s["xrt_temp"] = hw.get("FPGA TEMP")
    return s


def evaluate(sh, s0, s1=None, expect_mode="on", board=None, baseline=None):
    """Rules over one or two snapshots. Returns the list of items (INFO, WARN and FAIL). `baseline` is a usable
    'innova2-smi ecc-baseline' record (H.load_ecc_baseline + H.baseline_usable), or None."""
    items = []
    lay, regs = sh.lay, (s1 or s0)["regs"]
    r0 = s0["regs"]
    dt = (s1["t"] - s0["t"]) if s1 else None
    rd = lambda o: regs.get(o, 0)
    delta = lambda o: ((s1["regs"].get(o, 0) - r0.get(o, 0)) & 0xFFFFFFFF) if s1 else 0

    # ---- DDR
    st = (s1 or s0)
    status = rd(0x00)
    mode_force = R.MODE_NAMES[status >> 29 & 3]
    gate = status >> 28 & 1
    rv = R.is_routev(lay)
    if rv:
        # route V: DDR exists only when the loaded xclbin's kernels use it (KEPT). A closed gate under a PRUNED xclbin
        # is NORMAL; it is a FAIL only when the xclbin has DDR and calibration / ddr_ready is not up.
        info = rd(0x1C0)
        kept, cal, released = info & 1, info >> 1 & 1, info >> 4 & 1
        ev_rv = dict(rv_info="0x%08x" % info, xrt_calibration_bit=st["memcalib"] & 1, mode=mode_force)
        if not released:
            items.append(_item("ddr.rp_isolated", "the dynamic region is isolated", ev_rv))
        elif not kept:
            items.append(_item("ddr.pruned", "no DDR in this xclbin (PRUNED)", ev_rv))
        elif not (cal and gate) and mode_force == "on":
            items.append(_item("ddr.kept_not_calibrated", "the xclbin has DDR (KEPT) but it is %s" % (
                "not calibrated" if not cal else "fenced (gate closed)"), ev_rv))
        req = R.MODE_NAMES[st["mode"] & 3]
        if req in R.ROUTEV_DEFERRED_MODES:
            items.append(_item("ddr.mode_deferred", "requested DDR mode '%s' is deferred on route V (in force: %s)" % (
                req, mode_force), dict(requested=req, in_force=mode_force)))
        lost = rd(0x1C8) >> 16
        if lost:
            items.append(_item("ddr.cal_lost", "%d DDR calibrations lost while KEPT" % lost, dict(rv_cal="0x%08x" % rd(0x1C8))))
    elif mode_force in ("on", "hybrid") and not gate:
        items.append(_item("ddr.not_calibrated", "DDR is not calibrated (mode %s, gate closed)" % mode_force,
                           dict(status="0x%08x" % status, xrt_calibration_bit=st["memcalib"] & 1)))
    if expect_mode and mode_force != expect_mode and not (rv and R.MODE_NAMES[st["mode"] & 3] in R.ROUTEV_DEFERRED_MODES):
        items.append(_item("ddr.mode", "DDR mode in force is '%s', expected '%s'" % (mode_force, expect_mode),
                           dict(requested=R.MODE_NAMES[st["mode"] & 3], in_force=mode_force)))
    ue, ce = rd(0x18), rd(0x14)
    items += ecc_items(sh, s0, s1, baseline)
    if rv:
        fk, fall = rd(0x1EC), rd(0xA0)
        if fk:
            items.append(_item("ddr.fenced_kept", "%d host/P2P DDR accesses fenced while a KEPT xclbin was loaded" % fk,
                               dict(fenced_while_kept=fk, fenced_total=fall,
                                    last_at_s=round(rd(0x1F4) * R.TS_S, 3))))
        if fall > fk:
            items.append(_item("ddr.fenced_pruned", "%d DDR accesses fenced while no DDR was loaded" % (fall - fk),
                               dict(fenced_total=fall, fenced_while_kept=fk)))
    elif rd(0xA0):
        ev = dict(count=rd(0xA0))
        if rd(0xA4) >> 31 & 1:
            ev["first"] = "%s at 0x%x" % ("read" if rd(0xA4) >> 30 & 1 else "write", rd(0xAC) << 32 | rd(0xA8))
        items.append(_item("ddr.fenced", "%d fenced DDR accesses" % rd(0xA0), ev))
    ld = rd(0x10) & 0xFFFF
    if s1 and (delta(0x10) & 0xFFFF):
        items.append(_item("ddr.link_down_rising", "link-down count rose by %d in %.1f s" % (delta(0x10) & 0xFFFF, dt),
                           dict(count=ld)))
    elif ld:
        items.append(_item("ddr.link_down_count", "%d link-downs since configuration" % ld, dict(count=ld)))
    calibrated = mode_force in ("on", "hybrid") and gate
    if rv:
        # route V: the poller exists only in a KEPT xclbin; judge it on this load's poll flag
        info = rd(0x1C0)
        calibrated = bool(info & 1 and info >> 1 & 1 and info >> 3 & 1 and gate)
        ue = (ue & ~(1 << 29)) | (rd(0x1D4) & (1 << 29))
    if calibrated and not (ue >> 29 & 1):
        items.append(_item("ddr.poller", "ECC poller has never completed a poll", dict(ecc_ue_reg="0x%08x" % ue)))
    elif calibrated and s1 and delta(0x2C) == 0 and dt and dt >= 0.2:
        items.append(_item("ddr.poller", "ECC poll counter did not move in %.1f s" % dt, dict(polls=rd(0x2C))))

    # ---- DDR rate: ui_clk from the ECC poll count over the two snapshots (needs >= 0.3 s and a calibrated MIG)
    items += rate_items(sh, s0, s1)

    # ---- temperature
    sm = st["sysmon"]
    if not sm["adc_converting"]:
        items.append(_item("temp.adc_never", "SYSMON max/min trackers never set",
                           dict(temp_max="unset", temp_min="unset")))
    t = sm["temp_c"]
    if t >= RULES["temp.hot_fail"]["threshold"]:
        items.append(_item("temp.hot_fail", "FPGA %.1f C" % t, dict(sysmon_c=t, threshold=RULES["temp.hot_fail"]["threshold"])))
    elif t >= RULES["temp.hot_warn"]["threshold"]:
        items.append(_item("temp.hot_warn", "FPGA %.1f C" % t, dict(sysmon_c=t, threshold=RULES["temp.hot_warn"]["threshold"])))
    lim = RULES["temp.disagree"]["threshold"]
    srcs = {}
    if lay["e7"]:
        srcs["throttle"] = R.code2c(rd(0xC8) >> 16 & 0x3FF)
    if st["xrt_temp"] is not None:
        srcs["xrt_cmc"] = st["xrt_temp"]
    for name, v in srcs.items():
        d = v - t
        frozen_src = not lay["e7a3"]               # E6b/E7a2: the wizard bus feeds cmc and the throttle
        if abs(d) > lim + (0.99 if name == "xrt_cmc" else 0):   # xrt_cmc is whole degrees
            items.append(_item("temp.disagree", "%s %.1f C vs SYSMON %.1f C" % (name, v, t),
                               dict(source=name, value_c=round(v, 2), sysmon_c=t, delta_c=round(d, 2), threshold=lim),
                               note="known frozen temperature bus on this build" if frozen_src else None))
        elif frozen_src:
            items.append(_item("temp.known_frozen", "%s comes from the wizard temperature bus" % name,
                               dict(source=name, value_c=round(v, 2), sysmon_c=t)))
    if lay["e7a3"]:
        sd = rd(0x14C)
        if sd >> 16 & 1:
            items.append(_item("temp.feed_stale", "temperature STALE now (no sample for > 1 s)",
                               dict(temp_stale="0x%08x" % sd, age_ms=round((rd(0x148) >> 16) * R.TS_S * 1e3, 1))))
        elif s1 and delta(0x140) == 0 and dt and dt >= 0.3:
            items.append(_item("temp.feed_stale", "TEMP_SAMPLES did not move in %.1f s" % dt,
                               dict(samples=rd(0x140))))
        if sd >> 17 & 1:
            items.append(_item("temp.failsafe", "stale fail-safe engaged", dict(temp_stale="0x%08x" % sd)))

    # ---- throttle
    if lay["e7"]:
        c, ths = rd(0xC0), rd(0xC8)
        force = c >> 1 & 1
        if force:
            items.append(_item("thr.force", "FORCE throttle on (level 1/%d)" % R.lvl(c >> 2),
                               dict(thr_ctrl="0x%02x" % c, thr_status="0x%08x" % ths)))
        elif ths >> 1 & 1:
            ev = dict(thr_status="0x%08x" % ths, code=ths >> 16 & 0x3FF, temp_c=round(R.code2c(ths >> 16 & 0x3FF), 2))
            if lay["e7a3"]:
                ev.update(tier=R.TIER_NAMES[ths >> 4 & 3], level="1/%d" % R.lvl(ths >> 8))
            items.append(_item("thr.auto", "throttled by temperature%s" % (
                " (%s)" % ev["tier"] if "tier" in ev else ""), ev))
        if not c & 1:
            items.append(_item("thr.disabled", "automatic throttle disabled", dict(thr_ctrl="0x%02x" % c)))
        dfl = R.THR_DEFAULTS.get(sh.obs_id, {})
        diff = {"0x%x" % o: "0x%08x (default 0x%08x)" % (rd(o), v) for o, v in dfl.items()
                if o != 0xC0 and rd(o) != v}
        cdiff = (c & ~0x2) != (dfl.get(0xC0, c) & ~0x2)       # FORCE has its own item
        if cdiff:
            diff["0xc0"] = "0x%02x (default 0x%02x)" % (c, dfl[0xC0])
        if diff:
            items.append(_item("thr.not_default", "throttle registers differ from the defaults", diff))
    try:
        if os.path.exists(H.thr_state_path(sh)):
            items.append(_item("thr.state_file", "leftover simulate-hot state file", dict(path=H.thr_state_path(sh))))
    except OSError:
        pass

    # ---- clocks
    lk, c1, c2 = rd(0x34), rd(0x38), rd(0x3C)
    cnt = [c1 & 0xFF, c1 >> 8 & 0xFF, c1 >> 16 & 0xFF, c1 >> 24 & 0xFF, c2 & 0xFF]
    for i, n in enumerate(R.LOCK_NAMES):
        if not lk >> i & 1:
            items.append(_item("clk.unlocked", "%s not locked" % n, dict(lock="0x%08x" % lk)))
    if s1:
        p1, p2 = r0.get(0x38, 0), r0.get(0x3C, 0)
        pc = [p1 & 0xFF, p1 >> 8 & 0xFF, p1 >> 16 & 0xFF, p1 >> 24 & 0xFF, p2 & 0xFF]
        for i, n in enumerate(R.LOCK_NAMES):
            if cnt[i] != pc[i]:
                items.append(_item("clk.rising", "%s lost lock %d times in %.1f s" % (n, cnt[i] - pc[i], dt),
                                   dict(before=pc[i], after=cnt[i])))
    if cnt[2]:
        if cnt[2] <= RULES["clk.icap_benign"]["threshold"]:
            items.append(_item("clk.icap_benign", "icap wizard lost lock %d x" % cnt[2], dict(losses=cnt[2])))
        else:
            items.append(_item("clk.icap", "icap wizard lost lock %d x (benign: <= %d)" % (
                cnt[2], RULES["clk.icap_benign"]["threshold"]), dict(losses=cnt[2])))
    if cnt[0] or cnt[1]:
        items.append(_item("clk.kernel_benign", "kernel-clock wizards lost lock %d / %d x" % (cnt[0], cnt[1]),
                           dict(data_clk=cnt[0], kernel_clk=cnt[1])))
    for i in (3, 4):
        if cnt[i]:
            if cnt[i] <= ld + 1:
                items.append(_item("clk.xdma_benign", "%s lost lock %d x (link-downs %d)" % (R.LOCK_NAMES[i], cnt[i], ld),
                                   dict(losses=cnt[i], link_downs=ld)))
            else:
                items.append(_item("clk.xdma", "%s lost lock %d x, more than link-downs + 1 (%d)" % (
                    R.LOCK_NAMES[i], cnt[i], ld + 1), dict(losses=cnt[i], link_downs=ld)))
    ck = st["clocks"]
    tol = RULES["clk.freq"]["threshold"]
    for name in ("data_clk", "kernel_clk"):
        w, m = ck[name]["wizard_mhz"], ck[name]["counter_mhz"]
        if w and m is not None and abs(m - w) * 100.0 / w > tol:
            items.append(_item("clk.freq", "%s counter %s MHz vs programmed %s MHz" % (name.upper(), m, w),
                               dict(wizard_mhz=w, counter_mhz=m, tolerance_pct=tol)))

    # ---- PCIe
    h = sh.host
    def spd(x):
        try:
            return float((x or "0").split()[0])
        except ValueError:
            return 0.0

    def wid(x):
        try:
            return int(x or 0)
        except ValueError:
            return 0
    cur_s, cur_w = h.attr(sh.mgmt, "current_link_speed"), h.attr(sh.mgmt, "current_link_width")
    max_s, max_w = h.attr(sh.mgmt, "max_link_speed"), h.attr(sh.mgmt, "max_link_width")
    if cur_s and max_s and (spd(cur_s) < spd(max_s) or wid(cur_w) < wid(max_w)):
        items.append(_item("pcie.fpga_link", "FPGA link %s x%s, capable %s x%s" % (cur_s, cur_w, max_s, max_w),
                           dict(current="%s x%s" % (cur_s, cur_w), max="%s x%s" % (max_s, max_w))))
    up, rp = sh.card["switch"], sh.card.get("root_port")
    us_s, us_w = h.attr(up, "current_link_speed"), h.attr(up, "current_link_width")
    if us_s:
        exp_s = min(spd(h.attr(up, "max_link_speed")), spd(h.attr(rp, "max_link_speed")) if rp else 99)
        exp_w = min(wid(h.attr(up, "max_link_width")), wid(h.attr(rp, "max_link_width")) if rp else 99)
        if spd(us_s) < exp_s or wid(us_w) < exp_w:
            items.append(_item("pcie.slot_link", "card slot link %s x%s, expected %.1f GT/s x%d" % (us_s, us_w, exp_s, exp_w),
                               dict(port=up, current="%s x%s" % (us_s, us_w), expected="%.1f GT/s x%d" % (exp_s, exp_w))))
    if lay["e7"]:
        if rd(0x108):
            items.append(_item("pcie.fatal", "%d PCIe fatal errors" % rd(0x108), dict(count=rd(0x108))))
        if rd(0x104):
            items.append(_item("pcie.nonfatal", "%d PCIe non-fatal errors" % rd(0x104), dict(count=rd(0x104))))
        if s1 and dt:
            rate = delta(0x100) / dt
            if rate > RULES["pcie.cor_rate"]["threshold"]:
                items.append(_item("pcie.cor_rate", "correctable PCIe errors at %.1f/s" % rate,
                                   dict(rate_per_s=round(rate, 1), threshold=RULES["pcie.cor_rate"]["threshold"])))
        if rd(0x100):
            items.append(_item("pcie.cor_count", "%d correctable PCIe errors" % rd(0x100), dict(count=rd(0x100))))
    for k, v in st["aer"].items():
        if v and ("nonfatal" in k or k.endswith("fatal")):
            items.append(_item("pcie.aer", "AER %s = %d" % (k, v), dict(counter=k, value=v)))

    for k, v in st.get("slot_aer", {}).items():
        if not v:
            continue
        if k.endswith("correctable"):
            rate = ((v - s0["slot_aer"].get(k, v)) / dt) if (s1 and dt) else None
            if rate is not None and rate > RULES["pcie.slot_aer_rate"]["threshold"]:
                items.append(_item("pcie.slot_aer_rate", "%s: %.1f correctable errors/s" % (k, rate),
                                   dict(port=k, rate_per_s=round(rate, 1), total=v)))
            elif v >= RULES["pcie.slot_aer"]["threshold"]:
                items.append(_item("pcie.slot_aer", "%s: %d correctable errors since boot" % (k, v),
                                   dict(port=k, total=v, threshold=RULES["pcie.slot_aer"]["threshold"])))
        else:
            items.append(_item("pcie.slot_aer_fatal", "%s: %d" % (k, v), dict(port=k, total=v)))

    # ---- AXI error monitors
    if lay["mon"]:
        for base, n in ((0x40, "P2P"), (0x60, "HOST"), (0x80, "PEER")):
            w, r_ = rd(base), rd(base + 4)
            if w or r_:
                items.append(_item("axi.monitor", "AXI %s: writes %d SLVERR/%d DECERR, reads %d SLVERR/%d DECERR" % (
                    n, w & 0xFFFF, w >> 16, r_ & 0xFFFF, r_ >> 16), dict(write="0x%08x" % w, read="0x%08x" % r_)))

    # ---- events since host boot (E7 last-event timestamps; FPGA uptime at host boot = uptime - host uptime)
    if lay["e7"] and st.get("host_uptime") is not None:
        up_s = ((rd(0xB4) << 32) | rd(0xB0)) / (rd(0xB8) or 250000000)
        boot_at = up_s - st["host_uptime"]
        for o, n in ((0xE0, "link-down"), (0xE8, "ECC CE"), (0xEC, "ECC UE"), (0xF0, "fenced access"),
                     (0xF8, "PCIe error")):
            ts = rd(o) * R.TS_S
            if baseline and o in (0xE8, 0xEC) and ts <= baseline["uptime_s"]:
                continue                            # acknowledged by the ECC baseline
            if rd(o) and ts > boot_at:
                items.append(_item("event.since_boot", "last %s at FPGA uptime %.1f s, after the host booted "
                                   "(%.1f s)" % (n, ts, max(boot_at, 0)), dict(event=n, at_s=round(ts, 3),
                                                                               host_boot_at_s=round(boot_at, 3))))
    return items


def ecc_items(sh, s0, s1=None, baseline=None):
    """ECC CE/UE items. The counters are cumulative since configuration and not host-clearable, so a UE is judged
    against a CLEAN POINT: the configuration itself, or a host-side 'innova2-smi ecc-baseline' taken later.

      * UEs since the clean point, no link-down since it         -> FAIL ddr.ue (DDR content has not been reset since:
                                                                    these are errors on data written after it)
      * ... with link-down(s) since it, the last UE AFTER the last link-down
                                                                  -> WARN ddr.ue_stale: likely reads of pre-reboot data
                                                                    the link-down MIG reset lost (the shell records no
                                                                    host fills, so it cannot prove it: never OK)
      * ... the last UE BEFORE the only link-down since it        -> FAIL ddr.ue (no MIG reset preceded them)
      * ... the last UE before the last of several link-downs, or no event timestamps (E6b layout)
                                                                  -> WARN ddr.ue_unclassified (stale or real: cannot tell)
      * nothing since a baseline                                  -> INFO ddr.ue_baselined (totals)"""
    regs = (s1 or s0)["regs"]
    rd = lambda o: regs.get(o, 0)
    items = []
    ue_r, ce = rd(0x18), rd(0x14)
    ue, ue_seen, ld = ue_r & 0xFFFF, bool(ue_r >> 31 & 1), rd(0x10) & 0xFFFF
    b = baseline
    ue_new = ue - b["ue_polls"] if b else ue
    seen_new = ue_seen and not (b and b.get("ue_seen"))
    ce_new = ce - b["ce"] if b else ce
    ld_new = ld - b["link_down"] if b else ld
    clean = ("the ecc-baseline at FPGA uptime %.1f s (%s)" % (b["uptime_s"], time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(b.get("written_at", 0)))) if b else "configuration")
    up_cyc = rd(0xB4) << 32 | rd(0xB0)
    ts_ok = bool(sh.lay["e7"]) and up_cyc < 1 << 48         # event TS = uptime[47:16]: wraps after ~13 days
    t_ue, t_ld = rd(0xEC) * R.TS_S, rd(0xE0) * R.TS_S
    if ue_new > 0 or seen_new or (ue == 0xFFFF and b and b["ue_polls"] == 0xFFFF):
        ev = dict(ue_polls=ue, ue_polls_since_clean_point=ue_new, clean_point=clean, ce=ce, link_downs_since=ld_new)
        if rd(0x28) >> 31 & 1:
            ev["first_ue"] = R.ffa_str(R.decode_ffa((rd(0x28) & 0x7FFFFFFF) << 32 | rd(0x24)))
        if ts_ok:
            ev["last_ue_at_s"] = round(t_ue, 3) if rd(0xEC) else None
            ev["last_link_down_at_s"] = round(t_ld, 3) if rd(0xE0) else None
        if s1:
            ev["ue_polls_in_interval"] = ((s1["regs"].get(0x18, 0) - s0["regs"].get(0x18, 0)) & 0xFFFF)
        n = "%d ECC uncorrectable polls%s" % (ue_new, " since %s" % clean if b else "")
        if ld_new == 0:
            items.append(_item("ddr.ue", n + " with no link-down since %s: not stale-data reads" % clean, ev))
        elif ts_ok and rd(0xEC) and rd(0xE0) and t_ue > t_ld:
            items.append(_item("ddr.ue_stale", n + ", the last at %.1f s, after the warm reboot (link-down) at %.1f s: "
                               "likely stale-data reads after warm reboot" % (t_ue, t_ld), ev))
        elif ts_ok and rd(0xEC) and rd(0xE0) and ld_new == 1:
            items.append(_item("ddr.ue", n + ", the last at %.1f s, BEFORE the only link-down since %s (%.1f s): no MIG "
                               "reset preceded them, so they are not stale-data reads" % (t_ue, clean, t_ld), ev))
        else:
            items.append(_item("ddr.ue_unclassified", n + " with %d link-downs since %s: %s" % (
                ld_new, clean, "the last UE precedes the last link-down" if ts_ok else
                "this shell has no event timestamps"), ev))
    elif ue or ue_seen:
        items.append(_item("ddr.ue_baselined", "%d ECC uncorrectable polls in total, none since %s" % (ue, clean),
                           dict(ue_polls=ue, at_baseline=b["ue_polls"] if b else None)))
    if ce_new > 0:
        ev = dict(ce=ce, ce_since_clean_point=ce_new, clean_point=clean, link_downs_since=ld_new)
        if rd(0x20) >> 31 & 1:
            ev["first_ce"] = R.ffa_str(R.decode_ffa((rd(0x20) & 0x7FFFFFFF) << 32 | rd(0x1C)))
        if s1:
            ev["ce_in_interval"] = (s1["regs"].get(0x14, 0) - s0["regs"].get(0x14, 0)) & 0xFFFFFFFF
        items.append(_item("ddr.ce", "%d ECC corrected errors%s" % (ce_new, " since %s" % clean if b else ""), ev))
    elif ce:
        items.append(_item("ddr.ce_baselined", "%d ECC corrected errors in total, none since %s" % (ce, clean),
                           dict(ce=ce, at_baseline=b["ce"] if b else None)))
    return items


def rate_items(sh, s0, s1):
    if not s1 or s1["t"] - s0["t"] < 0.3:
        return []
    ok0, _ = H.ddr_ready_for_rate(sh, s0["regs"])
    ok1, _ = H.ddr_ready_for_rate(sh, s1["regs"])
    r0, r1 = s0["regs"], s1["regs"]
    if not (ok0 and ok1) or (r0[0x10] & 0xFFFF) != (r1[0x10] & 0xFFFF):
        return []
    cfg = R.mig_config(sh.rom_ts)
    if not cfg:
        return [_item("ddr.rate_unverified", "no MIG table for ROM_TS %s" % sh.rom_ts, {})]
    ui, dp, dt = R.ui_clk_from_polls(r0[0x2C], r0[0xB4] << 32 | r0[0xB0], r1[0x2C], r1[0xB4] << 32 | r1[0xB0],
                                     cfg["poll_cyc"])
    if not ui:
        return []                                   # a dead poller is ddr.poller's finding
    m = H.rate_result(cfg, ui, dp, dt, routev=R.is_routev(sh.lay))
    ev = dict(measured_ui_clk_mhz=m["ui_clk_mhz"], table_ui_clk_mhz=cfg["ui_clk_mhz"], measured_mts=m["mts"],
              table=cfg["speed_grade"], deviation_pct=m["vs_table_pct"], precision_pct=m["precision_pct"])
    if R.is_routev(sh.lay):
        return [_item("ddr.rate_unverified", "route V: measured %.0f MT/s (template %s, %+.2f%%)" % (
            m["mts"], cfg["speed_grade"], m["vs_table_pct"]), ev)]
    if abs(m["vs_table_pct"]) > RULES["ddr.rate_mismatch"]["threshold"] + m["precision_pct"]:
        return [_item("ddr.rate_mismatch", "DDR measured %.1f MT/s (ui_clk %.2f MHz), table %s (%.2f MHz): %+.2f%%" % (
            m["mts"], m["ui_clk_mhz"], cfg["speed_grade"], cfg["ui_clk_mhz"], m["vs_table_pct"]), ev)]
    return []


def platform_items(sh):
    """Installed platform vs the flashed shell (file headers only)."""
    h, items = sh.host, []
    if not sh.rom_ts:
        return items
    x = "/lib/firmware/xilinx/10ee-%s-4351-%016x.xsabin" % (R.FPGA_MGMT_DEV[2:], sh.rom_ts)
    if not h.exists(x):
        items.append(_item("platform.no_xsabin", "no boot xsabin for ROM_TS 0x%08x" % sh.rom_ts, dict(expected=x)))
    d = "/opt/xilinx/dsa/%s/test" % sh.vbnv
    for f in ("verify.xclbin", "bandwidth.xclbin"):
        hdr = R.xclbin_header(h.file_head(os.path.join(d, f), 512) or b"")
        if hdr and hdr["rom_ts"] != sh.rom_ts:
            items.append(_item("platform.test_dir", "%s/%s is for ROM_TS 0x%08x, the card runs 0x%08x" % (
                d, f, hdr["rom_ts"], sh.rom_ts), dict(file=os.path.join(d, f), file_rom_ts="0x%08x" % hdr["rom_ts"],
                                                     card_rom_ts="0x%08x" % sh.rom_ts,
                                                     link=h.readlink(d))))
    return items


def xrt_items(sh):
    h, items = sh.host, []
    rdy = H.xrt_ready(h, sh.user)
    if sh.user and rdy != "Yes":
        items.append(_item("xrt.not_ready", "Device Ready: %s" % (rdy or "unknown (xocl not bound)"),
                           dict(user=sh.user, ready=h.attr(sh.user, "ready"), driver=h.driver(sh.user))))
    for fn, pat in ((sh.mgmt, "firewall.*/"), (sh.user, "firewall.*/")):
        if not fn:
            continue
        st, ds = h.attr(fn, pat + "status"), h.attr(fn, pat + "detected_status")
        if (st and st not in ("0", "0x0")) or (ds and ds not in ("0", "0x0")):
            items.append(_item("xrt.firewall", "firewall on %s: status %s, detected %s" % (fn, st, ds),
                               dict(status=st, detected_status=ds, trip=h.attr(fn, pat + "detected_trip"))))
    if sh.user:
        mcs = h.attr(sh.user, "mailbox_connect_state")
        try:
            ok = int(mcs, 0) & 1 if mcs else None
        except ValueError:
            ok = None
        if ok == 0:
            items.append(_item("xrt.mailbox", "mailbox_connect_state %s" % mcs, dict(mailbox_connect_state=mcs)))
    return items


def mem_items(mem):
    """Per-bank items from H.xrt_memory (its context is the check's own snapshot)."""
    items = []
    if not mem.get("available"):
        items.append(_item("mem.unavailable", mem.get("reason") or "mem_topology not readable", {}))
        return items
    if not mem.get("xclbin_loaded"):
        items.append(_item("mem.no_xclbin", "no xclbin loaded (%s)" % mem.get("reason"),
                           dict(xclbin_uuid=mem.get("xclbin_uuid"))))
        return items
    for b in mem["banks"]:
        if b["used"] and b["ready"] is False:
            items.append(_item("mem.bank_not_ready", "bank %s (%s %s @%s) is used by the xclbin but is %s" % (
                b["tag"], b["kind"], b["type"], b["base"], b["state"].replace("in use, ", "")),
                dict(cu_args=len(b["connections"]), evidence=b["evidence"], xclbin=mem.get("xclbin_uuid"))))
    return items


def routev_mem_items(ctx):
    """Route V: KEPT while ddr_ready=0 (from the register snapshot; independent of mem_topology)."""
    if ctx.get("routev") and ctx.get("rp_released") and ctx.get("ddr_kept") and not ctx.get("ddr_ready"):
        return [_item("mem.ddr_kept_not_ready", "DDR KEPT (the RM has a MIG) while ddr_ready=0",
                      dict(source=ctx.get("ddr_ready_source")))]
    return []


def card_items(host, card):
    """Items from discovery/classification (the ConnectX-5 side); card must have been classify()'d."""
    items = []
    q = card.get("cx5_query") or {}
    cxv = ("admin=%s oper=%s status=%s" % (q.get("admin"), q.get("oper"), q.get("status"))) if "oper" in q else None
    st = card.get("state")
    if st == "FPGA_MISSING":
        items.append(_item("fpga.missing", "ConnectX-5 %s present, no FPGA function behind switch %s" % (
            card["cx5"][0], card["switch"]), dict(cx5_view=cxv or q.get("error"))))
    elif st == "FLEX":
        items.append(_item("fpga.flex", "Flex/Factory image running (%s)" % (
            "burn endpoint %s" % card["bope"] if card.get("bope") else "no burn endpoint"),
            dict(cx5_view=cxv, oper_image=H.CX5_IMAGES.get(q.get("oper")))))
    elif st == "OTHER_XILINX":
        items.append(_item("fpga.other", "FPGA function(s) %s" % ", ".join(
            "%s 10ee:%s (driver %s)" % (f["bdf"], f["device"][2:], f["driver"]) for f in card["fpga"]),
            dict(cx5_view=cxv)))
    for sev, text in card.get("flags", []):
        items.append(_item("cx5.inconsistent" if sev == "FAIL" else "cx5.query", text, dict(cx5_view=cxv), sev=sev))
    hl = card.get("cx5_health")
    if hl:
        for n in hl["notes"]:
            if n.startswith("no /dev/"):
                items.append(_item("cx5.no_node", n, dict(node=hl.get("node"))))
            else:
                items.append(_item("cx5.health", n, dict(driver=hl.get("driver"), fw=hl.get("fw_ver"))))
    return items


def run_check(host, card, interval=1.0, expect_mode="on", query_cx5=True):
    """Everything for one card. Returns dict(card, identity, items, severity)."""
    if "state" not in card:
        H.classify(host, card, query_cx5=query_cx5)
    card.setdefault("cx5_health", H.cx5_health(host, card))
    items = card_items(host, card)
    sh = H.Shell(host, card)
    if card["state"] == "OUR_SHELL?":
        sh.identify()
        card["state"] = sh.state
    bi = H.board_id(host, card)
    if bi.get("vpd_empty"):
        items.append(_item("identity.vpd_empty", "ConnectX-5 %s has an empty VPD: PN/EC/serial unknown" % bi.get("nic"),
                           dict(psid=bi.get("psid"), fw=bi.get("fw_ver"))))
    elif "error" not in bi and not bi.get("validated"):
        items.append(_item("identity.board", "board %s rev %s is not a validated board" % (bi.get("PN"), bi.get("EC")),
                           dict(pn=bi.get("PN"), ec=bi.get("EC"), serial=bi.get("SN"))))
    if not sh.ok:
        if card["state"] == "OUR_SHELL_OLD":
            items.append(_item("identity.old", sh.reason, dict(vbnv=sh.vbnv, rom_ts=sh.rom_ts)))
            items += mem_items(H.xrt_memory(host, card, None))      # sysfs only: XRT's own calibration attribute
        elif card["state"] == "OUR_SHELL_UNREADABLE":
            failed = [x for x in sh.steps if not x["ok"]]
            items.append(_item("identity.refused", sh.reason or "shell not identified",
                               dict(failed_step=failed[-1]["step"] if failed else None,
                                    passed=[x["step"] for x in sh.steps if x["ok"]])))
        return _result(card, sh, items)
    for w in sh.warnings:
        items.append(_item("identity.obs_mismatch", w, dict(rom_ts=sh.rom_ts, obs_id="0x%08X" % sh.obs_id)))
    if sh.uuid_warning:
        u = sh.uuid_info()
        items.append(_item("identity.uuid_mismatch", sh.uuid_warning, dict(live=u["interface_uuid"], source=sh.uuid_source,
                                                                            table=u["interface_uuid_table"])))
    items += platform_items(sh)
    items += xrt_items(sh)
    s0 = snapshot(sh)
    s1 = None
    if interval and interval > 0:
        host.sleep(interval)
        s1 = snapshot(sh)
    base = None
    bpath, b, why = H.load_ecc_baseline(sh)
    if b is not None:
        why = H.baseline_usable(sh, b, (s1 or s0)["regs"], sn=bi.get("SN"))
    if why:
        items.append(_item("ddr.baseline_ignored", "ecc-baseline %s ignored: %s" % (bpath, why), dict(file=bpath)))
    elif b is not None:
        base = b
        items.append(_item("ddr.baseline", "ECC counted since the ecc-baseline at FPGA uptime %.1f s: CE %d, UE polls %d "
                           "then" % (b["uptime_s"], b["ce"], b["ue_polls"]), dict(file=bpath)))
    items += evaluate(sh, s0, s1, expect_mode=expect_mode, baseline=base)
    last = s1 or s0
    ctx = H.mem_context(host, card, sh, regs=last["regs"], memcalib=last["memcalib"])
    items += routev_mem_items(ctx)
    items += mem_items(H.xrt_memory(host, card, sh, ctx=ctx))
    return _result(card, sh, items)


def _result(card, sh, items):
    worst = max([SEV_ORDER[i["severity"]] for i in items] + [0])
    sev = {0: "OK", 1: "WARN", 2: "FAIL"}[worst]
    return dict(card=dict(index=card.get("index"), state=card.get("state"), mgmt=card.get("mgmt"), user=card.get("user"),
                          cx5=card.get("cx5"), switch=card.get("switch")),
                identity=sh.ident(), severity=sev, items=items)


def render(res, verbose=False):
    c = res["card"]
    who = "card %s (CX5 %s, FPGA %s, %s)" % (c["index"], (c["cx5"] or ["?"])[0], c["mgmt"] or "-",
                                             res["identity"].get("build") or c.get("state") or "?")
    shown = [i for i in res["items"] if i["severity"] in ("WARN", "FAIL") or verbose]
    lines = []
    if not [i for i in res["items"] if i["severity"] in ("WARN", "FAIL")]:
        n_info = len([i for i in res["items"] if i["severity"] == "INFO"])
        lines.append("%s: OK: no concerns%s" % (who, " (%d info item%s; -v shows them)" % (n_info, "s" if n_info > 1 else "")
                                                if n_info and not verbose else ""))
    else:
        lines.append("%s: %s" % (who, res["severity"]))
    for i in sorted(shown, key=lambda i: -SEV_ORDER[i["severity"]] if i["severity"] != "INFO" else 1):
        ev = ", ".join("%s=%s" % (k, v) for k, v in i["evidence"].items() if v is not None)
        lines.append("  %-4s %-22s %s%s" % (i["severity"], i["rule"], i["message"], " [%s]" % ev if ev else ""))
        lines.append("       %-22s hint: %s" % ("", i["hint"]))
    return "\n".join(lines)


def rules_table():
    out = ["%-22s %-4s %-9s %s" % ("rule", "sev", "threshold", "hint")]
    for k, r in RULES.items():
        out.append("%-22s %-4s %-9s %s" % (k, r["sev"], r.get("threshold", r.get("expect", "")), r["hint"]))
    return "\n".join(out)
