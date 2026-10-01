# Licence audit — innova2_py_app (2026-09-28, revised 2026-10-01)

Every file in this repository, classified by provenance. Classes and what they mean are at the end.

**Provenance notes:** Register addresses, bit fields and formulas were transcribed from Mellanox's Innova_2_Flex_Open_18_12 app (dual GPL-2.0 / OpenIB BSD); the raw FPGA register field positions used with mlxreg from include/linux/mlx5/mlx5_ifc_fpga.h (Mellanox, same dual licence). No third-party code or binaries are included.

| file | class | licence evidence in the file |
|---|---|---|
| `LICENSE` | Licence text | Apache License; Apache License to your work, attac; Apache License to your work.; Copyrigh |
| `LICENSES/Apache-2.0.txt` | Licence text | Apache License; Apache License to your work, attac; Apache License to your work.; Copyrigh |
| `LICENSES/GPL-2.0-only.txt` | Licence text | Copyright (C) 1989, 1991 Free Software Foundation, Inc.; GNU GENERAL PUBLIC LICENSE; GNU G |
| `LICENSES/Linux-OpenIB.txt` | Licence text | COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER; copyright notice,; copyright  |
| `LICENSE_AUDIT.md` | Ours | Apache License  /; Apache License;; Apache License to your work.; Copy; Apache License; Ap |
| `NOTICE` | Ours | BSD licence (LICENSES/Linux-OpenI; Copyright 2026 the innova2 contributors. Licensed under |
| `README.md` | Ours | GPL-2.0 |
| `REUSE.toml` | Ours | CopyrightText = ["2026 the innova2 contributors"]; CopyrightText = ["Mellanox Technologies |
| `SHA256SUMS` | Ours | GPL-2.0 |
| `VERSION` | Ours | — |
| `innova2_app.py` | Derived from Mellanox (GPL-2.0 / OpenIB BSD) | CopyrightText: 2026 the innova2 contributors; CopyrightText: Mellanox Technologies Ltd.; S |
| `innova2_areg.sh` | Derived from Mellanox (GPL-2.0 / OpenIB BSD) | CopyrightText: 2026 the innova2 contributors; CopyrightText: Mellanox Technologies Ltd.; S |
| `innova2_areg_kmod/Makefile` | Ours, GPL-2.0 | CopyrightText: 2026 the innova2 contributors; SPDX-License-Identifier: GPL-2.0-only |
| `innova2_areg_kmod/build.sh` | Ours, GPL-2.0 | CopyrightText: 2026 the innova2 contributors; SPDX-License-Identifier: GPL-2.0-only |
| `innova2_areg_kmod/dkms.conf` | Ours, GPL-2.0 | CopyrightText: 2026 the innova2 contributors; SPDX-License-Identifier: GPL-2.0-only |
| `innova2_areg_kmod/innova2_areg.c` | Ours, GPL-2.0 | CopyrightText: 2026 the innova2 contributors; SPDX-License-Identifier: GPL-2.0-only |
| `install.sh` | Ours | CopyrightText: 2026 the innova2 contributors; SPDX-License-Identifier: Apache-2.0 |
| `rawspi.py` | Ours | CopyrightText: 2026 the innova2 contributors; SPDX-License-Identifier: Apache-2.0 |

## Classes

* **Licence text** (4 files): Verbatim licence text (SPDX licence list, or our LicenseRef explanation). Not a work of ours to license.
* **Ours** (8 files): Written in this project. Apache-2.0, (c) 2026 the innova2 contributors (SPDX header or REUSE.toml).
* **Derived from Mellanox (GPL-2.0 / OpenIB BSD)** (2 files): Ours, but register maps / protocol / ioctl numbers transcribed from Mellanox sources that are dual GPL-2.0 or OpenIB BSD. Ship the BSD notice + attribution.
* **Ours, GPL-2.0** (4 files): Written in this project; GPL-2.0 because it is a Linux kernel module (SPDX tag in file).
