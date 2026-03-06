#!/usr/bin/env bash
set -e

echo "Starting Tesla Fleet Setup add-on..."

# Ensure data directory exists for persistent key storage
mkdir -p /data/keys

# Debug: log whether SUPERVISOR_TOKEN is available
if [ -n "$SUPERVISOR_TOKEN" ]; then
    echo "SUPERVISOR_TOKEN is set (length: ${#SUPERVISOR_TOKEN})"
else
    echo "WARNING: SUPERVISOR_TOKEN is NOT set"
    # Try s6-overlay container environment
    if [ -f /run/s6-container-environment/SUPERVISOR_TOKEN ]; then
        export SUPERVISOR_TOKEN
        SUPERVISOR_TOKEN=$(cat /run/s6-container-environment/SUPERVISOR_TOKEN)
        echo "Loaded SUPERVISOR_TOKEN from s6 container environment"
    elif [ -f /var/run/s6/container_environment/SUPERVISOR_TOKEN ]; then
        export SUPERVISOR_TOKEN
        SUPERVISOR_TOKEN=$(cat /var/run/s6/container_environment/SUPERVISOR_TOKEN)
        echo "Loaded SUPERVISOR_TOKEN from s6 container_environment"
    fi
    # Try legacy HASSIO_TOKEN
    if [ -z "$SUPERVISOR_TOKEN" ] && [ -n "$HASSIO_TOKEN" ]; then
        export SUPERVISOR_TOKEN="$HASSIO_TOKEN"
        echo "Using legacy HASSIO_TOKEN as SUPERVISOR_TOKEN"
    fi
fi

exec python3 /opt/tesla-setup/server.py
