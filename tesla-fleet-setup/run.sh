#!/usr/bin/env bash
set -eo pipefail

echo "Starting Tesla Fleet Setup add-on (version: ${BUILD_VERSION:-unknown})..."

# Ensure data directories exist with restrictive permissions
mkdir -p /data/keys /data/tls
chmod 700 /data/keys /data/tls

# Resolve SUPERVISOR_TOKEN from available sources
if [ -n "$SUPERVISOR_TOKEN" ]; then
    echo "SUPERVISOR_TOKEN is set"
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

# Check for tesla-http-proxy binary
if command -v tesla-http-proxy &>/dev/null || [ -x /usr/local/bin/tesla-http-proxy ]; then
    echo "tesla-http-proxy binary: available"
else
    echo "WARNING: tesla-http-proxy binary not found"
fi

exec python3 /opt/tesla-setup/server.py
