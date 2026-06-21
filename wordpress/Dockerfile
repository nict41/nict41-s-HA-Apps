FROM wordpress:php8.3-apache

ARG BASHIO_VERSION="0.17.5"

# Install a MySQL client (used to wait for the database to come online) and
# fetch bashio so this add-on can read its Home Assistant options.json like
# any other Supervisor add-on.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates default-mysql-client; \
    curl -L -f -s -o /tmp/bashio.tar.gz \
        "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz"; \
    mkdir -p /usr/src/bashio; \
    tar zxf /tmp/bashio.tar.gz --strip-components=1 -C /usr/src/bashio; \
    mv /usr/src/bashio/lib /usr/lib/bashio; \
    ln -s /usr/lib/bashio/bashio /usr/bin/bashio; \
    rm -rf /usr/src/bashio /tmp/bashio.tar.gz; \
    rm -rf /var/lib/apt/lists/*

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
