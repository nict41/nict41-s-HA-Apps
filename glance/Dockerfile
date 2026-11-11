ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.23
FROM glanceapp/glance:v0.8.8 AS glance

FROM ${BUILD_FROM}

# Pick up security fixes the distribution has published since the base image was built.
RUN apk upgrade --no-cache && apk add --no-cache nginx

COPY --from=glance /app/glance /usr/bin/glance

COPY config/glance.yml /etc/glance/glance.yml
COPY rootfs /
