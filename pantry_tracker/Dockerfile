ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# zbar: the C library pyzbar wraps via ctypes, for barcode decoding.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        zbar

COPY app/requirements.txt /app/requirements.txt
# --break-system-packages: Alpine's python3 marks itself "externally managed"
# (PEP 668); this is an add-on container, not a shared system, so it's safe.
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY rootfs /

RUN chmod a+x /etc/services.d/pantry_tracker/run

WORKDIR /app
ENV PYTHONUNBUFFERED=1

LABEL \
    org.opencontainers.image.title="Pantry Tracker" \
    org.opencontainers.image.description="Scan grocery barcodes to track your pantry and get notified before food expires" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
