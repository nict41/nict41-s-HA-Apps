ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:trixie
FROM ${BUILD_FROM}

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jdk-headless python3 python3-pip python3-venv unzip curl git \
    procps util-linux coreutils ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt
COPY app /app
COPY rootfs /
RUN chmod a+x /etc/services.d/android_dev/run

ENV PYTHONUNBUFFERED=1
WORKDIR /app
