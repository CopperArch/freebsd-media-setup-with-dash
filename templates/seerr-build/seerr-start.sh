#!/bin/sh
# seerr-start.sh — env-setting exec wrapper for the seerr rc.d service.
# daemon(8) has no flag for setting environment variables on the child it
# execs, so this is the standard rc.d pattern for an app that needs some:
# rc.d calls THIS script (as the target user, via daemon -u), which sets
# what Seerr needs and execs the real process.
set -eu
export NODE_ENV=production
export PORT="${SEERR_PORT:-5055}"
cd /usr/local/jellyseerr
exec /usr/local/bin/node dist/index.js
