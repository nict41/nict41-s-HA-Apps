ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM glanceapp/glance:v0.8.4 AS glance

FROM ${BUILD_FROM}

RUN apk add --no-cache nginx

COPY --from=glance /app/glance /usr/bin/glance

COPY config/glance.yml /etc/glance/glance.yml
COPY rootfs /
