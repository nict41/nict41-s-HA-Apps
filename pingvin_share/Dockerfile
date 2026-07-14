FROM smp46/pingvin-share-x:v1.21.0

ARG BASHIO_VERSION="0.17.5"

# Pingvin Share X's own image is Alpine-based and doesn't ship bash, so
# bashio (needed to read this add-on's options.json) is vendored from
# source, same as the WordPress app in this repository.
RUN set -eu; \
    apk add --no-cache bash curl ca-certificates jq; \
    curl -L -f -s -o /tmp/bashio.tar.gz \
        "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz"; \
    mkdir -p /usr/src/bashio; \
    tar zxf /tmp/bashio.tar.gz --strip-components=1 -C /usr/src/bashio; \
    mv /usr/src/bashio/lib /usr/lib/bashio; \
    ln -s /usr/lib/bashio/bashio /usr/bin/bashio; \
    rm -rf /usr/src/bashio /tmp/bashio.tar.gz

# Always run as root inside the container - Home Assistant add-ons don't
# have the host-side "which uid owns my bind mounts" problem that PUID/PGID
# exists to solve (Supervisor already owns /data), and it keeps the
# upstream image's own user-creation step (scripts/docker/create-user.sh,
# still the ENTRYPOINT) a no-op.
ENV PUID=0
ENV PGID=0

COPY run.sh /run.sh
RUN chmod a+x /run.sh

# The base image's ENTRYPOINT (create-user.sh) is kept so PUID/PGID handling
# still runs; it execs this CMD as its "$@" once done.
CMD [ "/run.sh" ]
