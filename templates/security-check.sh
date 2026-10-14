#!/bin/sh
# security-check.sh — the security & firewall pass of the nightly routine
# (FreeBSD edition; daily-routine.sh runs it as root). Each check prints one
# tagged line ([OK] / [WARN] / [FAIL] / [SKIP]) that the dashboard picks up,
# and fixes what it safely can. A fix that was applied is reported as a
# [WARN] starting "Fixed:", so it shows on the dashboard once.
#
#   security-check.sh            quick checks: pf, VPN leak, secret files
#   security-check.sh --full     + what the internet can reach, sshd
#   security-check.sh --audit    known-vulnerable packages (host + jails) and
#                                a pending kernel; run after the updates
#   security-check.sh --accept   record what is exposed now as expected
#
# Exposure is compared with a baseline (security-baseline.txt in the
# dashboard's conf dir), written on the first full run. Anything new is
# reported until it is closed or accepted with --accept.
#
# POSIX sh, base system only (pfctl, jexec, fetch, nc, sockstat, pkg).
set -u
PATH="/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin"
export PATH

ROOT="{{HOME}}"
CONF="$ROOT/.config/status-dashboard"
BASELINE="$CONF/security-baseline.txt"
MODE="${1:-quick}"

ok()   { echo "  [OK]   $*"; }
warn() { echo "  [WARN] $*"; }
fail() { echo "  [FAIL] $*"; }
skip() { echo "  [SKIP] $*"; }
jail_up() { jls -j "$1" >/dev/null 2>&1; }
in_jail() { _j=$1; shift; jexec -l -U root "$_j" "$@"; }
public_ip() { fetch -T 10 -qo - https://api.ipify.org 2>/dev/null | tr -d '[:space:]'; }

# ── audit: known-vulnerable packages and a pending kernel ────────────────────
if [ "$MODE" = "--audit" ]; then
    pkg audit -F -q >/dev/null 2>&1          # refresh the vulnerability database
    VULN=$(pkg audit -q 2>/dev/null | sort -u | tr '\n' ' ')
    if [ -n "$VULN" ]; then
        # The updates just ran; try once more for exactly these packages.
        env ASSUME_ALWAYS_YES=YES pkg upgrade -y $(echo "$VULN" | sed 's/-[0-9][^ ]*//g') >/dev/null 2>&1
        LEFT=$(pkg audit -q 2>/dev/null | sort -u | tr '\n' ' ')
        if [ -z "$LEFT" ]; then
            warn "Fixed: upgraded host package(s) with known vulnerabilities: $VULN"
        else
            warn "host packages with known vulnerabilities and no fixed version yet: $LEFT(pkg audit)"
        fi
    else
        ok "no host package has a known vulnerability (pkg audit)"
    fi
    for j in $(jls name 2>/dev/null); do
        in_jail "$j" sh -c 'command -v pkg >/dev/null' 2>/dev/null || continue
        pkg -j "$j" audit -F -q >/dev/null 2>&1
        JV=$(pkg -j "$j" audit -q 2>/dev/null | sort -u | tr '\n' ' ')
        [ -z "$JV" ] && continue
        env ASSUME_ALWAYS_YES=YES pkg -j "$j" upgrade -y $(echo "$JV" | sed 's/-[0-9][^ ]*//g') >/dev/null 2>&1
        JL=$(pkg -j "$j" audit -q 2>/dev/null | sort -u | tr '\n' ' ')
        if [ -z "$JL" ]; then
            warn "Fixed: upgraded vulnerable package(s) in jail $j: $JV"
        else
            warn "jail $j has packages with known vulnerabilities and no fixed version yet: $JL"
        fi
        JAIL_VULN=true
    done
    [ -z "${JAIL_VULN:-}" ] && ok "no jail package has a known vulnerability"
    # freebsd-update installs a patched kernel that only counts after a reboot.
    if [ "$(freebsd-version -k 2>/dev/null)" != "$(uname -r)" ]; then
        warn "security patches installed but the running kernel is $(uname -r), not $(freebsd-version -k) — reboot to finish"
    else
        ok "running kernel $(uname -r) has every installed patch"
    fi
    exit 0
fi

# ── 1. pf firewall ───────────────────────────────────────────────────────────
if pfctl -s info 2>/dev/null | grep -q 'Status: Enabled'; then
    ok "pf firewall enabled"
elif pfctl -e >/dev/null 2>&1 && pfctl -s info 2>/dev/null | grep -q 'Status: Enabled'; then
    sysrc -q pf_enable=YES >/dev/null 2>&1
    warn "Fixed: pf firewall was disabled (no NAT, no VPN kill switch) — enabled it again"
else
    fail "pf firewall is disabled and would not start — service pf start"
fi

# ── 2. VPN leak: the download jail must not leave on the home address ────────
HOME_IP=$(public_ip)
# Testing only: probe another address instead of this machine's own.
PROBE_IP="${SECURITY_PROBE_IP:-$HOME_IP}"
if jail_up qbittorrent && jail_up vpn; then
    Q_IP=$(in_jail qbittorrent fetch -T 10 -qo - https://api.ipify.org 2>/dev/null | tr -d '[:space:]')
    if [ -n "$HOME_IP" ] && [ "$Q_IP" = "$HOME_IP" ]; then
        # Same address as home. A VPN server on this same connection exits
        # there too, so the address alone proves nothing: it is a leak only
        # if the kill switch is gone or the vpn jail is not routing through
        # the tunnel.
        if pfctl -s rules 2>/dev/null | grep -q 'on e0a_vpn' \
           && in_jail vpn netstat -rn -f inet 2>/dev/null | awk '$1=="0.0.0.0/1" && $NF ~ /^wg/ {f=1} END {exit !f}'; then
            warn "downloads leave on the home address, but through the tunnel with the kill switch on — is the VPN server on this same connection?"
        else
            bastille stop qbittorrent >/dev/null 2>&1
            fail "VPN LEAK: the qbittorrent jail reached the internet on the home address, outside the tunnel — stopped it; check the pf kill switch"
        fi
    elif [ -z "$Q_IP" ]; then
        ok "qbittorrent jail has no way out right now (tunnel down or reconnecting): blocked, not leaking"
    else
        ok "downloads leave through the VPN, not the home address"
    fi
else
    skip "no VPN download jails running"
fi

# ── 3. Secret files readable by others ───────────────────────────────────────
FIXED=""
for f in "$CONF/media-keys.env" "$BASELINE" \
         /usr/local/bastille/jails/vpn/root/usr/local/etc/wireguard/wg0.conf \
         /usr/local/etc/cloudflared/token "$ROOT/.dbmedia/alert-email.conf"; do
    [ -f "$f" ] || continue
    case "$(stat -f %Lp "$f")" in
        ?00) ;;
        *) chmod go-rwx "$f" && FIXED="$FIXED $f" ;;
    esac
done
if [ -n "$FIXED" ]; then
    warn "Fixed: secret file(s) were readable by other users, now owner-only:$FIXED"
else
    ok "secret files (keys, WireGuard private key) are owner-only"
fi

[ "$MODE" = "quick" ] && exit 0

# ── 4. What the internet can reach (probed from inside the VPN jail) ─────────
EXPOSED=""
if jail_up vpn && [ -n "$HOME_IP" ]; then
    V_IP=$(in_jail vpn fetch -T 10 -qo - https://api.ipify.org 2>/dev/null | tr -d '[:space:]')
fi
if [ -n "${V_IP:-}" ] && [ "$V_IP" != "$PROBE_IP" ]; then
    PORTS=$( { sockstat -46lL 2>/dev/null | awk 'NR>1 {print $6}' | grep -v '^127\.\|^\[::1\]\|^::1' | sed 's/.*[:.]//'
               pfctl -s nat 2>/dev/null | sed -n 's/^rdr .* port = \([0-9]*\).*/\1/p'
               printf '%s\n' 22 80 443 1443 8443 8080 8096 32400; } | grep -E '^[0-9]+$' | sort -un)
    for p in $PORTS; do
        in_jail vpn nc -z -w 4 "$PROBE_IP" "$p" >/dev/null 2>&1 && EXPOSED="$EXPOSED $p:open"
    done
    EXPOSED=$(echo "$EXPOSED" | sed 's/^ //')
    if [ -n "$EXPOSED" ]; then ok "reachable from the internet: $EXPOSED"
    else ok "nothing on this box is reachable from the internet"; fi
    if [ "$MODE" = "--accept" ] || [ ! -s "$BASELINE" ]; then
        mkdir -p "$CONF"
        { echo "# recorded $(date '+%Y-%m-%d %H:%M'): ports reachable from the internet"
          for e in $EXPOSED; do echo "$e"; done; } > "$BASELINE"
        chmod 600 "$BASELINE"
        ok "recorded what is exposed now as expected ($BASELINE)"
    else
        NEW=""
        for e in $EXPOSED; do grep -qx "$e" "$BASELINE" || NEW="$NEW $e"; done
        if [ -n "$NEW" ]; then
            fail "new ports reachable from the internet:$NEW — close the router port forward / stop the service, or if it is meant to be there: security-check.sh --accept"
        else
            ok "no new ports reachable from the internet"
        fi
    fi
else
    skip "internet exposure: needs a working VPN tunnel to probe from outside"
fi

# ── 5. sshd ──────────────────────────────────────────────────────────────────
if service sshd enabled >/dev/null 2>&1; then
    SSHD=$(sshd -T 2>/dev/null)
    if echo "$SSHD" | grep -q '^permitrootlogin yes'; then
        warn "sshd lets root log in with a password — set PermitRootLogin no (or prohibit-password)"
    fi
    if echo "$SSHD" | grep -q '^passwordauthentication yes' && echo " $EXPOSED " | grep -q ' 22:open '; then
        fail "SSH is reachable from the internet AND accepts passwords — use keys only, or close port 22 on the router"
    else
        ok "sshd: not open to the internet with password logins"
    fi
fi
exit 0
