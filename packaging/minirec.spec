%{!?minirec_release:%global minirec_release 1}

Name:           minirec
Version:        0.1.23
Release:        %{minirec_release}%{?dist}
Summary:        Accessible GTK 4 audio recorder

License:        GPL-3.0-or-later
URL:            https://github.com/vlcekapps/minirec-linux
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  appstream
BuildRequires:  desktop-file-utils
BuildRequires:  gstreamer1
BuildRequires:  gstreamer1-plugins-base
BuildRequires:  gstreamer1-plugins-good
BuildRequires:  gtk4 >= 4.20
BuildRequires:  libadwaita >= 1.7
BuildRequires:  meson >= 1.2.0
BuildRequires:  pipewire-gstreamer
BuildRequires:  python3-devel >= 3.11
BuildRequires:  python3-gobject >= 3.48

Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       gtk4 >= 4.20
Requires:       hicolor-icon-theme
Requires:       libadwaita >= 1.7
Requires:       pipewire-gstreamer
Requires:       python3 >= 3.11
Requires:       python3-gobject >= 3.48

%description
MiniRec is a native, accessible GTK 4 application for recording, managing,
and playing Ogg/Opus, MP3, and WAV audio on Fedora Linux.

%prep
%autosetup -n %{name}-%{version}

%build
%meson
%meson_build

%install
%meson_install

%check
%{__python3} -m unittest discover -s tests -v
desktop-file-validate %{_vpath_builddir}/cz.pvlcek.minirec.desktop
%{__python3} tools/validate_appstream_catalog.py

%files
%{_bindir}/minirec
%{python3_sitelib}/minirec/
%{_datadir}/applications/cz.pvlcek.minirec.desktop
%{_datadir}/icons/hicolor/scalable/apps/cz.pvlcek.minirec.svg
%{_datadir}/metainfo/cz.pvlcek.minirec.metainfo.xml
%{_datadir}/swcatalog/xml/minirec.xml
%license %{_datadir}/licenses/minirec-linux/LICENSE
%doc %{_datadir}/doc/minirec-linux/README.md
%doc %{_datadir}/doc/minirec-linux/REPOSITORY_NOTICE.md
%doc %{_datadir}/doc/minirec-linux/android-parity.md
%doc %{_datadir}/doc/minirec-linux/desktop-integration.md

%changelog
* Mon Aug 03 2026 Pavel Vlček <19784140+pavelvlcek@users.noreply.github.com> - 0.1.23-1
- Show recording date, duration, size, and explicit format in the library
- Complete the Linux port documentation and source handoff metadata
- Relicense the public Linux source as GPL-3.0-or-later

* Mon Aug 03 2026 Pavel Vlček <19784140+pavelvlcek@users.noreply.github.com> - 0.1.22-1
- Keep the GNOME launcher valid while RPM replaces /usr/bin/minirec
- Add a regression contract for the stable desktop Exec program

* Mon Aug 03 2026 Pavel Vlček <19784140+pavelvlcek@users.noreply.github.com> - 0.1.21-2
- Prevent settings dropdown activation from rebuilding its live GTK model
- Defer and coalesce settings UI updates, including language changes

* Mon Aug 03 2026 Pavel Vlček <19784140+pavelvlcek@users.noreply.github.com> - 0.1.21-1
- Port MiniRec 0.1.21 to an accessible native GTK 4 application
- Add Ogg/Opus, MP3 and WAV recording, safe recovery and internal playback
- Add Czech and English interfaces, Fedora desktop metadata and RPM packaging
