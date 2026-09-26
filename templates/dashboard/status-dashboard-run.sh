#!/bin/sh
# status-dashboard-run.sh — FreeBSD edition. Launch the dashboard browser window
# and block for its lifetime, so a cron/daemon watchdog can tell whether it's up.
#
# Much simpler than the Linux version: there's no systemd and no process-scope
# escape on FreeBSD, so the browser is an ordinary child we can wait on. On exit
# (session logout / --stop) the trap tears the window down with us.
set -u

URL="${DASHBOARD_URL:-http://127.0.0.1:8099/index.html}"
GEOMETRY="${DASHBOARD_GEOMETRY:-1920,1080}"
PROFILE="$HOME/.local/share/status-dashboard/chrome-profile"
BIN="$HOME/.local/bin"

BROWSER="${DASHBOARD_BROWSER:-}"
if [ -z "$BROWSER" ]; then
    for b in chrome chromium ungoogled-chromium brave falkon librewolf firefox; do
        command -v "$b" >/dev/null 2>&1 && { BROWSER="$(command -v $b)"; break; }
    done
fi
[ -n "$BROWSER" ] || { echo "no browser found (install chromium or librewolf)" >&2; exit 1; }
case "${BROWSER##*/}" in
    librewolf|firefox) FAMILY=firefox ;;
    *)                 FAMILY=chromium ;;
esac

# Clear a stale window from a previous session (its singleton would swallow
# us). The profile path is the one string present on both launch styles'
# command lines (--user-data-dir=... vs --profile ...).
pkill -f "status-dashboard/chrome-profile" 2>/dev/null && sleep 1

cleanup() { pkill -f "status-dashboard/chrome-profile" 2>/dev/null; exit 0; }
trap cleanup TERM INT EXIT

# Wait for the local server (cold boot can beat it), then install the KWin rule.
i=0; while [ $i -lt 30 ]; do
    fetch -qo /dev/null "$URL" 2>/dev/null && break
    sleep 1; i=$((i+1))
done
[ -x "$BIN/status-dashboard-show.sh" ] && "$BIN/status-dashboard-show.sh" --rule-only >/dev/null 2>&1

if [ "$FAMILY" = firefox ]; then
    # Firefox-family: --app/--user-data-dir/--class don't exist, and
    # --profile refuses to start if the directory is missing. The window
    # won't carry a matchable WM_CLASS, so the KWin rule above is skipped
    # by status-dashboard-show.sh for this family.
    mkdir -p "$PROFILE"
    "$BROWSER" \
        --profile "$PROFILE" \
        --new-window "$URL" \
        >/dev/null 2>&1 &
else
    mkdir -p "$PROFILE"
    "$BROWSER" \
        --app="$URL" \
        --user-data-dir="$PROFILE" \
        --class=status-dashboard \
        --window-position=0,0 \
        --window-size="$GEOMETRY" \
        --start-maximized \
        --no-first-run \
        --no-default-browser-check \
        --password-store=basic \
        --disable-features=TranslateUI,InfiniteSessionRestore \
        --disable-session-crashed-bubble \
        --hide-crash-restore-bubble \
        --disable-infobars \
        --noerrdialogs \
        >/dev/null 2>&1 &
fi
BPID=$!
echo "dashboard running — $URL (browser pid $BPID)"

# Block for the browser's lifetime; it's our child on FreeBSD, so this is exact.
while kill -0 "$BPID" 2>/dev/null; do sleep 5; done
echo "dashboard browser exited"
