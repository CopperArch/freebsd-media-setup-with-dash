#!/bin/sh
# install-nextcloud.sh — bring up the Nextcloud + PostgreSQL jail pair.
#
# Runs ON THE HOST and drives both jails via `bastille cmd`. Rendered from the
# profile, so it carries the DB + admin credentials — the installer writes it
# 0600 and deletes it after running. Idempotent: re-running skips an existing
# install and just re-asserts config.
#
# This is the FreeBSD-native equivalent of `docker compose up` for the Immich
# stack it replaces: PostgreSQL (its own jail), php-fpm + redis + Caddy (the
# app jail), then `occ maintenance:install` and the Photos/Memories apps.
set -eu

DBJ=nextcloud-db
APPJ=nextcloud
WEBROOT=/usr/local/www/nextcloud
NCDATA=/data/nextcloud-data          # nullfs mount inside the app jail
PGDATA=/var/db/postgres/data17
DB_HOST="{{NC_DB_HOST}}"
APP_IP="{{NC_JAIL_IP}}"
# Secrets arrive rendered inside the single-quoted assignments below (the
# installer escapes any literal ' as '\''), then travel to psql/occ through
# the environment and their own variables -- the VALUES are never
# re-scanned by any shell, because a "$VAR" expansion's result is never
# re-parsed. No character the user picked can break quoting or smuggle in
# a substitution at any layer.
DB_PASS='{{NC_DB_PASSWORD}}'
ADMIN_USER='{{NC_ADMIN_USER}}'
ADMIN_PASS='{{NC_ADMIN_PASSWORD}}'
NC_DOMAIN="{{NEXTCLOUD_DOMAIN}}"

say() { echo "[nextcloud] $*"; }
occ() { bastille cmd "$APPJ" su -m www -c "php $WEBROOT/occ $*"; }

# ── 1. PostgreSQL: initdb + role + database (all idempotent) ────────────────
say "initialising PostgreSQL in $DBJ"
bastille cmd "$DBJ" sh -c "test -f $PGDATA/PG_VERSION || service postgresql initdb" || true
# Listen on the jail's address and trust the app jail over the bridge.
bastille cmd "$DBJ" sh -c "grep -q \"listen_addresses = '\\*'\" $PGDATA/postgresql.conf || echo \"listen_addresses = '*'\" >> $PGDATA/postgresql.conf"
bastille cmd "$DBJ" sh -c "grep -q '$APP_IP/32' $PGDATA/pg_hba.conf || echo 'host nextcloud nextcloud $APP_IP/32 scram-sha-256' >> $PGDATA/pg_hba.conf"
bastille cmd "$DBJ" service postgresql restart || bastille cmd "$DBJ" service postgresql start

# Role: create if missing, else converge the password onto the profile's.
# The password reaches psql as a psql VARIABLE (-v) -- :'dbpass' expands to
# a correctly SQL-quoted literal, so no shell or SQL quoting can be broken
# by its contents. Converging on re-runs matters: `rebuild` (re)generates
# a blank profile secret, and without the ALTER the database would keep a
# password the saved profile no longer knows.
if bastille cmd "$DBJ" su -m postgres -c \
     "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='nextcloud'\"" | grep -q 1; then
    NC_DB_PASS="$DB_PASS" bastille cmd "$DBJ" su -m postgres -c \
        "psql -v dbpass=\"\$NC_DB_PASS\" -c \"ALTER ROLE nextcloud LOGIN PASSWORD :'dbpass';\""
else
    NC_DB_PASS="$DB_PASS" bastille cmd "$DBJ" su -m postgres -c \
        "psql -v dbpass=\"\$NC_DB_PASS\" -c \"CREATE ROLE nextcloud LOGIN PASSWORD :'dbpass';\""
fi
bastille cmd "$DBJ" su -m postgres -c \
  "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='nextcloud'\" | grep -q 1 || \
   psql -c \"CREATE DATABASE nextcloud OWNER nextcloud;\""
say "database ready"

# ── 2. App jail: ownership, then occ install (skip if already installed) ────
bastille cmd "$APPJ" sh -c "chown -R www:www $WEBROOT $NCDATA"

if bastille cmd "$APPJ" su -m www -c "php $WEBROOT/occ status" 2>/dev/null | grep -q 'installed: true'; then
    say "already installed — skipping maintenance:install"
else
    say "running occ maintenance:install"
    # Same env route: the secrets expand inside the jail's own shell into
    # single argv elements -- never embedded in any intermediate shell
    # string.
    NC_ADMIN_USER="$ADMIN_USER" NC_ADMIN_PASS="$ADMIN_PASS" NC_DB_PASS="$DB_PASS" \
    bastille cmd "$APPJ" su -m www -c \
      "php $WEBROOT/occ maintenance:install \
         --database pgsql --database-host '$DB_HOST' --database-name nextcloud \
         --database-user nextcloud --database-pass \"\$NC_DB_PASS\" \
         --admin-user \"\$NC_ADMIN_USER\" --admin-pass \"\$NC_ADMIN_PASS\" \
         --data-dir '$NCDATA'"
fi

# Converge the admin credential onto the profile on every run (the rebuild
# contract): the WebUI can change it later, and a re-run re-asserts the
# declared value -- which is what the saved profile records, so the two
# can't drift apart silently.
if ! NC_ADMIN_USER="$ADMIN_USER" NC_ADMIN_PASS="$ADMIN_PASS" \
        bastille cmd "$APPJ" su -m www -c \
        "php $WEBROOT/occ user:password \"\$NC_ADMIN_USER\" \"\$NC_ADMIN_PASS\""; then
    say "  (could not sync admin password — does user '$ADMIN_USER' exist?)"
fi

# ── 3. Config: trusted domains, caches, region, pretty URLs ─────────────────
say "applying system config"
occ "config:system:set trusted_domains 1 --value=$APP_IP"
i=2
for d in "$NC_DOMAIN" "{{LAN_IP}}" "{{HOST_IP}}"; do
    [ -n "$d" ] && { occ "config:system:set trusted_domains $i --value=$d"; i=$((i+1)); }
done
occ "config:system:set memcache.local  --value='\\OC\\Memcache\\APCu'"
occ "config:system:set memcache.locking --value='\\OC\\Memcache\\Redis'"
occ "config:system:set redis host --value=127.0.0.1"
occ "config:system:set redis port --value=6379 --type=integer"
occ "config:system:set default_phone_region --value=GB"
occ "config:system:set maintenance_window_start --value=1 --type=integer"
occ "background:cron"
occ "maintenance:update:htaccess" || true

# ── 4. Photos / Memories — the Immich-style timeline + phone backup ─────────
say "installing photo apps (Immich replacement)"
for app in photos memories recognize previewgenerator; do
    occ "app:install $app" 2>/dev/null || occ "app:enable $app" 2>/dev/null || \
        say "  ($app not available from the app store yet — skipping)"
done
# Pre-generate previews so the timeline scrolls smoothly.
occ "preview:generate-all" 2>/dev/null || true

# ── 5. Background jobs via cron (Nextcloud's recommended mode) ──────────────
bastille cmd "$APPJ" sh -c \
  "echo '*/5 * * * * www /usr/local/bin/php $WEBROOT/cron.php' > /etc/cron.d/nextcloud"
bastille cmd "$APPJ" service cron reload 2>/dev/null || true

say "done. Reach it via the front Caddy jail at https://$NC_DOMAIN (or http://$APP_IP on the LAN)."
