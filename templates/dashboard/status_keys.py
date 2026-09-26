"""Shared config for the FreeBSD status dashboard scripts.

Imported by both status-collect.py and status-dashboard-server.py. Credentials
live in ~/.config/status-dashboard/media-keys.env (chmod 600); the VPN-confined
jail group lives in vpn-group.json so the collector and the repair server can't
drift apart (the Linux edition's netns-group.json, renamed for what it is on
FreeBSD: the jails whose default route is the vpn jail's tunnel).
"""
import json
import os
from pathlib import Path

# DASHBOARD_HOME overrides Path.home() when the caller's own identity isn't
# the desktop user's -- status-dashboard-server.py runs as root (its repair
# actions need to restart jails/services/pkg, which the desktop user can't),
# but its data still lives under that user's actual home directory, not
# /root. status-collect.py and friends still just run as the desktop user,
# so Path.home() is correct for them and this env var is never set.
_HOME = Path(os.environ["DASHBOARD_HOME"]) if "DASHBOARD_HOME" in os.environ else Path.home()
CONF_DIR = _HOME / ".config/status-dashboard"
KEYS_FILE = CONF_DIR / "media-keys.env"
GROUP_FILE = CONF_DIR / "vpn-group.json"

# Kept in sync with stacks/stack.yaml (subnet + .N per jail).
SUBNET = "10.17.0"
JAIL_IPS = {"jellyfin": 10, "plex": 11, "prowlarr": 12, "bazarr": 13,
            "seerr": 14, "caddy": 15, "nextcloud-db": 16, "nextcloud": 17,
            "vpn": 20, "qbittorrent": 21,
            "sonarr": 22, "radarr": 23}

DEFAULT_GROUP = {"hub": "vpn", "members": ["qbittorrent", "sonarr", "radarr"]}


def jail_ip(name):
    return f"{SUBNET}.{JAIL_IPS.get(name, 1)}"


def load_env(path=KEYS_FILE):
    env = {}
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_group():
    """(hub, members) — the vpn jail and the jails confined to its tunnel.
    Falls back to the built-in list if the file is missing/corrupt."""
    try:
        data = json.loads(GROUP_FILE.read_text())
        hub, members = data.get("hub"), data.get("members")
        if (isinstance(hub, str) and hub and isinstance(members, list) and members
                and all(isinstance(s, str) and s for s in members)):
            return hub, [str(s) for s in members]
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_GROUP["hub"], list(DEFAULT_GROUP["members"])
