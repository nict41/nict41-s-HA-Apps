#!/usr/bin/env bash
# Work out which apps a change actually touches, so a one-line edit to one
# app does not rebuild every image in the repository.
#
# Reads the changed paths on stdin, one per line, and writes two GitHub
# Actions outputs:
#
#     build=[{"app":...,"arch":...,"platform":...}, ...]
#     test=["app", ...]
#
# Everything is selected when the change is not confined to apps - a
# workflow edit, this script, the matrix itself - because such a change can
# affect any of them. The caller also sets SELECT_ALL=true for a push to the
# default branch and for a manual run, where the point is to prove the whole
# repository still builds rather than to be quick.
#
# Selecting too much costs CI minutes. Selecting too little means a broken
# image merges, so every case that is not clearly confined to one app
# resolves to everything.
set -euo pipefail

matrix_file=${MATRIX_FILE:-.github/matrix.json}
select_all=${SELECT_ALL:-false}
# Apps are directories holding a config.yaml. Resolved against the checkout
# rather than the matrix so a *newly added* app directory is recognised.
repo_root=${REPO_ROOT:-.}

changed=$(cat || true)

apps=""
if [ "${select_all}" != "true" ]; then
    if [ -z "${changed//[[:space:]]/}" ]; then
        # No changed paths at all: nothing to prove.
        select_all=false
    else
        while IFS= read -r path; do
            [ -n "${path}" ] || continue
            dir=${path%%/*}
            if [ "${dir}" = "${path}" ] || [ ! -f "${repo_root}/${dir}/config.yaml" ]; then
                # A repository-level file, or a directory that is not an app.
                select_all=true
                break
            fi
            apps="${apps}${dir}"$'\n'
        done <<< "${changed}"
    fi
fi

if [ "${select_all}" = "true" ]; then
    build=$(jq -c '.build' "${matrix_file}")
    tests=$(jq -c '.test' "${matrix_file}")
else
    selected=$(printf '%s' "${apps}" | sort -u \
        | jq -R -s -c 'split("\n") | map(select(length > 0))')
    build=$(jq -c --argjson sel "${selected}" \
        '.build | map(select(.app as $a | $sel | index($a)))' "${matrix_file}")
    tests=$(jq -c --argjson sel "${selected}" \
        '.test | map(select(. as $a | $sel | index($a)))' "${matrix_file}")
fi

printf 'build=%s\n' "${build}"
printf 'test=%s\n' "${tests}"
