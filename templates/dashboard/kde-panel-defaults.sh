#!/bin/sh
# kde-panel-defaults.sh — FreeBSD/KDE edition. One-time cosmetic default:
# move the Plasma panel to the top edge, make it opaque, set its thickness
# to match the reference desktop this project is modeled on, switch the
# icon theme to candy-icons, set the Plasma style to Sweet with Breeze Dark
# colors, and set the desktop wallpaper to the same CopperArch logo the
# dashboard shows. Runs once per user via XDG autostart; a marker file
# stops it from re-fighting anything the user customizes by hand afterward.
#
# Two separate config files are involved for the panel itself, not one:
# edge/opacity live in plasma-org.kde.plasma.desktop-appletsrc
# ([Containments][N]), thickness lives in plasmashellrc
# ([PlasmaViews][Panel N][Defaults]) -- confirmed by inspecting both files
# after making each change live via plasmashell's own scripting console,
# since the panel scripting object's "opacityMode" property is
# undefined/unscriptable in this Plasma build and "height" is a read-only
# computed value (the real settable property is "thickness").
set -u

MARKER="$HOME/.config/status-dashboard/.kde-panel-defaults-applied"
[ -f "$MARKER" ] && exit 0

APPLETSRC="$HOME/.config/plasma-org.kde.plasma.desktop-appletsrc"
SHELLRC="$HOME/.config/plasmashellrc"
KDEGLOBALS="$HOME/.config/kdeglobals"
PLASMARC="$HOME/.config/plasmarc"
THICKNESS=38

# This runs from autostart, which can race plasmashell's own first-run
# generation of the default layout -- wait for it to actually exist.
i=0
while [ ! -f "$APPLETSRC" ] && [ "$i" -lt 30 ]; do sleep 1; i=$((i + 1)); done
[ -f "$APPLETSRC" ] || exit 0

# Find the panel's containment ID dynamically -- never hardcode it, Plasma
# assigns IDs sequentially at first-login time and a future default-layout
# change could shift which number lands on the panel.
PANEL_ID="$(awk '
    /^\[Containments\]\[[0-9]+\]$/ {
        id = $0
        sub(/^\[Containments\]\[/, "", id)
        sub(/\]$/, "", id)
        next
    }
    /^plugin=org\.kde\.panel$/ { print id; exit }
' "$APPLETSRC")"
[ -n "$PANEL_ID" ] || exit 0

# Same idea for the desktop containment (holds the wallpaper), never
# hardcoded either.
DESKTOP_ID="$(awk '
    /^\[Containments\]\[[0-9]+\]$/ {
        id = $0
        sub(/^\[Containments\]\[/, "", id)
        sub(/\]$/, "", id)
        next
    }
    /^plugin=org\.kde\.plasma\.folder$/ { print id; exit }
' "$APPLETSRC")"

# plasmashell holds its own in-memory copy of both config files and
# periodically flushes it back to disk, which silently clobbers an external
# edit made while it's still running -- confirmed live: the same edit made
# without this quit/restart cycle never took visible effect. Quit first so
# our edit lands on a quiescent file, then restart so it's picked back up.
pkill -u "$(id -un)" -f plasmashell 2>/dev/null
i=0
while pgrep -u "$(id -un)" -f plasmashell >/dev/null 2>&1 && [ "$i" -lt 10 ]; do
    sleep 1
    i=$((i + 1))
done

# location=3 is Plasma's Types::Location::TopEdge (BottomEdge=4 confirmed
# empirically as the fresh-install default before this script runs).
kwriteconfig6 --file "$APPLETSRC" --group Containments --group "$PANEL_ID" \
    --key location 3
# panelOpacity: 0=Adaptive (translucent floating, opaque once a window
# touches/maximizes under it -- both transparent AND opaque, contextually),
# 1=Opaque (always solid), 2=Translucent (always see-through).
kwriteconfig6 --file "$APPLETSRC" --group Containments --group "$PANEL_ID" \
    --key panelOpacity 0
kwriteconfig6 --file "$SHELLRC" --group PlasmaViews \
    --group "Panel $PANEL_ID" --group Defaults --key thickness "$THICKNESS"

# candy-icons is deployed system-wide to /usr/local/share/icons by
# firstboot-kde.sh (no FreeBSD port exists for it); only apply it here if
# that actually succeeded, so a failed/offline clone doesn't leave the user
# on a named theme KDE can't find.
if [ -f /usr/local/share/icons/candy-icons/index.theme ]; then
    kwriteconfig6 --file "$KDEGLOBALS" --group Icons --key Theme candy-icons
fi

# Sweet (Plasma style, panels/plasmoid chrome) + Breeze Dark (color scheme,
# governs everything else -- windows, widgets). Sweet is deployed system-wide
# to /usr/local/share/plasma/desktoptheme by firstboot-kde.sh; same guard
# pattern as candy-icons above so a missing/failed deploy never points KDE
# at a style it can't find. Breeze Dark ships with KDE itself, no guard needed.
if [ -f /usr/local/share/plasma/desktoptheme/Sweet/metadata.desktop ]; then
    kwriteconfig6 --file "$PLASMARC" --group Theme --key name Sweet
fi
kwriteconfig6 --file "$KDEGLOBALS" --group General --key ColorScheme BreezeDark

# Desktop wallpaper: same CopperArch logo as the dashboard, deployed
# system-wide by firstboot-kde.sh -- same existence guard as the theme/icon
# assets above.
WALLPAPER=/usr/local/share/wallpapers/CopperArch/copperarch-wallpaper.png
if [ -n "$DESKTOP_ID" ] && [ -f "$WALLPAPER" ]; then
    kwriteconfig6 --file "$APPLETSRC" --group Containments \
        --group "$DESKTOP_ID" --group Wallpaper --group org.kde.image \
        --group General --key Image "$WALLPAPER"
fi

(plasmashell < /dev/null > /dev/null 2>&1 &)

mkdir -p "$(dirname "$MARKER")"
touch "$MARKER"
