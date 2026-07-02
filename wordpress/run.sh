#!/usr/bin/env bashio
set -e

# ==========================================================================
# Mode detection: no db_host configured -> bundled local MariaDB
# ==========================================================================
LOCAL_DB=false
bashio::config.has_value 'db_host' || LOCAL_DB=true

DB_NAME=$(bashio::config 'db_name')
DB_USER=$(bashio::config 'db_user')
TABLE_PREFIX=$(bashio::config 'table_prefix')

MARIADB_DATADIR=/data/mysql
MARIADB_SOCKET=/run/mysqld/mysqld.sock
DUMP_DIR=/data/backups
DUMP_KEEP=7

if [ "${LOCAL_DB}" = true ]; then
    # The bundled server only ever listens on the container's loopback, so
    # the fixed port can't collide with anything outside the add-on.
    DB_HOST="127.0.0.1"
    DB_PORT=3306
    if [ "$(bashio::config 'db_port')" != "3306" ]; then
        bashio::log.info "Using the built-in database: 'db_port' is ignored."
    fi
    # Password precedence: explicit option > previously generated > fresh.
    # Whatever wins is persisted so it stays stable across restarts (and is
    # captured by Home Assistant backups along with the rest of /data).
    if bashio::config.has_value 'db_password'; then
        DB_PASSWORD=$(bashio::config 'db_password')
    elif [ -s /data/.db_password ]; then
        DB_PASSWORD=$(cat /data/.db_password)
        bashio::log.info "Using previously generated database password."
    else
        DB_PASSWORD=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)
        bashio::log.info "Generated a database password (stored in /data/.db_password)."
    fi
    printf '%s' "${DB_PASSWORD}" > /data/.db_password
    chmod 600 /data/.db_password
else
    if ! bashio::config.has_value 'db_password'; then
        bashio::log.fatal "'db_password' is required when 'db_host' is set."
        bashio::exit.nok
    fi
    DB_HOST=$(bashio::config 'db_host')
    DB_PORT=$(bashio::config 'db_port')
    DB_PASSWORD=$(bashio::config 'db_password')
fi

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

# ==========================================================================
# Local database helpers
# ==========================================================================
sql_escape() {
    # Escapes a value for use inside a single-quoted SQL string literal:
    # backslashes first, then single quotes.
    local s=${1//\\/\\\\}
    printf '%s' "${s//\'/\\\'}"
}

dump_local_db() {
    # A logical dump alongside the raw datadir: Home Assistant backups copy
    # /data while the server is running, and a raw InnoDB datadir copied
    # mid-write isn't guaranteed to be usable - the newest dump always is.
    mkdir -p "${DUMP_DIR}"
    local file
    file="${DUMP_DIR}/wordpress-$(date +%Y%m%d-%H%M%S).sql.gz"
    if mariadb-dump --socket="${MARIADB_SOCKET}" --single-transaction --quick \
            "${DB_NAME}" 2>/dev/null | gzip > "${file}"; then
        bashio::log.info "Database dump written to ${file}"
        # shellcheck disable=SC2012
        ls -1t "${DUMP_DIR}"/wordpress-*.sql.gz 2>/dev/null \
            | tail -n +$((DUMP_KEEP + 1)) | xargs -r rm -f
    else
        rm -f "${file}"
        bashio::log.warning "Database dump failed."
    fi
}

start_local_db() {
    mkdir -p /run/mysqld
    chown mysql:mysql /run/mysqld

    # Fresh-install detection keys off the system schema directory, not the
    # datadir itself, so a half-created empty directory still initializes.
    local fresh=false
    if [ ! -d "${MARIADB_DATADIR}/mysql" ]; then
        fresh=true
        bashio::log.info "Initializing MariaDB data directory in ${MARIADB_DATADIR}..."
        mkdir -p "${MARIADB_DATADIR}"
        chown -R mysql:mysql "${MARIADB_DATADIR}"
        # Root authenticates via the unix socket (this script runs as root),
        # so no root database password ever exists or needs storing.
        mariadb-install-db \
            --user=mysql \
            --datadir="${MARIADB_DATADIR}" \
            --auth-root-authentication-method=socket \
            --skip-test-db \
            >/dev/null
    elif [ "$(stat -c %U "${MARIADB_DATADIR}")" != "mysql" ]; then
        # e.g. after a Home Assistant backup restore that reset ownership.
        bashio::log.info "Fixing ownership of ${MARIADB_DATADIR}..."
        chown -R mysql:mysql "${MARIADB_DATADIR}"
    fi

    bashio::log.info "Starting bundled MariaDB..."
    # Everything is passed explicitly so the distro's packaged config file
    # defaults (datadir, pid-file, bind address) can never redirect state
    # outside /data and /run/mysqld.
    /usr/sbin/mariadbd \
        --user=mysql \
        --datadir="${MARIADB_DATADIR}" \
        --socket="${MARIADB_SOCKET}" \
        --pid-file=/run/mysqld/mariadbd.pid \
        --bind-address=127.0.0.1 \
        --port=3306 \
        --skip-name-resolve \
        --innodb-buffer-pool-size=128M \
        &
    MARIADB_PID=$!

    local tries=0
    until mariadb-admin --socket="${MARIADB_SOCKET}" ping --silent >/dev/null 2>&1; do
        if ! kill -0 "${MARIADB_PID}" 2>/dev/null; then
            bashio::log.fatal "Bundled MariaDB exited during startup. Check the log above."
            bashio::exit.nok
        fi
        tries=$((tries + 1))
        if [ "${tries}" -ge 60 ]; then
            bashio::log.fatal "Bundled MariaDB did not become ready within 60s."
            bashio::exit.nok
        fi
        sleep 1
    done

    local pw_sql
    pw_sql=$(sql_escape "${DB_PASSWORD}")
    bashio::log.info "Ensuring database '${DB_NAME}' and user '${DB_USER}' exist..."
    # ALTER USER keeps the account's password in sync when the db_password
    # option is changed after the first boot.
    mariadb --socket="${MARIADB_SOCKET}" <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`;
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${pw_sql}';
CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${pw_sql}';
ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${pw_sql}';
ALTER USER '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${pw_sql}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

    # Disaster recovery: a brand-new data directory but dumps exist (e.g.
    # the raw datadir inside a Home Assistant backup was unusable) ->
    # restore the newest dump automatically. This can never fire on a truly
    # fresh install, since no dumps exist yet then.
    if [ "${fresh}" = true ]; then
        local latest
        # shellcheck disable=SC2012
        latest=$(ls -1t "${DUMP_DIR}"/wordpress-*.sql.gz 2>/dev/null | head -n 1 || true)
        if [ -n "${latest}" ]; then
            bashio::log.warning "Fresh database but an existing dump was found."
            bashio::log.warning "Restoring ${latest}. Delete ${DUMP_DIR} first to start truly fresh."
            gunzip -c "${latest}" | mariadb --socket="${MARIADB_SOCKET}" "${DB_NAME}"
        fi
    fi

    # Manual restore/migration: drop a dump in /share and restart the
    # add-on. Also the migration path from an external database to the
    # built-in one - see DOCS.md.
    local restore
    for restore in /share/wordpress-restore.sql.gz /share/wordpress-restore.sql; do
        if [ -f "${restore}" ]; then
            bashio::log.warning "Importing ${restore} into database '${DB_NAME}'..."
            case "${restore}" in
                *.gz) gunzip -c "${restore}" | mariadb --socket="${MARIADB_SOCKET}" "${DB_NAME}" ;;
                *)    mariadb --socket="${MARIADB_SOCKET}" "${DB_NAME}" < "${restore}" ;;
            esac
            mv "${restore}" "${restore}.imported-$(date +%Y%m%d-%H%M%S)"
            bashio::log.info "Import finished; file renamed so it is not imported again."
        fi
    done
}

# ==========================================================================
# Supervision / clean shutdown
# ==========================================================================
APACHE_PID=""
MARIADB_PID=""
DUMP_LOOP_PID=""

shutdown_handler() {
    local code=${1:-0}
    trap - TERM INT
    bashio::log.info "Stopping WordPress..."
    if [ -n "${DUMP_LOOP_PID}" ]; then
        kill "${DUMP_LOOP_PID}" 2>/dev/null || true
    fi
    # Apache first, so nothing is writing to the database when it stops.
    if [ -n "${APACHE_PID}" ]; then
        kill -TERM "${APACHE_PID}" 2>/dev/null || true
        wait "${APACHE_PID}" 2>/dev/null || true
    fi
    if [ "${LOCAL_DB}" = true ] && [ -n "${MARIADB_PID}" ]; then
        bashio::log.info "Stopping bundled MariaDB..."
        mariadb-admin --socket="${MARIADB_SOCKET}" shutdown 2>/dev/null || true
        wait "${MARIADB_PID}" 2>/dev/null || true
    fi
    bashio::log.info "Shutdown complete."
    exit "${code}"
}
trap 'shutdown_handler 0' TERM INT

# ==========================================================================
# Start services
# ==========================================================================
if [ "${LOCAL_DB}" = true ]; then
    bashio::log.info "No 'db_host' configured, using the built-in MariaDB server."
    start_local_db

    dump_local_db
    (
        while sleep 86400; do
            dump_local_db
        done
    ) &
    DUMP_LOOP_PID=$!
else
    bashio::log.info "Waiting for database at ${DB_HOST}:${DB_PORT}..."
    tries=0
    until mariadb-admin ping --connect-timeout=3 -h"${DB_HOST}" -P"${DB_PORT}" \
            -u"${DB_USER}" -p"${DB_PASSWORD}" --silent >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "${tries}" -ge 30 ]; then
            bashio::log.warning "Database still not reachable after 60s, starting WordPress anyway..."
            break
        fi
        sleep 2
    done
fi

bashio::log.info "Starting WordPress..."
# Not exec'd: the upstream entrypoint execs apache, so $! stays valid and
# this script stays alive as the supervisor for both services.
docker-entrypoint.sh apache2-foreground &
APACHE_PID=$!

# Block until Apache (or, in local mode, MariaDB) exits, or a signal arrives.
set +e
if [ "${LOCAL_DB}" = true ]; then
    wait -n "${APACHE_PID}" "${MARIADB_PID}"
else
    wait "${APACHE_PID}"
fi
rc=$?
bashio::log.warning "A service exited unexpectedly (rc=${rc}); shutting down add-on."
shutdown_handler 1
