#!/bin/sh
# status-dashboard-show.sh — FreeBSD/KDE edition. Install the KWin rule that
# pins the dashboard window borderless, maximized and BELOW everything, so it
# behaves like a live, scrollable wallpaper (Meta+D reveals it). KDE Plasma on
# FreeBSD uses the same KWin as on Linux, so the rule is byte-for-byte the same;
# only the tooling around it (systemd, qdbus binary name) changes.
#
#   status-dashboard-show.sh --rule-only   # install/refresh the rule, exit
#   status-dashboard-show.sh --restart | --stop
set -u

URL="http://127.0.0.1:8099/index.html"
PROFILE="$HOME/.local/share/status-dashboard/chrome-profile"
TITLE_MATCH="system status"
CLASS_MATCH="chrome-127.0.0.1__index.html-Default|status-dashboard"
RULES="$HOME/.config/kwinrulesrc"
RULE_ID="statusdashboard"

# The below-layer rule pins the window by WM_CLASS, which only a
# Chromium-family launch sets (--class=status-dashboard). With a
# firefox-family dashboard browser there is nothing to match -- don't
# write a dead rule, just say so. Same detection order as
# status-dashboard-install.sh / status-dashboard-run.sh (Chromium family
# preferred, librewolf/firefox as the fallback this installer itself
# deploys).
dash_browser_family() {
    for b in chrome chromium ungoogled-chromium brave falkon librewolf firefox; do
        command -v "$b" >/dev/null 2>&1 || continue
        case "$b" in
            librewolf|firefox) echo firefox ;;
            *)                 echo chromium ;;
        esac
        return
    done
    echo none
}

reconfigure() {
    qdbus6 org.kde.KWin /KWin reconfigure 2>/dev/null \
        || qdbus-qt6 org.kde.KWin /KWin reconfigure 2>/dev/null \
        || qdbus org.kde.KWin /KWin reconfigure 2>/dev/null || true
}

install_kwin_rule() {
    if [ "$(dash_browser_family)" != chromium ]; then
        echo "KWin below-layer rule skipped: needs a Chromium-family browser"
        return 0
    fi
    if ! grep -q "^\[$RULE_ID\]" "$RULES" 2>/dev/null; then
        cat >> "$RULES" <<RULE

[$RULE_ID]
Description=System status dashboard (desktop background layer)
title=$TITLE_MATCH
titlematch=2
types=1
wmclass=$CLASS_MATCH
wmclasscomplete=false
wmclassmatch=3
below=true
belowrule=2
noborder=true
noborderrule=2
skiptaskbar=true
skiptaskbarrule=2
skippager=true
skippagerrule=2
skipswitcher=true
skipswitcherrule=2
placement=4
placementrule=2
maximizehoriz=true
maximizehorizrule=2
maximizevert=true
maximizevertrule=2
RULE
        python3.11 - "$RULES" "$RULE_ID" <<'PY'
import re, sys
path, rid = sys.argv[1], sys.argv[2]
text = open(path).read()
m = re.search(r"^\[General\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
if m:
    block = m.group(1)
    ids = re.search(r"^rules=(.*)$", block, re.M)
    have = [x for x in (ids.group(1).split(",") if ids else []) if x]
    if rid not in have:
        have.append(rid)
    new = re.sub(r"^rules=.*$", "rules=" + ",".join(have), block, flags=re.M) \
        if ids else block.rstrip("\n") + "\nrules=" + ",".join(have) + "\n"
    new = re.sub(r"^count=.*$", f"count={len(have)}", new, flags=re.M) \
        if re.search(r"^count=", new, re.M) else f"count={len(have)}\n" + new
    text = text[:m.start(1)] + new + text[m.end(1):]
else:
    text = f"[General]\ncount=1\nrules={rid}\n" + text
open(path, "w").write(text)
PY
        echo "installed KWin rule [$RULE_ID]"
    fi
    reconfigure
}

case "${1:-}" in
    --stop)      pkill -f "status-dashboard/chrome-profile" 2>/dev/null; echo "dashboard stopped"; exit 0 ;;
    --restart)   pkill -f "status-dashboard/chrome-profile" 2>/dev/null; sleep 1 ;;
    --rule-only) install_kwin_rule; exit 0 ;;
esac

pgrep -f "status-dashboard/chrome-profile" >/dev/null 2>&1 && { echo "already running (--restart to reload)"; exit 0; }
install_kwin_rule
env DASHBOARD_URL="$URL" "$HOME/.local/bin/status-dashboard-run.sh" >/dev/null 2>&1 &
echo "dashboard starting — $URL"
