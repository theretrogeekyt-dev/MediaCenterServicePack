#!/bin/sh
set -e

# Default PUID/PGID to 1000 if not specified
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# If already running as non-root (e.g. rootless container or docker run -u), execute directly
if [ "$(id -u)" != "0" ]; then
    mkdir -p /config/cache 2>/dev/null || true
    exec "$@"
fi

# If running as root and PUID/PGID requested root (0), execute directly as root
if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    mkdir -p /config/cache 2>/dev/null || true
    chmod -R 777 /config 2>/dev/null || true
    umask 0000
    exec "$@"
fi

# Create openmce group if it doesn't exist
if ! getent group openmce >/dev/null 2>&1; then
    groupadd -g "$PGID" -o openmce 2>/dev/null || true
fi

# Create openmce user if it doesn't exist
if ! id -u openmce >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -o -d /config -s /bin/sh openmce 2>/dev/null || true
fi

# Ensure permissions on internal config and cache directories
mkdir -p /config/cache 2>/dev/null || true

# Grant full read/write permissions to /config so mounted volumes work seamlessly on all hosts
chmod -R 777 /config 2>/dev/null || chmod -R u+rwx,g+rwx,o+rwx /config 2>/dev/null || true
chown -R "$PUID:$PGID" /config 2>/dev/null || true

# Set umask so newly created files (guide.mxf, settings.json) are writable
umask 0000

# Execute target command as the configured non-root user
exec gosu "$PUID:$PGID" "$@"
