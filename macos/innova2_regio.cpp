// SPDX-FileCopyrightText: 2026 the innova2 contributors
//
// SPDX-License-Identifier: Apache-2.0
//
// innova2_regio -- register access for innova2-smi on macOS, through the XRT macOS port's DriverKit extension
// (org.xrtmac.XrtUser). One long-lived process; requests on stdin, one reply line each on stdout:
//   list                          one line per function the dext is attached to:
//                                 "fn <bus> <dev> <func> <vendor> <device> <subvendor> <subdevice> <link_status>
//                                  <link_caps> <bar0 size> <bar1 size> ..."
//   r   <bus> <dev> <func> <bar> <off>          -> "ok <value>"           (32-bit read; hex numbers)
//   rb  <bus> <dev> <func> <bar> <off> <n>      -> "ok <v0> <v1> ..."     (n consecutive 32-bit reads)
//   cfg <bus> <dev> <func> <off>                -> "ok <value>"           (PCI configuration dword)
//   section <bus> <dev> <slot>                  -> "ok <hex bytes>"       (xclbin section the dext keeps: 0 mem
//                                                                           topology, 1 ip layout, 2 connectivity)
//   clock <bus> <dev> <index>                   -> "ok <MHz>"             (kernel clock counter, 0..2)
//   uuid <bus> <dev>                            -> "ok <32 hex>"          (UUID of the xclbin the dext loaded)
//   quit
//   w <bus> <dev> <func> <bar> <off> <val>      -> "ok <readback>"       (XRT_DEXT_REG_WRITE; mgmt BAR0, Innova-2 windows)
// Errors: "err <reason>". Reads only through the mapping: the dext maps BARs read-only (kIOUserClientMemoryReadOnly) and this tool has no
// write request. Built against the port's xrt_dext_abi.h; refuses a dext whose ABI version differs (a tool built
// against another ABI silently reads the wrong selectors -- see the port's build_tools.sh).
#include <IOKit/IOKitLib.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <tuple>
#include <vector>
#include "xrt_dext_abi.h"

struct fn { io_connect_t conn; xrt_dext_device_info info; std::map<uint32_t, std::pair<mach_vm_address_t, mach_vm_size_t>> bars; };
static std::vector<fn> fns;

static int open_all() {
  io_iterator_t it = 0;
  if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceNameMatching(XRT_DEXT_USER_CLASS), &it)) return -1;
  while (io_service_t svc = IOIteratorNext(it)) {
    io_connect_t conn = 0;
    if (IOServiceOpen(svc, mach_task_self(), 0, &conn) == KERN_SUCCESS) {
      fn f{}; f.conn = conn; size_t sz = sizeof(f.info);
      if (IOConnectCallStructMethod(conn, XRT_DEXT_GET_DEVICE_INFO, nullptr, 0, &f.info, &sz) == KERN_SUCCESS) {
        if (f.info.abi_version != XRT_DEXT_ABI_VERSION) {
          printf("err dext ABI %u, built for %u -- rebuild innova2_regio\n", f.info.abi_version, XRT_DEXT_ABI_VERSION);
          fflush(stdout); exit(2);
        }
        fns.push_back(f);
      } else IOServiceClose(conn);
    }
    IOObjectRelease(svc);
  }
  IOObjectRelease(it);
  return (int)fns.size();
}

static fn* find(unsigned b, unsigned d, unsigned f) {
  for (auto& x : fns) if (x.info.bus == b && x.info.dev == d && x.info.func == f) return &x;
  return nullptr;
}

static volatile uint32_t* bar(fn* f, uint32_t n, mach_vm_size_t* len) {
  auto it = f->bars.find(n);
  if (it == f->bars.end()) {
    mach_vm_address_t a = 0; mach_vm_size_t l = 0;
    if (IOConnectMapMemory64(f->conn, XRT_DEXT_MAP_BAR(n), mach_task_self(), &a, &l, kIOMapAnywhere | kIOMapInhibitCache) != KERN_SUCCESS)
      return nullptr;
    it = f->bars.emplace(n, std::make_pair(a, l)).first;
  }
  *len = it->second.second;
  return reinterpret_cast<volatile uint32_t*>(it->second.first);
}

int main() {
  setvbuf(stdout, nullptr, _IOLBF, 0);
  if (open_all() < 0) { puts("err no IOKit matching for " XRT_DEXT_USER_CLASS); return 1; }
  char line[512];
  while (fgets(line, sizeof line, stdin)) {
    char cmd[16] = {0}; unsigned b, d, f, n; unsigned long long off, cnt, val;
    if (sscanf(line, "%15s", cmd) != 1) continue;
    if (!strcmp(cmd, "quit")) break;
    if (!strcmp(cmd, "list")) {
      for (auto& x : fns) {
        printf("fn %x %x %x %x %x %x %x %x %x", x.info.bus, x.info.dev, x.info.func, x.info.vendor, x.info.device,
               x.info.subsystem_vendor, x.info.subsystem_id, x.info.link_status, x.info.link_caps);
        for (int i = 0; i < 6; ++i) printf(" %llx", (unsigned long long)x.info.bars[i].size);
        printf("\n");
      }
      printf("end\n"); continue;
    }
    if (!strcmp(cmd, "cfg") && sscanf(line, "%*s %x %x %x %llx", &b, &d, &f, &off) == 4) {
      fn* x = find(b, d, f); if (!x) { puts("err no such function"); continue; }
      if (off % 4 || off >= XRT_CFG_SPACE_BYTES) { puts("err cfg offset"); continue; }
      uint64_t in = off, out = 0; uint32_t outn = 1;
      if (IOConnectCallScalarMethod(x->conn, XRT_DEXT_CFG_READ, &in, 1, &out, &outn) != KERN_SUCCESS) { puts("err cfg read failed"); continue; }
      printf("ok %llx\n", (unsigned long long)out); continue;
    }
    // management-function queries the dext answers from what it keeps (no register access from this process)
    if (!strcmp(cmd, "section") && sscanf(line, "%*s %x %x %x", &b, &d, &n) == 3) {
      fn* x = find(b, d, 0); if (!x) { puts("err no management function"); continue; }
      std::vector<unsigned char> res; uint64_t total = 0; bool bad = false;
      do {
        uint64_t in[2] = { n, res.size() }; uint64_t out = 0; uint32_t nout = 1;
        unsigned char chunk[XRT_SECTION_CHUNK]; size_t got = sizeof(chunk);
        if (IOConnectCallMethod(x->conn, XRT_DEXT_GET_SECTION, in, 2, nullptr, 0, &out, &nout, chunk, &got) != KERN_SUCCESS
            || out > XRT_SECTION_MAX_BYTES) { bad = true; break; }
        total = out; if (!got) break;
        res.insert(res.end(), chunk, chunk + got);
      } while (res.size() < total);
      if (bad || res.size() != total) { puts("err no such section (no xclbin loaded through this dext?)"); continue; }
      printf("ok "); for (unsigned char c : res) printf("%02x", c); printf("\n"); continue;
    }
    if (!strcmp(cmd, "clock") && sscanf(line, "%*s %x %x %x", &b, &d, &n) == 3) {
      fn* x = find(b, d, 0); if (!x) { puts("err no management function"); continue; }
      uint64_t in = n, out = 0; uint32_t nout = 1;
      kern_return_t kr = IOConnectCallScalarMethod(x->conn, XRT_DEXT_CLOCK_MEASURE, &in, 1, &out, &nout);
      if (kr != KERN_SUCCESS) { printf("err clock %u: 0x%x\n", n, kr); continue; }
      printf("ok %llu\n", (unsigned long long)out); continue;
    }
    if (!strcmp(cmd, "uuid") && sscanf(line, "%*s %x %x", &b, &d) == 2) {
      fn* x = find(b, d, 0); if (!x) { puts("err no management function"); continue; }
      unsigned char u[16] = {0}; size_t sz = sizeof(u);
      if (IOConnectCallStructMethod(x->conn, XRT_DEXT_GET_XCLBIN_UUID, nullptr, 0, u, &sz) != KERN_SUCCESS) { puts("err uuid"); continue; }
      printf("ok "); for (unsigned char c : u) printf("%02x", c); printf("\n"); continue;
    }
    // w <bus> <dev> <func> <bar> <off> <val> -> "ok <readback>": one 32-bit write through the dext's
    // XRT_DEXT_REG_WRITE (selector 36), which allows only the management function's BAR0, only on an Innova-2 shell
    // (feature ROM VBNV "innova2_*"), only inside its two register windows -- the dext enforces all of it
    if (!strcmp(cmd, "w") && sscanf(line, "%*s %x %x %x %x %llx %llx", &b, &d, &f, &n, &off, &val) == 6) {
#ifdef XRT_DEXT_REG_WRITE_VBNV_PREFIX   /* the patch defines this macro; XRT_DEXT_REG_WRITE itself is an enum value */
      fn* x = find(b, d, f); if (!x) { puts("err no such function"); continue; }
      if (f != 0 || n != 0) { puts("err writes go to the management function's BAR0 only"); continue; }
      uint64_t in[2] = { off, val }, out = 0; uint32_t nout = 1;
      kern_return_t kr = IOConnectCallScalarMethod(x->conn, XRT_DEXT_REG_WRITE, in, 2, &out, &nout);
      if (kr != KERN_SUCCESS) { printf("err write 0x%llx: 0x%x\n", off, kr); continue; }
      printf("ok %x\n", (uint32_t)out); continue;
#else
      puts("err this innova2_regio was built against a dext without XRT_DEXT_REG_WRITE"); continue;
#endif
    }
    bool bulk = !strcmp(cmd, "rb");
    if ((!strcmp(cmd, "r") && sscanf(line, "%*s %x %x %x %x %llx", &b, &d, &f, &n, &off) == 5) ||
        (bulk && sscanf(line, "%*s %x %x %x %x %llx %llx", &b, &d, &f, &n, &off, &cnt) == 6)) {
      if (!bulk) cnt = 1;
      fn* x = find(b, d, f); if (!x) { puts("err no such function"); continue; }
      mach_vm_size_t len = 0; volatile uint32_t* p = bar(x, n, &len);
      if (!p) { puts("err cannot map BAR"); continue; }
      if (off % 4 || off + 4 * cnt > len || cnt > 4096) { puts("err offset outside BAR"); continue; }
      printf("ok");
      for (unsigned long long i = 0; i < cnt; ++i) printf(" %x", p[off / 4 + i]);
      printf("\n"); continue;
    }
    puts("err bad request");
  }
  return 0;
}
