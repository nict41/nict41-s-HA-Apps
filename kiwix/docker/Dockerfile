# Standalone Docker image for the Kiwix manager - the same app as the Home
# Assistant add-on one directory up, packaged for a plain Docker host.
#
# Build from the kiwix/ directory so the shared app code is in context:
#     docker build -f kiwix/docker/Dockerfile -t kiwix-manager kiwix
FROM alpine:3.23

# kiwix-tools ships fully static Linux builds, so kiwix-serve/kiwix-manage
# drop straight onto Alpine with no runtime libraries to match. TARGETARCH is
# filled in by buildx (amd64 / arm64).
ARG TARGETARCH
ARG KIWIX_TOOLS_VERSION=3.8.2

RUN apk add --no-cache \
        python3 \
        py3-pip \
        tzdata \
        su-exec \
        shadow \
        wget \
    && case "${TARGETARCH:-amd64}" in \
        amd64) KIWIX_ARCH="x86_64" ;; \
        arm64) KIWIX_ARCH="aarch64" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && wget -qO /tmp/kiwix-tools.tar.gz \
        "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-${KIWIX_ARCH}-${KIWIX_TOOLS_VERSION}.tar.gz" \
    && tar -xzf /tmp/kiwix-tools.tar.gz -C /tmp \
    && install -m 0755 \
        "/tmp/kiwix-tools_linux-${KIWIX_ARCH}-${KIWIX_TOOLS_VERSION}/kiwix-serve" \
        "/tmp/kiwix-tools_linux-${KIWIX_ARCH}-${KIWIX_TOOLS_VERSION}/kiwix-manage" \
        /usr/bin/ \
    && rm -rf /tmp/kiwix-tools.tar.gz "/tmp/kiwix-tools_linux-${KIWIX_ARCH}-${KIWIX_TOOLS_VERSION}" \
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
