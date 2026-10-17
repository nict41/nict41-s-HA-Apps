ARG BUILD_FROM=ghcr.io/soldatigabriele/ascoltino-amd64:1.4.1
FROM ${BUILD_FROM}

# The upstream image already holds the bot, its Python dependencies and
# /run.sh. All this adds is patch.py's rewrite of bot.py and engines.py.
COPY patch.py /tmp/patch.py
RUN python3 /tmp/patch.py /app && rm /tmp/patch.py

LABEL \
    org.opencontainers.image.title="Transcribe-a-bot" \
    org.opencontainers.image.description="Telegram bot that transcribes voice notes and audio files locally" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps/tree/main/ascoltino"
