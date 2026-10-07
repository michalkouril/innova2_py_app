#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Register maps of the Innova-2 XRT shell (fp8c lineage), as data, plus the decoders innova2-smi uses.

Pure Python, no hardware access: innova2_smi.py, the unit tests and the generated register-map document
(docs/innova2_smi_regmap.md, `innova2-smi regmap --markdown`) all read the tables below. `innova2-smi regmap
--check-rtl <ddr_health.v>` compares them against the read multiplexer of the RTL, which is the authority
(ip_src/ddr_health/ddr_health.v in the shell source tree).

SAFETY MODEL. An undecoded management-PF read hard-locks the host (the endpoint answers Unsupported Request; see the
shell notes). So innova2-smi never reads an address that is not listed here for the running shell's layout, and it
learns the layout from the OBS_ID register before it reads anything else. An OBS_ID that is not in LAYOUTS stops all
further register access ("fail closed").

All fp8c builds share ONE management address map (measured: amap_this_build.txt is identical for e6b, e6b2400dw8,
e7a2, e7a3_2400 and e7a3_2666ca48), so the regions below are the same for every supported layout; what differs per
OBS_ID is which ddr_health offsets exist.

ROUTE V (OBS_ID 0x0B5E0008, VBNV innova2_innova2_noddr_202320_3): the DDR4 MIG is in the dynamic region, built into
an xclbin only when its kernels use DDR (KEPT) and pruned otherwise (PRUNED). Its management map is the E7a3 one minus
the static MIG's ECC master (nothing the tool reads; build gate AMAP), and ddr_health adds a block at 0x1C0..0x1F8
(KEPT/PRUNED, calibration, per-load ECC, fences while KEPT). EMU/HYBRID are deferred on it (the gate runs on/off).

ROUTE V r2 (OBS_ID 0x0B5E000B, same VBNV, own ROM_TS): the route-V map plus 0x218..0x220 -- fenced DDR accesses split
by link-down window (FENCE_LDWIN + FENCE_OUTWIN = FENCE), so `check` places a warm reboot's fenced write from the
registers alone. Its flash image boots with the PRUNED filler as the dynamic region (no MIG until a KEPT xclbin loads).

ROUTE V r3 (OBS_ID 0x0B5E000D, same VBNV, ROM_TS 1792486800): register map and ddr_health RTL identical to r2; the ID
marks the r3 static (RP-side PEER/HOST kernel-clock pipelining, PLRAM controller pblocks, PLRAM2 read latency 4) and its
link-default CA drive RDRV_60_60 (the template xdc still says RDRV_48_48; the link hook switches it).

FP8C RC1 (OBS_ID 0x0B5E000C, VBNV innova2_innova2_xdma_202320_3, ROM_TS 1792483200): E7a3-2666ca60 (static MIG, DDR4-2666,
CA RDRV_60_60) plus the same fence-window counters at 0x218..0x220 (the route-V r2 RTL ported to E7a3); no route-V block.
"""

import math

# ----------------------------------------------------------------------------------------------- platform identity
VBNV_FP8C = "innova2_innova2_xdma_202320_3"       # fp8c and later (PLATFORM_VENDOR innova2)
VBNV_OLD = "xilinx_innova2_xdma_202320_3"          # pre-fp8c production shells (inn2x r3..r7): identity only
VBNV_ROUTEV = "innova2_innova2_noddr_202320_3"     # route V: DDR4 MIG in the dynamic region (per xclbin)
VBNVS = (VBNV_FP8C, VBNV_ROUTEV)                   # platforms whose ddr_health map this tool reads
FPGA_VENDOR, FPGA_MGMT_DEV, FPGA_USER_DEV = "0x10ee", "0x788f", "0x6a90"
CX5_VENDOR, BOPE_DEV = "0x15b3", "0x0264"

# Builds this tool knows by ROM timestamp (ROM_TS = the feature-ROM TimeSinceEpoch = rom.m.*/timestamp = the
# xclbin's m_featureRomTimeStamp). Several builds share an OBS_ID, so the ROM_TS is what names the build; the OBS_ID
# is what selects the register map. A build that is not listed here is still handled if its OBS_ID is known.
# uuid = the interface uuid, checked 2026-10-06 against each build's release/boot.xsabin and bandwidth.xclbin axlf
# headers (m_interface_uuid) and chain.out. It is only a cross-check: status/check read the live value from the
# xocl / xclmgmt `interface_uuids` sysfs attribute, else from the header of the boot xsabin xclmgmt loaded for this
# ROM_TS (XRT 2.17 answers EINVAL for these shells: their device tree carries no interface_uuid property).
# Route V: none released yet, so None.
SHELLS = {
    1792022400: dict(name="E6b", obs_id=0x0B5E0002, uuid="2c2e0dfa2a774cec299b3fe47a5c6627", ddr="DDR4-2666 72-bit ECC"),
    1792368000: dict(name="E6b-2400", obs_id=0x0B5E0002, uuid="389b4c78ceec074b8d869b79e6a385f2", ddr="DDR4-2400 72-bit ECC"),
    1792371600: dict(name="E6b-2400dw8", obs_id=0x0B5E0002, uuid="a046ff5ca633dbd389ba09abdada8923",
                     ddr="DDR4-2400 72-bit ECC (device width 8)"),
    1792281600: dict(name="E7a2", obs_id=0x0B5E0005, uuid="50d5d173eb28dc07390e435c16516a34", ddr="DDR4-2666 72-bit ECC"),
    1792458000: dict(name="E7a3-2400", obs_id=0x0B5E0006, uuid="5970d74a63e26d3fe4041d0b50f5319f",
                     ddr="DDR4-2400 72-bit ECC (dw8_833)"),
    1792461600: dict(name="E7a3-2666ca48", obs_id=0x0B5E0007, uuid="869cfe845d0b1f79869844ef67a34cd5", ddr="DDR4-2666 72-bit ECC, CA RDRV_48_48"),
    1792468800: dict(name="E7a3-2400ca48", obs_id=0x0B5E0009, uuid="8b85b528d4b122cd232eb5ff8787ddce",
                     ddr="DDR4-2400 72-bit ECC (dw8_833), CA RDRV_48_48"),
    1792472400: dict(name="E7a3-2666ca60", obs_id=0x0B5E000A, uuid="3f6b9d2219f59bb33a37cf39bf4a06d3",
                     ddr="DDR4-2666 72-bit ECC, CA RDRV_60_60"),
    1792483200: dict(name="fp8c-rc1", obs_id=0x0B5E000C, uuid="be1cab316e1fce901b02ed3d5d6c6191",
                     ddr="DDR4-2666 72-bit ECC, CA RDRV_60_60"),
    1792465200: dict(name="routeV", obs_id=0x0B5E0008, uuid="5027516c91a681bd01afaf11083d9d60", boot_rm="KEPT template",
                     ddr="DDR4-2666 72-bit ECC, CA RDRV_48_48, MIG in the dynamic region (KEPT/PRUNED per xclbin)"),
    # route V r2: the flash image boots with the PRUNED filler in the dynamic region (no MIG at power-on)
    1792479600: dict(name="routeV2", obs_id=0x0B5E000B, uuid="af913744c494e27fe46148db9ab1392c", boot_rm="filler",
                     ddr="DDR4-2666 72-bit ECC, CA RDRV_48_48, MIG in the dynamic region (KEPT/PRUNED per xclbin)"),
    # route V r3: r2's map and filler boot RM; uuid from the r3 build's release/INTERFACE_UUID (2026-10-07)
    1792486800: dict(name="routeV3", obs_id=0x0B5E000D, uuid="2ffac711516be3bd9f58f7d3b897803f", boot_rm="filler",
                     ddr="DDR4-2666 72-bit ECC, CA RDRV_60_60 (link default), MIG in the dynamic region (KEPT/PRUNED per xclbin)"),
}

# ----------------------------------------------------------------------------------------------- management BAR0
# Decoded regions of the management PF BAR0 (32 MB AXI-Lite) that innova2-smi may map. Everything else is either
# not needed or not decoded. (start, length, what)
MGMT_REGIONS = {
    "memcalib": (0x32000, 0x1000, "mgmt_memcalib axi_gpio: calibration bit (GPIO), DDR mode register (GPIO2)"),
    "status":   (0x36000, 0x1000, "mgmt_status = ddr_health (12-bit AXI-Lite; every undefined offset reads 0)"),
    "flash":    (0x40000, 0x1000, "mgmt_flash axi_quad_spi (rawspi; flash-verify only)"),
    "sysmon":   (0xA0000, 0x1000, "system_management_wiz_0 AXI view (DRP register file at +0x400)"),
}
USER_REGIONS = {
    "peer":  (0x100000, 0x1000, "peer_window v1.1 control"),
    "alias": (0x102000, 0x1000, "p2p_alias v1.0 control (NIC-visible PLRAM)"),
}

MEMCALIB, MODE_REG = 0x32000, 0x32008
STATUS = 0x36000                       # ddr_health base; offsets below are relative to it
SYSMON_DRP = 0xA0400                   # SYSMONE4 DRP register d at SYSMON_DRP + 4*d
PEER_BASE, ALIAS_BASE = 0x100000, 0x102000

# SYSMONE4 DRP registers read (UG580): measurement (0x00..0x06) and the hardware max/min trackers (0x20..0x27).
SYSMON_REGS = {"temp": 0x00, "vccint": 0x01, "vccaux": 0x02, "vccbram": 0x06,
               "temp_max": 0x20, "vccint_max": 0x21, "vccaux_max": 0x22, "vccbram_max": 0x23,
               "temp_min": 0x24, "vccint_min": 0x25, "vccaux_min": 0x26, "vccbram_min": 0x27}
SYSMON_ADDRS = tuple(SYSMON_DRP + 4 * d for d in SYSMON_REGS.values())

# ----------------------------------------------------------------------------------------------- ddr_health layouts
# Each register: offset -> (name, access, description). access: "r", "rw" (control), "w1" (write = command).
_BASE = {
    0x00: ("STATUS", "r", "status_in (old axi_gpio GPIO_DATA): [27] fenced access seen (sticky, cleared by aresetn) "
                          "[28] DDR gate open [30:29] mode in force [31] MIG held in reset"),
    0x04: ("STATUS_TRI", "r", "0xFFFFFFFF (GPIO_TRI)"),
    0x08: ("STATUS2", "r", "status2_in (GPIO2_DATA)"),
    0x0C: ("STATUS2_TRI", "r", "0xFFFFFFFF (GPIO2_TRI)"),
    0x10: ("LINKDN", "r", "[15:0] link-down events (sat) [16] recalibrated after the last [17] link-down MIG reset "
                          "active [18] user_lnk_up now [19] waiting for recalibration"),
    0x14: ("ECC_CE", "r", "corrected errors since configuration (sat)"),
    0x18: ("ECC_UE", "r", "[15:0] polls that saw UE (sat) [29] poller completed >= 1 poll [30] ECC_ON_OFF "
                          "[31] UE seen (sticky)"),
    0x1C: ("CE_FFA_LO", "r", "first corrected-error address, low word"),
    0x20: ("CE_FFA_HI", "r", "first corrected-error address, high word; [31] valid"),
    0x24: ("UE_FFA_LO", "r", "first uncorrectable-error address, low word"),
    0x28: ("UE_FFA_HI", "r", "first uncorrectable-error address, high word; [31] valid"),
    0x2C: ("POLLS", "r", "ECC polls completed (wraps; ~1 per ms while calibrated)"),
    0x30: ("OBS_ID", "w1", "layout id; write 0xC1EAC1EA = clear the observability counters"),
    0x34: ("LOCK", "r", "[4:0] locked now [12:8] lock lost (sticky) [20:16] ever locked; bits: DATA_CLK wizard, "
                        "KERNEL_CLK wizard, icap, spi, freerun"),
    0x38: ("LOCKCNT", "r", "lock-loss events (sat 255): [7:0] kernel [15:8] kernel2 [23:16] icap [31:24] spi"),
    0x3C: ("LOCKCNT2", "r", "[7:0] freerun (clk_wiz_0) lock losses"),
    0xA0: ("FENCE", "r", "fenced DDR accesses (sat)"),
    0xA4: ("FENCE_INFO", "r", "[31] valid [30] read: the first fenced access"),
    0xA8: ("FENCE_ADDR_LO", "r", "first fenced access address, low word (DDR bank offset)"),
    0xAC: ("FENCE_ADDR_HI", "r", "first fenced access address, high word"),
    0xB0: ("UPTIME_LO", "r", "aclk cycles since configuration, low word; reading latches the high word"),
    0xB4: ("UPTIME_HI", "r", "uptime high word as latched by the last UPTIME_LO read"),
    0xB8: ("UPTIME_HZ", "r", "uptime clock in Hz (250000000)"),
}
_E7 = {
    0xC0: ("THR_CTRL", "rw", "[0] enable [1] FORCE [3:2] FORCE level (0 = 1/2, else 1/4) [4] may gate DATA_CLK "
                             "[5] may gate KERNEL_CLK; reset by configuration only"),
    0xC4: ("THR_THRESH", "rw", "[9:0] SET (tier 1 on) [25:16] REL (tier 1 off), SYSMON temperature codes"),
    0xC8: ("THR_STATUS", "r", "[0] alarm [1] requested [2] DATA_CLK throttled [3] KERNEL_CLK throttled (measured) "
                              "[25:16] temperature code (E7a3 adds [5:4] tier in effect [6] stale [7] fail-safe "
                              "[9:8] level [13:12] temperature tier)"),
    0xCC: ("THR_EVENTS", "r", "throttle starts (sat)"),
    0xD0: ("THR_TIME", "r", "time throttled, 262.144 us units (sat)"),
    0xD4: ("TS_THR", "r", "timestamp of the last throttle start"),
    0xE0: ("TS_LINKDN", "r", "timestamp of the last link-down"),
    0xE4: ("TS_LOCK", "r", "timestamp of the last lock loss"),
    0xE8: ("TS_CE", "r", "timestamp of the last ECC CE"),
    0xEC: ("TS_UE", "r", "timestamp of the last ECC UE"),
    0xF0: ("TS_FENCE", "r", "timestamp of the last fenced access"),
    0xF4: ("TS_PR", "r", "timestamp of the last PR load (RP isolated)"),
    0xF8: ("TS_PCIE", "r", "timestamp of the last PCIe error"),
    0x100: ("PCIE_COR", "r", "PCIe correctable errors (sat)"),
    0x104: ("PCIE_NONFATAL", "r", "PCIe non-fatal errors"),
    0x108: ("PCIE_FATAL", "r", "PCIe fatal errors"),
    0x10C: ("PCIE_LINK", "r", "[5:0] LTSSM [10:8] current speed [19:16] negotiated width"),
    0x110: ("PCIE_LINKCHG", "r", "speed/width changes (sat)"),
    0x120: ("PR_COUNT", "r", "RP isolations = xclbin loads (sat)"),
    0x124: ("PR_LAST_DUR", "r", "duration of the last isolation, aclk cycles (sat)"),
    0x128: ("PR_GATE", "r", "[0] RP released now"),
    0x180: ("FR_INFO", "r", "flight recorder [4:0] log2(entries) [15:8] record format"),
    0x184: ("FR_WPTR", "r", "next record index"),
    0x188: ("FR_WRAPS", "r", "ring wraps (sat)"),
    0x18C: ("FR_IDX", "rw", "write an index to fetch that record"),
    0x190: ("FR_DLO", "r", "fetched record, low word"),
    0x194: ("FR_DHI", "r", "fetched record, high word"),
    0x198: ("FR_READY", "r", "[0] fetched record ready"),
}
_E7A3 = {
    0xD8: ("THR_THRESH2", "rw", "tier 2: [9:0] SET2 [25:16] REL2"),
    0xDC: ("THR_TIER", "rw", "[1:0] tier-1 level [9:8] tier-2 level (0 = 1/2, else 1/4) [16] stale fail-safe enable"),
    0x140: ("TEMP_SAMPLES", "r", "temperature samples taken (wraps); frozen = poller not sampling"),
    0x144: ("TEMP_CHANGES", "r", "samples whose 16-bit word changed; frozen = ADC not converting"),
    0x148: ("TEMP_RAW", "r", "[15:0] last TEMP word [31:16] its age, 262.144 us units (sat)"),
    0x14C: ("TEMP_STALE", "r", "[15:0] stale episodes [16] stale now [17] fail-safe engaged [18] stale seen"),
    0x150: ("TEMP_MINMAX", "r", "[9:0] lowest code [25:16] highest code since configuration/clear"),
    0x154: ("THR_TIERCHG", "r", "tier changes of the state machine (sat)"),
}


# route V (OBS_ID 0x0B5E0008): the DDR-in-the-dynamic-region block. Everything at 0x00-0x198 keeps its E7a3 meaning;
# 0x14-0x2C stay cumulative since configuration (the in-RP poller's deltas cross the RP boundary), 0x1D0-0x1E8 are the
# same ECC values for the CURRENT load only (reset when the RP is isolated for a load).
_RV = {
    0x1C0: ("RV_INFO", "r", "[0] DDR in the loaded xclbin (KEPT; 0 = PRUNED) [1] calibrated (RM ddr_ready AND RP "
                            "released) [2] rp_calib (filler: 1) [3] ECC poller present [4] RP released [5] DDR window "
                            "gate open [6] ECC snapshots accepted [31:16] 0x5256"),
    0x1C4: ("RV_LOADS", "r", "[15:0] KEPT loads [31:16] PRUNED loads, counted 262 us after each RP release (sat)"),
    0x1C8: ("RV_CAL", "r", "[15:0] calibrations completed [31:16] calibrations LOST while a KEPT xclbin was loaded (sat)"),
    0x1CC: ("RV_CALTIME", "r", "aclk cycles from the last RP release to its calibration (0 = none yet)"),
    0x1D0: ("ECC_CE_LOAD", "r", "corrected errors since the last xclbin load (per load)"),
    0x1D4: ("ECC_UE_LOAD", "r", "[15:0] UE polls since the last load [29] polled since the load [31] UE seen since the load"),
    0x1D8: ("ECC_POLLS_LOAD", "r", "ECC polls since the last load"),
    0x1DC: ("CE_FFA_LOAD_LO", "r", "first corrected-error address of this load, low word"),
    0x1E0: ("CE_FFA_LOAD_HI", "r", "first corrected-error address of this load, high word; [31] valid"),
    0x1E4: ("UE_FFA_LOAD_LO", "r", "first uncorrectable-error address of this load, low word"),
    0x1E8: ("UE_FFA_LOAD_HI", "r", "first uncorrectable-error address of this load, high word; [31] valid"),
    0x1EC: ("FENCE_KEPT", "r", "fenced DDR accesses while a KEPT xclbin was loaded and released (sat)"),
    0x1F0: ("TS_CAL", "r", "timestamp of the last calibration completed"),
    0x1F4: ("TS_FENCE_KEPT", "r", "timestamp of the last fence while KEPT"),
    0x1F8: ("TS_LOAD", "r", "timestamp of the last load settle (KEPT/PRUNED counted)"),
    # DDR_INFO (owner 2026-10-06): the loaded xclbin's MIG configuration, reported by the RM, and the MEASURED ui_clk
    0x200: ("DDR_INFO0", "r", "[15:0] tCK ps [31:16] magic 0xD4D4 = DDR in this xclbin (KEPT), 0x0D0D = no DDR (PRUNED); "
                              "latched at load settle, 0 while isolated"),
    0x204: ("DDR_INFO1", "r", "[7:0] CL [15:8] CWL [23:16] tRCD nCK [31:24] tRP nCK"),
    0x208: ("DDR_INFO2", "r", "[7:0] DQ width [8] ECC on [11:9] CA drive (0 RDRV_40_40, 1 48_48, 2 60_60) [15:12] MIG "
                              "variant (1 mwrnd833, 2 dw8_833, 3 e6b750)"),
    0x20C: ("DDR_INFO3", "r", "part id: CRC32 of the custom-parts CSV + MemoryPart (0 = built-in part)"),
    0x210: ("UI_CLK_EDGES", "r", "MIG ui_clk/2^SHIFT rising edges in the last 2^WIN aclk window (0 = no valid window)"),
    0x214: ("UI_CLK_CFG", "r", "[7:0] WIN (log2 aclk cycles) [15:8] SHIFT [16] the last window was valid"),
}
# route V r2 (OBS_ID 0x0B5E000B) and fp8c rc1 (0x0B5E000C): fenced DDR accesses split by LINK-DOWN WINDOW. A window is open while the link is
# down, while the link-down MIG reset is active, or -- after a link-down -- while the static waits for recalibration
# and less than 2 s have passed since the link came back (it ends at RECAL_DONE or at that limit). Every fenced access
# goes to exactly one of the two counters: FENCE_LDWIN + FENCE_OUTWIN = FENCE (0xA0). CLEAR zeroes all three.
_RV2 = {
    0x218: ("FENCE_LDWIN", "r", "fenced DDR accesses INSIDE a link-down window (sat)"),
    0x21C: ("FENCE_OUTWIN", "r", "fenced DDR accesses OUTSIDE every link-down window (sat)"),
    0x220: ("FENCE_WIN", "r", "[0] a link-down window is open now [1] link down now [2] link-down MIG reset active "
                              "[3] waiting for recalibration [4] the window's 2 s limit has run out (or never started) "
                              "[31:16] the limit in 2^16-aclk units (7629 = 2.0 s)"),
}
DDR_KEPT_MAGIC, DDR_NONE_MAGIC = 0xD4D4, 0x0D0D
CA_CODES = {0: "RDRV_40_40", 1: "RDRV_48_48", 2: "RDRV_60_60"}
MIG_VARIANTS = {1: "mwrnd833", 2: "dw8_833", 3: "e6b750"}


def decode_ddr_info(w0, w1, w2, w3, edges, cfg):
    """route V DDR_INFO -> dict (tCK, MT/s, CL/CWL/tRCD/tRP, width, ECC, CA drive, variant, part id, measured ui_clk)."""
    magic = w0 >> 16
    win, shift, valid = cfg & 0xFF, cfg >> 8 & 0xFF, bool(cfg >> 16 & 1)
    ui_hz = edges * (1 << shift) * ACLK_HZ / (1 << win) if (valid and win) else 0.0
    tck = w0 & 0xFFFF
    return dict(magic="0x%04X" % magic, kept=magic == DDR_KEPT_MAGIC, no_ddr=magic == DDR_NONE_MAGIC,
                tck_ps=tck, mts=round(2e6 / tck) if tck else 0, cl=w1 & 0xFF, cwl=w1 >> 8 & 0xFF,
                trcd=w1 >> 16 & 0xFF, trp=w1 >> 24 & 0xFF, dq_width=w2 & 0xFF, ecc=bool(w2 >> 8 & 1),
                ca_drive=CA_CODES.get(w2 >> 9 & 7, "?"), mig_variant=MIG_VARIANTS.get(w2 >> 12 & 0xF, "?"),
                part_id="0x%08x" % w3, ui_clk_mhz_measured=round(ui_hz / 1e6, 3), ui_clk_window_valid=valid,
                ui_clk_mhz_expected=round(1e6 / (4 * tck), 3) if tck else 0.0)
RV_SIGNATURE = 0x5256


def _merge(*maps):
    out = {}
    for m in maps:
        out.update(m)
    return out


_MON = {}
for _b, _n in ((0x40, "P2P"), (0x60, "HOST"), (0x80, "PEER")):
    _what = {"P2P": "host -> P2P BAR (xdma M_AXI_BYPASS)", "HOST": "kernels -> host memory (host_remap)",
             "PEER": "kernels -> PEER window (peer_window)"}[_n]
    _MON.update({
        _b + 0x00: ("MON_%s_W" % _n, "r", "%s writes: [15:0] SLVERR [31:16] DECERR (sat)" % _what),
        _b + 0x04: ("MON_%s_R" % _n, "r", "%s reads: [15:0] SLVERR [31:16] DECERR (sat)" % _what),
        _b + 0x08: ("MON_%s_FIRST" % _n, "r", "first error: [31] valid [30] read [29:28] resp [27] address known "
                                              "[23:16] ID" ),
        _b + 0x0C: ("MON_%s_ADDR_LO" % _n, "r", "first error address, low word"),
        _b + 0x10: ("MON_%s_ADDR_HI" % _n, "r", "first error address, high word"),
        _b + 0x14: ("MON_%s_UNTRACKED" % _n, "r", "[15:0] errors whose address was not tracked (sat)"),
    })


# OBS_ID -> layout. Only these OBS_IDs are accepted; every other value stops register access (fail closed).
# Each map was checked against the read multiplexer of its ddr_health.v (`regmap --check-rtl`). 0x0B5E0001, 0x0B5E0003
# and 0x0B5E0004 were development builds that were never released (released=False): their maps are known from the RTL
# and accepted, but nothing about them has been run on hardware by this tool.
LAYOUTS = {
    0x0B5E0001: dict(name="fp8c full (AXI error monitors; development build)", e7=False, e7a3=False, mon=True,
                     released=False, regs=_merge(_BASE, _MON)),
    0x0B5E0002: dict(name="E4 layout (E4/E6/E6b/E6b-2400/E6b-2400dw8)", e7=False, e7a3=False, mon=False,
                     released=True, regs=_merge(_BASE)),
    0x0B5E0003: dict(name="E7a (edge-gating throttle; development build)", e7=True, e7a3=False, mon=False,
                     released=False, regs=_merge(_BASE, _E7)),
    0x0B5E0004: dict(name="E7b (E7a + AXI error monitors; development build)", e7=True, e7a3=False, mon=True,
                     released=False, regs=_merge(_BASE, _E7, _MON)),
    0x0B5E0005: dict(name="E7a2", e7=True, e7a3=False, mon=False, released=True, regs=_merge(_BASE, _E7)),
    0x0B5E0006: dict(name="E7a3", e7=True, e7a3=True, mon=False, released=True, regs=_merge(_BASE, _E7, _E7A3)),
    0x0B5E0007: dict(name="E7a3-2666ca48 (E7a3 register map)", e7=True, e7a3=True, mon=False, released=True,
                     regs=_merge(_BASE, _E7, _E7A3)),
    0x0B5E0009: dict(name="E7a3-2400ca48 (E7a3 register map)", e7=True, e7a3=True, mon=False, released=True,
                     regs=_merge(_BASE, _E7, _E7A3)),
    0x0B5E000A: dict(name="E7a3-2666ca60 (E7a3 register map)", e7=True, e7a3=True, mon=False, released=True,
                     regs=_merge(_BASE, _E7, _E7A3)),
    # fp8c rc1: E7a3-2666ca60 + the fence-window counters (route V r2 RTL ported to E7a3)
    0x0B5E000C: dict(name="fp8c rc1 (E7a3 map + fence-window counters 0x218)", e7=True, e7a3=True, mon=False,
                     released=True, fencewin=True, regs=_merge(_BASE, _E7, _E7A3, _RV2)),
    # route V: not yet run on hardware (released=False until the owner's first-hardware session)
    0x0B5E0008: dict(name="route V (E7a3 map + DDR-in-RP block 0x1C0; DDR per xclbin)", e7=True, e7a3=True, mon=False,
                     released=True, routev=True, regs=_merge(_BASE, _E7, _E7A3, _RV)),
    # route V r2: + the fence-window counters; the flash image boots with the PRUNED filler (not yet run on hardware)
    0x0B5E000B: dict(name="route V r2 (route-V map + fence-window counters 0x218; boot RM = filler)", e7=True,
                     e7a3=True, mon=False, released=False, routev=True, fencewin=True,
                     regs=_merge(_BASE, _E7, _E7A3, _RV, _RV2)),
    # route V r3: r2's map and RTL (only the static changed); not yet run on hardware
    0x0B5E000D: dict(name="route V r3 (route-V r2 map; RP pipelining + PLRAM pblocks; boot RM = filler)", e7=True,
                     e7a3=True, mon=False, released=False, routev=True, fencewin=True,
                     regs=_merge(_BASE, _E7, _E7A3, _RV, _RV2)),
}
OBS_ID_OFF, OBS_CLEAR = 0x30, 0xC1EAC1EA

# Power-on defaults of the throttle registers, per layout (restored by 'throttle defaults' and by the tests).
THR_DEFAULTS = {
    0x0B5E0003: {0xC0: 0x00000031, 0xC4: 0x02D402E9},
    0x0B5E0004: {0xC0: 0x00000031, 0xC4: 0x02D402E9},
    0x0B5E0005: {0xC0: 0x00000031, 0xC4: 0x02D402E9},
    0x0B5E0006: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E0007: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E0009: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E000A: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E000C: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E0008: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E000B: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E000D: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
}


def layout(obs_id):
    return LAYOUTS.get(obs_id)


def is_routev(lay):
    return bool(lay and lay.get("routev"))


def has_fencewin(lay):
    """route V r2: FENCE_LDWIN / FENCE_OUTWIN (0x218 / 0x21C) exist."""
    return bool(lay and lay.get("fencewin"))


def read_set(obs_id):
    """Absolute mgmt BAR0 offsets innova2-smi may read on a shell with this OBS_ID (empty for an unknown one)."""
    lay = LAYOUTS.get(obs_id)
    if not lay:
        return frozenset([STATUS + OBS_ID_OFF])
    s = {STATUS + o for o in lay["regs"]} | {MEMCALIB, MODE_REG} | set(SYSMON_ADDRS)
    return frozenset(s)


def write_set(obs_id):
    """Absolute mgmt BAR0 offsets innova2-smi may write (each still behind an explicit command + confirmation)."""
    lay = LAYOUTS.get(obs_id)
    if not lay:
        return frozenset()
    s = {MODE_REG, STATUS + OBS_ID_OFF}
    s |= {STATUS + o for o, r in lay["regs"].items() if r[1] == "rw"}
    return frozenset(s)


USER_READS = frozenset([PEER_BASE + o for o in (0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20,
                                                 0x28, 0x2C, 0x30, 0x34, 0x38, 0x3C)] +
                       [ALIAS_BASE + o for o in (0x00, 0x04, 0x08, 0x0C, 0x10, 0x18, 0x1C, 0x20, 0x24, 0x28)])
PEER_ID, ALIAS_ID = 0x50454552, 0x50414C49

# ----------------------------------------------------------------------------------------------- units, formulas
ACLK_HZ = 250e6
TS_S = 65536 / ACLK_HZ                # event timestamps = uptime[47:16]: 262.144 us
REC_S = 256 / ACLK_HZ                 # recorder timestamps = uptime[47:8]: 1.024 us
MODES = {"on": 0, "off": 1, "emu": 2, "hybrid": 3}
MODE_NAMES = {0: "on", 1: "off", 2: "emu", 3: "hybrid"}
LOCK_NAMES = ["DATA_CLK wizard", "KERNEL_CLK wizard", "icap wizard", "spi wizard", "freerun clk_wiz_0"]
TIER_NAMES = {0: "FULL", 1: "tier 1", 2: "tier 2", 3: "?"}
REC_TYPES = {1: "START", 2: "LINK_DOWN", 3: "RECAL_DONE", 4: "LOCK_LOSS", 5: "ECC_CE", 6: "ECC_UE", 7: "FENCE",
             8: "PR_ISOLATE", 9: "PR_RELEASE", 10: "THR_START", 11: "THR_STOP", 12: "PCIE_COR", 13: "PCIE_NONFATAL",
             14: "PCIE_FATAL", 15: "PCIE_LINK", 16: "OBS_CLEAR", 17: "THR_TIER", 18: "TEMP_STALE", 19: "TEMP_FRESH",
             20: "RM_DDR", 21: "CAL_DONE", 22: "CAL_LOST"}          # 20-22: route V
ROUTEV_DEFERRED_MODES = ("emu", "hybrid")   # route V: the static gate runs on/off only (EMU/HYBRID deferred)
DDR_BANK0_BASE = 0x200000000          # XRT device address of bank0 (AXI address 0)
VALIDATED_BOARDS = {("MNV303212A-ADLT", "A7")}
DDR_PART = "Micron MT40A1G16KNR-075 (16 Gb x16, DDR4-2666 grade)"

# ----------------------------------------------------------------------------------------------- MIG per build
# No shell has a register that reports its MIG configuration, so the table is per build (ROM_TS). Every value was read
# from the build tree on 2026-10-06: tCK/tRCD/tRP/tRAS/tRFC/DQ_WIDTH/ECC/nCK_PER_CLK/MEMORY_PART from the generated
# MIG top (rtl/ip_top/*ddrmem*_ddr4.sv), CL/CWL/InputClockPeriod/PhyClockRatio/CustomParts from the IP's .xml, the CA
# drive from the MIG's par .xdc (RDRV_40_40 on every build) plus the ca_drive.tcl override and its routed gate
# (E7a3-2666ca48: "CA OUTPUT_IMPEDANCE RDRV_48_48, failures=0"), POLL_CYC from the BD cell mgmt_status (create_project's
# "CP mgmt_status POLL_CYC" line; the IP default 333000 where it is not overridden). Timing parameters are in DDR4
# clock cycles. Every build runs the PHY at 4:1: ui_clk = 1 / (4 tCK), data rate = 2 / tCK = 8 x ui_clk.
DW8_CSV = "custom CSV custom_parts_mt40a1g16knr075_dw8b (the board's MT40A1G16KNR-075 described as device width 8)"
_MIG_2666 = dict(part="MT40A1G16KNRD8B-075", part_note=DW8_CSV, tck_ps=750, cl=19, cwl=18, trcd=20, trp=20, tras=43,
                 trfc=467, dq_width=72, ecc=True, input_clk_ps=10000, phy_ratio=4, ca_drive="RDRV_40_40 (MIG default)",
                 poll_cyc=333000)
_MIG_2400_DW8 = dict(_MIG_2666, tck_ps=833, trcd=19, trp=19, tras=39, trfc=421, input_clk_ps=9996, poll_cyc=300000)
MIG_BUILDS = {
    1792022400: dict(_MIG_2666, variant="dw8_750 (before mig_variant.tcl)"),
    1792368000: dict(_MIG_2400_DW8, variant="mwrnd833", part="MT40A1G16WBU-083E", part_note="Vivado catalogue part",
                     cl=16, cwl=12, trcd=16, trp=16),
    1792371600: dict(_MIG_2400_DW8, variant="dw8_833"),
    1792281600: dict(_MIG_2666, variant="dw8_750 (before mig_variant.tcl)"),
    1792458000: dict(_MIG_2400_DW8, variant="dw8_833"),
    1792461600: dict(_MIG_2666, variant="e6b750", ca_drive="RDRV_48_48 (ca_drive.tcl, routed gate passed)"),
    1792468800: dict(_MIG_2400_DW8, variant="dw8_833", ca_drive="RDRV_48_48 (ca_drive.tcl, routed gate passed)"),
    1792472400: dict(_MIG_2666, variant="e6b750", ca_drive="RDRV_60_60 (ca_drive.tcl, routed gate passed)"),
    1792483200: dict(_MIG_2666, variant="e6b750", ca_drive="RDRV_60_60 (ca_drive.tcl, routed gate passed)"),
    # route V: the MIG is in each KEPT xclbin; these are the template's (MIG_VARIANT e6b750, CA_DRIVE RDRV_48_48, the
    # in-RP ddr_ecc_poller's POLL_CYC 333000). An xclbin built from another template differs: DDR_INFO will say.
    1792465200: dict(_MIG_2666, variant="e6b750 (route-V template; per xclbin)", per_xclbin=True,
                     ca_drive="RDRV_48_48 (template ddr_rp_late.xdc)"),
    1792479600: dict(_MIG_2666, variant="e6b750 (route-V template; per xclbin)", per_xclbin=True,
                     ca_drive="RDRV_48_48 (template ddr_rp_late.xdc)"),
    # route V r3: the link hook's default CA drive is RDRV_60_60 (INNOVA2_CA_DRIVE overrides; DDR_INFO says per xclbin)
    1792486800: dict(_MIG_2666, variant="e6b750 (route-V template; per xclbin)", per_xclbin=True,
                     ca_drive="RDRV_60_60 (link default; template ddr_rp_late.xdc RDRV_48_48)"),
}
# Route V's planned DDR_INFO block (MIG constants + a ui_clk counter, from the template) replaces the table and the poll
# estimate when its OBS_ID layout lists it: {obs_id: {"regs": {offset: (name, decoder)}}}. Empty until that map exists.
# route V DDR_INFO block (mgmt 0x36200..0x36214): {"regs": {offset: (name, decoder)}} for `innova2-smi ddr`
DDR_INFO_MAPS = {
    0x0B5E0008: dict(regs={
        0x200: ("info0_tck_ps_magic", lambda v: "tCK %d ps, magic 0x%04X%s" % (v & 0xFFFF, v >> 16,
                {DDR_KEPT_MAGIC: " (KEPT)", DDR_NONE_MAGIC: " (no DDR)"}.get(v >> 16, " (?)"))),
        0x204: ("info1_cl_cwl_trcd_trp", lambda v: "CL%d CWL%d tRCD %d tRP %d" % (v & 0xFF, v >> 8 & 0xFF, v >> 16 & 0xFF, v >> 24)),
        0x208: ("info2_width_ecc_ca_variant", lambda v: "%d-bit, ECC %s, CA %s, MIG %s" % (
            v & 0xFF, "on" if v >> 8 & 1 else "off", CA_CODES.get(v >> 9 & 7, "?"), MIG_VARIANTS.get(v >> 12 & 0xF, "?"))),
        0x20C: ("info3_part_id", lambda v: "0x%08x" % v),
        0x210: ("ui_clk_edges", lambda v: str(v)),
        0x214: ("ui_clk_cfg", lambda v: "window 2^%d aclk, divider 2^%d, valid %d" % (v & 0xFF, v >> 8 & 0xFF, v >> 16 & 1)),
    }),
}
DDR_INFO_MAPS[0x0B5E000B] = DDR_INFO_MAPS[0x0B5E0008]          # route V r2: the same DDR_INFO block
DDR_INFO_MAPS[0x0B5E000D] = DDR_INFO_MAPS[0x0B5E0008]          # route V r3: the same DDR_INFO block


def mig_rates(tck_ps, phy_ratio=4):
    """(DDR clock MHz, ui_clk MHz, MT/s) of a tCK."""
    ck = 1e6 / tck_ps
    return ck, ck / phy_ratio, 2 * ck


def mig_config(rom_ts):
    """The MIG table entry for a build, with its derived rates; None for an unknown build."""
    m = MIG_BUILDS.get(rom_ts)
    if m is None:
        return None
    ck, ui, mts = mig_rates(m["tck_ps"], m["phy_ratio"])
    return dict(m, ddr_clk_mhz=round(ck, 2), ui_clk_mhz=round(ui, 3), mts=round(mts, 1),
                speed_grade="DDR4-%d" % int(mts), width="%d-bit%s" % (m["dq_width"], " (64 data + 8 ECC)"
                                                                       if m["ecc"] and m["dq_width"] == 72 else ""))


def ui_clk_from_polls(polls0, up0, polls1, up1, poll_cyc, aclk_hz=ACLK_HZ):
    """Measured MIG ui_clk (MHz) from two (polls completed, uptime aclk cycles) samples: the ECC poller finishes one poll
    every POLL_CYC ui_clk cycles plus its own few AXI-Lite cycles (biases the result low by < 0.05 %). Returns
    (ui_mhz, dpolls, dt_s); ui_mhz is None when nothing was polled."""
    dp = (polls1 - polls0) & 0xFFFFFFFF
    dt = ((up1 - up0) & 0xFFFFFFFFFFFFFFFF) / aclk_hz
    if dp <= 0 or dt <= 0:
        return None, dp, dt
    return poll_cyc * dp / dt / 1e6, dp, dt


def ui_clk_precision_pct(dpolls):
    """+- precision of ui_clk_from_polls: polls reach the aclk side in batches, so each endpoint is uncertain by one
    poll (2 / dpolls); plus the aclk's own tolerance (the PCIe reference, <= 300 ppm) and the poll overhead (< 0.05 %)."""
    return round(100.0 * 2 / dpolls + 0.08, 2) if dpolls else None


def code2c(k):
    """SYSMONE4 10-bit temperature code -> degrees C (UG580)."""
    return k * 509.3140064 / 1024 - 280.2308787


def c2code(c):
    """The first code whose temperature is >= c."""
    return max(0, min(1023, int(math.ceil((c + 280.2308787) * 1024 / 509.3140064))))


def sysmon_volts(word16):
    """SYSMONE4 supply register (16-bit, 10 MSBs significant) -> volts (3 V full scale)."""
    return (word16 >> 6) * 3.0 / 1024


def lvl(v):
    """kclk_gate level field -> divisor (0 = 1/2, anything else = 1/4)."""
    return 4 if v & 3 else 2


def decode_ffa(v):
    """ddr_health's copy of the MIG first-failing-address register (bit 63 not kept). Returns a dict."""
    beats = v >> 56 & 0x7F
    row, col, rank, grp, bnk = v >> 24 & 0xFFFF, v >> 8 & 0x3FF, v >> 4 & 0xF, v >> 2 & 3, v & 3
    app = row << 14 | (col >> 3) << 7 | bnk << 5 | grp << 3 | (col & 7)
    return dict(raw="0x%016x" % v, beats="0x%02x" % beats, rmw=bool(v >> 44 & 1), row=row, col=col,
                bank_group=grp, bank=bnk, rank=rank, axi_addr=app * 8, device_addr=app * 8 + DDR_BANK0_BASE)


def ffa_str(d):
    return "%s: beats %s, row 0x%x col 0x%x bg %d bank %d%s -> AXI 0x%x (XRT device 0x%x)" % (
        d["raw"], d["beats"], d["row"], d["col"], d["bank_group"], d["bank"], ", RMW" if d["rmw"] else "",
        d["axi_addr"], d["device_addr"])


def decode_record(v):
    """64-bit flight-recorder record -> (type, payload, time in s)."""
    return v >> 58 & 0x3F, v >> 40 & 0x3FFFF, (v & 0xFFFFFFFFFF) * REC_S


def record_detail(t, p):
    if t == 4:
        return "wizard(s) " + ",".join(LOCK_NAMES[i] for i in range(5) if p >> i & 1)
    if t == 9:
        return "isolated %.3f ms" % ((p << 14) / ACLK_HZ * 1e3)
    if t == 10:
        return "%.1f C%s" % (code2c(p & 0x3FF), ", FORCED" if p >> 10 & 1 else "")
    if t == 11:
        return "%.1f C" % code2c(p & 0x3FF)
    if t == 15:
        return "LTSSM 0x%02x speed %d width %d" % (p & 0x3F, p >> 6 & 7, p >> 9 & 0xF)
    if t == 17:
        return "%s -> %s at %.2f C" % (TIER_NAMES[p >> 12 & 3], TIER_NAMES[p >> 10 & 3], code2c(p & 0x3FF))
    if t in (18, 19):
        return "last %.2f C" % code2c(p & 0x3FF)
    if t == 20:
        return "%s, ECC poller %s" % ("DDR KEPT" if p & 1 else "DDR PRUNED (no DDR in this xclbin)",
                                      "present" if p >> 1 & 1 else "absent")
    if t == 21:
        return "calibrated %.1f ms after the RP release" % ((p << 14) / ACLK_HZ * 1e3)
    if t in (1, 3, 16):
        return ""
    return "count %d" % p


def parse_vpd(raw):
    """PCI VPD -> {"name": identifier, keyword: value} for the read-only section (PN, EC, SN, ...)."""
    out, i = {}, 0
    while i < len(raw):
        tag = raw[i]
        if tag in (0x78, 0x0F):
            break
        if not tag & 0x80:
            i += 1 + (tag & 7)
            continue
        if i + 3 > len(raw):
            break
        n = raw[i + 1] | raw[i + 2] << 8
        body = raw[i + 3:i + 3 + n]
        i += 3 + n
        if tag == 0x82:
            out["name"] = body.decode("ascii", "replace").strip()
        elif tag == 0x90:
            j = 0
            while j + 3 <= len(body):
                k, ln = body[j:j + 2].decode("ascii", "replace"), body[j + 2]
                if k != "RV":
                    out[k] = body[j + 3:j + 3 + ln].decode("ascii", "replace").strip()
                j += 3 + ln
    return out


def xclbin_header(b):
    """axlf header fields innova2-smi checks before loading anything: magic, featureRomTimeStamp, interface uuid
    (m_interface_uuid), VBNV, uuid."""
    import struct
    if len(b) < 432 or b[:7] != b"xclbin2":
        return None
    return dict(rom_ts=struct.unpack_from("<Q", b, 320)[0], interface_uuid=b[336:352].hex(),
                vbnv=b[352:416].split(b"\0")[0].decode("ascii", "replace"),
                uuid=b[416:432].hex())


# ----------------------------------------------------------------------------------------------- counters
# Decoded fields of the ddr_health registers, for `innova2-smi counters`. (offset, lsb, width, name, kind, description)
# kinds: n = count; s262 = 262.144 us units -> s; cyc = aclk cycles -> ms; code = temperature code -> C;
#        ms262 = 262.144 us units -> ms; hex = shown raw; bool = flag. Every register of the layout is dumped; a
# register with no entry here is shown raw with its register-table description.
COUNTER_FIELDS = [
    (0x10, 0, 16, "link_down_events", "n", "PCIe link-down events that reset the DDR side (sat)"),
    (0x10, 16, 1, "recalibrated_after_last_link_down", "bool", "MIG recalibrated after the last link-down"),
    (0x10, 17, 1, "link_down_mig_reset_active", "bool", "link-down MIG reset active now"),
    (0x10, 18, 1, "user_lnk_up", "bool", "PCIe user_lnk_up now"),
    (0x10, 19, 1, "waiting_for_recalibration", "bool", "waiting for recalibration after a link-down"),
    (0x14, 0, 32, "ecc_ce", "n", "ECC corrected errors since configuration (sat)"),
    (0x18, 0, 16, "ecc_ue_polls", "n", "ECC polls that saw an uncorrectable error (sat)"),
    (0x18, 29, 1, "ecc_poller_alive", "bool", "the ECC poller completed at least one poll"),
    (0x18, 30, 1, "ecc_on", "bool", "MIG ECC_ON_OFF as last read"),
    (0x18, 31, 1, "ecc_ue_seen", "bool", "an uncorrectable error has been seen (sticky)"),
    (0x2C, 0, 32, "ecc_polls", "n", "ECC polls completed (wraps; ~1/ms while calibrated)"),
    (0x34, 0, 5, "locked_now", "hex", "wizards locked now (bit0 DATA_CLK, 1 KERNEL_CLK, 2 icap, 3 spi, 4 freerun)"),
    (0x34, 8, 5, "lock_lost", "hex", "wizards that lost lock since configuration/clear (sticky)"),
    (0x34, 16, 5, "ever_locked", "hex", "wizards that have ever locked"),
    (0x38, 0, 8, "lock_losses_data_clk", "n", "DATA_CLK wizard lock losses (sat 255; normal on clock reprogramming)"),
    (0x38, 8, 8, "lock_losses_kernel_clk", "n", "KERNEL_CLK wizard lock losses (sat 255)"),
    (0x38, 16, 8, "lock_losses_icap", "n", "icap wizard lock losses (sat 255; never reset)"),
    (0x38, 24, 8, "lock_losses_spi", "n", "spi wizard lock losses (sat 255; normal on XDMA reset)"),
    (0x3C, 0, 8, "lock_losses_freerun", "n", "freerun clk_wiz_0 lock losses (sat 255; normal on XDMA reset)"),
    (0xA0, 0, 32, "fenced_accesses", "n", "DDR accesses fenced by the calibration gate (sat)"),
    (0xCC, 0, 32, "throttle_starts", "n", "throttle starts (sat)"),
    (0xD0, 0, 32, "time_throttled", "s262", "time throttled (sat)"),
    (0xD4, 0, 32, "ts_last_throttle_start", "s262", "uptime at the last throttle start (0 = never)"),
    (0xE0, 0, 32, "ts_last_link_down", "s262", "uptime at the last link-down (0 = never)"),
    (0xE4, 0, 32, "ts_last_lock_loss", "s262", "uptime at the last lock loss"),
    (0xE8, 0, 32, "ts_last_ecc_ce", "s262", "uptime at the last ECC CE"),
    (0xEC, 0, 32, "ts_last_ecc_ue", "s262", "uptime at the last ECC UE"),
    (0xF0, 0, 32, "ts_last_fenced", "s262", "uptime at the last fenced access"),
    (0xF4, 0, 32, "ts_last_pr_load", "s262", "uptime at the last PR load"),
    (0xF8, 0, 32, "ts_last_pcie_error", "s262", "uptime at the last PCIe error"),
    (0x100, 0, 32, "pcie_correctable", "n", "PCIe correctable errors (sat)"),
    (0x104, 0, 16, "pcie_nonfatal", "n", "PCIe non-fatal errors (sat)"),
    (0x108, 0, 16, "pcie_fatal", "n", "PCIe fatal errors (sat)"),
    (0x10C, 0, 6, "pcie_ltssm", "hex", "LTSSM state now"),
    (0x10C, 8, 3, "pcie_speed_code", "n", "current link speed code"),
    (0x10C, 16, 4, "pcie_width_code", "n", "negotiated link width code"),
    (0x110, 0, 16, "pcie_link_changes", "n", "link speed/width changes (sat)"),
    (0x120, 0, 32, "pr_count", "n", "RP isolations = xclbin loads (sat)"),
    (0x124, 0, 32, "pr_last_duration", "cyc", "duration of the last RP isolation"),
    (0x128, 0, 1, "rp_released", "bool", "the reconfigurable partition is released now"),
    (0x154, 0, 16, "tier_changes", "n", "throttle tier changes of the state machine (sat)"),
    (0x140, 0, 32, "temp_samples", "n", "temperature samples taken (wraps; frozen = poller stopped)"),
    (0x144, 0, 32, "temp_changes", "n", "samples whose 16-bit word changed (frozen = ADC not converting)"),
    (0x148, 0, 16, "temp_last_word", "hex", "last SYSMON TEMP word"),
    (0x148, 16, 16, "temp_age", "ms262", "age of the last temperature sample (sat 0xFFFF = 17 s)"),
    (0x14C, 0, 16, "temp_stale_episodes", "n", "temperature STALE episodes (sat)"),
    (0x14C, 16, 1, "temp_stale_now", "bool", "temperature STALE now"),
    (0x14C, 17, 1, "failsafe_engaged", "bool", "stale fail-safe engaged (tier 2)"),
    (0x14C, 18, 1, "temp_stale_seen", "bool", "STALE seen since configuration/clear (sticky)"),
    (0x150, 0, 10, "temp_min", "code", "lowest temperature since configuration/clear (0x3FF = none yet)"),
    (0x150, 16, 10, "temp_max", "code", "highest temperature since configuration/clear (0 = none yet)"),
    (0x184, 0, 32, "recorder_next_index", "n", "flight recorder next record index"),
    (0x188, 0, 32, "recorder_wraps", "n", "flight recorder ring wraps (sat)"),
    (0xC8, 16, 10, "throttle_temp", "code", "the throttle's temperature (last sample)"),
    # route V
    (0x1C0, 0, 1, "rv_ddr_kept", "bool", "the loaded xclbin has DDR (KEPT); 0 = PRUNED, no DDR"),
    (0x1C0, 1, 1, "rv_calibrated", "bool", "DDR calibrated as the static sees it (ddr_ready AND RP released)"),
    (0x1C0, 2, 1, "rv_rp_calib", "bool", "the RM's term of XRT's calibration bit (the PRUNED filler drives 1)"),
    (0x1C0, 3, 1, "rv_ecc_poller", "bool", "the loaded xclbin has the ECC poller"),
    (0x1C0, 4, 1, "rv_rp_released", "bool", "the reconfigurable partition is released"),
    (0x1C0, 5, 1, "rv_gate_open", "bool", "the host/P2P DDR window gate is open"),
    (0x1C0, 6, 1, "rv_ecc_accepting", "bool", "ECC snapshots are being accepted"),
    (0x1C4, 0, 16, "rv_loads_kept", "n", "xclbin loads with DDR (KEPT) since configuration/clear"),
    (0x1C4, 16, 16, "rv_loads_pruned", "n", "xclbin loads without DDR (PRUNED) since configuration/clear"),
    (0x1C8, 0, 16, "rv_calibrations", "n", "DDR calibrations completed since configuration/clear"),
    (0x1C8, 16, 16, "rv_calibrations_lost", "n", "calibrations lost while a KEPT xclbin was loaded"),
    (0x1CC, 0, 32, "rv_last_calibration_time", "cyc", "RP release to calibration, the last time"),
    (0x1D0, 0, 32, "ecc_ce_this_load", "n", "ECC corrected errors since the last xclbin load"),
    (0x1D4, 0, 16, "ecc_ue_polls_this_load", "n", "ECC polls that saw a UE since the last load"),
    (0x1D4, 29, 1, "ecc_polled_this_load", "bool", "the poller completed a poll since the last load"),
    (0x1D4, 31, 1, "ecc_ue_seen_this_load", "bool", "an uncorrectable error since the last load"),
    (0x1D8, 0, 32, "ecc_polls_this_load", "n", "ECC polls since the last load"),
    (0x1EC, 0, 32, "fenced_while_kept", "n", "host/P2P DDR accesses fenced while a KEPT xclbin was loaded (sat)"),
    (0x1F0, 0, 32, "ts_last_calibration", "s262", "uptime at the last calibration completed"),
    (0x1F4, 0, 32, "ts_last_fence_kept", "s262", "uptime at the last fence while KEPT"),
    (0x1F8, 0, 32, "ts_last_load", "s262", "uptime at the last load settle"),
    # route V r2
    (0x218, 0, 32, "fenced_in_link_down_window", "n", "fenced DDR accesses inside a link-down window (sat)"),
    (0x21C, 0, 32, "fenced_outside_link_down_window", "n", "fenced DDR accesses outside every link-down window (sat)"),
    (0x220, 0, 1, "fence_window_open", "bool", "a link-down window is open now"),
    (0x220, 1, 1, "fence_window_link_down", "bool", "the link is down now"),
    (0x220, 2, 1, "fence_window_mig_reset", "bool", "the link-down MIG reset is active now"),
    (0x220, 3, 1, "fence_window_waiting_recal", "bool", "waiting for recalibration after a link-down"),
    (0x220, 4, 1, "fence_window_limit_run_out", "bool", "the window's 2 s limit has run out (or never started)"),
    (0x220, 16, 16, "fence_window_limit", "s262", "the window's limit after link-up"),
]
for _b, _n in ((0x40, "p2p"), (0x60, "host"), (0x80, "peer")):
    COUNTER_FIELDS += [
        (_b + 0x00, 0, 16, "axi_%s_write_slverr" % _n, "n", "AXI monitor %s: write SLVERR responses (sat)" % _n.upper()),
        (_b + 0x00, 16, 16, "axi_%s_write_decerr" % _n, "n", "AXI monitor %s: write DECERR responses (sat)" % _n.upper()),
        (_b + 0x04, 0, 16, "axi_%s_read_slverr" % _n, "n", "AXI monitor %s: read SLVERR (sat)" % _n.upper()),
        (_b + 0x04, 16, 16, "axi_%s_read_decerr" % _n, "n", "AXI monitor %s: read DECERR (sat)" % _n.upper()),
        (_b + 0x14, 0, 16, "axi_%s_untracked" % _n, "n", "AXI monitor %s: errors with untracked address" % _n.upper()),
    ]


def decode_field(kind, v):
    """(decoded value, unit) of a counter field."""
    if kind == "n":
        return v, ""
    if kind == "bool":
        return bool(v), ""
    if kind == "hex":
        return "0x%x" % v, ""
    if kind == "s262":
        return round(v * TS_S, 6), "s"
    if kind == "ms262":
        return round(v * TS_S * 1e3, 3), "ms"
    if kind == "cyc":
        return round(v / ACLK_HZ * 1e3, 6), "ms"
    if kind == "code":
        return round(code2c(v), 2), "C"
    return v, ""


# ----------------------------------------------------------------------------------------------- documentation
def markdown():
    """The register-map document (docs/innova2_smi_regmap.md), generated from the tables above."""
    L = ["<!-- SPDX-FileCopyrightText: 2026 the innova2 contributors -->",
         "<!-- SPDX-License-Identifier: Apache-2.0 -->", "",
         "# Innova-2 XRT shell register map (fp8c lineage) — as used by innova2-smi", "",
         "Generated by `innova2-smi regmap --markdown` from `innova2_smi_regmap.py`. The authority is the read "
         "multiplexer in `ip_src/ddr_health/ddr_health.v` of the shell source; `innova2-smi regmap --check-rtl "
         "<ddr_health.v>` compares the two (offsets, and which offsets the RTL decodes).", "",
         "**Never read an offset that is not listed for the running shell.** An undecoded management-PF read "
         "hard-locks the host. innova2-smi reads OBS_ID (mgmt BAR0 0x36030) first and refuses every other access "
         "on an OBS_ID that is not below.", "",
         "## Shells", "", "| ROM_TS | hex | build | OBS_ID | DDR |", "|---|---|---|---|---|"]
    for ts, s in sorted(SHELLS.items()):
        L.append("| %d | 0x%08x | %s | 0x%08X | %s |" % (ts, ts, s["name"], s["obs_id"], s["ddr"]))
    L += ["", "## Layouts (OBS_ID)", "",
          "| OBS_ID | layout | released | throttle/PCIe/PR/recorder | live temperature, 2 tiers | AXI error monitors |",
          "|---|---|---|---|---|---|"]
    yn = lambda b: "yes" if b else "-"
    for oid, lay in sorted(LAYOUTS.items()):
        L.append("| 0x%08X | %s | %s | %s | %s | %s |" % (oid, lay["name"], yn(lay["released"]), yn(lay["e7"]),
                                                          yn(lay["e7a3"]), yn(lay["mon"])))
    L += ["", "Any other OBS_ID is refused. The unreleased layouts are accepted because their maps are known from the "
          "RTL, but innova2-smi has not been run on them.", "",
          "## Management PF BAR0 regions used", "", "| base | size | block |", "|---|---|---|"]
    for k, (b, n, d) in MGMT_REGIONS.items():
        L.append("| 0x%06X | 0x%X | %s |" % (b, n, d))
    L += ["", "Fixed registers: 0x32000 [0] XRT calibration bit; 0x32008 [1:0] requested DDR mode "
          "(0 on, 1 off, 2 emu, 3 hybrid); SYSMON DRP register d at 0xA0400 + 4*d (temperature 0x00, VCCINT 0x01, "
          "VCCAUX 0x02, VCCBRAM 0x06, max 0x20-0x23, min 0x24-0x27).", "",
          "## ddr_health (mgmt BAR0 + 0x36000)", "", "| offset | name | access | layouts | description |",
          "|---|---|---|---|---|"]
    allregs = {}
    for oid, lay in LAYOUTS.items():
        for o, r in lay["regs"].items():
            allregs.setdefault(o, (r, []))[1].append(oid)
    for o in sorted(allregs):
        (name, acc, desc), oids = allregs[o]
        who = "all" if len(oids) == len(LAYOUTS) else ", ".join("%X" % (x & 0xF) for x in sorted(oids))
        L.append("| 0x%03X | %s | %s | %s | %s |" % (o, name, acc, who, desc.replace("|", "/")))
    L += ["", "Layout column: the last hex digit of the OBS_IDs that decode the offset (1 = full, 2 = E4 layout, "
          "3 = E7a, 4 = E7b, 5 = E7a2, 6 = E7a3, 7 = E7a3-2666ca48, 8 = route V, B = route V r2, D = route V r3).", "",
          "Route V (OBS_ID 0x0B5E0008, VBNV %s): the DDR4 MIG lives in the dynamic region; an xclbin either has "
          "it (KEPT: its kernels use bank0) or not (PRUNED). 0x00-0x198 keep the E7a3 meaning; 0x14-0x2C stay "
          "cumulative since configuration (the in-RP ECC poller sends deltas over the RP boundary) and 0x1D0-0x1E8 "
          "hold the same values for the current load only. 0x10 [16] needs a KEPT xclbin to become 1. XRT's "
          "calibration bit (0x32000 [0]) is the KEPT MIG's calibration, and 1 under a PRUNED xclbin. The DDR "
          "mode register keeps its 2 bits but the gate runs ON/OFF only: a requested EMU runs ON and HYBRID runs "
          "OFF (EMU/HYBRID deferred). Fences while PRUNED are expected (no DDR), fences while KEPT are counted "
          "at 0x1EC. Recorder types 20 RM_DDR, 21 CAL_DONE, 22 CAL_LOST." % VBNV_ROUTEV, "",
          "Route V r2 (OBS_ID 0x0B5E000B, same VBNV): the route-V map plus 0x218 FENCE_LDWIN / 0x21C FENCE_OUTWIN / "
          "0x220 FENCE_WIN. A link-down window is open while the link is down, the link-down MIG reset is active, or "
          "(after a link-down) the static waits for recalibration and less than 2 s have passed since link-up; it "
          "closes at RECAL_DONE or at the limit. FENCE_LDWIN + FENCE_OUTWIN = FENCE (0xA0). The r2 flash image boots "
          "with the PRUNED filler in the dynamic region, so RV_INFO/DDR_INFO read 'no DDR' until a KEPT xclbin loads.",
          "",
          "Route V r3 (OBS_ID 0x0B5E000D, same VBNV): the r2 map and ddr_health RTL unchanged; the ID marks the r3 "
          "static (RP-side PEER/HOST pipelining, PLRAM pblocks) and its link-default CA drive RDRV_60_60.",
          "",
          "PR_COUNT (0x120) counts RP ISOLATIONS: XRT isolates the RP twice per `xbutil program` (a zero-length "
          "isolate first, then the reconfiguration), so it rises by 2 per xclbin load. Route V's RV_LOADS (0x1C4) "
          "counts each load once, 262 us after its release, plus the configuration's own RM at power-on.", "",
          "Units: timestamps are uptime[47:16] (262.144 us); recorder records are 64 bits, [63:58] type "
          "[57:40] payload [39:0] uptime[47:8] (1.024 us). Temperature code to C: code * 509.3140064 / 1024 - "
          "280.2308787.", "",
          "Throttle power-on defaults: E7a2 THR_CTRL 0x31, THR_THRESH 0x02D402E9 (745/724); E7a3 THR_CTRL 0x31, "
          "THR_THRESH 0x02C102D5 (725/705 = 80.37/70.42 C), THR_THRESH2 0x02D502E9 (745/725 = 90.31/80.37 C), "
          "THR_TIER 0x00010100.", "",
          "## User PF BAR0 regions used", "", "| base | size | block |", "|---|---|---|"]
    for k, (b, n, d) in USER_REGIONS.items():
        L.append("| 0x%06X | 0x%X | %s |" % (b, n, d))
    L += ["", "peer_window: 0x00 ID 0x50454552, 0x04 CTRL [0] enable, 0x08/0x0C BASE0, 0x10 WIN_LOG2, 0x14 writes "
          "forwarded, 0x18 reads forwarded, 0x1C refused, 0x20 NWIN, 0x28-0x3C BASE1-3. p2p_alias: 0x00 ID "
          "0x50414C49, 0x04 CTRL [0] enable [8] in force, 0x08/0x0C ALIAS, 0x10 WIN_LOG2, 0x18/0x1C TGT_BASE, "
          "0x20/0x24 writes/reads redirected, 0x28 writes outstanding.", ""]
    L += ["## MIG configuration per build (`innova2-smi ddr`)", "",
          "No shell reports its MIG configuration in a register, so innova2-smi keeps this table by ROM_TS (read from "
          "each build's generated MIG RTL, IP parameters, par XDC, CA-drive gate and create_project log). Timing in "
          "DDR4 clock cycles; every build's PHY is 4:1, so ui_clk = 1/(4 tCK) and MT/s = 8 x ui_clk. The ui_clk is "
          "measured passively: the ECC poller completes one poll per POLL_CYC ui_clk cycles (0x2C counts them), "
          "timed by the 250 MHz uptime counter (0xB0/0xB4); precision about 2/polls + 0.08 %.", "",
          "| ROM_TS | build | variant | part | tCK ps | MT/s | CL/CWL | tRCD/tRP/tRAS/tRFC | width | ECC | "
          "InputClockPeriod ps | CA drive | POLL_CYC |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for ts in sorted(MIG_BUILDS):
        m = mig_config(ts)
        L.append("| %d | %s | %s | %s | %d | %.0f | %d/%d | %d/%d/%d/%d | %d | %s | %d | %s | %d |" % (
            ts, SHELLS.get(ts, {}).get("name", "?"), m["variant"], m["part"], m["tck_ps"], m["mts"], m["cl"], m["cwl"],
            m["trcd"], m["trp"], m["tras"], m["trfc"], m["dq_width"], "on" if m["ecc"] else "off", m["input_clk_ps"],
            m["ca_drive"], m["poll_cyc"]))
    L += ["", "Dw8 CSV builds use `%s`. Route V's MIG is in each KEPT xclbin: its row is the template's. Any other "
          "ROM_TS is an unknown build." % DW8_CSV, ""]
    return "\n".join(L)


def check_rtl(text):
    """Compare a ddr_health.v read multiplexer with LAYOUTS. Returns (obs_id, problems list)."""
    import re
    m = re.search(r"OBS_ID\s*=\s*32'h([0-9A-Fa-f_]+)", text)
    if not m:
        return None, ["no OBS_ID localparam in the file"]
    oid = int(m.group(1).replace("_", ""), 16)
    lay = LAYOUTS.get(oid)
    if not lay:
        return oid, ["OBS_ID 0x%08X is not a layout innova2-smi accepts" % oid]
    body = text[text.find("case (s_axi_araddr[11:2])"):]
    body = body[:body.find("endcase")]
    rtl = {int(x, 16) * 4 for x in re.findall(r"10'h([0-9A-Fa-f]+)\s*:", body)}
    if not rtl:
        return oid, ["no read multiplexer found"]
    mine = set(lay["regs"])
    probs = ["0x%03X in the map but not decoded by the RTL (reads 0)" % o for o in sorted(mine - rtl)]
    probs += ["0x%03X decoded by the RTL but not in the map" % o for o in sorted(rtl - mine)]
    return oid, probs
