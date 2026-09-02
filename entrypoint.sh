#!/bin/sh
# Outriggarr container entrypoint.
#   PUID/PGID   run the app as this uid/gid (match Sonarr/Radarr so they can move staged files)
#   UMASK       applied before the app starts (default 022)
#   OUTRIGGARR_STAGING_DIR   where downloads are staged (default /staging). Mount either that
#               folder alone, or the whole data share as /data and set this to /data/<sub>.
#   OUTRIGGARR_YTDLP_UPDATE=1   upgrade yt-dlp to the latest release on every start (needs network)
set -e

umask "${UMASK:-022}"

if [ "${OUTRIGGARR_YTDLP_UPDATE:-0}" = "1" ]; then
    echo "entrypoint: upgrading yt-dlp + yt-dlp-ejs (OUTRIGGARR_YTDLP_UPDATE=1)"
    # ejs is pinned by yt-dlp per release: upgrade them together or the JS challenge
    # solver stops loading. Cache under /tmp so nothing root-owned lands in /config.
    UV_CACHE_DIR=/tmp/uv-cache uv pip install --python /app/.venv/bin/python --upgrade yt-dlp yt-dlp-ejs \
        || echo "entrypoint: yt-dlp upgrade failed; continuing with the bundled version"
    /app/.venv/bin/python -c "import yt_dlp, importlib.metadata as m; print('entrypoint: yt-dlp', yt_dlp.version.__version__, 'yt-dlp-ejs', m.version('yt-dlp-ejs'))" || true
fi

if [ "$(id -u)" = "0" ] && [ -n "${PUID:-}" ]; then
    PGID="${PGID:-$PUID}"
    # Recursive: a container first run as root (no PUID) leaves app.db/.deno root-owned.
    chown -R "$PUID:$PGID" /config 2>/dev/null || true
    # A bind-mount source that did not exist gets created by Docker as root:root 755,
    # which the unprivileged app cannot write into. Create/take ownership of the staging
    # directory only (never its parent share, never its contents — Sonarr/Radarr may own
    # files in flight).
    STAGING="${OUTRIGGARR_STAGING_DIR:-/staging}"
    mkdir -p "$STAGING" 2>/dev/null || true
    if ! setpriv --reuid="$PUID" --regid="$PGID" --clear-groups sh -c "test -w '$STAGING'"; then
        echo "entrypoint: $STAGING is not writable by $PUID:$PGID — chowning it"
        chown "$PUID:$PGID" "$STAGING" 2>/dev/null || echo "entrypoint: WARNING could not chown $STAGING; downloads will fail"
    fi
    echo "entrypoint: running as uid=$PUID gid=$PGID umask=$(umask)"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
