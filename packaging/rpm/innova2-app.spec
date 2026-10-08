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
Version: 1.3.15
Release:        0.1%{?dist}
Summary:        Management app and shell health tool for the Mellanox Innova-2 Flex FPGA card
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
install -m 755 innova2_app.py rawspi.py innova2_areg.sh innova2_smi.py %{buildroot}%{_prefix}/lib/innova2-app/
install -m 644 innova2_smi_regmap.py innova2_smi_hw.py innova2_smi_check.py innova2_smi_ddrassess.py innova2_smi_tests.py innova2_smi_xrt.py \
    VERSION %{buildroot}%{_prefix}/lib/innova2-app/
ln -s ../lib/innova2-app/innova2_app.py %{buildroot}%{_bindir}/innova2_app
ln -s ../lib/innova2-app/innova2_areg.sh %{buildroot}%{_bindir}/innova2_areg
ln -s ../lib/innova2-app/innova2_smi.py %{buildroot}%{_bindir}/innova2-smi
install -d %{buildroot}%{_usrsrc}/%{dkms_name}-%{dkms_ver} %{buildroot}%{_prefix}/lib/modules-load.d
install -m 644 innova2_areg_kmod/innova2_areg.c innova2_areg_kmod/Makefile innova2_areg_kmod/dkms.conf \
    %{buildroot}%{_usrsrc}/%{dkms_name}-%{dkms_ver}/
install -m 644 debian/innova2_areg.conf %{buildroot}%{_prefix}/lib/modules-load.d/innova2_areg.conf

%check
# innova2-smi unit tests: mock hosts, no hardware
python3 -m unittest discover -s tests

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
%doc README.md docs/innova2_smi_regmap.md docs/innova2_smi_prune_marker.md
%{_prefix}/lib/innova2-app/
%{_bindir}/innova2_app
%{_bindir}/innova2_areg
%{_bindir}/innova2-smi
%dir %{_localstatedir}/lib/innova2-app

%files -n innova2-areg-dkms
%license LICENSES/GPL-2.0-only.txt
%doc README.md
%{_usrsrc}/%{dkms_name}-%{dkms_ver}/
%{_prefix}/lib/modules-load.d/innova2_areg.conf

%changelog
* Thu Oct 08 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.15-0.1
- ddr-assess (per-card DDR profile from the tester image); route-V speed from the loaded xclbin's MIG; untested as an RPM.
* Thu Oct 08 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.14-0.1
- Read-only reports summarise every card by default (-v details, -d one card); check identification fix; untested as an RPM.
* Thu Oct 08 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.13-0.1
- recorder: now and 'ago' per record; ddr: configuration source and configured-part notes; untested as an RPM.
* Thu Oct 08 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.12-0.1
- route V r3: uuid of the rebuilt static; untested as an RPM.
* Thu Oct 08 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.11-0.1
- ddr: plain verdict first (OK / NOT USABLE / NO DDR IN THIS XCLBIN / ...), explained facts, raw registers behind -v; untested as an RPM.
* Wed Oct 07 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.10-0.1
- Error messages fold to the terminal width; untested as an RPM.
* Wed Oct 07 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.9-0.1
- Reports fold to the terminal (INNOVA2_SMI_COLUMNS, 0 = never); status shows the last 10 recorder records; untested as an RPM.
* Wed Oct 07 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.8-0.1
- innova2-smi: route V r3 (OBS_ID 0x0B5E000D, ROM_TS 1792486800): the route-V r2 map on the r3 static; untested as an RPM.
* Wed Oct 07 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.7-0.1
- innova2-smi: fp8c rc1 (OBS_ID 0x0B5E000C, ROM_TS 1792483200): E7a3 map + fence-window counters; plain check classifies the warm-reboot fence from registers; status shows the counters on any fence-window shell; untested as an RPM.
* Wed Oct 07 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.6-0.1
- innova2-smi: route V r2 (OBS_ID 0x0B5E000B): fence-window counters classify the warm-reboot fence from registers; boot-RM wording; PR_COUNT = isolations; untested as an RPM.
* Wed Oct 07 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.5-0.1
- innova2-smi: benign link-down fenced write is INFO; xbutil PASSED WITH WARNINGS counts as a pass; untested as an RPM.
* Tue Oct 06 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.4-0.1
- innova2-smi: live interface uuid, unambiguous throttle line, stale-UE classification and ecc-baseline; untested as an RPM.
* Tue Oct 06 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.2-0.1
- innova2-smi ddr: per-build MIG table and the measured DDR rate; untested as an RPM.
* Tue Oct 06 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.1-0.1
- innova2-smi memory (bank used/ready/PRUNED); empty-VPD boards reported; untested as an RPM.
* Tue Oct 06 2026 Michal Kouril <xmkouril@gmail.com> - 1.3.0-0.1
- innova2-smi: query and test the Innova-2 XRT shell; untested as an RPM.
* Sun Oct 04 2026 Michal Kouril <xmkouril@gmail.com> - 1.2.0-0.1
- Placeholder spec mirroring the Debian packaging; untested.
