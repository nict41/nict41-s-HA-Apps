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

# Bundled display fonts (SIL OFL) from a pinned commit of google/fonts, each
# with its licence saved as /app/fonts/<dir>-OFL.txt. Brackets in the file
# names are %5B / %5D in the URLs (raw names on disk). One RUN: any failed
# download fails the build.
ARG FONTS_COMMIT=35e77c3854970a550d9894a85aba21420813a019
RUN mkdir -p /app/fonts \
    && set -f \
    && for spec in \
        "bebasneue BebasNeue-Regular.ttf BebasNeue-Regular.ttf" \
        "orbitron Orbitron[wght].ttf Orbitron%5Bwght%5D.ttf" \
        "sharetechmono ShareTechMono-Regular.ttf ShareTechMono-Regular.ttf" \
        "oswald Oswald[wght].ttf Oswald%5Bwght%5D.ttf" \
        "jetbrainsmono JetBrainsMono[wght].ttf JetBrainsMono%5Bwght%5D.ttf" \
        "nunito Nunito[wght].ttf Nunito%5Bwght%5D.ttf" \
        "playfairdisplay PlayfairDisplay[wght].ttf PlayfairDisplay%5Bwght%5D.ttf" \
        "pressstart2p PressStart2P-Regular.ttf PressStart2P-Regular.ttf" \
        "robotocondensed RobotoCondensed[wght].ttf RobotoCondensed%5Bwght%5D.ttf" \
        "audiowide Audiowide-Regular.ttf Audiowide-Regular.ttf" \
    ; do \
        set -- $spec; dir=$1; file=$2; url_file=$3; \
        wget -q -O "/app/fonts/$file" \
            "https://raw.githubusercontent.com/google/fonts/${FONTS_COMMIT}/ofl/$dir/$url_file" \
        && wget -q -O "/app/fonts/$dir-OFL.txt" \
            "https://raw.githubusercontent.com/google/fonts/${FONTS_COMMIT}/ofl/$dir/OFL.txt" \
        || exit 1; \
    done

COPY app /app
COPY integration /app/integration
COPY rootfs /

RUN chmod a+x /etc/services.d/tube_display/run

WORKDIR /app
ENV PYTHONUNBUFFERED=1

LABEL \
    org.opencontainers.image.title="Tube Display" \
    org.opencontainers.image.description="Renders Home Assistant information into pictures and pushes them to an IPSTube clock's six panel displays" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps/tree/main/tube_display" \
    org.opencontainers.image.licenses="MIT"
