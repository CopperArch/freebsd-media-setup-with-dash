"""FreeBSD platform layer.

The Linux edition of this installer switched between apt/dnf/pacman/zypper.
FreeBSD has exactly one package manager (pkg) and one service manager (rc.d),
so the "family" axis collapses — but two new axes open up that Docker hid on
Linux:

  * containers  -> FreeBSD **jails**, driven by **Bastille** (POSIX sh, zero
                   deps, actively maintained; the 2026 consensus manager).
  * VPN         -> there is no gluetun. A dedicated **WireGuard jail** owns the
                   tunnel and the torrent jail routes through it, with a **pf**
                   kill-switch so a dropped tunnel leaks nothing (see vpn/).

Everything the installer installs on the HOST goes through PKGS; everything it
installs INTO A JAIL goes through JAIL_PKGS + the Bastille helpers below. No
step ever calls pkg / service / bastille / pfctl directly.
"""
from __future__ import annotations

import os
import platform as _platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .util import have, run


@dataclass
class Platform:
    distro_id: str = "freebsd"
    pretty: str = "FreeBSD"
    release: str = ""                      # e.g. 15.1-RELEASE
    arch: str = field(default_factory=lambda: _platform.machine())
    abi: str = ""                          # FreeBSD:15:amd64

    # ── host packages (logical -> pkg origin/name) ───────────────────────
    # Kept as ORIGINS where the plain name is ambiguous; pkg resolves both.
    PKGS = {
        "python3":       "python311",
        "tkinter":       "py311-tkinter",
        "git":           "git",
        "curl":          "curl",
        "jq":            "jq",
        "smartmontools": "smartmontools",
        "htop":          "htop",
        "ncdu":          "ncdu",
        "rsync":         "rsync",
        "unzip":         "unzip",
        # container + network substrate (this is what replaces Docker)
        "bastille":      "bastille",
        "wireguard":     "wireguard-tools",   # kernel if_wg + wg-quick userland
        "caddy":         "caddy",             # reverse proxy, runs on host or jail
        # desktop (KDE Plasma 6 on X11 — the stable session on FreeBSD in 2026)
        "kde":           "plasma6-plasma-desktop",
        "sddm":          "sddm",
        "xorg":          "xorg",
        "drm-kmod":      "drm-kmod",          # GPU / KMS for transcode + desktop
        # privacy-hardened default browser -- opens dashboard app links/quick
        # links (OpenRouter, VPN providers, ...) with one persistent,
        # always-logged-in profile. No Helium port exists on FreeBSD.
        "librewolf":     "librewolf",
        "ollama":        "ollama",              # local LLM runner (CPU + Vulkan), only if use_local_glm
        "cloudflared":   "cloudflared",         # Cloudflare Tunnel client, only if public_access=cloudflare
    }

    # ── packages installed INSIDE service jails (logical -> pkg) ─────────
    # These are the FreeBSD-native equivalents of the linuxserver.io images.
    JAIL_PKGS = {
        "plex":       "plexmediaserver-plexpass",   # or plexmediaserver
        "jellyfin":   "jellyfin",
        "sonarr":     "sonarr",
        "radarr":     "radarr",
        "prowlarr":   "prowlarr",
        "bazarr":     "bazarr",
        "qbittorrent":"qbittorrent-nox",
        "jellyseerr": "jellyseerr",                 # falls back to node build if absent
        "caddy":      "caddy",
        "wireguard":  "wireguard-tools",
    }

    # ── rc.d knob + service name per app ─────────────────────────────────
    # rc knob names occasionally differ from the app name (Plex is the classic
    # offender). Centralised here so a wrong guess is a one-line fix, and the
    # installer prints `pkg info -D <pkg>` (the pkg-message) so you can confirm.
    RC = {
        "plex":        ("plexmediaserver_plexpass", "plexmediaserver_plexpass"),
        "jellyfin":    ("jellyfin", "jellyfin"),
        "sonarr":      ("sonarr", "sonarr"),
        "radarr":      ("radarr", "radarr"),
        "prowlarr":    ("prowlarr", "prowlarr"),
        "bazarr":      ("bazarr", "bazarr"),
        "qbittorrent": ("qbittorrent", "qbittorrent"),
        "jellyseerr":  ("jellyseerr", "jellyseerr"),
        "caddy":       ("caddy", "caddy"),
    }

    def pkg_name(self, logical: str) -> str | None:
        return self.PKGS.get(logical)

    def jail_pkg(self, logical: str) -> str | None:
        return self.JAIL_PKGS.get(logical)

    # ── detection ────────────────────────────────────────────────────────
    @staticmethod
    def detect() -> "Platform":
        sysname = _platform.system()
        if sysname != "FreeBSD":
            # Let the caller decide; the installer refuses on non-FreeBSD but we
            # still return a usable object for --dry-run on a dev laptop.
            return Platform(distro_id=sysname.lower(),
                            pretty=f"{sysname} (not FreeBSD — dry-run only)")
        rel = run(["freebsd-version", "-u"], quiet=True)
        abi = run(["pkg", "config", "ABI"], quiet=True)
        return Platform(
            release=(rel.stdout.strip() if rel.returncode == 0 else _platform.release()),
            abi=(abi.stdout.strip() if abi.returncode == 0 else ""),
            pretty=f"FreeBSD {_platform.release()}",
        )

    def is_freebsd(self) -> bool:
        return self.distro_id == "freebsd"

    # ── host package installation ────────────────────────────────────────
    def install(self, sudo, logicals: list[str]) -> tuple[bool, str]:
        names = [self.pkg_name(l) for l in logicals]
        missing = [l for l, n in zip(logicals, names) if n is None]
        if missing:
            return False, "no pkg mapping for: " + ", ".join(missing)
        names = [n for n in names if n]
        # ASSUME_ALWAYS_YES keeps pkg non-interactive; bootstraps pkg if needed.
        p = sudo.run(["pkg", "install", "-y", *names], timeout=3600,
                     env=dict(os.environ, ASSUME_ALWAYS_YES="YES"))
        return p.returncode == 0, f"rc={p.returncode}"

    # ── rc.d service control (host) ──────────────────────────────────────
    def service(self, sudo, knob: str, action: str) -> bool:
        """action: enable | disable | start | stop | restart | status."""
        if action in ("enable", "disable"):
            val = "YES" if action == "enable" else "NO"
            return sudo.run(["sysrc", f"{knob}_enable={val}"],
                            timeout=60).returncode == 0
        # map to `service <name> <action>`; knob doubles as service name here
        return sudo.run(["service", knob, action], timeout=180).returncode == 0

    def sysrc(self, sudo, *assignments: str) -> bool:
        return sudo.run(["sysrc", *assignments], timeout=60).returncode == 0

    # ── Bastille jail helpers (the Docker-compose replacement) ───────────
    def bastille_bootstrapped(self) -> bool:
        p = run(["bastille", "list", "release"], quiet=True)
        return p.returncode == 0 and bool(p.stdout.strip())

    def bastille_bootstrap(self, sudo, release: str) -> bool:
        ok = self.sysrc(sudo, "bastille_enable=YES")
        p = sudo.run(["bastille", "bootstrap", release, "update"], timeout=3600)
        return ok and p.returncode == 0

    def jail_exists(self, name: str) -> bool:
        # "-a" is fully deprecated on newer bastille -- it now exits 1
        # instead of just warning, which silently made this always return
        # False (every idempotency check in the installer relies on this,
        # so a re-run would try to recreate every jail from scratch).
        p = run(["bastille", "list", "all"], quiet=True)
        return p.returncode == 0 and any(
            line.split() and line.split()[-1] == name or f" {name} " in f" {line} "
            for line in p.stdout.splitlines())

    def jail_create(self, sudo, name: str, release: str, ip: str,
                    interface: str = "", vnet: bool = False,
                    gateway: str = "") -> bool:
        """Classic (shared-stack) jail by default -- same as every media jail
        today. `vnet=True` gives the jail its OWN network stack via `-B`
        (bridge mode -- `interface` must already be a bridge, which bastille0
        always is here; the plain `-V`/--vnet flag instead wants a physical
        NIC, wrong for our shared-bridge topology). A VNET jail needs a real
        `gateway` IP too: bastille's own auto-detected default (the HOST's
        WAN gateway) is meaningless for a jail whose only interface is the
        private jail bridge -- confirmed live, that guess leaves the jail
        with zero connectivity ("Network is unreachable") until `-g` points
        it at the bridge's own gateway address instead. Classic jails never
        needed this because they share the host's single routing table.
        This is what actually lets a VNET jail create its own pseudo-
        interfaces (e.g. `ifconfig wg create`) -- a classic jail fails that
        with "Operation not permitted" no matter what jail.conf allow.* flags
        you throw at it, confirmed live."""
        cmd = ["bastille", "create"]
        if vnet:
            cmd += ["-B"]
            if gateway:
                cmd += ["-g", gateway]
        cmd += [name, release, ip]
        if interface:
            cmd += [interface]
        return sudo.run(cmd, timeout=1200).returncode == 0

    def ensure_bridge_gateway(self, sudo, bridge: str, gateway_cidr: str) -> bool:
        """VNET jails need a real gateway address to route through -- classic
        jails share the host's own stack so nothing on `bridge` has ever
        needed its own address before now. Idempotent: a persistent alias
        (survives reboot, since bastille itself only manages the bridge0->
        bastille0 rename in rc.conf, never an address on it) plus an
        immediate `ifconfig alias` so it's live for THIS install run without
        having to restart the bridge (which would be disruptive to every
        already-running classic jail's alias on it)."""
        gw_ip = gateway_cidr.split("/")[0]
        current = run(["ifconfig", bridge], quiet=True).stdout
        if gw_ip in current:
            return True
        ok1 = self.sysrc(sudo, f"ifconfig_{bridge}_alias0=inet {gateway_cidr}")
        ok2 = sudo.run(["ifconfig", bridge, "inet", gateway_cidr, "alias"],
                       timeout=30).returncode == 0
        return ok1 and ok2

    def jail_enable_forwarding(self, sudo, name: str) -> bool:
        """Same idea as the host's own enable_forwarding(): a VNET jail runs
        its own copy of /etc/rc at boot, so `gateway_enable=YES` in ITS
        rc.conf is what makes it forward at all -- but that only takes
        effect on next jail restart, and we need this immediately (the vpn
        jail has to start forwarding for the confined jails the moment it's
        up, not after some later reboot)."""
        ok = self.jail_sysrc(sudo, name, "gateway_enable=YES")
        sudo.run(["bastille", "cmd", name, "sysctl",
                 "net.inet.ip.forwarding=1"], timeout=30)
        return ok

    def jail_defaultrouter(self, sudo, name: str) -> str:
        p = run(["bastille", "cmd", name, "sysrc", "-n", "defaultrouter"],
               quiet=True)
        return p.stdout.strip().splitlines()[-1] if p.returncode == 0 and p.stdout.strip() else ""

    def jail_pkg_install(self, sudo, name: str, pkgs: list[str]) -> bool:
        p = sudo.run(["bastille", "pkg", name, "install", "-y", *pkgs],
                     timeout=3600, env=dict(os.environ, ASSUME_ALWAYS_YES="YES"))
        return p.returncode == 0

    def jail_sysrc(self, sudo, name: str, *assignments: str) -> bool:
        return sudo.run(["bastille", "sysrc", name, *assignments],
                        timeout=60).returncode == 0

    def jail_service(self, sudo, name: str, svc: str, action: str) -> bool:
        return sudo.run(["bastille", "cmd", name, "service", svc, action],
                        timeout=180).returncode == 0

    def jail_add_param(self, sudo, name: str, key: str, value: str) -> bool:
        """`bastille config <jail> set <prop> <val>` only accepts a small
        whitelist of properties; anything else (e.g. allow.mlock, needed by
        every .NET-based *arr app -- see stack.yaml's `allow_mlock` flag) has
        to go through `add` instead, which appends the raw jail.conf line
        verbatim. Takes effect only after the jail is restarted."""
        return sudo.run(["bastille", "config", name, "add", key, value],
                        timeout=30).returncode == 0

    def jail_restart(self, sudo, name: str) -> bool:
        return sudo.run(["bastille", "restart", name], timeout=120).returncode == 0

    def jail_mount(self, sudo, name: str, host_path: str, jail_path: str,
                   ro: bool = False) -> bool:
        """nullfs-mount a host dataset into a jail (the compose `volumes:` map).
        Bastille records this in the jail's fstab so it survives restarts."""
        opts = "ro" if ro else "rw"
        return sudo.run(["bastille", "mount", name, host_path, jail_path,
                         "nullfs", opts, "0", "0"], timeout=60).returncode == 0

    # ── pf firewall (replaces ufw/firewalld AND gluetun's kill-switch) ───
    def pf_reload(self, sudo, conf: str = "/etc/pf.conf") -> bool:
        self.sysrc(sudo, "pf_enable=YES")
        # service pf start must come first: it's what loads pf.ko, and
        # pfctl needs /dev/pf to exist before it can do anything. On a box
        # where pf has never run, `pfctl -f` before this just fails outright.
        sudo.run(["service", "pf", "start"], timeout=60)
        # -f is idempotent, so this is also the right call if pf was
        # already running and we're just pushing an updated ruleset.
        return sudo.run(["pfctl", "-f", conf], timeout=60).returncode == 0

    def detect_wan_interface(self) -> str:
        """The interface the default route goes out -- what pf NATs jail
        traffic through. Empty string if there's no default route yet."""
        p = run(["route", "-n", "get", "default"], quiet=True)
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                return line.split(":", 1)[1].strip()
        return ""

    def detect_lan_ip(self) -> str:
        """First IPv4 address on the default-route interface -- what Caddy
        and the WebUIs should bind on a fresh box nobody asked the user
        about (the installer's profile has no lan_ip prompt; without this
        a fresh interactive install could never pass validation). Empty
        when there's no default route/address yet."""
        wan_if = self.detect_wan_interface()
        if not wan_if:
            return ""
        addrs = run(["ifconfig", wan_if], quiet=True).stdout
        for line in addrs.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "127.0.0.1" not in line:
                return line.split()[1]
        return ""

    def enable_forwarding(self, sudo) -> bool:
        ok = self.sysrc(sudo, "gateway_enable=YES")
        sudo.run(["sysctl", "net.inet.ip.forwarding=1"], timeout=30)
        return ok

    def kldload(self, sudo, *modules: str) -> bool:
        ok = True
        for m in modules:
            # persist across reboot + load now
            self.sysrc(sudo, f'kld_list+={m}')
            if run(["kldstat", "-q", "-n", m], quiet=True).returncode != 0:
                ok = sudo.run(["kldload", m], timeout=60).returncode == 0 and ok
        return ok

    # ── cron (root or user) — identical semantics to the Linux edition ───
    def crontab_user(self, sudo, entries: list[str], user: str,
                     tag: str = "default") -> bool:
        """Install into `user`'s crontab specifically.

        The installer itself always runs as root (jail/pkg work requires
        it), so a bare `crontab -l` / `crontab -` here edits ROOT's
        crontab, not the target user's -- the routine gets deployed to
        ~/.hermes for the profile's user but the cron entry that's meant
        to run it silently lands somewhere else entirely. -u makes the
        target explicit; the new table goes through a temp file rather
        than stdin since Sudo.run() has no stdin-piping path.

        `tag` scopes a BEGIN/END block so unrelated freebsd-media-setup
        entries (the daily routine, the DuckDNS updater, the dashboard
        watchdog, ...) never disturb each other, and so calling this AGAIN
        with a CHANGED entry (e.g. the daily routine's schedule edited from
        the dashboard) replaces the old line instead of leaving both the old
        and new cron jobs installed side by side -- confirmed this was a
        real latent bug in the old marker-line-only version, which only ever
        stripped an exact line match, not "whatever this tag last wrote".
        Pass `entries=[]` to remove a tag's block entirely."""
        p = sudo.run(["crontab", "-u", user, "-l"], quiet=True)
        existing = p.stdout.splitlines() if p.returncode == 0 else []
        begin, end = f"# freebsd-media-setup:{tag} BEGIN", f"# freebsd-media-setup:{tag} END"
        out, skipping = [], False
        for l in existing:
            if l.strip() == begin:
                skipping = True; continue
            if l.strip() == end:
                skipping = False; continue
            if not skipping:
                out.append(l)
        if entries:
            out += [begin, *entries, end]
        new = "\n".join(out).strip("\n") + "\n"
        tmp = Path(f"/tmp/.crontab-{user}-{os.getpid()}")
        tmp.write_text(new)
        try:
            return sudo.run(["crontab", "-u", user, str(tmp)],
                            quiet=True).returncode == 0
        finally:
            tmp.unlink(missing_ok=True)

    # ── storage auto-pooling (media_pool auto-detect) ─────────────────────
    def list_physical_disks(self) -> list[str]:
        """Real disks (ada*/nvd*/da*/nda*), optical drives excluded."""
        p = run(["sysctl", "-n", "kern.disks"], quiet=True)
        if p.returncode != 0:
            return []
        return [d for d in p.stdout.split() if not d.startswith("cd")]

    def root_pool_disks(self) -> set[str]:
        """Base disk names (e.g. `ada0`, partition suffix stripped) backing
        whatever pool is mounted at `/` -- these must never be swallowed into
        the auto-detected media pool."""
        p = run(["mount"], quiet=True)
        root_dev = ""
        for line in p.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "/":
                root_dev = parts[0]
                break
        if "/" not in root_dev:
            return set()
        pool = root_dev.split("/", 1)[0]
        zp = run(["zpool", "status", pool], quiet=True)
        if zp.returncode != 0:
            return set()
        disks = set()
        for line in zp.stdout.splitlines():
            dev = line.strip().split()[0] if line.strip() else ""
            m = re.match(r"^([a-zA-Z]+\d+)", dev)
            if m:
                disks.add(m.group(1))
        return disks

    def unused_disks(self) -> list[str]:
        """Physical disks not backing the root pool -- candidates for the
        auto-created media pool. ONLY meaningful when the root filesystem
        is ZFS: on a UFS root there is no root pool, so this would
        silently include the boot disk."""
        root = self.root_pool_disks()
        return [d for d in self.list_physical_disks() if d not in root]

    def root_is_zfs(self) -> bool:
        """True when / itself lives on ZFS -- the precondition for trusting
        root_pool_disks()'s boot-disk detection (and for bastille's
        ZFS-backed jails). On a UFS root there is no root pool, so
        unused_disks() cannot tell the boot disk from a spare and disk
        auto-pooling must not offer to swallow them."""
        return bool(self.root_pool_disks())

    def zpool_exists(self, name: str) -> bool:
        return run(["zpool", "list", name], quiet=True).returncode == 0

    def zpool_create(self, sudo, name: str, disks: list[str],
                     mountpoint: str) -> bool:
        """Striped pool -- matches this project's existing convention
        (installerconfig uses ZFSBOOT_VDEV_TYPE=stripe for the boot pool
        too), so there's no surprise RAID behavior for a user who didn't
        ask for one."""
        if self.zpool_exists(name):
            return True
        cmd = ["zpool", "create", "-m", mountpoint, name, *disks]
        return sudo.run(cmd, timeout=120).returncode == 0

    def zfs_dataset_exists(self, dataset: str) -> bool:
        return run(["zfs", "list", dataset], quiet=True).returncode == 0

    def zfs_create_dataset(self, sudo, dataset: str, mountpoint: str) -> bool:
        if self.zfs_dataset_exists(dataset):
            return True
        cmd = ["zfs", "create", "-o", f"mountpoint={mountpoint}", dataset]
        return sudo.run(cmd, timeout=60).returncode == 0

    # ── public access: dynamic DNS + Cloudflare Tunnel ───────────────────
    def ddns_update_duckdns(self, subdomains: list[str], token: str) -> tuple[bool, str]:
        """DuckDNS's update API auto-CREATES a subdomain the first time it's
        called for a name that doesn't exist yet under this token -- no
        separate "create the hostname" step needed, unlike No-IP/Dynu. One
        call updates every app's subdomain at once (comma-separated)."""
        url = (f"https://www.duckdns.org/update?domains={','.join(subdomains)}"
              f"&token={token}&ip=")
        p = run(["curl", "-fsS", "-m", "15", url], timeout=20, quiet=True)
        ok = p.returncode == 0 and p.stdout.strip().upper().startswith("OK")
        return ok, p.stdout.strip() or f"rc={p.returncode}"

    def ddns_update_simple(self, hostname: str, user: str, password: str,
                           update_url: str) -> tuple[bool, str]:
        """No-IP and Dynu both expose the same long-established "dynamic
        update" API shape (basic-auth GET, hostname query param) -- unlike
        DuckDNS this does NOT create the hostname; it has to already exist
        on that provider's own dashboard. Good/nochg are both success (Dynu
        and No-IP each use a slightly different token for "already up to
        date", both mean nothing needs fixing)."""
        p = run(["curl", "-fsS", "-m", "15", "-u", f"{user}:{password}",
                f"{update_url}?hostname={hostname}"], timeout=20, quiet=True)
        out = p.stdout.strip()
        ok = p.returncode == 0 and any(s in out.lower() for s in ("good", "nochg"))
        return ok, out or f"rc={p.returncode}"

    @staticmethod
    def tunnel_id_from_token(token: str) -> str:
        """Decode the tunnel UUID out of a Cloudflare Tunnel token. The
        token is base64-encoded JSON -- {"a": account-tag, "t": tunnel-id,
        "s": secret} -- exactly what `cloudflared tunnel run --token`
        decodes. Per-hostname DNS records must CNAME to
        <tunnel-id>.cfargotunnel.com, so this is the value the DNS upsert
        needs; "" if the token doesn't decode (bad paste, or a revocation
        changed its shape)."""
        import base64
        import json as _json
        try:
            data = _json.loads(base64.b64decode(token).decode())
            tid = data.get("t", "")
            return tid if isinstance(tid, str) else ""
        except Exception:  # noqa: BLE001
            return ""

    def cloudflare_dns_upsert(self, api_token: str, zone_id: str, name: str,
                              content: str, record_type: str = "CNAME") -> tuple[bool, str]:
        """Create (or leave alone if already correct) one DNS record via
        Cloudflare's REST API -- used to point each app's hostname at the
        tunnel (a CNAME to <tunnel-id>.cfargotunnel.com). Idempotent: checks
        for an existing record with this exact name first rather than
        blindly POSTing a duplicate every re-run."""
        import json as _json
        base = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
        auth = f"Authorization: Bearer {api_token}"
        p = run(["curl", "-fsS", "-m", "15", "-H", auth,
                f"{base}?type={record_type}&name={name}"], timeout=20, quiet=True)
        try:
            existing = _json.loads(p.stdout).get("result", [])
        except Exception:  # noqa: BLE001
            existing = []
        if existing:
            return True, f"{name} already exists"
        body = _json.dumps({"type": record_type, "name": name,
                            "content": content, "proxied": True})
        p = run(["curl", "-fsS", "-m", "15", "-X", "POST", "-H", auth,
                "-H", "Content-Type: application/json", "-d", body, base],
               timeout=20, quiet=True)
        try:
            ok = _json.loads(p.stdout).get("success", False)
        except Exception:  # noqa: BLE001
            ok = False
        return ok, p.stdout.strip()[:200]

    def cloudflared_install_tunnel(self, sudo, token: str) -> bool:
        """Runs cloudflared as a host-level rc.d daemon carrying the
        Cloudflare Zero Trust tunnel token (created on their dashboard,
        Zero Trust -> Networks -> Tunnels -- a normal, free, standard
        Cloudflare Tunnel setup, no different in spirit from pasting a
        DuckDNS token). Public hostname -> local address mapping for a
        token-run tunnel lives in Cloudflare's own dashboard for that
        tunnel, not a local config file -- every hostname should be pointed
        at this host's front Caddy jail so Caddy's own Host-header routing
        still does the per-app split, same as every other provider here."""
        if not have("cloudflared"):
            ok, _ = self.install(sudo, ["cloudflared"])
            if not ok:
                return False
        cfg_dir = "/usr/local/etc/cloudflared"
        sudo.run(["mkdir", "-p", cfg_dir], timeout=10)
        token_file = f"{cfg_dir}/tunnel-token"
        sudo.run(["sh", "-c", f"echo {token!r} > {token_file}"], timeout=10)
        sudo.run(["chmod", "600", token_file], timeout=10)
        rc_script = "/usr/local/etc/rc.d/cloudflared_tunnel"
        script = (f"#!/bin/sh\n# PROVIDE: cloudflared_tunnel\n# REQUIRE: NETWORKING\n"
                 f"# KEYWORD: shutdown\n. /etc/rc.subr\nname=cloudflared_tunnel\n"
                 f"rcvar=cloudflared_tunnel_enable\npidfile=/var/run/cloudflared_tunnel.pid\n"
                 f"start_cmd=cloudflared_tunnel_start\nstop_cmd=cloudflared_tunnel_stop\n"
                 f"cloudflared_tunnel_start() {{\n"
                 f"  /usr/local/bin/cloudflared tunnel run --token \"$(cat {token_file})\" "
                 f"> /var/log/cloudflared_tunnel.log 2>&1 & echo $! > $pidfile\n}}\n"
                 f"cloudflared_tunnel_stop() {{\n"
                 f"  [ -f $pidfile ] && kill \"$(cat $pidfile)\" 2>/dev/null; rm -f $pidfile\n}}\n"
                 f"load_rc_config $name\nrun_rc_command \"$1\"\n")
        sudo.run(["sh", "-c", f"cat > {rc_script} << 'EOF'\n{script}EOF"], timeout=10)
        sudo.run(["chmod", "755", rc_script], timeout=10)
        self.sysrc(sudo, "cloudflared_tunnel_enable=YES")
        sudo.run(["service", "cloudflared_tunnel", "start"], timeout=30)
        # The rc.d start_cmd backgrounds cloudflared itself, so a 0 exit here
        # only means "the launch was kicked off", not "cloudflared is
        # actually running" (a bad/expired token fails a few seconds later,
        # after this call has already returned) -- check for real.
        import time; time.sleep(3)
        return run(["pgrep", "-f", "cloudflared tunnel run"], quiet=True).returncode == 0

    # ── Plex ───────────────────────────────────────────────────────────────
    def plex_claim(self, ip: str, token: str, attempts: int = 12,
                   delay: int = 5) -> bool:
        """Tie a freshly-started, still-unclaimed Plex Media Server to your
        plex.tv account without ever opening its web UI -- this is the same
        local HTTP endpoint Plex's own docs describe for claiming headless
        servers, so it works identically to the Docker-image PLEX_CLAIM env
        var despite FreeBSD's pkg having no such wrapper itself. Retries
        because a jail that just got `pkg install`ed can take the better
        part of a minute to finish Plex's own first-run init before this
        endpoint responds at all."""
        import time
        for _ in range(attempts):
            p = run(["curl", "-fsS", "-m", "10",
                    f"http://{ip}:32400/myplex/claim?token={token}"],
                   timeout=15, quiet=True)
            if p.returncode == 0:
                return True
            time.sleep(delay)
        return False
