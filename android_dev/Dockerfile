ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:trixie
FROM ${BUILD_FROM}

# Pick up security fixes the distribution has published since the base image was built.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y && apt-get install -y --no-install-recommends \
    openjdk-21-jdk-headless python3 python3-pip python3-venv unzip curl git nodejs npm \
    procps util-linux coreutils ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt platformio==6.2.0
COPY app /app
COPY rootfs /
RUN chmod a+x /etc/services.d/android_dev/run

ENV PYTHONUNBUFFERED=1
WORKDIR /app
