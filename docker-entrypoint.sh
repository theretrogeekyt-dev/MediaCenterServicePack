#!/bin/sh
set -e

# Default PUID/PGID to 1000 if not specified
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Create openmce group if it doesn't exist
if ! getent group openmce >/dev/null 2>&1; then
    groupadd -g "$PGID" -o openmce 2>/dev/null || true
fi

# Create openmce user if it doesn't exist
if ! id -u openmce >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -o -d /config -s /bin/sh openmce 2>/dev/null || true
fi

# Ensure permissions on internal config and cache directories
mkdir -p /config/cache /media/movies /media/music
chown -R "$PUID:$PGID" /config

# Execute target command as the configured non-root user
exec gosu "$PUID:$PGID" "$@"

