Name:      rockit-camera-moravian
Version:   %{_version}
Release:   1
Summary:   Control code for a Moravian CMOS camera
Url:       https://github.com/rockit-astro/moravian_camd
License:   GPL-3.0
BuildArch: noarch

%description


%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}
mkdir -p %{buildroot}%{_sysconfdir}/camd
mkdir -p %{buildroot}%{_udevrulesdir}

%{__install} %{_sourcedir}/moravian_camd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/moravian_camd@.service %{buildroot}%{_unitdir}

%{__install} %{_sourcedir}/config/halfmetre.json %{buildroot}%{_sysconfdir}/camd

%package server
Summary:  Moravian camera server
Group:    Unspecified
Requires: python3-rockit-camera-moravian
%description server

%files server
%defattr(0755,root,root,-)
%{_bindir}/moravian_camd
%defattr(0644,root,root,-)
%{_unitdir}/moravian_camd@.service

%package data-halfmetre
Summary: Half metre camera data
Group:   Unspecified
%description data-halfmetre

%files data-halfmetre
%defattr(0644,root,root,-)
%{_sysconfdir}/camd/halfmetre.json

%changelog
