#!/usr/bin/env bashio
set -e

bashio::config.require 'db_host'
bashio::config.require 'db_password'

DB_HOST=$(bashio::config 'db_host')
DB_PORT=$(bashio::config 'db_port')

DB_NAME=$(bashio::config 'db_name')
DB_USER=$(bashio::config 'db_user')
DB_PASSWORD=$(bashio::config 'db_password')
TABLE_PREFIX=$(bashio::config 'table_prefix')

export WORDPRESS_DB_HOST="${DB_HOST}:${DB_PORT}"
export WORDPRESS_DB_NAME="${DB_NAME}"
export WORDPRESS_DB_USER="${DB_USER}"
export WORDPRESS_DB_PASSWORD="${DB_PASSWORD}"
export WORDPRESS_TABLE_PREFIX="${TABLE_PREFIX}"

if bashio::config.true 'debug'; then
    export WORDPRESS_DEBUG=1
fi

if bashio::config.has_value 'locale'; then
    WORDPRESS_LOCALE=$(bashio::config 'locale')
    export WORDPRESS_LOCALE
fi

if bashio::config.has_value 'config_extra'; then
    WORDPRESS_CONFIG_EXTRA=$(bashio::config 'config_extra')
    export WORDPRESS_CONFIG_EXTRA
fi

# Persist the full WordPress install (core, themes, plugins, uploads) across
# restarts and add-on updates, mirroring the upstream image's documented
# `wordpress:/var/www/html` volume.
mkdir -p /data/wordpress
if [ ! -L /var/www/html ]; then
    rm -rf /var/www/html
    ln -s /data/wordpress /var/www/html
fi

bashio::log.info "Waiting for database at ${DB_HOST}:${DB_PORT}..."
tries=0
until mysqladmin ping --connect-timeout=3 -h"${DB_HOST}" -P"${DB_PORT}" -u"${WORDPRESS_DB_USER}" -p"${WORDPRESS_DB_PASSWORD}" --silent 2>/dev/null; do
    tries=$((tries + 1))
    if [ "${tries}" -ge 30 ]; then
        bashio::log.warning "Database still not reachable after 60s, starting WordPress anyway..."
        break
    fi
    sleep 2
done

bashio::log.info "Starting WordPress..."
exec docker-entrypoint.sh apache2-foreground
