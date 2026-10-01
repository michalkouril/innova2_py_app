# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: GPL-2.0-only

obj-m += innova2_areg.o

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build

# mlx5_core_access_reg() lives in mlx5_core. With CONFIG_MODVERSIONS the loader checks the symbol
# CRC, and modpost only knows that CRC if it can see the Module.symvers of the build that EXPORTED
# it. Get this wrong and the module COMPILES CLEANLY and then refuses to load with "disagrees about
# version of symbol" -- so find it here, once, for both build.sh and DKMS.
#
# Order matters: when OFED/DOCA supplies mlx5 its Module.symvers is the right one, and the kernel's
# own (which also lists the symbol, from the in-box driver) would give the WRONG CRC.
SYMVERS ?= $(firstword $(wildcard \
    /usr/src/ofa_kernel/$(shell uname -m)/$(KVER)/Module.symvers \
    /var/lib/dkms/mlnx-ofed-kernel/*/$(KVER)/*/module/Module.symvers \
    /usr/src/ofa_kernel-*/Module.symvers \
    $(KDIR)/Module.symvers))

ifeq ($(strip $(SYMVERS)),)
$(error no Module.symvers found for $(KVER) -- cannot resolve mlx5_core_access_reg's CRC)
endif

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KBUILD_EXTRA_SYMBOLS=$(SYMVERS) modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
symvers:
	@echo $(SYMVERS)
