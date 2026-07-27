ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# mosquitto-clients provides mosquitto_pub, used to publish the MQTT
# discovery config and state/attributes. bashio (for reading options and the
# Supervisor-injected MQTT service) is already provided by the HA base image.
RUN apk add --no-cache mosquitto-clients

COPY rootfs /

RUN chmod a+x /etc/services.d/nas1_usb_watcher/run

LABEL \
    org.opencontainers.image.title="NAS1 USB Link Watcher" \
    org.opencontainers.image.description="Publishes a Home Assistant MQTT binary_sensor tracking the NAS1 drive's USB link state" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps/tree/main/nas1_usb_watcher" \
    org.opencontainers.image.licenses="MIT"
