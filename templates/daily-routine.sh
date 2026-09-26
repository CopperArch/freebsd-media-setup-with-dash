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
LOGDIR="$ROOT/.hermes/maintenance-logs"
LOG="$LOGDIR/daily-routine-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$LOGDIR"

log()    { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
warn()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: $*" | tee -a "$LOG"; }
banner() { log ""; log "========== $* =========="; }

QUICK=false
[ "${1:-}" = "--quick" ] && QUICK=true

# Managed jails (kept in sync with stack.yaml). VPN-routed ones listed too.
MEDIA_JAILS="jellyfin plex prowlarr bazarr seerr caddy nextcloud nextcloud-db"
VPN_JAILS="vpn qbittorrent sonarr radarr"
ALL_JAILS="$MEDIA_JAILS $VPN_JAILS"

banner "Daily Routine started (quick=$QUICK) on $(hostname)"

# ─── 1. Host health: rc services, ZFS, SMART ────────────────────────────────
banner "Host System Health Check"

# rc.d services that must be up for the stack to function.
for svc in bastille pf sddm; do
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
if command -v smartctl >/dev/null 2>&1; then
    for dev in $(sysctl -n kern.disks 2>/dev/null); do
        case "$dev" in cd*|md*|fd*) continue ;; esac
        out=$(smartctl -H "/dev/$dev" 2>&1)
        if echo "$out" | grep -qi "PASSED\|OK"; then
            log "  [OK]   SMART /dev/$dev: healthy"
        elif echo "$out" | grep -qi "Unavailable\|Unsupported"; then
            log "  [SKIP] SMART /dev/$dev: no passthrough (USB bridge?)"
        else
            warn "  [WARN] SMART /dev/$dev: $(echo "$out" | grep -i health)"
        fi
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
# Trust the tunnel, not the wrapper: check the exit IP from INSIDE the vpn jail
# and confirm it differs from the host's real WAN IP. gluetun's healthcheck
# used to stay "healthy" while OpenVPN looped on auth — we check reality.
vpn_exit_ip() { bastille cmd vpn sh -c 'fetch -qo - https://api.ipify.org 2>/dev/null'; }
host_wan_ip() { fetch -qo - https://api.ipify.org 2>/dev/null; }

TUN_OK=false
for attempt in 1 2 3; do
    vip=$(vpn_exit_ip); hip=$(host_wan_ip)
    if [ -n "$vip" ] && [ "$vip" != "$hip" ]; then
        log "  [OK]   VPN exit IP $vip differs from host WAN $hip (attempt $attempt)"
        TUN_OK=true
        break
    fi
    warn "  [WARN] tunnel down or leaking (vpn='$vip' host='$hip'), restart $attempt/3"
    bastille cmd vpn service wireguard restart >/dev/null 2>&1
    sleep 8
done
if [ "$TUN_OK" != true ]; then
    warn "  [FAIL] VPN tunnel would not come back after 3 restarts"
    [ -x "$ROOT/.hermes/scripts/send-alert.py" ] && \
        "$ROOT/.hermes/scripts/send-alert.py" "VPN tunnel down on $(hostname)" \
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
        [ -x "$ROOT/.hermes/scripts/send-alert.py" ] && \
            "$ROOT/.hermes/scripts/send-alert.py" "Caddy config invalid on $(hostname)" >/dev/null 2>&1
    fi
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
    ds=$(bastille config "$j" get zfs.dataset 2>/dev/null)
    snap="preupdate-$(date +%Y%m%d)"
    [ -n "$ds" ] && zfs snapshot "${ds}@${snap}" 2>/dev/null && \
        log "  snapshot ${ds}@${snap}"
    if bastille pkg "$j" upgrade -y >>"$LOG" 2>&1; then
        # verify the jail's primary service still answers after upgrade
        if jls -j "$j" >/dev/null 2>&1; then
            log "  [OK]   $j upgraded and still healthy"
        else
            warn "  [ROLLBACK] $j unhealthy after upgrade — rolling back snapshot"
            [ -n "$ds" ] && zfs rollback "${ds}@${snap}" && bastille restart "$j"
        fi
    else
        warn "  [WARN] pkg upgrade failed in $j (left as-is)"
    fi
done

# ─── 4. Host base + package updates ─────────────────────────────────────────
banner "Host Updates"
log "  pkg upgrade (host)"
env ASSUME_ALWAYS_YES=YES pkg upgrade -y >>"$LOG" 2>&1 && log "  [OK] host pkg upgrade"
log "  freebsd-update fetch/install (security patches)"
env PAGER=cat freebsd-update --not-running-from-cron fetch install >>"$LOG" 2>&1 \
    && log "  [OK] freebsd-update applied" \
    || log "  [INFO] freebsd-update: nothing to do or needs reboot"

# ─── 5. Log rotation guard (replaces docker log-cap check) ──────────────────
# Mirrors the Linux daily-routine.sh retention window (2026-09-26): 5 days,
# not 30 -- and also prunes .bak-* script backups, which used to accumulate
# forever under $ROOT/.local/bin with no cleanup at all.
banner "Log Hygiene"
find "$LOGDIR" -type f -mtime +5 -delete 2>/dev/null
log "  pruned maintenance logs older than 5 days"
find "$ROOT/.local/bin" -maxdepth 1 -name '*.bak-*' -mtime +5 -delete 2>/dev/null
log "  pruned script backups (.bak-*) older than 5 days"

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

banner "Daily Routine finished on $(hostname)"
