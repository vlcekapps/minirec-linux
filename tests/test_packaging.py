from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree

from minirec import __version__


ROOT = Path(__file__).resolve().parent.parent


def render_desktop_entry(*, python: str, launcher: str) -> str:
    template = (
        ROOT / "data" / "cz.pvlcek.minirec.desktop.in"
    ).read_text(encoding="utf-8")
    return template.replace("@PYTHON@", python).replace(
        "@MINIREC_LAUNCHER@", launcher
    )


def rpm_desktop_entry() -> str:
    return render_desktop_entry(
        python="/usr/bin/python3",
        launcher="/usr/bin/minirec",
    )


class PackagingContractTest(unittest.TestCase):
    def test_release_versions_match(self) -> None:
        meson = (ROOT / "meson.build").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "minirec.spec").read_text(encoding="utf-8")
        metainfo = ElementTree.parse(
            ROOT / "data" / "cz.pvlcek.minirec.metainfo.xml"
        ).getroot()
        meson_version = re.search(r"version:\s*'([^']+)'", meson)
        project_version = re.search(r'(?m)^version = "([^"]+)"$', pyproject)
        spec_version = re.search(r"(?m)^Version:\s*(\S+)$", spec)
        release = metainfo.find("releases/release")
        self.assertIsNotNone(meson_version)
        self.assertIsNotNone(project_version)
        self.assertIsNotNone(spec_version)
        self.assertIsNotNone(release)
        self.assertEqual(
            {
                __version__,
                meson_version.group(1),
                project_version.group(1),
                spec_version.group(1),
                release.get("version"),
            },
            {__version__},
        )

    def test_default_rpm_release_matches_the_build_script(self) -> None:
        spec = (ROOT / "packaging" / "minirec.spec").read_text(
            encoding="utf-8"
        )
        script = (ROOT / "tools" / "build-rpm.sh").read_text(
            encoding="utf-8"
        )
        spec_release = re.search(
            r"(?m)^%\{!\?minirec_release:%global minirec_release (\d+)\}$",
            spec,
        )
        script_release = re.search(
            r'MINIREC_RPM_RELEASE:-([1-9][0-9]*)',
            script,
        )
        self.assertIsNotNone(spec_release)
        self.assertIsNotNone(script_release)
        self.assertEqual(spec_release.group(1), script_release.group(1))

    def test_every_installed_python_source_exists(self) -> None:
        meson = (ROOT / "meson.build").read_text(encoding="utf-8")
        sources = re.findall(r"'((?:minirec/)[^']+\.py)'", meson)
        self.assertGreaterEqual(len(sources), 10)
        self.assertEqual([], [source for source in sources if not (ROOT / source).is_file()])

    def test_desktop_identity_is_stable(self) -> None:
        desktop = rpm_desktop_entry()
        metainfo = ElementTree.parse(
            ROOT / "data" / "cz.pvlcek.minirec.metainfo.xml"
        ).getroot()
        self.assertIn('Exec="/usr/bin/python3" "/usr/bin/minirec"\n', desktop)
        self.assertIn("Icon=cz.pvlcek.minirec\n", desktop)
        self.assertIn("X-GNOME-UsesNotifications=true\n", desktop)
        self.assertEqual(metainfo.findtext("id"), "cz.pvlcek.minirec")
        self.assertEqual(
            metainfo.findtext("launchable"), "cz.pvlcek.minirec.desktop"
        )

    def test_project_urls_point_to_the_linux_source_repository(self) -> None:
        expected = "https://github.com/vlcekapps/minirec-linux"
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "minirec.spec").read_text(
            encoding="utf-8"
        )
        metainfo = ElementTree.parse(
            ROOT / "data" / "cz.pvlcek.minirec.metainfo.xml"
        ).getroot()
        catalog = ElementTree.parse(ROOT / "data" / "minirec.xml").getroot()
        catalog_component = catalog.find("component")
        self.assertIsNotNone(catalog_component)
        assert catalog_component is not None
        self.assertIn(f'Homepage = "{expected}"', pyproject)
        self.assertIn(f'Repository = "{expected}"', pyproject)
        self.assertIn(f"URL:            {expected}", spec)
        self.assertEqual(expected, metainfo.findtext("url[@type='homepage']"))
        self.assertEqual(
            expected,
            catalog_component.findtext("url[@type='homepage']"),
        )

    def test_desktop_exec_stays_valid_while_rpm_replaces_launcher(self) -> None:
        desktop = rpm_desktop_entry()
        values = dict(
            line.split("=", 1)
            for line in desktop.splitlines()
            if "=" in line
        )
        command = shlex.split(values["Exec"])

        # GIO validates the first Exec program while refreshing GNOME's app
        # cache.  It must therefore be the stable system interpreter, not the
        # package-owned /usr/bin/minirec file that RPM is replacing.
        self.assertEqual(["/usr/bin/python3", "/usr/bin/minirec"], command)
        self.assertNotEqual(command[0], command[1])
        self.assertNotIn("TryExec", values)

    def test_gio_accepts_desktop_while_second_exec_path_is_missing(self) -> None:
        probe = """
import gi
gi.require_version("GioUnix", "2.0")
from gi.repository import GioUnix

try:
    entry = GioUnix.DesktopAppInfo.new("cz.pvlcek.minirec.desktop")
except TypeError:
    entry = None
raise SystemExit(0 if entry is not None else 3)
"""
        with tempfile.TemporaryDirectory(prefix="minirec-desktop-test-") as temporary:
            root = Path(temporary)
            applications = root / "applications"
            applications.mkdir()
            launcher = root / "temporarily-missing-minirec"
            desktop_path = applications / "cz.pvlcek.minirec.desktop"
            desktop_path.write_text(
                render_desktop_entry(
                    python="/usr/bin/python3",
                    launcher=str(launcher),
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["XDG_DATA_HOME"] = str(root)
            environment["XDG_DATA_DIRS"] = str(root / "no-system-data")

            valid = subprocess.run(
                [sys.executable, "-c", probe],
                check=False,
                env=environment,
            )
            self.assertEqual(0, valid.returncode)

            desktop_path.write_text(
                render_desktop_entry(
                    python=str(root / "temporarily-missing-python"),
                    launcher=str(launcher),
                ),
                encoding="utf-8",
            )
            invalid = subprocess.run(
                [sys.executable, "-c", probe],
                check=False,
                env=environment,
            )
            self.assertEqual(3, invalid.returncode)

    def test_project_license_is_consistent_and_shipped(self) -> None:
        meson = (ROOT / "meson.build").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "minirec.spec").read_text(
            encoding="utf-8"
        )
        metainfo = ElementTree.parse(
            ROOT / "data" / "cz.pvlcek.minirec.metainfo.xml"
        ).getroot()
        catalog = ElementTree.parse(ROOT / "data" / "minirec.xml").getroot()
        catalog_component = catalog.find("component")
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        notice = (ROOT / "REPOSITORY_NOTICE.md").read_text(encoding="utf-8")
        normalized_notice = " ".join(notice.split())
        self.assertIsNotNone(catalog_component)
        assert catalog_component is not None
        self.assertIn("license: 'GPL-3.0-or-later'", meson)
        self.assertIn(
            "license_files: ['LICENSE', 'REPOSITORY_NOTICE.md']",
            meson,
        )
        self.assertIn('license = "GPL-3.0-or-later"', pyproject)
        self.assertIn(
            'license-files = ["LICENSE", "REPOSITORY_NOTICE.md"]',
            pyproject,
        )
        self.assertIn("License:        GPL-3.0-or-later", spec)
        self.assertEqual(
            "GPL-3.0-or-later",
            metainfo.findtext("project_license"),
        )
        self.assertEqual(
            "GPL-3.0-or-later",
            catalog_component.findtext("project_license"),
        )
        self.assertEqual("FSFAP", metainfo.findtext("metadata_license"))
        self.assertEqual(
            "FSFAP",
            catalog_component.findtext("metadata_license"),
        )
        self.assertIn("GNU GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3, 29 June 2007", license_text)
        self.assertIn(
            "SPDX-License-Identifier: GPL-3.0-or-later",
            normalized_notice,
        )
        self.assertIn("either version 3 of the License", normalized_notice)
        self.assertIn("any later version", normalized_notice)
        self.assertIn("GPL-3.0-or-later", normalized_notice)
        self.assertIn(
            "%license %{_datadir}/licenses/minirec-linux/REPOSITORY_NOTICE.md",
            spec,
        )
        self.assertNotIn("LicenseRef-Proprietary", meson + pyproject + spec)
        self.assertNotIn(".invalid", spec)

    def test_release_and_gate_entry_points_are_executable(self) -> None:
        paths = (
            ROOT / "run.sh",
            ROOT / "tools" / "build-rpm.sh",
            ROOT / "tools" / "validate_appstream_catalog.py",
            ROOT / "tools" / "gui_smoke.py",
            ROOT / "tools" / "large_text_smoke.py",
            ROOT / "tools" / "accessibility_smoke.py",
        )
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertTrue(path.stat().st_mode & stat.S_IXUSR)

    def test_package_owns_no_recording_or_preference_paths(self) -> None:
        spec = (ROOT / "packaging" / "minirec.spec").read_text(
            encoding="utf-8"
        )
        files_section = spec.split("%files", 1)[1].split("%changelog", 1)[0]
        self.assertNotIn("Recordings", files_section)
        self.assertNotIn(".config", files_section)
        self.assertNotIn(".local/state", files_section)

    def test_package_installs_the_primary_user_documentation(self) -> None:
        meson = (ROOT / "meson.build").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "minirec.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn("'README.md'", meson)
        self.assertIn(
            "%doc %{_datadir}/doc/minirec-linux/README.md",
            spec,
        )


if __name__ == "__main__":
    unittest.main()
