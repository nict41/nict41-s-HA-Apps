#!/usr/bin/env bashio
set -e

WORKERS=$(bashio::config 'workers')
MODEL_TTL=$(bashio::config 'model_ttl')
WORKER_TIMEOUT=$(bashio::config 'worker_timeout')
CACHE_FOLDER=/data/model-cache

export MACHINE_LEARNING_WORKERS="${WORKERS}"
export MACHINE_LEARNING_MODEL_TTL="${MODEL_TTL}"
export MACHINE_LEARNING_WORKER_TIMEOUT="${WORKER_TIMEOUT}"
export MACHINE_LEARNING_CACHE_FOLDER="${CACHE_FOLDER}"

# Persist downloaded models (multi-GB) across restarts and app updates,
# instead of the upstream image's default of an unpersisted /cache.
mkdir -p "${CACHE_FOLDER}"

bashio::log.info "Starting Immich Machine Learning (OpenVINO): workers=${WORKERS} model_ttl=${MODEL_TTL}s worker_timeout=${WORKER_TIMEOUT}s cache=${CACHE_FOLDER}"

# Sysfs hardware stats like this aren't namespaced per-container, so this
# is readable regardless of whether the /dev/dri device passthrough
# itself is working - a simple, independent "is the iGPU actually being
# used" signal right in this add-on's own log, instead of needing to SSH
# into the host or docker exec into the container to check it manually.
# Assumes a single GPU (card0), true for this add-on's target hardware.
monitor_gpu() {
    local freq_file=/sys/class/drm/card0/gt_act_freq_mhz
    if [ ! -r "${freq_file}" ]; then
        bashio::log.warning "GPU frequency file not visible inside this container (${freq_file}); check it on the host instead."
        return
    fi
    bashio::log.info "Watching iGPU activity (${freq_file}) - logs a line here whenever it's actually busy."
    local freq
    while sleep 10; do
        freq=$(cat "${freq_file}" 2>/dev/null || echo 0)
        if [ "${freq}" -gt 0 ] 2>/dev/null; then
            bashio::log.info "iGPU active: ${freq} MHz (OpenVINO is using the GPU)"
        fi
    done
}
monitor_gpu &

# Not exec'd until here: everything above just prepares the environment
# the upstream image's own entrypoint (tini, inherited unchanged) expects.
exec python -m immich_ml
