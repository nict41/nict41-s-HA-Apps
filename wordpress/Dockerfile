FROM wordpress:php8.3-apache

ARG BASHIO_VERSION="0.17.5"

# Install the MariaDB server (used when no external db_host is configured,
# making the add-on fully self-contained) and its client tools, and fetch
# bashio so this add-on can read its Home Assistant options.json like any
# other Supervisor add-on. Native mariadb-* command names are used
# throughout run.sh - Debian trixie ships the mysql* names as separate
# compat packages, deliberately not installed here.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates jq \
        mariadb-server mariadb-client; \
    curl -L -f -s -o /tmp/bashio.tar.gz \
        "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz"; \
    mkdir -p /usr/src/bashio; \
    tar zxf /tmp/bashio.tar.gz --strip-components=1 -C /usr/src/bashio; \
    mv /usr/src/bashio/lib /usr/lib/bashio; \
    ln -s /usr/lib/bashio/bashio /usr/bin/bashio; \
    rm -rf /usr/src/bashio /tmp/bashio.tar.gz; \
    rm -rf /var/lib/apt/lists/*; \
    # The package postinst initializes a system datadir this add-on never
    # uses (the real datadir lives in /data/mysql); drop it in the same
    # layer to keep the image small.
    rm -rf /var/lib/mysql/*

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
