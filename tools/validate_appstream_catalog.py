#!/usr/bin/python3
"""Validate MiniRec metainfo and its package-backed local AppStream catalog."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree


COMPONENT_ID = "cz.pvlcek.minirec"
PACKAGE_NAME = "minirec"
SHARED_TAGS = (
    "id",
    "metadata_license",
    "project_license",
    "name",
    "summary",
    "launchable",
    "developer",
    "url",
    "provides",
    "categories",
    "supports",
    "content_rating",
    "releases",
)
XML_LANGUAGE = "{http://www.w3.org/XML/1998/namespace}lang"


class MetadataError(ValueError):
    """Raised when desktop metadata cannot safely be packaged."""


def _canonical(element: ElementTree.Element) -> tuple[object, ...]:
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        " ".join((element.text or "").split()),
        tuple(_canonical(child) for child in element),
    )


def _values(component: ElementTree.Element, tag: str) -> list[tuple[object, ...]]:
    return sorted((_canonical(item) for item in component.findall(tag)), key=repr)


def _description_child(element: ElementTree.Element) -> tuple[object, ...]:
    return (
        element.tag,
        tuple(
            sorted(
                (name, value)
                for name, value in element.attrib.items()
                if name != XML_LANGUAGE
            )
        ),
        " ".join((element.text or "").split()),
        tuple(_description_child(child) for child in element),
    )


def _localized_descriptions(
    component: ElementTree.Element,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    localized: dict[str, list[tuple[object, ...]]] = {}
    for description in component.findall("description"):
        section_language = description.get(XML_LANGUAGE, "C")
        for child in description:
            language = child.get(XML_LANGUAGE, section_language)
            localized.setdefault(language, []).append(_description_child(child))
    return {language: tuple(values) for language, values in sorted(localized.items())}


def validate_sources(metainfo_path: Path, catalog_path: Path) -> None:
    metainfo = ElementTree.parse(metainfo_path).getroot()
    catalog_root = ElementTree.parse(catalog_path).getroot()
    if metainfo.tag != "component":
        raise MetadataError("metainfo must contain one component root")
    if catalog_root.tag != "components" or catalog_root.get("origin") != "minirec-local":
        raise MetadataError("catalog must have a minirec-local components root")
    catalog_components = catalog_root.findall("component")
    if len(catalog_components) != 1:
        raise MetadataError("catalog must contain exactly one component")
    catalog = catalog_components[0]

    for label, component in (("metainfo", metainfo), ("catalog", catalog)):
        if component.get("type") != "desktop-application":
            raise MetadataError(f"{label}: expected desktop-application type")
        if (component.findtext("id") or "").strip() != COMPONENT_ID:
            raise MetadataError(f"{label}: unexpected component id")
    if catalog.get("merge") is not None:
        raise MetadataError("catalog component must be standalone")
    if [item.text for item in catalog.findall("pkgname")] != [PACKAGE_NAME]:
        raise MetadataError("catalog must contain exactly one minirec pkgname")
    if metainfo.findall("pkgname"):
        raise MetadataError("pkgname belongs only in distribution catalog data")

    for tag in SHARED_TAGS:
        upstream = _values(metainfo, tag)
        packaged = _values(catalog, tag)
        if not upstream:
            raise MetadataError(f"metainfo is missing required <{tag}> data")
        if upstream != packaged:
            raise MetadataError(f"catalog <{tag}> differs from metainfo")

    if _localized_descriptions(metainfo) != _localized_descriptions(catalog):
        raise MetadataError("catalog descriptions differ from metainfo")

    icons = [
        (item.get("type"), (item.text or "").strip())
        for item in catalog.findall("icon")
    ]
    if icons != [("stock", COMPONENT_ID)]:
        raise MetadataError("catalog must contain the installed stock icon")


def _run_appstreamcli(*paths: Path) -> None:
    executable = shutil.which("appstreamcli")
    if executable is None:
        raise MetadataError("appstreamcli is required")
    for path in paths:
        result = subprocess.run(
            [executable, "validate", "--no-net", "--strict", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode:
            raise MetadataError(f"appstreamcli rejected {path}:\n{result.stdout.rstrip()}")


def _run_isolated_pool(metainfo_path: Path, catalog_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="minirec-appstream-") as temporary:
        root = Path(temporary)
        catalog_dir = root / "catalog"
        metainfo_dir = root / "metainfo"
        catalog_dir.mkdir()
        metainfo_dir.mkdir()
        shutil.copy2(catalog_path, catalog_dir / catalog_path.name)
        shutil.copy2(metainfo_path, metainfo_dir / metainfo_path.name)
        previous_cache = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(root / "cache")
        try:
            import gi

            gi.require_version("AppStream", "1.0")
            from gi.repository import AppStream

            pool = AppStream.Pool.new()
            pool.set_load_std_data_locations(False)
            pool.add_extra_data_location(str(catalog_dir), AppStream.FormatStyle.CATALOG)
            pool.add_extra_data_location(str(metainfo_dir), AppStream.FormatStyle.METAINFO)
            if not pool.load():
                raise MetadataError("isolated AppStream pool did not load")
            components = pool.get_components_by_id(COMPONENT_ID).as_array()
        except (ImportError, ValueError) as error:
            raise MetadataError("AppStream GI bindings are required") from error
        finally:
            if previous_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = previous_cache
        if len(components) != 1 or components[0].get_pkgname() != PACKAGE_NAME:
            raise MetadataError("catalog and metainfo did not merge into one package-backed app")


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--metainfo",
        type=Path,
        default=root / "data" / "cz.pvlcek.minirec.metainfo.xml",
    )
    parser.add_argument(
        "--catalog", type=Path, default=root / "data" / "minirec.xml"
    )
    arguments = parser.parse_args(argv)
    try:
        validate_sources(arguments.metainfo, arguments.catalog)
        _run_appstreamcli(arguments.metainfo, arguments.catalog)
        _run_isolated_pool(arguments.metainfo, arguments.catalog)
    except (MetadataError, ElementTree.ParseError, OSError) as error:
        print(f"AppStream metadata validation failed: {error}", file=sys.stderr)
        return 1
    print("MiniRec AppStream metadata passed strict and isolated-pool validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
