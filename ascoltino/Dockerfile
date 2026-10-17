ARG BUILD_FROM=ghcr.io/soldatigabriele/ascoltino-amd64:1.4.1
FROM ${BUILD_FROM}

# The upstream image already holds the bot, its Python dependencies and
# /run.sh. All this adds is patch_audio.py's rewrite of bot.py.
COPY patch_audio.py /tmp/patch_audio.py
RUN python3 /tmp/patch_audio.py /app/bot.py && rm /tmp/patch_audio.py

LABEL \
    org.opencontainers.image.title="Ascoltino" \
    org.opencontainers.image.description="Telegram bot that transcribes voice notes and audio files locally" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps/tree/main/ascoltino"
