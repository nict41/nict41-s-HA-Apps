ARG BUILD_FROM=ghcr.io/soldatigabriele/ascoltino-amd64:1.4.1
FROM ${BUILD_FROM}

# Pick up security fixes the distribution has published since the base image
# was built, and refresh the CA bundle the image's Python uses. Runs as root
# (the image never switches USER) so apt and pip both work.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --upgrade certifi

# The upstream image already holds the bot, its Python dependencies and
# /run.sh. All this adds is patch.py's rewrite of bot.py and engines.py.
COPY patch.py /tmp/patch.py
RUN python3 /tmp/patch.py /app && rm /tmp/patch.py

LABEL \
    org.opencontainers.image.title="Transcribe-a-bot" \
    org.opencontainers.image.description="Telegram bot that transcribes voice notes and audio files locally" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps/tree/main/ascoltino"
