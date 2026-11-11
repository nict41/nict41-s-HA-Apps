# Pinned by digest so a Home Assistant rebuild can't reuse a cached,
# unpatched base layer. The digest is the multi-arch index for the
# wordpress:php8.3-apache image published 2026-11-08.
FROM wordpress:php8.3-apache@sha256:9001ea7dafd2bc3f05d068e6413b0edd2a116eeed4dfe9e08b6bd718dee4960a

ARG BASHIO_VERSION="0.19.0"

# Install the MariaDB server (used when no external db_host is configured,
# making the add-on fully self-contained) and its client tools, and fetch
# bashio so this add-on can read its Home Assistant options.json like any
# other Supervisor add-on. Native mariadb-* command names are used
# throughout run.sh - Debian trixie ships the mysql* names as separate
# compat packages, deliberately not installed here.
# Pick up security fixes the distribution has published since the base image was built.
RUN set -eu; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; \
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
    rm -rf /var/lib/mysql/*; \
    # The base image declares /var/www/html as a VOLUME, which makes it a
    # real mount point - it can't be removed or replaced with a symlink at
    # runtime (fails with "Device or resource busy"), and even a build-time
    # symlink would just get hidden by Docker's anonymous volume at
    # container start. So the WordPress install lives directly in
    # /data/wordpress (the app's real persistent storage) instead:
    # docker-entrypoint.sh operates purely relative to $PWD (no hardcoded
    # /var/www/html path in it), so moving WORKDIR is enough on that side;
    # Apache's DocumentRoot/Directory config is the other half - the
    # DocumentRoot itself says /var/www/html, but the <Directory> access
    # grant (both Debian's own default and the one this base image adds)
    # is scoped to the bare /var/www/ prefix, so both forms need rewriting.
    find /etc/apache2 -type f -name '*.conf' \
        -exec sed -i \
            -e 's!/var/www/html!/data/wordpress!g' \
            -e 's!/var/www/!/data/wordpress/!g' \
            {} +

# Don't advertise the PHP version in an X-Powered-By header.
RUN echo 'expose_php = Off' > "${PHP_INI_DIR}/conf.d/zz-expose-php.ini"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /data/wordpress
CMD [ "/run.sh" ]
