ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# kiwix-tools ships fully static Linux builds, so kiwix-serve/kiwix-manage
# drop straight onto the Alpine base with no runtime libraries to match.
ARG BUILD_ARCH=amd64
ARG KIWIX_TOOLS_VERSION=3.8.2

COPY fetch-kiwix-tools.sh /tmp/fetch-kiwix-tools.sh
RUN apk add --no-cache \
        python3 \
        py3-pip \
    && case "${BUILD_ARCH}" in \
        amd64) KIWIX_ARCH="x86_64" ;; \
        aarch64) KIWIX_ARCH="aarch64" ;; \
        *) echo "Unsupported architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac \
    && sh /tmp/fetch-kiwix-tools.sh "${KIWIX_TOOLS_VERSION}" "${KIWIX_ARCH}" \
    && rm /tmp/fetch-kiwix-tools.sh

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
