#!/bin/sh
# status-dashboard-install.sh — FreeBSD edition. Idempotent installer for the
# desktop status dashboard. Safe to run on every boot / from daily-routine.sh.
#
# FreeBSD has no systemd user units, so the three roles map to:
#   collector (every 60s)  -> a cron line (1-minute granularity == 60s)
#   HTTP server (always)   -> daemon(8) -r supervision + a ROOT cron watchdog
#                             line -- NOT this script's crontab. The repair
#                             API restarts jails, upgrades packages and
#                             restarts services; it needs root to actually do
#                             any of that, and a plain user's crontab can't
#                             grant it. lib/steps.py::install_dashboard sets
#                             that watchdog up separately, as root, the same
#                             way it does for daily-routine.sh. This script
#                             only starts/supervises the collector and ttyd.
#   desktop window         -> XDG autostart .desktop (needs the KDE session) that
#                             runs status-dashboard-run.sh
# ttyd comes from pkg (www/ttyd), not a bundled Linux binary.
#
#   status-dashboard-install.sh           # install/repair, then ensure running
#   status-dashboard-install.sh --check   # report only
set -u

BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share/status-dashboard"
AUTOSTART="$HOME/.config/autostart"
PAGE="$SHARE/index.html"
CHECK=false
[ "${1:-}" = "--check" ] && CHECK=true
say() { echo "  $*"; }
changed=0

# ── prerequisites ───────────────────────────────────────────────────────────
BROWSER=""
for b in chrome chromium ungoogled-chromium brave falkon; do
    if command -v "$b" >/dev/null 2>&1; then BROWSER="$(command -v $b)"; break; fi
done
[ -z "$BROWSER" ] && { say "[FAIL] no Chromium-family browser (pkg install chromium)"; exit 1; }
command -v python3.11 >/dev/null 2>&1 || { say "[FAIL] python3.11 missing"; exit 1; }

for f in "$BIN/status-collect.py" "$BIN/status-dashboard-server.py" \
         "$BIN/status-dashboard-run.sh" "$BIN/status_keys.py"; do
    [ -f "$f" ] || { say "[FAIL] missing $f"; exit 1; }
done
[ -f "$PAGE" ] || { say "[FAIL] $PAGE missing (dashboard page not installed)"; exit 1; }

# Screen geometry so the window matches the display (KDE first, then X11).
GEO="1920,1080"
if command -v kscreen-doctor >/dev/null 2>&1; then
    G=$(kscreen-doctor -o 2>/dev/null | grep -oE 'Geometry:[^ ]* [0-9]+x[0-9]+' | grep -oE '[0-9]+x[0-9]+' | head -1)
    [ -n "${G:-}" ] && GEO="$(echo "$G" | sed 's/x/,/')"
elif command -v xrandr >/dev/null 2>&1; then
    G=$(xrandr 2>/dev/null | grep -oE '[0-9]+x[0-9]+\+' | head -1 | tr -d '+')
    [ -n "${G:-}" ] && GEO="$(echo "$G" | sed 's/x/,/')"
fi
$CHECK && { say "[OK] browser=$BROWSER geometry=$GEO page=$PAGE"; exit 0; }

# ── prerequisites from pkg: bash (pane script shebang) + ttyd (pane bridge) ─
for p in bash ttyd; do
    if ! command -v "$p" >/dev/null 2>&1; then
        say "[..] installing $p from pkg"
        env ASSUME_ALWAYS_YES=YES sudo -n pkg install -y "$p" >/dev/null 2>&1 \
            && say "[FIX] $p installed" || say "[SKIP] could not install $p"
    fi
done
TTYD_OK=false; command -v ttyd >/dev/null 2>&1 && TTYD_OK=true

# ── cron: collector + server watchdog + ttyd watchdog, every minute ─────────
# cron supervision keeps the server and terminal bridge alive headless (the
# systemd Restart=always + timer analogue). Both log to server.log, which the
# dashboard's "Dashboard log" pane tails.
LOG="$SHARE/server.log"
TTYD_CMD=""
if $TTYD_OK; then
  # Same hardening as the Linux edition: loopback only, --check-origin refuses
  # cross-origin WebSocket upgrades (WebSockets aren't CORS-governed), --url-arg
  # lets the page pick a pane, --writable grants a real shell on loopback only.
  TTYD_CMD="ttyd --port 7682 --interface 127.0.0.1 --writable --check-origin --url-arg --max-clients 2 --client-option fontSize=13 --client-option cursorBlink=true $BIN/dashboard-pane.sh"
fi
# NOTE: the repair-server watchdog line lives in ROOT's crontab, set up
# separately by lib/steps.py::install_dashboard, not here -- see the header
# comment. This script only owns the collector and ttyd.
CRON_TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v '# status-dashboard' > "$CRON_TMP" || true
{
  echo "* * * * * $BIN/status-collect.py >/dev/null 2>&1  # status-dashboard collector"
  [ -n "$TTYD_CMD" ] && echo "* * * * * pgrep -x ttyd >/dev/null 2>&1 || /usr/sbin/daemon -r -o $LOG $TTYD_CMD  # status-dashboard ttyd watchdog"
} >> "$CRON_TMP"
crontab "$CRON_TMP" && { say "[FIX] cron: collector + ttyd watchdog"; changed=1; }
rm -f "$CRON_TMP"

# Kick ttyd now so the terminal panes work before next minute (the repair
# server is started by lib/steps.py::install_dashboard, as root).
[ -n "$TTYD_CMD" ] && { pgrep -x ttyd >/dev/null 2>&1 || \
    /usr/sbin/daemon -r -o "$LOG" $TTYD_CMD 2>/dev/null; }

# ── XDG autostart for the desktop window (KDE honours this) ─────────────────
mkdir -p "$AUTOSTART"
DESKTOP="$AUTOSTART/status-dashboard.desktop"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Status Dashboard
Comment=System status on the desktop background layer
Exec=env DASHBOARD_URL=http://127.0.0.1:8099/index.html DASHBOARD_BROWSER=$BROWSER DASHBOARD_GEOMETRY=$GEO $BIN/status-dashboard-run.sh
X-KDE-autostart-phase=2
NoDisplay=true
EOF
say "[FIX] wrote autostart entry"; changed=1

# ── KWin below-layer rule (KDE — same KWin the Linux edition targeted) ──────
if command -v kwriteconfig6 >/dev/null 2>&1; then
    "$BIN/status-dashboard-show.sh" --rule-only >/dev/null 2>&1 \
        && say "[OK] KWin below-layer rule present"
else
    say "[SKIP] not KDE — set the dashboard window 'keep below' in your WM"
fi

# ── start the window now if we're in a graphical session ────────────────────
if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    pgrep -f "status-dashboard-run.sh" >/dev/null 2>&1 || \
        ( env DASHBOARD_URL=http://127.0.0.1:8099/index.html \
              DASHBOARD_BROWSER="$BROWSER" DASHBOARD_GEOMETRY="$GEO" \
              "$BIN/status-dashboard-run.sh" >/dev/null 2>&1 & )
    say "[FIX] launched dashboard window"
fi

[ $changed -eq 0 ] && say "[OK] dashboard already installed" || say "[OK] dashboard install/repair complete"
