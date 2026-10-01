#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Raw AXI Quad SPI driver for the Innova2 config flash.

WHY THIS EXISTS. Repointing Factory's WBSTAR needs ONE byte changed on chip 0 at offset 0x78
(0x30 -> 0x10, a single bit cleared). NOR flash programs 1->0 without an erase, so this is the
safest possible flash operation -- but no existing tool can express it:
  * xbflash applies an unconditional +0x1000 payload shift, so offset 0x78 would need a declared
    base of -0x1000;
  * JTAG flash programming needs a cable, Vivado and the JTAG grant, and rewrites whole sectors.

CRITICAL -- ctypes, NOT mmap slicing. Python's mmap slice assignment goes through glibc memcpy,
which on this platform issues the store TWICE (measured). The SPI transmit
register is a FIFO: a duplicated store injects an extra byte into the command stream and turns a
1-byte program into something else entirely. Every register access here is a ctypes 32-bit access.

SAFETY. `program` writes ONE byte and refuses unless the new value only CLEARS bits (val & old ==
val), so it can never require an erase and can never set a bit that was clear. There is no erase
command in this file at all -- by design.
"""
import ctypes, mmap, os, struct, sys, time

# AXI Quad SPI register offsets (PG153), relative to the IP base
SRR      = 0x40   # software reset: write 0x0000000A
SPICR    = 0x60   # control
SPISR    = 0x64   # status
SPI_DTR  = 0x68   # data transmit (FIFO)
SPI_DRR  = 0x6C   # data receive  (FIFO)
SPISSR   = 0x70   # slave select (active low)
TX_OCY   = 0x74
RX_OCY   = 0x78
# SPICR bits
CR_LOOP=1<<0; CR_SPE=1<<1; CR_MASTER=1<<2; CR_CPOL=1<<3; CR_CPHA=1<<4
CR_TXRST=1<<5; CR_RXRST=1<<6; CR_MANSS=1<<7; CR_INHIBIT=1<<8
# SPISR bits
SR_RXEMPTY=1<<0; SR_RXFULL=1<<1; SR_TXEMPTY=1<<2; SR_TXFULL=1<<3

class QSpi:
    def __init__(self, bdf=None, bar_off=None, span=0x1000):
        # the default was HARDCODED to Host C's FPGA, and only the CLI
        # honoured $BDF -- so every LIBRARY user of this class silently addressed the wrong
        # card. A flash script's safety gate did exactly that and threw
        # FileNotFoundError, which its grep turned into "gate did not pass": a tool failure
        # presented as a hardware verdict, on a perfectly healthy card.
        bdf = bdf or default_bdf()
        # the flash controller's BAR and offset: BAR0 + 0x40000 on the Flex burn endpoint;
        # RAWSPI_BAR / RAWSPI_BAR_OFFSET for any other design.
        if bar_off is None: bar_off=int(os.environ.get("RAWSPI_BAR_OFFSET","0x40000"),0)
        path="/sys/bus/pci/devices/%s/resource%d" % (bdf, int(os.environ.get("RAWSPI_BAR","0")))
        enable_mem(bdf)
        self.fd=os.open(path, os.O_RDWR|os.O_SYNC)
        pagesz=mmap.PAGESIZE
        self.base=(bar_off//pagesz)*pagesz
        self.delta=bar_off-self.base
        self.map=mmap.mmap(self.fd, self.delta+span, mmap.MAP_SHARED,
                           mmap.PROT_READ|mmap.PROT_WRITE, offset=self.base)
        addr=ctypes.addressof(ctypes.c_char.from_buffer(self.map))
        self.regs=addr+self.delta
    def wr(self,off,val):
        ctypes.c_uint32.from_address(self.regs+off).value = val & 0xFFFFFFFF
    def rd(self,off):
        return ctypes.c_uint32.from_address(self.regs+off).value
    def reset(self):
        self.wr(SRR, 0x0000000A); time.sleep(0.01)
        self.wr(SPICR, CR_MASTER|CR_MANSS|CR_INHIBIT|CR_TXRST|CR_RXRST)
        self.wr(SPISSR, 0xFFFFFFFF)
        self.wr(SPICR, CR_MASTER|CR_MANSS|CR_INHIBIT|CR_SPE)
    def xfer(self, slave, out_bytes, nread=0):
        """Assert SS, clock out out_bytes, keep clocking nread dummy bytes, return the read bytes."""
        self.wr(SPICR, CR_MASTER|CR_MANSS|CR_INHIBIT|CR_SPE|CR_TXRST|CR_RXRST)
        self.wr(SPICR, CR_MASTER|CR_MANSS|CR_INHIBIT|CR_SPE)
        # Drain anything left in the RX FIFO. After a timed-out transaction the FIFO can still hold
        # bytes; they would prepend to the next transfer, and in a bulk dump that SHIFTS THE WHOLE
        # FILE and reads as ~38% corruption. The FIFO reset above should cover it, but
        # draining explicitly costs nothing and removes the failure mode.
        guard=0
        while not (self.rd(SPISR) & SR_RXEMPTY):
            self.rd(SPI_DRR); guard+=1
            if guard>4096: raise RuntimeError("RX FIFO will not drain")
        payload = bytes(out_bytes) + b"\x00"*nread
        for b in payload:
            self.wr(SPI_DTR, b)                       # FIFO: ctypes, single store
        self.wr(SPISSR, ~(1 << slave) & 0xFFFFFFFF)   # assert this slave
        self.wr(SPICR, CR_MASTER|CR_MANSS|CR_SPE)     # release inhibit -> transfer runs
        t0=time.time()
        while not (self.rd(SPISR) & SR_TXEMPTY):
            if time.time()-t0 > 2: raise RuntimeError("SPI TX timeout")
        self.wr(SPICR, CR_MASTER|CR_MANSS|CR_INHIBIT|CR_SPE)
        self.wr(SPISSR, 0xFFFFFFFF)                   # deassert
        got=bytearray()
        while not (self.rd(SPISR) & SR_RXEMPTY):
            got.append(self.rd(SPI_DRR) & 0xFF)
        return bytes(got[len(out_bytes):]) if nread else bytes(got)
    def rdid(self, slave):
        return self.xfer(slave, [0x9F], 5)
    def rdsr(self, slave):
        r=self.xfer(slave, [0x05], 1)
        return r[0] if r else 0xFF
    def read(self, slave, addr, n):
        # 0x13 = READ with 4-byte address
        return self.xfer(slave, [0x13,(addr>>24)&0xFF,(addr>>16)&0xFF,(addr>>8)&0xFF,addr&0xFF], n)
    def program_byte(self, slave, addr, val):
        old=self.read(slave, addr, 1)[0]
        if (old & val) != val:
            raise RuntimeError("REFUSED: 0x%02X -> 0x%02X would SET bits (needs an erase); "
                               "this tool only clears bits" % (old,val))
        if old == val:
            return old, val, "already correct, nothing written"
        self.xfer(slave, [0x06])                       # WREN
        st=self.rdsr(slave)
        if not (st & 0x02):
            raise RuntimeError("write-enable latch did not set (SR=0x%02X)" % st)
        # 0x12 = PAGE PROGRAM with 4-byte address
        self.xfer(slave, [0x12,(addr>>24)&0xFF,(addr>>16)&0xFF,(addr>>8)&0xFF,addr&0xFF, val])
        t0=time.time()
        while self.rdsr(slave) & 0x01:                 # WIP
            if time.time()-t0 > 5: raise RuntimeError("program timeout (WIP stuck)")
        return old, self.read(slave, addr, 1)[0], "programmed"

def enable_mem(bdf):
    """Turn on PCI Memory Space if it is off. Nothing binds a driver to the Flex burn endpoint, so its BAR stays
    disabled after boot and every register read returns nothing (the RDID comes back empty)."""
    cfg = "/sys/bus/pci/devices/%s/config" % bdf
    with open(cfg, "rb") as f:
        f.seek(4); cmd = struct.unpack("<H", f.read(2))[0]
    if not cmd & 0x2:
        with open("/sys/bus/pci/devices/%s/enable" % bdf, "w") as f:
            f.write("1")
        with open(cfg, "rb") as f:
            f.seek(4); cmd = struct.unpack("<H", f.read(2))[0]
        if not cmd & 0x2:
            raise SystemExit("rawspi: could not enable memory space on %s (COMMAND 0x%04x)" % (bdf, cmd))

def default_bdf():
    """$BDF, else the one Flex burn endpoint (15b3:0264) on this host. Refuses to guess when there are several or none: a wrong default once addressed the
    wrong card silently."""
    if os.environ.get("BDF"): return os.environ["BDF"]
    import glob
    c = []
    for d in glob.glob("/sys/bus/pci/devices/*"):
        try:
            v, i = open(d + "/vendor").read().strip(), open(d + "/device").read().strip()
        except OSError:
            continue
        if (v, i) == ("0x15b3", "0x0264"): c.append(os.path.basename(d))
    if len(c) != 1:
        raise SystemExit("rawspi: set BDF=<bdf> -- %s" % ("found several cards: " + " ".join(sorted(c)) if c else "no Flex burn endpoint found"))
    return c[0]

def main():
    a=sys.argv[1:]
    if not a: print(__doc__); return
    q=QSpi(default_bdf()); q.reset()
    # chip 0 is clocked through STARTUPE3, which drops the first transaction after configuration (measured:
    # the first RDID of chip 0 on a freshly configured Flex image reads all-ones, every later one is correct).
    # A throw-away read absorbs it; RDID changes nothing.
    q.rdid(0)
    cmd=a[0]
    if cmd=="rdid":
        for sl in (0,1): print("  slave%d RDID: %s" % (sl, q.rdid(sl).hex()))
    elif cmd=="dump":
        # chunked bulk read -- the IP's TX/RX FIFO is shallow, so a single huge xfer would overflow.
        # Each chunk is an independent READ command at its own address, so a dropped chunk cannot
        # silently shift the rest of the file.
        sl=int(a[1]); addr=int(a[2],16); n=int(a[3],16); out=a[4]
        # 240, not 256: the IP returns at most 251 bytes per transaction (FIFO depth minus the
        # 5 command bytes), so a 256-byte request SHORT-READS. Each chunk re-addresses, so a short
        # read cannot shift the file -- but a zero-length one would spin, hence the guard below.
        CH=int(os.environ.get('RAWSPI_CHUNK','240')); buf=bytearray()
        while len(buf)<n:
            k=min(CH,n-len(buf))
            # Retry on a transient stall. xbflash releases the controller asynchronously, so the
            # first transaction after a flash write can time out once; aborting a 9 MB verification
            # for that would be needless. Each chunk re-addresses, so a retry cannot shift the file.
            piece=None
            for attempt in range(4):
                try:
                    piece=q.read(sl,addr+len(buf),k)
                    # Length discipline: a transfer must return EXACTLY what was asked for. A short
                    # read is recoverable (the next chunk re-addresses), but a LONG one means stale
                    # FIFO bytes crept in and every subsequent byte is misaligned -- so reject it.
                    if len(piece)>k:
                        raise RuntimeError("over-long read: asked %d got %d" % (k,len(piece)))
                    break
                except RuntimeError as e:
                    if attempt==3: raise
                    sys.stderr.write("  (SPI stall at 0x%08X, resetting and retrying: %s)\n"
                                     % (addr+len(buf), e))
                    time.sleep(0.2); q.reset()
            if not piece:
                raise RuntimeError("zero-length SPI read at 0x%08X -- aborting" % (addr+len(buf)))
            buf+=piece
        if len(buf)!=n:
            raise RuntimeError("dump length %d != requested %d -- refusing to write a misaligned file"
                               % (len(buf),n))
        open(out,"wb").write(bytes(buf))
        print("  dumped %d bytes from slave%d 0x%08X -> %s" % (len(buf),sl,addr,out))
    elif cmd=="read":
        sl=int(a[1]); addr=int(a[2],16); n=int(a[3],16)
        print(q.read(sl,addr,n).hex())
    elif cmd=="program":
        sl=int(a[1]); addr=int(a[2],16); val=int(a[3],16)
        if os.environ.get("RAWSPI_CONFIRM")!="yes":
            print("REFUSED: set RAWSPI_CONFIRM=yes to actually program"); sys.exit(1)
        old,new,how=q.program_byte(sl,addr,val)
        print("  slave%d 0x%08X: 0x%02X -> 0x%02X  (%s)" % (sl,addr,old,new,how))
        print("  VERIFY %s" % ("OK" if new==val else "*** MISMATCH"))
    else: print("unknown command"); sys.exit(1)

if __name__ == "__main__":
    main()
