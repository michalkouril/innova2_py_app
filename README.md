# innova2_app — Innova-2 management app for current Mellanox/NVIDIA drivers

Version 1.2.0 (2026-10-04).

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

* **`innova2-app`**: the app, `rawspi.py` and `innova2_areg.sh` in `/usr/lib/innova2-app`, with the commands
  `innova2_app` and `innova2_areg` in `/usr/bin`.
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
