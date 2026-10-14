ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# ffmpeg does the container remux and thumbnail embedding; aria2 is the
# optional external downloader; mosquitto-clients is unused (MQTT is done in
# Python via paho) but python3/py3-pip carry the app itself.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        ffmpeg \
        aria2

COPY app/requirements.txt /app/requirements.txt
# --break-system-packages: Alpine's python3 marks itself "externally managed"
# (PEP 668); this is an add-on container, not a shared system, so it's safe.
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY rootfs /

RUN chmod a+x /etc/services.d/youtube_music_sync/run

WORKDIR /app
ENV PYTHONUNBUFFERED=1

LABEL \
    org.opencontainers.image.title="YouTube Music Sync" \
    org.opencontainers.image.description="Mirrors public YouTube playlists into a music library as audio files, with title+duration duplicate detection" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
