"""Install profile — FreeBSD edition.

Same idea as the Linux edition (pure-data YAML of your choices, chmod 600),
but the VPN section carries a WireGuard keypair instead of gluetun/OpenVPN
username+password, because on FreeBSD the tunnel is kernel WireGuard (if_wg),
not a container. Docker-only knobs (tugtainer secret, immich DB) are dropped.
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def _gen_secret() -> str:
    return secrets.token_hex(16)


@dataclass
class Profile:
    # identity / network
    user: str = "copper"
    lan_ip: str = ""
    lan_cidr: str = "192.168.1.0/24"
    host_ip: str = ""
    jail_subnet: str = "10.17.0"
    jail_release: str = "15.1-RELEASE"
    timezone: str = "Europe/London"
    # domains
    domain: str = ""
    jellyfin_public_url: str = ""
    nextcloud_domain: str = ""         # e.g. cloud.example.com (blank = LAN only)
    # storage (ZFS datasets/paths on the host, nullfs-mounted into jails)
    # blank = auto-detect: installer finds non-OS disks and builds one
    # striped pool at install time. Set explicitly to skip auto-detection.
    media_pool: str = ""
    # optional second media path on the OS disk itself (zroot/media dataset),
    # used ALONGSIDE media_pool, not instead of it -- set by the
    # provision_storage step if the user opts in, never by hand normally.
    media_pool_extra: str = ""
    nextcloud_data: str = "/mnt/nextcloud-data"
    # nextcloud (the Immich replacement — photos/files)
    use_nextcloud: bool = True
    nc_admin_user: str = "admin"
    nc_admin_password: str = ""
    nc_db_password: str = ""
    # vpn — WireGuard (from your provider's wg config)
    wg_private_key: str = ""
    wg_address: str = "10.2.0.2/32"
    wg_dns: str = "10.2.0.1"
    wg_peer_public_key: str = ""
    wg_endpoint: str = ""              # host:port
    vpn_rotate_hourly: bool = True
    # components
    use_media_stack: bool = True
    use_vpn_stack: bool = True
    use_dashboard: bool = True
    use_daily_routine: bool = True
    use_alerts: bool = False
    # Local AI: Z.ai's official open-weights GLM via FreeBSD's ollama package,
    # downloaded only on machines where this is enabled. "auto" picks by RAM
    # at install time (glm-4.7-flash, 19 GB, needs ~24 GB RAM; else glm4:9b).
    use_local_glm: bool = False
    glm_model: str = "auto"
    # nightly self-heal schedule -- also changeable live from the dashboard
    # (status-dashboard-server.py's /api/schedule), which rewrites root's
    # crontab directly; these fields are just what a fresh install seeds it
    # with.
    daily_routine_frequency: str = "daily"   # "daily" | "weekly" | "monthly" | "yearly"
    daily_routine_time: str = "03:00"        # HH:MM, 24h
    # secrets
    plex_claim: str = ""
    qbit_user: str = "admin"
    qbit_password: str = ""
    # one OpenRouter key powers every dashboard AI pane -- see
    # https://openrouter.ai/keys to create one (free tier available)
    openrouter_api_key: str = ""
    # public access -- reverse-proxies every app through the front `caddy`
    # jail under its own subdomain (e.g. name-jellyfin.duckdns.org), so
    # nothing has to be reached by raw jail IP:port from outside the LAN.
    # Blank = skip entirely (LAN-only, same as today); every provider below
    # is independently optional -- pick at most one.
    public_access: str = ""            # "duckdns" | "cloudflare" | "noip" | "dynu" | ""
    ddns_base: str = ""                 # duckdns/no-ip/dynu subdomain base, e.g. "coppermedia"
    ddns_token: str = ""                # duckdns/dynu token, or no-ip account password
    ddns_user: str = ""                 # no-ip/dynu account username (duckdns doesn't use one)
    cloudflare_tunnel_token: str = ""   # from the Cloudflare Zero Trust dashboard (Tunnels -> your tunnel)
    cloudflare_api_token: str = ""      # a Zone:DNS:Edit scoped token, for creating the per-app CNAMEs
    cloudflare_zone_id: str = ""        # the zone id for cloudflare_domain
    cloudflare_domain: str = ""         # a domain YOU already own and added to Cloudflare -- Cloudflare
                                        # doesn't hand out free subdomains the way DuckDNS does
    # alerts (SMTP)
    smtp_host: str = ""
    smtp_port: str = "587"
    smtp_user: str = ""
    smtp_pass: str = ""
    mail_from: str = ""
    mail_to: str = ""

    def render_vars(self) -> dict:
        user = self.user or "copper"
        pool = self.media_pool or "/mnt/storage"
        # WG_ENDPOINT carries the provider's endpoint verbatim (host:port
        # exactly as their config gives it) -- wg0.conf.tmpl is the only
        # consumer, and splitting host/port into separate variables only
        # invited drift (the default port used to silently win over the
        # port embedded in the endpoint).
        return {
            "HOME": f"/home/{user}",
            "DASH_USER": user,
            "LAN_IP": self.lan_ip or "127.0.0.1",
            "LAN_CIDR": self.lan_cidr,
            "HOST_IP": self.host_ip or self.lan_ip or "127.0.0.1",
            "SUBNET": self.jail_subnet,
            "DOMAIN": self.domain,
            "JELLYFIN_PUBLIC_URL": self.jellyfin_public_url,
            "NEXTCLOUD_DOMAIN": self.nextcloud_domain,
            "NC_ADMIN_USER": self.nc_admin_user,
            "NC_ADMIN_PASSWORD": self.nc_admin_password,
            "NC_DB_PASSWORD": self.nc_db_password,
            "NC_DB_HOST": f"{self.jail_subnet}.16",
            "NC_JAIL_IP": f"{self.jail_subnet}.17",
            "TZ": self.timezone or "UTC",
            "MEDIA_POOL": pool,
            "MEDIA_POOL_EXTRA": self.media_pool_extra,
            "NEXTCLOUD_DATA": self.nextcloud_data or f"{pool}/nextcloud-data",
            # A SIBLING of NEXTCLOUD_DATA, never a child of it. stack.yaml
            # used to mount the Postgres jail's data at "{{NEXTCLOUD_DATA}}/db"
            # -- a subdirectory of the exact host dataset that
            # install-nextcloud.sh's `chown -R www:www $NCDATA` recurses
            # into for the Nextcloud app jail. Since both are nullfs mounts
            # of the same underlying host path, that chown silently flipped
            # Postgres's data directory from postgres:postgres to www:www,
            # so `service postgresql initdb` (and everything downstream)
            # failed with "Permission denied" / "connection refused" on
            # every install. Keeping the two paths disjoint on the host is
            # the actual fix; nesting them was the bug.
            "NEXTCLOUD_DB_DATA": f"{self.nextcloud_data or f'{pool}/nextcloud-data'}-db",
            "PLEX_CLAIM": self.plex_claim,
            "QBIT_USER": self.qbit_user,
            "QBIT_PASSWORD": self.qbit_password,
            "WG_PRIVATE_KEY": self.wg_private_key,
            "WG_ADDRESS": self.wg_address,
            "WG_DNS": self.wg_dns,
            "WG_PEER_PUBLIC_KEY": self.wg_peer_public_key,
            "WG_ENDPOINT": self.wg_endpoint or "",
        }

    def to_yaml(self) -> str:
        d = {"meta": {"created": datetime.now(timezone.utc).isoformat(),
                      "installer": "freebsd-media-setup-with-dash"}}
        for f in self.__dataclass_fields__:
            d[f] = getattr(self, f)
        return yaml.safe_dump(d, sort_keys=False, default_flow_style=False)

    def save(self, path: Path) -> Path:
        path = Path(path)
        # Create 0600 from the very first byte: this file carries every
        # secret on the box, and a plain write_text() followed by chmod()
        # leaves a window where it is world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self.to_yaml())
        return path

    @classmethod
    def from_yaml(cls, path: Path) -> "Profile":
        data = yaml.safe_load(Path(path).read_text()) or {}
        p = cls()
        for f in p.__dataclass_fields__:
            if f in data and data[f] is not None:
                setattr(p, f, data[f])
        return p

    def validate(self) -> list[str]:
        errs = []
        if (self.use_media_stack or self.use_vpn_stack) and not \
                re.fullmatch(r"[0-9a-fA-F.:]+", self.lan_ip or ""):
            errs.append("lan_ip must be a valid IPv4/IPv6 address")
        # NOT a hard error: a missing WireGuard config is a completely valid
        # "configure this later" state (create_vpn_stack skips cleanly and
        # says so) -- blocking the whole install over it would contradict
        # the installer's own "leave blank, deal with it later" prompts.
        if self.use_alerts and not self.smtp_host:
            errs.append("smtp settings required when alerts are enabled")
        if self.use_nextcloud and not (self.nc_admin_password and self.nc_db_password):
            errs.append("nc_admin_password / nc_db_password required when "
                        "Nextcloud is enabled (leave blank to auto-generate via "
                        "with_generated_secrets, or set your own)")
        return errs

    def fill_generated_secrets(self) -> list[str]:
        """Generate a secret for any password field that's still blank --
        never overwrites one you (or a loaded profile) already set. Returns
        the field names it actually filled, so the caller can tell a user
        which of their credentials were auto-generated vs. their own."""
        filled = []
        if self.use_vpn_stack and not self.qbit_password:
            self.qbit_password = _gen_secret(); filled.append("qbit_password")
        if self.use_nextcloud:
            if not self.nc_admin_password:
                self.nc_admin_password = _gen_secret(); filled.append("nc_admin_password")
            if not self.nc_db_password:
                self.nc_db_password = _gen_secret(); filled.append("nc_db_password")
        return filled

    @classmethod
    def with_generated_secrets(cls) -> "Profile":
        p = cls()
        p.fill_generated_secrets()
        return p
