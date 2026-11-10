ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23

# kiwix-serve and kiwix-manage come from Kiwix's official multi-arch image on
# GHCR (the same static binaries as their download.kiwix.org tarballs, which
# are served by a mirror that manages only tens of KB/s). Pinned by digest;
# to upgrade, change the tag and digest here and in docker/Dockerfile.
FROM ghcr.io/kiwix/kiwix-tools:3.8.2@sha256:2914a200ff64e0861c82733010eb7583d17334dde4d7248b9d9aa7b2f7b5a36d AS kiwix-tools

FROM ${BUILD_FROM}

COPY --from=kiwix-tools /usr/local/bin/kiwix-serve /usr/local/bin/kiwix-manage /usr/bin/
RUN apk add --no-cache \
        python3 \
        py3-pip \
    && kiwix-serve --version

COPY app/requirements.txt /app/requirements.txt
# --break-system-packages: Alpine's python3 marks itself "externally managed"
# (PEP 668); this is an add-on container, not a shared system, so it's safe.
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY rootfs /

RUN chmod a+x /etc/services.d/kiwix/run

WORKDIR /app
ENV PYTHONUNBUFFERED=1

LABEL \
    org.opencontainers.image.title="Kiwix" \
    org.opencontainers.image.description="Browse, download and serve offline Wikipedia (and other ZIM) archives from NAS storage" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
