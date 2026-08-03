#!/usr/bin/bash
set -euo pipefail

# Build entirely from local sources and distro-provided build dependencies.
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_directory="$(cd -- "${script_directory}/.." && pwd)"
spec_file="${project_directory}/packaging/minirec.spec"
rpm_release="${MINIREC_RPM_RELEASE:-1}"
source_date_epoch="${SOURCE_DATE_EPOCH:-1785715200}"
output_directory="${MINIREC_RPM_OUTPUT:-${project_directory}/dist/rpm}"

if [[ ! "${rpm_release}" =~ ^[1-9][0-9]*$ ]]; then
    echo "MINIREC_RPM_RELEASE must be a positive integer." >&2
    exit 2
fi
if [[ ! "${source_date_epoch}" =~ ^[0-9]+$ ]]; then
    echo "SOURCE_DATE_EPOCH must be a non-negative integer." >&2
    exit 2
fi

meson_version="$(sed -n "s/^[[:space:]]*version: '\([^']*\)'.*/\1/p" "${project_directory}/meson.build")"
python_version="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "${project_directory}/minirec/__init__.py")"
project_version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "${project_directory}/pyproject.toml" | head -n 1)"
appstream_version="$(sed -n 's/.*<release version="\([^"]*\)".*/\1/p' "${project_directory}/data/cz.pvlcek.minirec.metainfo.xml" | head -n 1)"
spec_version="$(sed -n 's/^Version:[[:space:]]*\([^[:space:]]*\).*/\1/p' "${spec_file}")"

if [[ -z "${meson_version}" ]] || [[ "${meson_version}" != "${python_version}" ]] || \
   [[ "${meson_version}" != "${project_version}" ]] || \
   [[ "${meson_version}" != "${appstream_version}" ]] || \
   [[ "${meson_version}" != "${spec_version}" ]]; then
    echo "Version mismatch; update Meson, Python, pyproject, AppStream, and RPM spec together." >&2
    printf 'Meson=%s Python=%s pyproject=%s AppStream=%s spec=%s\n' \
        "${meson_version}" "${python_version}" "${project_version}" \
        "${appstream_version}" "${spec_version}" >&2
    exit 2
fi

work_directory="$(mktemp -d -p /tmp minirec-rpm-build.XXXXXXXX)"
trap 'rm -rf -- "${work_directory}"' EXIT
desktop_build_directory="${work_directory}/desktop-build"
source_copy="${work_directory}/source"
sdist_directory="${work_directory}/sdist"
repack_directory="${work_directory}/repack"
rpm_top_directory="${work_directory}/rpmbuild"
rpm_temp_directory="${work_directory}/tmp"

python3 -m unittest discover -s "${project_directory}/tests" -v
meson setup "${desktop_build_directory}" "${project_directory}" \
    --buildtype=plain --prefix=/usr --bindir=/usr/bin --datadir=/usr/share \
    --wrap-mode=nodownload
desktop-file-validate \
    "${desktop_build_directory}/cz.pvlcek.minirec.desktop"
python3 "${project_directory}/tools/validate_appstream_catalog.py"

mkdir -p -- "${source_copy}" "${sdist_directory}" "${repack_directory}" \
    "${rpm_temp_directory}"
mkdir -p -- "${rpm_top_directory}/BUILD" "${rpm_top_directory}/BUILDROOT"
mkdir -p -- "${rpm_top_directory}/RPMS" "${rpm_top_directory}/SOURCES"
mkdir -p -- "${rpm_top_directory}/SPECS" "${rpm_top_directory}/SRPMS"

cp -a -- "${project_directory}/." "${source_copy}/"
rm -rf -- "${source_copy}/.agents" "${source_copy}/.codex" \
    "${source_copy}/.git" "${source_copy}/_build" "${source_copy}/build" \
    "${source_copy}/dist" "${source_copy}/minirec_linux.egg-info"
find "${source_copy}" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "${source_copy}" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

(
    cd -- "${source_copy}"
    SOURCE_DATE_EPOCH="${source_date_epoch}" python3 -c \
        'import sys; from setuptools.build_meta import build_sdist; build_sdist(sys.argv[1])' \
        "${sdist_directory}"
)

generated_sdist="${sdist_directory}/minirec_linux-${meson_version}.tar.gz"
if [[ ! -f "${generated_sdist}" ]]; then
    echo "setuptools did not create the expected archive: ${generated_sdist}" >&2
    exit 1
fi

tar -xzf "${generated_sdist}" -C "${repack_directory}"
mv -- "${repack_directory}/minirec_linux-${meson_version}" \
    "${repack_directory}/minirec-${meson_version}"
find "${repack_directory}/minirec-${meson_version}" -exec \
    touch -h -d "@${source_date_epoch}" -- {} +

source_tar="${rpm_top_directory}/SOURCES/minirec-${meson_version}.tar"
tar --sort=name --format=gnu --owner=0 --group=0 --numeric-owner \
    --mtime="@${source_date_epoch}" -C "${repack_directory}" \
    -cf "${source_tar}" "minirec-${meson_version}"
gzip -n -- "${source_tar}"
install -m 0644 -- "${spec_file}" "${rpm_top_directory}/SPECS/minirec.spec"

SOURCE_DATE_EPOCH="${source_date_epoch}" rpmbuild -ba \
    --define "_topdir ${rpm_top_directory}" \
    --define "_tmppath ${rpm_temp_directory}" \
    --define "_buildhost localhost" \
    --define "_smp_build_ncpus 1" \
    --define "minirec_release ${rpm_release}" \
    --define "use_source_date_epoch_as_buildtime 1" \
    "${rpm_top_directory}/SPECS/minirec.spec"

mkdir -p -- "${output_directory}"
install -m 0644 -- "${source_tar}.gz" "${output_directory}/"
mapfile -d '' artifacts < <(
    find "${rpm_top_directory}/RPMS" "${rpm_top_directory}/SRPMS" \
        -type f -name '*.rpm' -print0 | sort -z
)
if (( ${#artifacts[@]} == 0 )); then
    echo "rpmbuild completed without producing an RPM." >&2
    exit 1
fi
for artifact in "${artifacts[@]}"; do
    install -m 0644 -- "${artifact}" "${output_directory}/"
done

echo "Normalized source archive:"
sha256sum -- "${output_directory}/$(basename -- "${source_tar}.gz")"
echo "RPM artifacts:"
for artifact in "${artifacts[@]}"; do
    sha256sum -- "${output_directory}/$(basename -- "${artifact}")"
done
