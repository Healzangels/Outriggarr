#!/bin/sh
# Outriggarr container entrypoint.
#   PUID/PGID   run the app as this uid/gid (match Sonarr/Radarr so they can move staged files)
#   UMASK       applied before the app starts (default 022)
#   OUTRIGGARR_YTDLP_UPDATE=1   upgrade yt-dlp to the latest release on every start (needs network)
set -e

umask "${UMASK:-022}"

if [ "${OUTRIGGARR_YTDLP_UPDATE:-0}" = "1" ]; then
    echo "entrypoint: upgrading yt-dlp (OUTRIGGARR_YTDLP_UPDATE=1)"
    uv pip install --python /app/.venv/bin/python --upgrade yt-dlp || echo "entrypoint: yt-dlp upgrade failed; continuing with the bundled version"
fi

if [ "$(id -u)" = "0" ] && [ -n "${PUID:-}" ]; then
    PGID="${PGID:-$PUID}"
    chown "$PUID:$PGID" /config 2>/dev/null || true
    echo "entrypoint: running as uid=$PUID gid=$PGID umask=$(umask)"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
