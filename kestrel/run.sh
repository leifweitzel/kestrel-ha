#!/usr/bin/with-contenv bashio
export STREAM_URL="$(bashio::config 'stream_url')"
export START_TIME="$(bashio::config 'start_time')"
export DURATION_MIN="$(bashio::config 'duration_minutes')"
export KEEP_HOURS="$(bashio::config 'keep_hours')"
export TZ_NAME="$(bashio::config 'timezone')"
export DELAY_SECONDS="$(bashio::config 'delay_seconds')"
export PART1_TO="$(bashio::config 'part1_to')"
export PART2_TO="$(bashio::config 'part2_to')"
export PART2_SWITCH="$(bashio::config 'part2_switch')"
export REC_DIR="/media/kestrel"
if [ -z "${STREAM_URL}" ]; then
  bashio::log.warning "stream_url is empty - set it in the configuration."
  sleep infinity
fi
exec python3 -u /recorder.py
