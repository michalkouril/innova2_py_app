#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""innova2-smi -- query and test the Innova-2 XRT shell (fp8c lineage: E6b, E6b-2400, E7a2, E7a3, E7a3-2666ca48).

  innova2-smi list                         every Innova-2 card, found from its ConnectX-5, with the FPGA's state
  innova2-smi status   [-d CARD]           one-screen health of a card
  innova2-smi check    [-d CARD|--all]     passive "concerns only" check (safe while kernels/DMA run); exit 0/1/2
  innova2-smi ddr      [-d CARD]           DDR configuration (per-build MIG table), measured speed, ECC totals
  innova2-smi memory   [-d CARD]           memory banks of the loaded xclbin: used, ready, PRUNED
  innova2-smi counters [-d CARD] [--watch N]   every counter the running shell exposes
  innova2-smi recorder [-d CARD] [-n N]    the flight recorder (E7a2+)
  innova2-smi clear | ddr-mode | throttle ...   controls (state changes need confirmation or --yes)
  innova2-smi test <name>|all              active tests (validate, ddr-soak, temp-live, throttle, pr-stress, p2p,
                                           ecc-live, flash-verify)
  innova2-smi regmap [--markdown|--check-rtl ddr_health.v]   the register maps this tool uses

Read-only by default. Every register access goes through the running shell's OBS_ID register map; an unknown shell
is refused before anything but its OBS_ID is read (an undecoded management read hard-locks the host). Run as root.
`innova2-smi <command> -h` documents each command.
"""

import argparse
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import innova2_smi_check as C          # noqa: E402
import innova2_smi_hw as H             # noqa: E402
import innova2_smi_regmap as R         # noqa: E402
import innova2_smi_tests as T          # noqa: E402

VERSION = "1.3.6"


def _version():
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().split()[0]
    except (OSError, IndexError):
        return VERSION


# ----------------------------------------------------------------------------------------------- helpers
def confirm(args, prompt):
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("*** %s -- refusing without --yes (no terminal to ask)" % prompt, file=sys.stderr)
        return False
    sys.stdout.write("%s [y/N] " % prompt)
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def out(args, obj, text):
    if getattr(args, "json", False):
        print(H.jdump(obj))
    else:
        print(text)


def cards(host, args, query=True):
    cs = H.discover(host)
    for c in cs:
        H.classify(host, c, query_cx5=query and not args.no_cx5, transport=args.transport)
    return cs


def open_card(host, args, need_shell=True):
    cs = cards(host, args)
    if not cs:
        raise H.Refused("no Innova-2 card found (no ConnectX-5 behind an Innova-2 switch)")
    card = H.select_card(cs, args.device)
    sh = H.Shell(host, card)
    if card["state"] == "OUR_SHELL?":
        sh.identify()
        card["state"] = sh.state
    if need_shell and not sh.ok:
        raise H.Refused("card %d (%s): %s -- %s" % (card["index"], card["cx5"][0], card["state"],
                                                     sh.reason or H.STATES.get(card["state"], "")))
    return card, sh


def fmt_bool(b, yes="yes", no="no"):
    return yes if b else no


# ----------------------------------------------------------------------------------------------- list
def cmd_list(host, args):
    """Every Innova-2 card: ConnectX-5, serial, FPGA functions, the FPGA's state and the shell identity."""
    if args.load_module:
        missing = [c for c in H.discover(host) if not host.exists("/dev/%s_mlx5_fpga_tools" % c["cx5"][0])]
        if missing:
            if confirm(args, "load mlx5_fpga_tools (or innova2_areg) for the ConnectX-5 image query?"):
                rc, o = host.run(["modprobe", "mlx5_fpga_tools"], timeout=30)
                if rc:
                    rc, o = host.run(["modprobe", "innova2_areg"], timeout=30)
                print("modprobe: %s" % ("ok" if rc == 0 else o.strip()))
    rows = []
    for c in cards(host, args):
        sh = H.Shell(host, c)
        if c["state"] == "OUR_SHELL?":
            sh.identify()
            c["state"] = sh.state
        bi = H.board_id(host, c)
        c["serial"] = bi.get("SN")
        hl = H.cx5_health(host, c)
        q = c.get("cx5_query") or {}
        rv = H.routev(sh) if sh.ok else None
        rows.append(dict(index=c["index"], switch=c["switch"], cx5=c["cx5"], serial=bi.get("SN"), part=bi.get("PN"),
                         rev=bi.get("EC"), board_validated=bi.get("validated"), psid=bi.get("psid"),
                         vpd_empty=bi.get("vpd_empty"), board=bi, mgmt=c["mgmt"], user=c["user"],
                         bope=c["bope"], fpga_functions=c["fpga"], state=c["state"],
                         running_image=H.CX5_IMAGES.get(q.get("oper")) if "oper" in q else None,
                         cx5_query=q, cx5_health=hl, flags=c["flags"],
                         shell=sh.ident(), device_ready=H.xrt_ready(host, c["user"]),
                         xclbin_ddr=rv["xclbin_ddr"] if rv else None, rm=rv["rm_label"] if rv else None,
                         dna=None, dna_note="the shell has no DNA_PORT reader; DNA is readable over JTAG only"))
    if args.json:
        print(H.jdump(rows))
        return 0
    if not rows:
        print("no Innova-2 card found")
        return 1
    for r in rows:
        s = r["shell"]
        q = r["cx5_query"] or {}
        b = r["board"]
        if "error" in b:
            bline = "board unknown (%s)" % b["error"]
        elif b.get("vpd_empty"):
            bline = "serial none, board none (VPD empty%s: not identifiable)" % (
                "; PSID %s fw %s" % (b["psid"], b.get("fw_ver")) if b.get("psid") else "")
        else:
            bline = "serial %s  board %s rev %s%s" % (
                r["serial"] or "(no SN in VPD)", r["part"] or "(no PN in VPD)", r["rev"] or "(no EC in VPD)",
                "" if r["board_validated"] in (None, True) else " (NOT a validated board)")
        print("card %d  ConnectX-5 %s  %s" % (r["index"], ",".join(r["cx5"]), bline))
        print("        FPGA %s  state %s" % (
            ", ".join("%s 10ee:%s [%s]" % (f["bdf"], f["device"][2:], f["driver"] or "no driver") for f in r["fpga_functions"])
            or (("burn endpoint " + r["bope"]) if r["bope"] else "none"), r["state"]))
        print("        ConnectX view: %s" % (("admin=%s oper=%s (%s) status=%s via %s" % (
            q["admin"], q["oper"], r["running_image"], q["status"], q.get("transport"))) if "oper" in q
            else (q.get("error") or "not queried")))
        if s.get("vbnv"):
            print("        shell %s  ROM_TS %s (%s)  OBS_ID %s  build %s  Device Ready %s%s" % (
                s["vbnv"], s["rom_ts"], s["rom_ts_hex"], s["obs_id"] or "-", s["build"] or "-", r["device_ready"] or "-",
                ("  %s" % (r["rm"] if r["rm"] and r["rm"].startswith("no xclbin") else "xclbin DDR %s" % r["xclbin_ddr"]))
                if r["xclbin_ddr"] else ""))
        if s.get("refused") and r["state"] not in ("FLEX", "FPGA_MISSING", "OTHER_XILINX"):
            print("        not read further: %s" % s["refused"])
        for sev, t in r["flags"]:
            print("        %s %s" % (sev, t))
        for n in r["cx5_health"]["notes"]:
            print("        note: %s" % n)
    return 0


# ----------------------------------------------------------------------------------------------- status
def collect_status(host, card, sh):
    st = dict(card=dict(index=card["index"], cx5=card["cx5"], switch=card["switch"], fpga=card["fpga"],
                        state=card["state"], flags=card["flags"], cx5_query=card.get("cx5_query")),
              identity=sh.ident(), board=H.board_id(host, card))
    st["memory"] = H.xrt_memory(host, card, sh)
    if not sh.ok:
        return st
    up = H.uptime(sh)
    st["xrt"] = H.xrt_state(sh)
    st["clocks"] = H.clocks(sh)
    st["ddr"] = dict(H.ddr_state(sh), ecc=H.ecc(sh), **H.health(sh))
    st["ddr_speed"] = H.ddr_speed(sh, window=1.0)
    rv = H.routev(sh)
    if rv:
        st["routev"] = rv
    srcs, hw = H.temperature(sh)
    q = card.get("cx5_query") or {}
    if "fpga_mtmp_c" in q:
        srcs["cx5_mtmp"] = dict(temp_c=q["fpga_mtmp_c"], path="ConnectX-5 MTMP sensor 63 (FPGA diode)",
                                state="LIVE", evidence="read by the ConnectX firmware")
    st["temperature"] = srcs
    st["voltages"] = dict(sysmon={k: srcs["sysmon"][k] for k in ("vccint", "vccaux", "vccbram")},
                          xrt={k: v for k, v in hw.items() if k.startswith(("VCC", "0V", "1V", "12V", "3V3", "MGT"))})
    st["fan_rpm"] = hw.get("FAN SPEED")
    if sh.lay["e7"]:
        st["throttle"] = H.throttle(sh)
        st["pr"] = H.pr(sh)
        st["last_events"] = H.last_events(sh, up["seconds"])
        inf, recs = H.recorder(sh, last=5)
        st["recorder"] = dict(inf, last=recs)
    st["pcie"] = H.pcie(sh)
    st["uptime"] = up
    st["p2p"] = H.p2p(sh)
    if os.path.exists(H.thr_state_path(sh)):
        st["simulate_hot_pending"] = H.thr_state_path(sh)
    return st


def uuid_str(i):
    """'<uuid> (<source>; table <agreement>)' -- the live sysfs value first, the table only as a cross-check."""
    u, src, chk = i.get("interface_uuid"), i.get("interface_uuid_source"), i.get("interface_uuid_check")
    if not u:
        return "- (not in sysfs, not in this tool's table)"
    if src == "table":
        return "%s (from this tool's table; sysfs interface_uuids not readable)" % u
    if chk == "match":
        return "%s (%s; table agrees)" % (u, src)
    if chk == "MISMATCH":
        return "%s (%s) -- WARN: table has %s" % (u, src, i.get("interface_uuid_table"))
    return "%s (%s; build not in this tool's table)" % (u, src)


def thr_effect(t):
    """What the throttle is doing NOW, unambiguously: 'FULL (not throttled)' or the tier/FORCE and its level."""
    if not t["enabled"]:
        return "FULL (not throttled: the throttle is disabled)"
    if t["force"]:
        return "FORCE 1/%d (throttled; FORCE bypasses the temperature tiers)" % t["force_level"]
    if "tier2" in t:
        tier = t["tier_in_effect"]
        if tier == "FULL" and not t["requested"]:
            return "FULL (not throttled)"
        return "%s (throttled to 1/%d%s)" % (tier.replace("tier ", "T"), t["level_now"],
                                             ", fail-safe: temperature STALE" if t["failsafe_engaged"] else "")
    if t["requested"]:
        return "throttled to 1/%d (over temperature)" % t["force_level"]
    return "FULL (not throttled)"


def render_status(st):
    L = []
    c, i = st["card"], st["identity"]
    b = st["board"]
    L.append("card %d: ConnectX-5 %s, switch %s, state %s" % (c["index"], ",".join(c["cx5"]), c["switch"], c["state"]))
    L.append("board          : " + H.board_summary(b))
    L.append("shell          : %s, ROM_TS %s (%s), OBS_ID %s (%s), interface uuid %s" % (
        i.get("build") or "?", i.get("rom_ts"), i.get("rom_ts_hex"), i.get("obs_id"), i.get("layout"), uuid_str(i)))
    for w in i.get("warnings", []):
        L.append("  *** %s" % w)
    for sev, t in c.get("flags", []):
        L.append("  %s %s" % (sev, t))
    if not i.get("supported"):
        L.append("not read further: %s" % i.get("refused"))
        for s in i.get("steps", []):
            L.append("  %-28s %s %s" % (s["step"], "ok  " if s["ok"] else "FAIL", s["detail"]))
        if st.get("memory"):
            L += render_memory(st["memory"], prefix="memory         : ")
        return "\n".join(L)
    x = st["xrt"]
    L.append("XRT            : Device Ready %s, mgmt ready %s, MIG calibration %s, xclbin %s" % (
        x["device_ready"], x["mgmt_ready"], x["mig_calibration"], x["xclbin_uuid"]))
    if st.get("memory"):
        L += render_memory(st["memory"], prefix="memory         : ")
    ck = st["clocks"]
    L.append("clocks         : DATA_CLK %s MHz (counter %s), KERNEL_CLK %s MHz (counter %s)" % (
        ck["data_clk"]["wizard_mhz"], ck["data_clk"]["counter_mhz"], ck["kernel_clk"]["wizard_mhz"],
        ck["kernel_clk"]["counter_mhz"]))
    L.append("clock wizards  : " + ", ".join("%s %s%s" % (l["name"], "locked" if l["locked"] else "NOT LOCKED",
                                                         " (lost %d x)" % l["losses"] if l["losses"] else "")
                                            for l in st["ddr"]["locks"]))
    d = st["ddr"]
    rv = st.get("routev")
    if rv:
        # route V: the DDR is part of the loaded xclbin (KEPT) or absent (PRUNED); a closed gate is normal when PRUNED
        if rv.get("xclbin_loaded", True):
            head = "xclbin %s" % ("KEPT (MIG in the dynamic region)" if rv["ddr_present"] else "PRUNED (no DDR in this xclbin)")
        else:                       # nothing loaded since power-on: the flash image's own RM (r1 KEPT template, r2 filler)
            head = "no xclbin (boot RM: %s)" % ("KEPT template, MIG in the dynamic region" if rv["ddr_present"]
                                                else "filler, no DDR")
        L.append("DDR (route V)  : %s, %s, %s, XRT calibration bit %d; mode %s (requested %s%s)" % (
            head,
            ("calibrated" if rv["calibrated"] else "NOT CALIBRATED") if rv["ddr_present"] else "nothing to calibrate",
            "window gate open" if d["gate_open"] else ("window fenced (expected: no DDR)" if not rv["ddr_present"]
                                                       else "WINDOW FENCED"),
            d["xrt_calibration_bit"], d["in_force"], d["requested"],
            "; emu/hybrid deferred on route V" if d["requested"] in R.ROUTEV_DEFERRED_MODES else ""))
        L.append("  loads        : %d KEPT, %d PRUNED (the power-on RM counts once); %d calibrations (%d lost), the last "
                 "%.1f ms after its load; fenced while KEPT %d; RP %s, ECC poller %s" % (
                     rv["loads_kept"], rv["loads_pruned"], rv["calibrations"], rv["calibrations_lost"],
                     rv["last_calibration_ms"], rv["fenced_while_kept"], "released" if rv["rp_released"] else "ISOLATED",
                     "present" if rv["ecc_poller"] else "absent"))
        di = rv["ddr_info"]
        if di["kept"]:
            L.append("  DDR config   : DDR4-%d (tCK %d ps), CL%d CWL%d tRCD %d tRP %d, %d-bit%s, CA %s, MIG %s, part %s; ui_clk "
                     "measured %.3f MHz (expected %.3f)" % (di["mts"], di["tck_ps"], di["cl"], di["cwl"], di["trcd"],
                                                            di["trp"], di["dq_width"], " ECC" if di["ecc"] else "",
                                                            di["ca_drive"], di["mig_variant"], di["part_id"],
                                                            di["ui_clk_mhz_measured"], di["ui_clk_mhz_expected"]))
        else:
            L.append("  DDR config   : %s (DDR_INFO magic %s)" % (("no DDR in this xclbin" if rv.get("xclbin_loaded", True)
                                                                  else "no DDR in the boot RM") if di["no_ddr"] else "not reported",
                                                                 di["magic"]))
        el = rv["ecc_this_load"]
        L.append("  this load    : ECC %d corrected, %d uncorrectable polls%s, %d polls" % (
            el["ce"], el["ue_polls"], " (UE SEEN)" if el["ue_seen"] else "", el["polls"]))
    else:
        L.append("DDR            : mode %s (requested %s), MIG %s, %s, XRT calibration bit %d%s" % (
            d["in_force"], d["requested"], "held in reset" if d["mig_held"] else "running",
            "gate open" if d["gate_open"] else "GATE CLOSED (fenced)", d["xrt_calibration_bit"],
            ", fenced access seen" if d["fenced_seen"] else ""))
    if st.get("ddr_speed"):
        L.append("DDR speed      : " + ddr_speed_line(st["ddr_speed"]))
    e = d["ecc"]
    L.append("ECC%s: %d corrected, %d uncorrectable polls%s, ECC %s, poller %s, %d polls" % (
        " (cumulative)" if rv else "            ",
        e["ce"], e["ue_polls"], " (UE SEEN)" if e["ue_seen"] else "", fmt_bool(e["ecc_on"], "on", "OFF"),
        fmt_bool(e["poller_alive"], "alive", "NEVER POLLED"), e["polls"]))
    for k in ("first_ce", "first_ue"):
        if e[k]:
            L.append("  %s       : %s" % (k.replace("_", " "), R.ffa_str(e[k])))
    ld, fe = d["link_down"], d["fenced"]
    fw = (rv or {}).get("fence_windows")
    L.append("link-down      : %d%s; fenced DDR accesses %d%s%s" % (
        ld["count"], ", recalibrated after the last" if ld["recalibrated"] else "", fe["count"],
        (" (first: %s at 0x%x)" % ("read" if fe["first"]["read"] else "write", fe["first"]["addr"])) if fe["first"] else "",
        ("; %d inside link-down windows, %d outside%s" % (fw["inside"], fw["outside"],
                                                          ", window OPEN now" if fw["open_now"] else "")) if fw else ""))
    for k, s in st["temperature"].items():
        L.append("temperature    : %-9s %6.2f C  %-10s %s" % (k, s["temp_c"], s["state"], s.get("evidence", "")))
    v = st["voltages"]["sysmon"]
    L.append("voltages       : VCCINT %.3f V, VCCAUX %.3f V, VCCBRAM %.3f V (SYSMON); fan %s RPM" % (
        v["vccint"]["v"], v["vccaux"]["v"], v["vccbram"]["v"], st.get("fan_rpm")))
    if "throttle" in st:
        t = st["throttle"]
        L.append("throttle       : %s; in effect: %s%s" % (
            fmt_bool(t["enabled"], "enabled", "DISABLED"), thr_effect(t),
            ("; temperature tier %s, stale %s, fail-safe %s" % (
                t["temperature_tier"], t["stale"],
                "ENGAGED" if t["failsafe_engaged"] else ("on" if t["failsafe_enabled"] else "OFF"))) if "tier2" in t else ""))
        L.append("  FORCE        : %s (force level if enabled: 1/%d)" % ("ON" if t["force"] else "off", t["force_level"]))
        if "tier2" in t:
            L.append("  auto tiers   : T1=1/%d on %.2f / off %.2f C; T2=1/%d on %.2f / off %.2f C" % (
                t["tier1_level"], t["tier1"]["on_c"], t["tier1"]["off_c"], t["tier2_level"], t["tier2"]["on_c"],
                t["tier2"]["off_c"]))
        else:
            L.append("  auto         : one tier at the FORCE level (1/%d) on %.2f / off %.2f C" % (
                t["force_level"], t["tier1"]["on_c"], t["tier1"]["off_c"]))
        L.append("  now          : %.2f C (code %d), requested %s, DATA_CLK %s, KERNEL_CLK %s; %d starts, %.3f s "
                 "throttled" % (t["temp_c"], t["code"], t["requested"], "THROTTLED" if t["data_clk_throttled"] else "full",
                                "THROTTLED" if t["kernel_clk_throttled"] else "full", t["events"], t["time_throttled_s"]))
    p = st["pcie"]
    L.append("PCIe           : FPGA link %s x%s; card slot %s x%s (port %s); AER %s" % (
        p["fpga_link"]["speed"], p["fpga_link"]["width"], p["card_slot_link"]["speed"], p["card_slot_link"]["width"],
        p["card_slot_link"]["port"], ", ".join("%s=%d" % kv for kv in list(p["aer"].items()) + list(p["slot_aer"].items())
                                               if kv[1]) or "clean"))
    if "ddr_health" in p:
        q = p["ddr_health"]
        L.append("  ddr_health   : %d correctable, %d non-fatal, %d fatal; LTSSM %s; %d speed/width changes" % (
            q["correctable"], q["nonfatal"], q["fatal"], q["ltssm"], q["changes"]))
    if "pr" in st:
        L.append("PR isolations  : %d (~%d xclbin loads: XRT isolates the RP twice per load, a zero-length isolate "
                 "first), the last isolated %.3f ms, RP %s" % (
            st["pr"]["count"], st["pr"]["xclbin_loads_estimate"], st["pr"]["last_isolation_ms"],
            "released" if st["pr"]["rp_released"] else "ISOLATED"))
    L.append("uptime         : %.1f s since configuration" % st["uptime"]["seconds"])
    pp = st["p2p"]
    L.append("P2P            : %s, BAR %s bytes (max %s); host memory %s" % (
        {True: "enabled", False: "disabled", None: "unknown"}[pp.get("enabled")], pp.get("bar4_size"),
        (pp.get("bar_config") or {}).get("max_bar"), pp.get("host_mem_size")))
    pw, al = pp.get("peer_window") or {}, pp.get("plram_alias") or {}
    if pw and not pw.get("absent"):
        L.append("  PEER windows : %s, %d x 2^%d, bases %s; %d writes / %d reads forwarded, %d refused" % (
            fmt_bool(pw["enabled"], "enabled", "disabled"), pw["nwin"], pw["win_log2"], ",".join(pw["bases"]),
            pw["writes"], pw["reads"], pw["refused"]))
    if al and not al.get("absent"):
        L.append("  PLRAM alias  : %s, window %s (2^%d) -> %s; %d writes / %d reads redirected" % (
            fmt_bool(al["enabled"], "enabled", "disabled"), al["alias"], al["win_log2"], al["target"], al["writes"],
            al["reads"]))
    if "recorder" in st:
        r = st["recorder"]
        L.append("flight recorder: %d written (ring of %d, wrapped %d x); last %d:" % (
            r["written"], r["entries"], r["wraps"], len(r["last"])))
        for rec in r["last"]:
            L.append("  %10.6f s %-14s %s" % (rec["time_s"], rec["type"], rec["detail"]))
    if st.get("simulate_hot_pending"):
        L.append("*** a simulate-hot was not restored: innova2-smi throttle restore")
    return "\n".join(L)


def cmd_status(host, args):
    """One-screen health: identity, clocks, DDR/ECC, temperatures, throttle, PCIe, PR, uptime, P2P, recorder.

PR isolations (PR_COUNT, 0x120) count RP isolations, not xclbin loads: XRT isolates the dynamic region twice per
'xbutil program' (a zero-length isolate first, then the reconfiguration), so the count rises by 2 per load and
xclbin loads ~ PR_COUNT / 2. Route V's 'loads' line (RV_LOADS, 0x1C4) counts each load once, plus the RM the flash
image configured at power-on. Route V: until an xclbin is loaded the dynamic region holds that power-on BOOT RM
(r1: the KEPT template, r2: the PRUNED filler); list/status then say 'no xclbin (boot RM: ...)'."""
    if args.concerns:
        return cmd_check(host, args)
    card, sh = open_card(host, args, need_shell=False)
    st = collect_status(host, card, sh)
    out(args, st, render_status(st))
    return 0 if sh.ok else 1


# ----------------------------------------------------------------------------------------------- memory
def _size(n):
    for u, sh in (("GB", 30), ("MB", 20), ("KB", 10)):
        if n >= 1 << sh and n % (1 << sh) == 0:
            return "%d %s" % (n >> sh, u)
    return "%d B" % n


def render_memory(m, prefix="memory: ", detail=False):
    """The memory section: one header line, then one line per bank (status and the memory command)."""
    if not m.get("available"):
        return [prefix + "not available: %s" % m.get("reason")]
    if not m.get("xclbin_loaded"):
        return [prefix + "no xclbin loaded (%s)" % m.get("reason")]
    L = [prefix + "xclbin %s, %d banks (xocl sysfs mem_topology; PRUNED is shown only where verifiable)" % (
        m["xclbin_uuid"], len(m["banks"]))]
    L.append("  %-3s %-8s %-6s %-5s %-14s %-8s %-4s %s" % ("idx", "tag", "kind", "type", "base", "size", "used", "state"))
    for b in m["banks"]:
        cu = sorted({c.split(".arg")[0] for c in b["connections"]})
        L.append("  %-3s %-8s %-6s %-5s %-14s %-8s %-4s %s%s" % (
            b["index"] if not b["slot"] else "%d/%d" % (b["slot"], b["index"]), b["tag"], b["kind"], b["type"],
            b["base"], _size(b["size_bytes"]), "yes" if b["used"] else "no", b["state"],
            ("  [%d CU arg%s: %s]" % (len(b["connections"]), "" if len(b["connections"]) == 1 else "s", ", ".join(cu))) if cu else ""))
        if detail and b.get("evidence"):
            L.append("        evidence: %s" % b["evidence"])
    if detail:
        L.append("  prune marker: %s" % m["prune_marker"]["note"])
    return L


def cmd_memory(host, args):
    """Memory banks of the loaded xclbin: tag, type, base, size, used, the CU arguments on each, readiness, and the
    prune state. Passive: xocl's sysfs copies of mem_topology / ip_layout / connectivity under the user PF plus the
    shell's own registers; no XRT context, no xrt-smi. PRUNED is reported only where it is verifiable (route V's
    DDR, from RV_INFO); an unused PLRAM / HOST / PEER bank is 'unused (may be pruned)': the prune hooks put no marker
    in the xclbin (docs/innova2_smi_prune_marker.md proposes one)."""
    card, sh = open_card(host, args, need_shell=False)
    m = H.xrt_memory(host, card, sh)
    out(args, m, "card %d (%s, %s)\n" % (card["index"], card.get("user") or "-", sh.name or card["state"]) +
        "\n".join(render_memory(m, prefix="memory: ", detail=True)))
    return 0 if m.get("available") else 1


# ----------------------------------------------------------------------------------------------- ddr
def ddr_speed_line(sp):
    """One line: the table's speed and the measured one."""
    t, m = sp.get("table"), sp.get("measured") or {}
    tab = ("%s (%s: tCK %d ps, CL%d/CWL%d)" % (t["speed_grade"], t["variant"], t["tck_ps"], t["cl"], t["cwl"])
           if t else sp.get("table_source"))
    if m.get("ui_clk_mhz"):
        meas = "measured ui_clk %.2f MHz = %.0f MT/s (+-%.2f%%, %.1f s)%s" % (
            m["ui_clk_mhz"], m["mts"], m["precision_pct"], m["window_s"],
            ", %+.2f%% vs table" % m["vs_table_pct"] if "vs_table_pct" in m else "")
    else:
        meas = "not measured: %s" % m.get("reason")
    return "%s; %s" % (tab, meas)


def render_ddr(d):
    L = []
    if not d.get("available"):
        L.append("DDR: not available on this shell (%s)" % d.get("reason"))
        if d.get("xrt_mig_calibration") is not None:
            L.append("  XRT mig_calibration (xclmgmt sysfs): %s" % d["xrt_mig_calibration"])
        L.append("  MIG table: %s" % (d["table_source"]))
        return "\n".join(L)
    c, sp = d["calibration"], d["speed"]
    L.append("calibration : XRT calibration bit %d, gate %s, mode %s (requested %s)%s" % (
        c["xrt_calibration_bit"], "open" if c["gate_open"] else "CLOSED", c["in_force"], c["requested"],
        (", xclbin DDR %s" % c["xclbin_ddr"]) if c.get("xclbin_ddr") else ""))
    t = sp.get("table")
    if t:
        L.append("MIG (table) : %s, %s; %s%s" % (t["variant"], sp["table_source"], t["part"],
                                                ", " + t["part_note"] if t.get("part_note") else ""))
        L.append("  rate      : tCK %d ps = DDR clock %.2f MHz, %s (%.1f MT/s), PHY %d:1, ui_clk %.3f MHz" % (
            t["tck_ps"], t["ddr_clk_mhz"], t["speed_grade"], t["mts"], t["phy_ratio"], t["ui_clk_mhz"]))
        L.append("  timing    : CL %d, CWL %d, tRCD %d, tRP %d, tRAS %d, tRFC %d (DDR4 clocks)" % (
            t["cl"], t["cwl"], t["trcd"], t["trp"], t["tras"], t["trfc"]))
        L.append("  interface : %s, ECC %s, InputClockPeriod %d ps, CA drive %s, ECC poll every %d ui_clk" % (
            t["width"], "on" if t["ecc"] else "off", t["input_clk_ps"], t["ca_drive"], t["poll_cyc"]))
        if t.get("per_xclbin"):
            L.append("  note      : route V -- the MIG is in each KEPT xclbin; these are the template's values")
    else:
        L.append("MIG (table) : %s" % sp["table_source"])
    if sp.get("ddr_info"):
        L.append("DDR_INFO    : " + ", ".join("%s=%s" % kv for kv in sp["ddr_info"].items()))
    m = sp["measured"]
    if m.get("ui_clk_mhz"):
        L.append("measured    : ui_clk %.3f MHz -> %.1f MT/s, +-%.2f%% (%d polls in %.3f s)%s" % (
            m["ui_clk_mhz"], m["mts"], m["precision_pct"], m["polls"], m["window_s"],
            "; %+.2f%% vs the table" % m["vs_table_pct"] if "vs_table_pct" in m else ""))
        L.append("  how       : %s" % m["formula"])
        if m.get("note"):
            L.append("  note      : %s" % m["note"])
    else:
        L.append("measured    : not measured: %s" % m.get("reason"))
    e = d["ecc"]
    L.append("ECC         : %d corrected, %d uncorrectable polls%s, ECC %s, %d polls (cumulative since configuration)" % (
        e["ce"], e["ue_polls"], " (UE SEEN)" if e["ue_seen"] else "", "on" if e["ecc_on"] else "OFF", e["polls"]))
    for k in ("first_ce", "first_ue"):
        if e.get(k):
            L.append("  %-9s : %s" % (k.replace("_", " "), R.ffa_str(e[k])))
    if d.get("ecc_this_load"):
        el = d["ecc_this_load"]
        L.append("  this load : %d corrected, %d uncorrectable polls%s" % (el["ce"], el["ue_polls"],
                                                                         " (UE SEEN)" if el["ue_seen"] else ""))
    return "\n".join(L)


def cmd_ddr(host, args):
    """DDR configuration and speed: calibration, mode, the build's MIG parameters (a per-build table: no shell reports
    its MIG configuration), the MIG ui_clk MEASURED passively from the ECC poller's poll count against the 250 MHz
    uptime counter (MT/s = 8 x ui_clk), and the ECC totals. Loads only; no XRT context."""
    card, sh = open_card(host, args, need_shell=False)
    if not sh.ok:
        mc = host.attr(card["mgmt"], "mig_calibration") if card.get("mgmt") else None
        d = dict(available=False, reason=sh.reason or card["state"], xrt_mig_calibration=mc,
                 table=R.mig_config(sh.rom_ts) if sh.rom_ts else None,
                 table_source="build table (ROM_TS %d)" % sh.rom_ts if R.mig_config(sh.rom_ts) else
                 "unknown build (ROM_TS %s)" % sh.rom_ts)
        out(args, d, render_ddr(d))
        return 1
    d = dict(available=True, calibration=H.ddr_state(sh), speed=H.ddr_speed(sh, window=args.window), ecc=H.ecc(sh))
    rv = H.routev(sh)
    if rv:
        d["calibration"]["calibrated"] = rv["calibrated"]
        d["ecc_this_load"] = rv["ecc_this_load"]
    out(args, d, "card %d (%s, %s)\n" % (card["index"], sh.mgmt, sh.name) + render_ddr(d))
    return 0


# ----------------------------------------------------------------------------------------------- check
def cmd_check(host, args):
    """Passive concerns-only check (see innova2_smi_check)."""
    if getattr(args, "rules", False):
        print(C.rules_table())
        return 0
    cs = cards(host, args)
    if not cs:
        print("FAIL: no Innova-2 card found")
        return 2
    sel = cs if (args.all or (args.device is None and len(cs) > 1)) else [H.select_card(cs, args.device)]
    interval = 0 if getattr(args, "since_boot", False) else args.interval
    res = [C.run_check(host, c, interval=interval, expect_mode=args.expect_mode, query_cx5=not args.no_cx5,
                       recorder=getattr(args, "recorder", False)) for c in sel]
    if args.json:
        print(H.jdump(res if len(res) > 1 else res[0]))
    else:
        print("\n".join(C.render(r, verbose=args.verbose) for r in res))
    return max(C.SEV_ORDER[r["severity"]] for r in res)


# ----------------------------------------------------------------------------------------------- ecc-baseline
def cmd_ecc_baseline(host, args):
    """Record each card's ECC counts (CE, UE polls), link-down count and FPGA uptime in
    ~/.cache/innova2-smi/ecc_baseline_<mgmt BDF>.json. From then on 'check' judges only the errors SINCE the
    baseline (a new UE is a FAIL again, a new CE a WARN) and shows the totals as INFO. The ECC counters themselves
    are cumulative since configuration and cannot be cleared from the host; take a baseline after a warm reboot that
    read stale pre-reboot data, once the buffers have been re-filled. Host-side only: the card is only READ.
    A baseline is used only for the same card, shell and configuration (a cold boot or reflash voids it).
    --show prints the stored baseline; --remove deletes it."""
    cs = cards(host, args)
    if not cs:
        raise H.Refused("no Innova-2 card found")
    sel = cs if (args.device is None and len(cs) > 1) else [H.select_card(cs, args.device)]
    rows, rc = [], 0

    def say(text):
        if not args.json:
            print(text)
    for c in sel:
        sh = H.Shell(host, c)
        if c["state"] == "OUR_SHELL?":
            sh.identify()
            c["state"] = sh.state
        who = "card %s (FPGA %s, %s)" % (c["index"], c.get("mgmt") or "-", sh.name or c["state"])
        if not sh.ok:
            rows.append(dict(card=c["index"], error=sh.reason or c["state"]))
            print("%s: no baseline: %s" % (who, sh.reason or c["state"]), file=sys.stderr)
            rc = 1
            continue
        if args.remove:
            path = H.ecc_baseline_path(sh)
            existed = os.path.exists(path)
            if existed:
                os.remove(path)
            rows.append(dict(card=c["index"], file=path, removed=existed))
            say("%s: %s %s" % (who, "removed" if existed else "no baseline at", path))
            continue
        if args.show:
            path, b, why = H.load_ecc_baseline(sh)
            rows.append(dict(card=c["index"], file=path, baseline=b, error=why))
            say("%s: %s" % (who, ("%s: %s" % (path, why)) if why else ("no baseline (%s)" % path) if not b else
                              "%s: CE %d, UE polls %d, link-downs %d at FPGA uptime %.1f s (%s)" % (
                                  path, b["ce"], b["ue_polls"], b["link_down"], b["uptime_s"],
                                  time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b.get("written_at", 0))))))
            continue
        path, b = H.save_ecc_baseline(sh)
        rows.append(dict(card=c["index"], file=path, baseline=b))
        say("%s: baseline %s: CE %d, UE polls %d, link-downs %d at FPGA uptime %.1f s -- 'check' now counts only "
              "ECC errors after this" % (who, path, b["ce"], b["ue_polls"], b["link_down"], b["uptime_s"]))
    if args.json:
        print(H.jdump(rows if len(rows) > 1 else rows[0]))
    return rc


# ----------------------------------------------------------------------------------------------- counters
def render_counters(cs, deltas=None):
    L = ["%-11s %-22s %-16s %-7s %-36s %-12s %14s %-4s %s" % ("region", "offset", "register", "field", "name", "raw",
                                                             "value", "unit", "description")]
    for e in cs:
        k = H.counter_key(e)
        dv = ""
        if deltas is not None and k in deltas:
            dv = "  (%+g)" % deltas[k]
        L.append("%-11s %-22s %-16s %-7s %-36s %-12s %14s %-4s %s%s" % (
            e["region"], e["offset"].replace("sysfs ", "")[:22], e["register"][:16], e["field"], e["name"][:36], e["raw"][:12],
            e["value"], e["unit"], e["description"], dv))
    return "\n".join(L)


def cmd_counters(host, args):
    """Every counter the running shell exposes, from its OBS_ID register map (plus SYSMON, XRT clocks, cmc sensors,
    PEER/alias counters, AER). --watch N re-reads every N seconds and shows deltas."""
    card, sh = open_card(host, args)
    prev = H.counters(sh)
    if not args.watch:
        out(args, prev, render_counters(prev))
        return 0
    try:
        if args.json:
            print(json.dumps(dict(t=time.time(), counters=prev), default=str), flush=True)
        else:
            print(render_counters(prev), flush=True)
        while True:
            time.sleep(args.watch)
            cur = H.counters(sh)
            d = H.counter_deltas(prev, cur)
            if args.json:
                print(json.dumps(dict(t=time.time(), deltas={"/".join(k): v for k, v in d.items()}, counters=cur),
                                 default=str), flush=True)
            else:
                print("\n=== %s  (%d changed in %g s)" % (time.strftime("%H:%M:%S"), len(d), args.watch))
                print(render_counters([e for e in cur if H.counter_key(e) in d] if not args.all_rows else cur, d),
                      flush=True)
            prev = cur
    except KeyboardInterrupt:
        return 0


# ----------------------------------------------------------------------------------------------- recorder
def cmd_recorder(host, args):
    """Decode the flight recorder (E7a2+): the last N records (all by default). Uses the recorder's read-port index
    register (FR_IDX), which selects a record and changes nothing else."""
    card, sh = open_card(host, args)
    inf, recs = H.recorder(sh, last=args.n)
    text = ["flight recorder: %d entries, %d written, wrapped %d x" % (inf["entries"], inf["written"], inf["wraps"]),
            "%-8s %-16s %-14s %s" % ("index", "time (s)", "event", "detail")]
    text += ["%-8d %-16.6f %-14s %s" % (r["index"], r["time_s"], r["type"], r["detail"]) for r in recs]
    out(args, dict(info=inf, records=recs), "\n".join(text))
    return 0


# ----------------------------------------------------------------------------------------------- controls
def cmd_clear(host, args):
    """Zero the observability counters (OBS_ID write 0xC1EAC1EA). ECC/link-down counts, uptime, throttle settings and
    the flight recorder are never cleared."""
    card, sh = open_card(host, args)
    if not confirm(args, "clear the observability counters of card %d (%s)?" % (card["index"], sh.mgmt)):
        return 1
    H.clear_counters(sh)
    print("cleared (lock losses, fenced accesses%s; ECC / link-down / uptime / throttle settings / recorder kept)" %
          (", throttle/PCIe/PR counters and timestamps" if sh.lay["e7"] else ""))
    return 0


def cmd_ddr_mode(host, args):
    """Show or set the DDR mode: on | off | emu | hybrid (the register resets to ON at every cold boot)."""
    card, sh = open_card(host, args)
    rv = R.is_routev(sh.lay)
    if not args.mode:
        st = H.ddr_state(sh)
        if rv:
            st["available_modes"] = ["on", "off"]
            st["not_available"] = {m: "deferred on route V (DDR is built into each xclbin)" for m in R.ROUTEV_DEFERRED_MODES}
        out(args, st, "DDR mode requested %s, in force %s, MIG %s, gate %s, XRT calibration bit %d%s" % (
            st["requested"], st["in_force"], "held" if st["mig_held"] else "running",
            "open" if st["gate_open"] else "closed", st["xrt_calibration_bit"],
            ("; xclbin DDR %s; modes on, off (emu, hybrid: not available -- deferred on route V)" % st["xclbin_ddr"])
            if rv else ""))
        return 0
    if rv and args.mode in R.ROUTEV_DEFERRED_MODES:
        raise H.Refused("DDR mode '%s' is not available on route V (deferred): the DDR is built into each xclbin in "
                        "the dynamic region and the static gate runs on/off only; nothing was written" % args.mode)
    if not confirm(args, "set the DDR mode of card %d (%s) to '%s'? Loaded kernels' DDR buffers change underneath "
                   "them" % (card["index"], sh.mgmt, args.mode)):
        return 1
    st = H.set_ddr_mode(sh, R.MODES[args.mode])
    out(args, st, "DDR mode in force %s, gate %s, XRT calibration bit %d" % (
        st["in_force"], "open" if st["gate_open"] else "closed", st["xrt_calibration_bit"]))
    return 0


class _Stop(Exception):
    pass


def _thr_line(sh):
    st = sh.r(0xC8)
    if sh.lay["e7a3"]:
        tier = (" in effect %s (not throttled)," % R.TIER_NAMES[st >> 4 & 3] if not st >> 1 & 1 else
                " in effect %s at 1/%d," % ("FORCE" if sh.r(0xC0) >> 1 & 1 else R.TIER_NAMES[st >> 4 & 3], R.lvl(st >> 8)))
    else:
        tier = ""
    return "%.2f C (code %d),%s requested %d, DATA_CLK %s, KERNEL_CLK %s (THR_STATUS 0x%08x)" % (
        R.code2c(st >> 16 & 0x3FF), st >> 16 & 0x3FF, tier, st >> 1 & 1, "throttled" if st >> 2 & 1 else "full",
        "throttled" if st >> 3 & 1 else "full", st)


def cmd_throttle(host, args):
    """Show or change the thermal throttle (E7a2+). See 'innova2-smi throttle -h'."""
    card, sh = open_card(host, args)
    sh.need("e7")
    a = args.action or ["show"]
    if a[0] == "show":
        t = H.throttle(sh)
        out(args, t, _thr_line(sh) + "\n" + "\n".join("  %s: %s" % kv for kv in t.items()))
        return 0
    if a[0] == "simulate-hot":
        return simulate_hot(sh, args, a[1:])
    if a[0] == "restore":
        path, d = H.load_thr_state(sh)
        if not d:
            print("nothing to restore (%s does not exist)" % path)
            return 0
        if d["rom_ts"] != sh.rom_ts or d["obs_id"] != sh.obs_id:
            raise H.Refused("%s was saved on another shell (ROM_TS %s, OBS_ID 0x%08X); not restoring onto this one"
                            % (path, d["rom_ts"], d["obs_id"]))
        saved = {int(k, 16): v for k, v in d["saved"].items()}
        H.restore_thr(sh, saved)
        os.remove(path)
        print("restored %s from %s" % (", ".join("0x%x=0x%08x" % kv for kv in saved.items()), path))
        print("now: " + _thr_line(sh))
        return 0
    # direct controls (FORCE bypasses the automatic tiers)
    if a[0] == "force" and len(a) == 2 and a[1] in ("2", "4"):
        a = ["force-level", a[1]]
    if a[0] == "force-level":
        c = sh.r(0xC0)
        regs = {0xC0: (c & ~0xE) | 2 | (4 if a[1] == "4" else 0)}
    elif a[0] == "off" and len(a) == 1:
        regs = {0xC0: sh.r(0xC0) & ~2}
    else:
        regs = H.throttle_change(sh, a)
    desc = ", ".join("0x%x: 0x%08x -> 0x%08x" % (R.STATUS + o, sh.r(o), v) for o, v in regs.items())
    if not confirm(args, "throttle %s on card %d: %s?" % (" ".join(args.action), card["index"], desc)):
        return 1
    H.throttle_write(sh, regs)
    time.sleep(0.2)
    print(("FORCE bypasses the automatic temperature path. " if a[0] in ("force", "force-level") else "") +
          "now: " + _thr_line(sh))
    return 0


def simulate_hot(sh, args, rest):
    """Make the AUTOMATIC throttle react as if the FPGA were hot, by moving the tier thresholds below the live
    temperature. The shell has no temperature-override register, so no fake sensor value is injected."""
    if len(rest) != 1 or rest[0] not in ("tier1", "tier2"):
        raise H.Refused("usage: throttle simulate-hot tier1|tier2 [--seconds S] [--xclbin X]")
    tier = rest[0]
    if not sh.lay["e7a3"] and tier == "tier2":
        raise H.Refused("%s has a single automatic tier: use 'simulate-hot tier1' (it moves THR_THRESH below the "
                        "live code)" % sh.name)
    c = sh.r(0xC0)
    if not c & 1:
        raise H.Refused("the automatic throttle is disabled (THR_CTRL 0x%02x): 'throttle enable on' first" % c)
    if c >> 1 & 1:
        raise H.Refused("FORCE is on (THR_CTRL 0x%02x): the automatic path is overridden; 'throttle off' first" % c)
    code, plan = H.sim_hot_plan(sh, tier)
    saved = H.throttle_regs(sh)
    lp = None
    if args.xclbin:
        T.xclbin_check(sh, args.xclbin)
        from innova2_smi_xrt import LatProbe, ensure_xrt_env
        ensure_xrt_env()
    print("simulate-hot %s: live temperature %.2f C (code %d). Heat is SIMULATED by moving thresholds below it:" % (
        tier, R.code2c(code), code))
    for o, v in plan.items():
        print("  0x%05x %s 0x%08x -> 0x%08x (SET %d = %.2f C, REL %d = %.2f C)" % (
            R.STATUS + o, R.LAYOUTS[sh.obs_id]["regs"][o][0], saved[o], v, v & 0x3FF, R.code2c(v & 0x3FF),
            v >> 16 & 0x3FF, R.code2c(v >> 16 & 0x3FF)))
    if not confirm(args, "apply for %g s, then restore the exact current values?" % args.seconds):
        return 1
    if args.xclbin:
        lp = LatProbe(sh, args.xclbin, args.cu)
        base = lp.mhz()
        print("DATA_CLK measured before: %.2f MHz" % base)
    path = H.save_thr_state(sh, saved, extra=dict(tier=tier, plan={"0x%x" % o: v for o, v in plan.items()}))
    print("saved the previous values to %s ('innova2-smi throttle restore' recovers them after a crash)" % path)

    def on_sig(signum, frame):
        raise _Stop()
    old = {s: signal.signal(s, on_sig) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    wp0 = sh.r(0x184)
    want = {"tier1": "tier 1", "tier2": "tier 2"}[tier]
    rc = 0
    try:
        H.throttle_write(sh, plan)
        t0 = time.time()
        while time.time() - t0 < 2.0:
            st = sh.r(0xC8)
            if (sh.lay["e7a3"] and R.TIER_NAMES[st >> 4 & 3] == want) or (not sh.lay["e7a3"] and st >> 1 & 1):
                break
            time.sleep(0.1)
        print("entered after %.1f s: %s" % (time.time() - t0, _thr_line(sh)))
        if lp:
            m = lp.mhz()
            print("DATA_CLK measured now: %.2f MHz (1/%.2f of %.2f)" % (m, base / m if m else 0, base))
        t_end = time.time() + args.seconds
        while time.time() < t_end:
            time.sleep(min(1.0, max(0.0, t_end - time.time())))
            print("  %s" % _thr_line(sh), flush=True)
    except _Stop:
        print("\ninterrupted -- restoring")
        rc = 130
    finally:
        for s, h in old.items():
            signal.signal(s, signal.SIG_IGN)
        try:
            H.restore_thr(sh, saved)
            os.remove(path)
            print("restored %s" % ", ".join("0x%05x=0x%08x" % (R.STATUS + o, v) for o, v in saved.items()))
        except Exception as e:                               # noqa: BLE001
            print("*** RESTORE FAILED (%s): the saved values stay in %s -- run 'innova2-smi throttle restore'" % (e, path))
            rc = 1
        for s, h in old.items():
            signal.signal(s, h)
    t0 = time.time()
    while time.time() - t0 < 3.0:
        st = sh.r(0xC8)
        if not st >> 1 & 1:
            break
        time.sleep(0.1)
    st = sh.r(0xC8)
    print("%s after %.1f s: %s" % ("released" if not st >> 1 & 1 else "*** STILL THROTTLED", time.time() - t0,
                                   _thr_line(sh)))
    if lp:
        print("DATA_CLK measured after: %.2f MHz" % lp.mhz())
    wp1 = sh.r(0x184)
    inf = H.recorder_info(sh)
    idxs = [(wp0 + k) % inf["entries"] for k in range((wp1 - wp0) % inf["entries"])]
    recs = [H.read_record(sh, i) for i in idxs]
    tr = [r for r in recs if r["type"] in ("THR_TIER", "THR_START", "THR_STOP", "TEMP_STALE", "TEMP_FRESH")]
    print("flight recorder (%d new records, throttle entries below):" % len(recs))
    for r in tr:
        print("  %10.6f s %-10s %s" % (r["time_s"], r["type"], r["detail"]))
    return rc if rc else (0 if not st >> 1 & 1 else 1)


# ----------------------------------------------------------------------------------------------- tests
def cmd_test(host, args):
    """Active tests; each prints PASS / FAIL / SKIP / INCONCLUSIVE with its evidence."""
    names = T.ALL_ORDER if args.name == "all" else [args.name]
    card, sh = open_card(host, args)

    def on_term(signum, frame):                 # SIGTERM/SIGHUP run the same restore paths as Ctrl-C
        raise KeyboardInterrupt()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_term)
    ecc_marker = os.path.exists(os.path.join(H.state_dir(), "ecc_arm_%s.json" % sh.mgmt.replace(":", "_")))

    def loads(n):                               # does this test load an xclbin / use DDR?
        if n == "ecc-live":
            return bool(args.arm or ecc_marker)
        return T.TESTS[n]["writes"]
    if any(T.TESTS[n]["xrt"] for n in names) or args.name == "all" or (args.name == "ecc-live" and loads("ecc-live")):
        from innova2_smi_xrt import ensure_xrt_env
        try:
            ensure_xrt_env()
        except H.Refused as e:
            if args.name != "all":
                raise
            print("note: %s (the pyxrt tests will be skipped)" % e)
    writes = [n for n in names if loads(n)]
    if writes and not confirm(args, "run %s on card %d (%s)? %s xclbins and %s DDR (flash is never written)"
                              % (", ".join(writes), card["index"], sh.user,
                                 "They load" if len(writes) > 1 else "It loads", "use" if len(writes) > 1 else "uses")):
        return 1
    ask = lambda p: confirm(args, p)
    results = []
    for n in names:
        fn = T.TESTS[n]["fn"]
        try:
            if args.name == "all" and n == "throttle" and not args.xclbin:
                r = T.result(n, "SKIP", "needs --xclbin (a lat_probe xclbin for this shell)")
            elif args.name == "all" and n == "flash-verify" and not (args.mcs_prefix or args.primary):
                r = T.result(n, "SKIP", "needs --primary/--secondary or --mcs-prefix")
            elif args.name == "all" and n == "throttle" and not sh.lay["e7"]:
                r = T.result(n, "SKIP", "no throttle on this shell")
            elif n in ("temp-live", "ecc-live"):
                r = fn(sh, args)
            elif n == "flash-verify":
                r = fn(sh, args)
            else:
                r = fn(sh, args, ask)
        except H.Refused as e:
            r = T.result(n, "SKIP" if args.name == "all" else "FAIL", "refused: %s" % e)
        results.append(r)
        if not args.json:
            print("%-13s %-12s %s" % (r["name"], r["status"], r["summary"]))
            for line in r["evidence"]:
                print("    " + line)
            sys.stdout.flush()
    if args.json:
        print(H.jdump(results))
    st = {r["status"] for r in results}
    return 1 if "FAIL" in st else (2 if "INCONCLUSIVE" in st and "PASS" not in st else 0)


# ----------------------------------------------------------------------------------------------- regmap
def cmd_regmap(host, args):
    """The register maps this tool uses (generated documentation; RTL cross-check)."""
    if args.check_rtl:
        rc = 0
        for p in args.check_rtl:
            oid, probs = R.check_rtl(open(p).read())
            print("%s: OBS_ID %s -- %s" % (p, "0x%08X" % oid if oid is not None else "?",
                                           "matches the map" if not probs else "; ".join(probs)))
            rc |= bool(probs)
        return rc
    if args.markdown:
        print(R.markdown())
        return 0
    lays = {args.obs_id: R.LAYOUTS[args.obs_id]} if args.obs_id else R.LAYOUTS
    obj = {"0x%08X" % k: dict(name=v["name"], released=v["released"],
                              regs={"0x%03X" % o: dict(name=r[0], access=r[1], description=r[2]) for o, r in
                                    sorted(v["regs"].items())}) for k, v in lays.items()}
    if args.json:
        print(H.jdump(obj))
    else:
        for k, v in obj.items():
            print("%s %s" % (k, v["name"]))
            for o, r in v["regs"].items():
                print("  %s %-18s %-3s %s" % (o, r["name"], r["access"], r["description"]))
    return 0


# ----------------------------------------------------------------------------------------------- argparse
THROTTLE_HELP = """actions (E7a2 and later; state changes ask for confirmation unless --yes):
  show                                 (default) the throttle registers, decoded
  simulate-hot tier1|tier2 [--seconds S] [--xclbin X]
        exercise the REAL automatic path: read the live temperature code, move THR_THRESH (tier 1) or THR_THRESH2
        (tier 2) BELOW it so the state machine itself enters the tier, show the tier, the delivered clock (kclk_gate's
        measured flags; with --xclbin a lat_probe measurement) and the recorder's THR_TIER entries, then restore the
        exact previous values after S seconds or on Ctrl-C and confirm the release. This SIMULATES heat by moving
        thresholds; it cannot inject a sensor value -- the shell has no temperature-override register. The saved
        values go to ~/.cache/innova2-smi/ first, so 'restore' recovers them after a crash. E7a2: tier1 only (one tier).
  restore                              write back the values saved by an interrupted simulate-hot
  force 2|4 | off                      FORCE the throttle on at 1/2 or 1/4, or off -- BYPASSES the automatic path
  force on|off | level 2|4             (innova2_ddr_mode compatible) FORCE bit / FORCE level
  enable on|off | gate data|kernel|both
  thresholds <on C> <off C>            tier 1 (E7a2: the only tier)
  thresholds2 <on C> <off C>           tier 2 (E7a3)
  tierlevel 1|2 2|4                    divide tier 1 / tier 2 by 2 or 4 (E7a3)
  failsafe on|off                      stale temperature -> tier 2 (E7a3)
  defaults                             the power-on values
"""

TEST_HELP = """tests:
""" + "\n".join("  %-13s %s%s" % (k, v["doc"], " [loads xclbins/uses DDR: --yes]" if v["writes"] else "")
                for k, v in T.TESTS.items()) + """
  all           every test whose prerequisites are given, in this order: %s

Prerequisites: XRT installed; for ddr-soak / ecc-live --arm / throttle: pyxrt (innova2-smi sets XRT's environment
itself). Every xclbin is checked against the running shell's ROM timestamp and VBNV before it is loaded.
The throttle test needs a lat_probe xclbin linked for THIS shell: the shell build's kernels stage links
kernels/regress_plram.cfg (four lat_probe CUs: lp_ddr on bank0, lp_p0..2 on PLRAM) into
<build>/pkgwork/kernels/regress_plram.xclbin; patch it like the validate kernels (the hardware-test copies are
<build>/hwtest_k/regress_plram_prog.xclbin) and pass it with --xclbin; --cu selects the CU (default lp_ddr).
""" % ", ".join(T.ALL_ORDER)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-d", "--device", help="card: index, any BDF of the card (ConnectX or FPGA, e.g. 08:00) or "
                                               "the board serial")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--yes", "-y", action="store_true", help="answer confirmations with yes")
    common.add_argument("--no-cx5", action="store_true", help="do not query the ConnectX-5 for the FPGA image")
    common.add_argument("--transport", default="auto", choices=["auto", "kmod", "mlxreg"],
                        help="ConnectX register transport, as innova2_app (default auto)")
    p = argparse.ArgumentParser(prog="innova2-smi", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Run '<command> -h' for each command.")
    p.add_argument("--version", action="version", version="innova2-smi " + _version())
    sp = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help_, **kw):
        q = sp.add_parser(name, parents=[common], help=help_, description=fn.__doc__ or help_,
                          formatter_class=argparse.RawDescriptionHelpFormatter, **kw)
        q.set_defaults(fn=fn)
        return q
    q = add("list", cmd_list, "every Innova-2 card and its FPGA state")
    q.add_argument("--load-module", action="store_true", help="modprobe mlx5_fpga_tools / innova2_areg when the "
                                                              "ConnectX node is missing (needs --yes)")
    q = add("status", cmd_status, "one-screen health of a card")
    q.add_argument("--concerns", action="store_true", help="alias of 'check'")
    q.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    q.add_argument("--interval", type=float, default=1.0, help=argparse.SUPPRESS)
    q.add_argument("--since-boot", action="store_true", help=argparse.SUPPRESS)
    q.add_argument("--expect-mode", default="on", help=argparse.SUPPRESS)
    q.add_argument("-v", "--verbose", action="store_true", help=argparse.SUPPRESS)
    q = add("check", cmd_check, "passive concerns-only check; exit 0 OK, 1 WARN, 2 FAIL",
            epilog="Strictly passive: register LOADS through the shell's map, sysfs, the xrt-smi device table. No "
                   "store (not even the recorder index), no xclbin, no DMA, no XRT context -- unless --recorder. "
                   "'check --rules' lists every rule, its severity, threshold and hint.")
    q.add_argument("--all", action="store_true", help="every card (default when -d is not given and there are several)")
    g = q.add_mutually_exclusive_group()
    g.add_argument("--interval", type=float, default=1.0, help="seconds between the two snapshots (default 1)")
    g.add_argument("--since-boot", action="store_true", help="one snapshot: absolute values and events since boot")
    q.add_argument("--expect-mode", default="on", choices=["on", "off", "emu", "hybrid", ""],
                   help="the DDR mode this card should be in (default on; '' = any)")
    q.add_argument("-v", "--verbose", action="store_true", help="also show INFO (known benign) items")
    q.add_argument("--rules", action="store_true", help="print the rules table and exit")
    q.add_argument("--recorder", action="store_true",
                   help="when DDR accesses were fenced, read the flight recorder (stores to its index register; no "
                        "longer strictly passive) to place every fence in a link-down window. Not needed on route V "
                        "r2 (OBS_ID 0x0B5E000B): its FENCE_LDWIN / FENCE_OUTWIN counters place them from registers")
    q = add("ecc-baseline", cmd_ecc_baseline, "record ECC counts now; 'check' then judges only new errors")
    g = q.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help="print the stored baseline")
    g.add_argument("--remove", action="store_true", help="delete the stored baseline (check judges the totals again)")
    q = add("ddr", cmd_ddr, "DDR configuration, measured speed and ECC totals")
    q.add_argument("--window", type=float, default=1.5, help="measurement window in seconds (default 1.5; the "
                                                              "precision is about 2/(1000 x window) + 0.08 %%)")
    add("memory", cmd_memory, "memory banks of the loaded xclbin (used / ready / PRUNED)")
    q = add("counters", cmd_counters, "every counter the running shell exposes")
    q.add_argument("--watch", type=float, metavar="N", help="re-read every N seconds and show deltas (Ctrl-C stops)")
    q.add_argument("--all-rows", action="store_true", help="with --watch: print every row, not only changed ones")
    q = add("recorder", cmd_recorder, "decode the flight recorder (E7a2+)")
    q.add_argument("-n", type=int, default=None, help="only the last N records")
    add("clear", cmd_clear, "zero the observability counters (confirmation)")
    q = add("ddr-mode", cmd_ddr_mode, "show or set the DDR mode (confirmation)")
    q.add_argument("mode", nargs="?", choices=list(R.MODES), help="on | off | emu | hybrid")
    q = add("throttle", cmd_throttle, "show / control / simulate-hot the thermal throttle (E7a2+)", epilog=THROTTLE_HELP)
    q.add_argument("action", nargs="*", help="see below")
    q.add_argument("--seconds", type=float, default=10.0, help="simulate-hot duration (default 10)")
    q.add_argument("--xclbin", help="simulate-hot: lat_probe xclbin for a measured clock")
    q.add_argument("--cu", default="lp_ddr", help="lat_probe CU (default lp_ddr)")
    q = add("test", cmd_test, "active tests", epilog=TEST_HELP)
    q.add_argument("name", choices=list(T.TESTS) + ["all"])
    q.add_argument("--xclbin", help="xclbin for ddr-soak/ecc-live (default: this shell's verify.xclbin) or the "
                                    "lat_probe xclbin for throttle")
    q.add_argument("--cu", default="lp_ddr", help="throttle: lat_probe CU on DATA_CLK (default lp_ddr)")
    q.add_argument("--tolerance", type=float, default=0.03, help="throttle: clock ratio tolerance (default 0.03)")
    q.add_argument("--run", default="all", help="validate: xrt-smi validate -r list (default all)")
    q.add_argument("--passes", type=int, default=3, help="ddr-soak: check passes after the fill (default 3)")
    q.add_argument("--chunk-mb", type=int, default=256, help="ddr-soak/ecc-live: chunk size (default 256)")
    q.add_argument("--size-mb", type=int, default=0, help="ddr-soak/ecc-live: MB of bank0 to use (default: all)")
    q.add_argument("--max-ce", type=int, default=0, help="ddr-soak: corrected errors tolerated (default 0)")
    q.add_argument("--loads", type=int, default=10, help="pr-stress: xclbin loads (default 10)")
    q.add_argument("--xclbins", type=lambda s: s.split(","), help="pr-stress: comma-separated xclbins to alternate")
    q.add_argument("--no-throttled", dest="throttled", action="store_false",
                   help="pr-stress: do not force the throttle on for the second half")
    q.add_argument("--arm", action="store_true", help="ecc-live: fill bank0 and leave a marker for after a warm reboot")
    q.add_argument("--window", type=float, default=3.0, help="temp-live: observation window (default 3 s)")
    q.add_argument("--primary", help="flash-verify: chip-0 image (.bin)")
    q.add_argument("--secondary", help="flash-verify: chip-1 image (.bin)")
    q.add_argument("--mcs-prefix", help="flash-verify: <prefix>_primary.mcs / _secondary.mcs instead of .bin files")
    q.add_argument("--offset", type=lambda s: int(s, 0), default=0x01000000,
                   help="flash-verify: flash address of the .bin images (default 0x01000000, the User slot)")
    q = add("regmap", cmd_regmap, "the register maps this tool uses")
    q.add_argument("--markdown", action="store_true", help="the register-map document (docs/innova2_smi_regmap.md)")
    q.add_argument("--check-rtl", nargs="+", metavar="DDR_HEALTH_V", help="compare with ddr_health.v read muxes")
    q.add_argument("--obs-id", type=lambda s: int(s, 0), help="one layout only")
    return p


def main(argv=None, host=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 1
    host = host or H.Host()
    if args.cmd != "regmap" and not host.is_root():
        print("*** run as root (sysfs resources, VPD and the ConnectX node need it)", file=sys.stderr)
        return 1
    try:
        return args.fn(host, args)
    except H.Refused as e:
        print("*** %s" % e, file=sys.stderr)
        return 1
    except H.UnsafeAccess as e:
        print("*** refused before touching the bus: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
