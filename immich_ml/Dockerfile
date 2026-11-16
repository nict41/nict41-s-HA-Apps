ARG BUILD_VERSION
FROM ghcr.io/immich-app/immich-machine-learning:${BUILD_VERSION}

ARG BASHIO_VERSION="0.19.0"

# This only adds bashio, so the entrypoint can read Home Assistant's
# options.json and translate it into the upstream image's own
# MACHINE_LEARNING_* env vars. The Immich Machine Learning application
# itself is used completely unmodified - same image, same tag as
# BUILD_VERSION (the app version above), nothing rebuilt or recompiled.
# Pick up security fixes the distribution has published since the base image was built.
RUN set -eu; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; \
    apt-get install -y --no-install-recommends curl ca-certificates jq; \
    curl -L -f -s -o /tmp/bashio.tar.gz \
        "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz"; \
    mkdir -p /usr/src/bashio; \
    tar zxf /tmp/bashio.tar.gz --strip-components=1 -C /usr/src/bashio; \
    mv /usr/src/bashio/lib /usr/lib/bashio; \
    ln -s /usr/lib/bashio/bashio /usr/bin/bashio; \
    rm -rf /usr/src/bashio /tmp/bashio.tar.gz; \
    rm -rf /var/lib/apt/lists/*

# anyio CVE-2026-63374 (CRITICAL, TLS certificate spoofing). The image pins
# anyio 4.2.0; upstream main has already moved to 4.14.2, but no release
# carries it yet. Swap it into the app's venv with the base image's system pip
# (the venv has none), pinned by hash and without touching anything else.
# Remove this block once an Immich release ships anyio >= 4.14.2, or it will
# hold anyio at 4.14.2.
RUN set -eu; \
    printf 'anyio==4.14.2 --hash=sha256:9f505dda5ac9f0c8309b5e8bd445a8c2bf7246f3ce950121e45ea15bc41d1494\n' > /tmp/anyio.txt; \
    /usr/local/bin/python3 -m pip --python /opt/venv/bin/python install --no-cache-dir \
        --no-deps --require-hashes --disable-pip-version-check -r /tmp/anyio.txt; \
    rm -f /tmp/anyio.txt; \
    /opt/venv/bin/python -c "import anyio, importlib.metadata as m; v = m.version('anyio'); assert v == '4.14.2', v; print('anyio', v)"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
