#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

getent group "$PGID" >/dev/null 2>&1 || groupadd -g "$PGID" kshow
getent passwd "$PUID" >/dev/null 2>&1 || \
    useradd -u "$PUID" -g "$PGID" -M -d /config -s /usr/sbin/nologin kshow

# Only /config is ours; /tv and /downloads are the user's library and are never chowned.
mkdir -p /config
chown "$PUID:$PGID" /config

exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups \
    uvicorn kshow.main:create_app --factory --host 0.0.0.0 --port 8080
