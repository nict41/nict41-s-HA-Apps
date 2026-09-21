# ghcr.io rather than lscr.io deliberately. lscr.io is a redirector in front of
# the same images and rate-limits shared build infrastructure: it answered a CI
# build with 429 before the base image was even resolved. Pulling the origin
# avoids that here and on the machine Supervisor builds this on.
ARG BUILD_FROM=ghcr.io/linuxserver/baseimage-kasmvnc:ubuntunoble
FROM ${BUILD_FROM}

# ElegooSlicer ships only a Linux AppImage — no repository, no container. It is
# fetched at a pinned version and unpacked at build time, so the runtime needs
# no FUSE and no privileges to mount it.
ARG ELEGOO_VERSION=1.5.3.5

# START_DOCKER=false is load-bearing. The base ships Docker-in-Docker and its
# service starts dockerd whenever /dev/cpu_dma_latency exists — which it does
# here, as a side effect of the device access granted for the iGPU. dockerd
# then fails in an unprivileged add-on (no modprobe, cannot mount
# /sys/kernel/security), s6 restarts it, and the add-on log fills with the
# same twelve lines forever. Nothing here wants Docker; with this set false the
# service parks on sleep infinity instead.
ENV TITLE="ElegooSlicer" \
    HOME=/config \
    PUID=0 \
    PGID=0 \
    START_DOCKER=false

# The AppImage bundles only ten libraries and links against thirty-five more.
# This list is not a guess: every DT_NEEDED entry in the payload was read,
# those the bundle ships were subtracted, and each remaining soname was
# resolved to its owning package through the Ubuntu Contents index. See DOCS.md.
#
# Two things constrain the base, both learned the hard way:
#
#   - The binary wants the webkit2gtk **4.1** / libsoup **3** pair, not
#     4.0 / libsoup2.4. They are not interchangeable.
#   - It requires GLIBC_2.38 and GLIBCXX_3.4.32. Ubuntu 22.04 has glibc 2.35
#     and libstdc++ 12 (GLIBCXX_3.4.30) and therefore CANNOT run it, which is
#     not fixable by installing anything. 24.04 has glibc 2.39 and libstdc++
#     14. Hence ubuntunoble, and hence the t64-renamed package names below.

# The base image carries a NodeSource node_18.x apt source that no longer
# exists — it 404s, apt-get update exits non-zero on it, and the install behind
# the && never runs. Nothing here wants Node, so drop any source pointing at it
# rather than papering over a failing update with `|| true`, which would hide a
# real apt failure later.
RUN grep -rlZ 'deb\.nodesource\.com' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null \
        | xargs -0 -r rm -f \
    && echo "Removed stale NodeSource apt source"

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        jq \
        libatk1.0-0t64 \
        libcairo-gobject2 \
        libcairo2 \
        libdbus-1-3 \
        libegl1 \
        libfontconfig1 \
        libgdk-pixbuf-2.0-0 \
        libgl1 \
        libgl1-mesa-dri \
        libglib2.0-0t64 \
        libglu1-mesa \
        libglx0 \
        libgstreamer-plugins-base1.0-0 \
        libgstreamer1.0-0 \
        libgtk-3-0t64 \
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
# Print whole ldd lines, not a field of them: ldd reports a missing library and
# a missing SYMBOL VERSION in different shapes, and taking $1 turned the latter
# into five copies of the binary's own path — a real failure reported so badly
# it looked like a bug in the check.
RUN out="$(LD_LIBRARY_PATH=/opt/elegoo-slicer/lib/orca-runtime:/opt/elegoo-slicer/bin \
        ldd /opt/elegoo-slicer/bin/elegoo-slicer 2>&1)" \
    && if printf '%s' "${out}" | grep -q 'not found'; then \
         echo "Unresolved libraries or symbol versions:"; \
         printf '%s\n' "${out}" | grep 'not found'; \
         exit 1; \
       fi \
    && echo "All libraries and symbol versions resolve"

# Upstream's launcher looks like it validates this itself — it has checks for a
# host OpenGL and WebKitGTK 4.1 runtime and a helpful error naming the packages
# to install. Do not rely on them: has_host_runtime_library() declares
# `local lib_name` and never assigns $1, so it greps ldconfig for a bare space,
# matches anything, and always reports the library present. Verified by running
# it on a machine with no webkit2gtk-4.1 at all, where it passes happily.
# The ldd gate above is therefore the only thing standing between a missing
# library and a desktop that opens to nothing.

# The base hardcodes the Xvnc desktop size. Make it settable, and fail the build
# if upstream ever changes that line rather than silently losing the option.
# The service script runs under with-contenv, so the variable resolves at start.
RUN grep -q -- '-geometry 1024x768' /etc/s6-overlay/s6-rc.d/svc-kasmvnc/run \
    && sed -i 's|-geometry 1024x768|-geometry ${KASM_GEOMETRY:-1440x900}|' \
        /etc/s6-overlay/s6-rc.d/svc-kasmvnc/run \
    && grep -q 'KASM_GEOMETRY' /etc/s6-overlay/s6-rc.d/svc-kasmvnc/run \
    && echo "Desktop size is now configurable"

# Do NOT symlink /config anywhere. The base image declares `VOLUME /config`, so
# Docker puts an ANONYMOUS volume there — one that Home Assistant discards and
# recreates empty every time it rebuilds the container, which it does on every
# update and restart. A symlink to /data is simply masked by that mount, and
# everything the slicer writes to its home directory goes into throwaway
# storage: printer selection, presets, the lot, gone on each restart.
#
# The fix is `addon_config:rw` in config.yaml, which bind-mounts a real host
# folder at /config. An explicit mount takes precedence over the image's
# VOLUME, so the anonymous volume is never created. /config just needs to exist
# as an ordinary directory for that to land on.
RUN mkdir -p /config

# Put the mobile page rewrites on the published port too. The ingress listener
# has its own copy, but a viewport meta is ignored inside Home Assistant's
# iframe and only does anything where this add-on is the top-level document —
# which is precisely the published port, the one place the rewrite was NOT
# being applied. init-nginx copies this file at startup and only substitutes
# ports and SUBFOLDER, so patching it here survives that.
RUN grep -q 'proxy_pass               http://127.0.0.1:6900;' /defaults/default.conf \
    && sed -i 's|proxy_pass               http://127.0.0.1:6900;|proxy_pass               http://127.0.0.1:6900;\n    proxy_set_header         Accept-Encoding "";\n    sub_filter "maximum-scale=1.0, user-scalable=no" "maximum-scale=5.0, user-scalable=yes";\n    sub_filter_once on;|g' /defaults/default.conf \
    && [ "$(grep -c 'user-scalable=yes' /defaults/default.conf)" = "2" ] \
    && echo "Published port serves a zoomable page" 

COPY rootfs /

RUN chmod a+x /defaults/autostart /custom-cont-init.d/*

LABEL \
    org.opencontainers.image.title="ElegooSlicer" \
    org.opencontainers.image.description="ElegooSlicer desktop, served in the browser" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
