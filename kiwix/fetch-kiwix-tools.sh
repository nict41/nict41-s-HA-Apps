#!/bin/sh
# Installs kiwix-serve and kiwix-manage from a pinned kiwix-tools release.
#
#   fetch-kiwix-tools.sh VERSION ARCH            download, verify, install to /usr/bin
#   fetch-kiwix-tools.sh VERSION ARCH --to DIR   download from kiwix.org and verify only
#
# ARCH is kiwix's name for it: x86_64 or aarch64.
#
# download.kiwix.org sends most clients to a mirror that manages only tens of
# KB/s, so the ~21 MB archive took 15-30 minutes per build and sometimes timed
# out. A copy of each archive is attached to this repo's
# "kiwix-tools-VERSION" GitHub release (made by the kiwix-tools-mirror
# workflow) and is tried first; kiwix.org is the fallback. Both are checked
# against the checksums below, so the copy can't differ from upstream's file.
#
# To move to a new kiwix-tools version: add its checksums here, bump
# KIWIX_TOOLS_VERSION in kiwix/build.yaml and both Dockerfiles, and run the
# "Mirror kiwix-tools" workflow (it also runs when this file changes on main).
set -eu

version="$1"
arch="$2"

case "${version}-${arch}" in
    3.8.2-x86_64) sha256="b0ae98dd344aa0469a15ab42feff6d5aafb79541a82fb4e2647c74b073123815" ;;
    3.8.2-aarch64) sha256="a9898fc5363437327350db669bbc03dcc7ecfd498dae727d7184bebe77019514" ;;
    *) echo "No pinned checksum for kiwix-tools ${version} (${arch})" >&2; exit 1 ;;
esac

name="kiwix-tools_linux-${arch}-${version}"
upstream="https://download.kiwix.org/release/kiwix-tools/${name}.tar.gz"
copy="https://github.com/nict41/nict41-s-HA-Apps/releases/download/kiwix-tools-${version}/${name}.tar.gz"

fetch() {  # fetch URL FILE -> 0 if downloaded and the checksum matches
    echo "Downloading ${1}"
    rm -f "${2}"
    if ! wget -q -T 60 -O "${2}" "${1}"; then
        echo "Download failed: ${1}" >&2
        return 1
    fi
    if ! echo "${sha256}  ${2}" | sha256sum -c - >/dev/null 2>&1; then
        echo "Checksum mismatch: ${1}" >&2
        return 1
    fi
}

if [ "${3:-}" = "--to" ]; then
    mkdir -p "${4}"
    fetch "${upstream}" "${4}/${name}.tar.gz"
    exit 0
fi

archive="/tmp/${name}.tar.gz"
fetch "${copy}" "${archive}" || fetch "${upstream}" "${archive}"
tar -xzf "${archive}" -C /tmp
install -m 0755 "/tmp/${name}/kiwix-serve" "/tmp/${name}/kiwix-manage" /usr/bin/
rm -rf "${archive}" "/tmp/${name}"
kiwix-serve --version
