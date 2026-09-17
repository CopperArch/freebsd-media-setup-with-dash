#!/bin/sh
# Deployed at /usr/local/freebsd-media-setup/scripts/drive-hotplug.sh, run by
# devd(8) (as root) whenever a new whole disk shows up -- see
# templates/devd-drive-hotplug.conf.tmpl for the matching rule.
#
# Just records the disk as "unclaimed" for the dashboard to surface via its
# existing attention-panel fix-button pattern (fix id: pool_add_drive, see
# status-dashboard-server.py); never touches the disk itself. One empty file
# per disk under MARKER_DIR avoids any shared-file write race between
# overlapping devd invocations.
set -eu

MARKER_DIR="/var/db/freebsd-media-setup/hotplug-disks"
disk="${1:-}"
[ -n "$disk" ] || exit 0

mkdir -p "$MARKER_DIR"

# Already a pool member (reattached disk, or a pool rebuilding itself) --
# not a candidate, don't flag it.
if zpool status 2>/dev/null | awk '{print $1}' | grep -qx "$disk"; then
	exit 0
fi

touch "$MARKER_DIR/$disk"
