ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        python3 \
        py3-pip \
        ffmpeg

COPY app/requirements.txt /app/requirements.txt
# --break-system-packages: Alpine's python3 marks itself "externally managed"
# (PEP 668); this is an add-on container, not a shared system, so it's safe.
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY rootfs /

RUN chmod a+x /etc/services.d/timelapse/run

WORKDIR /app
ENV PYTHONUNBUFFERED=1

LABEL \
    org.opencontainers.image.title="Print Timelapse" \
    org.opencontainers.image.description="Captures 3D print frame timelapses and compiles them into archived GIFs" \
    org.opencontainers.image.source="https://github.com/nict41/ha3dprinttimelapse" \
    org.opencontainers.image.licenses="MIT"
