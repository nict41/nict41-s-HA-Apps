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

# Not exec'd until here: everything above just prepares the environment
# the upstream image's own entrypoint (tini, inherited unchanged) expects.
exec python -m immich_ml
