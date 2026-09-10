ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# God's Eye View has no release artefact and no published image, so the source
# is fetched at a pinned commit and its dependencies are installed here. Bump
# GEV_COMMIT (in build.yaml) to move to a newer upstream revision.
ARG GEV_COMMIT=759652207fd1279ece97f0f19af566feb9a82146

ENV GEV_HOME=/opt/gods-eye-view \
    PUPPETEER_SKIP_DOWNLOAD=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

# Alpine 3.23 ships Node 24.18, which satisfies the app's ">=24.14 <25" engine
# range. nginx must carry http_sub_module: the ingress shim is injected into
# index.html with sub_filter, so a build without it would fail at runtime.
RUN apk add --no-cache nodejs npm nginx \
    && node --version \
    && nginx -V 2>&1 | grep -q -- '--with-http_sub_module'

RUN mkdir -p "${GEV_HOME}" \
    && wget -qO- "https://codeload.github.com/bilawalsidhu/gods-eye-view/tar.gz/${GEV_COMMIT}" \
        | tar -xz --strip-components=1 -C "${GEV_HOME}" \
    && cd "${GEV_HOME}" \
    && npm ci --loglevel=error

# Pre-bundle Cesium and friends now rather than on the first page load, which
# would otherwise run esbuild over ~200 MB of dependencies while the user is
# waiting. Non-fatal: a miss here only costs that one-time wait.
RUN cd "${GEV_HOME}" \
    && (node node_modules/vite/bin/vite.js optimize --force || \
        echo "Dependency pre-bundle skipped; it will run on first page load")

COPY rootfs /

RUN chmod a+x \
        /etc/s6-overlay/s6-rc.d/init-gev/run \
        /etc/s6-overlay/s6-rc.d/gev/run \
        /etc/s6-overlay/s6-rc.d/gev/finish \
        /etc/s6-overlay/s6-rc.d/nginx/run \
        /etc/s6-overlay/s6-rc.d/nginx/finish

WORKDIR ${GEV_HOME}

LABEL \
    org.opencontainers.image.title="God's Eye View" \
    org.opencontainers.image.description="Real-time intelligence console for planet Earth" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
