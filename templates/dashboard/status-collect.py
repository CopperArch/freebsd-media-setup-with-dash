#!/usr/bin/env python3.11
"""
status-collect.py — FreeBSD edition. Gathers host + jail state into one JSON
blob for the desktop status dashboard, emitting the SAME schema the Linux
edition's index.html consumes so the collector side needs no schema changes
(the dashboard's "Jails (containers)" panel shows Bastille jails under this
schema's "docker" key -- see collect_docker() below).

Retargeted sources vs. the Linux edition:
  /proc/*            -> sysctl (kern.cp_time(s), vm.stats, kern.boottime, hw.*)
  lm-sensors         -> sysctl dev.cpu.N.temperature / hw.acpi.thermal
  docker ps/inspect  -> jls + bastille list/cmd (state, per-service health)
  gluetun logs       -> wg handshake + exit-IP probe from inside the vpn jail
  df --output/-B1    -> df -k (portable) x1024
  systemctl --failed -> rc services enabled-but-not-running
  apt -s upgrade     -> pkg upgrade -n  (+ pkg audit for the "security" count)

Run every 60s from the status-collect rc/cron job.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from status_keys import load_env, load_group, jail_ip

OUT_DIR = Path.home() / ".local/share/status-dashboard"
OUT_FILE = OUT_DIR / "status.json"
LOG_DIR = Path.home() / ".hermes/maintenance-logs"

EXPECTED_VPN_COUNTRY = "Netherlands"
DAILY_ROUTINE_NEXT_RUN = "03:00 daily"
INTERESTING_MOUNTS = {"/", "{{MEDIA_POOL}}", "{{NEXTCLOUD_DATA}}",
                      "/var", "/usr", "/boot", "/tmp"}

# Managed jails and the port each app's WebUI/API listens on inside its jail.
# The collector runs on the host and reaches jails on the bastille0 bridge by
# IP (subnet.N), so probes target the jail IP, not localhost.
JAILS = ["jellyfin", "plex", "prowlarr", "bazarr", "seerr", "caddy", "nextcloud", "nextcloud-db",
         "vpn", "qbittorrent", "sonarr", "radarr"]
PORTS = {"jellyfin": 8096, "plex": 32400, "prowlarr": 9696, "bazarr": 6767,
         "seerr": 5055, "caddy": 80, "nextcloud": 80, "qbittorrent": 8080, "sonarr": 8989,
         "radarr": 7878}

KEYS = load_env()
def _url(jail, path=""):
    return f"http://{jail_ip(jail)}:{PORTS[jail]}{path}"

QBIT = {"url": _url("qbittorrent"), "user": KEYS.get("QBIT_USER", "admin"),
        "pass": KEYS.get("QBIT_PASS", "")}
RADARR = {"url": _url("radarr"), "key": KEYS.get("RADARR_KEY", "")}
SONARR = {"url": _url("sonarr"), "key": KEYS.get("SONARR_KEY", "")}
PROWLARR = {"url": _url("prowlarr"), "key": KEYS.get("PROWLARR_KEY", "")}
JELLYFIN = {"url": _url("jellyfin"), "key": KEYS.get("JELLYFIN_KEY", "")}
PLEX = {"url": _url("plex"), "token": None}

SERVICE_PROBES = [
    ("qBittorrent", _url("qbittorrent"), (200, 401, 403)),
    ("Prowlarr", _url("prowlarr"), (200, 401)),
    ("Radarr", _url("radarr"), (200, 401)),
    ("Sonarr", _url("sonarr"), (200, 401)),
    ("Seerr", _url("seerr"), (200, 307, 302)),
    ("Plex", _url("plex", "/identity"), (200,)),
    ("Jellyfin", _url("jellyfin", "/health"), (200,)),
    ("Bazarr", _url("bazarr"), (200, 401, 302)),
    ("Caddy", _url("caddy"), (200, 301, 302, 308, 404, 502)),
    ("Nextcloud", _url("nextcloud", "/status.php"), (200,)),
]

# The VPN "hub" jail and the jails confined to its tunnel (was gluetun netns).
GROUP_HUB, GROUP_MEMBERS = load_group()

TIMEOUT = 8


# ── helpers ──────────────────────────────────────────────────────────────────
def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout
    except Exception:
        return ""


def sysctl(name, default=""):
    out = run(["sysctl", "-n", name], timeout=6).strip()
    return out or default


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # tell urlopen not to follow -- see http()'s follow_redirects


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def http(url, headers=None, timeout=TIMEOUT, data=None, method=None,
         follow_redirects=True):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urllib.request.urlopen if follow_redirects else _no_redirect_opener.open
    try:
        with opener(req, timeout=timeout) as r:
            return r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception:
        return None, ""


def http_json(url, headers=None, timeout=TIMEOUT):
    code, body = http(url, headers, timeout)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def guard(name, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def human(n, unit=1024):
    n = float(n or 0)
    for s in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < unit:
            return f"{n:.0f}{s}" if s in ("B", "K") else f"{n:.1f}{s}"
        n /= unit
    return f"{n:.1f}E"


def jexec(jail, argv, timeout=15):
    """Run a command INSIDE a jail (the docker-exec replacement)."""
    return run(["bastille", "cmd", jail] + argv, timeout=timeout)


# ── host ─────────────────────────────────────────────────────────────────────
def cpu_sample():
    """Busy% total + per-core from kern.cp_time / kern.cp_times.
    FreeBSD cp_time columns: user nice sys intr idle."""
    def agg():
        vals = [int(x) for x in sysctl("kern.cp_time").split()]
        if len(vals) < 5:
            return None
        return sum(vals), vals[4]  # total, idle

    def per_core():
        raw = [int(x) for x in sysctl("kern.cp_times").split()]
        cores = []
        for i in range(0, len(raw) - 4, 5):
            c = raw[i:i + 5]
            cores.append((sum(c), c[4]))
        return cores

    a_t, a_c = agg(), per_core()
    time.sleep(0.4)
    b_t, b_c = agg(), per_core()

    def busy(a, b):
        dt = b[0] - a[0]; di = b[1] - a[1]
        return round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0

    total = busy(a_t, b_t) if a_t and b_t else 0.0
    cores = [busy(a_c[i], b_c[i]) for i in range(min(len(a_c), len(b_c)))]
    return {"total": total, "cores": cores}


def collect_sensors():
    """Temps from sysctl (coretemp/amdtemp expose dev.cpu.N.temperature;
    ACPI exposes hw.acpi.thermal.tzN.temperature). Values are like '54.0C'."""
    temps, fans = [], []
    raw = run(["sysctl", "-a"], timeout=10)
    for line in raw.splitlines():
        m = re.match(r"(dev\.cpu\.(\d+)\.temperature|hw\.acpi\.thermal\.tz\d+\.temperature):\s*([\d.]+)C", line)
        if m:
            label = ("cpu%s" % m.group(2)) if m.group(2) else "acpi"
            temps.append({"chip": "cpu", "label": label,
                          "value": round(float(m.group(3)), 1), "crit": None})
    return {"temps": temps, "fans": fans}


def collect_host():
    pagesize = int(sysctl("hw.pagesize", "4096"))
    total = int(sysctl("hw.physmem", "0"))
    free = int(sysctl("vm.stats.vm.v_free_count", "0")) * pagesize
    inactive = int(sysctl("vm.stats.vm.v_inactive_count", "0")) * pagesize
    cache = int(sysctl("vm.stats.vm.v_cache_count", "0")) * pagesize
    wired = int(sysctl("vm.stats.vm.v_wire_count", "0")) * pagesize
    avail = free + inactive + cache
    used = total - avail

    # boottime -> uptime
    bt = sysctl("kern.boottime")
    m = re.search(r"sec = (\d+)", bt)
    boot_epoch = int(m.group(1)) if m else int(time.time())
    uptime = max(0, int(time.time()) - boot_epoch)

    load = os.getloadavg()
    ncpu = int(sysctl("hw.ncpu", str(os.cpu_count() or 1)))

    # reboot needed when the installed kernel differs from the running one
    running_k = sysctl("kern.osrelease") or run(["uname", "-r"]).strip()
    installed_k = run(["freebsd-version", "-k"]).strip()
    reboot_required = bool(installed_k and running_k and
                           installed_k.split("-p")[0] != running_k.split("-p")[0]
                           or (installed_k and installed_k != running_k))

    # rc services that are enabled but not currently running
    failed = []
    for svc in ("bastille", "pf", "sddm", "sshd", "ntpd"):
        st = run(["service", svc, "status"], timeout=10)
        if st and re.search(r"is not running|not running", st, re.I):
            failed.append(svc)

    # pending pkg upgrades (offline plan) + vulnerable pkgs as "security"
    upg = run(["pkg", "upgrade", "-n"], timeout=40)
    pend = re.search(r"(\d+)\s+package[s]?\s+will be (?:upgraded|affected)", upg)
    pending = int(pend.group(1)) if pend else (
        len(re.findall(r"^\s+\S+:\s+\S+\s+->\s+\S+", upg, re.M)))
    audit = run(["pkg", "audit", "-q"], timeout=40)
    security = len([l for l in audit.splitlines() if l.strip()])

    return {
        "hostname": socket.gethostname(),
        "kernel": running_k,
        "os": f"FreeBSD {run(['freebsd-version', '-u']).strip() or running_k}",
        "uptime_seconds": uptime,
        "booted_at": datetime.fromtimestamp(boot_epoch).isoformat(timespec="seconds"),
        "load": {"1": load[0], "5": load[1], "15": load[2], "ncpu": ncpu},
        "cpu": {"model": sysctl("hw.model"), "cores": ncpu, **cpu_sample()},
        "memory": {
            "total": total, "used": used, "available": avail,
            "cached": cache, "buffers": wired,
            "percent": round(100.0 * used / total, 1) if total else 0,
            "swap_total": 0, "swap_used": 0,
        },
        "sensors": collect_sensors(),
        "reboot_required": reboot_required,
        "reboot_packages": [],
        "failed_units": failed,
        "updates_pending": pending,
        "updates_security": security,
        "update_packages": [],
    }


# ── disks (df -k, portable) ──────────────────────────────────────────────────
def collect_disks():
    out, seen = [], set()
    raw = run(["df", "-k"], timeout=15)
    for line in raw.splitlines()[1:]:
        f = line.split()
        if len(f) < 6:
            continue
        src, size, used, avail, cap, target = f[0], f[1], f[2], f[3], f[4], f[5]
        if src in ("devfs", "tmpfs", "fdescfs", "procfs", "linprocfs"):
            continue
        if target not in INTERESTING_MOUNTS and not target.startswith("{{MEDIA_POOL}}"):
            continue
        if target in seen:
            continue
        seen.add(target)
        try:
            size_i = int(size) * 1024; used_i = int(used) * 1024
            avail_i = int(avail) * 1024
        except ValueError:
            continue
        out.append({"mount": target, "source": src,
                    "fstype": "zfs" if ":" not in src and "/" in src else "zfs",
                    "size": size_i, "used": used_i, "avail": avail_i,
                    "percent": float(cap.rstrip("%") or 0),
                    "pool_member": target.startswith("{{MEDIA_POOL}}")})
    out.sort(key=lambda d: (d["pool_member"], d["mount"]))
    return {"filesystems": out}


# ── hotplug drives (devd-flagged, not yet in any pool) ──────────────────────
HOTPLUG_DIR = Path("/var/db/freebsd-media-setup/hotplug-disks")


def collect_hotplug_drives():
    """One marker file per disk devd saw attached since it was last claimed
    (see templates/scripts/drive-hotplug.sh). Re-checks zpool membership here
    too -- belt-and-suspenders in case a marker survived a pool-add that
    didn't get to clean up after itself."""
    if not HOTPLUG_DIR.is_dir():
        return []
    claimed = set(run(["zpool", "status"], timeout=15).split())
    names = []
    for f in sorted(HOTPLUG_DIR.iterdir()):
        if f.is_file() and f.name not in claimed:
            names.append(f.name)
    return names


# ── jails (the docker collector, retargeted) ────────────────────────────────
def _running_jails():
    """Names of currently-running jails (jls is authoritative)."""
    raw = run(["jls", "-N", "name"], timeout=15) or run(["jls", "name"], timeout=15)
    return set(raw.split())


def collect_docker():
    """Emitted under the key 'docker' so index.html renders it unchanged; the
    contents are Bastille jails. state=running|exited, health from the jail's
    primary service, image=the app it runs."""
    running = _running_jails()
    containers = []
    for j in JAILS:
        up = j in running
        # primary service health: ask rc inside the jail
        health = ""
        svc = {"plex": "plexmediaserver_plexpass"}.get(j, j)
        if up and j != "vpn":
            st = jexec(j, ["service", svc, "status"], timeout=12)
            if st and re.search(r"is running", st):
                health = "healthy"
            elif st and re.search(r"not running", st, re.I):
                health = "unhealthy"
        # uptime-ish: jail start not tracked; show state text
        containers.append({
            "name": j, "state": "running" if up else "exited",
            "status": "up" if up else "stopped",
            "image": j, "since": "", "health": health,
            "restarts": 0, "cpu": None, "mem": None, "mem_pct": None,
        })
    lst = sorted(containers, key=lambda c: c["name"])
    return {
        "containers": lst,
        "total": len(lst),
        "running": len([c for c in lst if c["state"] == "running"]),
        "unhealthy": [c["name"] for c in lst if c["health"] == "unhealthy"],
        "not_running": [c["name"] for c in lst if c["state"] != "running"],
        "restart_loops": [],
    }


# ── vpn (wg tunnel + confined-jail routing) ─────────────────────────────────
def collect_vpn():
    """Schema-compatible with the page: healthy/siblings_ok/ip/country/siblings.
    'healthy' == 'healthy' when the tunnel's exit IP differs from the host WAN.
    'siblings' are the confined jails; 'bound' means their default route is the
    vpn jail (the FreeBSD analogue of sharing gluetun's netns)."""
    info = {"ip": None, "country": None, "city": None, "healthy": None,
            "siblings": [], "siblings_ok": True, "configured": True}

    if GROUP_HUB not in _running_jails():
        info["healthy"] = "hub down"
        info["siblings_ok"] = False
        return info

    info["configured"] = Path(f"/usr/local/bastille/jails/{GROUP_HUB}/root"
                              "/usr/local/etc/wireguard/wg0.conf").exists()

    # wg handshake present?
    wg = jexec(GROUP_HUB, ["wg", "show"], timeout=12)
    handshake = "latest handshake" in wg and "0 seconds ago" not in wg.split("latest handshake")[-1][:0]
    exit_ip = jexec(GROUP_HUB, ["fetch", "-qo", "-", "https://api.ipify.org"], timeout=15).strip()
    host_ip = run(["fetch", "-qo", "-", "https://api.ipify.org"], timeout=15).strip()
    info["ip"] = exit_ip or None
    info["healthy"] = "healthy" if (exit_ip and exit_ip != host_ip) else "unhealthy"

    # country lookup (best-effort, through the tunnel)
    geo = jexec(GROUP_HUB, ["fetch", "-qo", "-",
                            f"https://ipapi.co/{exit_ip}/country_name/"], timeout=12).strip()
    info["country"] = geo or None

    # confined jails: default route must be the vpn jail IP
    vpn_ip = jail_ip(GROUP_HUB)
    for name in GROUP_MEMBERS:
        dr = run(["bastille", "sysrc", name, "-n", "defaultrouter"], timeout=10).strip()
        # `bastille sysrc -n` prints the value; fall back to jexec route -n get
        bound = (vpn_ip in dr) if dr else False
        info["siblings"].append({"name": name, "bound": bound,
                                 "mode": dr or "?"})
        if not bound:
            info["siblings_ok"] = False
    return info


# ── media (arr/qbit/jellyfin/plex — HTTP, carried over; endpoints=jail IPs) ─
def plex_token():
    if PLEX["token"]:
        return PLEX["token"]
    raw = jexec("plex", ["sh", "-c",
                'grep -o \'PlexOnlineToken="[^"]*"\' '
                '"/plexdata/Plex Media Server/Preferences.xml" 2>/dev/null || '
                'grep -ro \'PlexOnlineToken="[^"]*"\' /var/db/plexdata 2>/dev/null | head -1'],
                timeout=15)
    m = re.search(r'PlexOnlineToken="([^"]+)"', raw)
    PLEX["token"] = m.group(1) if m else None
    return PLEX["token"]


CF_BAN_CODES = {"1006": "IP address banned", "1007": "IP range banned",
                "1008": "ASN banned", "1015": "rate limited by Cloudflare"}


def detect_cloudflare_blocks(hours=6, live_hosts=None):
    raw = jexec("prowlarr", ["sh", "-c",
                "tail -n 4000 /var/db/prowlarr/logs/prowlarr.txt 2>/dev/null"],
                timeout=30)
    if not raw:
        return {"blocked": [], "checked": False}
    hits = {}
    for i, line in enumerate(raw.splitlines()):
        m = re.search(r"\[GET\]\s+(https?://([^/\s]+)\S*):\s*403", line, re.I)
        if not m:
            continue
        host = m.group(2)
        code = None
        for c in re.findall(r"error code:\s*(10\d\d)", line):
            code = c
        if code not in CF_BAN_CODES:
            continue
        if live_hosts is not None:
            tail = ".".join(host.split(".")[-2:])
            if not any(tail == ".".join(h.split(".")[-2:]) for h in live_hosts):
                continue
        hits[host] = {"host": host, "code": code, "reason": CF_BAN_CODES[code],
                      "url": m.group(1)[:120]}
    return {"blocked": sorted(hits.values(), key=lambda h: h["host"]), "checked": True}


def collect_media():
    out = {}

    def radarr():
        d = {}
        rh = {"X-Api-Key": RADARR["key"]}
        movies = http_json(f"{RADARR['url']}/api/v3/movie", rh, timeout=25)
        if isinstance(movies, list):
            d["radarr"] = {"movies": len(movies),
                           "with_file": len([m for m in movies if m.get("hasFile")]),
                           "missing": len([m for m in movies if m.get("monitored") and not m.get("hasFile")]),
                           "size": sum(m.get("sizeOnDisk", 0) or 0 for m in movies)}
        q = http_json(f"{RADARR['url']}/api/v3/queue?pageSize=200", rh, timeout=20)
        if isinstance(q, dict):
            recs = q.get("records", [])
            d.setdefault("radarr", {})["queue"] = len(recs)
            d["radarr"]["queue_warn"] = len([r for r in recs if r.get("trackedDownloadStatus") == "warning"])
        return d

    def sonarr():
        d = {}
        sh = {"X-Api-Key": SONARR["key"]}
        series = http_json(f"{SONARR['url']}/api/v3/series", sh, timeout=25)
        if isinstance(series, list):
            stats = [s.get("statistics", {}) or {} for s in series]
            d["sonarr"] = {"series": len(series),
                           "episodes": sum(s.get("episodeFileCount", 0) for s in stats),
                           "episodes_total": sum(s.get("totalEpisodeCount", 0) for s in stats),
                           "missing": sum(max(0, s.get("episodeCount", 0) - s.get("episodeFileCount", 0)) for s in stats),
                           "size": sum(s.get("sizeOnDisk", 0) or 0 for s in stats)}
        q = http_json(f"{SONARR['url']}/api/v3/queue?pageSize=200", sh, timeout=20)
        if isinstance(q, dict):
            recs = q.get("records", [])
            d.setdefault("sonarr", {})["queue"] = len(recs)
            d["sonarr"]["queue_warn"] = len([r for r in recs if r.get("trackedDownloadStatus") == "warning"])
        return d

    def prowlarr():
        d = {}
        ph = {"X-Api-Key": PROWLARR["key"]}
        idx = http_json(f"{PROWLARR['url']}/api/v1/indexer", ph, timeout=20)
        status = http_json(f"{PROWLARR['url']}/api/v1/indexerstatus", ph, timeout=20)
        live_hosts = set()
        for i in (idx if isinstance(idx, list) else []):
            for fld in i.get("fields", []):
                if fld.get("name") == "baseUrl" and fld.get("value"):
                    h = urllib.parse.urlparse(str(fld["value"])).hostname
                    if h:
                        live_hosts.add(h)
        d["cloudflare"] = detect_cloudflare_blocks(live_hosts=live_hosts or None)
        if isinstance(idx, list):
            failing = {s.get("indexerId") for s in (status or []) if s.get("disabledTill")}
            d["prowlarr"] = {"indexers": len(idx),
                             "enabled": len([i for i in idx if i.get("enable")]),
                             "failing": sorted(i.get("name", "?") for i in idx if i.get("id") in failing)}
        return d

    def qbittorrent():
        try:
            cj = __import__("http.cookiejar", fromlist=["CookieJar"]).CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            data = urllib.parse.urlencode({"username": QBIT["user"], "password": QBIT["pass"]}).encode()
            req = urllib.request.Request(f"{QBIT['url']}/api/v2/auth/login", data=data,
                                         headers={"Referer": QBIT["url"]})
            op.open(req, timeout=TIMEOUT).read()

            def qget(path):
                r = urllib.request.Request(f"{QBIT['url']}{path}", headers={"Referer": QBIT["url"]})
                return json.loads(op.open(r, timeout=TIMEOUT).read().decode())

            tr = qget("/api/v2/transfer/info")
            tor = qget("/api/v2/torrents/info")
            states = {}
            for t in tor:
                states[t["state"]] = states.get(t["state"], 0) + 1
            active = [t for t in tor if t.get("progress", 1) < 1]
            return {"qbittorrent": {
                "torrents": len(tor), "incomplete": len(active), "states": states,
                "dl_speed": tr.get("dl_info_speed", 0), "up_speed": tr.get("up_info_speed", 0),
                "connection": tr.get("connection_status", "?"), "dht": tr.get("dht_nodes", 0),
                "active": sorted([{"name": t["name"][:60], "state": t["state"],
                                   "progress": round(t.get("progress", 0) * 100, 1),
                                   "dl": t.get("dlspeed", 0), "seeds": t.get("num_seeds", 0),
                                   "eta": t.get("eta", 0)} for t in active], key=lambda x: -x["dl"])[:12]}}
        except Exception as e:  # noqa: BLE001
            return {"qbittorrent": {"error": str(e)}}

    def jellyfin():
        d = {}
        jh = {"Authorization": f'MediaBrowser Token={JELLYFIN["key"]}'}
        jf = http_json(f"{JELLYFIN['url']}/Items/Counts", jh, timeout=20)
        if isinstance(jf, dict):
            d["jellyfin"] = {"movies": jf.get("MovieCount", 0),
                             "series": jf.get("SeriesCount", 0),
                             "episodes": jf.get("EpisodeCount", 0)}
        sessions = http_json(f"{JELLYFIN['url']}/Sessions", jh, timeout=15)
        if isinstance(sessions, list):
            d.setdefault("jellyfin", {})["streams"] = len([s for s in sessions if s.get("NowPlayingItem")])
        return d

    def plex(tok):
        d = {}
        ph2 = {"X-Plex-Token": tok, "Accept": "application/json"}
        secs = http_json(f"{PLEX['url']}/library/sections", ph2, timeout=20)
        libs = []
        if isinstance(secs, dict):
            for sec in secs.get("MediaContainer", {}).get("Directory", []):
                cnt = http_json(f"{PLEX['url']}/library/sections/{sec.get('key')}/all"
                                f"?X-Plex-Container-Start=0&X-Plex-Container-Size=0", ph2, timeout=20)
                libs.append({"title": sec.get("title"), "type": sec.get("type"),
                             "count": (cnt or {}).get("MediaContainer", {}).get("totalSize")})
        ses = http_json(f"{PLEX['url']}/status/sessions", ph2, timeout=15)
        d["plex"] = {"libraries": libs, "streams": (ses or {}).get("MediaContainer", {}).get("size", 0)}
        return d

    jobs = {"radarr": radarr, "sonarr": sonarr, "prowlarr": prowlarr,
            "qbittorrent": qbittorrent, "jellyfin": jellyfin}
    tok = plex_token()
    if tok:
        jobs["plex"] = lambda: plex(tok)
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(fn): name for name, fn in jobs.items()}
        for fut in as_completed(futs):
            try:
                out.update(fut.result())
            except Exception as e:  # noqa: BLE001
                out[futs[fut]] = {"error": f"{type(e).__name__}: {e}"}
    return out


# ── services ─────────────────────────────────────────────────────────────────
def probe(item):
    label, url, ok_codes = item
    t0 = time.time()
    # Redirects must NOT be auto-followed here: several probes explicitly
    # accept a 3xx as "alive" (Caddy's HTTP->HTTPS 308, Seerr/Bazarr's login
    # redirects). Following them anyway used to mean the real response never
    # got inspected at all -- a self-signed HTTPS cert on the other end (like
    # Caddy's own local CA) fails verification and turns a perfectly healthy
    # 308 into a reported "no connection".
    code, _ = http(url, timeout=6, follow_redirects=False)
    return {"name": label, "url": url, "code": code,
            "ok": code in ok_codes, "ms": int((time.time() - t0) * 1000)}


def collect_services():
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(probe, SERVICE_PROBES))


# ── daily-routine log (unchanged parser) ─────────────────────────────────────
STATUS_RE = re.compile(r"\[(OK|WARN|FAIL|SKIP)\]\s*(.*)$")
TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]\s*(.*)$")
BANNER_RE = re.compile(r"^=+\s*(.+?)\s*=+$")


def _read_daily_routine_schedule():
    """Parse root's own crontab for the tag block status-dashboard-server.py's
    set_daily_routine_schedule() (and the installer's install_daily_routine
    step) writes -- returns (frequency, hh, mm) or None if not set up yet.
    Reading the REAL crontab instead of trusting a hardcoded constant is the
    whole point here: this used to be a fixed "03:00 daily" string that never
    reflected an actual schedule change."""
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return None
    if p.returncode != 0:
        return None
    begin, end = "# freebsd-media-setup:daily-routine BEGIN", "# freebsd-media-setup:daily-routine END"
    inside = False
    for line in p.stdout.splitlines():
        stripped = line.strip()
        if stripped == begin:
            inside = True; continue
        if stripped == end:
            inside = False; continue
        if inside and stripped and not stripped.startswith("#"):
            parts = stripped.split()
            if len(parts) < 5:
                continue
            mm, hh, dom, mon, dow = parts[:5]
            try:
                hh, mm = int(hh), int(mm)
            except ValueError:
                continue
            if dom != "*" and mon != "*":
                freq = "yearly"
            elif dom != "*":
                freq = "monthly"
            elif dow != "*":
                freq = "weekly"
            else:
                freq = "daily"
            return freq, hh, mm
    return None


def _next_run_human(frequency, hh, mm):
    now = datetime.now()
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if frequency == "weekly":
        days_ahead = (6 - now.weekday()) % 7  # cron dow=0 (Sun); Sunday is weekday() 6
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
    elif frequency == "monthly":
        candidate = candidate.replace(day=1)
        if candidate <= now:
            candidate = (candidate.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif frequency == "yearly":
        candidate = candidate.replace(month=1, day=1)
        if candidate <= now:
            candidate = candidate.replace(year=candidate.year + 1)
    else:
        if candidate <= now:
            candidate += timedelta(days=1)
    return candidate.strftime("%Y-%m-%d %H:%M") + f" ({frequency})"


def collect_daily_routine():
    logs = sorted(LOG_DIR.glob("daily-routine-*.log"))
    if not logs:
        return {"error": "no daily-routine logs found", "entries": []}
    latest = logs[-1]
    text = latest.read_text(errors="replace")
    section, entries = "start", []
    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for raw_line in text.splitlines():
        line = raw_line
        m = TS_RE.match(line)
        stamp = None
        if m:
            stamp, line = m.group(1), m.group(2)
        line = re.sub(r"^WARNING:\s*", "", line.strip())
        b = BANNER_RE.match(line)
        if b:
            section = b.group(1); continue
        s = STATUS_RE.search(line)
        if s:
            kind, msg = s.group(1), s.group(2).strip()
            counts[kind] += 1
            if kind != "OK":
                entries.append({"kind": kind, "section": section, "message": msg, "time": stamp})
            continue
        if re.search(r"\bWARNING\b|\bERROR\b", raw_line):
            counts["WARN"] += 1
            entries.append({"kind": "WARN", "section": section, "message": line[:200], "time": stamp})
    mtime = latest.stat().st_mtime
    age_h = (time.time() - mtime) / 3600.0
    sched = _read_daily_routine_schedule()
    if sched:
        frequency, hh, mm = sched
        next_run = _next_run_human(frequency, hh, mm)
        schedule = {"frequency": frequency, "time": f"{hh:02d}:{mm:02d}"}
    else:
        next_run, schedule = DAILY_ROUTINE_NEXT_RUN, None
    return {"log": str(latest),
            "ran_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "age_hours": round(age_h, 1), "stale": age_h > 26,
            "next_run": next_run, "schedule": schedule, "counts": counts,
            "entries": entries, "history": [p.name for p in logs[-7:]]}


# ── attention (fix ids retargeted: jail_*/service_*/pkg_upgrade/vpn_*) ───────
def build_attention(host, disks, docker, vpn, services, media, routine,
                    hotplug_drives=None):
    failed, warning, todo = [], [], []

    def add(bucket, source, message, action=None, fix=None, open_link=None):
        bucket.append({"source": source, "message": message, "action": action,
                       "fix": fix, "open": open_link})

    SERVICE_JAIL = {"qBittorrent": "qbittorrent", "Prowlarr": "prowlarr",
                    "Radarr": "radarr", "Sonarr": "sonarr", "Seerr": "seerr",
                    "Plex": "plex", "Jellyfin": "jellyfin", "Bazarr": "bazarr",
                    "Caddy": "caddy", "Nextcloud": "nextcloud"}

    if isinstance(docker, dict) and not docker.get("error"):
        for n in docker.get("not_running", []):
            add(failed, "jail", f"Jail {n} is not running", f"bastille start {n}",
                {"id": "jail_start", "args": {"name": n}, "label": f"Start {n}"})
        for n in docker.get("unhealthy", []):
            add(failed, "jail", f"Jail {n} service reports unhealthy",
                f"bastille cmd {n} service {n} status",
                {"id": "jail_restart", "args": {"name": n}, "label": f"Restart {n}"})

    for s in services if isinstance(services, list) else []:
        if not s["ok"]:
            code = s["code"] if s["code"] is not None else "no connection"
            jn = SERVICE_JAIL.get(s["name"])
            add(failed, "service", f"{s['name']} not responding ({code})",
                f"curl -v {s['url']}",
                {"id": "jail_restart", "args": {"name": jn}, "label": f"Restart {jn}"} if jn else None)

    if isinstance(vpn, dict) and not vpn.get("error"):
        if not vpn.get("configured", True):
            add(todo, "vpn", "No WireGuard config set -- qBittorrent/Sonarr/Radarr "
                "have no internet access until one is added (any provider works, "
                "these three have real free tiers)",
                open_link={"id": "protonvpn_wg", "label": "Get ProtonVPN (free)"})
            add(todo, "vpn", "Windscribe also has a genuine free WireGuard tier",
                open_link={"id": "windscribe_wg", "label": "Get Windscribe (free)"})
            add(todo, "vpn", "Surfshark is a cheap paid option if you'd rather not "
                "juggle a free tier's limits",
                open_link={"id": "surfshark_wg", "label": "Get Surfshark"})
        elif vpn.get("healthy") and vpn["healthy"] != "healthy":
            add(failed, "vpn", f"VPN tunnel is {vpn['healthy']} (exit IP not confirmed)",
                "bastille cmd vpn wg show",
                {"id": "vpn_rotate", "args": {}, "label": "Restart tunnel / rotate exit"})
        if not vpn.get("siblings_ok"):
            broken = [s["name"] for s in vpn.get("siblings", []) if not s["bound"]]
            add(failed, "vpn",
                f"Not routed through the VPN jail: {', '.join(broken)} — traffic may bypass the tunnel",
                "check defaultrouter in the confined jails",
                {"id": "vpn_recreate", "args": {}, "label": "Restart the VPN jail group"})
        if vpn.get("country") and vpn["country"] != EXPECTED_VPN_COUNTRY:
            add(warning, "vpn", f"VPN exit is {vpn['country']}, expected {EXPECTED_VPN_COUNTRY}",
                "wg-rotate.sh", {"id": "vpn_rotate", "args": {}, "label": "Rotate VPN exit IP"})

    for fs in (disks or {}).get("filesystems", []):
        if fs["percent"] >= 90:
            add(failed, "disk", f"{fs['mount']} is {fs['percent']:.0f}% full ({human(fs['avail'])} free)")
        elif fs["percent"] >= 80:
            add(warning, "disk", f"{fs['mount']} is {fs['percent']:.0f}% full ({human(fs['avail'])} free)")

    if host.get("reboot_required"):
        add(todo, "system", "Reboot required (installed kernel newer than running)", "sudo shutdown -r now")
    for u in host.get("failed_units", []):
        add(failed, "rc", f"Service enabled but not running: {u}", f"service {u} status",
            {"id": "restart_service", "args": {"name": u}, "label": f"Restart {u}"})
    if host.get("updates_security"):
        add(todo, "updates", f"{host['updates_security']} vulnerable package(s) (pkg audit)",
            "sudo pkg upgrade", {"id": "pkg_upgrade", "args": {}, "label": "Upgrade packages"})
    elif host.get("updates_pending"):
        add(todo, "updates", f"{host['updates_pending']} package update(s) pending",
            "sudo pkg upgrade", {"id": "pkg_upgrade", "args": {}, "label": "Upgrade packages"})

    for t in (host.get("sensors") or {}).get("temps", []):
        crit = t.get("crit")
        if crit and t["value"] >= crit * 0.92:
            add(warning, "thermal", f"{t['chip']} {t['label']} at {t['value']}°C (limit {crit:.0f}°C)")

    cf = (media or {}).get("cloudflare", {})
    for b in cf.get("blocked", []):
        add(failed, "cloudflare",
            f"{b['host']} is blocking this VPN exit — Cloudflare {b['code']} ({b['reason']})",
            "wg-rotate.sh  # then re-test indexers",
            {"id": "cloudflare_unblock", "args": {"host": b["host"]},
             "label": f"Rotate VPN exit and re-test ({b['host']})"})
    pw = (media or {}).get("prowlarr", {})
    if pw.get("failing"):
        add(warning, "indexers", f"Indexers failing: {', '.join(pw['failing'])}",
            "Prowlarr → Test All", {"id": "prowlarr_testall", "args": {}, "label": "Re-test all indexers"})
    qb = (media or {}).get("qbittorrent", {})
    if qb.get("error"):
        add(warning, "qbittorrent", f"qBittorrent API error: {qb['error'][:90]}", None,
            {"id": "jail_restart", "args": {"name": "qbittorrent"}, "label": "Restart qBittorrent"})
    elif qb.get("incomplete") and qb.get("dl_speed", 0) < 50_000:
        add(warning, "qbittorrent",
            f"{qb['incomplete']} incomplete torrent(s) but download speed is {human(qb.get('dl_speed', 0))}/s — possible stall",
            "daily-routine.sh --quick",
            {"id": "daily_routine_quick", "args": {}, "label": "Run self-heal (quick)"})

    if isinstance(routine, dict):
        if routine.get("stale"):
            add(warning, "daily-routine",
                f"Last run was {routine.get('age_hours')}h ago — cron may not be firing", "crontab -l")
        for e in routine.get("entries", []):
            if e["kind"] == "SKIP":
                continue
            bucket = failed if e["kind"] == "FAIL" else warning
            add(bucket, f"daily-routine › {e['section']}", e["message"], None,
                {"id": "daily_routine_quick", "args": {}, "label": "Re-run daily routine (quick)"})

    for d in hotplug_drives or []:
        add(todo, "storage", f"New drive detected: {d} (not yet in any pool)",
            f"zpool add storage {d}",
            {"id": "pool_add_drive", "args": {"disk": d},
             "label": f"Add {d} to storage pool"})

    def dedupe(items):
        seen, o = set(), []
        for i in items:
            if i["message"] in seen:
                continue
            seen.add(i["message"]); o.append(i)
        return o

    return {"failed": dedupe(failed), "warning": dedupe(warning), "todo": dedupe(todo)}


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    host = guard("host", collect_host)
    disks = guard("disks", collect_disks)
    docker = guard("docker", collect_docker)
    vpn = guard("vpn", collect_vpn)
    services = guard("services", collect_services)
    media = guard("media", collect_media)
    routine = guard("daily_routine", collect_daily_routine)
    hotplug = guard("hotplug_drives", collect_hotplug_drives)
    attention = guard("attention", build_attention,
                      host if isinstance(host, dict) else {},
                      disks if isinstance(disks, dict) else {},
                      docker if isinstance(docker, dict) else {},
                      vpn if isinstance(vpn, dict) else {},
                      services if isinstance(services, list) else [],
                      media if isinstance(media, dict) else {},
                      routine if isinstance(routine, dict) else {},
                      hotplug if isinstance(hotplug, list) else [])
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "generated_epoch": time.time(),
               "collect_seconds": round(time.time() - t0, 1),
               "host": host, "disks": disks, "docker": docker, "vpn": vpn,
               "services": services, "media": media, "daily_routine": routine,
               "hotplug_drives": hotplug,
               "attention": attention}
    tmp = OUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(OUT_FILE)
    print(f"wrote {OUT_FILE} in {payload['collect_seconds']}s "
          f"(fail={len(attention.get('failed', []))} "
          f"warn={len(attention.get('warning', []))} "
          f"todo={len(attention.get('todo', []))})")


if __name__ == "__main__":
    sys.exit(main())
