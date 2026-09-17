#!/bin/sh
# firstboot-kde.sh — take a base FreeBSD 15.1 install to a KDE Plasma 6 desktop
# with the Bastille jail substrate ready for the media stack.
#
# Safe to re-run: every step is idempotent (pkg install is a no-op if present,
# sysrc just re-asserts the knob). This is the FreeBSD analogue of the Linux
# edition installing the desktop session the dashboard lives on.
set -eu

log() { echo "[firstboot] $*"; }

log "Enabling latest pkg branch (arr ports track it more closely than quarterly)"
mkdir -p /usr/local/etc/pkg/repos
cat > /usr/local/etc/pkg/repos/FreeBSD.conf <<'EOF'
FreeBSD-ports: {
  url: "pkg+http://pkg.FreeBSD.org/${ABI}/latest",
  mirror_type: "srv",
  signature_type: "fingerprints",
  fingerprints: "/usr/share/keys/pkg",
  enabled: yes
}
EOF
env ASSUME_ALWAYS_YES=YES pkg update -f

# The "latest" branch's KDE dependency chain requires pkg>=2.8.99, which on
# this branch only pkg-devel provides. It owns the same files as the stock
# pkg package, so ASSUME_ALWAYS_YES alone won't swap it in -- pkg refuses to
# delete itself without -f. Do the swap up front so `pkg install kde` later
# doesn't hit the file conflict mid-run.
log "Swapping to pkg-devel (kde's dependency chain requires pkg>=2.8.99)"
env ASSUME_ALWAYS_YES=YES pkg install -f -y pkg-devel

# ── GPU / KMS: needed for both the desktop AND jellyfin/plex transcode ──
log "Installing drm-kmod (GPU/KMS)"
env ASSUME_ALWAYS_YES=YES pkg install -y drm-kmod
# Load the right module for the GPU. amdgpu covers RDNA (matches your RDNA4
# work); i915kms for Intel. Persist across reboots.
if pciconf -lv 2>/dev/null | grep -qi 'AMD\|Radeon'; then
    sysrc kld_list+="amdgpu"
elif pciconf -lv 2>/dev/null | grep -qi 'Intel.*Graphics'; then
    sysrc kld_list+="i915kms"
fi

# ── KDE Plasma 6 + SDDM on X11 (the stable session on FreeBSD in 2026) ──
log "Installing KDE Plasma 6, SDDM, Xorg"
# -f: some port in this dependency graph still names plain "pkg" instead of
# accepting the pkg-devel swap above, which reopens the exact same file
# conflict from the other direction. Forcing it back is safe -- it's the
# same self-replacement pkg already knows how to do, just refusing to do to
# itself without -f.
env ASSUME_ALWAYS_YES=YES pkg install -f -y kde sddm xorg konsole

# candy-icons: not a FreeBSD port (pkg search comes up empty), just an SVG
# icon theme -- fetch it straight from upstream like this project already
# does for Seerr when no package exists. GPL-3.0, ~3.6MB, safe to bundle.
log "Installing candy-icons theme (no FreeBSD port -- from upstream)"
if [ ! -d /usr/local/share/icons/candy-icons ]; then
    env ASSUME_ALWAYS_YES=YES pkg install -y git >/dev/null 2>&1 || true
    git clone --depth 1 https://github.com/EliverLara/candy-icons.git \
        /usr/local/share/icons/candy-icons \
        || log "candy-icons clone failed, will retry on next run"
fi

# Sweet Plasma style: also EliverLara, CC BY-SA 4.0, no FreeBSD port and no
# clean upstream git repo for the Plasma-specific desktoptheme package (the
# author's github.com/EliverLara/Sweet repo is the GTK/GNOME-Shell theme;
# these files are the Plasma desktoptheme variant) -- bundled directly in
# this project's own templates instead, copied wholesale like candy-icons
# (system-wide, so it's available to any user, not just the profile user).
log "Installing Sweet Plasma style (bundled, no FreeBSD port)"
SWEET_SRC="/usr/local/freebsd-media-setup/templates/kde-theme/Sweet"
if [ -d "$SWEET_SRC" ] && [ ! -d /usr/local/share/plasma/desktoptheme/Sweet ]; then
    mkdir -p /usr/local/share/plasma/desktoptheme
    cp -a "$SWEET_SRC" /usr/local/share/plasma/desktoptheme/Sweet
fi

# Desktop wallpaper: same CopperArch logo as the dashboard (full colour, on
# the dashboard's own dark background colour) -- bundled project asset, no
# fetch needed. System-wide like the theme/icon assets above.
WALLPAPER_SRC="/usr/local/freebsd-media-setup/templates/dashboard/copperarch-wallpaper.png"
WALLPAPER_DST="/usr/local/share/wallpapers/CopperArch/copperarch-wallpaper.png"
if [ -f "$WALLPAPER_SRC" ] && [ ! -f "$WALLPAPER_DST" ]; then
    mkdir -p "$(dirname "$WALLPAPER_DST")"
    cp "$WALLPAPER_SRC" "$WALLPAPER_DST"
fi

log "Enabling desktop services"
sysrc dbus_enable="YES"
sysrc sddm_enable="YES"
sysrc moused_enable="YES"
# Default SDDM session to Plasma X11 (Wayland is improving but not the safe
# daily-driver on FreeBSD yet).
mkdir -p /usr/local/etc/sddm.conf.d
cat > /usr/local/etc/sddm.conf.d/session.conf <<'EOF'
[Autologin]
Session=plasmax11.desktop
EOF

# Proc + fdesc filesystems some KDE bits expect.
grep -q '^proc' /etc/fstab || echo 'proc /proc procfs rw 0 0' >> /etc/fstab
mount -a || true

# ── Bastille: the container substrate that replaces Docker ──────────────
log "Installing + bootstrapping Bastille"
env ASSUME_ALWAYS_YES=YES pkg install -y bastille
sysrc bastille_enable="YES"
# This whole project's jail story (stack.yaml, ZFS-snapshot self-heal in
# daily-routine.sh) assumes ZFS-backed jails, but bastille.conf defaults to
# plain files -- without this, `bastille bootstrap` refuses outright
# ("ZFS is enabled in rc.conf but not bastille.conf").
ZPOOL="$(zpool list -H -o name | head -1)"
sed -i '' -e "s/^bastille_zfs_enable=.*/bastille_zfs_enable=\"YES\"/" \
          -e "s/^bastille_zfs_zpool=.*/bastille_zfs_zpool=\"${ZPOOL}\"/" \
          /usr/local/etc/bastille/bastille.conf
# Bring up the release the jails are cloned from (matches stack.yaml).
REL="$(freebsd-version -u | sed 's/-p[0-9]*//')"
bastille bootstrap "${REL}" update || \
    log "bastille bootstrap will retry on next run if the network wasn't ready"

# Bridge for VNET jails (so each jail gets its own stack + the vpn jail can own
# wg0). Persisted in rc.conf.
if ! ifconfig bastille0 >/dev/null 2>&1; then
    sysrc cloned_interfaces+="bridge0"
    sysrc ifconfig_bridge0_name="bastille0"
fi

# ── installer prerequisites (the Python app installer wants these) ──────
log "Installing installer prerequisites"
env ASSUME_ALWAYS_YES=YES pkg install -y python311 py311-tkinter git curl jq
# py311-yaml doesn't exist on this ports snapshot -- PyYAML is only built
# against py312+ here. Rather than re-pin the whole project's python3.11
# everywhere (install.py, the dashboard scripts, lib/platform.py all assume
# it), get it from pip for this one interpreter instead.
python3.11 -m ensurepip
python3.11 -m pip install --quiet pyyaml

log "Done. Reboot into KDE, then run: python3.11 install.py"
