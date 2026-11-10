# Standalone Docker image for the Kiwix manager - the same app as the Home
# Assistant add-on one directory up, packaged for a plain Docker host.
#
# Build from the kiwix/ directory so the shared app code is in context:
#     docker build -f kiwix/docker/Dockerfile -t kiwix-manager kiwix
# kiwix-serve and kiwix-manage come from Kiwix's official multi-arch image on
# GHCR (the same static binaries as their download.kiwix.org tarballs, which
# are served by a mirror that manages only tens of KB/s). Pinned by digest;
# to upgrade, change the tag and digest here and in ../Dockerfile.
FROM ghcr.io/kiwix/kiwix-tools:3.8.2@sha256:2914a200ff64e0861c82733010eb7583d17334dde4d7248b9d9aa7b2f7b5a36d AS kiwix-tools

FROM alpine:3.23

COPY --from=kiwix-tools /usr/local/bin/kiwix-serve /usr/local/bin/kiwix-manage /usr/bin/
RUN apk add --no-cache \
        python3 \
        py3-pip \
        tzdata \
        su-exec \
        shadow \
        wget \
    && kiwix-serve --version

COPY app/requirements.txt /app/requirements.txt
# --break-system-packages: Alpine's python3 marks itself "externally managed"
# (PEP 668); this is a single-purpose container, not a shared system.
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod a+x /entrypoint.sh \
    && addgroup -g 1000 kiwix \
    && adduser -D -H -u 1000 -G kiwix -s /sbin/nologin kiwix \
    && mkdir -p /data /zim \
    && chown kiwix:kiwix /data /zim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    KIWIX_PLATFORM=docker \
    DATA_DIR=/data \
    ZIM_PATH=/zim \
    PORT=8099 \
    PUID=1000 \
    PGID=1000 \
    TZ=UTC

VOLUME ["/data", "/zim"]
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- "http://127.0.0.1:${PORT}/healthz" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]

LABEL \
    org.opencontainers.image.title="Kiwix Manager" \
    org.opencontainers.image.description="Browse, download and serve offline Wikipedia (and other ZIM) archives" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
