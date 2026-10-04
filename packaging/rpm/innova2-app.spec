# SPDX-FileCopyrightText: 2026 the innova2 contributors
#
# SPDX-License-Identifier: Apache-2.0

# PLACEHOLDER -- NOT YET TESTED ON AN RPM-BASED HOST WITH THE CARD.
#
# Mirrors the Debian packaging (debian/): the same two packages, the same file layout and the same DKMS
# handling. Build from a release tarball:
#   rpmbuild -ba --define "_sourcedir $PWD" packaging/rpm/innova2-app.spec
# Open questions before publishing RPMs: does the module Makefile find the right Module.symvers on
# Fedora/RHEL kernels and their OFED builds, and are the kernel-devel dependencies right?

%global dkms_name innova2-areg
# Must match PACKAGE_VERSION in innova2_areg_kmod/dkms.conf.
%global dkms_ver  1.0

Name:           innova2-app
Version:        1.2.0
Release:        0.1%{?dist}
Summary:        Management app for the Mellanox Innova-2 Flex FPGA card
License:        Apache-2.0 AND Linux-OpenIB AND GPL-2.0-only
URL:            https://github.com/michalkouril/innova2_py_app
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/innova2_py_app-%{version}.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.8
Recommends:     innova2-areg-dkms
Suggests:       mstflint

%description
innova2_app does what Mellanox's innova2_flex_app 18.07.00 did, with the same menus and options: query and
select the FPGA image, grant JTAG access, read identity, temperature, fan and power, run the DDR and PCI tests,
and burn a User image into flash. It works on current kernels and MLNX_OFED/DOCA-OFED releases, which no longer
ship mlx5_fpga_tools.

%package -n innova2-areg-dkms
Summary:        innova2_areg kernel module (DKMS) for the Innova-2 Flex card
License:        GPL-2.0-only
Requires:       dkms
Requires:       kernel-devel

%description -n innova2-areg-dkms
Re-creates the /dev/<bdf>_mlx5_fpga_tools device node, with the vendor's ioctls, on top of the kernel's exported
mlx5_core_access_reg(). innova2_app needs it for image select and the JTAG grant on kernels and OFED releases
without the vendor mlx5_fpga_tools module (MLNX_OFED 5.3 and later). Do not install it on a host that still has
the vendor module.

%prep
%autosetup -n innova2_py_app-%{version}
grep -qx 'PACKAGE_VERSION="%{dkms_ver}"' innova2_areg_kmod/dkms.conf

%build
# Nothing to build: the app is Python, and DKMS builds the module on the target host.

%install
install -d %{buildroot}%{_prefix}/lib/innova2-app %{buildroot}%{_bindir} %{buildroot}%{_localstatedir}/lib/innova2-app
install -m 755 innova2_app.py rawspi.py innova2_areg.sh %{buildroot}%{_prefix}/lib/innova2-app/
ln -s ../lib/innova2-app/innova2_app.py %{buildroot}%{_bindir}/innova2_app
ln -s ../lib/innova2-app/innova2_areg.sh %{buildroot}%{_bindir}/innova2_areg
install -d %{buildroot}%{_usrsrc}/%{dkms_name}-%{dkms_ver} %{buildroot}%{_prefix}/lib/modules-load.d
install -m 644 innova2_areg_kmod/innova2_areg.c innova2_areg_kmod/Makefile innova2_areg_kmod/dkms.conf \
    %{buildroot}%{_usrsrc}/%{dkms_name}-%{dkms_ver}/
install -m 644 debian/innova2_areg.conf %{buildroot}%{_prefix}/lib/modules-load.d/innova2_areg.conf

%post -n innova2-areg-dkms
dkms remove -m %{dkms_name} -v %{dkms_ver} --all >/dev/null 2>&1 || :
dkms add -m %{dkms_name} -v %{dkms_ver} || :
for b in /lib/modules/*/build; do
    k=$(basename "$(dirname "$b")")
    [ -d "$b" ] && [ "$k" != "$(uname -r)" ] || continue
    dkms install -m %{dkms_name} -v %{dkms_ver} -k "$k" >/dev/null 2>&1 \
        || echo "note: innova2_areg did not build for $k (not the running kernel; skipped)" >&2
done
if ! dkms install -m %{dkms_name} -v %{dkms_ver} -k "$(uname -r)"; then
    echo "*** innova2_areg did not build for $(uname -r). Install kernel-devel for it, then run:" >&2
    echo "***   sudo dkms install -m %{dkms_name} -v %{dkms_ver}" >&2
elif ! { ls /dev/*_mlx5_fpga_tools >/dev/null 2>&1 && ! grep -q '^innova2_areg ' /proc/modules; }; then
    modprobe -r innova2_areg 2>/dev/null || :
    modprobe innova2_areg || echo "*** modprobe innova2_areg failed -- see dmesg" >&2
fi

%preun -n innova2-areg-dkms
# $1 == 0: erase, not upgrade (on upgrade the new package's %%post has already re-registered the module).
if [ "$1" -eq 0 ]; then
    modprobe -r innova2_areg 2>/dev/null || :
    dkms remove -m %{dkms_name} -v %{dkms_ver} --all >/dev/null 2>&1 || :
fi

%files
%license LICENSE NOTICE LICENSES/Linux-OpenIB.txt
%doc README.md
%{_prefix}/lib/innova2-app/
%{_bindir}/innova2_app
%{_bindir}/innova2_areg
%dir %{_localstatedir}/lib/innova2-app

%files -n innova2-areg-dkms
%license LICENSES/GPL-2.0-only.txt
%doc README.md
%{_usrsrc}/%{dkms_name}-%{dkms_ver}/
%{_prefix}/lib/modules-load.d/innova2_areg.conf

%changelog
* Sun Oct 04 2026 Michal Kouril <xmkouril@gmail.com> - 1.2.0-0.1
- Placeholder spec mirroring the Debian packaging; untested.
