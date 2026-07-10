ARG BUILD_VERSION
FROM ghcr.io/immich-app/immich-machine-learning:${BUILD_VERSION}

ARG BASHIO_VERSION="0.17.5"

# This only adds bashio, so the entrypoint can read Home Assistant's
# options.json and translate it into the upstream image's own
# MACHINE_LEARNING_* env vars. The Immich Machine Learning application
# itself is used completely unmodified - same image, same tag as
# BUILD_VERSION (the app version above), nothing rebuilt or recompiled.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates jq; \
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
