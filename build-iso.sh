#!/bin/sh
# build-iso.sh — bake an unattended KDE media-server install ISO.
#
# In the spirit of CopperArch BSD's build-iso.sh: take the official FreeBSD
# release image and inject our installerconfig so booting it installs the base
# OS onto ZFS (disk auto-picked on the target machine, never the USB stick
# itself) and then builds KDE + the Bastille stack on first boot — finishing
# with the interactive setup wizard on the console. No interactive prompts
# anywhere in the OS install itself.
#
# MUST run on a FreeBSD host as root (uses cd9660 mount + makefs/mkisoimages
# from base).
#
#   ./build-iso.sh                 # fetch + build for the running release
#   ./build-iso.sh 15.1-RELEASE    # explicit release
#
# The output is an ordinary hybrid ISO: dd it to a USB stick, boot any
# machine from it (BIOS or UEFI when built with /usr/src present), and walk
# away — it installs, reboots, builds the desktop, and then asks you for the
# few things only a human can answer.
set -eu

[ "$(id -u)" = 0 ] || { echo "build-iso.sh must run as root (mdconfig/mount/makefs)"; exit 1; }
[ "$(uname -s)" = "FreeBSD" ] || { echo "build-iso.sh must run on FreeBSD"; exit 1; }

REL="${1:-$(freebsd-version -u | sed 's/-p[0-9]*//')}"
ARCH="$(uname -m)"
# dvd1, not disc1: disc1.iso is a network-install image (ships a MANIFEST,
# not the actual base.txz/kernel.txz), so it can't produce a truly
# unattended/offline install. dvd1.iso bundles the distribution sets.
# Cached OUTSIDE the repo dir ($HERE), on purpose: this used to fetch into
# the CURRENT directory, which is normally $HERE itself -- and the repo-copy
# step below (`cp -a "$HERE"/ "$DEST"/`) then happily baked that 4.6GB
# scratch download INTO the built ISO's own filesystem as a side effect,
# doubling the final image size (9.3GB instead of ~4.6GB) for no reason.
# Confirmed live: mounted a built ISO and found the base dvd1.iso sitting
# inside it at /usr/local/freebsd-media-setup/. A stable cache location
# also means a second build doesn't re-download 4.6GB every time.
CACHE="${TMPDIR:-/tmp}/copperarch-iso-cache"
BASE_NAME="FreeBSD-${REL}-${ARCH}-dvd1.iso"
BASE="$CACHE/$BASE_NAME"
URL="https://download.freebsd.org/releases/ISO-IMAGES/${REL%-RELEASE}/${BASE_NAME}"
WORK="$(mktemp -d /tmp/copperarch-iso.XXXXXX)"
OUT="CopperArch-Media-${REL}-${ARCH}.iso"
HERE="$(cd "$(dirname "$0")" && pwd)"
MD=""

cleanup() {
    if [ -n "$MD" ]; then
        umount "$WORK/mnt" 2>/dev/null || true
        umount "$WORK/verify" 2>/dev/null || true
        mdconfig -d -u "$MD" 2>/dev/null || true
        MD=""
    fi
}
trap cleanup EXIT INT TERM

echo ">> release=$REL arch=$ARCH work=$WORK cache=$CACHE"

mkdir -p "$CACHE"
[ -f "$BASE" ] || { echo ">> fetching $URL"; fetch -o "$BASE" "$URL"; }

echo ">> unpacking official ISO"
MNT="$WORK/mnt"; ROOT="$WORK/root"
mkdir -p "$MNT" "$ROOT"
MD=$(mdconfig -a -t vnode -f "$BASE")
mount -t cd9660 "/dev/$MD" "$MNT"
cp -a "$MNT"/ "$ROOT"/
umount "$MNT"; mdconfig -d -u "$MD"; MD=""

echo ">> injecting installerconfig (unattended) + repo copy (gitignored/personal files excluded)"
chmod -R u+w "$ROOT"
cp "$HERE/bootstrap/installerconfig" "$ROOT/etc/installerconfig"
# Ship the repo on the media so first boot doesn't depend on cloning -- but
# never ship anything .gitignore marks as personal/local (real-secret
# profiles) or as build cruft (__pycache__, logs), even though this working
# copy may not be an actual git repo itself. cp -a alone would happily bake
# in a my-profile.yaml full of real WireGuard keys/passwords sitting next to
# example.yaml, since it has no concept of .gitignore.
DEST="$ROOT/usr/local/freebsd-media-setup"
mkdir -p "$DEST"
cp -a "$HERE"/ "$DEST"/
rm -rf "$DEST/.git"
# Belt-and-suspenders: never ship a stray ISO/disk image some other tool
# (or an old manual `fetch` into this same directory) left sitting in $HERE
# -- .gitignore doesn't list these since they were never meant to be there
# at all, but if one IS there, it must never end up baked into the image.
find "$DEST" -maxdepth 1 \( -name "*.iso" -o -name "*.qcow2" -o -name "*.img" \) -exec rm -f {} +
if [ -f "$HERE/.gitignore" ]; then
    while IFS= read -r pat; do
        case "$pat" in ''|'#'*) continue ;; esac
        pat="${pat#/}"; pat="${pat%/}"
        case "$pat" in
            */*) find "$DEST" -path "$DEST/$pat" -exec rm -rf {} + 2>/dev/null ;;
            *)   find "$DEST" -name "$pat" -exec rm -rf {} + 2>/dev/null ;;
        esac
    done < "$HERE/.gitignore"
fi

echo ">> repacking bootable ISO -> $OUT"
# mkisoimages.sh ships in /usr/src/release; makefs is the src-free fallback.
# The mkisoimages.sh path produces a BIOS+UEFI bootable image; the makefs
# fallback is BIOS-only unless the tree carries an efiboot.img to add as a
# second boot image -- flagged loudly, because a BIOS-only ISO will not boot
# a UEFI-only machine.
MAKEFS_OPTS="-t cd9660 -o rockridge,label=COPPERARCH -o bootimage=i386;$ROOT/boot/cdboot -o no-emul-boot"
if [ -f "$ROOT/boot/efiboot.img" ]; then
    MAKEFS_OPTS="$MAKEFS_OPTS -o bootimage=efi;$ROOT/boot/efiboot.img"
    echo ">> UEFI boot image found in the release tree -- media will be BIOS+UEFI"
fi
if [ -x /usr/src/release/"${ARCH}"/mkisoimages.sh ]; then
    sh /usr/src/release/"${ARCH}"/mkisoimages.sh -b COPPERARCH "$OUT" "$ROOT"
elif [ -f "$ROOT/boot/efiboot.img" ]; then
    # shellcheck disable=SC2086
    makefs $MAKEFS_OPTS "$OUT" "$ROOT"
else
    echo ">> WARNING: no /usr/src and no efiboot.img in the release tree --"
    echo ">>          the built media will be BIOS-boot only. Install /usr/src"
    echo ">>          (or fetch mkisoimages.sh) for a UEFI-bootable image."
    # shellcheck disable=SC2086
    makefs $MAKEFS_OPTS "$OUT" "$ROOT"
fi

echo ">> verifying built image"
VERIFY="$WORK/verify"
mkdir -p "$VERIFY"
MD=$(mdconfig -a -t vnode -f "$OUT")
mount -t cd9660 "/dev/$MD" "$VERIFY"
fail() { echo ">> VERIFY FAILED: $1"; umount "$VERIFY"; mdconfig -d -u "$MD"; MD=""; exit 1; }
[ -f "$VERIFY/etc/installerconfig" ] || fail "installerconfig missing from the image"
[ -f "$VERIFY/usr/local/freebsd-media-setup/install.py" ] || fail "bundled repo (install.py) missing"
[ -f "$VERIFY/usr/local/freebsd-media-setup/bootstrap/firstboot-kde.sh" ] || fail "bundled repo (firstboot-kde.sh) missing"
[ -x "$VERIFY/usr/local/freebsd-media-setup/rebuild" ] || fail "bundled repo (rebuild) missing"
if find "$VERIFY/usr/local/freebsd-media-setup" -name "*.iso" | grep -q .; then
    fail "an installer image got baked into the repo copy"
fi
if find "$VERIFY/usr/local/freebsd-media-setup" -name "*.yaml" -path "*profiles*" ! -name "example.yaml" | grep -q .; then
    fail "a non-example (real-secret?) profile got baked into the image"
fi
umount "$VERIFY"; mdconfig -d -u "$MD"; MD=""
echo ">> OK: installerconfig + bundled repo present, no stray media or profiles"

rm -rf "$WORK"
SIZE=$(ls -lh "$OUT" | awk '{print $5}')
echo
echo ">> done: $OUT ($SIZE)"
echo ">> write it to a USB stick (replace da0 -- CHECK THE DEVICE NAME):"
echo ">>   dd if=$OUT of=/dev/daX bs=4M conv=sync   (or use it in a VM)"
echo ">> then boot the target machine from USB. It will:"
echo ">>   1. install FreeBSD onto its own boot disk (auto-picked, USB stick"
echo ">>      excluded; two internal disks = mirrored ZFS root),"
echo ">>   2. reboot, build KDE + Bastille unattended,"
echo ">>   3. ask for root/copper passwords, then run the setup wizard on"
echo ">>      the console for app logins, VPN keys and public access."
