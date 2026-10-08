#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for innova2-smi against mock hosts and a mock register model per OBS_ID.

The model's DECODED address set is written out here from the ddr_health.v read multiplexers (not taken from
innova2_smi_regmap), so it checks the tool's maps independently: any read the real shell would not decode raises
HardLock, which is what the host would do. Run:  python3 -m unittest discover -s tests -v
Nothing here touches hardware, /tmp, or the user's home (HOME is pointed at tests/.state)."""

import io
import json
import os
import shutil
import sys
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from fnmatch import fnmatch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
STATE = os.path.join(HERE, ".state")
os.environ["HOME"] = STATE

import innova2_smi as SMI            # noqa: E402
import innova2_smi_check as C        # noqa: E402
import innova2_smi_hw as H           # noqa: E402
import innova2_smi_regmap as R       # noqa: E402
import innova2_smi_tests as T        # noqa: E402

# ----------------------------------------------------------------------------------------------- RTL-decoded sets
# word offsets x4 from each build's ddr_health.v `case (s_axi_araddr[11:2])` (2026-10-06)
_BASE = [0x0, 0x4, 0x8, 0xc, 0x10, 0x14, 0x18, 0x1c, 0x20, 0x24, 0x28, 0x2c, 0x30, 0x34, 0x38, 0x3c,
         0xa0, 0xa4, 0xa8, 0xac, 0xb0, 0xb4, 0xb8]
_E7 = [0xc0, 0xc4, 0xc8, 0xcc, 0xd0, 0xd4, 0xe0, 0xe4, 0xe8, 0xec, 0xf0, 0xf4, 0xf8, 0x100, 0x104, 0x108, 0x10c,
       0x110, 0x120, 0x124, 0x128, 0x180, 0x184, 0x188, 0x18c, 0x190, 0x194, 0x198]
_E7A3 = [0xd8, 0xdc, 0x140, 0x144, 0x148, 0x14c, 0x150, 0x154]
_MON = [0x40, 0x44, 0x48, 0x4c, 0x50, 0x54, 0x60, 0x64, 0x68, 0x6c, 0x70, 0x74, 0x80, 0x84, 0x88, 0x8c, 0x90, 0x94]
# route V (OBS_ID 0x0B5E0008): clkx300fp8c_routev_src/ip_src/ddr_health/ddr_health.v, 10'h070..10'h07E
_RV = list(range(0x1c0, 0x1fc, 4)) + list(range(0x200, 0x218, 4))
# route V r2 (OBS_ID 0x0B5E000B): clkx300fp8c_routev2_src ddr_health.v, + 10'h086..10'h088
_RV2 = [0x218, 0x21c, 0x220]
RTL_DECODE = {0x0B5E0002: _BASE, 0x0B5E0004: _BASE + _E7 + _MON, 0x0B5E0005: _BASE + _E7,
              0x0B5E0006: _BASE + _E7 + _E7A3, 0x0B5E0007: _BASE + _E7 + _E7A3, 0x0B5E0008: _BASE + _E7 + _E7A3 + _RV,
              0x0B5E000B: _BASE + _E7 + _E7A3 + _RV + _RV2,
              0x0B5E000C: _BASE + _E7 + _E7A3 + _RV2,      # fp8c rc1: clkx300fp8c_rc1_src ddr_health.v
              0x0B5E000D: _BASE + _E7 + _E7A3 + _RV + _RV2}  # route V r3: clkx300fp8c_routev3_src ddr_health.v (= r2)
ROM_TS = {0x0B5E0002: 1792371600, 0x0B5E0004: 1792000004, 0x0B5E0005: 1792281600, 0x0B5E0006: 1792458000,
          0x0B5E0007: 1792461600, 0x0B5E0008: 1792465200, 0x0B5E000B: 1792479600, 0x0B5E000C: 1792483200,
          0x0B5E000D: 1792486800}
RV = 0x0B5E0008
RV2 = 0x0B5E000B
RC1 = 0x0B5E000C
RV3 = 0x0B5E000D
ROUTEV_RTL = "/nobackup2/xilinx_shells_work/clkx300fp8c_routev_src/ip_src/ddr_health/ddr_health.v"
ROUTEV2_RTL = "/nobackup2/xilinx_shells_work/clkx300fp8c_routev2_src/ip_src/ddr_health/ddr_health.v"


def vbnv_of(obs_id):
    return R.VBNV_ROUTEV if obs_id in (RV, RV2, RV3) else R.VBNV_FP8C
WRITABLE = {0x30, 0xC0, 0xC4, 0xD8, 0xDC, 0x18C}


class HardLock(Exception):
    """What an undecoded management read does to the real host."""


def c2(c):
    return R.c2code(c)


class Model:
    """ddr_health + memcalib + SYSMON + user-PF PEER/alias, with time-driven counters and the tier state machine."""

    def __init__(self, host, obs_id, temp_c=41.6, mutants=()):
        self.h, self.obs, self.mut = host, obs_id, set(mutants)
        self.decoded = {0x36000 + o for o in RTL_DECODE.get(obs_id, [0x30])} | {0x32000, 0x32008} | \
            set(range(0xA0400, 0xA0800, 4))
        self.e7 = obs_id in (0x0B5E0004, 0x0B5E0005, 0x0B5E0006, 0x0B5E0007, RV, RV2, RV3, RC1)
        self.e7a3 = obs_id in (0x0B5E0006, 0x0B5E0007, RV, RV2, RV3, RC1)
        self.routev = obs_id in (RV, RV2, RV3)
        # route V model: the loaded xclbin is KEPT (MIG, poller) or PRUNED (filler); calibrated only when KEPT
        self.rv = dict(kept=True, cal=True, released=True, fence_kept=0, lost=0, loads_kept=1, loads_pruned=0,
                       tck=750, w2=0x3348)          # the KEPT xclbin's DDR_INFO: tCK, width/ECC/CA/variant word
        self.code = c2(temp_c)
        self.r = {o: 0 for o in RTL_DECODE.get(obs_id, [0x30])}
        self.r[0x30] = obs_id
        self.r[0x00] = 0x100000F9                       # mode ON, gate open
        self.r[0x04] = self.r[0x0C] = 0xFFFFFFFF
        self.r[0x18] = 0x60000000                       # ECC on, poller alive
        self.r[0x34] = 0x001F001F
        self.r[0xB8] = 250000000
        self.memcalib, self.mode = 1, 0
        if self.e7:
            self.r[0xC0] = 0x31
            self.r[0xC4] = R.THR_DEFAULTS.get(obs_id, {0xC4: 0x02D402E9})[0xC4]
            self.r[0x128] = 1
            self.r[0x10C] = 0x30210
            self.r[0x180] = 0x0110                       # 2^16 records, format 1
        if self.e7a3:
            self.r[0xD8], self.r[0xDC] = 0x02D502E9, 0x00010100
        if self.routev:
            self.r[0x1CC] = int(0.48 * 250e6)              # calibrated 480 ms after its load
        self.tier, self.alarm = 0, 0
        self.t0 = host.t
        self.samples_base = 1000
        self.records = [(1, 0, 0)]
        self.fr_idx, self.reads, self.writes = 0, [], []
        self.sysmon = {0x00: self.code << 6, 0x01: 0x4800, 0x02: 0x9900, 0x06: 0x4800, 0x20: (self.code + 5) << 6,
                       0x21: 0x4900, 0x22: 0x9A00, 0x23: 0x4900, 0x24: (self.code - 5) << 6, 0x25: 0x4700,
                       0x26: 0x9800, 0x27: 0x4700}
        self.peer = {0x00: R.PEER_ID, 0x04: 1, 0x08: 0xFB000000, 0x10: 20, 0x20: 4}
        self.alias = {0x00: R.ALIAS_ID, 0x04: 0x101, 0x08: 0x800000, 0x10: 18, 0x18: 0x2000000}
        self.base_mhz = 300.0
        self.ui_mhz = None                                # None: the build table's ui_clk (a mis-built shell sets it)

    # -- dynamics
    def now(self):
        return self.h.t - self.t0

    def _samples(self):
        if "temp_stuck" in self.mut:
            return self.samples_base
        return self.samples_base + int(self.now() / 0.1)

    def _eval_tiers(self):
        n = self._samples()
        last = getattr(self, "_last_eval", n - 1)
        for _ in range(min(5, n - last)):
            set1, rel1 = self.r.get(0xC4, 0) & 0x3FF, self.r.get(0xC4, 0) >> 16 & 0x3FF
            set2, rel2 = self.r.get(0xD8, 0) & 0x3FF, self.r.get(0xD8, 0) >> 16 & 0x3FF
            old = self.tier
            if self.e7a3:
                if self.tier == 0:
                    self.tier = 2 if self.code >= set2 else (1 if self.code >= set1 else 0)
                elif self.tier == 1:
                    self.tier = 2 if self.code >= set2 else (0 if self.code < rel1 else 1)
                elif self.tier == 2 and self.code < rel2:
                    self.tier = 1
                if old != self.tier:
                    self.r[0x154] = self.r.get(0x154, 0) + 1
                    self.records.append((17, old << 12 | self.tier << 10 | self.code, int(self.now() * 1e6)))
            elif self.e7:
                if not self.alarm and self.code >= set1:
                    self.alarm = 1
                elif self.alarm and self.code < rel1:
                    self.alarm = 0
                self.tier = self.alarm
        self._last_eval = n

    def thr_status(self):
        self._eval_tiers()
        c = self.r[0xC0]
        en, force, flvl, gd, gk = c & 1, c >> 1 & 1, c >> 2 & 3, c >> 4 & 1, c >> 5 & 1
        stale = self.e7a3 and "temp_stuck" in self.mut and self.now() > 1.0
        fs = stale and (self.r.get(0xDC, 0) >> 16 & 1)
        tier_eff = 2 if fs else self.tier
        req = en and (force or tier_eff != 0)
        if force:
            lvl = flvl
        elif self.e7a3:
            tl = self.r[0xDC]
            lvl = (tl & 3) if tier_eff == 1 else ((tl & 3) if "tier2_level_ignored" in self.mut else tl >> 8 & 3)
        else:
            lvl = flvl
        return (self.code << 16 | self.tier << 12 | (lvl & 3) << 8 | fs << 7 | stale << 6 | tier_eff << 4 |
                (req and gk) << 3 | (req and gd) << 2 | req << 1 | (tier_eff != 0))

    def poll_hz(self):
        cfg = R.mig_config(ROM_TS.get(self.obs))
        if self.obs in (0x0B5E0008, 0x0B5E000B, 0x0B5E000D) and self.rv["kept"]:
            # route V: the poller is the LOADED xclbin's, running at its MIG's ui_clk with its variant's POLL_CYC
            ui = self.ui_mhz if self.ui_mhz is not None else 1e6 / (4 * self.rv["tck"])
            return ui * 1e6 / R.VARIANT_POLL_CYC[R.MIG_VARIANTS[self.rv["w2"] >> 12 & 0xF]]
        ui = self.ui_mhz if self.ui_mhz is not None else (cfg["ui_clk_mhz"] if cfg else 333.0)
        return ui * 1e6 / (cfg["poll_cyc"] if cfg else 333000)

    def delivered_mhz(self):
        st = self.thr_status()
        if not st >> 2 & 1:
            return self.base_mhz
        return self.base_mhz / R.lvl(st >> 8)

    # -- route V helpers (tests change self.rv, the model derives every register from it)
    def set_pruned(self, pruned=True):
        self.rv.update(kept=not pruned, cal=not pruned)
        if pruned:
            self.rv["loads_pruned"] += 1
        else:
            self.rv["loads_kept"] += 1

    def _rv_reg(self, o):
        rv = self.rv
        ok = rv["kept"] and rv["cal"] and rv["released"]
        if o == 0x1C0:
            rp_calib = rv["cal"] if rv["kept"] else 1
            return (R.RV_SIGNATURE << 16 | ok << 6 | ok << 5 | rv["released"] << 4 | rv["kept"] << 3 |
                    rp_calib << 2 | (rv["kept"] and rv["cal"] and rv["released"]) << 1 | rv["kept"])
        if o == 0x1C4:
            return rv["loads_pruned"] << 16 | rv["loads_kept"]
        if o == 0x1C8:
            return rv["lost"] << 16 | rv["loads_kept"]
        if o == 0x1D4:
            return (1 << 29) if ok else 0
        if o == 0x1D8:
            return int(self.now() * 1000) if ok else 0
        if o == 0x1EC:
            return rv["fence_kept"]
        if o == 0x200:
            return (0xD4D4 << 16 | rv["tck"]) if rv["kept"] else 0x0D0D0000
        if o == 0x204:
            return 0x14141213 if rv["kept"] else 0
        if o == 0x208:
            return rv["w2"] if rv["kept"] else 0
        if o == 0x210:
            return 10923 if rv["kept"] else 0         # 333.3 MHz / 128 over 2^20 aclk (4.19 ms)
        if o == 0x214:
            return (rv["kept"] << 16) | 7 << 8 | 20
        return self.r.get(o, 0)

    # -- bus
    def read(self, off):
        if off not in self.decoded:
            raise HardLock("UNDECODED mgmt read 0x%06x" % off)
        self.reads.append(off)
        if off == 0x32000:
            if self.routev:                               # route V: rp_calib AND released (ddr_health xrt_calib)
                rv = self.rv
                return int(rv["released"] and (rv["cal"] if rv["kept"] else 1))
            return self.memcalib
        if off == 0x32008:
            return self.mode
        if 0xA0400 <= off < 0xA0800:
            return self.sysmon.get((off - 0xA0400) // 4, 0)
        o = off - 0x36000
        if self.routev and 0x1C0 <= o <= 0x214:
            return self._rv_reg(o)
        if self.routev and o == 0x00:                    # gate open only while a KEPT xclbin is calibrated
            ok = self.rv["kept"] and self.rv["cal"] and self.rv["released"]
            return (self.r[0x00] & ~(1 << 28)) | (ok << 28) if (self.r[0x00] >> 29 & 3) == 0 else self.r[0x00]
        up = int((self.now() + 120.0) * 250e6)
        if o == 0xB0:
            self._hi = up >> 32
            return up & 0xFFFFFFFF
        if o == 0xB4:
            return getattr(self, "_hi", 0) if "uptime_stuck" not in self.mut else 0
        if o == 0x2C:                                     # one poll per POLL_CYC cycles of the build's ui_clk
            return self.r[0x2C] if "polls_stuck" in self.mut else (self.r[0x2C] + int(self.now() * self.poll_hz())) & 0xFFFFFFFF
        if o == 0x140:
            return self._samples()
        if o == 0x144:
            return 300 if "temp_stuck" in self.mut else 300 + self._samples() // 3
        if o == 0x148:
            return (0xFFFF if "temp_stuck" in self.mut and self.now() > 1 else 200) << 16 | (self.code << 6 | 0x1E)
        if o == 0x14C:
            st = self.thr_status() if self.e7 else 0
            return (st >> 6 & 1) << 16 | (st >> 7 & 1) << 17 | self.r.get(0x14C, 0)
        if o == 0xC8:
            return self.thr_status()
        if o == 0x184:
            return len(self.records)
        if o in (0x190, 0x194, 0x198):
            t, p, ts = self.records[self.fr_idx] if self.fr_idx < len(self.records) else (0, 0, 0)
            v = t << 58 | p << 40 | (ts // 1)
            return {0x190: v & 0xFFFFFFFF, 0x194: v >> 32, 0x198: 1}[o]
        return self.r.get(o, 0)

    def write(self, off, val):
        self.writes.append((off, val))
        if off == 0x32008:
            self.mode = val & 3
            st = self.r[0x00] & ~(3 << 29) & ~(1 << 31)
            self.r[0x00] = st | (val & 3) << 29 | ((val & 3) in (1, 2)) << 31
            return
        o = off - 0x36000
        if o not in WRITABLE:
            raise AssertionError("model: write to read-only 0x%x reached the bus" % off)
        if o == 0x30:
            if val == R.OBS_CLEAR:
                for k in (0x38, 0x3C, 0xA0, 0x100, 0xCC):
                    self.r[k] = 0
            return
        if o == 0x18C:
            self.fr_idx = val
            return
        self.r[o] = val

    def user_read(self, off):
        if R.PEER_BASE <= off < R.PEER_BASE + 0x1000:
            return self.peer.get(off - R.PEER_BASE, 0)
        if R.ALIAS_BASE <= off < R.ALIAS_BASE + 0x1000:
            return self.alias.get(off - R.ALIAS_BASE, 0)
        raise HardLock("UNDECODED user read 0x%x" % off)


class MockIO(H.RegIO):
    def __init__(self, model, user, guard):
        self.model, self.user, self.guard = model, user, guard

    def rd(self, off):
        self.guard.check_read(off)
        return self.model.user_read(off) if self.user else self.model.read(off)

    def wr(self, off, val):
        self.guard.check_write(off)
        self.model.write(off, val & 0xFFFFFFFF)


def vpd(pn="MNV303212A-ADLT", ec="A7", sn="MT1935X08327"):
    def kw(k, v):
        return k.encode() + bytes([len(v)]) + v.encode()
    name = b"Innova-2 Flex Open"
    ro = kw("PN", pn) + kw("EC", ec) + kw("SN", sn) + b"RV\x01\x00"
    return bytes([0x82, len(name), 0]) + name + bytes([0x90, len(ro), 0]) + ro + b"\x78"


class MockHost:
    """One Innova-2 card (or a variant): CX5 09:00.0/.1 behind switch 06:00.0, FPGA 08:00.0/.1."""

    def __init__(self, obs_id=0x0B5E0006, fpga="shell", xclmgmt=True, cx5=None, mutants=(), temp_c=41.6,
                 slot_width="8", extra_attrs=None, files=None):
        self.t = 1000.0
        self.attrs_ = {}
        self.files = dict(files or {})
        self.links = {}
        self.devs = []
        self.runs = []
        self.cx5 = dict(admin=0, oper=0, status=0, transport="kmod") if cx5 is None else cx5
        sw, rp = "0000:06:00.0", "0000:00:02.0"
        self._dev("0000:09:00.0", "0x15b3", "0x1017", "0x020000", [rp, sw, "0000:07:10.0"], "mlx5_core")
        self._dev("0000:09:00.1", "0x15b3", "0x1017", "0x020000", [rp, sw, "0000:07:10.0"], "mlx5_core")
        self._dev(sw, "0x15b3", "0x1974", "0x060400", [rp], "pcieport")
        self.attrs_[(sw, "device")] = "0x1974"
        for b, s, w in ((sw, "8.0 GT/s PCIe", slot_width), (rp, "8.0 GT/s PCIe", "8")):
            self.attrs_[(b, "current_link_speed")] = s
            self.attrs_[(b, "current_link_width")] = w
            self.attrs_[(b, "max_link_speed")] = "8.0 GT/s PCIe"
            self.attrs_[(b, "max_link_width")] = "8"
        self.attrs_[("0000:09:00.0", "vpd")] = vpd()
        self.files["/dev/0000:09:00.0_mlx5_fpga_tools"] = b""
        self.model = None
        m, u = "0000:08:00.0", "0000:08:00.1"
        if fpga == "shell":
            self._dev(m, "0x10ee", "0x788f", "0x120000", [rp, sw, "0000:07:08.0"], "xclmgmt" if xclmgmt else None)
            self._dev(u, "0x10ee", "0x6a90", "0x120000", [rp, sw, "0000:07:08.0"], "xocl")
            self.model = Model(self, obs_id, temp_c=temp_c, mutants=mutants)
            ts = ROM_TS.get(obs_id, 1792999999)
            if xclmgmt:
                self.attrs_.update({(m, "rom.m.1/VBNV"): vbnv_of(obs_id), (m, "rom.m.1/timestamp"): str(ts),
                                    (m, "clock_wizard.m.1/clock_wiz_freqs"): "300\n500\n0\n0",
                                    (m, "clock_freq_counter.m.1/clock_counter_freqs"): "300\n500\n0\n0",
                                    (m, "ready"): "1", (m, "mig_calibration"): "1",
                                    (m, "firewall.m.1/status"): "0", (m, "firewall.m.1/detected_status"): "0",
                                    (m, "hwmon/hwmon3/temp4_label"): "FPGA TEMP",
                                    (m, "hwmon/hwmon3/temp4_input"): str(int(round(temp_c)) * 1000),
                                    (m, "hwmon/hwmon3/in9_label"): "VCC INT", (m, "hwmon/hwmon3/in9_input"): "843",
                                    (m, "hwmon/hwmon3/fan1_label"): "FAN SPEED", (m, "hwmon/hwmon3/fan1_input"): "6660",
                                    (m, "hwmon/hwmon3/in0_label"): "12V PEX", (m, "hwmon/hwmon3/in0_input"): "0"})
            self.attrs_.update({(u, "ready"): "0x1", (u, "xclbinuuid"): "97613339-17af-2573-b200-1d96740165e0",
                                (u, "p2p.u.1/p2p_enable"): "1", (u, "p2p.u.1/config"):
                                "bar:268435456 max_bar:8589934592 exp_bar:8589934592", (u, "host_mem_size"): "0",
                                (u, "mailbox_connect_state"): "0x3", (u, "firewall.u.1/status"): "0",
                                (u, "firewall.u.1/detected_status"): "0"})
            for b in (m, u):
                self.attrs_[(b, "current_link_speed")] = "8.0 GT/s PCIe"
                self.attrs_[(b, "current_link_width")] = "8"
                self.attrs_[(b, "max_link_speed")] = "8.0 GT/s PCIe"
                self.attrs_[(b, "max_link_width")] = "8"
                self.attrs_[(b, "aer_dev_correctable")] = "RxErr 0\nTOTAL_ERR_COR 0"
                self.attrs_[(b, "aer_dev_nonfatal")] = "TOTAL_ERR_NONFATAL 0"
                self.attrs_[(b, "aer_dev_fatal")] = "TOTAL_ERR_FATAL 0"
            self.files["/lib/firmware/xilinx/10ee-788f-4351-%016x.xsabin" % ts] = b"x"
            hdr = bytearray(512)
            hdr[:8] = b"xclbin2\0"
            hdr[320:328] = ts.to_bytes(8, "little")
            vb = vbnv_of(obs_id)
            hdr[352:352 + len(vb)] = vb.encode()
            d = "/opt/xilinx/dsa/%s/test" % vb
            for f in ("verify.xclbin", "bandwidth.xclbin"):
                self.files[d + "/" + f] = bytes(hdr)
            self.xclbin_hdr = bytes(hdr)
            self.examine = "|[%s]  |%s  |0x%08x  |user(inst=130)  |Yes            |" % (u, vbnv_of(obs_id), ts)
        elif fpga == "tester":
            self._dev(m, "0x10ee", "0x9038", "0x058000", [rp, sw, "0000:07:08.0"], None)
            self.examine = ""
        elif fpga == "bope":
            self._dev("0000:08:00.0", "0x15b3", "0x0264", "0x120000", [rp, sw, "0000:07:08.0"], None)
            self.examine = ""
        else:
            self.examine = ""
        self.attrs_.update(extra_attrs or {})

    def _dev(self, bdf, v, d, cls, chain, driver):
        self.devs.append(dict(bdf=bdf, vendor=v, device=d, cls=cls, chain=chain))
        self.attrs_[(bdf, "vendor")] = v
        self.attrs_[(bdf, "device")] = d
        self.attrs_[(bdf, "driver")] = driver

    # -- Host interface
    def pci_devices(self):
        return [dict(d) for d in self.devs]

    def attr(self, bdf, name):
        for (b, n), v in self.attrs_.items():
            if b == bdf and fnmatch(n, name):
                return v if not isinstance(v, bytes) else None
        return None

    def attrs(self, bdf, pattern):
        return {n.split("/")[-1]: v for (b, n), v in self.attrs_.items() if b == bdf and fnmatch(n, pattern)}

    def raw(self, bdf, name, n=4096):
        for (b, k), v in self.attrs_.items():
            if b == bdf and (k == name or fnmatch(k, name)) and isinstance(v, bytes):
                return v[:n]
        return None

    def driver(self, bdf):
        return self.attrs_.get((bdf, "driver"))

    def resource(self, bdf, n):
        if self.attrs_.get((bdf, "bar0_unassigned")):
            return 0, 0
        return (0xE0000000, 32 << 20) if n == 0 else (0, 0)

    def pci_command(self, bdf):
        return 0x0006

    def bar_size(self, bdf, n):
        return 256 << 20 if n == 4 else 32 << 20

    def regio(self, bdf, n, regions, guard):
        return MockIO(self.model, bdf.endswith(".1"), guard)

    def run(self, cmd, timeout=60, env=None):
        self.runs.append(cmd)
        if cmd[-1] == "examine":
            return 0, self.examine
        if cmd[0] == "devlink":
            return 1, "devlink not in the mock"
        if cmd[0] == "pgrep":
            return 1, ""
        return 0, ""

    def exists(self, path):
        return path in self.files or path in self.links

    def glob(self, pattern):
        return sorted({p for p in list(self.files) + list(self.links) if fnmatch(p, pattern)} |
                      {os.path.dirname(p) for p in self.files if fnmatch(os.path.dirname(p), pattern)})

    def read_file(self, path):
        v = self.files.get(path)
        return v.decode() if isinstance(v, bytes) else v

    def file_head(self, path, n):
        d = os.path.dirname(path)
        if d in self.links:
            path = os.path.join(os.path.dirname(d), self.links[d], os.path.basename(path))
        v = self.files.get(path)
        return v[:n] if v is not None else None

    def readlink(self, path):
        return self.links.get(path)

    def relink(self, link, target):
        self.links[link] = target

    def host_uptime(self):
        return 60.0

    def bounded(self, fn, timeout, default):
        try:
            return fn()
        except Exception:                               # noqa: BLE001
            return default

    def boot_id(self):
        return "boot-1"

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t

    def is_root(self):
        return True

    def which_xrt(self):
        return "/opt/xilinx/xrt/bin/xrt-smi"

    def cx5_query(self, bdf, transport="auto"):
        return dict(self.cx5)


def run_cli(host, argv, columns=0):
    """The CLI as a user runs it. columns=0 (default): human output is not folded, so content assertions do not
    depend on the layout; the width tests pass columns=80."""
    o, e = io.StringIO(), io.StringIO()
    old = os.environ.get("INNOVA2_SMI_COLUMNS")
    os.environ["INNOVA2_SMI_COLUMNS"] = str(columns)
    try:
        with redirect_stdout(o), redirect_stderr(e):
            rc = SMI.main(argv, host=host)
    finally:
        if old is None:
            os.environ.pop("INNOVA2_SMI_COLUMNS", None)
        else:
            os.environ["INNOVA2_SMI_COLUMNS"] = old
    return rc, o.getvalue(), e.getvalue()


def setUpModule():
    shutil.rmtree(STATE, ignore_errors=True)
    os.makedirs(STATE)


def tearDownModule():
    shutil.rmtree(STATE, ignore_errors=True)


# ----------------------------------------------------------------------------------------------- tests
class RegmapTests(unittest.TestCase):
    def test_maps_match_rtl_lists(self):
        for oid, offs in RTL_DECODE.items():
            self.assertEqual(set(R.LAYOUTS[oid]["regs"]), set(offs), "OBS_ID 0x%08X" % oid)

    def test_read_set_inside_decoded(self):
        for oid in RTL_DECODE:
            m = Model(MockHost(oid), oid)
            for off in R.read_set(oid):
                m.read(off)                     # raises HardLock if the tool could read an undecoded address

    def test_unknown_obs_id_reads_only_obs_id(self):
        self.assertEqual(R.read_set(0x12345678), frozenset([0x36030]))
        self.assertEqual(R.write_set(0x12345678), frozenset())

    def test_check_rtl(self):
        txt = "localparam [31:0] OBS_ID = 32'h0B5E0002;\ncase (s_axi_araddr[11:2])\n" + "".join(
            "10'h%03X: rd_mux = 0;\n" % (o // 4) for o in _BASE) + "endcase"
        self.assertEqual(R.check_rtl(txt), (0x0B5E0002, []))
        oid, probs = R.check_rtl(txt.replace("10'h02E:", "10'h0FF:"))
        self.assertTrue(probs)

    def test_markdown_lists_every_offset(self):
        md = R.markdown()
        for oid, lay in R.LAYOUTS.items():
            for o in lay["regs"]:
                self.assertIn("| 0x%03X |" % o, md)

    def test_formulas(self):
        self.assertEqual(R.c2code(80), 725)
        self.assertAlmostEqual(R.code2c(725), 80.37, places=2)
        d = R.decode_ffa(0x1 << 24 | 0x8 << 8)
        self.assertEqual(d["row"], 1)
        t, p, ts = R.decode_record(17 << 58 | 5 << 40 | 1000)
        self.assertEqual((t, p), (17, 5))


class DiscoveryTests(unittest.TestCase):
    def _card(self, host):
        cs = H.discover(host)
        self.assertEqual(len(cs), 1)
        H.classify(host, cs[0])
        return cs[0]

    def test_normal(self):
        h = MockHost()
        c = self._card(h)
        self.assertEqual((c["mgmt"], c["user"], c["cx5"][0]), ("0000:08:00.0", "0000:08:00.1", "0000:09:00.0"))
        sh = H.Shell(h, c).identify()
        self.assertTrue(sh.ok)
        self.assertEqual(sh.state, "OUR_SHELL")
        self.assertEqual(sh.name, "E7a3-2400")

    def test_fpga_missing(self):
        h = MockHost(fpga=None)
        c = self._card(h)
        self.assertEqual(c["state"], "FPGA_MISSING")
        self.assertTrue(any(s == "FAIL" and "inconsistent" in t for s, t in c["flags"]))
        r = C.run_check(h, c, interval=0)
        self.assertEqual(r["severity"], "FAIL")
        self.assertIn("fpga.missing", [i["rule"] for i in r["items"]])

    def test_flex(self):
        h = MockHost(fpga="bope", cx5=dict(admin=1, oper=1, status=0, transport="kmod"))
        c = self._card(h)
        self.assertEqual(c["state"], "FLEX")
        self.assertEqual(c["bope"], "0000:08:00.0")
        r = C.run_check(h, c, interval=0)
        self.assertEqual(r["severity"], "WARN")

    def test_tester_9038(self):
        h = MockHost(fpga="tester")
        c = self._card(h)
        self.assertEqual(c["state"], "OTHER_XILINX")
        self.assertTrue(any("not an Innova-2 XRT shell" in t for s, t in c["flags"]))   # oper=User -> inconsistent

    def test_unknown_obs_id(self):
        h = MockHost(obs_id=0x0B5E0099)
        c = self._card(h)
        sh = H.Shell(h, c).identify()
        self.assertFalse(sh.ok)
        self.assertEqual(sh.state, "OUR_SHELL_UNREADABLE")
        self.assertEqual(h.model.reads, [0x36030])          # nothing but OBS_ID was read
        self.assertIn("OBS_ID", sh.reason)
        with self.assertRaises(H.Refused):
            H.counters(sh)

    def test_xclmgmt_unbound(self):
        h = MockHost(xclmgmt=False)
        c = self._card(h)
        sh = H.Shell(h, c).identify()
        self.assertEqual(sh.state, "OUR_SHELL_UNREADABLE")
        self.assertEqual(h.model.reads, [])                 # no MMIO at all
        self.assertTrue(sh.reason.startswith("xclmgmt bound"))

    def test_bar_unassigned(self):
        h = MockHost(extra_attrs={("0000:08:00.0", "bar0_unassigned"): True})
        sh = H.Shell(h, self._card(h)).identify()
        self.assertTrue(sh.reason.startswith("mgmt BAR0"))
        self.assertEqual(h.model.reads, [])

    def test_cx5_failed_load(self):
        h = MockHost(cx5=dict(admin=0, oper=0, status=1, transport="kmod"))
        c = self._card(h)
        self.assertTrue(any(s == "FAIL" and "failed image load" in t for s, t in c["flags"]))

    def test_list_cli(self):
        rc, o, e = run_cli(MockHost(), ["list", "--json"])
        self.assertEqual(rc, 0, e)
        rows = json.loads(o)
        self.assertEqual(rows[0]["state"], "OUR_SHELL")
        self.assertEqual(rows[0]["serial"], "MT1935X08327")
        self.assertEqual(rows[0]["shell"]["obs_id"], "0x0B5E0006")


class StatusTests(unittest.TestCase):
    def test_every_layout_status_and_counters(self):
        for oid in RTL_DECODE:
            h = MockHost(oid)
            rc, o, e = run_cli(h, ["status", "-v"])
            self.assertEqual(rc, 0, "%x: %s" % (oid, e))
            self.assertIn("ECC", o)
            rc, o, e = run_cli(h, ["status", "--json"])
            st = json.loads(o)
            self.assertEqual("throttle" in st, oid != 0x0B5E0002)
            rc, o, e = run_cli(h, ["counters", "--json"])
            self.assertEqual(rc, 0, e)
            names = {x["name"] for x in json.loads(o)}
            self.assertIn("ecc_ce", names)
            self.assertIn("lock_losses_icap", names)
            self.assertEqual("temp_samples" in names, oid in (0x0B5E0006, 0x0B5E0007, RV, RV2, RV3, RC1))
            self.assertEqual("rv_ddr_kept" in names, oid in (RV, RV2, RV3))
            self.assertEqual("fenced_outside_link_down_window" in names, oid in (RV2, RV3, RC1))
            self.assertEqual("axi_p2p_write_slverr" in names, oid == 0x0B5E0004)
            self.assertEqual("pr_count" in names, oid != 0x0B5E0002)
            # every register of the layout is dumped
            offs = {int(x["offset"], 16) - 0x36000 for x in json.loads(o) if x["region"] == "ddr_health"}
            self.assertTrue(set(R.LAYOUTS[oid]["regs"]) <= offs)

    def test_status_temperature_states(self):
        h = MockHost(0x0B5E0006)
        rc, o, e = run_cli(h, ["status", "--json"])
        t = json.loads(o)["temperature"]
        self.assertEqual(t["throttle"]["state"], "LIVE")
        self.assertEqual(t["sysmon"]["state"], "LIVE")
        h = MockHost(0x0B5E0005)
        rc, o, e = run_cli(h, ["status", "--json"])
        self.assertEqual(json.loads(o)["temperature"]["throttle"]["state"], "UNRELIABLE")

    def test_recorder(self):
        rc, o, e = run_cli(MockHost(0x0B5E0006), ["recorder", "--json"])
        self.assertEqual(rc, 0, e)
        self.assertEqual(json.loads(o)["records"][0]["type"], "START")
        rc, o, e = run_cli(MockHost(0x0B5E0002), ["recorder", "-v"])
        self.assertEqual(rc, 1)
        self.assertIn("no throttle", e)

    def test_guard_blocks_writes(self):
        h = MockHost()
        c = H.discover(h)[0]
        H.classify(h, c)
        sh = H.Shell(h, c).identify()
        with self.assertRaises(H.UnsafeAccess):
            sh.io.wr(0x36014, 0)                       # ECC_CE is read-only
        with self.assertRaises(H.UnsafeAccess):
            sh.io.rd(0x36200)                          # not in the map
        self.assertEqual(h.model.writes, [])

    def test_counters_watch_deltas(self):
        h = MockHost()
        c = H.discover(h)[0]
        H.classify(h, c)
        sh = H.Shell(h, c).identify()
        a = H.counters(sh)
        h.sleep(2.0)
        b = H.counters(sh)
        d = H.counter_deltas(a, b)
        self.assertIn(("ddr_health", "0x3602c", "ecc_polls"), d)
        self.assertIn(("ddr_health", "0x36140", "temp_samples"), d)

    def test_controls_need_yes(self):
        h = MockHost()
        rc, o, e = run_cli(h, ["clear"])
        self.assertEqual(rc, 1)
        self.assertEqual(h.model.writes, [])
        rc, o, e = run_cli(h, ["clear", "--yes"])
        self.assertEqual(rc, 0, e)
        self.assertEqual(h.model.writes, [(0x36030, R.OBS_CLEAR)])
        rc, o, e = run_cli(h, ["ddr-mode", "off", "--yes"])
        self.assertEqual(rc, 0, e)
        self.assertEqual(h.model.mode, 1)
        rc, o, e = run_cli(h, ["throttle", "force", "4", "--yes"])
        self.assertEqual(h.model.r[0xC0], 0x37)
        self.assertIn("BYPASSES", o.upper())
        rc, o, e = run_cli(h, ["throttle", "off", "--yes"])
        self.assertEqual(h.model.r[0xC0], 0x35)


class CheckTests(unittest.TestCase):
    def check(self, h, **kw):
        c = H.discover(h)[0]
        return C.run_check(h, c, interval=kw.get("interval", 1.0))

    def test_clean_card_ok(self):
        h = MockHost()
        r = self.check(h)
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
        self.assertIn("OK: no concerns", C.render(r))
        rc, o, e = run_cli(MockHost(), ["check"])
        self.assertEqual(rc, 0)
        self.assertIn("OK: no concerns", o)

    def test_check_is_passive(self):
        h = MockHost()
        self.check(h)
        self.assertFalse([c for c in h.runs if "examine" in c], "check must not run xrt-smi examine")
        self.assertEqual(h.model.writes, [])
        self.assertLess(len(h.model.reads), 300)
        self.assertFalse(any(0x3618C <= x <= 0x36198 for x in h.model.reads))

    def test_icap_benign_stays_info(self):
        h = MockHost()
        h.model.r[0x38] = 4 << 16
        r = self.check(h)
        self.assertEqual(r["severity"], "OK")
        self.assertIn("clk.icap_benign", [i["rule"] for i in r["items"] if i["severity"] == "INFO"])
        h = MockHost()
        h.model.r[0x38] = 6 << 16
        self.assertIn("clk.icap", [i["rule"] for i in self.check(h)["items"] if i["severity"] == "WARN"])


# Each concern: (description, setup(host), expected rule, expected severity). Used by the test below and by the
# mutant test, which proves the suite fails when the check reports nothing.
def _set(attr, val):
    def f(h):
        h.attrs_[attr] = val
    return f


def heat(h, c):
    k = R.c2code(c)
    h.model.code = k
    h.model.sysmon.update({0x00: k << 6, 0x20: (k + 2) << 6})
    h.attrs_[("0000:08:00.0", "hwmon/hwmon3/temp4_input")] = str(int(c) * 1000)


def wfile(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def _reg(o, v):
    def f(h):
        h.model.r[o] = v
    return f


CONCERNS = [
    ("UE", _reg(0x18, 0xE0000003), "ddr.ue", "FAIL"),
    ("CE", _reg(0x14, 7), "ddr.ce", "WARN"),
    ("fenced", _reg(0xA0, 2), "ddr.fenced", "WARN"),
    ("not calibrated", _reg(0x00, 0x000000F9), "ddr.not_calibrated", "FAIL"),
    ("mode off", lambda h: (h.model.write(0x32008, 1)), "ddr.mode", "WARN"),
    ("DDR rate not the build's", lambda h: setattr(h.model, "ui_mhz", 333.33), "ddr.rate_mismatch", "WARN"),
    ("poller dead", _reg(0x18, 0x40000000), "ddr.poller", "FAIL"),
    ("poller stuck", lambda h: h.model.mut.add("polls_stuck"), "ddr.poller", "FAIL"),
    ("hot", lambda h: heat(h, 85), "temp.hot_warn", "WARN"),
    ("very hot", lambda h: heat(h, 97), "temp.hot_fail", "FAIL"),
    ("temp feed stuck", lambda h: h.model.mut.add("temp_stuck"), "temp.feed_stale", "FAIL"),
    ("ADC never", lambda h: h.model.sysmon.update({0x20: 0, 0x24: 0xFFFF}), "temp.adc_never", "FAIL"),
    ("XRT temp disagrees", _set(("0000:08:00.0", "hwmon/hwmon3/temp4_input"), "60000"), "temp.disagree", "WARN"),
    ("force", _reg(0xC0, 0x33), "thr.force", "WARN"),
    ("auto throttled", _reg(0xC4, (R.c2code(20) << 16) | R.c2code(30)), "thr.auto", "WARN"),
    ("thr disabled", _reg(0xC0, 0x30), "thr.disabled", "WARN"),
    ("thr not default", _reg(0xDC, 0x00010101), "thr.not_default", "WARN"),
    ("unlocked", _reg(0x34, 0x001F001B), "clk.unlocked", "FAIL"),
    ("icap", _reg(0x38, 9 << 16), "clk.icap", "WARN"),
    ("spi beyond link-downs", _reg(0x38, 5 << 24), "clk.xdma", "WARN"),
    ("freq", _set(("0000:08:00.0", "clock_freq_counter.m.1/clock_counter_freqs"), "250\n500"), "clk.freq", "WARN"),
    ("slot x1", _set(("0000:06:00.0", "current_link_width"), "1"), "pcie.slot_link", "WARN"),
    ("fpga link", _set(("0000:08:00.0", "current_link_width"), "4"), "pcie.fpga_link", "WARN"),
    ("pcie fatal", _reg(0x108, 1), "pcie.fatal", "FAIL"),
    ("pcie nonfatal", _reg(0x104, 2), "pcie.nonfatal", "WARN"),
    ("aer", _set(("0000:08:00.1", "aer_dev_nonfatal"), "TOTAL_ERR_NONFATAL 3"), "pcie.aer", "WARN"),
    ("firewall", _set(("0000:08:00.1", "firewall.u.1/status"), "0x1"), "xrt.firewall", "FAIL"),
    ("mailbox", _set(("0000:08:00.1", "mailbox_connect_state"), "0x0"), "xrt.mailbox", "WARN"),
    ("not ready", _set(("0000:08:00.1", "ready"), "0x0"), "xrt.not_ready", "FAIL"),
    ("slot link AER", _set(("0000:00:02.0", "aer_dev_correctable"), "Timeout 7000\nTOTAL_ERR_COR 7000"),
     "pcie.slot_aer", "WARN"),
    ("slot link AER fatal", _set(("0000:06:00.0", "aer_dev_fatal"), "TOTAL_ERR_FATAL 1"), "pcie.slot_aer_fatal", "FAIL"),
    ("no xsabin", lambda h: [h.files.pop(k) for k in list(h.files) if k.endswith(".xsabin")], "platform.no_xsabin", "FAIL"),
    ("test dir foreign", lambda h: h.files.update({"/opt/xilinx/dsa/%s/test/verify.xclbin" % R.VBNV_FP8C:
                                                   h.xclbin_hdr[:320] + (1792022400).to_bytes(8, "little") +
                                                   h.xclbin_hdr[328:]}), "platform.test_dir", "WARN"),
    ("board", lambda h: h.attrs_.update({("0000:09:00.0", "vpd"): vpd(ec="A5")}), "identity.board", "WARN"),
    ("event since boot", lambda h: [h.model.r.__setitem__(0xF8, int(150 / R.TS_S))], "event.since_boot", "WARN"),
    ("simulate-hot leftover", lambda h: wfile(os.path.join(H.state_dir(), "throttle_saved_0000_08_00.0.json"), "{}"),
     "thr.state_file", "WARN"),
]
def _warm_stale(h):
    """A warm reboot (link-down at FPGA uptime 50 s), then reads of pre-reboot data: 2 UE polls + 87 CE at 80 s."""
    m = h.model
    m.r[0x10] = 0x00050001                                  # 1 link-down, recalibrated, link up
    m.r[0xE0] = int(50 / R.TS_S)
    m.r[0x18] = 0xE0000002
    m.r[0x14] = 87
    m.r[0xEC] = m.r[0xE8] = int(80 / R.TS_S)


CONCERNS.append(("UE after a warm reboot", _warm_stale, "ddr.ue_stale", "WARN"))
AXI_CONCERN = ("axi monitor", _reg(0x40, 0x00010000), "axi.monitor", "WARN")


def concern_failures(evaluate=None):
    """Run every concern; return the descriptions of those the check did NOT report as expected."""
    saved = C.evaluate
    if evaluate is not None:
        C.evaluate = evaluate
    bad = []
    try:
        for desc, setup, rule, sev in CONCERNS + [AXI_CONCERN]:
            h = MockHost(0x0B5E0004 if rule == "axi.monitor" else 0x0B5E0006)
            setup(h)
            c = H.discover(h)[0]
            r = C.run_check(h, c, interval=1.0)
            hit = [i for i in r["items"] if i["rule"] == rule and i["severity"] == sev]
            text = C.render(r)
            if not hit or rule not in text or r["severity"] == "OK":
                bad.append(desc)
            for f in os.listdir(H.state_dir()):
                os.remove(os.path.join(H.state_dir(), f))
    finally:
        C.evaluate = saved
    return bad


class ConcernTests(unittest.TestCase):
    def test_each_concern_triggers(self):
        self.assertEqual(concern_failures(), [])

    def test_mutant_silent_check_is_caught(self):
        bad = concern_failures(evaluate=lambda *a, **k: [])
        self.assertGreater(len(bad), 20, "a check that reports nothing must fail the concern suite")

    def test_mutant_silent_render_is_caught(self):
        saved = C.render
        C.render = lambda r, verbose=False: "OK: no concerns"
        try:
            self.assertGreater(len(concern_failures()), 20)
        finally:
            C.render = saved

    def test_exit_codes(self):
        h = MockHost()
        h.model.r[0x14] = 3
        self.assertEqual(run_cli(h, ["check"])[0], 1)
        h = MockHost()
        h.model.r[0x18] = 0xE0000001
        self.assertEqual(run_cli(h, ["check", "--json"])[0], 2)


class ActiveTestTests(unittest.TestCase):
    def shell(self, h):
        c = H.discover(h)[0]
        H.classify(h, c)
        return H.Shell(h, c).identify()

    def args(self, **kw):
        d = dict(window=3.0, tolerance=0.03, cu="lp_ddr", xclbin=None, arm=False, chunk_mb=256, size_mb=0)
        d.update(kw)
        return types.SimpleNamespace(**d)

    def test_temp_live_pass_and_stuck_counter_fails(self):
        r = T.t_temp_live(self.shell(MockHost()), self.args())
        self.assertEqual(r["status"], "PASS", r)
        r = T.t_temp_live(self.shell(MockHost(mutants=["temp_stuck"])), self.args())
        self.assertEqual(r["status"], "FAIL")

    def test_temp_live_e7a2_frozen_bus(self):
        h = MockHost(0x0B5E0005)
        h.model.sysmon[0] = R.c2code(50) << 6               # SYSMON moved, the throttle's bus did not
        r = T.t_temp_live(self.shell(h), self.args())
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("frozen temperature bus", r["summary"])

    def test_ecc_live(self):
        r = T.t_ecc_live(self.shell(MockHost()), self.args())
        self.assertEqual(r["status"], "INCONCLUSIVE")
        r = T.t_ecc_live(self.shell(MockHost(mutants=["polls_stuck"])), self.args())
        self.assertEqual(r["status"], "FAIL")

    def test_throttle_walk(self):
        for oid in (0x0B5E0006, 0x0B5E0005):
            h = MockHost(oid)
            sh = self.shell(h)
            before = {o: h.model.r[o] for o in (0xC0, 0xC4)}
            r = T.t_throttle(sh, self.args(), lambda p: True, measure=lambda s: h.model.delivered_mhz())
            self.assertEqual(r["status"], "PASS", "\n".join(r["evidence"]))
            self.assertEqual({o: h.model.r[o] for o in (0xC0, 0xC4)}, before)
        h = MockHost(0x0B5E0006, mutants=["tier2_level_ignored"])
        r = T.t_throttle(self.shell(h), self.args(), lambda p: True, measure=lambda s: h.model.delivered_mhz())
        self.assertEqual(r["status"], "FAIL")

    def test_xclbin_rom_ts_guard(self):
        h = MockHost()
        sh = self.shell(h)
        p = "/opt/xilinx/dsa/%s/test/verify.xclbin" % R.VBNV_FP8C
        T.xclbin_check(sh, p)
        h.files[p] = h.xclbin_hdr[:320] + (1792022400).to_bytes(8, "little") + h.xclbin_hdr[328:]
        with self.assertRaises(H.Refused):
            T.xclbin_check(sh, p)

    def test_test_dir_switch(self):
        h = MockHost()
        sh = self.shell(h)
        d = "/opt/xilinx/dsa/%s/test" % R.VBNV_FP8C
        good = dict((k.replace("/test/", "/test.mine/"), v) for k, v in h.files.items() if k.startswith(d + "/"))
        foreign = h.xclbin_hdr[:320] + (1792022400).to_bytes(8, "little") + h.xclbin_hdr[328:]
        for k in [k for k in h.files if k.startswith(d + "/")]:
            del h.files[k]
        h.files.update(good)
        h.files[d + ".e6b/verify.xclbin"] = foreign
        h.links[d] = "test.e6b"
        old = T.select_test_dir(sh, lambda p: True)
        self.assertEqual((old, h.links[d]), ("test.e6b", "test.mine"))
        T.restore_test_dir(sh, old)
        self.assertEqual(h.links[d], "test.e6b")

    def test_flash_verify(self):
        h = MockHost()
        sh = self.shell(h)
        img0, img1 = bytes(range(256)) * 40, bytes(reversed(range(256))) * 40
        os.makedirs(STATE, exist_ok=True)
        p0, p1 = os.path.join(STATE, "c0.bin"), os.path.join(STATE, "c1.bin")
        wfile(p0, img0)
        wfile(p1, img1)

        class Reader:
            def __init__(s, flash, stalls=0, short=0):
                s.flash, s.stalls, s.short = flash, stalls, short

            def rdid(s):
                return ["ff20bb2010", "0020bb2010"]

            def read(s, chip, addr, n):
                data = s.flash[chip][addr - 0x01000000:addr - 0x01000000 + n]
                return data[:len(data) - s.short], s.stalls
        a = types.SimpleNamespace(mcs_prefix=None, primary=p0, secondary=p1, offset=0x01000000)
        self.assertEqual(T.t_flash_verify(sh, a, reader=Reader({0: img0, 1: img1}))["status"], "PASS")
        self.assertEqual(T.t_flash_verify(sh, a, reader=Reader({0: img0, 1: img1}, short=3))["status"], "FAIL")
        self.assertEqual(T.t_flash_verify(sh, a, reader=Reader({0: img0, 1: img1}, stalls=1))["status"], "FAIL")
        bad = bytearray(img1)
        bad[100] ^= 1
        r = T.t_flash_verify(sh, a, reader=Reader({0: img0, 1: bytes(bad)}))
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("first at +0x64", "\n".join(r["evidence"]))

    def test_mcs_shift(self):
        os.makedirs(STATE, exist_ok=True)
        p = os.path.join(STATE, "x_primary.mcs")
        wfile(p, ":0200000400FFFB\n:04F00000DEADBEEFC4\n:00000001FF\n")
        addr, b = T.mcs_image(p)
        self.assertEqual((addr, b), (0x00FFF000 + 0x1000, bytes.fromhex("deadbeef")))
        wfile(p, ":020000040000FA\n:020000040100F9\n:04000000DEADBEEFC4\n:00000001FF\n")
        addr, b = T.mcs_image(p)
        self.assertEqual(addr, 0x01000000)


class SimulateHotTests(unittest.TestCase):
    def test_tier2_and_restore(self):
        h = MockHost(0x0B5E0006)
        before = {o: h.model.r[o] for o in (0xC0, 0xC4, 0xD8, 0xDC)}
        orig = SMI.time
        SMI.time = types.SimpleNamespace(time=h.now, sleep=h.sleep, strftime=orig.strftime)
        try:
            rc, o, e = run_cli(h, ["throttle", "simulate-hot", "tier2", "--seconds", "2", "--yes"])
        finally:
            SMI.time = orig
        self.assertEqual(rc, 0, o + e)
        self.assertIn("in effect tier 2 at 1/4", o)
        self.assertIn("released", o)
        self.assertIn("THR_TIER", o)
        self.assertEqual({o_: h.model.r[o_] for o_ in before}, before)
        self.assertEqual(os.listdir(H.state_dir()), [])

    def test_e7a2_single_tier_and_refusals(self):
        h = MockHost(0x0B5E0005)
        rc, o, e = run_cli(h, ["throttle", "simulate-hot", "tier2", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("single automatic tier", e)
        h.model.r[0xC0] = 0x33
        rc, o, e = run_cli(h, ["throttle", "simulate-hot", "tier1", "--yes"])
        self.assertIn("FORCE is on", e)
        rc, o, e = run_cli(MockHost(0x0B5E0002), ["throttle", "simulate-hot", "tier1", "--yes"])
        self.assertIn("no throttle", e)

    def test_restore_after_crash(self):
        h = MockHost(0x0B5E0006)
        c = H.discover(h)[0]
        H.classify(h, c)
        sh = H.Shell(h, c).identify()
        saved = H.throttle_regs(sh)
        code, plan = H.sim_hot_plan(sh, "tier1")
        H.save_thr_state(sh, saved)
        H.throttle_write(sh, plan)                      # ... and the process dies here
        with self.assertRaises(H.Refused):
            H.save_thr_state(sh, saved)                 # a second simulate-hot refuses
        rc, o, e = run_cli(h, ["throttle", "restore"])
        self.assertEqual(rc, 0, e)
        self.assertEqual(H.throttle_regs(sh), saved)
        self.assertEqual(os.listdir(H.state_dir()), [])


class CliTests(unittest.TestCase):
    def test_loading_tests_need_yes(self):
        import innova2_smi_xrt as X
        saved = X.ensure_xrt_env
        X.ensure_xrt_env = lambda: None
        try:
            for argv in (["test", "ecc-live", "--arm"], ["test", "validate"], ["test", "ddr-soak"],
                         ["test", "pr-stress"], ["test", "throttle", "--xclbin", "x.xclbin"]):
                h = MockHost()
                rc, o, e = run_cli(h, argv)
                self.assertEqual(rc, 1, argv)
                self.assertIn("refusing without --yes", e, argv)
                self.assertEqual((h.model.writes, [c for c in h.runs if c[0] != "devlink"]),
                                 ([], []), argv)
            rc, o, e = run_cli(MockHost(), ["test", "ecc-live"])      # poller check only: read-only, no prompt
            self.assertEqual(rc, 2, e)
        finally:
            X.ensure_xrt_env = saved


    def test_help_for_every_command(self):
        p = SMI.build_parser()
        for cmd in ("list", "status", "check", "counters", "recorder", "clear", "ddr-mode", "throttle", "test", "regmap"):
            o = io.StringIO()
            with redirect_stdout(o), self.assertRaises(SystemExit):
                p.parse_args([cmd, "-h"])
            self.assertIn("usage", o.getvalue())
        o = io.StringIO()
        with redirect_stdout(o), self.assertRaises(SystemExit):
            p.parse_args(["throttle", "-h"])
        self.assertIn("SIMULATES heat by moving", o.getvalue())
        self.assertIn("no temperature-override register", o.getvalue())

    def test_regmap_markdown_matches_doc(self):
        doc = os.path.join(ROOT, "docs", "innova2_smi_regmap.md")
        if os.path.exists(doc):
            self.assertEqual(open(doc).read().rstrip("\n"), R.markdown().rstrip("\n"),
                             "docs/innova2_smi_regmap.md is stale: regenerate with 'innova2-smi regmap --markdown'")


# ----------------------------------------------------------------------------------------------- 1.3.4
def _fpga_ts(h):
    """The model's FPGA uptime now, in seconds (it starts at 120 s)."""
    return h.model.now() + 120.0


def _new_ue(h, n=1):
    h.model.r[0x18] = (h.model.r[0x18] & ~0xFFFF) | 1 << 31 | ((h.model.r[0x18] & 0xFFFF) + n)
    h.model.r[0xEC] = int(_fpga_ts(h) / R.TS_S)


def _warm_again(h):
    h.model.r[0x10] += 1
    h.model.r[0xE0] = int(_fpga_ts(h) / R.TS_S)


def _baseline(h):
    rc, o, e = run_cli(h, ["ecc-baseline"])
    assert rc == 0, e
    h.sleep(5.0)


def _setup(*fns):
    def f(h):
        for fn in fns:
            fn(h)
    return f


# (description, OBS_ID, setup, expected rule, its severity, the card's overall severity)
ECC_SCENARIOS = [
    ("cold, UE (no link-down)", 0x0B5E0007, _setup(lambda h: _new_ue(h, 2)), "ddr.ue", "FAIL", "FAIL"),
    ("warm reboot, stale UE after it", 0x0B5E0007, _warm_stale, "ddr.ue_stale", "WARN", "WARN"),
    ("UE before the only link-down", 0x0B5E0007,
     _setup(lambda h: _new_ue(h, 2), lambda h: h.sleep(3), _warm_again), "ddr.ue", "FAIL", "FAIL"),
    ("UE before the last of several link-downs", 0x0B5E0007,
     _setup(_warm_again, lambda h: h.sleep(1), lambda h: _new_ue(h), lambda h: h.sleep(3), _warm_again),
     "ddr.ue_unclassified", "WARN", "WARN"),
    ("E6b layout (no timestamps), UE with a link-down", 0x0B5E0002,
     _setup(_reg(0x10, 1), _reg(0x18, 0xE0000002)), "ddr.ue_unclassified", "WARN", "WARN"),
    ("stale UE acknowledged by a baseline", 0x0B5E0007, _setup(_warm_stale, _baseline), "ddr.ue_baselined", "INFO",
     "OK"),
    ("UE after the baseline", 0x0B5E0007, _setup(_warm_stale, _baseline, lambda h: _new_ue(h)), "ddr.ue", "FAIL",
     "FAIL"),
    ("CE after the baseline", 0x0B5E0007, _setup(_warm_stale, _baseline, _reg(0x14, 90)), "ddr.ce", "WARN", "WARN"),
    ("warm reboot after the baseline, then a UE", 0x0B5E0007,
     _setup(_warm_stale, _baseline, _warm_again, lambda h: h.sleep(1), lambda h: _new_ue(h)), "ddr.ue_stale", "WARN",
     "WARN"),
    ("baseline from an earlier configuration", 0x0B5E0007,
     _setup(lambda h: _new_ue(h), _baseline, lambda h: _rewrite_baseline(uptime_cycles=1 << 46)), "ddr.ue", "FAIL",
     "FAIL"),
]


def _rewrite_baseline(**kw):
    for f in os.listdir(H.state_dir()):
        if f.startswith("ecc_baseline_"):
            p = os.path.join(H.state_dir(), f)
            with open(p) as fh:
                d = json.load(fh)
            d.update(kw)
            with open(p, "w") as fh:
                json.dump(d, fh)


def ecc_failures(evaluate=None, ecc=None):
    """Run every ECC scenario; return the descriptions whose result was not the expected one."""
    saved = C.evaluate, C.ecc_items
    if evaluate is not None:
        C.evaluate = evaluate
    if ecc is not None:
        C.ecc_items = ecc
    bad = []
    try:
        for desc, oid, setup, rule, sev, overall in ECC_SCENARIOS:
            h = MockHost(oid)
            setup(h)
            r = C.run_check(h, H.discover(h)[0], interval=1.0)
            if not [i for i in r["items"] if i["rule"] == rule and i["severity"] == sev] or r["severity"] != overall:
                bad.append(desc)
            for f in os.listdir(H.state_dir()):
                os.remove(os.path.join(H.state_dir(), f))
    finally:
        C.evaluate, C.ecc_items = saved
    return bad


class EccStaleTests(unittest.TestCase):
    def tearDown(self):
        for f in os.listdir(H.state_dir()):
            os.remove(os.path.join(H.state_dir(), f))

    def test_scenarios(self):
        self.assertEqual(ecc_failures(), [])

    def test_warm_stale_is_warn_with_hint(self):
        h = MockHost(0x0B5E0007)
        _warm_stale(h)
        r = C.run_check(h, H.discover(h)[0])
        self.assertEqual(r["severity"], "WARN")
        it = [i for i in r["items"] if i["rule"] == "ddr.ue_stale"][0]
        self.assertIn("likely stale-data reads after warm reboot", it["message"])
        for w in ("link-down MIG reset", "expected after a warm reboot", "re-fill", "ecc-baseline", "cold-boot"):
            self.assertIn(w, it["hint"])
        self.assertIn("likely stale-data reads after warm reboot", C.render(r))
        self.assertEqual(run_cli(h, ["check"])[0], 1)

    def test_ue_after_baseline_fails_and_totals_are_info(self):
        h = MockHost(0x0B5E0007)
        _warm_stale(h)
        _baseline(h)
        rc, o, e = run_cli(h, ["check", "-v"])
        self.assertEqual(rc, 0, o)
        self.assertIn("ddr.ue_baselined", o)
        self.assertIn("ddr.ce_baselined", o)
        _new_ue(h)
        rc, o, e = run_cli(h, ["check"])
        self.assertEqual(rc, 2, o)
        self.assertIn("1 ECC uncorrectable polls since the ecc-baseline", o)

    def test_baseline_cli_file_and_passive(self):
        h = MockHost(0x0B5E0007)
        _warm_stale(h)
        rc, o, e = run_cli(h, ["ecc-baseline", "--json"])
        self.assertEqual(rc, 0, e)
        d = json.loads(o)
        self.assertEqual((d["baseline"]["ue_polls"], d["baseline"]["ce"], d["baseline"]["link_down"]), (2, 87, 1))
        self.assertTrue(d["file"].startswith(os.path.join(STATE, ".cache", "innova2-smi")))
        self.assertFalse(d["file"].startswith("/tmp/"))
        self.assertEqual(h.model.writes, [])                 # host-side only
        rc, o, e = run_cli(h, ["ecc-baseline", "--show"])
        self.assertIn("UE polls 2", o)
        rc, o, e = run_cli(h, ["ecc-baseline", "--remove"])
        self.assertIn("removed", o)
        self.assertEqual([f for f in os.listdir(H.state_dir()) if f.startswith("ecc_")], [])

    def test_corrupt_baseline_ignored(self):
        h = MockHost(0x0B5E0007)
        _new_ue(h)
        wfile(os.path.join(H.state_dir(), "ecc_baseline_0000_08_00.0.json"), "{not json")
        r = C.run_check(h, H.discover(h)[0])
        self.assertIn("ddr.baseline_ignored", [i["rule"] for i in r["items"]])
        self.assertEqual(r["severity"], "FAIL")

    def test_mutant_suppress_every_ue_is_caught(self):
        def silent(*a, **k):
            return [i for i in C_EVALUATE(*a, **k) if not i["rule"].startswith("ddr.ue")]
        self.assertGreaterEqual(len(ecc_failures(evaluate=silent)), 5)

    def test_mutant_every_ue_stale_is_caught(self):
        def stale(*a, **k):
            out = C_ECC(*a, **k)
            return [dict(i, rule="ddr.ue_stale", severity="WARN") if i["rule"].startswith("ddr.ue") else i for i in out]
        bad = ecc_failures(ecc=stale)
        self.assertIn("cold, UE (no link-down)", bad)
        self.assertIn("UE after the baseline", bad)

    def test_mutant_baseline_ignored_is_caught(self):
        self.assertIn("stale UE acknowledged by a baseline", ecc_failures(
            evaluate=lambda sh, s0, s1=None, **k: C_EVALUATE(sh, s0, s1, **dict(k, baseline=None))))


C_EVALUATE, C_ECC = C.evaluate, C.ecc_items


class UuidTests(unittest.TestCase):
    U = "869cfe845d0b1f79869844ef67a34cd5"

    def test_table_filled_and_hex(self):
        self.assertEqual(R.SHELLS[1792461600]["uuid"], self.U)
        for ts, d in R.SHELLS.items():
            if d["name"] != "routeV":
                self.assertRegex(d["uuid"] or "", r"^[0-9a-f]{32}$", d["name"])

    def test_table_matches_build_xsabins(self):
        dirs = {"E6b": "e6b", "E6b-2400": "e6b2400", "E6b-2400dw8": "e6b2400dw8", "E7a2": "e7a2",
                "E7a3-2400": "e7a3_2400", "E7a3-2666ca48": "e7a3_2666ca48"}
        for ts, d in R.SHELLS.items():
            f = "/nobackup2/xilinx_shells_work/clkx300fp8c_%s_build/release/boot.xsabin" % dirs.get(d["name"], "-")
            if not os.path.exists(f):
                continue
            with open(f, "rb") as fh:
                b = fh.read(512)
            self.assertEqual(int.from_bytes(b[320:328], "little"), ts, f)
            self.assertEqual(b[336:352].hex(), d["uuid"], f)

    def _st(self, h):
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertEqual(rc, 0, e)
        return o

    def test_live_uuid_matches(self):
        h = MockHost(0x0B5E0007, extra_attrs={("0000:08:00.1", "interface_uuids"): self.U + "\n"})
        self.assertIn("interface uuid %s (xocl sysfs; table agrees)" % self.U, self._st(h))
        r = C.run_check(h, H.discover(h)[0])
        self.assertEqual(r["identity"]["interface_uuid_check"], "match")
        self.assertEqual(r["severity"], "OK")

    def test_live_uuid_mismatch_warns(self):
        bad = "0" * 31 + "1"
        h = MockHost(0x0B5E0007, extra_attrs={("0000:08:00.1", "interface_uuids"): bad})
        o = self._st(h)
        self.assertIn("WARN: table has %s" % self.U, o)
        r = C.run_check(h, H.discover(h)[0])
        self.assertIn(("identity.uuid_mismatch", "WARN"), [(i["rule"], i["severity"]) for i in r["items"]])

    def test_boot_xsabin_fallback(self):
        h = MockHost(0x0B5E0007, extra_attrs={("0000:08:00.1", "interface_uuids"): None})
        x = "/lib/firmware/xilinx/10ee-788f-4351-%016x.xsabin" % 1792461600
        h.files[x] = h.xclbin_hdr[:336] + bytes.fromhex(self.U) + h.xclbin_hdr[352:]
        self.assertIn("interface uuid %s (boot xsabin header; table agrees)" % self.U, self._st(h))
        h.files[x] = h.xclbin_hdr[:336] + bytes(15) + b"\x01" + h.xclbin_hdr[352:]
        r = C.run_check(h, H.discover(h)[0])
        self.assertIn(("identity.uuid_mismatch", "WARN"), [(i["rule"], i["severity"]) for i in r["items"]])

    def test_mgmt_fallback_and_table_only(self):
        h = MockHost(0x0B5E0007, extra_attrs={("0000:08:00.0", "interface_uuids"): self.U})
        self.assertIn("(xclmgmt sysfs; table agrees)", self._st(h))
        self.assertIn("%s (from this tool's table" % self.U, self._st(MockHost(0x0B5E0007)))


class ThrottleWordingTests(unittest.TestCase):
    def st(self, h):
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertEqual(rc, 0, e)
        return o

    def test_not_throttled_is_unambiguous(self):
        o = self.st(MockHost(0x0B5E0007))
        self.assertIn("in effect: FULL (not throttled)", o)
        self.assertIn("FORCE        : off (force level if enabled: 1/2)", o)
        self.assertIn("auto tiers   : T1=1/2", o)
        self.assertIn("T2=1/4", o)
        self.assertNotIn("level now", o)

    def test_force_and_auto(self):
        h = MockHost(0x0B5E0007)
        h.model.r[0xC0] = 0x37
        self.assertIn("in effect: FORCE 1/4 (throttled", self.st(h))
        h = MockHost(0x0B5E0007)
        h.model.r[0xC4] = (R.c2code(20) << 16) | R.c2code(30)
        h.sleep(0.5)
        self.assertIn("in effect: T1 (throttled to 1/2)", self.st(h))

    def test_e7a2_one_tier(self):
        o = self.st(MockHost(0x0B5E0005))
        self.assertIn("in effect: FULL (not throttled)", o)
        self.assertIn("auto         : one tier at the FORCE level (1/2)", o)


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------------------------------- route V
class RouteVTests(unittest.TestCase):
    """OBS_ID 0x0B5E0008 (innova2_innova2_noddr_202320_3): DDR exists only in KEPT xclbins."""

    def check(self, h, interval=1.0):
        c = H.discover(h)[0]
        return C.run_check(h, c, interval=interval)

    def rules(self, r, sev=None):
        return [i["rule"] for i in r["items"] if sev is None or i["severity"] == sev]

    def test_map_against_the_rtl(self):
        if not os.path.exists(ROUTEV_RTL):
            self.skipTest("route-V shell source not on this machine")
        oid, probs = R.check_rtl(open(ROUTEV_RTL).read())
        self.assertEqual((oid, probs), (RV, []))

    def test_identify_and_list_kept(self):
        h = MockHost(RV)
        rc, o, e = run_cli(h, ["list"])
        self.assertEqual(rc, 0, e)
        self.assertIn(R.VBNV_ROUTEV, o)
        self.assertIn("xclbin DDR KEPT", o)
        h.model.set_pruned()
        rc, o, e = run_cli(h, ["list", "--json"])
        self.assertEqual(json.loads(o)[0]["xclbin_ddr"], "PRUNED")

    def test_status_kept_and_pruned(self):
        h = MockHost(RV)
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertEqual(rc, 0, e)
        self.assertIn("xclbin KEPT (MIG in the dynamic region), calibrated, window gate open", o)
        self.assertIn("this load", o)
        self.assertIn("DDR4-2667 (tCK 750 ps), CL19 CWL18 tRCD 20 tRP 20, 72-bit ECC, CA RDRV_48_48, MIG e6b750", o)
        rc, o, e = run_cli(h, ["status", "--json"])
        di = json.loads(o)["routev"]["ddr_info"]
        self.assertAlmostEqual(di["ui_clk_mhz_measured"], 333.3, delta=1.0)
        h.model.set_pruned()
        rc, o, e = run_cli(h, ["status", "--json"])
        st = json.loads(o)
        self.assertEqual(st["routev"]["xclbin_ddr"], "PRUNED")
        self.assertTrue(st["routev"]["rp_calib"])
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertIn("PRUNED (no DDR in this xclbin)", o)
        self.assertIn("window fenced (expected: no DDR)", o)
        self.assertIn("no DDR in this xclbin (DDR_INFO magic 0x0D0D)", o)

    def test_check_pruned_is_normal(self):
        h = MockHost(RV)
        h.model.set_pruned()
        r = self.check(h)
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
        self.assertIn("ddr.pruned", self.rules(r, "INFO"))
        self.assertNotIn("ddr.not_calibrated", self.rules(r))
        self.assertNotIn("ddr.poller", self.rules(r))

    def test_check_kept_clean(self):
        r = self.check(MockHost(RV))
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
        self.assertNotIn("ddr.pruned", self.rules(r))

    def test_check_kept_not_calibrated_fails(self):
        h = MockHost(RV)
        h.model.rv["cal"] = False
        r = self.check(h)
        self.assertEqual(r["severity"], "FAIL")
        self.assertIn("ddr.kept_not_calibrated", self.rules(r, "FAIL"))

    def test_check_fences(self):
        h = MockHost(RV)                                     # fences while PRUNED: expected (INFO)
        h.model.set_pruned()
        h.model.r[0xA0] = 5
        r = self.check(h)
        self.assertEqual(r["severity"], "OK")
        self.assertIn("ddr.fenced_pruned", self.rules(r, "INFO"))
        h = MockHost(RV)                                     # a fence while KEPT: FAIL
        h.model.r[0xA0], h.model.rv["fence_kept"] = 6, 1
        r = self.check(h)
        self.assertIn("ddr.fenced_kept", self.rules(r, "FAIL"))
        self.assertEqual(r["severity"], "FAIL")

    def test_check_cal_lost_and_isolated(self):
        h = MockHost(RV)
        h.model.rv["lost"] = 1
        self.assertIn("ddr.cal_lost", self.rules(self.check(h), "WARN"))
        h = MockHost(RV)
        h.model.rv["released"] = False
        self.assertIn("ddr.rp_isolated", self.rules(self.check(h), "WARN"))

    def test_ddr_mode_emu_hybrid_deferred(self):
        h = MockHost(RV)
        rc, o, e = run_cli(h, ["ddr-mode"])
        self.assertEqual(rc, 0, e)
        self.assertIn("emu, hybrid: not available -- deferred on route V", o)
        rc, o, e = run_cli(h, ["ddr-mode", "--json"])
        self.assertIn("emu", json.loads(o)["not_available"])
        for m in ("emu", "hybrid"):
            rc, o, e = run_cli(h, ["ddr-mode", m, "--yes"])
            self.assertEqual(rc, 1)
            self.assertIn("not available on route V", e + o)
            self.assertEqual(h.model.writes, [], "a deferred mode must not write the register")
        rc, o, e = run_cli(h, ["ddr-mode", "off", "--yes"])
        self.assertEqual(rc, 0, e)
        self.assertEqual(h.model.mode, 1)

    def test_requested_deferred_mode_warns(self):
        h = MockHost(RV)
        h.model.mode = 2                                     # written by something else: the gate runs ON
        r = self.check(h)
        self.assertIn("ddr.mode_deferred", self.rules(r, "WARN"))
        self.assertNotIn("ddr.mode", self.rules(r))

    def test_old_layout_rules_unchanged(self):
        h = MockHost(0x0B5E0006)
        h.model.r[0x00] &= ~(1 << 28)                        # E7a3: a closed gate is still a FAIL
        r = self.check(h)
        self.assertIn("ddr.not_calibrated", self.rules(r, "FAIL"))
        self.assertNotIn("ddr.pruned", self.rules(r))

    def test_vbnv_layout_mismatch_warns(self):
        h = MockHost(0x0B5E0006)
        h.attrs_[("0000:08:00.0", "rom.m.1/VBNV")] = R.VBNV_ROUTEV
        r = self.check(h)
        self.assertIn("identity.obs_mismatch", self.rules(r, "WARN"))

    def test_recorder_types(self):
        self.assertEqual(R.REC_TYPES[20], "RM_DDR")
        self.assertIn("PRUNED", R.record_detail(20, 0))
        self.assertIn("ms after the RP release", R.record_detail(21, 100))


class RouteV2Tests(unittest.TestCase):
    """OBS_ID 0x0B5E000B (route V r2): the boot RM is the PRUNED filler; fences split by link-down window (0x218)."""
    NOXCLBIN = "00000000-0000-0000-0000-000000000000"

    def check(self, h):
        return C.run_check(h, H.discover(h)[0], interval=1.0)

    def rules(self, r, sev=None):
        return [i["rule"] for i in r["items"] if sev is None or i["severity"] == sev]

    def boot(self, obs=RV2, pruned=True):
        h = MockHost(obs)
        if pruned:
            h.model.set_pruned()
        h.attrs_[("0000:08:00.1", "xclbinuuid")] = self.NOXCLBIN
        h.model.r[0x120] = 0                                 # no RP isolation since configuration
        return h

    def test_map_against_the_rtl(self):
        if not os.path.exists(ROUTEV2_RTL):
            self.skipTest("route-V r2 shell source not on this machine")
        oid, probs = R.check_rtl(open(ROUTEV2_RTL).read())
        self.assertEqual((oid, probs), (RV2, []))

    def test_layout(self):
        lay = R.layout(RV2)
        self.assertTrue(R.is_routev(lay) and R.has_fencewin(lay))
        self.assertFalse(R.has_fencewin(R.layout(RV)))
        self.assertEqual(R.SHELLS[1792479600]["obs_id"], RV2)
        self.assertEqual("%x" % 1792479600, "6ad71170")

    def test_boot_filler_wording(self):
        h = self.boot()
        rc, o, e = run_cli(h, ["list"])
        self.assertEqual(rc, 0, e)
        self.assertIn("no xclbin (boot RM: filler)", o)
        self.assertNotIn("xclbin DDR", o)
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertIn("DDR (route V)  : no xclbin (boot RM: filler, no DDR)", o)
        self.assertNotIn("xclbin KEPT", o)
        self.assertIn("no DDR in the loaded RM", o)
        st = json.loads(run_cli(h, ["status", "--json"])[1])["routev"]
        self.assertEqual((st["xclbin_loaded"], st["boot_rm"], st["xclbin_ddr"]), (False, "filler", "PRUNED"))
        r = self.check(h)
        self.assertNotIn("ddr.kept_not_calibrated", self.rules(r))
        it = [i for i in r["items"] if i["rule"] == "ddr.pruned"][0]
        self.assertIn("boot RM is the PRUNED filler", it["message"])
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])

    def test_boot_kept_template_r1(self):
        h = self.boot(RV, pruned=False)
        self.assertIn("no xclbin (boot RM: KEPT template)", run_cli(h, ["list"])[1])
        self.assertIn("no xclbin (boot RM: KEPT template, MIG in the dynamic region)", run_cli(h, ["status", "-v"])[1])

    def test_refused_kept_load_is_not_the_boot_rm(self):
        h = self.boot(pruned=False)                          # XRT refused a KEPT load (MIG timeout): uuid zero, PR done
        h.model.rv["cal"] = False
        h.model.r[0x120] = 4
        o = run_cli(h, ["status", "-v"])[1]
        self.assertIn("no xclbin registered by XRT (RP holds the KEPT, MIG in the dynamic region RM of a refused load)", o)
        self.assertNotIn("no xclbin (boot RM", o)
        self.assertIsNone(json.loads(run_cli(h, ["status", "--json"])[1])["routev"]["boot_rm"])

    def test_loaded_xclbin_wording_unchanged(self):
        h = MockHost(RV2)
        self.assertIn("xclbin DDR KEPT", run_cli(h, ["list"])[1])
        h.model.set_pruned()
        self.assertIn("xclbin PRUNED (no DDR in this xclbin)", run_cli(h, ["status", "-v"])[1])

    def test_no_xocl_falls_back_to_pr_count(self):
        h = self.boot()
        del h.attrs_[("0000:08:00.1", "xclbinuuid")]
        st = json.loads(run_cli(h, ["status", "--json"])[1])["routev"]
        self.assertFalse(st["xclbin_loaded"])
        self.assertIn("PR_COUNT 0", st["xclbin_loaded_source"])
        h.model.r[0x120] = 2
        st = json.loads(run_cli(h, ["status", "--json"])[1])["routev"]
        self.assertTrue(st["xclbin_loaded"])

    def test_pr_count_is_isolations(self):
        h = MockHost(RV2)
        h.model.r[0x120] = 46
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertIn("PR isolations  : 46 (~23 xclbin loads", o)
        self.assertEqual(json.loads(run_cli(h, ["status", "--json"])[1])["pr"]["xclbin_loads_estimate"], 23)
        self.assertIn("zero-length isolate", SMI.cmd_status.__doc__)

    def fences(self, h, total, inside, outside, kept=0):
        h.model.r[0xA0], h.model.r[0x218], h.model.r[0x21C] = total, inside, outside
        h.model.r[0xA4], h.model.r[0xA8], h.model.r[0xAC] = 1 << 31, 0, 2
        h.model.r[0x10] = 2                                  # hpy440: two link-downs per warm reboot
        h.model.rv["fence_kept"] = kept

    def test_fence_inside_windows_is_benign_from_registers(self):
        for pruned in (False, True):                        # KEPT: the fence in the recalibration window is benign too
            h = MockHost(RV2)
            if pruned:
                h.model.set_pruned()
            self.fences(h, 1, 1, 0, kept=0 if pruned else 1)
            r = self.check(h)
            self.assertIn("ddr.fenced_linkdown_benign", self.rules(r, "INFO"))
            self.assertNotIn("ddr.fenced_kept", self.rules(r))
            self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
            it = [i for i in r["items"] if i["rule"] == "ddr.fenced_linkdown_benign"][0]
            self.assertIn("registers", it["evidence"]["timing"])
        self.assertEqual(h.model.writes, [], "classification must not store to the recorder index")

    def test_fence_outside_window_is_not_benign(self):
        h = MockHost(RV2)
        self.fences(h, 2, 1, 1, kept=1)                      # one outside a window while KEPT: FAIL as before
        r = self.check(h)
        self.assertNotIn("ddr.fenced_linkdown_benign", self.rules(r))
        self.assertIn("ddr.fenced_kept", self.rules(r, "FAIL"))
        self.assertIn("ddr.fenced_outside_window", self.rules(r))
        h = MockHost(RV2)
        h.model.set_pruned()
        self.fences(h, 3, 0, 3)                              # outside, PRUNED: the r1 INFO
        r = self.check(h)
        self.assertIn("ddr.fenced_pruned", self.rules(r, "INFO"))
        self.assertNotIn("ddr.fenced_linkdown_benign", self.rules(r))
        h = MockHost(RV2)
        self.fences(h, 3, 1, 0, kept=1)                      # counters do not add up: never benign
        self.assertNotIn("ddr.fenced_linkdown_benign", self.rules(self.check(h)))

    def test_mutants_are_caught(self):
        orig = R.has_fencewin
        try:
            R.has_fencewin = lambda lay: False               # mutant: the window counters ignored (r1 rules on r2)
            h = MockHost(RV2)
            self.fences(h, 1, 1, 0, kept=1)
            self.assertIn("ddr.fenced_kept", self.rules(self.check(h), "FAIL"), "mutant not caught")
            R.has_fencewin = lambda lay: bool(lay and lay.get("routev"))   # mutant: r1 read as r2
            h = MockHost(RV)
            h.model.r[0xA0], h.model.rv["fence_kept"] = 1, 1
            r = self.check(h)                                # 0x218 is not in r1's map: never read, never benign
            self.assertNotIn("ddr.fenced_linkdown_benign", self.rules(r))
            self.assertNotIn(0x36218, h.model.reads)
        finally:
            R.has_fencewin = orig

    def test_e7a3_port_layout_uses_counters(self):
        class Sh:
            lay = R.layout(RC1)                              # the E7a3 fence-window port: fp8c rc1
        regs = {0xA0: 1, 0x10: 2, 0xA4: 1 << 31, 0xA8: 0, 0xAC: 2, 0x218: 1, 0x21C: 0}
        self.assertEqual(C.fence_items(Sh, regs)[0]["rule"], "ddr.fenced_linkdown_benign")
        regs.update({0xA0: 2, 0x21C: 1})
        it = C.fence_items(Sh, regs)[0]
        self.assertEqual(it["rule"], "ddr.fenced")
        self.assertIn("outside every link-down window", it["evidence"]["not_benign"])

    def test_r1_fences_unchanged(self):
        h = MockHost(RV)
        h.model.r[0xA0], h.model.rv["fence_kept"] = 1, 1
        r = self.check(h)
        self.assertIn("ddr.fenced_kept", self.rules(r, "FAIL"))
        self.assertNotIn("ddr.fenced_linkdown_benign", self.rules(r))


# ----------------------------------------------------------------------------------------------- memory banks
import struct                                                         # noqa: E402

USER = "0000:08:00.1"
FP8C_BANKS = [  # tag, MEM_TYPE index, size KB, base -- the fp8c / route-V platform's six banks
    ("bank0", 1, 8 << 20, 0x200000000), ("PLRAM2", 2, 256, 0x2040000), ("PLRAM0", 2, 256, 0x2000000),
    ("PLRAM1", 2, 256, 0x2020000), ("HOST", 2, 16 << 20, 0x2000000000), ("PEER", 2, 4096, 0x400000000)]


def mem_topology(used, banks=FP8C_BANKS):
    b = struct.pack("<i4x", len(banks))
    for (tag, t, kb, base), u in zip(banks, used):
        b += struct.pack("<BB6xQQ16s", t, u, kb, base, tag.encode())
    return b


def ip_layout(names):
    b = struct.pack("<i4x", len(names))
    for i, n in enumerate(names):
        b += struct.pack("<IIQ64s", 1, 0x101, 0x1000000 + 0x10000 * i, n.encode())
    return b


def connectivity(conns):
    return struct.pack("<i", len(conns)) + b"".join(struct.pack("<iii", *c) for c in conns)


def load_xclbin(h, used, conns, names=("lat_probe:lp_ddr", "lat_probe:lp_p0")):
    """What xocl shows under the user PF after an xclbin load: uuid + the three binary sections."""
    h.attrs_.update({(USER, "xclbinuuid"): "97613339-17af-2573-b200-1d96740165e0",
                     (USER, "icap.u.1/mem_topology"): mem_topology(used),
                     (USER, "icap.u.1/ip_layout"): ip_layout(list(names)),
                     (USER, "icap.u.1/connectivity"): connectivity(conns)})


class MemoryTests(unittest.TestCase):
    def check(self, h):
        return C.run_check(h, H.discover(h)[0], interval=1.0)

    def rules(self, r, sev=None):
        return [i["rule"] for i in r["items"] if sev is None or i["severity"] == sev]

    def banks(self, h):
        rc, o, e = run_cli(h, ["memory", "--json"])
        self.assertEqual(rc, 0, e)
        return {b["tag"]: b for b in json.loads(o)["banks"]}

    def test_fp8c_all_banks_used(self):
        h = MockHost(0x0B5E0007)
        h.attrs_[(USER, "host_mem_size")] = str(1 << 30)
        # lp_ddr args 0,1 on bank0; lp_p0 args 0..3 on PLRAM0, PLRAM1, PLRAM2, HOST, and PEER on arg 4
        load_xclbin(h, [1] * 6, [(0, 0, 0), (1, 0, 0), (0, 1, 2), (1, 1, 3), (2, 1, 1), (3, 1, 4), (4, 1, 5)])
        b = self.banks(h)
        self.assertEqual(list(b), ["bank0", "PLRAM2", "PLRAM0", "PLRAM1", "HOST", "PEER"])
        self.assertEqual((b["bank0"]["kind"], b["bank0"]["type"], b["bank0"]["base"], b["bank0"]["size_bytes"]),
                         ("DDR", "DDR4", "0x200000000", 8 << 30))
        self.assertEqual(b["PLRAM2"]["size_bytes"], 256 << 10)
        self.assertEqual(b["bank0"]["connections"], ["lp_ddr.arg0", "lp_ddr.arg1"])
        for t, x in b.items():
            self.assertTrue(x["used"] and x["ready"], (t, x["state"]))
            self.assertFalse(x["pruned"], t)
        self.assertEqual(b["bank0"]["state"], "in use, calibrated")
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertIn("memory         : xclbin 97613339", o)
        self.assertIn("PLRAM2", o)
        rc, o, e = run_cli(h, ["status", "--json"])
        self.assertEqual(len(json.loads(o)["memory"]["banks"]), 6)
        r = self.check(h)
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
        self.assertNotIn("mem.bank_not_ready", self.rules(r))

    def test_memory_is_passive(self):
        h = MockHost(0x0B5E0007)
        load_xclbin(h, [1] * 6, [(0, 0, 0)])
        run_cli(h, ["memory", "-v"])
        self.assertEqual(h.model.writes, [])
        self.assertFalse([c for c in h.runs if "examine" in c or any("xbutil" in x or "xrt-smi" in x for x in c)])

    def test_plram2_unused_may_be_pruned(self):
        h = MockHost(0x0B5E0007)
        load_xclbin(h, [1, 0, 1, 1, 0, 0], [(0, 0, 0), (0, 1, 2), (1, 1, 3)])
        b = self.banks(h)
        p2 = b["PLRAM2"]
        self.assertFalse(p2["used"])
        self.assertEqual(p2["state"], "unused (may be pruned)")
        self.assertIsNone(p2["pruned"], "no marker reaches the xclbin: PRUNED must not be claimed")
        self.assertIn("no marker", p2["evidence"])
        self.assertEqual(b["HOST"]["state"], "unused (may be pruned)")
        self.assertEqual(b["PLRAM0"]["state"], "in use, RP released")
        rc, o, e = run_cli(h, ["memory", "-v"])
        self.assertNotIn("PRUNED", o.replace("PRUNED is shown only where verifiable", ""))
        self.assertEqual(self.check(h)["severity"], "OK")
        h.attrs_[(USER, "icap.u.1/mem_topology")] = mem_topology([0, 1, 1, 1, 0, 0])
        self.assertEqual(self.banks(h)["bank0"]["state"], "unused (static DDR: never pruned)")

    def test_used_bank_not_ready_fails(self):
        h = MockHost(0x0B5E0007)
        load_xclbin(h, [1, 0, 0, 0, 1, 0], [(0, 0, 0), (1, 0, 4)])
        h.model.r[0x00] &= ~(1 << 28)                       # DDR gate closed: not calibrated
        r = self.check(h)
        bad = [i for i in r["items"] if i["rule"] == "mem.bank_not_ready"]
        self.assertEqual({i["message"].split()[1] for i in bad}, {"bank0", "HOST"})   # HOST: host_mem_size 0
        self.assertEqual(r["severity"], "FAIL")
        self.assertIn("mem.bank_not_ready", C.render(r))

    def test_emu_mode_bank_ready(self):
        # EMU: gate closed and MIG held by design, DDR served by the 1 MB URAM emulation -> not a bank_not_ready FAIL
        h = MockHost(0x0B5E0007)
        load_xclbin(h, [1, 0, 0, 0, 0, 0], [(0, 0, 0)])
        h.model.r[0x00] = (h.model.r[0x00] & ~(1 << 28) & ~(3 << 29)) | (2 << 29) | (1 << 31)
        b = self.banks(h)["bank0"]
        self.assertTrue(b["ready"])
        self.assertIn("EMULATED", b["state"])
        self.assertNotIn("mem.bank_not_ready", self.rules(self.check(h)))
        # mutant: EMU with XRT calibration bit 0 must still FAIL
        h.model.memcalib = 0
        self.assertIn("mem.bank_not_ready", self.rules(self.check(h)))

    def test_routev_pruned_ddr(self):
        h = MockHost(RV)
        h.model.set_pruned()
        load_xclbin(h, [0, 1, 1, 1, 0, 0], [(0, 1, 2), (1, 1, 3), (2, 1, 1)])
        b = self.banks(h)["bank0"]
        self.assertEqual((b["state"], b["pruned"], b["used"]), ("PRUNED", True, False))
        self.assertIn("RV_INFO[0]=0", b["evidence"])
        r = self.check(h)
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
        self.assertNotIn("mem.ddr_kept_not_ready", self.rules(r))
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertRegex(o, r"bank0 +DDR +DDR4 .* no +PRUNED")

    def test_routev_kept(self):
        h = MockHost(RV)
        load_xclbin(h, [1, 0, 0, 0, 0, 0], [(0, 0, 0)])
        b = self.banks(h)["bank0"]
        self.assertEqual((b["state"], b["pruned"], b["ready"]), ("KEPT, calibrated", False, True))
        self.assertEqual(self.check(h)["severity"], "OK")
        h.model.rv["cal"] = False                            # KEPT while ddr_ready=0
        r = self.check(h)
        self.assertIn("mem.ddr_kept_not_ready", self.rules(r, "WARN"))
        self.assertIn("mem.bank_not_ready", self.rules(r, "FAIL"))
        h = MockHost(RV)                                     # KEPT, ddr_ready=0, no mem_topology: still the WARN
        h.model.rv["cal"] = False
        self.assertIn("mem.ddr_kept_not_ready", self.rules(self.check(h), "WARN"))

    def test_no_xclbin(self):
        h = MockHost(0x0B5E0007)
        h.attrs_.update({(USER, "xclbinuuid"): "00000000-0000-0000-0000-000000000000",
                         (USER, "icap.u.1/mem_topology"): b""})
        rc, o, e = run_cli(h, ["memory", "-v"])
        self.assertEqual(rc, 0, e)
        self.assertIn("no xclbin loaded", o)
        r = self.check(h)
        self.assertIn("mem.no_xclbin", self.rules(r, "INFO"))
        self.assertEqual(r["severity"], "OK")
        rc, o, e = run_cli(h, ["status", "--json"])
        self.assertFalse(json.loads(o)["memory"]["xclbin_loaded"])

    def test_unreadable_is_info(self):
        r = self.check(MockHost(0x0B5E0007))                 # the default mock has no icap sysfs
        self.assertIn("mem.unavailable", self.rules(r, "INFO"))
        self.assertEqual(r["severity"], "OK")

    def test_old_shell_uses_xrt_calibration(self):
        h = MockHost(0x0B5E0007, extra_attrs={("0000:08:00.0", "rom.m.1/VBNV"): R.VBNV_OLD,
                                              ("0000:08:00.0", "mig_calibration"): "0"})
        load_xclbin(h, [1, 0, 0, 0, 0, 0], [(0, 0, 0)])
        b = self.banks(h)["bank0"]
        self.assertEqual((b["ready"], b["state"]), (False, "in use, NOT CALIBRATED"))
        self.assertIn("mig_calibration=0", b["evidence"])
        self.assertEqual(h.model.reads, [], "an unmapped shell's registers are never read")
        self.assertIn("mem.bank_not_ready", self.rules(self.check(h), "FAIL"))

    def test_parsers_roundtrip(self):
        m = H.parse_mem_topology(mem_topology([1, 0, 1, 1, 0, 1]) * 2)       # two slot copies
        self.assertEqual(len(m), 12)
        self.assertEqual((m[6]["slot"], m[6]["index"], m[6]["tag"]), (1, 0, "bank0"))
        self.assertEqual(H.parse_ip_layout(ip_layout(["k:a", "k:b"]))[1][2], "k:b")
        self.assertEqual(H.parse_connectivity(connectivity([(0, 1, 2)])), [(0, 1, 2)])


class BoardIdTests(unittest.TestCase):
    def test_empty_vpd_prints_what_is_there(self):
        h = MockHost(0x0B5E0007)
        h.attrs_[("0000:09:00.0", "vpd")] = b"\x78"           # hpz440's card: the VPD is only the end tag
        h.files["/sys/bus/pci/devices/0000:09:00.0/infiniband/mlx5_0/board_id"] = b"MT_0000000158"
        h.files["/sys/bus/pci/devices/0000:09:00.0/infiniband/mlx5_0/fw_ver"] = b"16.28.2006"
        rc, o, e = run_cli(h, ["list"])
        self.assertEqual(rc, 0, e)
        self.assertNotIn("?", o.splitlines()[0])
        self.assertIn("VPD empty; PSID MT_0000000158 fw 16.28.2006", o)
        self.assertNotIn("NOT a validated board", o)
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertIn("board          : VPD empty", o)
        r = C.run_check(h, H.discover(h)[0], interval=0)
        self.assertIn("identity.vpd_empty", [i["rule"] for i in r["items"] if i["severity"] == "WARN"])
        self.assertNotIn("identity.board", [i["rule"] for i in r["items"]])
        rc, o, e = run_cli(h, ["list", "--json"])
        j = json.loads(o)[0]
        self.assertEqual((j["vpd_empty"], j["psid"], j["board_validated"]), (True, "MT_0000000158", None))

    def test_normal_vpd_unchanged(self):
        h = MockHost(0x0B5E0007)
        rc, o, e = run_cli(h, ["list"])
        self.assertIn("serial MT1935X08327  board MNV303212A-ADLT rev A7", o)

    def test_list_fits_80_columns(self):
        """hpz440's worst case (empty VPD, route V r2 filler boot): every line fits an 80-column terminal, no field
        is split across lines."""
        h = RouteV2Tests().boot()
        h.attrs_[("0000:09:00.0", "vpd")] = b"\x78"
        h.files["/sys/bus/pci/devices/0000:09:00.0/infiniband/mlx5_0/board_id"] = b"MT_0000000158"
        h.files["/sys/bus/pci/devices/0000:09:00.0/infiniband/mlx5_0/fw_ver"] = b"16.28.2006"
        rc, o, e = run_cli(h, ["list"], columns=80)
        self.assertEqual(rc, 0, e)
        self.assertEqual([x for x in o.splitlines() if len(x) > 80], [])
        for f in ("(VPD empty; PSID MT_0000000158 fw 16.28.2006: not identifiable)", "no xclbin (boot RM: filler)",
                  "ROM_TS 1792479600 (0x6ad71170)", "shell %s" % R.VBNV_ROUTEV):
            self.assertIn(f, o)
        self.assertTrue(o.startswith("card 0  ConnectX-5 "), o)


class OutputWidthTests(unittest.TestCase):
    """fit_text: human reports fold to the terminal (80 columns off a tty) at top-level separators only."""

    def test_status_fits_80_and_keeps_labels(self):
        h = RouteV2Tests().boot()
        rc, o80, e = run_cli(h, ["status", "-v"], columns=80)
        self.assertEqual([x for x in o80.splitlines() if len(x) > 80], [])
        rc, wide, e = run_cli(h, ["status", "-v"])          # counters move between runs: fold this one report below
        o = SMI.fit_text(wide, 80)
        self.assertEqual([x for x in o.splitlines() if len(x) > 80], [])
        labels = [x[:17] for x in wide.splitlines() if x[15:17] == ": "]
        self.assertEqual([x[:17] for x in o.splitlines() if x[15:17] == ": "], labels)
        # folding changes only whitespace: the same words in the same order
        self.assertEqual(" ".join(o.split()), " ".join(wide.split()))
        for x in o.splitlines():
            if x.startswith(" " * 17):
                self.assertTrue(x[17:18].strip(), "continuation not under the value column: %r" % x)

    def test_check_verbose_fits_80(self):
        h = MockHost(0x0B5E0007)
        h.attrs_[("0000:09:00.0", "vpd")] = b"\x78"           # long WARN + hint lines (empty VPD)
        rc, o, e = run_cli(h, ["check", "-v", "--since-boot"], columns=80)
        self.assertEqual([x for x in o.splitlines() if len(x) > 80], [])
        rc, wide, e = run_cli(h, ["check", "-v", "--since-boot"])
        self.assertEqual(" ".join(SMI.fit_text(wide, 80).split()), " ".join(wide.split()))
        self.assertIn("identity.vpd_empty", o)

    def test_split_top_respects_brackets(self):
        self.assertEqual(SMI.split_top("a (b, c; d), e; f -- g [h, i]"), ["a (b, c; d),", "e;", "f --", "g [h, i]"])

    def test_short_lines_untouched_and_no_fold_switch(self):
        self.assertEqual(SMI.fit_text("x: 1\n  y", 80), "x: 1\n  y")
        line = "label          : " + ", ".join("item%d" % i for i in range(40))
        self.assertEqual(SMI.fit_text(line, 1 << 30), line)
        self.assertTrue(all(len(x) <= 60 for x in SMI.fit_text(line, 60).splitlines()))

    def test_status_recorder_last_10(self):
        h = MockHost(RV2)
        h.model.records += [(17, 0, 1000 * k) for k in range(1, 15)]   # 15 records in the ring
        st = json.loads(run_cli(h, ["status", "--json"])[1])
        self.assertEqual(len(st["recorder"]["last"]), 10)
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertIn("last 10; now = FPGA uptime", o)
        self.assertRegex(o, r"\d+\.\d{6} s +\S+( \S+)? ago  THR_TIER")
        j = json.loads(run_cli(h, ["recorder", "--json"])[1])
        self.assertTrue(all(0 <= r["ago_s"] <= j["now_s"] for r in j["records"]))
        self.assertIn("now: FPGA uptime", run_cli(h, ["recorder", "-v"])[1])


# ----------------------------------------------------------------------------------------------- DDR speed / MIG table
class DdrSpeedTests(unittest.TestCase):
    def rules(self, r, sev=None):
        return [i["rule"] for i in r["items"] if sev is None or i["severity"] == sev]

    def ddr(self, h, *extra):
        rc, o, e = run_cli(h, ["ddr", "--json"] + list(extra))
        return rc, json.loads(o) if o.strip().startswith("{") else o

    def test_table_from_the_build_reports(self):
        t = R.mig_config(1792371600)                                  # E6b-2400dw8
        self.assertEqual((t["tck_ps"], t["cl"], t["cwl"], t["trcd"], t["trp"], t["tras"], t["trfc"], t["poll_cyc"]),
                         (833, 19, 18, 19, 19, 39, 421, 300000))
        self.assertEqual((t["speed_grade"], t["ui_clk_mhz"], t["width"]), ("DDR4-2400", 300.12, "72-bit (64 data + 8 ECC)"))
        t = R.mig_config(1792022400)                                  # E6b
        self.assertEqual((t["tck_ps"], t["cl"], t["cwl"], t["input_clk_ps"], t["ca_drive"][:10], t["speed_grade"]),
                         (750, 19, 18, 10000, "RDRV_40_40", "DDR4-2666"))
        self.assertTrue(R.mig_config(1792461600)["ca_drive"].startswith("RDRV_48_48"))
        self.assertEqual(R.mig_config(1792368000)["cl"], 16)             # E6b-2400: catalogue part, CL16/CWL12
        self.assertTrue(R.mig_config(1792465200)["per_xclbin"])
        self.assertIsNone(R.mig_config(1791763200))
        for ts in R.SHELLS:
            self.assertIn(ts, R.MIG_BUILDS, "every known build has a MIG entry")

    def test_formula(self):
        # 333000 cycles per poll, 1000 polls in 1 s of 250 MHz aclk -> 333 MHz ui_clk; 4:1 -> 1332 MHz DDR clock, 2664 MT/s
        ui, dp, dt = R.ui_clk_from_polls(10, 0, 1010, 250000000, 333000)
        self.assertEqual((round(ui, 3), dp, dt), (333.0, 1000, 1.0))
        ck, uic, mts = R.mig_rates(750)
        self.assertAlmostEqual(mts, 8 * uic)
        self.assertAlmostEqual(uic, 333.333, places=3)
        self.assertEqual(R.ui_clk_from_polls(0xFFFFFFF0, 0, 0x10, 250000000, 333000)[1], 0x20)   # poll count wraps
        self.assertEqual(R.ui_clk_precision_pct(1500), 0.21)

    def test_ddr_command_2666(self):
        h = MockHost(0x0B5E0007)                                      # E7a3-2666ca48
        rc, d = self.ddr(h)
        self.assertEqual(rc, 0)
        m = d["speed"]["measured"]
        self.assertAlmostEqual(m["ui_clk_mhz"], 333.33, delta=333.33 * m["precision_pct"] / 100)
        self.assertAlmostEqual(m["mts"], 2666.7, delta=2666.7 * m["precision_pct"] / 100)
        self.assertLess(abs(m["vs_table_pct"]), m["precision_pct"])
        self.assertLess(m["precision_pct"], 0.3)
        self.assertEqual(d["speed"]["table"]["ca_drive"][:10], "RDRV_48_48")
        rc, o, e = run_cli(h, ["ddr", "-v"])
        for want in ("CL 19, CWL 18, tRCD 20, tRP 20, tRAS 43, tRFC 467", "InputClockPeriod 10000 ps", "PHY 4:1",
                     "measured    : 2667.", "(ui_clk 333.", "ECC         : 0 corrected", "DDR         : OK\n  summary   : calibrated and reachable",
                     "  access    : open", "  mode      : on -- real DDR", "not read from the card"):
            self.assertIn(want, o)
        self.assertEqual(h.model.writes, [])

    def test_status_line_and_check_clean(self):
        h = MockHost(0x0B5E0006)                                      # E7a3-2400 (dw8_833)
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertRegex(o, r"DDR speed      : DDR4-2400 \(dw8_833: tCK 833 ps, CL19/CWL18\); measured ui_clk 300\.\d+ MHz = 240\d MT/s")
        r = C.run_check(h, H.discover(h)[0], interval=1.0)
        self.assertNotIn("ddr.rate_mismatch", self.rules(r))

    def test_mis_built_shell_is_flagged(self):
        h = MockHost(0x0B5E0007)
        h.model.ui_mhz = 300.0                                        # a 2400 MIG behind a 2666 ROM_TS
        r = C.run_check(h, H.discover(h)[0], interval=1.0)
        self.assertIn("ddr.rate_mismatch", self.rules(r, "WARN"))
        i = [x for x in r["items"] if x["rule"] == "ddr.rate_mismatch"][0]
        self.assertAlmostEqual(i["evidence"]["deviation_pct"], -10.0, delta=0.3)
        h = MockHost(0x0B5E0007)
        h.model.ui_mhz = 333.33 * 1.005                               # inside the 1 % tolerance
        self.assertNotIn("ddr.rate_mismatch", self.rules(C.run_check(h, H.discover(h)[0], interval=1.0)))
        self.assertEqual(C.run_check(h, H.discover(h)[0], interval=0)["severity"], "OK")   # one snapshot: no rate

    def test_routev_loaded_variant_sets_the_speed(self):
        """2026-10-08, hpz440: a dw8_833 (DDR4-2400, POLL_CYC 300000) profile xclbin on route V r3, whose template is
        e6b750 (2666, 333000), was measured with the template's POLL_CYC and shown as 2666 MT/s."""
        h = MockHost(RV)
        h.model.rv.update(tck=833, w2=0x2000 | 2 << 9 | 32)          # dw8_833, CA 60, 32-bit, ECC off
        rc, d = self.ddr(h)
        self.assertAlmostEqual(d["speed"]["measured"]["mts"], 2400.0, delta=12)
        self.assertEqual(d["speed"]["loaded"]["poll_cyc"], 300000)
        self.assertAlmostEqual(d["speed"]["measured"]["vs_table_pct"], 0.0, delta=0.5)
        o = run_cli(h, ["ddr", "-v"])[1]
        self.assertIn("this load : DDR4-2401", o)
        self.assertIn("32-bit, ECC off, CA RDRV_60_60, MIG dw8_833", o)
        self.assertIn("vs the loaded xclbin's controller", o)
        self.assertIn("measured 2400 MT/s", run_cli(h, ["ddr"])[1].replace("2399", "2400").replace("2401", "2400"))
        h.model.ui_mhz = 270.0                                        # the loaded MIG running 10 % slow: now a finding
        self.assertIn("ddr.rate_mismatch", self.rules(C.run_check(h, H.discover(h)[0], interval=1.0)))

    def test_unknown_build_and_not_calibrated(self):
        h = MockHost(0x0B5E0004)                                      # ROM_TS not in the table
        rc, d = self.ddr(h)
        self.assertIn("unknown build", d["speed"]["table_source"])
        self.assertIsNone(d["speed"]["measured"]["ui_clk_mhz"])
        self.assertIn("ddr.rate_unverified", self.rules(C.run_check(h, H.discover(h)[0]), "INFO"))
        h = MockHost(0x0B5E0007)
        h.model.r[0x00] &= ~(1 << 28)
        rc, d = self.ddr(h)
        self.assertIn("DDR gate is closed", d["speed"]["measured"]["reason"])

    def test_routev(self):
        h = MockHost(RV)
        rc, d = self.ddr(h)
        self.assertIn("loaded xclbin's MIG variant", d["speed"]["measured"]["note"])
        self.assertNotIn("ddr.rate_unverified", self.rules(C.run_check(h, H.discover(h)[0]), "INFO"))
        self.assertNotIn("ddr.rate_mismatch", self.rules(C.run_check(h, H.discover(h)[0])))
        h.model.set_pruned()
        rc, d = self.ddr(h)
        self.assertIn("PRUNED", d["speed"]["measured"]["reason"])
        R.DDR_INFO_MAPS[RV] = {"regs": {0x1CC: ("cal_time_cycles", int)}}   # the hook for route V's DDR_INFO block
        try:
            rc, d = self.ddr(h)
            self.assertIn("cal_time_cycles", d["speed"]["ddr_info"])
        finally:
            R.DDR_INFO_MAPS.pop(RV)

    def test_old_shell_not_available(self):
        h = MockHost(0x0B5E0007, extra_attrs={("0000:08:00.0", "rom.m.1/VBNV"): R.VBNV_OLD,
                                              ("0000:08:00.0", "mig_calibration"): "0"})
        rc, o, e = run_cli(h, ["ddr", "-v"])
        self.assertEqual(rc, 1)
        self.assertIn("not available on this shell", o)
        self.assertEqual(h.model.reads, [])


class DdrVerdictTests(unittest.TestCase):
    """'ddr' leads with a plain verdict and explains each fact (owner, 2026-10-08: 'gate CLOSED' / 'xclbin DDR KEPT'
    / 'mode on (requested on)' meant nothing on their own)."""

    def ddr(self, h, *extra):
        rc, o, e = run_cli(h, ["ddr", "-v"] + list(extra))
        j = json.loads(run_cli(h, ["ddr", "--json"])[1])
        return o, j

    def test_refused_kept_load_is_not_usable(self):
        h = RouteV2Tests().boot(pruned=False)      # hpz440 after validate: a KEPT RM whose MIG never calibrated
        h.model.rv.update(kept=True, cal=False)
        h.model.r[0x120] = 2                        # the RP was reconfigured: not the boot RM
        o, j = self.ddr(h)
        self.assertEqual(j["verdict"]["state"], "NOT USABLE")
        self.assertIn("DDR         : NOT USABLE (did not calibrate)\n  why       : ", o)
        self.assertIn("XRT refused that xclbin", o)
        self.assertIn("  calibrated: NO", o)
        self.assertIn("  access    : blocked -- the shell's DDR gate is closed", o)
        self.assertIn("a DDR xclbin that XRT refused", o)
        self.assertNotIn("gate CLOSED", o)

    def test_pruned_has_no_ddr(self):
        h = RouteV2Tests().boot()                   # the boot filler: no DDR controller at all
        o, j = self.ddr(h)
        self.assertEqual(j["verdict"]["state"], "NO DDR IN THIS XCLBIN")
        self.assertIn("placeholder region has no DDR controller", o)
        self.assertIn("nothing to calibrate", o)

    def test_off_and_raw_registers_behind_verbose(self):
        h = MockHost(RV2)
        summary = run_cli(h, ["ddr"])[1]                 # bare: one verdict line per card, no registers
        self.assertIn("  DDR     : OK -- ", summary)
        self.assertNotIn("DDR_INFO (raw)", summary)
        o, j = self.ddr(h)                               # -v: the full page, raw registers included
        self.assertIn("loaded xclbin's controller (read from the card): DDR4-", o)
        self.assertIn("configuration: the shell's DDR template", o)
        self.assertIn("DDR_INFO (raw)", o)
        h.model.write(0x32008, 1)                   # mode off
        self.assertEqual(self.ddr(h)[1]["verdict"]["state"], "DISABLED")

    def test_ue_is_errors(self):
        h = MockHost(0x0B5E0007)
        h.model.r[0x18] = 3                          # 3 uncorrectable polls since configuration
        o, j = self.ddr(h)
        self.assertEqual(j["verdict"]["state"], "ERRORS")


class AllCardsTests(unittest.TestCase):
    """Owner convention (2026-10-08): read-only reports without parameters cover every card with a summary; -v gives
    the details, -d narrows; actions still need -d when there are several cards. Two cards = the mock card twice."""

    def setUp(self):
        self.orig = H.discover
        H.discover = lambda host: [self.orig(host)[0], dict(self.orig(host)[0], index=1)]

    def tearDown(self):
        H.discover = self.orig

    def test_bare_reports_cover_every_card(self):
        for cmd in ("status", "ddr", "memory", "counters", "recorder"):
            h = MockHost(RC1)
            rc, o, e = run_cli(h, [cmd])
            self.assertNotIn("several", e + o, cmd)
            self.assertIn("card 0  ", o, cmd)
            self.assertIn("card 1  ", o, cmd)
            self.assertIn("-d CARD: one card", o, cmd)

    def test_d_narrows_and_json_shape(self):
        h = MockHost(RC1)
        o = run_cli(h, ["status", "-d", "1"])[1]
        self.assertIn("card 1  ", o)
        self.assertNotIn("card 0  ", o)
        self.assertEqual(len(json.loads(run_cli(h, ["ddr", "--json"])[1])), 2)
        self.assertIsInstance(json.loads(run_cli(h, ["ddr", "-d", "0", "--json"])[1]), dict)

    def test_actions_still_need_d(self):
        h = MockHost(RC1)
        rc, o, e = run_cli(h, ["ddr-mode", "off", "--yes"])
        self.assertNotEqual(rc, 0)
        self.assertIn("2 Innova-2 cards found", e)
        self.assertEqual(h.model.writes, [])


class CheckAfterIdentifyTests(unittest.TestCase):
    """Regression (2026-10-08): run_check on a card a caller had already identified skipped every rule."""

    def test_rules_run_on_an_identified_card(self):
        h = MockHost(RC1, slot_width="1")
        c = H.discover(h)[0]
        H.classify(h, c, query_cx5=False)
        sh = SMI.card_shell(h, c)                              # what status does first
        self.assertEqual(c["state"], "OUR_SHELL")
        r = C.run_check(h, c, interval=0)
        self.assertIn("pcie.slot_link", [i["rule"] for i in r["items"]])
        self.assertEqual(r["severity"], "WARN")
        o = run_cli(MockHost(RC1, slot_width="1"), ["status"])[1]
        self.assertIn("  health  : WARN", o)
        self.assertIn("pcie.slot_link", o)


class SummaryTests(unittest.TestCase):
    def test_single_card_status_summary_then_full(self):
        h = MockHost(RC1)
        o = run_cli(h, ["status"])[1]
        self.assertLessEqual(len(o.splitlines()), 8, o)
        for want in ("  health  : OK", "  DDR     : OK -- ", "  thermal : ", "  PCIe    : FPGA x8", "(-v: details)"):
            self.assertIn(want, o)
        self.assertGreater(len(run_cli(h, ["status", "-v"])[1].splitlines()), 20)

    def test_counters_and_recorder_summaries(self):
        h = MockHost(RC1)
        o = run_cli(h, ["counters"])[1]
        self.assertIn("  ECC       : CE 0, UE polls 0", o)
        self.assertIn("counters in all", o)
        h.model.records += [(17, 0, 1000 * k) for k in range(1, 15)]
        j = json.loads(run_cli(h, ["recorder", "--json"])[1])
        self.assertEqual(len(j["records"]), 5)
        self.assertEqual(len(json.loads(run_cli(h, ["recorder", "-v", "--json"])[1])["records"]), 15)
        self.assertEqual(len(json.loads(run_cli(h, ["recorder", "-n", "3", "--json"])[1])["records"]), 3)


# ---- 1.3.5: the warm-reboot fenced write (ddr.fenced_linkdown_benign) and xbutil PASSED WITH WARNINGS
def _fence(n=1, ld=1, read=False, addr=0x200000000, after_s=0.3, ts=True):
    """n fenced accesses, the first a write (or read) at addr, after ld link-downs; the last link-down at FPGA uptime
    50 s (before the mock host booted at 60 s) and the last fenced access after_s later (E7 timestamps)."""
    def f(h):
        m = h.model
        m.r[0x10] = 0x00050000 | ld
        m.r[0xA0] = n
        m.r[0xA4] = 1 << 31 | (1 << 30 if read else 0)
        m.r[0xA8], m.r[0xAC] = addr & 0xFFFFFFFF, addr >> 32
        if ts:
            m.r[0xE0] = int(50 / R.TS_S)
            m.r[0xF0] = int((50 + after_s) / R.TS_S)
    return f


def _rec(m, typ, t, payload=0):
    m.records.append((typ, payload, int(t / R.REC_S)))


def _hpy440(fence_at=205.822379, shift=-170.0):
    """hpy440 2026-10-07 (innova2-smi recorder): ONE warm reboot gives TWO link-downs 5.6 s apart and the single fence
    lands in the FIRST window (0.54 s after LINK_DOWN 1, before its RECAL_DONE); the registers see only the last
    link-down, 5.06 s after the fence. Times as measured, shifted by `shift` so they precede the mock host's boot."""
    def f(h):
        m = h.model
        _fence(n=1, ld=2, ts=False)(h)
        m.r[0xE0], m.r[0xF0] = int((210.882117 + shift) / R.TS_S), int((fence_at + shift) / R.TS_S)
        for typ, t in ((2, 205.284475), (15, 205.802003), (7, fence_at), (3, 206.295772), (2, 210.882117),
                       (15, 211.400133), (3, 211.893878)):
            _rec(m, typ, t + shift)
        m.records.sort(key=lambda r: r[2])
    return f


def _rec_too(setup):
    def f(h):
        setup(h)
        _rec(h.model, 2, 50.0)
        _rec(h.model, 7, 50.3)
        _rec(h.model, 3, 51.0)
    return f


FENCE_SCENARIOS = [
    ("benign: 1 write at 0x200000000 in the window", 0x0B5E0006, _fence(), "ddr.fenced_linkdown_benign", "INFO"),
    ("benign: 2 writes, 3 link-downs", 0x0B5E0007, _fence(n=2, ld=3), "ddr.fenced_linkdown_benign", "INFO"),
    ("benign without timestamps (E4)", 0x0B5E0002, _fence(ts=False), "ddr.fenced_linkdown_benign", "INFO"),
    ("2 fences, 1 link-down", 0x0B5E0006, _fence(n=2), "ddr.fenced", "WARN"),
    ("fence without a link-down", 0x0B5E0006, _fence(ld=0), "ddr.fenced", "WARN"),
    ("a read", 0x0B5E0006, _fence(read=True), "ddr.fenced", "WARN"),
    ("another address", 0x0B5E0006, _fence(addr=0x200001000), "ddr.fenced", "WARN"),
    ("another address, no timestamps (E4)", 0x0B5E0002, _fence(addr=0x0, ts=False), "ddr.fenced", "WARN"),
    ("outside the link-down window", 0x0B5E0006, _fence(after_s=30), "ddr.fenced", "WARN"),
    ("before the last link-down", 0x0B5E0006, _fence(after_s=-5), "ddr.fenced", "WARN"),
    ("E7 without timestamps", 0x0B5E0006, _fence(ts=False), "ddr.fenced", "WARN"),
    ("hpy440 pattern, passive: fence before the last of 2 link-downs", 0x0B5E0007, _hpy440(), "ddr.fenced", "WARN"),
    ("hpy440 pattern, --recorder: fence inside the first window", 0x0B5E0007, _hpy440(), "ddr.fenced_linkdown_benign",
     "INFO", True),
    ("--recorder: FENCE record between windows", 0x0B5E0007, _hpy440(fence_at=208.0), "ddr.fenced", "WARN", True),
    ("--recorder: the single-link-down benign case", 0x0B5E0006, _rec_too(_fence()), "ddr.fenced_linkdown_benign",
     "INFO", True),
]


def fence_failures(fence=None):
    """Run every fence scenario; return the descriptions the check did NOT classify as expected."""
    saved = C.fence_items
    if fence is not None:
        C.fence_items = fence
    bad = []
    try:
        for desc, oid, setup, rule, sev, *rec in FENCE_SCENARIOS:
            h = MockHost(oid)
            setup(h)
            r = C.run_check(h, H.discover(h)[0], interval=1.0, recorder=bool(rec))
            got = [(i["rule"], i["severity"]) for i in r["items"] if i["rule"].startswith("ddr.fenced")]
            want_sev = "OK" if sev == "INFO" else sev
            if got != [(rule, sev)] or r["severity"] != want_sev:
                bad.append(desc)
    finally:
        C.fence_items = saved
    return bad


RC1_RTL = "/nobackup2/xilinx_shells_work/clkx300fp8c_rc1_src/ip_src/ddr_health/ddr_health.v"


class Rc1Tests(unittest.TestCase):
    """OBS_ID 0x0B5E000C (fp8c rc1 = E7a3-2666ca60 + the fence-window counters): a static-MIG E7a3 shell (no route-V
    block) whose warm-reboot fence is placed by FENCE_LDWIN / FENCE_OUTWIN, with a plain `check` (no --recorder)."""

    def check(self, h):
        return C.run_check(h, H.discover(h)[0], interval=1.0)

    def rules(self, r, sev=None):
        return [i["rule"] for i in r["items"] if sev is None or i["severity"] == sev]

    def fences(self, h, total, inside, outside, first_ts_before_last_ld=False):
        h.model.r[0xA0], h.model.r[0x218], h.model.r[0x21C] = total, inside, outside
        h.model.r[0xA4], h.model.r[0xA8], h.model.r[0xAC] = 1 << 31, 0, 2      # first: a write at 0x200000000
        h.model.r[0x10] = 2                                  # hpy440: two link-downs per warm reboot
        if first_ts_before_last_ld:                          # the fence in the FIRST link-down: timestamps cannot place it
            h.model.r[0xE0], h.model.r[0xF0] = 5000, 4000

    def test_layout_and_build(self):
        lay = R.layout(RC1)
        self.assertTrue(R.has_fencewin(lay) and lay["e7a3"] and lay["released"])
        self.assertFalse(R.is_routev(lay))
        self.assertTrue({0x218, 0x21C, 0x220} <= set(lay["regs"]))
        self.assertFalse(any(0x1C0 <= o < 0x218 for o in lay["regs"]), "rc1 has no route-V block")
        b = R.SHELLS[1792483200]
        self.assertEqual((b["obs_id"], b["name"]), (RC1, "fp8c-rc1"))
        self.assertRegex(b["uuid"] or "", "^[0-9a-f]{32}$")
        self.assertEqual("%x" % 1792483200, "6ad71f80")
        self.assertIn("RDRV_60_60", R.MIG_BUILDS[1792483200]["ca_drive"])
        self.assertEqual(R.THR_DEFAULTS[RC1], R.THR_DEFAULTS[0x0B5E000A])

    def test_map_against_the_rtl(self):
        if not os.path.exists(RC1_RTL):
            self.skipTest("rc1 shell source not on this machine")
        oid, probs = R.check_rtl(open(RC1_RTL).read())
        self.assertEqual((oid, probs), (RC1, []))

    def test_warm_reboot_fence_is_info_without_recorder(self):
        h = MockHost(RC1)
        self.fences(h, 1, 1, 0, first_ts_before_last_ld=True)
        r = self.check(h)
        self.assertIn("ddr.fenced_linkdown_benign", self.rules(r, "INFO"))
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
        it = [i for i in r["items"] if i["rule"] == "ddr.fenced_linkdown_benign"][0]
        self.assertIn("registers", it["evidence"]["timing"])
        self.assertEqual((it["evidence"]["inside_link_down_windows"], it["evidence"]["outside_link_down_windows"]), (1, 0))
        self.assertEqual(h.model.writes, [], "classification must not store to the recorder index")
        self.assertEqual(run_cli(h, ["check"])[0], 0)

    def test_fence_outside_window_is_not_benign(self):
        h = MockHost(RC1)
        self.fences(h, 2, 1, 1)
        r = self.check(h)
        self.assertNotIn("ddr.fenced_linkdown_benign", self.rules(r))
        it = [i for i in r["items"] if i["rule"] == "ddr.fenced"][0]
        self.assertIn("outside every link-down window", it["evidence"]["not_benign"])

    def test_status_shows_the_counters(self):
        h = MockHost(RC1)
        self.fences(h, 1, 1, 0)
        rc, o, e = run_cli(h, ["status", "-v"])
        self.assertEqual(rc, 0, e)
        self.assertIn("fenced DDR accesses 1 (first: write at 0x200000000); 1 inside link-down windows, 0 outside", o)
        fw = json.loads(run_cli(h, ["status", "--json"])[1])["ddr"]["fence_windows"]
        self.assertEqual((fw["inside"], fw["outside"], fw["total"]), (1, 0, 1))
        self.assertIsNone(json.loads(run_cli(MockHost(0x0B5E0007), ["status", "--json"])[1])["ddr"]["fence_windows"])

    def test_mutant_counters_ignored_is_caught(self):
        orig = R.has_fencewin
        try:
            R.has_fencewin = lambda lay: bool(lay and lay.get("routev"))   # mutant: only route V reads the counters
            h = MockHost(RC1)
            self.fences(h, 1, 1, 0, first_ts_before_last_ld=True)
            self.assertNotIn("ddr.fenced_linkdown_benign", self.rules(self.check(h), "INFO"), "mutant not caught")
        finally:
            R.has_fencewin = orig


ROUTEV3_RTL = "/nobackup2/xilinx_shells_work/clkx300fp8c_routev3_src/ip_src/ddr_health/ddr_health.v"


class RouteV3Tests(unittest.TestCase):
    """OBS_ID 0x0B5E000D (route V r3): r2's register map and RTL on a new static; same filler boot RM and fence windows."""

    def test_layout_and_build(self):
        lay = R.layout(RV3)
        self.assertTrue(R.is_routev(lay) and R.has_fencewin(lay))
        self.assertFalse(lay["released"])
        self.assertEqual(lay["regs"], R.layout(RV2)["regs"])
        b = R.SHELLS[1792486800]
        self.assertEqual((b["obs_id"], b["name"], b["boot_rm"]), (RV3, "routeV3", "filler"))
        self.assertRegex(b["uuid"] or "", "^[0-9a-f]{32}$")
        self.assertEqual("%x" % 1792486800, "6ad72d90")
        self.assertIn("RDRV_60_60", R.MIG_BUILDS[1792486800]["ca_drive"])
        self.assertTrue(R.MIG_BUILDS[1792486800]["per_xclbin"])
        self.assertEqual(R.THR_DEFAULTS[RV3], R.THR_DEFAULTS[RV2])
        self.assertIs(R.DDR_INFO_MAPS[RV3], R.DDR_INFO_MAPS[RV2])

    def test_map_against_the_rtl(self):
        if not os.path.exists(ROUTEV3_RTL):
            self.skipTest("route-V r3 shell source not on this machine")
        oid, probs = R.check_rtl(open(ROUTEV3_RTL).read())
        self.assertEqual((oid, probs), (RV3, []))

    def test_boot_filler(self):
        t = RouteV2Tests()
        h = t.boot(RV3)
        rc, o, e = run_cli(h, ["list"])
        self.assertEqual(rc, 0, e)
        self.assertIn("no xclbin (boot RM: filler)", o)
        r = t.check(h)
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])

    def test_warm_reboot_fence_is_info(self):
        t = Rc1Tests()
        h = RouteV2Tests().boot(RV3)
        t.fences(h, 1, 1, 0)
        self.assertIn("ddr.fenced_linkdown_benign", t.rules(t.check(h), "INFO"))


class FencedLinkDownTests(unittest.TestCase):
    def test_scenarios(self):
        self.assertEqual(fence_failures(), [])

    def test_benign_is_info_with_evidence(self):
        h = MockHost(0x0B5E0006)
        _fence()(h)
        r = C.run_check(h, H.discover(h)[0])
        self.assertEqual(r["severity"], "OK", [i for i in r["items"] if i["severity"] != "INFO"])
        it = [i for i in r["items"] if i["rule"] == "ddr.fenced_linkdown_benign"][0]
        self.assertEqual(it["severity"], "INFO")
        self.assertEqual(it["evidence"]["first"], "write at 0x200000000")
        self.assertAlmostEqual(it["evidence"]["fence_after_link_down_s"], 0.3, places=2)
        self.assertIn("E7 event timestamps", it["evidence"]["timing"])
        self.assertEqual(run_cli(h, ["check"])[0], 0)
        self.assertIn("ddr.fenced_linkdown_benign", run_cli(h, ["check", "-v"])[1])

    def test_no_timestamps_says_so(self):
        h = MockHost(0x0B5E0002)
        _fence(ts=False)(h)
        it = [i for i in C.run_check(h, H.discover(h)[0])["items"] if i["rule"] == "ddr.fenced_linkdown_benign"][0]
        self.assertIn("no event timestamps", it["evidence"]["timing"])
        self.assertIn("count, address and link-down count only", it["evidence"]["timing"])

    def test_warn_says_why(self):
        for setup, why in ((_fence(n=2), "2 fenced accesses > 1 link-downs"), (_fence(read=True), "is a read"),
                           (_fence(addr=0x1000), "address 0x1000"), (_fence(after_s=30), "outside the link-down")):
            h = MockHost(0x0B5E0006)
            setup(h)
            it = [i for i in C.run_check(h, H.discover(h)[0])["items"] if i["rule"] == "ddr.fenced"][0]
            self.assertIn(why, it["evidence"]["not_benign"])

    def test_routev_kept_fence_still_fails(self):
        h = MockHost(RV)
        _fence()(h)                                          # the benign signature, but while KEPT
        h.model.rv["fence_kept"] = 1
        r = C.run_check(h, H.discover(h)[0])
        self.assertIn("ddr.fenced_kept", [i["rule"] for i in r["items"] if i["severity"] == "FAIL"])
        self.assertNotIn("ddr.fenced_linkdown_benign", [i["rule"] for i in r["items"]])

    def test_mutant_every_fence_benign_is_caught(self):
        def all_benign(sh, regs, recorder=None):
            return [dict(i, rule="ddr.fenced_linkdown_benign", severity="INFO") for i in C_FENCE(sh, regs, recorder)]
        bad = fence_failures(fence=all_benign)
        for d in ("2 fences, 1 link-down", "a read", "another address", "outside the link-down window",
                  "--recorder: FENCE record between windows"):
            self.assertIn(d, bad)

    def test_mutant_recorder_ignored_is_caught(self):
        self.assertIn("hpy440 pattern, --recorder: fence inside the first window", fence_failures(
            fence=lambda sh, regs, recorder=None: C_FENCE(sh, regs)))

    def test_passive_by_default_recorder_only_on_request(self):
        h = MockHost(0x0B5E0007)
        _hpy440()(h)
        r = C.run_check(h, H.discover(h)[0])
        self.assertEqual(h.model.writes, [])
        it = [i for i in r["items"] if i["rule"] == "ddr.fenced"][0]
        self.assertIn("check --recorder", it["evidence"]["not_benign"])
        rc, o, e = run_cli(h, ["check", "--recorder", "-v"])
        self.assertEqual(rc, 0, o)
        self.assertIn("ddr.fenced_linkdown_benign", o)
        self.assertIn("flight recorder (check --recorder)", o)
        h = MockHost(0x0B5E0007)                             # no fence: --recorder stays passive too
        run_cli(h, ["check", "--recorder"])
        self.assertEqual(h.model.writes, [])


C_FENCE = C.fence_items

# real xbutil output, card 03 on x440 (x4 link): work/logs/e7a3ca48_c03/08b_pcie_link_raw.txt
XBUTIL_PCIE_WARN = """\
    Platform              : innova2_innova2_xdma_202320_3
    SC Version            : n/a
    Platform ID           : 0x6ad6cb20
-------------------------------------------------------------------------------
Test 1 [0000:03:00.1]     : pcie-link 
    Description           : Check if PCIE link is active
    Warning(s)            : Link is active
                            Please make sure that the device is plugged into Gen 3x8,
                            instead of Gen 3x4. Lower performance maybe experienced.
    Test Status           : [PASSED WITH WARNINGS]
-------------------------------------------------------------------------------
Test 2 [0000:03:00.1]     : sc-version 
    Description           : Check if SC firmware is up-to-date
    Test Status           : [PASSED]
-------------------------------------------------------------------------------
Test 3 [0000:03:00.1]     : aux-connection 
    Description           : Check if auxiliary power is connected
    Details               : Aux power connector is not available on this board
    Test Status           : [SKIPPED]
-------------------------------------------------------------------------------
Validation completed, but with warnings
"""


class ValidateWarningsTests(unittest.TestCase):
    def test_passed_with_warnings_parses_as_pass(self):
        t = {x["test"]: x for x in T.parse_validate(XBUTIL_PCIE_WARN)}
        self.assertEqual(t["pcie-link"]["status"], "PASSED")
        self.assertEqual(t["pcie-link"]["raw_status"], "PASSED WITH WARNINGS")
        self.assertEqual(t["pcie-link"]["warnings"], ["Link is active Please make sure that the device is plugged into "
                                                      "Gen 3x8, instead of Gen 3x4. Lower performance maybe "
                                                      "experienced."])
        self.assertEqual((t["sc-version"]["status"], t["sc-version"]["warnings"]), ("PASSED", []))
        self.assertEqual(t["aux-connection"]["status"], "SKIPPED")

    def test_verdict_counts_it_and_shows_the_warning(self):
        st, summ, ev = T.validate_verdict(T.parse_validate(XBUTIL_PCIE_WARN), dict(ue=0))
        self.assertEqual(st, "PASS")
        self.assertIn("2 passed (pcie-link, sc-version), 1 skipped", summ)
        self.assertIn("note: 1 passed with warnings (pcie-link: Link is active", summ)
        self.assertIn("Gen 3x4", summ)
        self.assertTrue(any("PASSED WITH WARNINGS" in e for e in ev))
        self.assertTrue(any("warning: Link is active" in e for e in ev))

    def test_warning_alone_still_passes_and_failure_still_fails(self):
        only = XBUTIL_PCIE_WARN.split("Test 2")[0]
        st, summ, _ = T.validate_verdict(T.parse_validate(only), dict(ue=0))
        self.assertEqual((st, summ.split(";")[0]), ("PASS", "1 passed (pcie-link), 0 skipped"))
        failed = XBUTIL_PCIE_WARN.replace("[PASSED]", "[FAILED]")
        self.assertEqual(T.validate_verdict(T.parse_validate(failed), dict(ue=0))[0], "FAIL")


# ---- ddr-assess (2026-10-08): settle a per-card DDR profile from tester measurements ----
import innova2_smi_ddrassess as A    # noqa: E402

_LOG_CLEAN = """== 12:01:02 s32_5678_833_ca60 0d0d  partials/g2x2_rm_s32_5678_833_ca60_partial.bit
identity: WIDTH=0x20 LANEMAP=FFFF8765 TCK_PS=0x341 MEM_MB=4096 CAL_DONE=0x35
  rep 0              PASS    errors=0          -                      st=0x00050F02    8.16 s (   502 MB/s)
  1MB[0]             PASS    errors=0          -  [SELFSKIP]          st=0x01050F02    0.00 s (   237 MB/s)
  4096MB[0]          PASS    errors=0          -  [SELFSKIP]          st=0x01050F02    8.15 s (   502 MB/s)
"""
_LOG_FAIL = """identity: WIDTH=0x40 LANEMAP=87654320 TCK_PS=0x341 MEM_MB=8192 CAL_DONE=0x35
  rep 0              FAIL    errors=7330       first=0x40  [SELFSKIP]  st=0x01050F06    8.15 s (  1006 MB/s)
  4MB[0]             FAIL    errors=120        first=0x30  [SELFSKIP]  st=0x01050F16    0.01 s (   634 MB/s)
"""
_LOG_NOCAL = """identity: WIDTH=0x48 LANEMAP=76543210 TCK_PS=0x2EE MEM_MB=8192 CAL_DONE=0x34
  rep 0              INVALID errors=0          -  [NO_CAL]            st=0x00000026    4.00 s (  2048 MB/s)
"""


def _res(name, w, lanes, tck, result="CLEAN", ca="RDRV_60_60", variant="dw8_833"):
    return dict(name=name, width=w, lanes=lanes, tck_ps=tck, mem_mb=w * 128, result=result, ca=ca, variant=variant,
                why="x")


class DdrAssessSettleTests(unittest.TestCase):
    def test_log_parser_keeps_the_verdicts(self):
        c = A.parse_run_log(_LOG_CLEAN, "s32_5678_833_ca60")
        self.assertEqual((c["result"], c["lanes"], c["tck_ps"], c["ca"], c["variant"]),
                         ("CLEAN", [5, 6, 7, 8], 833, "RDRV_60_60", "dw8_833"))
        self.assertFalse(c["reloads_confirmed"])
        f = A.parse_run_log(_LOG_FAIL, "x64n1_833_ca60")
        self.assertEqual(f["result"], "FAIL")
        self.assertIn("7330 errors", f["why"])
        n = A.parse_run_log(_LOG_NOCAL, "b72r_dw8_750_ecc_ca60")
        self.assertEqual((n["result"], n["lanes"]), ("NO-CAL", list(range(9))))       # 72-bit: lane 8 implicit

    def test_settle_widest_then_fastest_then_ca(self):
        r = [_res("d16", 16, [6, 8], 833), _res("s32_833_ca48", 32, [5, 6, 7, 8], 833, ca="RDRV_48_48"),
             _res("s32_833_ca60", 32, [5, 6, 7, 8], 833), _res("s32_750", 32, [5, 6, 7, 8], 750, "FAIL", variant="e6b750"),
             _res("x64", 64, [0, 2, 3, 4, 5, 6, 7, 8], 833, "FAIL")]
        s = A.settle(r)
        self.assertEqual(s["choice"]["name"], "s32_833_ca60")
        r.append(_res("s32_750_ok", 32, [5, 6, 7, 8], 750, variant="e6b750"))
        self.assertEqual(A.settle(r)["choice"]["name"], "s32_750_ok")                  # faster wins at equal width

    def test_settle_skips_unusable_and_reports_72(self):
        s = A.settle([_res("x64_1071", 64, [0, 2, 3, 4, 5, 6, 7, 8], 1071, variant=None), _res("d16", 16, [6, 8], 833)])
        self.assertEqual(s["choice"]["name"], "d16")                                    # 1071 ps: no route-V variant
        self.assertIn("banks", A.profile_problem([0, 7], 833, "dw8_833"))
        s72 = A.settle([_res("b72", 72, list(range(9)), 750, variant="e6b750")])
        self.assertTrue(s72["standard"])
        self.assertIsNone(A.settle([_res("bad", 32, [5, 6, 7, 8], 833, "FAIL")])["choice"])

    def test_profile_text_is_what_the_link_hook_reads(self):
        p = A.profile_text(_res("s32", 32, [5, 6, 7, 8], 833), "hpz440_x32_5678", "evidence")
        self.assertIn("set ::innova2_ddr_profile {name hpz440_x32_5678 lanes {5 6 7 8} variant dw8_833 ca RDRV_60_60}", p)
        self.assertIn("prune_unused.tcl", p)

    def test_cli_from_logs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for n, t in (("s32_5678_833_ca60", _LOG_CLEAN), ("x64n1_833_ca60", _LOG_FAIL), ("b72r_dw8_750_ecc_ca60", _LOG_NOCAL)):
                open(os.path.join(d, n + ".log"), "w").write(t)
            rc, o, e = run_cli(MockHost(RC1), ["ddr-assess", "--from-logs", d, "--card-name", "hpz440"])
        self.assertEqual(rc, 0, e)
        self.assertIn("settled: s32_5678_833_ca60 -- 32-bit on lanes 5,6,7,8, DDR4-2401 (dw8_833)", o)
        self.assertIn("one load only", o)
        self.assertIn("lanes {5 6 7 8} variant dw8_833 ca RDRV_60_60}", o)
        self.assertIn("param=compiler.userPreSysLinkOverlayTcl=", o)


class _FakeTester:
    """The tester static as a register model: hwicap (FIFO vacancy, self-clearing CR write, DONE), axi_gpio, and an RP
    whose identity becomes the loaded partial's; BIST runs go BUSY then DONE with the configuration's error count."""

    def __init__(self, configs):
        self.configs, self.cur, self.loaded = configs, None, 0    # configs: name -> dict(width, lmap, tck, mb, cal, errors)
        self.gpio, self.cr, self.fifo, self.fed, self.reg, self.bist_left = 0, 0, 0, [], {}, 0

    def rd(self, o):
        if o == 0x00:
            return self.gpio
        if o == 0x08:
            return int(bool(self.cur) and self.configs[self.cur]["cal"] == 0x35)
        if o == 0x20000 + A.WFV:
            return 1023 - self.fifo
        if o == 0x20000 + A.CR:
            return self.cr
        if o == 0x20000 + A.SR:
            return 1
        if o == 0x20000 + A.ASR:
            return 0
        if o >= 0x30000:
            return 0xC5 if o == 0x30000 else 0x4002
        c = self.configs.get(self.cur, {})
        rp = o - 0x10000
        if rp == A.B["STATUS"]:
            if self.bist_left > 0:
                self.bist_left -= 1
                return A.BUSY
            return A.DONE
        return {A.B["MAGIC"]: 0x42495354 if c else 0x42495300, A.B["VERSION"]: self.loaded + 1, A.B["WIDTH"]: c.get("width", 0),
                A.B["LMAP"]: c.get("lmap", 0), A.B["TCK"]: c.get("tck", 0), A.B["MEMMB"]: c.get("mb", 0),
                A.B["CAL"]: c.get("cal", 0), A.B["ERR_LO"]: self.reg.get("err", 0), A.B["ERR_HI"]: 0}.get(rp, 0)

    def wr(self, o, v):
        if o == 0x00:
            if self.gpio == 0x2 and v == 0x0 and self.fed:            # the RM leaves reset: the fed partial is live
                self.cur = self.fed[0]; self.loaded += 1; self.fed = []
            self.gpio = v
        elif o == 0x20000 + A.WF:
            self.fifo = min(1023, self.fifo + 1)
            if v == 0xAA995566 and self.gpio == 0x3:
                pass
        elif o == 0x20000 + A.CR:
            if v & 0x4:
                self.fifo = 0
            if v & 0x1:
                self.fifo = 0                                          # the ICAP drains the FIFO; CR bit0 self-clears
            self.cr = 0
        elif o - 0x10000 == A.B["CTRL"]:
            c = self.configs[self.cur]
            self.bist_left = 2
            self.reg["err"] = c["errors"] if c["cal"] == 0x35 else 0


class _TesterIO:
    def __init__(self, t):
        self.t = t

    def rd(self, o):
        return self.t.rd(o)

    def wr(self, o, v):
        self.t.wr(o, v)


class DdrAssessLiveTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.cfgs = {"s32_5678_833_ca60": dict(width=32, lmap=0xFFFF8765, tck=833, mb=4096, cal=0x35, errors=0),
                     "x64n1_833_ca60": dict(width=64, lmap=0x87654320, tck=833, mb=8192, cal=0x35, errors=7330),
                     "b72r_dw8_750_ecc_ca60": dict(width=72, lmap=0x76543210, tck=750, mb=8192, cal=0x34, errors=0)}
        for n in self.cfgs:      # a minimal .bit body: sync word + padding (the fake identifies the load by name)
            with open(os.path.join(self.d, "g2x2_rm_%s_partial.bit" % n), "wb") as fh:
                fh.write(b"\xff" * 16 + b"\xaa\x99\x55\x66" + b"\x20\x00\x00\x00" * 8)
        self.fake = _FakeTester(self.cfgs)
        fake = self.fake
        orig = A.Tester.pr_load

        def pr_load(t, path):
            fake.fed = [os.path.basename(path)[len("g2x2_rm_"):-len("_partial.bit")]]
            return orig(t, path)
        self.patch = (A.Tester, "pr_load", orig)
        A.Tester.pr_load = pr_load

    def tearDown(self):
        setattr(*self.patch)

    def host(self):
        h = MockHost(RC1)
        h.pci_devices = lambda: [dict(bdf="0000:03:00.0", vendor="0x10ee", device="0x9038", cls="0x070000", chain=[])]
        h.regio = lambda bdf, n, regions, guard: _TesterIO(self.fake)
        return h

    def test_refuses_without_tester_or_yes(self):
        rc, o, e = run_cli(MockHost(RC1), ["ddr-assess", "--partials", self.d])
        self.assertIn("no DDR tester image running", e)
        rc, o, e = run_cli(self.host(), ["ddr-assess", "--partials", self.d])
        self.assertIn("add --yes", e)
        self.assertEqual(self.fake.loaded, 0)

    def test_assess_and_settle_on_a_simulated_card(self):
        rc, o, e = run_cli(self.host(), ["ddr-assess", "--partials", self.d, "--yes", "--card-name", "hpz440"])
        self.assertEqual(rc, 0, e)
        self.assertIn("settled: s32_5678_833_ca60 -- 32-bit on lanes 5,6,7,8", o)
        self.assertNotIn("one load only", o)                        # measured live: reloads confirmed
        self.assertIn("NO-CAL", o)
        self.assertIn("7330", o)
        self.assertEqual(self.fake.loaded, 1 + 1 + 3)               # 72 + 64 once each; the clean 32-bit 1 + 2 reloads


class DdrAssessSequenceTests(DdrAssessLiveTests):
    """The staged sequence on a simulated card like hpz440: lanes 5-8 clean at tCK 833 (each alone / anchored and all
    four together), nothing clean at 750, 72-bit never calibrates; 3+4+5+6 fails together (a combination effect)."""
    GOOD = {833: {5, 6, 7, 8}, 750: set()}

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        lib = [l.split("\t")[0] for l in open(os.path.join(os.path.dirname(__file__), "ddr_assess_library.tsv")) if l.strip()]
        self.cfgs = {}
        for n in lib:
            self.add(n)
        self.fake = _FakeTester(self.cfgs)
        fake = self.fake
        orig = A.Tester.pr_load

        def pr_load(t, path):
            fake.fed = [os.path.basename(path)[len("g2x2_rm_"):-len("_partial.bit")]]
            return orig(t, path)
        self.patch = (A.Tester, "pr_load", orig)
        A.Tester.pr_load = pr_load

    def add(self, n):
        w, lanes, tck, ecc = A.lib_meta(n)
        lmap = 0x76543210 if w == 72 else sum(l << (4 * k) for k, l in enumerate(lanes)) | (0xFFFFFFFF << (4 * len(lanes)) & 0xFFFFFFFF)
        bad = not set(lanes) <= self.GOOD[tck] or set(lanes) == {3, 4, 5, 6}
        self.cfgs[n] = dict(width=w, lmap=lmap, tck=tck, mb=min(w, 64) * 128, cal=0x34 if w == 72 else 0x35,
                            errors=1000 if bad else 0)
        with open(os.path.join(self.d, "g2x2_rm_%s_partial.bit" % n), "wb") as fh:
            fh.write(b"\xff" * 16 + b"\xaa\x99\x55\x66" + b"\x20\x00\x00\x00" * 8)

    def test_assess_and_settle_on_a_simulated_card(self):
        st, need = os.path.join(self.d, "state.json"), os.path.join(self.d, "needed.tsv")
        rc, o, e = run_cli(self.host(), ["ddr-assess", "--partials", self.d, "--yes", "--card-name", "hpz440",
                                         "--state", st, "--needed", need])
        self.assertEqual(rc, 0, e)
        self.assertIn("clean at tCK 833: lanes [5, 6, 7, 8]", o)
        self.assertIn("lane 7: untested", o)                         # at 750 no bank-67 anchor survives
        self.assertIn("settled: lib_x16_57_833_ca60 -- 16-bit on lanes 5,7", o)
        rows = open(need).read()
        self.assertIn("lib_x32_5678_833_ca60\t32\t5,6,7,8\t833\tDM_DBI_RD\t0\t1\t-", rows)
        n1 = self.fake.loaded
        self.assertLess(n1, 44)                                       # the sequence did not walk the whole library
        self.add("lib_x32_5678_833_ca60")                            # round 2: the needed image was built
        rc, o, e = run_cli(self.host(), ["ddr-assess", "--partials", self.d, "--yes", "--card-name", "hpz440",
                                         "--state", st])
        self.assertEqual(rc, 0, e)
        self.assertIn("settled: lib_x32_5678_833_ca60 -- 32-bit on lanes 5,6,7,8", o)
        self.assertIn("lanes {5 6 7 8} variant dw8_833 ca RDRV_60_60}", o)
        self.assertEqual(self.fake.loaded - n1, 3)                    # only the new image: 1 load + 2 reloads

    def test_refuses_without_tester_or_yes(self):
        pass

    def test_a_healthy_card_stops_at_72_bit(self):
        for n, c in self.cfgs.items():
            if c["width"] == 72 and A.lib_meta(n)[2] == 750:
                c.update(cal=0x35, errors=0)
        rc, o, e = run_cli(self.host(), ["ddr-assess", "--partials", self.d, "--yes"])
        self.assertEqual(rc, 0, e)
        self.assertIn("standard platform DDR works", o)
        self.assertEqual(self.fake.loaded, 3)                         # one image: load + 2 reloads
