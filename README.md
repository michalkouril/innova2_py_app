# innova2_app — Innova-2 management app for current Mellanox/NVIDIA drivers

Version 1.3.2 (2026-10-06). New in 1.3.2: `innova2-smi ddr`, the DDR configuration of each build (MIG part, tCK, CL/CWL, tRCD/tRP/tRAS/tRFC, width, ECC, input clock, CA drive) and its speed measured on the card. New in 1.3.1: `innova2-smi memory` (and a memory section in `status`): every memory bank of
the loaded xclbin, used / ready / PRUNED; boards with an empty ConnectX-5 VPD are reported as such. New in 1.3.0:
[`innova2-smi`](#innova2-smi--query-and-test-the-xrt-shell), which queries and tests the Innova-2 XRT shell.
`innova2_app` itself is unchanged.

`innova2_app` manages the Xilinx FPGA on a Mellanox Innova-2 Flex card. It does what Mellanox's
`innova2_flex_app` 18.07.00 did, with the same menus and options:

* query which FPGA image is selected and which one is running (User, Factory or Flex)
* select the image to boot on the next cold power cycle
* grant or revoke JTAG access to the FPGA
* read the FPGA's identity, temperature, fan speed and power level, and run the DDR and PCI tests
* burn a new User image into the card's flash

It adds non-interactive batch commands (`--batch …`) for scripts.

## Why it exists

The vendor app no longer runs on current software. It talks to the card through the `mlx5_fpga_tools` kernel
module. MLNX_OFED 5.2 is the last release that ships that module; 5.3 removed it, and no later MLNX_OFED or
DOCA-OFED release has it. So on a current kernel and driver the vendor app stops at startup and the card cannot be
managed at all.

The firmware interface underneath is still there. Every operation the vendor app performs is a read or write of
one of three ConnectX-5 access registers (`FPGA_CAP` 0x4022, `FPGA_CTRL` 0x4023, `FPGA_ACCESS_REG` 0x4024), and
current kernels still export the function that sends them. `innova2_app` drives those registers directly.

## How it reaches the card

It uses whichever of two paths the host offers (`--transport auto`, the default, tries them in this order):

| `--transport` | what provides it | what works |
|---|---|---|
| **kmod** (kernel module) | The device node `/dev/<bdf>_mlx5_fpga_tools`. It is created either by the vendor `mlx5_fpga_tools` module (OFED 5.2) **or** by the `innova2_areg` module in this package, which builds on any current kernel/OFED. | everything |
| **mlxreg** | MFT's `mlxreg`, or mstflint's open-source `mstreg` (same flags; not yet tested on the card). The FPGA registers are sent raw, by ID and length, so no register database is needed. No kernel module needed. | query, capabilities, CR-space reads and writes. **Not** image select or the JTAG grant: the firmware rejects `FPGA_CTRL` writes on this path (`ME_ICMD_OPERATIONAL_ERROR`). |

In short: install the `innova2_areg` kernel module and everything works; without it, you can still query the card.

The app reports which path it chose and leaves out menu items that path cannot perform. It never starts an
MFT/mstflint tool unless you ask: `--transport mlxreg`, or `--cross-check`, which re-reads each image-select/JTAG
write through `mlxreg`/`mstreg` as an independent confirmation. (`--transport node`, the 1.1.0 name for `kmod`, is
still accepted.)

## Contents

| file | what |
|---|---|
| `innova2_app.py` | the app. Vendor menus and options, plus `[ext]` batch commands (`--batch …`, `--transport`, `--yes`). |
| `innova2_smi.py` (+ `innova2_smi_*.py`) | `innova2-smi`: query and test the Innova-2 XRT shell (see below). `innova2_smi_regmap.py` holds the register maps, `_hw` discovery and guarded MMIO, `_check` the passive check and its rules table, `_tests` / `_xrt` the active tests, `_flash` `setup`, `shells` and `flash`. |
| `docs/innova2_smi_prune_marker.md` | proposal: a marker the v++ prune hooks could put in the xclbin so `innova2-smi memory` can show PRUNED for PLRAM/HOST/PEER. |
| `docs/innova2_smi_regmap.md` | the shell register map innova2-smi uses, generated from `innova2_smi_regmap.py` (`innova2-smi regmap --markdown`) and checked against `ddr_health.v` (`regmap --check-rtl`). |
| `tests/` | innova2-smi unit tests against mock hosts (`python3 -m unittest discover -s tests`). |
| `rawspi.py` | direct access to both flash chips. The app uses it to check that both chips answer before burning from a running User image, and to verify a burn: first and last 4 KB plus 14 spread blocks. |
| `innova2_areg_kmod/` | `innova2_areg.ko` source (GPL-2.0). It re-creates the vendor device node, with the same ioctls, on top of the kernel's exported `mlx5_core_access_reg()`. `build.sh install` registers it with DKMS so it rebuilds on kernel/OFED updates, and loads it at boot. |
| `innova2_areg.sh` | query, capabilities and CR reads/writes as a shell script over `mlxreg`/`mstreg`, for hosts without Python. Image select and the JTAG grant are refused there (the firmware rejects them on that path); use `innova2_app`. |
| `install.sh` | copies everything to `/opt/innova2_app`, links the commands into `/usr/local/bin`, and installs the kernel module. |
| `debian/` | Debian/Ubuntu packaging: `innova2-app` and `innova2-areg-dkms` (see Install). |
| `packaging/rpm/innova2-app.spec` | RPM spec for the same two packages. A placeholder: it builds, but has not been tested on an RPM-based host. |

## Requirements

* Linux, root, Python 3.8+ (standard library only). No Mellanox/NVIDIA user-space tools are needed.
* A working `mlx5_core` for the card's ConnectX-5. The in-box kernel driver is enough; MLNX_OFED/DOCA-OFED also work
  (tested there; an in-box-only host is not yet tested).
* **For image select and the JTAG grant:** the `innova2_areg` kernel module, which needs `dkms` and the kernel
  headers. Hosts that still have the vendor `mlx5_fpga_tools` module don't need it.
* **Optional:** `mlxreg` (MFT, proprietary) or `mstreg` (mstflint, open source) for the module-free path and
  `--cross-check`; XRT's `/opt/xilinx/xrt/bin/xbflash.qspi` for burning from a running User image.

## Install

### Debian/Ubuntu packages

Two packages, built from this folder (needs `debhelper`; the `.deb` files land in the parent folder):

```
dpkg-buildpackage -us -uc -b
sudo apt install ../innova2-app_*_all.deb ../innova2-areg-dkms_*_all.deb
sudo innova2_app --batch query
```

* **`innova2-app`**: the app, `rawspi.py`, `innova2_areg.sh` and `innova2-smi` in `/usr/lib/innova2-app`, with the
  commands `innova2_app`, `innova2_areg` and `innova2-smi` in `/usr/bin`.
* **`innova2-areg-dkms`**: the kernel module. Installing it registers the module with DKMS, builds it for the
  running kernel and loads it, and it is loaded at boot from then on. It needs `dkms` and the running kernel's
  headers. Leave it out (`innova2-app` only recommends it) if you only need the `mlxreg` path, or on a host that still
  has the vendor `mlx5_fpga_tools` module: both modules create the same device node.

`sudo apt remove innova2-app innova2-areg-dkms` removes both, including the DKMS registration.

If you used `install.sh` before, remove that install first with `sudo ./install.sh --uninstall`. Once the packages
are installed, `install.sh` leaves the kernel module alone: the package manages it.

### Without packages

```
sudo ./install.sh              # or: sudo ./install.sh --no-kmod   (no kernel module: query/CR only, through mlxreg/mstreg)
sudo innova2_app --batch query
```

`sudo ./install.sh --uninstall` removes everything, including the DKMS module.

## Use

Run `sudo innova2_app` with no arguments for the interactive menus, the same as the vendor app. The menu shown
depends on which FPGA image is running (User, Factory or Flex). The ConnectX-5 is found automatically; use
`-d <bdf>` if there is more than one. `-h` lists all options. The vendor log goes to
`/var/log/innova2_flex_app.log`, as the vendor app's does.

Batch commands (non-interactive):

```
sudo innova2_app --batch query                 # admin/oper image, ConnectX status, JTAG grant
sudo innova2_app --batch health                # identity, temperature, fan, power (Flex/Factory image)
sudo innova2_app --batch image-sel user        # select the User image; takes effect on the next COLD cycle
sudo innova2_app --batch jtag-on | jtag-off    # grant/revoke JTAG access
sudo innova2_app --batch crrd <hexaddr>        # CR-space read (not while the User image runs, see below)
```

### Burning a User image

There are two ways to write a new User image into flash. Which one to use depends on which image the card is
running at the time.

* **From the Flex image** (the vendor's way). The Flex image exposes a burn endpoint (BOPE, `15b3:0264`). Pass
  per-chip `.bin` files and use menu **6 "Burn of customer User image"**, then **7**. This does the vendor's two
  passes and offset checks, followed by a sampled read-back through `rawspi.py`.
  ```
  sudo innova2_app -b user_primary.bin,0 -b user_secondary.bin,1
  ```
* **From a running User image** (`burn-inshell`, "burn in shell"). This replaces one User image with another without
  first switching the card to the Flex image and cold-cycling. It needs the running User image (the "shell") to
  include a flash controller that reaches both flash chips: an AXI Quad SPI at BAR0 + 0x40000 of its management PF,
  as in the innova2 XDMA shell.
  ```
  sudo innova2_app --batch burn-inshell <path>/user_<tag>        # expects <tag>_primary.mcs + _secondary.mcs
  ```
  It refuses to write unless both flash chips answer with a Micron ID (`rawspi.py rdid`), and unless the MCS is a
  User-slot image. The write itself is XRT's stock `xbflash.qspi`.
  The vendor app refuses to burn while a User image runs, so this path exists only as a batch command.

Either way, then select the User image and **cold** cycle the host. The FPGA reads flash only at power-on.

## innova2-smi — query and test the XRT shell

`innova2-smi` is for the Innova-2 **XRT shell** (the fp8c lineage: E6b, E6b-2400, E6b-2400dw8, E7a2, E7a3,
E7a3-2666ca48). It finds every card from its ConnectX-5, works out what the FPGA is running, and reads the shell's
health registers (the `ddr_health` block at management BAR0 + 0x36000, SYSMON, XRT's sysfs). Run it as root.

**macOS (1.3.17, read-only).** `innova2-smi` also runs on macOS against the XRT macOS port's DriverKit extension
(`org.xrtmac.XrtUser`). Build the register helper against the port's ABI header and install for the current user:
`macos/build.sh [<port>/src/runtime_src/core/pcie/driver/macos/XrtUser]` then `macos/install.sh`
(-> `~/.local/bin/innova2-smi`). It is picked automatically on macOS (`innova2_smi_macos.MacHost`): PCI topology, IDs and
link state from the IOKit device tree (`ioreg`), shell registers and PCI configuration space through `innova2_regio`
(holds the dext's BAR mappings), the feature ROM through the user function. Works: `list`, `status`, `ddr`, `memory`
(memory topology and xclbin UUID from the dext's kept sections), `counters`, `check`, `regmap`, `ddr-assess --from-logs`.
**Read-only:** the dext maps BARs read-only, so commands that write registers (`recorder`, `ddr-mode`, `clear`,
`throttle`, `test`) are refused with that reason; `status` shows the flight recorder as not available. Not available on
macOS: ConnectX-5 access (no mlx5 driver), PCIe AER counters, VPD, kernel clock frequencies (the dext learns the counters from a platform package); the
boot-xsabin check does not apply (the port identifies these shells by their feature ROM).

**Output convention (owner, 2026-10-08; 1.3.14):** every read-only report run **without parameters prints a short
summary for every card** in the host -- `status`, `ddr`, `memory`, `counters`, `recorder`, and (as before) `list`,
`check`, `ecc-baseline`. Details come with `-v`; `-d CARD` narrows to one card. A read-only report never refuses
with "several cards found". Actions (`ddr-mode`, `clear`, `throttle`, `test`, flashing) still need `-d` when more
than one card is present. `--json` always carries the full objects: one object for one card, a list for several.

| command | what |
|---|---|
| `innova2-smi list` | every card: ConnectX-5 BDFs and serial, FPGA functions, FPGA state, the image the ConnectX reports, shell (VBNV, ROM_TS, OBS_ID, build), XRT Device Ready |
| `innova2-smi status [-d CARD] [-v]` | every card: health (= `check`), DDR verdict, temperature, PCIe, uptime; `-v` the full page: identity and board check, clocks, DDR mode/calibration/ECC (first-fail decoded), link-downs, fenced accesses, every temperature source (LIVE / STALE / UNRELIABLE), voltages, throttle, PCIe, PR count, uptime, P2P / PEER windows / PLRAM alias, the last flight-recorder records |
| `innova2-smi check [-d CARD\|--all]` | passive, concerns only: one line per WARN/FAIL with evidence and a hint, or `OK: no concerns`. Exit 0 OK, 1 WARN, 2 FAIL (for cron). `--interval S` (default 1) takes two snapshots for rates; `--since-boot` takes one; `-v` adds the known-benign INFO items; `--rules` prints the rules table; `--recorder` (not strictly passive: stores to the flight-recorder index) places fenced DDR accesses in link-down windows when the registers cannot (several link-downs per warm reboot). `status --concerns` is an alias. |
| `innova2-smi ecc-baseline [-d CARD] [--show\|--remove]` | records each card's ECC counts (CE, UE polls), link-down count and FPGA uptime in `~/.cache/innova2-smi/ecc_baseline_<mgmt BDF>.json` (host-side; the card is only read). From then on `check` judges only the errors **since** the baseline -- a new UE is a FAIL again, a new CE a WARN -- and shows the totals as INFO. Used only for the same card, shell and configuration: a cold boot or reflash voids it (reported as INFO, totals judged again). See "ECC after a warm reboot" below |
| `innova2-smi ddr [-d CARD] [-v] [--window S]` | every card: the DDR verdict and its reason; `-v`: DDR calibration and mode, the build's MIG parameters (a per-build table keyed by ROM_TS, from the build reports: no shell reports its MIG configuration; listed in `docs/innova2_smi_regmap.md`), the MIG ui_clk **measured** passively from the ECC poller's poll count against the 250 MHz uptime counter (ui_clk = POLL_CYC x polls / time; on route V the POLL_CYC and reference of the LOADED xclbin's MIG variant, from DDR_INFO; MT/s = 8 x ui_clk at the 4:1 PHY; precision about 2/polls + 0.08 %, i.e. +-0.2 % over the default 1.5 s), and the ECC totals. `status` has a one-line summary; `check` WARNs (`ddr.rate_mismatch`) when the measured rate is more than 1 % (plus the precision) off the table. Shells without ddr_health: "not available on this shell" |
| `innova2-smi ddr-assess --partials DIR --yes [--write FILE]` / `--from-logs DIR` | on a card booted into the DDR tester image (PCI 10ee:9038): loads each candidate DDR configuration (lanes, speed, CA drive) as a partial bitstream, judges it by full self-checked read/write passes (never by calibration), reloads a clean one and re-tests it; settles on the widest clean configuration, then the fastest, then the CA drive, among those a route-V per-card DDR profile can use, and prints (`--write`: saves) the profile file plus the link-config line that carries it: `[advanced] param=compiler.userPreSysLinkOverlayTcl=<profile>`. `--from-logs` settles from existing tester campaign logs (reloads then unconfirmed) |
| `innova2-smi memory [-d CARD] [-v]` | every card: the loaded xclbin, used and unused banks; `-v`: every memory bank of the loaded xclbin (bank0/DDR, PLRAM0..2, HOST, PEER): tag, type, base, size, `used`, the CU arguments on it, whether it is ready (DDR calibrated, RP released, host memory reserved, P2P on) and its prune state. PRUNED is shown only where it can be verified (route V's DDR, from the shell's RV_INFO); an unused PLRAM/HOST/PEER bank is `unused (may be pruned)`, because the prune hooks put no marker in the xclbin (see `docs/innova2_smi_prune_marker.md`). Passive: xocl's sysfs copies of mem_topology / ip_layout / connectivity, no XRT context. Also part of `status`; `check` FAILs a used bank that is not ready |
| `innova2-smi counters [-d CARD] [-v] [--watch N]` | every card: ECC, link-downs/fences, PCIe errors, PR, throttle, lock losses; `-v`: every counter and register the running shell exposes, from its OBS_ID map, plus SYSMON, XRT's clock wizards and frequency counters, the cmc sensors, the PEER/alias counters and AER; `--watch` shows deltas |
| `innova2-smi recorder [-d CARD] [-n N] [-v]` | the flight recorder, decoded (E7a2 and later): every card's last 5 records with 'now' and how long ago each happened; `-n N` the last N, `-v` all |
| `innova2-smi clear` / `ddr-mode [on\|off\|emu\|hybrid]` | controls, with confirmation (or `--yes`) |
| `innova2-smi throttle ...` | show, `force 2\|4`, `off`, `enable`, `gate`, `thresholds`, `thresholds2`, `tierlevel`, `failsafe`, `defaults`, `simulate-hot tier1\|tier2`, `restore` |
| `innova2-smi test NAME\|all` | active tests (below) |
| `innova2-smi regmap` | the register maps; `--markdown`, `--check-rtl ddr_health.v` |
| `innova2-smi setup [-v]` | read-only: XRT, `innova2_areg`, the shells installed on this host (every image checked against its sha256) and each card's state, with the command that fixes each gap and the `flash` command to run next. Installs nothing. Exit 0 when flashing can go ahead |
| `innova2-smi shells [-v]` | every shell this tool knows (the host's manifests, then the built-in released shells): package and version, ROM_TS, state -- `flashable` (installed, every image matches its sha256), `partial` (files copied by hand, no package), `damaged`, `not installed` -- the cards running it, and the `flash --shell` or install command. Read-only; exit 0 when one is flashable |
| `innova2-smi flash [-d CARD] [--shell NAME] [--dry-run]` | writes an installed shell into the card's User slot through the tool that already does it, schedules the User image, then asks you to cold cycle (below) |

`-d` takes a card index, any BDF on the card (ConnectX or FPGA, `08:00` is enough) or the board serial. Every query
takes `--json`. `innova2-smi <command> -h` documents each command.

**Flashing a card (1.3.25).** The shortest path from a new host to a card running a shell:
```
sudo innova2-smi setup          # lists what is missing, with the apt/wget line for each; installs nothing
sudo innova2-smi shells         # which shells can be flashed (and which cards run them)
sudo innova2-smi flash          # -d CARD with several cards, --shell NAME with several shells installed
# cold cycle (power off, then on)
sudo innova2-smi check
```
`flash` writes nothing itself and only ever the User slot. On a card running an innova2 XRT shell it runs the steps
of the shell package's own flash tool (`innova2_flash_user`, `innova2_noddr_flash_user`): both flash chips probed with
the package's `xbflash.probe`, the Factory region scanned, stock `xbflash.qspi` writes, Factory scanned again and
compared block by block. The Factory check is sampled by default: 66 of the 512 64 KB blocks (every 8th per chip,
the last, and the one holding XRT's default bitstream guard at 0x00801000), about 10 s per scan. The ways a User-slot
write can land in Factory damage it contiguously (a dropped address bit wraps the image onto Factory's start, a wrong
MCS base shifts it), so every misplaced write of 512 KB or more hits a sampled block; `--full-factory-check` reads all
512 (1-2 min per scan), `--skip-factory-check` none. These scans read through `/dev/xfpga`, which can misread; they
catch a misplaced write, they do not prove Factory is good (verify that with rawspi). It runs the steps itself so the
long ones (the scans, a few minutes for the write) show progress -- one dot per 64 KB block or xbflash beat, `x` for a block that fails (short read, or changed
after writing), and done/total, elapsed and ETA at the end of each row; `--batch` draws none of it. The log, both
scans and the tools' output are kept in `/var/tmp/innova2_flash/smi_<card>_<time>/`. On a card running the Flex image it runs `innova2_app -b <chip0>.bin,0 -b <chip1>.bin,1 --batch
burn` (sampled read-back); any other state is refused with the image-select step that gets it to the Flex image. It
then schedules the User image through the ConnectX (needs `innova2_areg`) when it is not already, and checks that the
ConnectX reports it. `--dry-run` prints the plan and the commands. Shells are found from the manifest a shell package
ships (`/opt/xilinx/dsa/<vbnv>/innova2_flash.json`, format in `innova2-smi setup -h`) or the built-in table of released
shells (noddr r1 202320.3.0, inn2x r10 202320.4.0). XRT is not installed for you: it is a patched DKMS build per
host, so `setup` points at the shell README's Requirements.

**Safety.** Read-only unless you give a control command, and every state change asks first (or needs `--yes`).
* An undecoded management-PF read hard-locks these hosts. So the tool identifies the shell step by step: the
  management function, its BAR0 and memory decoding, `xclmgmt` bound, the feature ROM in sysfs, the platform name,
  then OBS_ID (0x36030), the first register it reads. From then on every access is checked against that OBS_ID's
  register map (`docs/innova2_smi_regmap.md`), and an OBS_ID it does not know stops all further access.
* Register accesses are single ctypes 32-bit loads/stores (a python mmap slice issues them twice), and only the
  few 4 KB windows in use are mapped.
* No command writes flash; `test flash-verify` only reads it. Nothing unloads `xocl`.
* Before any xclbin is loaded, its feature-ROM timestamp and platform name are compared with the running shell's.
  `test validate` makes the shared validate dir (`/opt/xilinx/dsa/<VBNV>/test`, one per platform name, whatever
  shell is flashed) point at this shell's kernels when it is a symlink with a matching sibling (`test.<tag>`), and
  points it back afterwards.
* `check` is strictly passive: loads only (not even the recorder's index register), sysfs, the xrt-smi device
  table. It never opens an XRT context, so it can run while kernels and DMA run.

**ECC after a warm reboot.** The shell's ECC counters are cumulative since configuration and cannot be cleared
from the host. A warm reboot drops the PCIe link, and the link-down MIG reset loses the DDR contents; reading a
buffer that was filled before the reboot (a stale-data read) then gives UEs and CEs that stay counted until a cold
boot. `check` therefore judges UEs against a *clean point* -- the configuration, or an `ecc-baseline` taken later:

| UEs since the clean point | check |
|---|---|
| no link-down since the clean point | FAIL `ddr.ue` (DDR content has not been reset since: not stale data) |
| link-down(s) since, the last UE after the last link-down (E7 event timestamps) | WARN `ddr.ue_stale`: likely stale-data reads after warm reboot; re-fill and `ecc-baseline`, or cold-boot to clear |
| the last UE before the only link-down since | FAIL `ddr.ue` (no MIG reset preceded them) |
| the last UE before the last of several link-downs, or a shell without event timestamps (E6b layout) | WARN `ddr.ue_unclassified`: the shell cannot tell stale from fresh; `innova2-smi recorder` shows each ECC_UE / LINK_DOWN record |
| none since an `ecc-baseline` | INFO `ddr.ue_baselined` (the totals) |

The shell records no host fills, so a stale-data verdict is never better than WARN. The usual sequence after a warm
reboot that read stale data: re-fill the buffers, `innova2-smi ecc-baseline`, and from then on `check` FAILs on any
new UE.

**Interface uuid.** `status` shows the interface uuid read passively from xocl's sysfs `interface_uuids` (xclmgmt's
as a fallback) -- a print of the device tree the driver already holds, no XRT context. XRT 2.17 answers EINVAL there
for these shells (their device tree has no interface_uuid property), so the next source is the header
(m_interface_uuid) of the boot xsabin xclmgmt loaded for the card's ROM_TS. The per-build table is only a
cross-check; a disagreement is a WARN (`identity.uuid_mismatch`). With no source it shows the table value and says so.

**Card states.** `OUR_SHELL` (full status), `OUR_SHELL_OLD` (a pre-fp8c shell: identity only),
`OUR_SHELL_UNREADABLE` (a shell device whose BAR, driver, ROM or OBS_ID check failed; the failing step is
reported and nothing past it is read), `FLEX` (the Flex/Factory image), `FPGA_MISSING` (ConnectX-5 present, no FPGA
function), `OTHER_XILINX` (e.g. the DDR tester 10ee:9038). The ConnectX's own view of the FPGA image is shown too
when `mlx5_fpga_tools` or `innova2_areg` (or MFT) is available; a disagreement is reported as a FAIL.

**Tests.** Tests that load xclbins or use DDR ask first or need `--yes`.

| test | does | needs |
|---|---|---|
| `validate` | `xrt-smi validate` with this shell's kernels in the validate dir; ECC/fence deltas | XRT |
| `p2p` | `xrt-smi validate -r p2p` | P2P enabled |
| `ddr-soak` | fill bank 0 (address-derived pattern) and read it back `--passes` times, ECC per pass | pyxrt, numpy |
| `temp-live` | every temperature source live and in agreement (read-only) | — |
| `ecc-live` | ECC poller alive (read-only); with `--arm` + a warm reboot, proves the counters count on stale DRAM | pyxrt for `--arm` |
| `throttle` | walks FULL → tier 1 → tier 2 → hysteresis → release, FULL → tier 2 directly, per-tier levels, FORCE, with the delivered DATA_CLK measured by a lat_probe kernel each step; restores the settings | `--xclbin` with lat_probe (`regress_plram_prog.xclbin` of the shell build) |
| `pr-stress` | `--loads` xclbin loads (half with the throttle forced on, on E7 shells), Device Ready / firewall / DDR gate / PR count after each | XRT |
| `flash-verify` | reads the User slot of both flash chips through the shell's quad-SPI controller and compares with `--primary/--secondary` (.bin) or `--mcs-prefix`; exact length, any SPI stall fails (re-run) | — |

`test all` runs those whose prerequisites are given. Exit 0 = all PASS/SKIP, 1 = a FAIL, 2 = only INCONCLUSIVE.

**Simulating heat.** `innova2-smi throttle simulate-hot tier1|tier2 [--seconds S] [--xclbin X]` moves the tier
thresholds below the live temperature, so the shell's own state machine enters the tier. It shows the tier, the
delivered clock and the recorder's tier entries, and after S seconds (or Ctrl-C) restores the exact previous values
and confirms the release. The shell has no temperature-override register, so no sensor value is faked. The previous
values are saved to `~/.cache/innova2-smi/` first; `innova2-smi throttle restore` recovers them after a crash.
`throttle force 2|4` and `throttle off` force the throttle directly, bypassing the automatic path.

**Verified on hardware (2026-10-06, x440: card 03:00 E6b, card 08:00 E6b-2400dw8, both OBS_ID 0x0B5E0002, read-only):**
`list`, `status` (text and `--json`), `check` (`--all`, `--since-boot`, `status --concerns`), `counters` (incl.
`--watch`), `test temp-live`, `test ecc-live` (poller check), and the refusals (`recorder` on an E4-layout shell, a
test without `--yes`). On that host `check` found three real items: card 03's slot link runs x1 (riser) with its FPGA
link at x4, its root port had logged ~7,500 correctable AER errors since boot, and card 08's shared validate dir
pointed at E6b's kernels. **Unit tests only (mock register maps for OBS_ID 0x0B5E0002/4/5/6/7):** the E7a2/E7a3
paths (throttle, tiers, simulate-hot, recorder), the controls, the xclbin-loading tests (`validate`, `p2p`,
`ddr-soak`, `ecc-live --arm`, `throttle`, `pr-stress`) and `flash-verify`.

innova2-smi takes XRT's Device Ready from sysfs, not from `xrt-smi examine`: on x440 one `examine` added ~400
correctable AER errors on card 03's marginal link, while a whole `innova2-smi status` or `check` adds about ten
(the idle rate there is ~20 a minute).

## Behaviour to know about

* **CR space is refused while the User image runs.** That is the ConnectX's rule, not a bug in the app.
  Identity, temperature, fan and DDR health read correctly on the Flex/Factory image. Under the User image,
  read them through XRT (`xbutil examine`).
* **Image select takes effect only on a cold power cycle**, exactly as with the vendor app.
* **`mlxreg` cannot do `FPGA_CTRL` writes** (image select, JTAG grant). Install the kernel module for those.
* **The kernel module is tied to `mlx5_core`'s symbol CRC.** Install it through DKMS (the package and `install.sh` both do), or it
  will silently stop loading after the next kernel or OFED update.

## Tested on

An HP Z440 (kernel 6.8.0-138, DOCA-OFED 3.5.0, XRT 2.19): menus, query, image select, the JTAG grant,
and both burn paths, each followed by a cold boot into the burned image (2026-09-27). Earlier versions were
exercised on two other hosts, one with kernel 5.8 and OFED 5.2.

## Acknowledgements

Thanks to [mwrnd](https://github.com/mwrnd/) for the Innova-2 setup and usage notes
([innova2_flex_xcku15p_notes](https://github.com/mwrnd/innova2_flex_xcku15p_notes)) and related Innova-2 projects,
which document the card, its flash layout and the MLNX_OFED 5.2 dependency in detail.

## Provenance and licences

The register addresses, bit fields, temperature and fan formulas, the power-level ladder and the menu structure
are transcribed from the vendor sources (`Innova_2_Flex_Open_18_12`, `app/fpga_access.c`, `app/interactive.c`).

* Everything else here: Apache-2.0, © 2026 the innova2 contributors. Files derived from the Mellanox sources are
  `Apache-2.0 AND Linux-OpenIB` (Mellanox's OpenIB.org BSD option).
* `innova2_areg_kmod/`: GPL-2.0-only.
* Per-file licence information follows the REUSE specification (`LICENSES/`, `REUSE.toml`); third-party notices are in `NOTICE`.
