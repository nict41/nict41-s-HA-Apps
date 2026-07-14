#!/usr/bin/env bashio
set -e

# ==========================================================================
# Networking
# ==========================================================================
# The built-in Caddy reverse proxy (see reverse-proxy/Caddyfile in the
# upstream image) always listens on :3000 and fans out to the frontend and
# backend behind it - this add-on only ever exposes that single port, which
# is exactly what a tunnel client (e.g. cloudflared) needs to point at.
TRUST_PROXY=$(bashio::config 'trust_proxy')
export TRUST_PROXY

if bashio::config.has_value 'clamav_host'; then
    CLAMAV_HOST=$(bashio::config 'clamav_host')
    export CLAMAV_HOST
    CLAMAV_PORT=$(bashio::config 'clamav_port')
    export CLAMAV_PORT
fi

# ==========================================================================
# Persistent storage
# ==========================================================================
# Uploaded files and the SQLite database (unless S3 storage is configured
# in the app's own Admin UI) live directly under the add-on's persistent
# /data, so they survive restarts/updates and are included in Home
# Assistant backups.
export DATA_DIRECTORY=/data
export DATABASE_URL="file:/data/pingvin-share.db?connection_limit=1"

# Custom branding images (logo/favicon, uploaded via the admin UI) are
# written by the app to a fixed path inside the image. The upstream Docker
# Compose file persists that with its own bind mount; here the same
# directory is swapped for a symlink into /data so it survives too. This is
# safe because, unlike the WordPress app in this repository, the upstream
# Dockerfile does not declare it a VOLUME.
mkdir -p /data/images
rm -rf /opt/app/frontend/public/img
ln -s /data/images /opt/app/frontend/public/img

# ==========================================================================
# Optional config.yaml (advanced)
# ==========================================================================
# Pingvin Share can be configured either from its own Admin UI
# (recommended - branding, sharing rules, SMTP, OAuth, LDAP, S3, legal
# pages) or from a config.yaml file, but NOT both: as soon as a config.yaml
# is present, the app disables editing settings from the UI entirely. So
# this is opt-in and off by default. A file dropped in the share folder
# takes precedence over the inline 'config_yaml' option, so a long config
# can be maintained as a real file instead of pasted into the add-on's
# options box.
CONFIG_TARGET=/opt/app/config.yaml
SHARE_CONFIG=/share/pingvin_share/config.yaml
if [ -f "${SHARE_CONFIG}" ]; then
    bashio::log.info "Using ${SHARE_CONFIG} (Admin UI settings are disabled while this file exists)."
    cp "${SHARE_CONFIG}" "${CONFIG_TARGET}"
elif bashio::config.has_value 'config_yaml'; then
    bashio::log.info "Using the 'config_yaml' option (Admin UI settings are disabled while this option is set)."
    bashio::config 'config_yaml' > "${CONFIG_TARGET}"
else
    rm -f "${CONFIG_TARGET}"
fi

# ==========================================================================
# Start Pingvin Share
# ==========================================================================
# Handed off to the upstream entrypoint, unmodified: it starts Caddy, the
# Next.js frontend and the NestJS backend and supervises all three. exec so
# it replaces this script as PID 1 and receives Supervisor's stop signal
# directly.
bashio::log.info "Starting Pingvin Share..."
cd /opt/app
exec sh ./scripts/docker/entrypoint.sh
