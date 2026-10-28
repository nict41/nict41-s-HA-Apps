#!/bin/sh
# Start the Kiwix manager (which supervises kiwix-serve itself) as PUID:PGID,
# so archives written to a NAS volume belong to the host user that owns it.
set -eu

PUID=${PUID:-1000}
PGID=${PGID:-1000}
PORT=${PORT:-8099}

log() { echo "[entrypoint] $*"; }

if [ "$(id -u)" = "0" ]; then
    # Re-number the built-in user to match the host's IDs. -o allows IDs that
    # already exist in the image (e.g. PGID=100 "users").
    if [ "$(id -g kiwix)" != "${PGID}" ]; then
        groupmod -o -g "${PGID}" kiwix
    fi
    if [ "$(id -u kiwix)" != "${PUID}" ]; then
        usermod -o -u "${PUID}" kiwix
    fi

    # Only the app's own state is chowned. The ZIM volume is the user's
    # storage and may be huge or a network share, so it is checked, not
    # rewritten.
    mkdir -p "${DATA_DIR:-/data}"
    chown -R kiwix:kiwix "${DATA_DIR:-/data}"

    run_as="su-exec kiwix:kiwix"
else
    # Already started as a non-root user (docker run --user): just use it.
    run_as=""
fi

if [ -n "${ZIM_PATH:-}" ]; then
    log "ZIM storage path: ${ZIM_PATH}"
    if [ "${ZIM_PATH}" = "/zim" ] && ! mountpoint -q /zim 2>/dev/null; then
        log "WARNING: /zim is not a mounted volume - archives would be written"
        log "WARNING: inside the container and lost when it is recreated."
        log "WARNING: Mount a host folder there, e.g. -v /mnt/nas/kiwix:/zim"
    fi
else
    log "WARNING: ZIM_PATH is empty - set it to the folder archives are stored in"
fi

if [ -n "${KIWIX_USERNAME:-}" ] && [ -n "${KIWIX_PASSWORD:-}" ]; then
    log "Login required (HTTP Basic) for user '${KIWIX_USERNAME}'"
else
    log "WARNING: no KIWIX_USERNAME/KIWIX_PASSWORD set - anyone who can reach"
    log "WARNING: port ${PORT} can download and delete archives. Set them, or"
    log "WARNING: keep the port behind an authenticating reverse proxy."
fi

if [ -n "${BASE_PATH:-}" ]; then
    log "Expecting to be served under ${BASE_PATH}/ by a reverse proxy"
fi

log "Starting Kiwix manager on port ${PORT} as $(id -un kiwix 2>/dev/null || id -un) (${PUID}:${PGID}), TZ=${TZ:-UTC}"

cd /app
# shellcheck disable=SC2086 # run_as is intentionally word-split
exec ${run_as} python3 -m uvicorn main:app \
    --host 0.0.0.0 --port "${PORT}" \
    --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
