#!/usr/bin/env bash
# One-time import of another Claude Code app's private /data folder.
#
# Every Home Assistant app has its own /data, and no app can read another's.
# To move from the upstream Claude Code app (or any copy of it) to this one,
# copy the old app's /data into $IMPORT_DIR from inside the OLD app, then
# write $IMPORT_DIR/.stage-complete. On its next start this app copies
# everything in it into its own /data, records that in $MARKER, and deletes
# $IMPORT_DIR so tokens and logins don't stay on the shared folder.
#
# Runs at boot before /data/npm-global is seeded, so an imported npm-global
# (Claude CLI, extra tools, their logins) wins over the image copy.
#
# Paths are variables so the tests can point them at a temporary folder.
set -u

IMPORT_DIR=${HA_SERVER_IMPORT_DIR:-/share/claude_code_import}
DATA_DIR=${HA_SERVER_DATA_DIR:-/data}
MARKER="${DATA_DIR}/.ha-server-imported"

[ -d "${IMPORT_DIR}" ] || exit 0

if [ -f "${MARKER}" ]; then
    echo "[WARN] ${IMPORT_DIR} exists, but data was already imported ($(cat "${MARKER}" 2>/dev/null)). Ignoring it; delete it if it is left over."
    exit 0
fi

if [ ! -f "${IMPORT_DIR}/.stage-complete" ]; then
    echo "[WARN] ${IMPORT_DIR} has no .stage-complete file, so the copy into it may not have finished. Not importing."
    exit 0
fi

echo "[INFO] Importing app data from ${IMPORT_DIR} into ${DATA_DIR}"
mkdir -p "${DATA_DIR}"

failed=0
count=0
shopt -s dotglob nullglob
for entry in "${IMPORT_DIR}"/*; do
    name=$(basename "${entry}")
    case "${name}" in
        # options.json belongs to the Supervisor; the marker is ours.
        options.json | .stage-complete) continue ;;
    esac
    if [ -e "${DATA_DIR}/${name}" ]; then
        rm -rf "${DATA_DIR}/${name}.pre-import"
        mv "${DATA_DIR}/${name}" "${DATA_DIR}/${name}.pre-import"
        echo "[INFO] Kept this app's own ${name} as ${name}.pre-import"
    fi
    if cp -a "${entry}" "${DATA_DIR}/${name}"; then
        count=$((count + 1))
    else
        echo "[ERROR] Failed to import ${name}"
        failed=1
    fi
done
shopt -u dotglob nullglob

if [ "${failed}" -ne 0 ]; then
    echo "[ERROR] Import incomplete. ${IMPORT_DIR} has been left in place; fix the error and restart the app to try again."
    exit 1
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "${MARKER}"
rm -rf "${IMPORT_DIR}"
echo "[INFO] Imported ${count} item(s) and removed ${IMPORT_DIR}"
