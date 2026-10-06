#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""innova2-smi pieces that need XRT's Python binding (pyxrt) or the flash controller: DDR tiling (fill / check),
the lat_probe kernel-clock measurement, and the raw SPI reader. Imported only by the tests that need them.

pyxrt needs XRT's environment (LD_LIBRARY_PATH etc.); innova2_smi re-executes itself once with it when the import
fails (ensure_xrt_env)."""

import os
import sys
import time

import innova2_smi_hw as H

XRT = "/opt/xilinx/xrt"


def ensure_xrt_env():
    """Import pyxrt; if that fails, re-exec this process once with XRT's environment (setup.sh's variables)."""
    try:
        import pyxrt                                           # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("INNOVA2_SMI_XRT_ENV") == "1":
        raise H.Refused("pyxrt cannot be imported even with XRT's environment (is XRT installed in %s?)" % XRT)
    env = dict(os.environ, INNOVA2_SMI_XRT_ENV="1", XILINX_XRT=XRT,
               LD_LIBRARY_PATH=XRT + "/lib" + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else ""),
               PYTHONPATH=XRT + "/python" + (":" + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
               PATH=XRT + "/bin:" + os.environ.get("PATH", ""))
    sys.stdout.flush()
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def _bank0(pyxrt, xb):
    mems = [(i, m) for i, m in enumerate(xb.get_mems()) if m.get_tag().startswith("bank") and m.get_used()]
    if not mems:
        raise H.Refused("the xclbin has no bank0 memory")
    return mems[0][0], mems[0][1].get_size_kb() * 1024


def tile_bank(sh, xclbin, mode, chunk_mb=256, size_mb=0, ev=None, quiet=False, count_only=False):
    """Fill (or check) bank0 through XRT DMA, one host-backed chunk at a time (the low-memory scheme of
    ddr_fill_lowmem.py): after a chunk is done its BO is freed and a device-only placeholder takes its place, so the
    next chunk lands above it. Pattern: (device address of each 64-bit word) ^ 0xA5A5A5A5A5A5A5A5, so data from before
    a reset cannot pass. Returns the chunk count (fill) or the number of mismatching words (check)."""
    import numpy as np
    import pyxrt
    ev = ev if ev is not None else []
    dev = pyxrt.device(sh.user)
    xb = pyxrt.xclbin(xclbin)
    dev.load_xclbin(xb)
    bank, size = _bank0(pyxrt, xb)
    n = chunk_mb << 20
    limit = (size_mb << 20) if size_mb else size
    holders, bad, chunks, t0 = [], 0, 0, time.time()
    dev_only = getattr(pyxrt.bo, "device_only", None)
    if dev_only is None:
        raise H.Refused("pyxrt.bo.device_only is not available in this XRT")
    sync_to, sync_from = pyxrt.xclBOSyncDirection.XCL_BO_SYNC_BO_TO_DEVICE, pyxrt.xclBOSyncDirection.XCL_BO_SYNC_BO_FROM_DEVICE
    while chunks * n < limit:
        try:
            bo = pyxrt.bo(dev, n, pyxrt.bo.normal, bank)
        except Exception as e:                                # noqa: BLE001 -- the bank is full
            if not quiet:
                ev.append("allocation stopped after %d chunks (%d MB): %s" % (chunks, chunks * chunk_mb, str(e)[:60]))
            break
        a = bo.address()
        want = (np.uint64(a) + np.arange(n // 8, dtype=np.uint64) * np.uint64(8)) ^ np.uint64(0xA5A5A5A5A5A5A5A5)
        if mode == "fill":
            bo.write(want.tobytes(), 0)
            bo.sync(sync_to, n, 0)
        else:
            try:
                bo.sync(sync_from, n, 0)
                got = np.frombuffer(bytes(bo.read(n, 0)), dtype=np.uint64)
                k = int(np.count_nonzero(got != want))
                bad += k
                if k and not quiet:
                    ev.append("chunk %d @0x%x: %d of %d words differ" % (chunks, a, k, n // 8))
            except Exception as e:                            # noqa: BLE001 -- XRT reports a failed DMA this way
                bad += 1
                ev.append("chunk %d @0x%x: READ FAILED %s" % (chunks, a, str(e)[:60]))
        del want, bo
        h = pyxrt.bo(dev, n, dev_only, bank)
        if h.address() != a:
            raise H.Refused("placeholder landed at 0x%x, not 0x%x: the tiling broke" % (h.address(), a))
        holders.append(h)
        chunks += 1
    if not quiet:
        ev.append("%s: %d chunks of %d MB in %.1f s%s" % (mode, chunks, chunk_mb, time.time() - t0,
                                                          ", %d mismatching words" % bad if mode != "fill" else ""))
    del holders
    return chunks if mode == "fill" else bad


def lfsr(n, s=0xACE1):
    for _ in range(n):
        s = (s >> 1) ^ ((-(s & 1)) & 0xB4BCD35C)
    return s & 0xFFFFFFFF


class LatProbe:
    """lat_probe mode 2 (spin n cycles, an LFSR step per cycle at II=1) timed from the host: the delivered kernel
    clock = (n2 - n1) / (t2 - t1). The result word is checked against the LFSR computed here."""
    N1, N2 = 1000000, 9000000

    def __init__(self, sh, xclbin, cu="lp_ddr"):
        import pyxrt
        self.pyxrt = pyxrt
        self.dev = pyxrt.device(sh.user)
        xb = pyxrt.xclbin(xclbin)
        uuid = self.dev.load_xclbin(xb)
        name = "lat_probe:{%s}" % cu
        try:
            self.k = pyxrt.kernel(self.dev, uuid, name, pyxrt.kernel.cu_access_mode.shared)
        except (AttributeError, TypeError):
            self.k = pyxrt.kernel(self.dev, uuid, name)
        self.bo = pyxrt.bo(self.dev, 1 << 20, pyxrt.bo.normal, self.k.group_id(0))
        self.res = pyxrt.bo(self.dev, 4096, pyxrt.bo.normal, self.k.group_id(1))
        self.want = {self.N1: lfsr(self.N1), self.N2: lfsr(self.N2)}
        self.errors = 0

    def _spin(self, n):
        p = self.pyxrt
        self.res.write(b"\xef\xbe\xad\xde", 0)
        self.res.sync(p.xclBOSyncDirection.XCL_BO_SYNC_BO_TO_DEVICE, 4, 0)
        t0 = time.perf_counter()
        r = self.k(self.bo, self.res, 0, n, 2)
        r.wait()
        t = time.perf_counter() - t0
        self.res.sync(p.xclBOSyncDirection.XCL_BO_SYNC_BO_FROM_DEVICE, 4, 0)
        got = int.from_bytes(bytes(self.res.read(4, 0)), "little")
        if got != self.want[n]:
            self.errors += 1
            raise H.Refused("lat_probe spin of %d returned 0x%08x, want 0x%08x" % (n, got, self.want[n]))
        return t

    def mhz(self, sh=None, reps=3):
        v = []
        for _ in range(reps):
            a, b = self._spin(self.N1), self._spin(self.N2)
            v.append((self.N2 - self.N1) / (b - a) / 1e6)
        v.sort()
        return v[len(v) // 2]


class SpiReader:
    """The User slot read through the shell's own AXI Quad SPI (mgmt BAR0 + 0x40000) with rawspi's QSpi, which does
    every register access with a single ctypes 32-bit load/store (the TX register is a FIFO)."""
    CH = 240                                      # the IP returns at most 251 bytes per transaction

    def __init__(self, sh):
        here = os.path.dirname(os.path.realpath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import rawspi
        self.q = rawspi.QSpi(sh.mgmt, bar_off=0x40000)
        self.q.reset()
        self.q.rdid(0)                            # chip 0 drops the first transaction after configuration

    def rdid(self):
        return [self.q.rdid(0).hex(), self.q.rdid(1).hex()]

    def read(self, chip, addr, n):
        buf, stalls = bytearray(), 0
        while len(buf) < n:
            k = min(self.CH, n - len(buf))
            try:
                piece = self.q.read(chip, addr + len(buf), k)
            except RuntimeError:
                stalls += 1
                if stalls > 3:
                    break
                time.sleep(0.2)
                self.q.reset()
                continue
            if not piece or len(piece) > k:
                stalls += 1
                break
            buf += piece
        return bytes(buf), stalls
