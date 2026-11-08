ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        python3 \
        py3-pip \
        font-inter \
        font-dejavu

COPY app/requirements.txt /app/requirements.txt
# --break-system-packages: Alpine's python3 marks itself "externally managed"
# (PEP 668); this is an add-on container, not a shared system, so it's safe.
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Material Design Icons: pinned webfont + its CSS, and a generated
# name -> codepoint map so screens.yaml can say `mdi:robot`.
COPY app/tools/mdi_codepoints.py /app/tools/mdi_codepoints.py
ARG MDI_VERSION=7.4.47
RUN mkdir -p /app/mdi \
    && wget -q -O /app/mdi/materialdesignicons-webfont.ttf \
        "https://cdn.jsdelivr.net/npm/@mdi/font@${MDI_VERSION}/fonts/materialdesignicons-webfont.ttf" \
    && wget -q -O /app/mdi/materialdesignicons.css \
        "https://cdn.jsdelivr.net/npm/@mdi/font@${MDI_VERSION}/css/materialdesignicons.css" \
    && python3 /app/tools/mdi_codepoints.py \
        /app/mdi/materialdesignicons.css /app/mdi/codepoints.json \
    && rm /app/mdi/materialdesignicons.css

COPY app /app
COPY rootfs /

RUN chmod a+x /etc/services.d/tube_display/run

WORKDIR /app
ENV PYTHONUNBUFFERED=1

LABEL \
    org.opencontainers.image.title="Tube Display" \
    org.opencontainers.image.description="Renders Home Assistant information into pictures and pushes them to an IPSTube clock's six panel displays" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps/tree/main/tube_display" \
    org.opencontainers.image.licenses="MIT"
