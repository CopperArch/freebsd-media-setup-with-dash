#!/bin/sh
# firstboot-kde.sh — take a base FreeBSD 15.1 install to a KDE Plasma 6 desktop
# with the Bastille jail substrate ready for the media stack.
#
# Safe to re-run: every step is idempotent (pkg install is a no-op if present,
# sysrc just re-asserts the knob). This is the FreeBSD analogue of the Linux
# edition installing the desktop session the dashboard lives on.
set -eu

log() { echo "[firstboot] $*"; }

# ── hardware summary (also written to the log for post-install triage) ──
log "CPU: $(sysctl -n hw.model 2>/dev/null)"
log "Memory: $(( $(sysctl -n hw.physmem 2>/dev/null || echo 0) / 1024 / 1024 )) MB"
log "Disks: $(sysctl -n kern.disks 2>/dev/null)"
log "Display controllers present:"
pciconf -lv 2>/dev/null | awk '
    /^[^ \t]/   { dev = $1; sub(/:$/, "", dev) }
    /^[ \t]+vendor/ { v = $0; sub(/^[ \t]+vendor[ \t]+=[ \t]+/, "", v) }
    /^[ \t]+device/ { d = $0; sub(/^[ \t]+device[ \t]+=[ \t]+/, "", d) }
    /^[ \t]+subclass/ { if ($0 ~ /[Vv][Gg][Aa]|3D|[Dd]isplay/) print "  " dev ": " v " " d }
' | while IFS= read -r l; do log "$l"; done

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
# Detect display hardware by PCI VENDOR ID, not by name greps. The old
# approach (grep 'AMD\|Radeon', then 'Intel.*Graphics') had two real holes:
# Intel Arc boards whose device string says "DG2" or "Arc" but never
# "Graphics" fell through entirely, and an NVIDIA card matched nothing at
# all. The numeric vendor= code on each pciconf device header is
# authoritative, and only blocks whose subclass is actually a display
# controller (VGA / 3D / Display) count -- so a box's BMC/ASPEED chip or a
# virtual GPU never causes a KMS driver to be loaded.
#   8086 = Intel (covers HD/Iris/Arc -- i915kms handles the whole family)
#   1002 = AMD/ATI (amdgpu covers GCN through RDNA)
#   10de = NVIDIA (nvidia-drm-kmod meta-package + nvidia-modeset)
#   15ad/1af4/1b36/1234/1013/15d1 = VMware/virtio/QEMU/Bochs/Cirrus/GVNIC
#     virtual display hardware -- skip the driver, scfb/efifb is correct.
GPU_VIDS="$(pciconf -lv 2>/dev/null | awk '
    /^[^ \t]/ {
        if (sub_ok && vid != "") print vid
        vid = ""; sub_ok = 0
        if (match($0, /vendor=0x[0-9a-fA-F]+/))
            vid = tolower(substr($0, RSTART + 9, RLENGTH - 9))
    }
    /^[ \t]+subclass/ {
        if ($0 ~ /VGA|3D|[Dd]isplay/) sub_ok = 1
    }
    END { if (sub_ok && vid != "") print vid }
' | sort -u)"
INTEL=0; AMD=0; NVIDIA=0; VIRTUAL=0
for vid in $GPU_VIDS; do
    case "$vid" in
        8086)          INTEL=1 ;;
        1002|1022)     AMD=1 ;;
        10de)          NVIDIA=1 ;;
        15ad|1af4|1b36|1234|1013|15d1) VIRTUAL=1 ;;
        *)             log "  unknown display vendor 0x$vid -- ignoring" ;;
    esac
done
if [ "$INTEL" = 1 ]; then
    log "Intel display adapter found (HD/Iris/Arc all use the same driver)"
    sysrc kld_list+="i915kms"
fi
if [ "$AMD" = 1 ]; then
    log "AMD/ATI display adapter found"
    sysrc kld_list+="amdgpu"
fi
if [ "$NVIDIA" = 1 ]; then
    log "NVIDIA display adapter found"
    env ASSUME_ALWAYS_YES=YES pkg install -y nvidia-drm-kmod \
        || log "  nvidia-drm-kmod install failed (proprietary driver fetch?)"
    sysrc kld_list+="nvidia-modeset"
fi
if [ "$INTEL" = 0 ] && [ "$AMD" = 0 ] && [ "$NVIDIA" = 0 ]; then
    if [ "$VIRTUAL" = 1 ]; then
        log "Only virtual/BMC display hardware found (VM?) -- no KMS driver, scfb/efifb it is"
    else
        log "No display hardware detected -- skipping GPU driver (headless?)"
    fi
fi
# Persist across reboots; a hybrid Intel+NVIDIA laptop intentionally loads
# both (KMS drivers coexist; the NVIDIA one only owns its own outputs).

# ── KDE Plasma 6 + SDDM on X11 (the stable session on FreeBSD in 2026) ──
log "Installing KDE Plasma 6, SDDM, Xorg"
# -f: some port in this dependency graph still names plain "pkg" instead of
# accepting the pkg-devel swap above, which reopens the exact same file
# conflict from the other direction. Forcing it back is safe -- it's the
# same self-replacement pkg already knows how to do, just refusing to do to
# itself without -f. The Plasma 6 meta-package is `kde6` (`kde` is the
# retired Plasma 5 name and 404s on 15.1).
env ASSUME_ALWAYS_YES=YES pkg install -f -y plasma6-plasma-desktop sddm xorg konsole

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
