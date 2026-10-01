# innova2_app — Innova-2 management app for current Mellanox/NVIDIA drivers

Version 1.1.0 (2026-10-01).

`innova2_app` is a work-alike of Mellanox's `innova2_flex_app` 18.07.00, the tool that queries and manages the
Xilinx FPGA on a Mellanox Innova-2 Flex card: image select, JTAG access, identity, temperature, fan, DDR
health, flash burning. The vendor binary no longer runs on current software. It needs the `mlx5_fpga_tools`
kernel module, which only OFED 5.2-era packages ship; OFED 23.10 and every DOCA-OFED dropped it.

This app does the same work through the ConnectX-5 access registers underneath (`FPGA_CAP` 0x4022,
`FPGA_CTRL` 0x4023, `FPGA_ACCESS_REG` 0x4024). It uses whichever transport the host offers:

| transport | what provides it | what works |
|---|---|---|
| **node** | `/dev/<bdf>_mlx5_fpga_tools`, from the vendor module (OFED 5.2) **or** from the `innova2_areg` module in this package (any current kernel/OFED) | everything |
| **mlxreg** | MFT's `mlxreg`, or mstflint's open-source `mstreg` (same flags; not yet tested on the card). The FPGA registers are sent raw, by ID and length, so no register database is needed. No kernel module needed. | query, capabilities, CR-space reads and writes. **Not** image select or the JTAG grant: the firmware rejects `FPGA_CTRL` writes on this path (`ME_ICMD_OPERATIONAL_ERROR`). |

The app reports which transport it chose and leaves out menu items the transport cannot perform. It never starts an
MFT/mstflint tool unless you ask: `--transport mlxreg`, or `--cross-check`, which re-reads each image-select/JTAG
write through `mlxreg`/`mstreg` as an independent confirmation.

## Contents

| file | what |
|---|---|
| `innova2_app.py` | the app. Vendor menus and options, plus `[ext]` batch commands (`--batch …`, `--transport`, `--yes`). |
| `rawspi.py` | direct access to both flash chips. The app uses it to check that both chips answer before `burn-inshell` writes, and to verify a burn: first and last 4 KB plus 14 spread blocks. |
| `innova2_areg_kmod/` | `innova2_areg.ko` source (GPL-2.0). It re-creates the vendor chardev, with the same ioctls, on top of the kernel's exported `mlx5_core_access_reg()`. `build.sh install` registers it with DKMS so it rebuilds on kernel/OFED updates, and loads it at boot. |
| `innova2_areg.sh` | query, capabilities and CR reads/writes as a shell script over `mlxreg`/`mstreg`, for hosts without Python. Image select and the JTAG grant are refused there (the firmware rejects them on that path); use `innova2_app`. |
| `install.sh` | copies everything to `/opt/innova2_app`, links the commands into `/usr/local/bin`, and installs the module. |

## Requirements

* Linux, root, Python 3.8+ (standard library only). No Mellanox/NVIDIA user-space tools are needed.
* A working `mlx5_core` for the card's ConnectX-5. The in-box kernel driver is enough; MLNX_OFED/DOCA-OFED also work
  (tested there; an in-box-only host is not yet tested).
* **For image select and the JTAG grant:** the `innova2_areg` module, which needs `dkms` and the kernel headers.
  Hosts that still have the vendor `mlx5_fpga_tools` node don't need it.
* **Optional:** `mlxreg` (MFT, proprietary) or `mstreg` (mstflint, open source) for the module-free transport and
  `--cross-check`; XRT's `/opt/xilinx/xrt/bin/xbflash.qspi` for `burn-inshell`.

## Install

```
sudo ./install.sh              # or: sudo ./install.sh --no-kmod   (query/CR only, through mlxreg/mstreg)
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

* **Card running the Flex image** (vendor path, BOPE endpoint `15b3:0264`): pass per-chip `.bin` files and use
  menu **6 "Burn of customer User image"**, then **7**. This does the vendor's two passes and offset checks,
  followed by a sampled read-back through `rawspi.py`.
  ```
  sudo innova2_app -b user_primary.bin,0 -b user_secondary.bin,1
  ```
* **Card running a User image whose flash controller reaches both chips** (an AXI Quad SPI at BAR0 + 0x40000 of its
  management PF, as in the innova2 XDMA shell):
  ```
  sudo innova2_app --batch burn-inshell <path>/user_<tag>        # expects <tag>_primary.mcs + _secondary.mcs
  ```
  It refuses to write unless both flash chips answer with a Micron ID (`rawspi.py rdid`), and unless the MCS is a
  User-slot image. The write itself is XRT's stock `xbflash.qspi`.
  The vendor app refuses to burn in user mode, so this path exists only as a batch command.

Then select the User image and **cold** cycle the host. The FPGA reads flash only at power-on.

## Behaviour to know about

* **CR space is refused while the User image runs.** That is the ConnectX's rule, not a bug in the app.
  Identity, temperature, fan and DDR health read correctly on the Flex/Factory image. Under the User image,
  read them through XRT (`xbutil examine`).
* **Image select takes effect only on a cold power cycle**, exactly as with the vendor app.
* **`mlxreg` cannot do `FPGA_CTRL` writes** (image select, JTAG grant). Install the module for those.
* **The module is tied to `mlx5_core`'s symbol CRC.** Install it through DKMS (`install.sh` does), or it will
  silently stop loading after the next kernel or OFED update.

## Tested on

An HP Z440 (kernel 6.8.0-138, DOCA-OFED 3.5.0, XRT 2.19): menus, query, image select, the JTAG grant,
and both burn paths, each followed by a cold boot into the burned image (2026-09-27). Earlier versions were
exercised on two other hosts, one with kernel 5.8 and OFED 5.2.

## Provenance and licences

The register addresses, bit fields, temperature and fan formulas, the power-level ladder and the menu structure
are transcribed from the vendor sources (`Innova_2_Flex_Open_18_12`, `app/fpga_access.c`, `app/interactive.c`).

* Everything else here: Apache-2.0, © 2026 the innova2 contributors. Files derived from the Mellanox sources are
  `Apache-2.0 AND Linux-OpenIB` (Mellanox's OpenIB.org BSD option).
* `innova2_areg_kmod/`: GPL-2.0-only.
* Per-file licence information follows the REUSE specification (`LICENSES/`, `REUSE.toml`); third-party notices are in `NOTICE`.
