ARG BUILD_FROM=lscr.io/linuxserver/baseimage-kasmvnc:ubuntujammy
FROM ${BUILD_FROM}

# ElegooSlicer ships only a Linux AppImage — no repository, no container. It is
# fetched at a pinned version and unpacked at build time, so the runtime needs
# no FUSE and no privileges to mount it.
ARG ELEGOO_VERSION=1.5.3.5

ENV TITLE="ElegooSlicer" \
    HOME=/config \
    PUID=0 \
    PGID=0

# The AppImage bundles only ten libraries and links against thirty-five more.
# This list is not a guess: every DT_NEEDED entry in the payload was read,
# those the bundle ships were subtracted, and each remaining soname was
# resolved to its owning package through the Ubuntu Contents index. See DOCS.md.
#
# Worth noting for anyone tempted to "modernise" it: the binary wants the
# webkit2gtk **4.1** / libsoup **3** pair, not 4.0 / libsoup2.4. Swapping those
# for the older generation produces an image that builds cleanly and then fails
# to start.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        jq \
        libatk1.0-0 \
        libcairo-gobject2 \
        libcairo2 \
        libdbus-1-3 \
        libegl1 \
        libfontconfig1 \
        libgdk-pixbuf-2.0-0 \
        libgl1 \
        libgl1-mesa-dri \
        libglib2.0-0 \
        libglu1-mesa \
        libglx0 \
        libgstreamer-plugins-base1.0-0 \
        libgstreamer1.0-0 \
        libgtk-3-0 \
        libharfbuzz0b \
        libice6 \
        libjavascriptcoregtk-4.1-0 \
        libopengl0 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libpangoft2-1.0-0 \
        libsecret-1-0 \
        libsm6 \
        libsoup-3.0-0 \
        libwayland-client0 \
        libwayland-egl1 \
        libwayland-server0 \
        libwebkit2gtk-4.1-0 \
        libx11-6 \
        libxext6 \
        libxkbcommon0 \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/ElegooSlicer.AppImage \
        "https://github.com/ELEGOO-3D/ElegooSlicer/releases/download/v${ELEGOO_VERSION}/ElegooSlicer_Linux_V${ELEGOO_VERSION}.AppImage" \
    && chmod +x /tmp/ElegooSlicer.AppImage \
    && cd /tmp \
    && ./ElegooSlicer.AppImage --appimage-extract > /dev/null \
    && mv /tmp/squashfs-root /opt/elegoo-slicer \
    && rm -f /tmp/ElegooSlicer.AppImage \
    && test -x /opt/elegoo-slicer/AppRun \
    && test -x /opt/elegoo-slicer/bin/elegoo-slicer

# Prove the library set is complete while the build can still fail loudly: a
# missing soname here is far cheaper to find than a desktop that opens to
# nothing.
#
# LD_LIBRARY_PATH has to mirror what libexec/elegoo-slicer-env sets, because
# six of the binary's DT_NEEDED entries (liblzma, libmspack, libz, libexpat,
# libagora_rtm_sdk, libaosl) ship inside the payload and its RUNPATH points at
# the absolute paths of the machine upstream built on. A bare ldd reports those
# as missing and would fail this build for no reason.
RUN missing="$(LD_LIBRARY_PATH=/opt/elegoo-slicer/lib/orca-runtime:/opt/elegoo-slicer/bin \
        ldd /opt/elegoo-slicer/bin/elegoo-slicer | awk '/not found/ {print $1}')" \
    && if [ -n "${missing}" ]; then \
         echo "Unresolved libraries in the image:"; echo "${missing}"; exit 1; \
       fi \
    && echo "All libraries resolve"

# Upstream's launcher looks like it validates this itself — it has checks for a
# host OpenGL and WebKitGTK 4.1 runtime and a helpful error naming the packages
# to install. Do not rely on them: has_host_runtime_library() declares
# `local lib_name` and never assigns $1, so it greps ldconfig for a bare space,
# matches anything, and always reports the library present. Verified by running
# it on a machine with no webkit2gtk-4.1 at all, where it passes happily.
# The ldd gate above is therefore the only thing standing between a missing
# library and a desktop that opens to nothing.

# The desktop's home is /config; point it at the add-on's persistent volume so
# printer profiles and slicing presets survive a restart or an update.
RUN rm -rf /config && ln -s /data /config

COPY rootfs /

RUN chmod a+x /defaults/autostart /custom-cont-init.d/*

LABEL \
    org.opencontainers.image.title="ElegooSlicer" \
    org.opencontainers.image.description="ElegooSlicer desktop, served in the browser" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
