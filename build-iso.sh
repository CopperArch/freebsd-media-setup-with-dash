#!/bin/sh
# build-iso.sh — bake an unattended KDE media-server install ISO.
#
# In the spirit of CopperArch BSD's build-iso.sh: take the official FreeBSD
# release image and inject our installerconfig so booting it installs the base
# OS onto ZFS and then builds KDE + the Bastille stack on first boot — no
# interactive prompts.
#
# MUST run on a FreeBSD host (uses cd9660 mount + makefs/mkisoimages from base).
#
#   ./build-iso.sh                 # fetch + build for the running release
#   ./build-iso.sh 15.1-RELEASE    # explicit release
set -eu

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

echo ">> release=$REL arch=$ARCH work=$WORK cache=$CACHE"

mkdir -p "$CACHE"
[ -f "$BASE" ] || { echo ">> fetching $URL"; fetch -o "$BASE" "$URL"; }

echo ">> unpacking official ISO"
MNT="$WORK/mnt"; ROOT="$WORK/root"
mkdir -p "$MNT" "$ROOT"
MD=$(mdconfig -a -t vnode -f "$BASE")
mount -t cd9660 "/dev/$MD" "$MNT"
cp -a "$MNT"/ "$ROOT"/
umount "$MNT"; mdconfig -d -u "$MD"

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
if [ -x /usr/src/release/"${ARCH}"/mkisoimages.sh ]; then
    sh /usr/src/release/"${ARCH}"/mkisoimages.sh -b COPPERARCH "$OUT" "$ROOT"
else
    makefs -t cd9660 -o rockridge,label=COPPERARCH,bootimage="i386;$ROOT/boot/cdboot" \
        -o no-emul-boot "$OUT" "$ROOT"
fi

echo ">> done: $OUT"
echo ">> write it: dd if=$OUT of=/dev/daX bs=1m   (or use it in a VM)"
rm -rf "$WORK"
