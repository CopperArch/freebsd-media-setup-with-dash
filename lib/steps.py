"""Install steps, in order — FreeBSD edition.

Same contract as the Linux edition: each step takes (ctx, accept) and returns
(ok, summary); nothing that changes the system runs without an explicit accept.
The difference is entirely in the substrate — pkg/bastille/rc.d/pf instead of
apt-dnf-pacman/docker/systemd/ufw.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from .platform import Platform
from .profile import Profile
from .render import render_text, render_file, MissingVar
from .util import have, log, run


class Ctx:
    def __init__(self, profile: Profile, plat: Platform, sudo, dry_run=False,
                 repo_root: Path | None = None):
        self.profile = profile
        self.plat = plat
        self.sudo = sudo
        self.dry_run = dry_run
        self.repo = repo_root or Path(__file__).resolve().parent.parent
        self.tpl = self.repo / "templates"

    @property
    def v(self) -> dict:
        return self.profile.render_vars()

    def stack(self) -> dict:
        raw = (self.repo / "stacks" / "stack.yaml").read_text()
        # render {{SUBNET}} etc. before parsing so the manifest is concrete
        rendered = render_text(raw, self._stack_vars(), "stack.yaml")
        return yaml.safe_load(rendered)

    def _stack_vars(self) -> dict:
        d = dict(self.v)
        d.setdefault("SUBNET", "10.17.0")
        return d


def _exec(ctx: Ctx, desc, fn):
    if ctx.dry_run:
        log(f"  [DRY] {desc}")
        return True, "dry-run"
    try:
        return fn(ctx)
    except Exception as e:  # noqa: BLE001
        return False, f"{desc} failed: {e}"


# ── 0. guardrail ────────────────────────────────────────────────────────────
def check_freebsd(ctx: Ctx, accept) -> tuple[bool, str]:
    if not ctx.plat.is_freebsd() and not ctx.dry_run:
        return False, ("this installer targets FreeBSD; run --dry-run to preview "
                       "on another OS")
    return True, f"{ctx.plat.pretty} (release {ctx.plat.release or '?'})"


# ── 1. host packages ─────────────────────────────────────────────────────────
def install_host_packages(ctx: Ctx, accept) -> tuple[bool, str]:
    need = ["python3", "git", "curl", "jq", "bastille"]
    if ctx.profile.use_daily_routine:
        need += ["smartmontools", "rsync"]
    if ctx.profile.use_dashboard:
        need += ["tkinter", "librewolf"]
    if ctx.profile.use_vpn_stack:
        need += ["wireguard"]
    names = [ctx.plat.pkg_name(n) for n in need]
    if not accept("Install host packages?\n  " + ", ".join(names)):
        return False, "declined"
    return _exec(ctx, "host pkg install", lambda c: c.plat.install(c.sudo, need))


# ── 2. Bastille substrate ────────────────────────────────────────────────────
def bootstrap_bastille(ctx: Ctx, accept) -> tuple[bool, str]:
    rel = ctx.stack().get("release", "15.1-RELEASE")
    if not accept(f"Bootstrap Bastille release {rel} (downloads a base)?"):
        return False, "declined"
    def do(c):
        if c.plat.bastille_bootstrapped():
            bootstrapped, msg = True, "already bootstrapped"
        else:
            ok = c.plat.bastille_bootstrap(c.sudo, rel)
            bootstrapped, msg = ok, f"bootstrap {rel} rc={'0' if ok else 'nonzero'}"
        if not bootstrapped:
            return False, msg
        # Without this, jails have no path to the internet at all -- every
        # jail's pkg install fails, not just the VPN-confined ones. See
        # templates/pf-nat.conf.tmpl for why.
        wan_if = c.plat.detect_wan_interface()
        if not wan_if:
            return False, f"{msg}; no default route found, can't set up jail NAT"
        c.plat.enable_forwarding(c.sudo)
        nat_vars = dict(c.v, WAN_IF=wan_if)
        render_file(c.tpl / "pf-nat.conf.tmpl", Path("/etc/pf.conf"), nat_vars,
                    mode=0o644)
        nat_ok = c.plat.pf_reload(c.sudo)
        return nat_ok, f"{msg}; jail NAT via {wan_if} {'ok' if nat_ok else 'FAILED'}"
    return _exec(ctx, "bastille bootstrap", do)


# ── 2b. storage auto-pooling ─────────────────────────────────────────────────
def provision_storage(ctx: Ctx, accept) -> tuple[bool, str]:
    """If the profile didn't pin media_pool, auto-detect non-OS disks and
    build one striped pool for the media jails. Runs before create_media_jails
    since jails mount MEDIA_POOL on creation -- it has to exist already.

    Also offers an independent (not either-or) `zroot/media` dataset on the
    OS disk itself as *extra* capacity: the OS disk's own space is already
    spent on zroot, so this is a dataset within it, not a second pool.
    """
    notes = []
    if ctx.profile.media_pool:
        notes.append(f"media_pool pinned to {ctx.profile.media_pool}, skipping auto-detect")
    elif not ctx.plat.root_is_zfs():
        # root_pool_disks() identifies the boot disk(s) via the root ZFS
        # pool; with a UFS root it returns nothing, so "unused" would
        # silently include the boot disk and `zpool create` would destroy
        # it. Auto-detect is only safe on a ZFS root.
        notes.append("root filesystem is not ZFS -- skipping disk auto-detect "
                     "(boot disk can't be told apart from spares); set media_pool "
                     "explicitly")
    else:
        candidates = ctx.plat.unused_disks()
        if not candidates:
            notes.append("no spare disks found for auto-pooling")
        else:
            listing = ", ".join(candidates)
            if accept(f"Create a striped ZFS pool 'storage' at /mnt/storage "
                      f"from unused disk(s): {listing}?"):
                def do_pool(c):
                    ok = c.plat.zpool_create(c.sudo, "storage", candidates,
                                             "/mnt/storage")
                    if ok:
                        c.profile.media_pool = "/mnt/storage"
                    return ok, f"pool 'storage' from {listing}"
                ok, msg = _exec(ctx, "zpool create", do_pool)
                if not ok:
                    return False, msg
                notes.append(msg)
            else:
                notes.append("auto-pooling declined")

    if ctx.plat.root_is_zfs():
        if accept("Also use a zroot/media dataset on the OS disk as extra "
                  "media storage (alongside any pool above)?"):
            def do_extra(c):
                ok = c.plat.zfs_create_dataset(c.sudo, "zroot/media",
                                               "/mnt/os-media")
                if ok:
                    c.profile.media_pool_extra = "/mnt/os-media"
                return ok, "zroot/media dataset at /mnt/os-media"
            ok, msg = _exec(ctx, "zfs create", do_extra)
            if not ok:
                return False, msg
            notes.append(msg)
    else:
        notes.append("zroot/media extra dataset skipped (no ZFS root pool)")

    if not ctx.profile.media_pool:
        ctx.profile.media_pool = "/mnt/storage"
        notes.append("no pool created; defaulting media_pool to /mnt/storage")
        # A defaulted path that doesn't exist leaves every jail's nullfs
        # mount pointing at nothing. On a ZFS root, offer the real fix: a
        # dataset for the media jails to live in.
        if ctx.plat.root_is_zfs():
            if accept("Create a zroot/media dataset at /mnt/storage for the "
                      "media jails?"):
                def do_default_ds(c):
                    ok = c.plat.zfs_create_dataset(c.sudo, "zroot/media",
                                                   "/mnt/storage")
                    return ok, "zroot/media dataset mounted at /mnt/storage"
                ok, msg = _exec(ctx, "zfs create", do_default_ds)
                if not ok:
                    return False, msg
                notes.append(msg)
    return True, "; ".join(notes) if notes else "nothing to do"


# ── 2c. hotplug drive watch (devd -> dashboard attention panel) ─────────────
def install_hotplug_watch(ctx: Ctx, accept) -> tuple[bool, str]:
    """A drive attached AFTER install (e.g. to grow the media pool later)
    doesn't go through provision_storage -- this is what notices it instead.
    devd(8) flags new disks; the existing dashboard fix-button pattern (see
    status-dashboard-server.py's FIXES dict) does the actual pool-add, so no
    new UI is needed, just a new fix id."""
    if not accept("Watch for newly attached drives and surface them in the "
                  "dashboard's attention panel?"):
        return False, "declined"
    def do(c):
        script = Path("/usr/local/freebsd-media-setup/scripts/drive-hotplug.sh")
        render_file(c.tpl / "scripts" / "drive-hotplug.sh", script, c.v,
                   mode=0o755)
        render_file(c.tpl / "devd-drive-hotplug.conf.tmpl",
                   Path("/usr/local/etc/devd/drive-hotplug.conf"), c.v,
                   mode=0o644)
        ok = c.sudo.run(["service", "devd", "restart"], timeout=30).returncode == 0
        return ok, f"devd rule + {script} installed; devd restart {'ok' if ok else 'FAILED'}"
    return _exec(ctx, "install hotplug watch", do)


# ── 3. media jails (no VPN) ──────────────────────────────────────────────────
def create_media_jails(ctx: Ctx, accept) -> tuple[bool, str]:
    if not ctx.profile.use_media_stack:
        return True, "media stack disabled in profile"
    st = ctx.stack()
    rel = st["release"]
    jails = st["jails"]
    if not ctx.profile.use_nextcloud:
        jails = [j for j in jails if j["name"] not in ("nextcloud", "nextcloud-db")]
    listing = ", ".join(j["name"] for j in jails)
    if not accept(f"Create media jails: {listing}?"):
        return False, "declined"
    def do(c):
        made = []
        mount_failures = []
        claim_note = ""
        for j in jails:
            fresh = not c.plat.jail_exists(j["name"])
            if fresh:
                c.plat.jail_create(c.sudo, j["name"], rel, j["ip"],
                                   f"{st['bridge']}")
                if j.get("allow_mlock"):
                    c.plat.jail_add_param(c.sudo, j["name"], "allow.mlock", "1")
                    c.plat.jail_restart(c.sudo, j["name"])
                if j.get("sysvipc"):
                    for p in ("sysvmsg", "sysvsem", "sysvshm"):
                        c.plat.jail_add_param(c.sudo, j["name"], p, "new")
                    c.plat.jail_restart(c.sudo, j["name"])
            # Everything below is idempotent (pkg install is a no-op if
            # present, sysrc just re-asserts the knob, bastille mount is
            # safe to re-issue) and MUST still run even when the jail
            # already existed -- e.g. a Ctrl-C or a failed earlier pass can
            # leave a jail created but its mounts/services never applied.
            # Skipping all of this on "(exists)" used to strand jails
            # (Nextcloud's data mount in particular) permanently unmounted:
            # occ maintenance:install then fails with "Cannot create or
            # write into the data directory" forever, since the directory
            # it sees is just the jail's own unmounted, root-owned /data.
            c.plat.jail_pkg_install(c.sudo, j["name"], j["pkg"])
            for k, val in (j.get("rc") or {}).items():
                c.plat.jail_sysrc(c.sudo, j["name"], f"{k}={val}")
            for m in (j.get("mounts") or []):
                if not c.plat.jail_mount(c.sudo, j["name"], m["host"], m["jail"],
                                         ro=m.get("ro", False)):
                    mount_failures.append(f"{j['name']}:{m['jail']}")
                if (c.profile.media_pool_extra
                        and m["jail"] == "/data/media"):
                    if not c.plat.jail_mount(c.sudo, j["name"],
                                             c.profile.media_pool_extra,
                                             "/data/media-local",
                                             ro=m.get("ro", False)):
                        mount_failures.append(f"{j['name']}:/data/media-local")
            for svc in (j.get("services") or []):
                c.plat.jail_service(c.sudo, j["name"], svc, "start")
            made.append(j["name"] if fresh else j["name"] + "(exists)")
            # Only on a FRESH plex jail this run -- an already-running,
            # possibly-already-claimed server shouldn't get re-poked on
            # every re-run of the installer, and a claim token is single-use
            # anyway (~4 minute window from https://plex.tv/claim).
            if fresh and j["name"] == "plex" and c.profile.plex_claim:
                claimed = c.plat.plex_claim(j["ip"], c.profile.plex_claim)
                claim_note = ("; plex claimed" if claimed
                             else "; plex claim FAILED (token expired? claim manually)")
        ok = not mount_failures
        msg = "jails: " + ", ".join(made) + claim_note
        if mount_failures:
            msg += "; MOUNT FAILED (bastille mount returned nonzero): " + ", ".join(mount_failures)
        return ok, msg
    return _exec(ctx, "create media jails", do)


# ── 3a. Seerr (Jellyseerr's successor) — no FreeBSD package, build from source ──
def build_seerr_from_source(ctx: Ctx, accept) -> tuple[bool, str]:
    """No FreeBSD package exists for Seerr/Jellyseerr at all -- pkg search
    comes up empty under every name this project or upstream has ever used.
    This is the actual implementation of the "falls back to node build if
    absent" comment that's sat unused in lib/platform.py's JAIL_PKGS for a
    long time (nothing ever read that dict; stack.yaml's pkg list is what
    create_media_jails actually installs, and it can't list a package that
    doesn't exist). Clones a pinned release tag, patches three verified
    upstream migration bugs (see templates/seerr-build/build-seerr.sh's own
    comments for why), builds with pnpm, and installs a hand-written rc.d
    service since none ships from a package."""
    if not ctx.profile.use_media_stack:
        return True, "media stack disabled in profile"
    if not ctx.plat.jail_exists("seerr"):
        return False, "seerr jail not created yet -- run the Media jails step first"
    if not accept("Build Seerr from source (no FreeBSD package exists for it) "
                  "-- this downloads and compiles a Node.js app, several minutes?"):
        return False, "declined"
    def do(c):
        jail_root = "/usr/local/bastille/jails/seerr/root"
        render_file(c.repo / "templates/seerr-build/build-seerr.sh",
                    Path(f"{jail_root}/usr/local/seerr-build/build-seerr.sh"),
                    c.v, mode=0o755)
        fixes_dir = c.repo / "templates/seerr-build/migration-fixes"
        for f in fixes_dir.iterdir():
            render_file(f, Path(f"{jail_root}/usr/local/seerr-build/"
                               f"migration-fixes/{f.name}"), c.v, mode=0o644)
        # Runs as ONE script inside the jail -- see build-seerr.sh's header
        # for why (nested `bastille cmd seerr sh -c '...'` quoting through
        # multiple shell layers is a real trap; a real script file isn't).
        build_rc = c.sudo.run(["bastille", "cmd", "seerr", "sh",
                              "/usr/local/seerr-build/build-seerr.sh"],
                             timeout=1800)
        if build_rc.returncode != 0:
            return False, "build failed -- see command output above"
        render_file(c.repo / "templates/seerr-build/seerr-start.sh",
                    Path(f"{jail_root}/usr/local/jellyseerr/seerr-start.sh"),
                    c.v, mode=0o755)
        render_file(c.repo / "templates/seerr-build/seerr.rc",
                    Path(f"{jail_root}/usr/local/etc/rc.d/seerr"), c.v, mode=0o755)
        c.plat.jail_sysrc(c.sudo, "seerr", "seerr_enable=YES")
        started = c.plat.jail_service(c.sudo, "seerr", "seerr", "start")
        return started, ("seerr built and started" if started
                         else "seerr built but the service failed to start")
    return _exec(ctx, "build seerr from source", do)


# ── 3b. Nextcloud (Immich replacement: photos + files) ──────────────────────
def create_nextcloud_stack(ctx: Ctx, accept) -> tuple[bool, str]:
    if not ctx.profile.use_nextcloud:
        return True, "nextcloud disabled in profile"
    if not accept("Provision Nextcloud (photos/files) — config + occ install "
                  "+ Photos/Memories apps?"):
        return False, "declined"

    def do(c):
        import tempfile
        app_root = "/usr/local/bastille/jails/nextcloud/root"
        # 1. in-jail Caddy vhost + PHP tuning
        render_file(c.repo / "templates/nextcloud/Caddyfile.tmpl",
                    Path(f"{app_root}/usr/local/etc/caddy/Caddyfile"),
                    c.v, mode=0o644)
        render_file(c.repo / "templates/nextcloud/php.ini.tmpl",
                    Path(f"{app_root}/usr/local/etc/php/nextcloud.ini"),
                    c.v, mode=0o644)
        # 2. front reverse-proxy vhost (only if a public domain is set)
        if c.profile.nextcloud_domain:
            front = Path("/usr/local/bastille/jails/caddy/root/usr/local/etc/caddy/Caddyfile")
            snippet = render_text(
                (c.repo / "templates/nextcloud/front-caddy.snippet.tmpl").read_text(),
                c.v, "front-caddy")
            if front.exists() and snippet.strip() not in front.read_text():
                with front.open("a") as fh:
                    fh.write("\n" + snippet + "\n")
            c.plat.jail_service(c.sudo, "caddy", "caddy", "reload")
        # 3. render the bring-up script (carries secrets) to a 600 temp, run, wipe
        fd, tmp = tempfile.mkstemp(suffix="-install-nextcloud.sh")
        os.close(fd)
        tmp_path = Path(tmp)
        # install-nextcloud.sh embeds these values inside POSIX
        # single-quoted assignments; escape any literal ' so a user-typed
        # password like `it's` can't terminate the string and break (or
        # inject into) the rendered script. Auto-generated secrets are hex
        # and were never at risk.
        nc_vars = dict(c.v)
        for k in ("NC_DB_PASSWORD", "NC_ADMIN_PASSWORD", "NC_ADMIN_USER"):
            nc_vars[k] = str(nc_vars[k]).replace("'", "'\\''")
        try:
            render_file(c.repo / "templates/nextcloud/install-nextcloud.sh",
                        tmp_path, nc_vars, mode=0o700)
            rc = c.sudo.run(["sh", str(tmp_path)], timeout=1800)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        # 4. restart the app jail's services now that config is in place
        for svc in ("redis", "php-fpm", "caddy"):
            c.plat.jail_service(c.sudo, "nextcloud", svc, "restart")
        return rc.returncode == 0, (
            "nextcloud provisioned (admin user "
            f"'{c.profile.nc_admin_user}'); photos/memories installed; "
            f"reach it at {c.profile.nextcloud_domain or c.v['NC_JAIL_IP']}")
    return _exec(ctx, "provision nextcloud", do)


# ── 4. VPN jail + kill-switch + confined torrent/arr jails ───────────────────
def create_vpn_stack(ctx: Ctx, accept) -> tuple[bool, str]:
    """The vpn jail and everything confined to it are VNET jails, not the
    classic shared-stack jails every other app in this stack uses. Two real,
    live-confirmed reasons:
      1. Creating a pseudo-interface (`ifconfig wg create`) from inside a
         classic jail fails with "Operation not permitted" no matter what
         jail.conf allow.* flags are set -- there's no non-VNET workaround.
      2. pf itself can't run inside ANY jail (VNET or not) -- `pfctl` fails
         with "DIOCADDRULE: Operation not permitted" even with every allow.*
         flag tried. The kill-switch has to live on the HOST instead (see
         vpn/pf-killswitch.conf.tmpl), scoped to the WAN interface.
    A VNET jail also needs a REAL gateway IP (bastille's own auto-detected
    guess is the HOST's WAN gateway, meaningless on the private jail bridge)
    -- ensure_bridge_gateway() gives bastille0 itself an address for this.
    """
    if not ctx.profile.use_vpn_stack:
        return True, "vpn stack disabled in profile"
    st = ctx.stack()
    vpn = st["vpn"]
    confined = ", ".join(c["name"] for c in vpn["confined"])
    if not accept(f"Create VPN jail + kill-switch, confine: {confined}?"):
        return False, "declined"
    def do(c):
        rel = st["release"]
        bridge_gw = f"{c.v['SUBNET']}.1"
        # kernel WireGuard + a real gateway address for the jail bridge
        c.plat.kldload(c.sudo, *vpn.get("kmod", ["if_wg"]))
        c.plat.ensure_bridge_gateway(c.sudo, st["bridge"], f"{bridge_gw}/24")
        # vpn jail: VNET, gatewayed onto the bridge itself (not the vpn jail's
        # own IP -- it needs the SAME kind of real-internet path every other
        # jail gets, since it's the one that has to reach the WireGuard
        # endpoint over the physical NIC in the first place)
        if not c.plat.jail_exists(vpn["jail"]):
            c.plat.jail_create(c.sudo, vpn["jail"], rel, vpn["ip"], st["bridge"],
                               vnet=True, gateway=bridge_gw)
            c.plat.jail_pkg_install(c.sudo, vpn["jail"], vpn["pkg"])
        c.plat.jail_enable_forwarding(c.sudo, vpn["jail"])
        # render wg0.conf from the profile, into the vpn jail -- but only
        # actually start the tunnel if there's a real config to start. No
        # WireGuard credentials is a legitimate "configure this later" state
        # (see install.py's VPN prompt), not an error: the vpn jail and every
        # confined jail below still get created either way, so re-running the
        # installer later with real creds just needs to render+start rather
        # than re-creating anything.
        have_wg = bool(c.profile.wg_private_key and c.profile.wg_peer_public_key
                       and c.profile.wg_endpoint)
        jail_root = f"/usr/local/bastille/jails/{vpn['jail']}/root"
        wg_note = ""
        if have_wg:
            render_file(c.repo / "vpn" / "wg0.conf.tmpl",
                        Path(f"{jail_root}/usr/local/etc/wireguard/wg0.conf"),
                        c.v, mode=0o600)
            for k, val in vpn["rc"].items():
                c.plat.jail_sysrc(c.sudo, vpn["jail"], f"{k}={val}")
            # NOT jail_service() here -- confirmed live that `bastille cmd
            # <jail> service wireguard start` effectively hangs the caller:
            # wg-quick's own monitor_daemon backgrounds a route-watcher that
            # runs forever (by design, until `wg-quick down`), and the
            # wrapping subprocess.run(capture_output=True) never sees EOF on
            # its inherited pipe as long as that descendant is alive -- the
            # exact same class of bug already hit and fixed for daemon(8)
            # elsewhere in this file. Same fix: background and fully redirect
            # the launch ourselves rather than waiting on it.
            c.sudo.run(["sh", "-c", f"(bastille cmd {vpn['jail']} service "
                       "wireguard start) < /dev/null > /dev/null 2>&1 &"],
                      timeout=30)
            import time; time.sleep(3)
        else:
            wg_note = ("; NO WireGuard config set -- vpn jail created but "
                      "tunnel not started, qBittorrent/Sonarr/Radarr have no "
                      "internet access until you add one and re-run")
        # confined jails: VNET too (a classic jail has no routing table of its
        # own, so "route it via the vpn jail" would be pure documentation,
        # never actually enforced -- confirmed by the fact this same line
        # existed before with zero effect). Gatewayed onto the BRIDGE (not the
        # vpn jail) for their initial pkg install -- only re-pointed at the
        # vpn jail's own IP once they're fully provisioned, so first-time
        # setup isn't starved of internet before the kill-switch below exists
        # to make that safe.
        confined_ips = []
        for cj in vpn["confined"]:
            confined_ips.append(cj["ip"])
            freshly_created = not c.plat.jail_exists(cj["name"])
            if freshly_created:
                c.plat.jail_create(c.sudo, cj["name"], rel, cj["ip"], st["bridge"],
                                   vnet=True, gateway=bridge_gw)
                if cj.get("allow_mlock"):
                    c.plat.jail_add_param(c.sudo, cj["name"], "allow.mlock", "1")
                    c.plat.jail_restart(c.sudo, cj["name"])
                c.plat.jail_pkg_install(c.sudo, cj["name"], cj["pkg"])
            for k, val in (cj.get("rc") or {}).items():
                c.plat.jail_sysrc(c.sudo, cj["name"], f"{k}={val}")
            for m in (cj.get("mounts") or []):
                c.plat.jail_mount(c.sudo, cj["name"], m["host"], m["jail"],
                                  ro=m.get("ro", False))
            # Lock the default route onto the vpn jail's own IP. A freshly
            # created jail was deliberately gatewayed onto the plain bridge
            # (bridge_gw) above so its OWN pkg install just had internet --
            # sysrc alone only edits rc.conf, so its LIVE route table still
            # points at bridge_gw until an actual restart re-runs the jail's
            # boot-time routing setup; confirmed live (a freshly-created
            # jail kept routing straight to the bridge gateway, silently
            # skipping the vpn jail entirely, until this restart was added).
            # Only skip the restart when the value is ALREADY correct, so a
            # plain re-run doesn't bounce an already-locked-down jail.
            if c.plat.jail_defaultrouter(c.sudo, cj["name"]) != vpn["ip"]:
                c.plat.jail_sysrc(c.sudo, cj["name"], f"defaultrouter={vpn['ip']}")
                c.plat.jail_restart(c.sudo, cj["name"])
            for svc in (cj.get("services") or []):
                c.plat.jail_service(c.sudo, cj["name"], svc, "start")
            if cj["name"] == "qbittorrent":
                _qbittorrent_skip_login(c, cj["name"])
        # HOST-side kill-switch: append once (idempotent), covering every
        # confined jail's IP, then reload. This is what actually enforces
        # "no tunnel, no leak" now that jail-side pf is off the table.
        pf_conf = Path("/etc/pf.conf")
        marker = "# freebsd-media-setup vpn kill-switch"
        text = pf_conf.read_text() if pf_conf.exists() else ""
        if marker not in text:
            snippet = render_text(
                (c.repo / "vpn" / "pf-killswitch.conf.tmpl").read_text(),
                dict(c.v, VPN_CONFINED_IPS=", ".join(confined_ips),
                    VPN_JAIL_NAME=vpn["jail"], VPN_JAIL_IP=vpn["ip"]),
                "pf-killswitch")
            pf_conf.write_text(text.rstrip("\n") + f"\n\n{marker}\n{snippet}\n")
            c.plat.pf_reload(c.sudo)
        return True, f"vpn + confined: {confined}{wg_note}"
    return _exec(ctx, "create vpn stack", do)


def _qbittorrent_skip_login(c: Ctx, name: str) -> None:
    """"No login details, auto enter" for anyone on the LAN or the jail
    bridge -- qBittorrent's own documented AuthSubnetWhitelist feature skips
    the WebUI login page entirely for whitelisted source subnets, rather than
    trying to fake a stored credential. Still requires login from anywhere
    else (e.g. if later reverse-proxied to the public internet), which is the
    behavior actually wanted -- this isn't a global auth disable.

    The config file only exists after qBittorrent has started at least once
    (confirmed live -- it self-populates [BitTorrent]/[Network] on first
    run), so this patches it in place and restarts rather than trying to
    seed it before the service has ever run."""
    conf = Path(f"/usr/local/bastille/jails/{name}/root/var/db/qbittorrent"
               "/conf/qBittorrent/config/qBittorrent.conf")
    for _ in range(10):
        if conf.exists():
            break
        import time; time.sleep(1)
    if not conf.exists():
        return
    text = conf.read_text()
    if "AuthSubnetWhitelistEnabled" in text:
        return
    subnets = f"{c.v['SUBNET']}.0/24, {c.profile.lan_cidr}"
    block = ("[Preferences]\n"
            "WebUI\\AuthSubnetWhitelistEnabled=true\n"
            f"WebUI\\AuthSubnetWhitelist={subnets}\n")
    if "[Preferences]" in text:
        text = text.replace("[Preferences]\n", block, 1)
    else:
        text = text.rstrip("\n") + "\n\n" + block
    conf.write_text(text)
    c.plat.jail_service(c.sudo, name, "qbittorrent", "restart")


# ── 4b. public access: Caddy reverse-proxy + dynamic DNS / Cloudflare Tunnel ──
# qbittorrent is excluded on purpose: its WebUI login is a source-subnet
# whitelist that deliberately includes the caddy jail's own IP (see
# _qbittorrent_skip_login), so every reverse-proxied request would arrive
# pre-authenticated -- publicly exposing it would mean exposing it with NO
# login at all. The *arr apps keep their own form/API auth and stay
# reachable; qBittorrent stays LAN-only.
_PUBLIC_APPS_EXCLUDE = {"nextcloud", "nextcloud-db", "caddy", "qbittorrent"}


def _public_domain(profile: Profile, app: str) -> str:
    """One hostname per app -- Plex specifically doesn't reverse-proxy well
    under a path prefix, so every provider that CAN do real per-app
    subdomains (DuckDNS, Cloudflare) gets one per app. No-IP/Dynu's simple
    dynamic-update API only keeps ONE pre-existing hostname's IP current --
    it can't auto-create new ones the way DuckDNS's update API does -- so
    under those two, only `app == "jellyfin"` gets a domain (the single
    shared hostname), everything else stays LAN-only. This is a real,
    working, honestly-scoped-down integration for those two, not a partial
    per-app scheme with some apps silently broken."""
    p = profile
    if p.public_access == "duckdns" and p.ddns_base:
        return f"{p.ddns_base}-{app}.duckdns.org"
    if p.public_access == "cloudflare" and p.cloudflare_domain:
        return f"{app}.{p.cloudflare_domain}"
    if p.public_access in ("noip", "dynu") and p.ddns_base and app == "jellyfin":
        return p.ddns_base
    return ""


def configure_public_access(ctx: Ctx, accept) -> tuple[bool, str]:
    if not ctx.profile.public_access:
        return True, "public access not configured (LAN-only, skipped)"
    st = ctx.stack()
    apps = [(j["name"], j["ip"], j["ports"][0]) for j in st["jails"]
           if j["name"] not in _PUBLIC_APPS_EXCLUDE and j.get("ports")]
    apps += [(cj["name"], cj["ip"], cj["ports"][0]) for cj in st["vpn"]["confined"]
            if cj.get("ports")]
    provider = ctx.profile.public_access
    if not accept(f"Expose apps to the internet via {provider} (reverse-proxied "
                  f"through Caddy, one hostname per app)?"):
        return False, "declined"
    def do(c):
        front = Path("/usr/local/bastille/jails/caddy/root/usr/local/etc/caddy/Caddyfile")
        text = front.read_text() if front.exists() else ""
        hostnames = []  # (app, domain)
        changed = False
        for name, ip, port in apps:
            domain = _public_domain(c.profile, name)
            if not domain:
                continue
            hostnames.append((name, domain))
            marker = f"# app:{name}"
            if marker in text:
                continue
            text += f"\n{marker}\n{domain} {{\n\treverse_proxy {ip}:{port}\n}}\n"
            changed = True
        # Nextcloud: respect an explicitly-set nextcloud_domain (its own
        # dedicated step already wires that one up) -- only auto-derive a
        # public hostname here if the user hasn't already pinned one.
        if c.profile.use_nextcloud and not c.profile.nextcloud_domain:
            d = _public_domain(c.profile, "nextcloud")
            if d and "# app:nextcloud" not in text:
                text += f"\n# app:nextcloud\n{d} {{\n\treverse_proxy {c.v['NC_JAIL_IP']}:80\n}}\n"
                hostnames.append(("nextcloud", d))
                changed = True
        elif c.profile.nextcloud_domain:
            hostnames.append(("nextcloud", c.profile.nextcloud_domain))
        if changed:
            front.write_text(text)
            c.plat.jail_service(c.sudo, "caddy", "caddy", "reload")

        limited_note = ""
        if provider in ("noip", "dynu") and len(hostnames) <= 1:
            limited_note = ("; No-IP/Dynu's update API can't auto-create new "
                            "hostnames like DuckDNS does, so only Jellyfin is "
                            "public under this provider -- everything else "
                            "stays LAN-only")

        if provider == "duckdns":
            subs = [d.split(".duckdns.org")[0] for _, d in hostnames
                   if d.endswith(".duckdns.org")]
            ok, out = c.plat.ddns_update_duckdns(subs, c.profile.ddns_token)
            script = (f"#!/bin/sh\ncurl -fsS -m 15 "
                     f"\"https://www.duckdns.org/update?domains={','.join(subs)}"
                     f"&token={c.profile.ddns_token}&ip=\" >/dev/null 2>&1\n")
            dst = Path("/usr/local/freebsd-media-setup/scripts/duckdns-update.sh")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(script); dst.chmod(0o700)
            c.plat.crontab_user(c.sudo, [f"*/5 * * * * {dst} >/dev/null 2>&1"],
                               "root", tag="duckdns")
            note = f"; DuckDNS update {'ok' if ok else 'FAILED: ' + out}, refreshed every 5 min via cron"
        elif provider in ("noip", "dynu"):
            update_url = ("https://dynupdate.no-ip.com/nic/update" if provider == "noip"
                         else "https://api.dynu.com/nic/update")
            hostname = hostnames[0][1] if hostnames else c.profile.ddns_base
            ok, out = c.plat.ddns_update_simple(hostname, c.profile.ddns_user,
                                                c.profile.ddns_token, update_url)
            note = f"; {provider} update {'ok' if ok else 'FAILED: ' + out}"
        elif provider == "cloudflare":
            tunnel_ok = c.plat.cloudflared_install_tunnel(c.sudo, c.profile.cloudflare_tunnel_token)
            # A dashboard-managed tunnel routes a hostname by a proxied
            # CNAME to <tunnel-id>.cfargotunnel.com; the tunnel UUID rides
            # inside the token itself, so decode it and create real
            # working records (an earlier version pointed them at
            # {domain}.cdn.cloudflare.net, which nothing resolves).
            tunnel_id = c.plat.tunnel_id_from_token(c.profile.cloudflare_tunnel_token)
            cname_notes = []
            if tunnel_id:
                for name, domain in hostnames:
                    ok, out = c.plat.cloudflare_dns_upsert(
                        c.profile.cloudflare_api_token, c.profile.cloudflare_zone_id,
                        domain, f"{tunnel_id}.cfargotunnel.com")
                    cname_notes.append(f"{domain}:{'ok' if ok else 'FAILED'}")
            else:
                cname_notes.append("tunnel ID undecodable from token -- create the "
                                   "per-hostname CNAMEs to <tunnel-id>.cfargotunnel.com "
                                   "by hand")
            note = (f"; cloudflared tunnel {'started' if tunnel_ok else 'FAILED to start'}; "
                    f"DNS: {', '.join(cname_notes)} -- point every hostname's Public "
                    f"Hostname entry at this box's Caddy in the Cloudflare Zero Trust "
                    f"dashboard (tunnel routing itself isn't set by a local file)")
        else:
            note = ""
        return True, (f"public access via {provider}: "
                      f"{', '.join(d for _, d in hostnames) or '(none configured)'}"
                      f"{note}{limited_note}")
    return _exec(ctx, "configure public access", do)


# ── 5. self-healing routine + cron ───────────────────────────────────────────
def cron_expr(frequency: str, time_str: str) -> str:
    """Turn a (frequency, HH:MM) pair into a 5-field cron expression. Shared
    logic with the dashboard's own live schedule-change endpoint (status-
    dashboard-server.py duplicates this -- that script is deployed standalone
    with no import path back into lib/, matching every other self-contained
    helper already duplicated there)."""
    try:
        hh, mm = time_str.split(":")
        hh, mm = int(hh) % 24, int(mm) % 60
    except (ValueError, AttributeError):
        hh, mm = 3, 0
    if frequency == "weekly":
        return f"{mm} {hh} * * 0"      # every Sunday
    if frequency == "monthly":
        return f"{mm} {hh} 1 * *"      # 1st of every month
    if frequency == "yearly":
        return f"{mm} {hh} 1 1 *"      # Jan 1st
    return f"{mm} {hh} * * *"          # daily (also the fallback for a bad value)


def install_daily_routine(ctx: Ctx, accept) -> tuple[bool, str]:
    if not ctx.profile.use_daily_routine:
        return True, "daily routine disabled in profile"
    if not accept(f"Install self-heal routine, running {ctx.profile.daily_routine_frequency} "
                  f"at {ctx.profile.daily_routine_time}?"):
        return False, "declined"
    def do(c):
        dst = Path(c.v["HOME"]) / ".hermes" / "scripts" / "daily-routine.sh"
        render_file(c.tpl / "daily-routine.sh", dst, c.v, mode=0o755)
        expr = cron_expr(c.profile.daily_routine_frequency, c.profile.daily_routine_time)
        entries = [f"{expr} {dst} >/dev/null 2>&1"]
        # root, not the profile user: every real op in daily-routine.sh
        # (bastille cmd, pfctl -f, zfs rollback) needs root, and a cron job
        # can't be prompted for a sudo password at 3am to get it -- {{HOME}}
        # is just where the script/logs live, not who should run it.
        ok = c.plat.crontab_user(c.sudo, entries, "root", tag="daily-routine")
        # Publish the effective schedule to a user-readable file: the
        # authoritative tag block lives in ROOT's crontab (mode 600),
        # which the user-run dashboard collector can never read (see
        # status-collect.py's _read_daily_routine_schedule). The
        # dashboard's conf dir is chowned to the profile user by the
        # dashboard step; a plain 644 file stays readable either way.
        sched = (Path(c.v["HOME"]) / ".config/status-dashboard"
                 / "routine-schedule.json")
        sched.parent.mkdir(parents=True, exist_ok=True)
        sched.write_text(json.dumps(
            {"frequency": c.profile.daily_routine_frequency,
             "time": c.profile.daily_routine_time}) + "\n")
        sched.chmod(0o644)
        if c.profile.vpn_rotate_hourly:
            c.plat.crontab_user(c.sudo, [f"0 * * * * {dst} --quick >/dev/null 2>&1"],
                                "root", tag="vpn-rotate-hourly")
        else:
            c.plat.crontab_user(c.sudo, [], "root", tag="vpn-rotate-hourly")
        return ok, (f"installed {dst}, runs {c.profile.daily_routine_frequency} "
                   f"at {c.profile.daily_routine_time} (cron: {expr}) "
                   f"rc={'0' if ok else 'nonzero'}")
    return _exec(ctx, "install daily routine", do)


# ── 6. desktop panel defaults (top edge, opaque, host-matched thickness) ─────
def configure_desktop_panel(ctx: Ctx, accept) -> tuple[bool, str]:
    if not accept("Set the KDE panel to the top edge, opaque, candy-icons, "
                   "Sweet Plasma style + Breeze Dark colors, and the "
                   "CopperArch desktop wallpaper "
                   "(one-time default, applies on next login)?"):
        return False, "declined"
    def do(c):
        home = Path(c.v["HOME"])
        bin_dir = home / ".local/bin"
        autostart = home / ".config/autostart"
        bin_dir.mkdir(parents=True, exist_ok=True)
        autostart.mkdir(parents=True, exist_ok=True)
        render_file(c.tpl / "dashboard/kde-panel-defaults.sh",
                    bin_dir / "kde-panel-defaults.sh", c.v, mode=0o755)
        (autostart / "kde-panel-defaults.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=KDE Panel Defaults\n"
            f"Exec={bin_dir}/kde-panel-defaults.sh\n"
            "X-KDE-autostart-phase=1\nNoDisplay=true\n")
        c.sudo.run(["chown", "-R", f"{c.profile.user}:{c.profile.user}",
                   str(bin_dir), str(autostart)], timeout=30)
        return True, "panel defaults autostart installed (applies next login)"
    return _exec(ctx, "configure desktop panel", do)


# ── 6b. Konsole theme (CopperArch colorscheme, faint badge watermark) ────────
def configure_konsole_theme(ctx: Ctx, accept) -> tuple[bool, str]:
    if not accept("Set Konsole's default profile to a CopperArch theme "
                   "(faint badge watermark, no other terminal changes)?"):
        return False, "declined"
    def do(c):
        home = Path(c.v["HOME"])
        kdir = home / ".local/share/konsole"
        kdir.mkdir(parents=True, exist_ok=True)
        render_file(c.tpl / "dashboard/copperarch-konsole-bg.png",
                    kdir / "copperarch-konsole-bg.png", c.v, mode=0o644)
        render_file(c.tpl / "dashboard/CopperArch.colorscheme",
                    kdir / "CopperArch.colorscheme", c.v, mode=0o644)
        render_file(c.tpl / "dashboard/CopperArch.profile",
                    kdir / "CopperArch.profile", c.v, mode=0o644)
        konsolerc = home / ".config/konsolerc"
        text = konsolerc.read_text() if konsolerc.exists() else ""
        if "[Desktop Entry]" in text:
            if "DefaultProfile=" in text:
                text = re.sub(r"^DefaultProfile=.*$",
                              "DefaultProfile=CopperArch.profile", text, flags=re.M)
            else:
                text = text.replace("[Desktop Entry]",
                                     "[Desktop Entry]\nDefaultProfile=CopperArch.profile", 1)
        else:
            text += "\n[Desktop Entry]\nDefaultProfile=CopperArch.profile\n"
        konsolerc.write_text(text)
        c.sudo.run(["chown", "-R", f"{c.profile.user}:{c.profile.user}",
                   str(kdir), str(konsolerc)], timeout=30)
        return True, "Konsole default profile set to CopperArch"
    return _exec(ctx, "configure Konsole theme", do)


# ── 7. dashboard (loopback HTTP + collector + KWin below-layer window) ───────
def install_dashboard(ctx: Ctx, accept) -> tuple[bool, str]:
    if not ctx.profile.use_dashboard:
        return True, "dashboard disabled in profile"
    if not accept("Install the KDE desktop status dashboard?"):
        return False, "declined"
    def do(c):
        home = Path(c.v["HOME"])
        src = c.tpl / "dashboard"
        bin_dir = home / ".local/bin"
        share = home / ".local/share/status-dashboard"
        conf = home / ".config/status-dashboard"
        for d in (bin_dir, share, conf):
            d.mkdir(parents=True, exist_ok=True)

        # ttyd (terminal-pane bridge) + bash (pane script shebang) + tmux
        # (keeps an agent pane's job alive across a dock close/disconnect --
        # see dashboard-pane.sh): host pkgs, installed as root here rather
        # than relying on status-dashboard-install.sh's own `sudo -n pkg
        # install` once it's running as the profile user -- that user is
        # never granted any sudo rights, so that call is a guaranteed no-op
        # and the terminal panes silently never work.
        c.sudo.run(["pkg", "install", "-y", "bash", "ttyd", "tmux"], timeout=300,
                   env=dict(os.environ, ASSUME_ALWAYS_YES="YES"))

        # Scripts + python modules -> ~/.local/bin, rendering {{MEDIA_POOL}} etc.
        # (data source is bastille/jls; the KWin below-layer rule is identical
        # because KDE Plasma uses the same KWin the Linux edition targeted.)
        for f in sorted(src.iterdir()):
            if f.suffix in (".py", ".sh"):
                render_file(f, bin_dir / f.name, c.v, mode=0o755)
        # The page carries no {{TOKENS}} — copy it verbatim, 644.
        render_file(src / "index.html", share / "index.html", c.v, mode=0o644)
        # Project version, shown in the dashboard's About panel (click the logo).
        if (c.repo / "VERSION").exists():
            render_file(c.repo / "VERSION", share / "VERSION", c.v, mode=0o644)

        # Seed config: API keys (blank, chmod 600) + the VPN-confined group,
        # kept in sync with stack.yaml so collector and repair server agree.
        keys = conf / "media-keys.env"
        if not keys.exists():
            keys.write_text(
                "# chmod 600. Fill these from each app's Settings → General → "
                "Security (API key) and qBittorrent WebUI creds.\n"
                f"QBIT_USER={c.profile.qbit_user}\nQBIT_PASS={c.profile.qbit_password}\n"
                "RADARR_KEY=\nSONARR_KEY=\nPROWLARR_KEY=\nJELLYFIN_KEY=\n")
            keys.chmod(0o600)
        grp = conf / "vpn-group.json"
        if not grp.exists():
            grp.write_text('{"hub": "vpn", '
                           '"members": ["qbittorrent", "sonarr", "radarr"]}\n')

        # AI panes: OpenRouter key (blank) + model slugs managed by
        # ai-panes-check.py; then run it once so model-pricing.json populates
        # and the paid/free pane labels + prices are correct on first paint.
        ds = conf / "deepseek.env"
        if not ds.exists():
            ds.write_text(
                "# chmod 600. One OpenRouter key powers every online AI pane.\n"
                "# Get one (free tier available) at https://openrouter.ai/keys\n"
                f"DEEPSEEK_API_KEY={c.profile.openrouter_api_key}\n"
                "DEEPSEEK_BASE_URL=https://openrouter.ai/api/v1\n"
                "# The block below is rewritten nightly by ai-panes-check.py.\n")
            ds.chmod(0o600)
        elif c.profile.openrouter_api_key:
            # Re-running the installer with a key now set in the profile --
            # don't clobber the managed model block below it, just patch the
            # one line a person would otherwise have to hand-edit.
            text = ds.read_text()
            text = re.sub(r"^DEEPSEEK_API_KEY=.*$",
                          f"DEEPSEEK_API_KEY={c.profile.openrouter_api_key}",
                          text, count=1, flags=re.M)
            ds.write_text(text)
        # Everything above was written as root (this installer always runs
        # as root). The su -l calls below need to actually create/rewrite
        # files here as the profile user, and deepseek.env is chmod 600 --
        # owner-only -- so without this chown, copper has zero access to
        # its own dashboard config despite the directory looking shared.
        c.sudo.run(["chown", "-R", f"{c.profile.user}:{c.profile.user}",
                   str(bin_dir), str(share), str(conf)], timeout=30)

        # -l, not -m: -m/-p preserve the CALLER's environment, which means
        # HOME stays root's (whatever ran the installer) instead of
        # switching to the target user's -- every Path.home()-based lookup
        # in ai-panes-check.py (env file, pricing cache) then points at
        # /root instead of the profile user's actual config directory.
        c.sudo.run(["sh", "-c",
                    f"su -l {c.profile.user} -c '{bin_dir}/ai-panes-check.py' "
                    f"|| {bin_dir}/ai-panes-check.py"], timeout=90)

        # Idempotent installer: cron collector + ttyd watchdog, autostart,
        # KWin rule. Best-effort — the collector works even if the desktop
        # bits are skipped on a headless box.
        # su -l, not a bare sudo.run: the script resolves everything off
        # $HOME ($HOME/.local/bin, $HOME/.config/autostart, ...) and this
        # runs as root otherwise, both pointing it at /root instead of the
        # profile user's actual directories and leaving the autostart
        # entry it writes owned by root inside that user's own $HOME.
        rc = c.sudo.run(["su", "-l", c.profile.user, "-c",
                         f"sh {bin_dir}/status-dashboard-install.sh"],
                        timeout=300)

        # The repair server runs as ROOT, deliberately, not through the su -l
        # above: its whitelisted fixes restart jails, upgrade packages and
        # restart services, none of which the desktop user can do, and a
        # user crontab can't grant that. Same pattern as daily-routine.sh.
        # DASHBOARD_HOME must be passed explicitly: as root, Path.home() in
        # the script would resolve to /root instead of this user's actual
        # data directory (see status-dashboard-server.py / status_keys.py).
        log_file = share / "server.log"
        launch = (f"pgrep -f status-dashboard-server.py >/dev/null 2>&1 || "
                 f"env DASHBOARD_HOME={home} /usr/sbin/daemon -r -o {log_file} "
                 f"python3.11 {bin_dir}/status-dashboard-server.py")
        server_ok = c.plat.crontab_user(
            c.sudo, [f"* * * * * {launch}  # status-dashboard server watchdog"],
            "root", tag="dashboard-watchdog")
        # Backgrounded and fully redirected, not just piped through -o: this
        # call goes through Python's own capturing subprocess.run(), and
        # daemon(8) doesn't always finish detaching from the invoking shell
        # before returning -- if it hasn't, the still-open stdout/stderr
        # pipe back to Python never sees EOF and the install hangs forever
        # waiting to read output that will never stop coming. Redirecting
        # away from that pipe and backgrounding the whole thing here (rather
        # than relying on daemon's own detach) sidesteps it either way.
        c.sudo.run(["sh", "-c", f"({launch}) < /dev/null > /dev/null 2>&1 &"],
                   timeout=30)

        return server_ok, ("dashboard deployed to ~/.local/bin + ~/.local/share "
                      f"(installer rc={rc.returncode}, server cron rc="
                      f"{'0' if server_ok else 'nonzero'}); fill API keys in "
                      "~/.config/status-dashboard/media-keys.env")
    return _exec(ctx, "install dashboard", do)


# ── 8. local AI (GLM via ollama) — opt-in only ───────────────────────────────
# Official sources only: FreeBSD's misc/ollama package and the ollama.com
# model library (which mirrors zai-org's weights). "Free GLM-5.x installer"
# repos on GitHub are malware lures -- never wire one in here.
GLM_BIG, GLM_SMALL = "glm-4.7-flash", "glm4:9b"   # 19 GB / 5.5 GB downloads
GLM_BIG_MIN_RAM_GB = 24


def _ram_gb() -> float:
    p = run(["sysctl", "-n", "hw.physmem"], quiet=True)
    try:
        return int(p.stdout.strip()) / 1024 ** 3
    except (ValueError, AttributeError):
        return 0.0


def pick_glm_model(profile: Profile) -> str:
    if profile.glm_model and profile.glm_model != "auto":
        return profile.glm_model
    return GLM_BIG if _ram_gb() >= GLM_BIG_MIN_RAM_GB else GLM_SMALL


def install_local_ai(ctx: Ctx, accept) -> tuple[bool, str]:
    if not ctx.profile.use_local_glm:
        return True, "not selected in profile"
    model = pick_glm_model(ctx.profile)
    size = {GLM_BIG: "19 GB", GLM_SMALL: "5.5 GB"}.get(model, "size unknown")
    if not accept(f"Install ollama and download GLM model '{model}' ({size}, "
                  f"{_ram_gb():.0f} GB RAM detected)?"):
        return False, "declined"
    def do(c):
        import shutil, time
        user = c.profile.user
        home = Path(c.v["HOME"])
        ok, msg = c.plat.install(c.sudo, ["ollama"])
        if not ok:
            return False, f"pkg install ollama {msg}"
        # Models on the media pool when there is one, never the boot pool.
        pool = Path(c.profile.media_pool or "")
        models = pool / "ollama-models" if pool.is_dir() else home / ".ollama/models"
        c.sudo.run(["install", "-d", "-o", user, "-g", user, str(models)], timeout=30)
        free_gb = shutil.disk_usage(models if models.exists() else home).free / 1e9
        if free_gb < 30:
            return False, f"only {free_gb:.0f} GB free at {models} -- not pulling"
        # The package's rc.d script (misc/ollama) runs as ollama_user; rc.subr's
        # generic <name>_env passes our model dir, loopback bind and a short
        # keep-alive (the default would pin a 19 GB model in RAM indefinitely).
        c.plat.sysrc(c.sudo, "ollama_enable=YES", f"ollama_user={user}",
                     f"ollama_env=OLLAMA_MODELS={models} "
                     "OLLAMA_HOST=127.0.0.1:11434 OLLAMA_KEEP_ALIVE=10m")
        c.plat.service(c.sudo, "ollama", "restart")
        env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434")
        for _ in range(30):
            if run(["ollama", "list"], quiet=True, env=env).returncode == 0:
                break
            time.sleep(2)
        log(f"  downloading {model} -- {size}, this can take a while")
        p = c.sudo.run(["su", "-l", user, "-c",
                        f"env OLLAMA_HOST=127.0.0.1:11434 ollama pull {model}"],
                       timeout=6 * 3600)
        if p.returncode != 0:
            return False, f"ollama pull {model} rc={p.returncode} (re-run to resume)"
        # Tells the dashboard which model to show; its GLM panes stay hidden
        # on machines without this file.
        conf = home / ".config/status-dashboard"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "local-ai.env").write_text(f"GLM_MODEL={model}\nGLM_KEEPALIVE=10m\n")
        c.sudo.run(["chown", "-R", f"{user}:{user}", str(conf)], timeout=30)
        return True, f"ollama + {model} installed (models in {models})"
    return _exec(ctx, f"install ollama + pull {model}", do)


ORDER = [
    ("Check FreeBSD",          check_freebsd),
    ("Host packages",          install_host_packages),
    ("Bastille bootstrap",     bootstrap_bastille),
    ("Storage auto-pooling",   provision_storage),
    ("Hotplug drive watch",    install_hotplug_watch),
    ("Media jails",            create_media_jails),
    ("Build Seerr",            build_seerr_from_source),
    ("Nextcloud",              create_nextcloud_stack),
    ("VPN stack + killswitch", create_vpn_stack),
    ("Public access",          configure_public_access),
    ("Self-heal routine",      install_daily_routine),
    ("Desktop panel defaults", configure_desktop_panel),
    ("Konsole theme",          configure_konsole_theme),
    ("Desktop dashboard",      install_dashboard),
    ("Local AI (GLM)",         install_local_ai),
]
