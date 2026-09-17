#!/bin/sh
# build-seerr.sh — build Seerr (Jellyseerr's actively-maintained successor,
# seerr-team/seerr) from source, inside its own jail.
#
# WHY FROM SOURCE: there is no FreeBSD package for Seerr/Jellyseerr at all
# (pkg search comes up empty under every name) -- this is the actual fallback
# lib/platform.py's JAIL_PKGS comment always intended but never implemented.
#
# Runs INSIDE the seerr jail (invoked as `bastille cmd seerr sh /path/to/this`
# by lib/steps.py) so every command here is a normal single-layer shell
# script -- no nested `bastille cmd ... sh -c '...'` quoting to fight.
#
# Pinned to a specific release tag ($SEERR_TAG below), not a moving branch:
# reproducible builds, and the three migration patches below are verified
# against exactly this tag's migration files.
set -eu

REPO_DIR=/usr/local/jellyseerr
SEERR_TAG=v3.4.1
FIXES_DIR=/usr/local/seerr-build/migration-fixes

say() { echo "[build-seerr] $*"; }

# ── 1. fetch (idempotent: re-running just re-checks-out the pinned tag) ─────
if [ ! -d "$REPO_DIR/.git" ]; then
    say "cloning seerr-team/seerr"
    git clone --depth 1 https://github.com/seerr-team/seerr.git "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch --tags --depth 1 origin "refs/tags/${SEERR_TAG}:refs/tags/${SEERR_TAG}" 2>&1 || true
git checkout "$SEERR_TAG" 2>&1

# ── 2. three known-bad migrations, patched with verified-correct files ──────
# All three are the same underlying bug: a SQLite "rebuild the table to add
# a column" migration whose INSERT...SELECT also selects that brand-new
# column FROM THE OLD (pre-migration) table, where it doesn't exist yet.
# Old, lenient SQLite builds silently treated the unmatched identifier as an
# empty value; the sqlite3 driver version bundled today enforces strict
# SQLITE_DQS=0 behavior and correctly errors instead -- so the very first
# fresh-install boot fails outright without these fixes. Confirmed these
# are genuine upstream bugs (not FreeBSD-specific) by testing against both
# Fallenbagel/jellyseerr and seerr-team/seerr, same failure both times.
for f in "$FIXES_DIR"/*.ts; do
    name=$(basename "$f")
    ts=$(echo "$name" | cut -d- -f1)
    target=$(find server/migration/sqlite -name "${ts}-*.ts" | head -1)
    if [ -n "$target" ]; then
        say "patching $target"
        cp "$f" "$target"
    fi
done

# ── 3. install deps + build ──────────────────────────────────────────────
export CYPRESS_INSTALL_BINARY=0    # devDependency test runner, no FreeBSD binary, irrelevant to running the app
export npm_config_python=/usr/local/bin/python3.11
export PYTHON=/usr/local/bin/python3.11    # node-gyp's bundled sqlite3 build needs distutils, removed in 3.12+

corepack enable
corepack prepare pnpm@10.24.0 --activate
say "installing dependencies (this takes a few minutes)"
pnpm install --frozen-lockfile

say "building frontend"
# Next.js 16 defaults to Turbopack, which has no native bindings on FreeBSD
# ("Turbopack is not supported on this platform") -- Webpack is the
# documented fallback for exactly this case.
npx next build --webpack

say "building server"
pnpm build:server

say "build complete"
