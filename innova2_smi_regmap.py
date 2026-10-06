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
"""

import math

# ----------------------------------------------------------------------------------------------- platform identity
VBNV_FP8C = "innova2_innova2_xdma_202320_3"       # fp8c and later (PLATFORM_VENDOR innova2)
VBNV_OLD = "xilinx_innova2_xdma_202320_3"          # pre-fp8c production shells (inn2x r3..r7): identity only
FPGA_VENDOR, FPGA_MGMT_DEV, FPGA_USER_DEV = "0x10ee", "0x788f", "0x6a90"
CX5_VENDOR, BOPE_DEV = "0x15b3", "0x0264"

# Builds this tool knows by ROM timestamp (ROM_TS = the feature-ROM TimeSinceEpoch = rom.m.*/timestamp = the
# xclbin's m_featureRomTimeStamp). Several builds share an OBS_ID, so the ROM_TS is what names the build; the OBS_ID
# is what selects the register map. A build that is not listed here is still handled if its OBS_ID is known.
SHELLS = {
    1792022400: dict(name="E6b", obs_id=0x0B5E0002, uuid="2c2e0dfa2a774cec299b3fe47a5c6627", ddr="DDR4-2666 72-bit ECC"),
    1792368000: dict(name="E6b-2400", obs_id=0x0B5E0002, uuid="389b4c78ceec074b8d869b79e6a385f2", ddr="DDR4-2400 72-bit ECC"),
    1792371600: dict(name="E6b-2400dw8", obs_id=0x0B5E0002, uuid="a046ff5ca633dbd389ba09abdada8923",
                     ddr="DDR4-2400 72-bit ECC (device width 8)"),
    1792281600: dict(name="E7a2", obs_id=0x0B5E0005, uuid="50d5d173eb28dc07390e435c16516a34", ddr="DDR4-2666 72-bit ECC"),
    1792458000: dict(name="E7a3-2400", obs_id=0x0B5E0006, uuid="5970d74a63e26d3fe4041d0b50f5319f",
                     ddr="DDR4-2400 72-bit ECC (dw8_833)"),
    1792461600: dict(name="E7a3-2666ca48", obs_id=0x0B5E0007, uuid=None, ddr="DDR4-2666 72-bit ECC, CA RDRV_48_48"),
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
}
OBS_ID_OFF, OBS_CLEAR = 0x30, 0xC1EAC1EA

# Power-on defaults of the throttle registers, per layout (restored by 'throttle defaults' and by the tests).
THR_DEFAULTS = {
    0x0B5E0003: {0xC0: 0x00000031, 0xC4: 0x02D402E9},
    0x0B5E0004: {0xC0: 0x00000031, 0xC4: 0x02D402E9},
    0x0B5E0005: {0xC0: 0x00000031, 0xC4: 0x02D402E9},
    0x0B5E0006: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
    0x0B5E0007: {0xC0: 0x00000031, 0xC4: 0x02C102D5, 0xD8: 0x02D502E9, 0xDC: 0x00010100},
}


def layout(obs_id):
    return LAYOUTS.get(obs_id)


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
             14: "PCIE_FATAL", 15: "PCIE_LINK", 16: "OBS_CLEAR", 17: "THR_TIER", 18: "TEMP_STALE", 19: "TEMP_FRESH"}
DDR_BANK0_BASE = 0x200000000          # XRT device address of bank0 (AXI address 0)
VALIDATED_BOARDS = {("MNV303212A-ADLT", "A7")}
DDR_PART = "Micron MT40A1G16KNR-075 (16 Gb x16, DDR4-2666 grade)"


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
    """axlf header fields innova2-smi checks before loading anything: magic, featureRomTimeStamp, VBNV, uuid."""
    import struct
    if len(b) < 432 or b[:7] != b"xclbin2":
        return None
    return dict(rom_ts=struct.unpack_from("<Q", b, 320)[0],
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
          "3 = E7a, 4 = E7b, 5 = E7a2, 6 = E7a3, 7 = E7a3-2666ca48).", "",
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
