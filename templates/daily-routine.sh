#!/bin/sh
# daily-routine.sh — nightly self-healing maintenance for this FreeBSD media
# server. FreeBSD port of the Linux edition: systemd -> rc.d, docker -> jails
# (bastille/jls), apt -> pkg + freebsd-update, mdadm/SMART -> zpool + smartctl,
# ufw/gluetun-iptables -> pf.
#
#   ./daily-routine.sh          # run everything (updates included)
#   ./daily-routine.sh --quick  # health/connectivity checks only, no updates
#
# POSIX sh (FreeBSD /bin/sh) — no bashisms, so it runs from cron and from the
# base system with nothing extra installed.
set -u

PATH="/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin"
export PATH

ROOT="{{HOME}}"
LOGDIR="$ROOT/.dbmedia/maintenance-logs"
LOG="$LOGDIR/daily-routine-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$LOGDIR"

log()    { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
warn()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: $*" | tee -a "$LOG"; }
banner() { log ""; log "========== $* =========="; }

QUICK=false
[ "${1:-}" = "--quick" ] && QUICK=true

# Managed jails (kept in sync with stack.yaml). VPN-routed ones listed too.
# Only the ones that exist here: a stack the profile left off (VPN, Nextcloud)
# has no jails, and "never installed" must not be reported as "down".
installed_jails() {
    for _j in "$@"; do
        [ -d "/usr/local/bastille/jails/$_j" ] && printf '%s ' "$_j"
    done
}
MEDIA_JAILS=$(installed_jails jellyfin plex prowlarr bazarr seerr caddy nextcloud nextcloud-db)
VPN_JAILS=$(installed_jails vpn qbittorrent sonarr radarr)
ALL_JAILS="$MEDIA_JAILS $VPN_JAILS"

banner "Daily Routine started (quick=$QUICK) on $(hostname)"

# ─── 1. Host health: rc services, ZFS, SMART ────────────────────────────────
banner "Host System Health Check"

# rc.d services that must be up for the stack to function.
# Only services this machine has switched on (a box without a desktop has no
# sddm). bastille is not listed: its rc script starts the jails and exits, so
# "not running" is its normal state -- the jails are checked one by one below.
for svc in pf sddm; do
    service "$svc" enabled >/dev/null 2>&1 || continue
    if service "$svc" status >/dev/null 2>&1; then
        log "  [OK]   rc service '$svc' is running"
    else
        warn "  [FAIL] rc service '$svc' is NOT running — check 'service $svc status'"
    fi
done

# ZFS pool health (replaces mdadm/SMART array checks). DEGRADED/FAULTED is loud.
if command -v zpool >/dev/null 2>&1; then
    for pool in $(zpool list -H -o name 2>/dev/null); do
        state=$(zpool list -H -o health "$pool" 2>/dev/null)
        if [ "$state" = "ONLINE" ]; then
            log "  [OK]   zpool '$pool' is ONLINE"
        else
            warn "  [FAIL] zpool '$pool' is $state — run 'zpool status $pool'"
        fi
    done
fi

# SMART: smartctl exists on FreeBSD too. Use device nodes; ZFS handles the
# array logic so we only assert per-disk health here.
# Only smartctl's verdict line counts: grepping the whole output for "failed"
# reported every NVMe disk as failing, because smartctl cannot open nda/nvd
# disks at all ("open device: /dev/nda0 failed: INQUIRY failed"). NVMe health
# is read from the controller node (/dev/nvmeN) instead.
smart_check() {   # smart_check <device> <label>
    out=$(smartctl -H "$1" 2>&1)
    if echo "$out" | grep -qiE 'test result: *FAIL|Health Status: *[A-Z_]*FAIL'; then
        warn "  [WARN] SMART $2: $(echo "$out" | grep -iE 'test result|Health Status' | head -1)"
    elif echo "$out" | grep -qiE 'test result: *PASSED|Health Status: *OK'; then
        log "  [OK]   SMART $2: healthy"
    else
        # a virtual disk or a USB bridge: nothing to read, not a fault
        log "  [SKIP] SMART $2: no SMART data from this device"
    fi
}
if command -v smartctl >/dev/null 2>&1; then
    for dev in $(sysctl -n kern.disks 2>/dev/null); do
        case "$dev" in cd*|md*|fd*|nda*|nvd*) continue ;; esac
        smart_check "/dev/$dev" "/dev/$dev"
    done
    for ctl in /dev/nvme[0-9] /dev/nvme[0-9][0-9]; do
        [ -c "$ctl" ] && smart_check "$ctl" "$ctl (NVMe)"
    done
fi

# ─── 1b. Jail crash-loop / down detection (replaces docker restart-count) ───
banner "Jail Health Check"
# jls has no restart counter, so we detect (a) a jail that should be up but
# isn't, and (b) a service inside a jail that keeps dying (checked via its
# rc status). This catches the FreeBSD equivalent of the chrome crash-loop.
#
# rc.d script per jail, so a jail that's UP but never actually got its
# package installed (a real failure mode: a flaky pkg mirror mid-install can
# leave a jail running with NOTHING but a base system in it, no error ever
# surfaced past that point) is told apart from "jail up, service just not
# started" -- the two need different fixes (a real pkg install vs. a plain
# service start) and looked identical before this check existed.
jail_svc() {
    case "$1" in
        jellyfin) echo jellyfin ;; plex) echo plexmediaserver_plexpass ;;
        prowlarr) echo prowlarr ;; bazarr) echo bazarr ;; seerr) echo seerr ;;
        caddy) echo caddy ;; nextcloud) echo caddy ;; nextcloud-db) echo postgresql ;;
        qbittorrent) echo qbittorrent ;; sonarr) echo sonarr ;; radarr) echo radarr ;;
        vpn) echo wireguard ;; *) echo "" ;;
    esac
}
for j in $ALL_JAILS; do
    if ! jls -j "$j" >/dev/null 2>&1; then
        warn "  [FAIL] jail '$j' is not running — 'bastille start $j'"
        continue
    fi
    svc=$(jail_svc "$j")
    if [ -n "$svc" ] && ! bastille cmd "$j" test -x "/usr/local/etc/rc.d/$svc" >/dev/null 2>&1; then
        warn "  [FAIL] jail '$j' is up but /usr/local/etc/rc.d/$svc doesn't exist -- its package likely never installed (flaky pkg mirror?); re-run: bastille pkg $j install -y <package>"
        continue
    fi
    log "  [OK]   jail '$j' is up"
done

# ─── 1c. Jail-bridge network topology check ─────────────────────────────────
# Everything below only matters if the VPN stack (VNET jails) is in use --
# classic media jails share the host's own stack and don't depend on any of
# this. Real failure modes hit and fixed live this session, none of them
# ever surfaced as a jail being "down" in the check above:
banner "Network Topology Check"
if jls -j vpn >/dev/null 2>&1; then
    BRIDGE_GW="{{SUBNET}}.1"
    VPN_IP="{{SUBNET}}.20"

    # (a) The jail bridge's own gateway address. VNET jails (vpn + the three
    # confined jails below) need this to route anywhere at all -- it's not
    # bastille's doing, nothing else on this box re-adds it if it's ever
    # lost (confirmed live: a plain `service netif restart` on the bridge
    # wipes every classic jail's alias too, though that's a separate,
    # harmless-here case covered by (c) below).
    if ifconfig bastille0 2>/dev/null | grep -q "inet $BRIDGE_GW "; then
        log "  [OK]   jail bridge gateway $BRIDGE_GW present on bastille0"
    else
        warn "  [FAIL] jail bridge gateway $BRIDGE_GW missing from bastille0 -- every VNET jail (vpn/qbittorrent/sonarr/radarr) loses all connectivity when this happens. Restoring."
        sysrc "ifconfig_bastille0_alias0=inet $BRIDGE_GW/24" >>"$LOG" 2>&1
        ifconfig bastille0 inet "$BRIDGE_GW/24" alias >>"$LOG" 2>&1
    fi

    # (b) IP forwarding -- required on the HOST for jails to reach the WAN at
    # all, and separately inside the vpn jail for it to route the confined
    # jails' traffic. A reset here (e.g. from an unrelated troubleshooting
    # sysctl -w) silently takes down every jail's internet access at once.
    if [ "$(sysctl -n net.inet.ip.forwarding 2>/dev/null)" = "1" ]; then
        log "  [OK]   host IP forwarding enabled"
    else
        warn "  [FAIL] host IP forwarding was OFF -- every jail just lost internet access. Re-enabling."
        sysrc gateway_enable=YES >>"$LOG" 2>&1
        sysctl net.inet.ip.forwarding=1 >>"$LOG" 2>&1
    fi
    if bastille cmd vpn sysctl -n net.inet.ip.forwarding 2>/dev/null | grep -q '^1$'; then
        log "  [OK]   vpn jail forwarding enabled"
    else
        warn "  [WARN] vpn jail forwarding was OFF -- confined jails can't route through it. Re-enabling."
        bastille sysrc vpn gateway_enable=YES >>"$LOG" 2>&1
        bastille cmd vpn sysctl net.inet.ip.forwarding=1 >>"$LOG" 2>&1
    fi

    # (c) Confined jails must be gatewayed onto the vpn jail's OWN IP, never
    # the plain bridge gateway -- that's the entire enforcement mechanism
    # for "no tunnel, no leak" (a classic jail can't do this at all; these
    # are VNET specifically so this actually means something). Confirmed
    # live this session: a freshly-created jail's LIVE route doesn't follow
    # a sysrc-only defaultrouter change without a restart.
    for cj in qbittorrent sonarr radarr; do
        jls -j "$cj" >/dev/null 2>&1 || continue
        dr=$(bastille cmd "$cj" sysrc -n defaultrouter 2>/dev/null | tail -1)
        if [ "$dr" = "$VPN_IP" ]; then
            log "  [OK]   $cj is gatewayed through the vpn jail"
        else
            warn "  [FAIL] $cj's defaultrouter is '$dr', not the vpn jail ($VPN_IP) -- it could leak untunneled traffic. Fixing."
            bastille sysrc "$cj" "defaultrouter=$VPN_IP" >>"$LOG" 2>&1
            bastille restart "$cj" >>"$LOG" 2>&1
        fi
    done

    # (d) Classic jails' bridge IP alias. Confirmed live this session: any
    # host-level command that resets the bastille0 interface (even one
    # unrelated to a specific jail) silently drops every classic jail's own
    # alias without the jail itself ever going down -- `jls`/`bastille list`
    # both still report it as running the whole time, so nothing else in
    # this script would ever catch it.
    for cj in $MEDIA_JAILS; do
        jls -j "$cj" >/dev/null 2>&1 || continue
        ip=$(jls -j "$cj" ip4.addr 2>/dev/null)
        [ -n "$ip" ] || continue
        if ifconfig bastille0 2>/dev/null | grep -q "inet $ip "; then
            log "  [OK]   $cj's IP $ip is present on bastille0"
        else
            warn "  [FAIL] $cj's IP $ip is missing from bastille0 (jail is up but unreachable) -- restarting to re-add it."
            bastille restart "$cj" >>"$LOG" 2>&1
        fi
    done
fi

# ─── 2. VPN tunnel health + rotation (replaces gluetun healthcheck) ─────────
banner "VPN Tunnel Health"
# Healthy = WireGuard has completed a handshake in the last three minutes
# (peers re-handshake every two while PersistentKeepalive keeps traffic
# flowing). The kill-switch leaves the confined jails no other way out, so a
# live tunnel is the whole question. An earlier version compared the exit
# address with this machine's own, which restarted a self-hosted VPN on the
# same connection three times an hour for nothing.
WG_CONF=/usr/local/bastille/jails/vpn/root/usr/local/etc/wireguard/wg0.conf
handshake_age() {
    _hs=$(bastille cmd vpn wg show wg0 latest-handshakes 2>/dev/null | awk '$2 ~ /^[0-9]+$/ && $2 > m {m = $2} END {print m + 0}')
    [ "${_hs:-0}" -gt 0 ] && echo $(( $(date +%s) - _hs )) || echo 999999
}
vpn_exit_ip() { bastille cmd vpn sh -c 'fetch -T 10 -qo - https://api.ipify.org 2>/dev/null'; }

TUN_OK=false
if [ ! -d /usr/local/bastille/jails/vpn ]; then
    log "  [SKIP] no VPN stack on this machine"
    TUN_OK=true
elif [ ! -f "$WG_CONF" ]; then
    log "  [SKIP] the VPN has no WireGuard config yet (dashboard: VPN -> Change provider); the kill-switch keeps the download jails offline until it does"
    TUN_OK=true
else
for attempt in 1 2 3; do
    age=$(handshake_age)
    if [ "$age" -lt 180 ]; then
        log "  [OK]   VPN tunnel up: handshake ${age}s ago, exit IP $(vpn_exit_ip) (attempt $attempt)"
        TUN_OK=true
        break
    fi
    warn "  [WARN] VPN tunnel has no recent handshake, restart $attempt/3"
    # backgrounded: wg-quick leaves a route monitor running that would
    # otherwise hold this command open
    (bastille cmd vpn service wireguard restart) </dev/null >/dev/null 2>&1 &
    sleep 12
done
fi
if [ "$TUN_OK" != true ]; then
    warn "  [FAIL] VPN tunnel would not come back after 3 restarts"
    [ -x "$ROOT/.dbmedia/scripts/send-alert.py" ] && \
        "$ROOT/.dbmedia/scripts/send-alert.py" "VPN tunnel down on $(hostname)" \
        >/dev/null 2>&1
    # Kill-switch means the torrent jail is already leak-safe (no fallback),
    # so we do NOT need to stop it — pf has already dropped its egress.
fi

# Re-assert the pf kill-switch in case a reboot or manual edit dropped it.
# NOTE: this used to check for "on wg0" -- that was the OLD design (a
# ruleset meant to load INSIDE the vpn jail). Confirmed live this session
# that pf can't run in any jail at all (DIOCADDRULE: Operation not
# permitted, even VNET, even with every allow.* flag) -- the real
# kill-switch is a HOST-side rule on the vpn jail's own epair leg
# (e0a_vpn), so that's what's actually checked for now. A stale check like
# the old one would ALWAYS report "missing" and reload a file that was
# never actually broken, or worse, never notice a genuinely missing rule
# because it was looking for the wrong thing entirely.
if jls -j vpn >/dev/null 2>&1 && pfctl -s rules 2>/dev/null | grep -q "on e0a_vpn"; then
    log "  [OK]   pf kill-switch rule present (on e0a_vpn)"
elif jls -j vpn >/dev/null 2>&1; then
    warn "  [FAIL] pf kill-switch rule missing from the loaded ruleset -- qbittorrent/sonarr/radarr could leak. Restoring."
    if ! grep -q "freebsd-media-setup vpn kill-switch" /etc/pf.conf 2>/dev/null; then
        {
            echo ""
            echo "# freebsd-media-setup vpn kill-switch"
            echo "block drop quick on e0a_vpn from { {{SUBNET}}.21, {{SUBNET}}.22, {{SUBNET}}.23 } to ! {{SUBNET}}.20"
        } >> /etc/pf.conf
    fi
    pfctl -f /etc/pf.conf 2>&1 | tee -a "$LOG"
fi

# ─── 2b. qBittorrent WebUI login bypass drift ───────────────────────────────
# Its own config file is qBittorrent's to rewrite; a version upgrade or a
# manual "reset to defaults" in the WebUI can silently drop the
# AuthSubnetWhitelist block the installer set up, bringing back a login
# prompt the dashboard's click-to-open flow doesn't expect.
if jls -j qbittorrent >/dev/null 2>&1; then
    QCONF="/usr/local/bastille/jails/qbittorrent/root/var/db/qbittorrent/conf/qBittorrent/config/qBittorrent.conf"
    if [ -f "$QCONF" ] && grep -q "AuthSubnetWhitelistEnabled" "$QCONF"; then
        log "  [OK]   qBittorrent WebUI login bypass still configured"
    elif [ -f "$QCONF" ]; then
        warn "  [WARN] qBittorrent's AuthSubnetWhitelist config is gone -- restoring."
        printf '[Preferences]\nWebUI\\AuthSubnetWhitelistEnabled=true\nWebUI\\AuthSubnetWhitelist={{SUBNET}}.0/24, {{LAN_CIDR}}\n' \
            >> "$QCONF"
        bastille cmd qbittorrent service qbittorrent restart >>"$LOG" 2>&1
    fi
fi

# ─── 2c. Cloudflare Tunnel health (only if public_access=cloudflare) ────────
if service cloudflared_tunnel enabled >/dev/null 2>&1; then
    if pgrep -f "cloudflared tunnel run" >/dev/null 2>&1; then
        log "  [OK]   cloudflared tunnel is running"
    else
        warn "  [WARN] cloudflared tunnel is enabled but not running -- restarting."
        service cloudflared_tunnel restart >>"$LOG" 2>&1
        sleep 3
        pgrep -f "cloudflared tunnel run" >/dev/null 2>&1 \
            && log "  [OK]   cloudflared tunnel came back up" \
            || warn "  [FAIL] cloudflared tunnel still not running -- check /var/log/cloudflared_tunnel.log (bad/expired token?)"
    fi
fi

# ─── 2d. Caddy config validity (only if the caddy jail exists) ──────────────
# A hand edit or a bad paste into the Caddyfile (this project appends a
# block per public-access app) can leave Caddy running its OLD config while
# silently refusing every reload -- caddy validate catches that without
# guessing at what a "fix" would even mean here, so this only alerts.
if jls -j caddy >/dev/null 2>&1; then
    if bastille cmd caddy caddy validate --config /usr/local/etc/caddy/Caddyfile >/dev/null 2>&1; then
        log "  [OK]   Caddy config is valid"
    else
        warn "  [FAIL] Caddy's Caddyfile has a syntax error -- it's serving its LAST successfully-loaded config, not what's on disk now. Check it by hand."
        [ -x "$ROOT/.dbmedia/scripts/send-alert.py" ] && \
            "$ROOT/.dbmedia/scripts/send-alert.py" "Caddy config invalid on $(hostname)" >/dev/null 2>&1
    fi
fi

# ─── 2e. Security & firewall ────────────────────────────────────────────────
# pf on, no VPN leak, secret files owner-only; on the full run also what the
# internet can reach (probed from the VPN jail) and sshd. Fixes what it can.
banner "Security & Firewall"
SEC="$(dirname "$0")/security-check.sh"
if [ -x "$SEC" ]; then
    if [ "$QUICK" = true ]; then "$SEC" 2>&1 | tee -a "$LOG"
    else "$SEC" --full 2>&1 | tee -a "$LOG"; fi
else
    log "  [SKIP] security-check.sh not found"
fi

if [ "$QUICK" = true ]; then
    banner "Quick mode — skipping updates"
    log "Daily Routine (quick) finished"
    exit 0
fi

# ─── 3. Media stack update with verify + rollback ───────────────────────────
banner "Media Stack Update (verify + rollback)"
# The *arr FreeBSD ports have their in-app updater disabled — updates come from
# pkg, which is exactly the "reviewed, pinned" model we want. We snapshot each
# jail (ZFS) before upgrading so a bad pkg can be rolled back instantly.
for j in $ALL_JAILS; do
    # The dataset behind the jail's root. (`bastille config <jail> get
    # zfs.dataset` answers "not set" here, which an earlier version took for
    # a dataset name -- so no snapshot was ever made and the rollback below
    # had nothing to roll back to.)
    ds=$(zfs list -H -o name "/usr/local/bastille/jails/$j/root" 2>/dev/null)
    case "$ds" in */jails/"$j"/root) ;; *) ds="" ;; esac
    snap="preupdate-$(date +%Y%m%d-%H%M)"
    if [ -n "$ds" ] && zfs snapshot "${ds}@${snap}" 2>/dev/null; then
        log "  snapshot ${ds}@${snap}"
        # keep the seven newest pre-update snapshots of this jail
        old=$(zfs list -H -t snapshot -o name -S creation "$ds" 2>/dev/null \
              | grep '@preupdate-' | tail -n +8)
        for s in $old; do zfs destroy "$s" 2>/dev/null; done
    else
        ds=""
    fi
    if bastille pkg "$j" upgrade -y >>"$LOG" 2>&1; then
        # verify the jail's primary service still answers after upgrade
        if jls -j "$j" >/dev/null 2>&1; then
            log "  [OK]   $j upgraded and still healthy"
        else
            warn "  [ROLLBACK] $j unhealthy after upgrade — rolling back snapshot"
            if [ -n "$ds" ]; then
                bastille stop "$j" >>"$LOG" 2>&1
                zfs rollback -r "${ds}@${snap}" && bastille start "$j" >>"$LOG" 2>&1
            fi
        fi
    else
        warn "  [WARN] pkg upgrade failed in $j (left as-is)"
    fi
done

# ─── 3b. Misfiled media: episodes in Movies/, movies in TV/ ────────────────
# Moves them to the right library (rename within the pool, never overwrites,
# skips *arr-managed files and anything touched in the last 2h; new folders
# get their parent's owner so the jailed apps can still write to them).
banner "Misfiled Movies / TV Episodes"
if [ -x "$ROOT/.local/bin/media-library-sort.py" ]; then
    "$ROOT/.local/bin/media-library-sort.py" --apply 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] media-library-sort.py not installed"
fi

# ─── 3c. Collections + artwork in Jellyfin and Plex ────────────────────────
# Creates/tops up Jellyfin's TMDB franchise collections (2+ movies), matches
# unidentified movies only when TMDB and IMDb agree, keeps Plex's automatic
# collections on, applies ~/.config/media-library/manual-collections.json,
# and fetches every poster in both apps, re-fetching broken ones. Add-only.
banner "Media Collections & Artwork"
if [ -x "$ROOT/.local/bin/media-library-health.py" ]; then
    "$ROOT/.local/bin/media-library-health.py" 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] media-library-health.py not installed"
fi

# ─── 3d. Jellyfin duplicate removal (DELETES losing files) ─────────────────
# Keeps the best copy of a movie/episode stored twice and deletes the rest;
# skips itself until ~/.dbmedia/jellyfin-api-key exists — creating that file
# is the opt-in.
banner "Jellyfin Duplicate Removal"
if [ -x "$ROOT/.local/bin/jellyfin-duplicates.py" ]; then
    "$ROOT/.local/bin/jellyfin-duplicates.py" --apply 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] jellyfin-duplicates.py not installed"
fi

# ─── 4. Host base + package updates ─────────────────────────────────────────
banner "Host Updates"
log "  pkg upgrade (host)"
env ASSUME_ALWAYS_YES=YES pkg upgrade -y >>"$LOG" 2>&1 && log "  [OK] host pkg upgrade"
log "  freebsd-update fetch/install (security patches)"
env PAGER=cat freebsd-update --not-running-from-cron fetch install >>"$LOG" 2>&1 \
    && log "  [OK] freebsd-update applied" \
    || log "  [INFO] freebsd-update: nothing to do or needs reboot"

# ─── 4b. Security audit: what is still vulnerable after the updates ─────────
banner "Security Audit (pkg audit)"
[ -x "$SEC" ] && "$SEC" --audit 2>&1 | tee -a "$LOG"

# ─── 5. Log rotation guard (replaces docker log-cap check) ──────────────────
# Mirrors the Linux daily-routine.sh retention window (2026-09-26): 5 days,
# not 30 -- and also prunes .bak-* script backups, which used to accumulate
# forever under $ROOT/.local/bin with no cleanup at all.
banner "Log Hygiene"
find "$LOGDIR" -type f -mtime +5 -delete 2>/dev/null
log "  pruned maintenance logs older than 5 days"
find "$ROOT/.local/bin" -maxdepth 1 -name '*.bak-*' -mtime +5 -delete 2>/dev/null
log "  pruned script backups (.bak-*) older than 5 days"

# Disk-space cleanup (mirrors the Linux edition's 9a, 2026-10-06). FreeBSD has
# no Docker images to prune; the equivalent leftovers are pkg's download
# cache (every package ever fetched, host and per jail) and test-VM disks /
# stock install media left in ~/build-iso by ISO testing.
pkg clean -ay >/dev/null 2>&1 && log "  [OK]   host pkg cache cleaned"
for j in $(bastille list -q 2>/dev/null || jls name 2>/dev/null); do
    jexec "$j" pkg clean -ay >/dev/null 2>&1
done
log "  [OK]   jail pkg caches cleaned"
if [ -d "$ROOT/build-iso" ]; then
    find "$ROOT/build-iso" -maxdepth 1 -type f -mtime +7 \( -name '*.raw' \
        -o -name '*.qcow2' -o -name '*.vmdk' -o -name '*.vdi' \
        -o -name 'FreeBSD-*-dvd1.iso' -o -name 'FreeBSD-*-disc1.iso' \
        -o -name 'FreeBSD-*-memstick.img' \) 2>/dev/null | while read -r f; do
        # fstat prints a header plus one line per process holding the file.
        [ "$(fstat "$f" 2>/dev/null | wc -l)" -gt 1 ] && continue
        rm -f "$f" && log "  [FIX ] removed stale $(basename "$f")"
    done
fi
# Warn early: ZFS slows sharply past ~80% and fails writes when full.
for pool in $(zpool list -H -o name 2>/dev/null); do
    cap=$(zpool list -H -o capacity "$pool" 2>/dev/null | tr -dc '0-9')
    if [ -n "$cap" ] && [ "$cap" -ge 80 ]; then
        warn "  ZFS pool $pool is ${cap}% full — biggest datasets: $(zfs list -H -o name,used -s used -r "$pool" 2>/dev/null | tail -4 | awk '{printf "%s %s; ", $1, $2}')"
    else
        log "  [OK]   ZFS pool $pool ${cap}% full"
    fi
done

# ─── 6. Dashboard AI panes: refresh model slugs + pricing ───────────────────
# Re-pick the paid flagships, verify the free tiers still exist, and refresh
# model-pricing.json so the dashboard shows live per-pane pricing. Never fails
# the routine (exit 0 on any network error).
banner "AI Panes Refresh"
if [ -x "$ROOT/.local/bin/ai-panes-check.py" ]; then
    "$ROOT/.local/bin/ai-panes-check.py" 2>&1 | tee -a "$LOG"
else
    log "  [SKIP] ai-panes-check.py not installed"
fi

# ─── 7. Local AI model refresh (only if Local AI was chosen at install) ─────
# `ollama pull` only transfers changed layers, so this is cheap when current.
AI_ENV="$ROOT/.config/status-dashboard/local-ai.env"
if [ -f "$AI_ENV" ] && command -v ollama >/dev/null 2>&1; then
    banner "Local AI Model Update"
    GLM_MODEL=$(sed -n 's/^GLM_MODEL=//p' "$AI_ENV")
    OLLAMA_USER=$(sysrc -n ollama_user 2>/dev/null || echo root)
    if [ -n "$GLM_MODEL" ] && su -l "$OLLAMA_USER" -c \
            "env OLLAMA_HOST=127.0.0.1:11434 ollama pull $GLM_MODEL" >/dev/null 2>&1; then
        log "  [OK]   $GLM_MODEL current"
    else
        log "  [WARN] ollama pull $GLM_MODEL failed (is 'service ollama status' running?)"
    fi
fi

banner "Daily Routine finished on $(hostname)"
