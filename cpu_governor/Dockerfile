ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# docker-cli: the add-on's own container can't get a writable /sys from
# Supervisor (see README), so it falls back to managing a genuinely
# --privileged --pid=host sidecar container via the host Docker API. That
# needs the docker client. bashio is already provided by the HA base image.
RUN apk add --no-cache docker-cli

COPY rootfs /

RUN chmod a+x /etc/services.d/cpu_governor/run

LABEL \
    org.opencontainers.image.title="CPU Performance Governor" \
    org.opencontainers.image.description="Sets the host CPU cpufreq scaling governor and re-applies it on every host boot" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps/tree/main/cpu_governor" \
    org.opencontainers.image.licenses="MIT"
