#!/usr/bin/env bash
set -e

echo "Starting Tesla Fleet Setup add-on..."

# Ensure data directory exists for persistent key storage
mkdir -p /data/keys

exec python3 /opt/tesla-setup/server.py
