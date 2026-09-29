#!/usr/bin/env python3.11
"""
status-dashboard-server.py — FreeBSD edition. Serves the dashboard and runs its
one-click repairs. Same security model as the Linux edition:

  * binds loopback only (127.0.0.1),
  * closed whitelist of fix ids — the page asks for "jail_restart" with a jail
    name and nothing else; no shell strings ever come from the client,
  * every argument validated against live state (a jail name must match a real
    jail) and every command run as an argv list, never through a shell,
  * Sec-Fetch-Site / Origin CSRF guard, 64 KB body cap, async job + /api/job
    polling, and reboot gated behind an explicit confirmation token.

Handlers retargeted: docker->bastille, systemctl->service, apt->pkg,
gluetun->the vpn jail + wg.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from status_keys import load_env, load_group, jail_ip

# This server runs as root (its repair actions restart jails/services and
# run pkg upgrades -- the desktop user can't do any of that), but its data
# lives under that user's actual home, not /root. lib/steps.py sets
# DASHBOARD_HOME when launching it; see status_keys.py for the same pattern.
_HOME = Path(os.environ["DASHBOARD_HOME"]) if "DASHBOARD_HOME" in os.environ else Path.home()
ROOT = _HOME / ".local/share/status-dashboard"
BIN = _HOME / ".local/bin"
PORT = 8099
BIND = "127.0.0.1"
MAX_BODY = 64 * 1024

PROWLARR = {"url": f"http://{jail_ip('prowlarr')}:9696",
            "key": load_env().get("PROWLARR_KEY", "")}
_GROUP_HUB, GROUP_MEMBERS = load_group()
_DESKTOP_USER = _HOME.name

# Whitelisted "open this in a browser" targets -- same security posture as
# FIXES: the page can only ever ask for one of these ids, never a raw URL.
OPEN_LINKS = {
    "jellyfin":        f"http://{jail_ip('jellyfin')}:8096",
    "plex":            f"http://{jail_ip('plex')}:32400/web",
    "prowlarr":        f"http://{jail_ip('prowlarr')}:9696",
    "sonarr":          f"http://{jail_ip('sonarr')}:8989",
    "radarr":          f"http://{jail_ip('radarr')}:7878",
    "bazarr":          f"http://{jail_ip('bazarr')}:6767",
    "seerr":           f"http://{jail_ip('seerr')}:5055",
    "qbittorrent":     f"http://{jail_ip('qbittorrent')}:8080",
    "nextcloud":       f"http://{jail_ip('nextcloud')}",
    "caddy":           f"http://{jail_ip('caddy')}",
    "openrouter_keys": "https://openrouter.ai/keys",
    "protonvpn_wg":    "https://protonvpn.com/support/wireguard-configuration-generator/",
    "windscribe_wg":   "https://windscribe.com/getconfig/wireguard",
    "surfshark_wg":    "https://surfshark.com/download/router",
}
_LINK_BROWSER_PROFILE = str(ROOT / "link-browser-profile")

_lock = threading.Lock()
_running = {"id": None, "since": 0}
_job_lock = threading.Lock()
_job = {"running": False, "done": False, "results": [], "total": 0,
        "started": 0, "current": None, "error": None}


# ── helpers ──────────────────────────────────────────────────────────────────
def sh(argv, timeout=300):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {shlex.join(argv)}"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _session_env():
    """DISPLAY/XAUTHORITY for the logged-in desktop session, read off a live
    process in it. This server runs as root (repairs need it) but the
    session belongs to the desktop user, and SDDM randomizes the xauth path
    every login -- nothing here can be hardcoded, it has to be discovered."""
    rc, out = sh(["pgrep", "-u", _DESKTOP_USER, "-f", "plasmashell"], timeout=10)
    pid = out.split()[0] if rc == 0 and out.strip() else None
    if not pid:
        return {}
    rc, out = sh(["procstat", "-e", pid], timeout=10)
    lines = out.strip().splitlines()
    if rc != 0 or len(lines) < 2:
        return {}
    env = {}
    for field in lines[-1].split()[2:]:   # drop the PID/COMM columns
        if "=" in field:
            k, _, v = field.partition("=")
            env[k] = v
    return env


# (binary name, family) -- librewolf preferred (the user's pick, privacy
# hardened), but this checks for whatever's ACTUALLY installed rather than
# assuming: covers every browser this project's own kiosk-window search
# (status-dashboard-run.sh) already knows how to find, plus the Firefox
# family. Family matters because the two use completely different profile
# flags -- Chromium's --user-data-dir auto-creates a fresh directory;
# Firefox's --profile refuses to start at all if the directory doesn't
# already exist.
_BROWSER_CANDIDATES = [
    ("librewolf", "firefox"),
    ("firefox", "firefox"),
    ("chromium", "chromium"),
    ("chrome", "chromium"),
    ("google-chrome", "chromium"),
    ("ungoogled-chromium", "chromium"),
    ("brave", "chromium"),
]


def _find_browser():
    for name, family in _BROWSER_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p, family
    return None, None


def open_link(link_id):
    """Open a whitelisted URL in the desktop's browser, using one persistent
    profile so a login (Jellyfin, OpenRouter, a VPN provider's site, ...)
    sticks across clicks -- if that browser's already running, this just
    opens a new tab in it rather than a second window."""
    url = OPEN_LINKS.get(link_id)
    if not url:
        return False, f"unknown link id: {link_id!r}"
    browser, family = _find_browser()
    if not browser:
        return False, ("no browser installed -- install one of: "
                       + ", ".join(n for n, _ in _BROWSER_CANDIDATES))
    sess = _session_env()
    if not sess.get("DISPLAY"):
        return False, "no active desktop session found to open a browser in"

    if family == "firefox":
        # Firefox-family --profile refuses to start at all if the directory
        # doesn't already exist ("profile cannot be loaded") -- it won't
        # create one for you, unlike Chromium's --user-data-dir. This
        # server runs as root, so a plain mkdir here would leave it
        # root-owned and unwritable by the su'd-to desktop user; chown it.
        profile_path = Path(_LINK_BROWSER_PROFILE)
        if not profile_path.is_dir():
            profile_path.mkdir(parents=True, exist_ok=True)
            # HTTPS-Only mode (on by default, correctly) otherwise
            # interstitials every plain-http jail link (jellyfin, sonarr,
            # ... none of them terminate TLS themselves) -- this pref scopes
            # the exemption to private/local-network addresses only, so
            # real external sites (openrouter.ai, the VPN providers) keep
            # full HTTPS-Only enforcement. Chromium has no equivalent
            # default-on interstitial, so it needs no matching fix.
            (profile_path / "user.js").write_text(
                'user_pref("dom.security.https_only_mode.upgrade_local", false);\n')
            try:
                pw = pwd.getpwnam(_DESKTOP_USER)
                for f in (profile_path, profile_path / "user.js"):
                    os.chown(f, pw.pw_uid, pw.pw_gid)
            except KeyError:
                pass
        profile_flag = f"--profile {shlex.quote(_LINK_BROWSER_PROFILE)}"
    else:
        profile_flag = f"--user-data-dir={shlex.quote(_LINK_BROWSER_PROFILE)}"

    inner = (f"env DISPLAY={shlex.quote(sess['DISPLAY'])} "
            f"XAUTHORITY={shlex.quote(sess.get('XAUTHORITY', ''))} "
            f"{shlex.quote(browser)} {profile_flag} {shlex.quote(url)}")
    try:
        subprocess.Popen(["su", "-l", _DESKTOP_USER, "-c", inner],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return True, f"opened {url}"


def known_jails():
    """All jails Bastille knows about (running or stopped)."""
    # "-a" is fully deprecated on newer bastille (exits 1, not just a
    # warning) -- "all" is the replacement. jls only lists RUNNING jails, so
    # it stays as a fallback, not the primary source (a stopped jail should
    # still validate as "known" for e.g. a jail_start fix).
    rc, out = sh(["bastille", "list", "all"], timeout=30)
    if rc != 0:
        rc, out = sh(["jls", "name"], timeout=15)
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0].lower() in ("jid", "state", "name"):
            continue
        # bastille list columns vary by version, so the jail name can't be
        # pinned to a column -- but it is never a bare number (the JID
        # column) or an IP (the address columns). Filtering those keeps a
        # JID like "12" or an address like "10.17.0.21" from passing
        # valid_jail() and being handed to bastille as a jail name.
        for tok in parts:
            if (re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tok)
                    and not tok.isdigit()
                    and not re.fullmatch(r"[0-9.]+", tok)):
                names.add(tok)
    return names


def valid_jail(name):
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        return False
    return name in known_jails()


def recollect():
    return sh(["python3.11", str(BIN / "status-collect.py")], timeout=180)


# ── fix handlers ─────────────────────────────────────────────────────────────
def _root_argv(argv):
    """Wrap argv for a privileged command. This server runs as root (its
    cron watchdog is root's), and FreeBSD ships no sudo -- the Linux
    edition's unconditional `sudo -n` prefix made these repairs fail
    outright with "command not found" wherever sudo wasn't installed.
    Fall back to passwordless sudo only when we're somehow NOT root
    (e.g. a manually launched dev instance)."""
    if os.geteuid() == 0:
        return list(argv)
    if shutil.which("sudo"):
        return ["sudo", "-n"] + list(argv)
    return list(argv)


def fix_jail_start(args):
    n = args.get("name")
    if not valid_jail(n):
        return False, f"unknown jail: {n!r}"
    rc, out = sh(["bastille", "start", n], timeout=120)
    return rc == 0, out or f"started {n}"


def fix_jail_restart(args):
    n = args.get("name")
    if not valid_jail(n):
        return False, f"unknown jail: {n!r}"
    # Restarting the vpn hub alone strips the confined jails' route — route to
    # the group restart instead (mirrors the Linux netns-hub special case).
    if n == _GROUP_HUB:
        return fix_vpn_recreate({})
    rc, out = sh(["bastille", "restart", n], timeout=180)
    return rc == 0, out or f"restarted {n}"


def fix_vpn_recreate(args):
    """Restart the vpn jail and every jail confined to it, hub first, so the
    tunnel is up before the members re-establish their default route."""
    present = known_jails()
    group = [c for c in [_GROUP_HUB] + GROUP_MEMBERS if c in present]
    outs = []
    for j in group:
        rc, out = sh(["bastille", "restart", j], timeout=180)
        outs.append(f"{j}: {'ok' if rc == 0 else out[:120]}")
    return True, "restarted: " + ", ".join(outs)


def _restart_tunnel(jail):
    """Restart the WireGuard tunnel inside `jail`, returning (ok, output).

    `service wireguard restart` cannot simply be run under capture_output:
    wg-quick's monitor_daemon is backgrounded by `up`, inherits the output
    pipe and never lets it see EOF, so the wrapping read blocks until a
    timeout -- the exact trap lib/steps.py works around at install time
    (same class as the daemon(8) hang). Background the launch with all
    output discarded, then confirm reality by polling `wg show wg0` inside
    the jail instead of trusting the launcher's exit."""
    sh(["sh", "-c",
        f"(bastille cmd {jail} service wireguard restart) "
        "< /dev/null > /dev/null 2>&1 &"], timeout=30)
    for _ in range(15):
        rc, out = sh(["bastille", "cmd", jail, "wg", "show", "wg0"], timeout=15)
        if rc == 0 and out.strip():
            return True, out
        time.sleep(2)
    return False, "wg0 did not come up after restart"


def fix_vpn_rotate(args):
    """Restart the WireGuard tunnel to force a fresh handshake. There is no
    rotation script in this repo -- "rotate" means: bounce the tunnel and
    let it re-handshake against whatever Endpoint wg0.conf currently names
    (edit the config or use Change VPN provider for a different exit)."""
    ok, out = _restart_tunnel(_GROUP_HUB)
    if not ok:
        return False, out
    ip = _exit_ip()
    return True, out[-200:] + (f" -- exit IP now {ip}" if ip else "")


_WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_WG_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9.\-]+:[0-9]{1,5}$")


def _parse_wg_conf(text):
    """[Interface]/[Peer] sections both flatten into one dict -- their key
    names never collide (PrivateKey only lives in Interface, PublicKey/
    Endpoint only in Peer), so a real section-aware parser isn't needed."""
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        fields[k.strip().lower()] = v.strip()
    return fields


def set_vpn_config(raw_text):
    """Paste-a-whole-.conf VPN provider switch, for the dashboard's "Change
    VPN provider" button. Parses what a WireGuard config from any real
    provider (ProtonVPN, Windscribe, Surfshark, ...) actually looks like --
    the person just downloads one and pastes the file, no manual field
    splitting -- validates the load-bearing fields, writes it into the vpn
    jail, and restarts the tunnel."""
    if not isinstance(raw_text, str) or len(raw_text) > 8192:
        return False, "config missing or too large"
    fields = _parse_wg_conf(raw_text)
    private_key = fields.get("privatekey", "")
    public_key = fields.get("publickey", "")
    endpoint = fields.get("endpoint", "")
    if not (private_key and public_key and endpoint):
        return False, ("couldn't find PrivateKey/PublicKey/Endpoint in that "
                       "text -- paste the whole .conf file your provider gave you")
    if not _WG_KEY_RE.match(private_key):
        return False, "PrivateKey doesn't look like a WireGuard key"
    if not _WG_KEY_RE.match(public_key):
        return False, "PublicKey doesn't look like a WireGuard key"
    if not _WG_ENDPOINT_RE.match(endpoint):
        return False, f"Endpoint should be host:port, got: {endpoint!r}"

    address = fields.get("address", "10.2.0.2/32")
    dns = fields.get("dns", "10.2.0.1")
    allowed_ips = fields.get("allowedips", "0.0.0.0/0, ::/0")
    keepalive = fields.get("persistentkeepalive", "25")

    conf_path = Path(f"/usr/local/bastille/jails/{_GROUP_HUB}/root"
                     "/usr/local/etc/wireguard/wg0.conf")
    if not conf_path.parent.is_dir():
        return False, (f"{conf_path.parent} not found -- is the vpn jail "
                       "created yet? (run the installer's VPN step first)")

    new_conf = (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {address}\n"
        f"DNS = {dns}\n"
        # Table left at wg-quick's default ("auto"), matching
        # vpn/wg0.conf.tmpl: with AllowedIPs=0.0.0.0/0 that's what makes
        # wg-quick install the /1 most-specific routes that actually carry
        # the confined jails' traffic into wg0. (An earlier version set
        # Table=off here, which silently blackholed them once the
        # host-side kill-switch became the only pf enforcement point.)
        "MTU = 1420\n\n"
        "[Peer]\n"
        f"PublicKey = {public_key}\n"
        f"Endpoint = {endpoint}\n"
        f"AllowedIPs = {allowed_ips}\n"
        f"PersistentKeepalive = {keepalive}\n"
    )
    if conf_path.exists():
        conf_path.with_name("wg0.conf.bak").write_text(conf_path.read_text())
    conf_path.write_text(new_conf)
    conf_path.chmod(0o600)

    sh(["bastille", "sysrc", _GROUP_HUB, "wireguard_enable=YES",
       "wireguard_interfaces=wg0"], timeout=30)
    ok, out = _restart_tunnel(_GROUP_HUB)
    if not ok:
        return False, f"config saved but the tunnel did not come up: {out}"
    return True, "wireguard tunnel restarted with the new config"


_SCHEDULE_FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
_TIME_RE = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")


def _cron_expr(frequency, hh, mm):
    if frequency == "weekly":
        return f"{mm} {hh} * * 0"
    if frequency == "monthly":
        return f"{mm} {hh} 1 * *"
    if frequency == "yearly":
        return f"{mm} {hh} 1 1 *"
    return f"{mm} {hh} * * *"


def _crontab_set_tag(entries, tag):
    """Same BEGIN/END tag-block replace as lib/platform.py's crontab_user()
    -- duplicated here since this script runs standalone with no import path
    back into lib/. Runs as root already (no sudo needed, unlike the
    installer)."""
    rc, out = sh(["crontab", "-l"], timeout=15)
    existing = out.splitlines() if rc == 0 else []
    begin, end = f"# freebsd-media-setup:{tag} BEGIN", f"# freebsd-media-setup:{tag} END"
    kept, skipping = [], False
    for l in existing:
        if l.strip() == begin:
            skipping = True; continue
        if l.strip() == end:
            skipping = False; continue
        if not skipping:
            kept.append(l)
    if entries:
        kept += [begin, *entries, end]
    new = "\n".join(kept).strip("\n") + "\n"
    tmp = Path(f"/tmp/.crontab-root-{os.getpid()}")
    tmp.write_text(new)
    try:
        rc, out = sh(["crontab", str(tmp)], timeout=15)
        return rc == 0, out
    finally:
        tmp.unlink(missing_ok=True)


def set_daily_routine_schedule(frequency, time_str):
    """Live schedule change for the nightly self-heal routine, from the
    dashboard -- rewrites the SAME cron tag the installer's own
    install_daily_routine step uses, so a later re-run of the installer
    won't fight this change or duplicate the job."""
    if frequency not in _SCHEDULE_FREQUENCIES:
        return False, f"unknown frequency {frequency!r}"
    m = _TIME_RE.match(time_str or "")
    if not m:
        return False, "time must be HH:MM (24h)"
    hh, mm = m.group(1), m.group(2)
    script = _HOME / ".hermes/scripts/daily-routine.sh"
    if not script.exists():
        return False, f"{script} not found -- is the daily routine installed?"
    expr = _cron_expr(frequency, hh, mm)
    ok, out = _crontab_set_tag([f"{expr} {script} >/dev/null 2>&1"], "daily-routine")
    if not ok:
        return False, out or "crontab install failed"
    # Publish the effective schedule to the user-readable conf dir: the
    # authoritative tag block lives in ROOT's crontab (mode 600), which the
    # user-run dashboard collector can never read (see status-collect.py's
    # _read_daily_routine_schedule).
    try:
        sched_file = _HOME / ".config/status-dashboard/routine-schedule.json"
        sched_file.parent.mkdir(parents=True, exist_ok=True)
        sched_file.write_text(json.dumps(
            {"frequency": frequency,
             "time": f"{int(hh):02d}:{int(mm):02d}"}) + "\n")
        sched_file.chmod(0o644)
        try:
            pw_entry = pwd.getpwnam(_DESKTOP_USER)
            os.chown(sched_file, pw_entry.pw_uid, pw_entry.pw_gid)
        except KeyError:
            pass
    except OSError:
        pass
    return True, f"scheduled {frequency} at {hh}:{mm} (cron: {expr})"


def fix_prowlarr_testall(args):
    req = urllib.request.Request(f"{PROWLARR['url']}/api/v1/indexer/testall",
                                 data=b"", method="POST",
                                 headers={"X-Api-Key": PROWLARR["key"]})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    try:
        data = json.loads(body or "[]")
    except Exception:
        return False, f"unparseable response: {body[:300]}"
    names = {}
    try:
        nreq = urllib.request.Request(f"{PROWLARR['url']}/api/v1/indexer",
                                      headers={"X-Api-Key": PROWLARR["key"]})
        with urllib.request.urlopen(nreq, timeout=60) as r:
            names = {i["id"]: i.get("name", str(i["id"])) for i in json.loads(r.read().decode())}
    except Exception:
        pass
    bad = []
    for x in data:
        if x.get("isValid"):
            continue
        why = "; ".join(f.get("errorMessage", "") for f in x.get("validationFailures", []))
        bad.append(f"{names.get(x.get('id'), x.get('id'))}: {why[:120]}")
    if bad:
        return False, f"{len(bad)} of {len(data)} indexers still failing:\n  " + "\n  ".join(bad)
    return True, f"all {len(data)} indexers passed"


def _exit_ip():
    rc, out = sh(["bastille", "cmd", _GROUP_HUB, "fetch", "-qo", "-",
                  "https://api.ipify.org"], timeout=30)
    ips = re.findall(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", out)
    return ips[-1] if ips else None


def fix_cloudflare_unblock(args):
    host = args.get("host") or ""
    if host and not re.fullmatch(r"[A-Za-z0-9.\-]{3,120}", host):
        return False, f"refusing suspicious host: {host!r}"
    log = [f"exit IP before: {_exit_ip() or 'unknown'}"]
    for attempt in range(1, 4):
        ok, out = fix_vpn_rotate({})
        if not ok:
            log.append(f"attempt {attempt}: rotation failed — {out[:200]}"); break
        time.sleep(12)
        log.append(f"attempt {attempt}: exit IP now {_exit_ip() or 'unknown'}")
        if not host:
            break
        rc2, body = sh(["bastille", "cmd", "qbittorrent", "sh", "-c",
                        f"fetch -qo - -T15 https://{host}/ 2>&1 | head -3"], timeout=60)
        if "403" not in body:
            log.append(f"{host} no longer 403 — unblocked"); break
        log.append(f"{host} still 403 on this IP")
    else:
        log.append("exhausted 3 rotations; the whole provider range looks banned")
    ok_test, test_out = fix_prowlarr_testall({})
    log.append(test_out)
    return ok_test, "\n".join(log)


def fix_pkg_upgrade(args):
    """Upgrade the host, then each managed jail (bastille pkg upgrade). Read-only
    to media; never removes packages. Runs as root (see _root_argv)."""
    rc, out = sh(_root_argv(["env", "ASSUME_ALWAYS_YES=YES", "pkg", "upgrade", "-y"]),
                 timeout=1800)
    return rc == 0, out[-3500:] or "pkg upgrade complete"


def fix_daily_routine_quick(args):
    # Not under BIN: install_daily_routine deploys this to ~/.hermes/scripts,
    # not ~/.local/bin -- it predates the dashboard and was never moved.
    script = _HOME / ".hermes/scripts/daily-routine.sh"
    if not script.exists():
        return False, "daily-routine.sh not found"
    rc, out = sh(["sh", str(script), "--quick"], timeout=1800)
    return rc == 0, out[-4000:] or "daily routine (quick) complete"


_DISK_RE = re.compile(r"^(da|ada|nvd|nda)[0-9]+$")
_HOTPLUG_MARKERS = Path("/var/db/freebsd-media-setup/hotplug-disks")


def fix_pool_add_drive(args):
    """Claim a devd-flagged disk into the 'storage' pool (creating it first
    if this is the very first drive added post-install). Whitelisted name
    shape + a live re-check against zpool status -- the disk name comes from
    the dashboard page, never trust it blind."""
    disk = args.get("disk", "")
    if not _DISK_RE.fullmatch(disk):
        return False, f"refusing suspicious disk name: {disk!r}"
    _, status_out = sh(["zpool", "status"], timeout=30)
    if re.search(rf"^\s*{re.escape(disk)}\s", status_out, re.M):
        return False, f"{disk} is already a pool member, refusing"
    if sh(["zpool", "list", "storage"], timeout=15)[0] != 0:
        rc, out = sh(["zpool", "create", "-m", "/mnt/storage", "storage", disk],
                    timeout=60)
    else:
        rc, out = sh(["zpool", "add", "storage", disk], timeout=60)
    if rc == 0:
        (_HOTPLUG_MARKERS / disk).unlink(missing_ok=True)
    return rc == 0, out or f"added {disk} to storage pool"


def fix_restart_service(args):
    """Restart a base/rc service on the host. Whitelisted name shape only."""
    name = args.get("name", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        return False, f"refusing suspicious service name: {name!r}"
    rc, out = sh(_root_argv(["service", name, "restart"]), timeout=120)
    return rc == 0, out or f"restarted {name}"


FIXES = {
    "jail_start": fix_jail_start,
    "jail_restart": fix_jail_restart,
    "vpn_recreate": fix_vpn_recreate,
    "vpn_rotate": fix_vpn_rotate,
    "prowlarr_testall": fix_prowlarr_testall,
    "cloudflare_unblock": fix_cloudflare_unblock,
    "pkg_upgrade": fix_pkg_upgrade,
    "daily_routine_quick": fix_daily_routine_quick,
    "restart_service": fix_restart_service,
    "pool_add_drive": fix_pool_add_drive,
}


def run_fix(fix_id, args):
    handler = FIXES.get(fix_id)
    if handler is None:
        return {"id": fix_id, "ok": False, "output": f"unknown fix id: {fix_id}"}
    t0 = time.time()
    try:
        ok, output = handler(args or {})
    except Exception as e:  # noqa: BLE001
        ok, output = False, f"{type(e).__name__}: {e}"
    return {"id": fix_id, "ok": bool(ok), "output": output, "seconds": round(time.time() - t0, 1)}


# ── terminal panes ───────────────────────────────────────────────────────────
# Full parity with the Linux edition. "needs" is the command that must exist for
# the entry to be listed (so a box without ollama simply doesn't show that pane).
# "group" drives the picker's sections; for paid/free it names the env key in
# model-pricing.json (written nightly by ai-panes-check.py) that prices it.
# System panes retargeted to FreeBSD: docker->jails, journalctl->tail, bash shell.
PANES = [
    ("claude",  "Claude Code",     "claude",   "agent",  None),
    ("opencode", "opencode",       "opencode", "agent",  None),
    ("gpt",     "ChatGPT",         "curl",     "paid",   "CHATGPT_MODEL"),
    ("gm",      "Gemini",          "curl",     "paid",   "GEMINI_MODEL"),
    ("hy",      "Hy4",             "curl",     "paid",   "HY4_MODEL"),
    ("oa",      "Ox Alpha",        "curl",     "free",   "OXALPHA_MODEL"),
    ("ds",      "DeepSeek",        "curl",     "free",   "DEEPSEEK_MODEL"),
    ("mm",      "Minimax M3",      "curl",     "free",   "MINIMAX_MODEL"),
    ("llm",     "Local LLM",       "ollama",   "local",  None),
    ("glm",     "GLM (local)",     "ollama",   "local",  None),
    ("ask",     "Ask Claude",      "claude",   "hidden", None),
    ("askllm",  "Ask Local LLM",   "ollama",   "hidden", None),
    ("askglm",  "Ask GLM (local)", "ollama",   "hidden", None),
    ("askgpt",  "Ask ChatGPT",     "curl",     "hidden", "CHATGPT_MODEL"),
    ("askgm",   "Ask Gemini",      "curl",     "hidden", "GEMINI_MODEL"),
    ("askhy",   "Ask Hy4",         "curl",     "hidden", "HY4_MODEL"),
    ("askoa",   "Ask Ox Alpha",    "curl",     "hidden", "OXALPHA_MODEL"),
    ("askds",   "Ask DeepSeek",    "curl",     "hidden", "DEEPSEEK_MODEL"),
    ("askmm",   "Ask Minimax M3",  "curl",     "hidden", "MINIMAX_MODEL"),
    ("shell",   "Shell",           "bash",     "system", None),
    ("htop",    "Processes",       "htop",     "system", None),
    ("jails",   "Jail stats",      "jls",      "system", None),
    ("logs",    "System log",      "tail",     "system", None),
    ("dashlog", "Dashboard log",   "tail",     "system", None),
    ("disk",    "Disk usage",      "ncdu",     "system", None),
    ("routine", "Daily routine",   "less",     "system", None),
]

PRICING_FILE = _HOME / ".config/status-dashboard/model-pricing.json"
# Written by the installer only when Local AI (GLM) was chosen -- the GLM
# panes stay hidden elsewhere so a stray click can't start a 19 GB download.
LOCAL_AI_FILE = _HOME / ".config/status-dashboard/local-ai.env"
GLM_PANES = ("glm", "askglm")


def pane_enabled(pid, needs):
    if pid in GLM_PANES and not LOCAL_AI_FILE.exists():
        return False
    return bool(shutil.which(needs))


def read_pricing():
    try:
        return json.loads(PRICING_FILE.read_text())
    except Exception:  # noqa: BLE001 — missing/stale file just means no price shown
        return {}


def format_price(entry):
    if not entry:
        return None
    if entry.get("free"):
        return "free"
    p, c = entry.get("prompt"), entry.get("completion")
    if p is None or c is None:
        return None
    return f"${p * 1e6:.2f}/${c * 1e6:.2f} per M tokens"


def available_panes():
    pricing = read_pricing()
    out = []
    for pid, label, needs, group, price_key in PANES:
        if group == "hidden" or not pane_enabled(pid, needs):
            continue
        entry = pricing.get(price_key) if price_key else None
        # Let live pricing override the static free/paid label: ai-panes-check.py
        # may have downgraded a free tier to a cheap paid variant overnight.
        eff_group = group
        if group in ("free", "paid") and entry is not None and "free" in entry:
            eff_group = "free" if entry["free"] else "paid"
        out.append({"id": pid, "label": label, "group": eff_group,
                    "price": format_price(entry)})
    return out


def ask_targets():
    pricing = read_pricing()
    out = []
    for pid, label, needs, group, price_key in PANES:
        if group != "hidden" or not pid.startswith("ask") or not pane_enabled(pid, needs):
            continue
        price = format_price(pricing.get(price_key)) if price_key else None
        out.append({"id": pid, "label": label.removeprefix("Ask "), "price": price})
    return out


# ── http ─────────────────────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *a)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _same_origin(self):
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site != "cross-site"
        origin = self.headers.get("Origin")
        if origin:
            return origin in (f"http://{BIND}:{PORT}", f"http://localhost:{PORT}")
        return True

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/job":
            with _job_lock:
                return self._json(200, dict(_job))
        if route == "/api/panes":
            return self._json(200, {"panes": available_panes()})
        if route == "/api/ask-targets":
            return self._json(200, {"targets": ask_targets()})
        if route.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        return super().do_GET()

    def do_POST(self):
        route = urlparse(self.path).path
        if not route.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin requests are refused"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad content-length"})
        if n > MAX_BODY:
            return self._json(413, {"error": "request body too large"})
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        if route == "/api/reboot":
            if body.get("confirm") != "REBOOT":
                return self._json(400, {"ok": False, "error": "missing confirmation"})
            def go():
                time.sleep(2)
                sh(_root_argv(["shutdown", "-r", "now"]), timeout=30)
            threading.Thread(target=go, daemon=True).start()
            return self._json(200, {"ok": True, "output": "reboot scheduled in 2s"})

        if route == "/api/refresh":
            rc, out = recollect()
            return self._json(200, {"ok": rc == 0, "output": out[-500:]})

        if route == "/api/open":
            ok, msg = open_link(body.get("id", ""))
            return self._json(200 if ok else 400, {"ok": ok, "output": msg})

        if route == "/api/vpn-config":
            ok, msg = set_vpn_config(body.get("config", ""))
            return self._json(200 if ok else 400, {"ok": ok, "output": msg})

        if route == "/api/schedule":
            ok, msg = set_daily_routine_schedule(body.get("frequency", ""),
                                                 body.get("time", ""))
            return self._json(200 if ok else 400, {"ok": ok, "output": msg})

        if route != "/api/fix":
            return self._json(404, {"error": "not found"})

        items = body.get("fixes")
        if items is None:
            items = [{"id": body.get("id"), "args": body.get("args", {})}]
        if not isinstance(items, list) or len(items) > 25:
            return self._json(400, {"error": "bad fix list"})

        seen, todo = set(), []
        for it in items:
            fid = (it or {}).get("id")
            args = (it or {}).get("args") or {}
            key = (fid, json.dumps(args, sort_keys=True))
            if key in seen:
                continue
            seen.add(key); todo.append((fid, args))

        if not _lock.acquire(blocking=False):
            return self._json(409, {"error": f"a repair is already running ({_running['id']})"})

        with _job_lock:
            _job.update({"running": True, "done": False, "results": [],
                         "total": len(todo), "started": time.time(),
                         "current": None, "error": None})

        def worker():
            try:
                for fid, args in todo:
                    with _job_lock:
                        _job["current"] = fid
                    _running.update({"id": fid, "since": time.time()})
                    r = run_fix(fid, args)
                    with _job_lock:
                        _job["results"].append(r)
                _running.update({"id": "recollect", "since": time.time()})
                recollect()
            except Exception as e:  # noqa: BLE001
                with _job_lock:
                    _job["error"] = f"{type(e).__name__}: {e}"
            finally:
                _running.update({"id": None, "since": 0})
                with _job_lock:
                    _job.update({"running": False, "done": True, "current": None})
                _lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return self._json(202, {"started": True, "total": len(todo)})


def main():
    os.chdir(ROOT)
    srv = ThreadingHTTPServer((BIND, PORT), partial(Handler, directory=str(ROOT)))
    print(f"status dashboard on http://{BIND}:{PORT} (root {ROOT})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
