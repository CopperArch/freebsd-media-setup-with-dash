#!/usr/bin/env python3.11
"""intrusion-check.py — nightly malware + "is anyone in / watching" pass
(FreeBSD edition, ported from the Linux edition 2026-11-05).

security-check.sh --full runs it as root, so its tagged [OK]/[WARN]/[FAIL]
lines land on the dashboard with the rest of the security section.

  1. Malware     ClamAV over files changed in the last day (home + downloads),
                 the whole home folder on Sundays. Reports, never deletes.
  2. Rootkits    chkrootkit (rkhunter is not packaged for FreeBSD 15).
  3. Logins      SSH logins from outside the home LAN/Tailscale, failed SSH and
                 sudo attempts (/var/log/auth.log), account changes
                 (/var/log/userlog, written by pw(8)).
  4. App logins  failed Jellyfin and Nextcloud logins (brute-force signs).
  5. Persistence new login accounts, wheel members, SSH keys, sudo/doas rules,
                 cron jobs, enabled services, jails — compared with a baseline.
  6. Listeners   new listening ports (host and jails).
  7. Outbound    programs talking to the internet that weren't before, and
                 connections to classic backdoor/IRC ports.

Anything new in 5-7 is reported until it is removed or accepted with
    intrusion-check.py --accept      (security-check.sh --accept does it too)
which records the current state as expected (the first run does this itself).
Any [FAIL] also sends one summary e-mail through send-alert.py, if installed.
"""
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path("{{HOME}}")
CONF = HOME / ".config/status-dashboard"
BASELINE = CONF / "intrusion-baseline.json"
STATE = HOME / ".dbmedia/state"
SEND_ALERT = HOME / ".dbmedia/scripts/send-alert.py"
DOWNLOADS = Path("{{MEDIA_POOL}}/downloads")
NEXTCLOUD_LOG = Path("{{NEXTCLOUD_DATA}}/nextcloud.log")
JELLYFIN_LOGS = Path("/usr/local/bastille/jails/jellyfin/root/var/db/jellyfin/log")
AUTH_LOG = Path("/var/log/auth.log")
USER_LOG = Path("/var/log/userlog")
CLAMSCAN = "/usr/local/bin/clamscan"
CHKROOTKIT = "/usr/local/sbin/chkrootkit"
ROOTKIT_KNOWN = CONF / "chkrootkit-known.txt"

# Where logins are expected from: home LAN, Tailscale, the jail bridge, loopback.
TRUSTED_NETS = [ipaddress.ip_network(n) for n in (
    "{{LAN_CIDR}}", "{{SUBNET}}.0/24", "100.64.0.0/10", "fd7a:115c:a1e0::/48",
    "127.0.0.0/8", "::1/128")]
# Classic backdoor / botnet C2 / IRC ports: an outbound connection to one of
# these is never normal on a media server.
BAD_PORTS = {23, 1337, 4444, 5555, 6666, 6667, 6668, 6669, 6697, 12345, 31337}
FAILED_SSH_WARN = 20
FAILED_APP_WARN = 10
SCAN_SKIP_DIRS = (".cache", ".local/share/Trash", ".npm", ".cargo", "node_modules",
                  ".dbmedia/staging")
SCAN_MAX_MB = 200   # skip big media files; malware isn't a 4 GB .mkv

ACCEPT = "--accept" in sys.argv[1:]
fails = []


def ok(m): print(f"  [OK]   {m}", flush=True)
def warn(m): print(f"  [WARN] {m}", flush=True)
def skip(m): print(f"  [SKIP] {m}", flush=True)


def fail(m):
    print(f"  [FAIL] {m}", flush=True)
    fails.append(m)


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _addr(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0].strip("[]"))
    except ValueError:
        return None
    return a.ipv4_mapped if a.version == 6 and a.ipv4_mapped else a


def trusted(ip):
    a = _addr(ip)
    return a is not None and any(a in n for n in TRUSTED_NETS if n.version == a.version)


def public(ip):
    a = _addr(ip)
    return a is not None and a.is_global and not trusted(str(a))


def log_time(line, now):
    """syslogd's 'Nov  5 19:40:01' (no year), pw(8)'s '2026-11-05 20:02:45',
    or an RFC 3339/5424 stamp."""
    try:
        t = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        return t.replace(tzinfo=now.tzinfo)
    except ValueError:
        pass
    try:
        t = datetime.fromisoformat(line.split(" ", 1)[0].removeprefix("<13>1 "))
        return t if t.tzinfo else t.replace(tzinfo=now.tzinfo)
    except ValueError:
        pass
    try:
        t = datetime.strptime(f"{now.year} {line[:15]}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None
    t = t.replace(tzinfo=now.tzinfo)
    return t.replace(year=now.year - 1) if t > now + timedelta(days=1) else t


# ── 1. Malware (ClamAV) ──────────────────────────────────────────────────────
def malware_scan():
    if not os.access(CLAMSCAN, os.X_OK):
        skip("malware scan: ClamAV not installed (pkg install clamav)")
        return
    full = datetime.now().weekday() == 6
    cutoff = time.time() - 26 * 3600
    files = []
    for root in [HOME] + ([DOWNLOADS] if DOWNLOADS.is_dir() else []):
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, HOME) if root == HOME else ""
            dirnames[:] = [d for d in dirnames
                           if not any(os.path.join(rel, d).startswith(s) or d == s
                                      for s in SCAN_SKIP_DIRS)]
            for f in filenames:
                p = os.path.join(dirpath, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if os.path.islink(p) or not os.path.isfile(p) \
                        or st.st_size > SCAN_MAX_MB * 1048576:
                    continue
                if full or st.st_mtime > cutoff:
                    files.append(p)
    if not files:
        ok("malware scan: no new or changed files to scan")
        return
    STATE.mkdir(parents=True, exist_ok=True)
    lst = STATE / "clamscan-files.txt"
    lst.write_text("\n".join(files))
    rc, out = run([CLAMSCAN, "--no-summary", "--infected", f"--max-filesize={SCAN_MAX_MB}M",
                   f"--max-scansize={SCAN_MAX_MB}M", "--file-list", str(lst)], timeout=6 * 3600)
    hits = [l for l in out.splitlines() if l.endswith(" FOUND")]
    what = "full home scan" if full else "files changed in the last day"
    if hits:
        fail(f"MALWARE found ({what}): " + "; ".join(hits[:10]) +
             (f" (+{len(hits) - 10} more)" if len(hits) > 10 else "") +
             " — not deleted; check and remove them")
    elif rc not in (0, 1):
        warn(f"malware scan did not finish (clamscan rc={rc}): {out.strip()[-200:]}")
    else:
        ok(f"malware scan clean ({len(files)} files, {what})")


# ── 2. Rootkits (chkrootkit) ─────────────────────────────────────────────────
def rootkit_scan():
    if not os.access(CHKROOTKIT, os.X_OK):
        skip("rootkit scan: chkrootkit not installed (pkg install chkrootkit)")
        return
    # -q prints only what looks wrong; jail interfaces in promiscuous mode
    # (bridge members) are normal here, so packet-sniffer notes are ignored.
    # Some checks always say INFECTED on a clean FreeBSD (`nologin', Syslogk
    # LKM — tested on a fresh 15.1 install), so what it reports on the first
    # run / --accept is recorded and only NEW findings are raised.
    rc, out = run([CHKROOTKIT, "-q"], timeout=1800)
    lines = sorted({re.sub(r"\s+", " ", l.strip()) for l in out.splitlines() if l.strip()
                    and not re.search(r"PROMISC|packet sniffer|not promisc|^! ", l, re.I)})
    if ACCEPT or not ROOTKIT_KNOWN.exists():
        CONF.mkdir(parents=True, exist_ok=True)
        ROOTKIT_KNOWN.write_text("\n".join(lines) + "\n")
        ok(f"rootkit scan (chkrootkit): recorded {len(lines)} known note(s) as this "
           f"system's normal state ({ROOTKIT_KNOWN})")
        return
    known = set(ROOTKIT_KNOWN.read_text().splitlines())
    new = [l for l in lines if l not in known]
    infected = [l for l in new if re.search(r"INFECTED|backdoor|trojan|rootkit", l, re.I)]
    if infected:
        fail("chkrootkit: " + "; ".join(infected[:5]))
    elif new:
        warn(f"chkrootkit: {len(new)} new note(s) — " + "; ".join(new[:3]))
    else:
        ok("rootkit scan (chkrootkit): nothing new")


# ── 3. Logins from the system logs ───────────────────────────────────────────
def login_audit():
    if not AUTH_LOG.exists():
        skip("login audit: /var/log/auth.log not found")
        return
    now = datetime.now(timezone.utc).astimezone()
    since = now - timedelta(hours=24)
    accepted, failed, sudo_fail, acct = [], {}, 0, []
    with open(AUTH_LOG, errors="replace") as f:
        for line in f:
            ts = log_time(line, now)
            if ts is None or ts < since:
                continue
            m = re.search(r"Accepted (\S+) for (\S+) from (\S+)", line)
            if m:
                accepted.append((m.group(2), m.group(3), m.group(1)))
                continue
            m = re.search(r"(?:Failed \S+ for (?:invalid user )?\S+|Invalid user \S+) from (\S+)", line)
            if m:
                failed[m.group(1)] = failed.get(m.group(1), 0) + 1
                continue
            if re.search(r"(sudo|doas)\b.*(authentication failure|incorrect password|failed)", line):
                sudo_fail += 1
    if USER_LOG.exists():
        with open(USER_LOG, errors="replace") as f:
            for line in f:
                # pw(8): "2026-11-05 20:02:45 [root:useradd] eve(1002):...:/bin/sh".
                # Packages add system accounts (uid < 1000, nologin) — not
                # logins, so not reported; group changes only for wheel.
                ts = log_time(line, now)
                m = re.search(r"\[(\S+):(useradd|userdel|usermod|lock|unlock|groupmod)\] (.*)", line)
                if not (ts and ts >= since and m):
                    continue
                who, op, rest = m.groups()
                if op == "groupmod" and not rest.startswith("wheel"):
                    continue
                uid = re.match(r"\S+?\((\d+)\)", rest)
                if "nologin" in rest or (uid and int(uid.group(1)) < 1000 and op != "groupmod"):
                    continue
                if re.search(r"home .*(made|removed)", rest):
                    continue
                acct.append(f"{op} by {who}: {rest[:100]}")
    outside = [a for a in accepted if not trusted(a[1])]
    if outside:
        fail("SSH login(s) from outside your home network/Tailscale: " +
             ", ".join(f"{u} from {ip} ({how})" for u, ip, how in outside[:5]))
    else:
        ok(f"SSH logins in the last day: {len(accepted)}, all from home/Tailscale")
    nfail = sum(failed.values())
    if nfail >= FAILED_SSH_WARN:
        top = sorted(failed.items(), key=lambda x: -x[1])[:3]
        warn(f"{nfail} failed SSH logins in the last day (top: " +
             ", ".join(f"{ip} x{n}" for ip, n in top) + ") — someone is guessing passwords")
    else:
        ok(f"failed SSH logins in the last day: {nfail}")
    if sudo_fail:
        warn(f"{sudo_fail} failed sudo/doas password attempt(s) in the last day")
    if acct:
        fail("user account changes in the last day: " + "; ".join(acct[:5]))


# ── 4. App logins (Jellyfin, Nextcloud) ──────────────────────────────────────
def app_logins():
    since = time.time() - 24 * 3600
    if JELLYFIN_LOGS.is_dir():
        denied = 0
        for lf in JELLYFIN_LOGS.glob("*.log"):
            if lf.stat().st_mtime < since:
                continue
            denied += len(re.findall(r"Authentication request for .* has been denied",
                                     lf.read_text(errors="replace")))
        (warn if denied >= FAILED_APP_WARN else ok)(
            f"Jellyfin failed logins (recent logs): {denied}")
    if NEXTCLOUD_LOG.exists():
        cut = datetime.now(timezone.utc) - timedelta(hours=24)
        n, ips = 0, set()
        with open(NEXTCLOUD_LOG, "rb") as f:
            f.seek(max(0, NEXTCLOUD_LOG.stat().st_size - 5_000_000))
            for line in f.read().decode(errors="replace").splitlines():
                if "Login failed" not in line and "Bruteforce" not in line:
                    continue
                try:
                    e = json.loads(line)
                    if datetime.fromisoformat(e["time"].replace("Z", "+00:00")) < cut:
                        continue
                    ips.add(e.get("remoteAddr", ""))
                except Exception:
                    continue
                n += 1
        if n >= FAILED_APP_WARN:
            warn(f"Nextcloud: {n} failed logins in the last day from "
                 f"{', '.join(sorted(i for i in ips if i)[:5])}")
        else:
            ok(f"Nextcloud failed logins in the last day: {n}")


# ── 5-7. Things compared with the baseline ───────────────────────────────────
def _digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def snapshot():
    s = {}
    users = []
    for l in Path("/etc/passwd").read_text().splitlines():
        p = l.split(":")
        if len(p) < 7 or l.startswith("#"):
            continue
        if p[2] == "0" or (int(p[2]) >= 1000 and p[2] != "65534"
                           and not p[6].endswith(("nologin", "false"))):
            users.append(f"{p[0]}(uid {p[2]}, {p[6]})")
    s["login accounts"] = users
    grp = {l.split(":")[0]: l.split(":")[3] for l in Path("/etc/group").read_text().splitlines()
           if l and not l.startswith("#") and l.count(":") >= 3}
    s["admin (wheel) members"] = sorted(set(filter(None, grp.get("wheel", "").split(","))))
    keys = []
    for ak in (HOME / ".ssh/authorized_keys", Path("/root/.ssh/authorized_keys")):
        if ak.exists():
            for l in ak.read_text().splitlines():
                parts = l.split()
                if len(parts) > 1 and not l.startswith("#"):
                    keys.append(f"{ak.parent.parent.name}: {parts[-1]} {parts[1][-12:]}")
    s["SSH keys allowed to log in"] = keys
    rules = []
    for f in ("/usr/local/etc/sudoers", "/usr/local/etc/doas.conf"):
        d = _digest(f)
        if d:
            rules.append(f"{f} {d}")
    if os.path.isdir("/usr/local/etc/sudoers.d"):
        rules += [f"/usr/local/etc/sudoers.d/{f} {_digest('/usr/local/etc/sudoers.d/' + f)}"
                  for f in sorted(os.listdir("/usr/local/etc/sudoers.d"))]
    s["sudo/doas rules"] = rules
    cron = [f"/etc/crontab {_digest('/etc/crontab')}"]
    for d in ("/var/cron/tabs", "/etc/cron.d", "/usr/local/etc/cron.d"):
        if os.path.isdir(d):
            cron += [f"{d}/{f} {_digest(os.path.join(d, f))}" for f in sorted(os.listdir(d))]
    s["cron jobs"] = cron
    rc, out = run(["service", "-e"])
    s["enabled services"] = sorted(os.path.basename(l) for l in out.split()) if rc == 0 else []
    rc, out = run(["jls", "name"])
    s["jails"] = sorted(out.split()) if rc == 0 else []
    # Listening ports: sockstat lists host and jail sockets alike.
    rc, out = run(["sockstat", "-46l", "-q"])
    lst = set()
    for l in out.splitlines():
        p = l.split()
        if len(p) < 6:
            continue
        cmd, proto, local = p[1], p[4], p[5]
        port = local.rsplit(":", 1)[-1]
        scope = "local" if local.startswith(("127.", "[::1]", "::1")) else "network"
        lst.add(f"{proto}/{port} {scope} ({cmd})")
    s["listening ports"] = sorted(lst)
    # Outbound: which programs talk to the internet (names only — IPs change).
    rc, out = run(["sockstat", "-46c", "-q"])
    talkers, bad = set(), []
    for l in out.splitlines():
        p = l.split()
        if len(p) < 7:
            continue
        cmd, foreign = p[1], p[6]
        ip, _, port = foreign.rpartition(":")
        if not public(ip):
            continue
        talkers.add(cmd)
        if port.isdigit() and int(port) in BAD_PORTS:
            bad.append(f"{cmd} -> {ip}:{port}")
    s["programs talking to the internet"] = sorted(talkers)
    return s, bad


def compare(snap, bad):
    if bad:
        fail("connection(s) to backdoor/IRC ports: " + ", ".join(bad[:5]))
    if ACCEPT or not BASELINE.exists():
        CONF.mkdir(parents=True, exist_ok=True)
        old = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
        # Outbound talkers accumulate: a program seen once is expected later.
        k = "programs talking to the internet"
        snap[k] = sorted(set(snap[k]) | set(old.get(k, [])))
        BASELINE.write_text(json.dumps({"recorded": time.strftime("%Y-%m-%d %H:%M"),
                                        **snap}, indent=1))
        BASELINE.chmod(0o600)
        ok(f"recorded accounts, keys, services, ports and outbound programs as expected ({BASELINE})")
        return
    base = json.loads(BASELINE.read_text())
    severe = {"login accounts", "admin (wheel) members", "SSH keys allowed to log in",
              "sudo/doas rules"}
    for k, now in snap.items():
        new = sorted(set(now) - set(base.get(k, [])))
        if not new:
            ok(f"no new {k}")
            continue
        msg = (f"new {k}: {', '.join(new[:6])}" + (f" (+{len(new) - 6} more)" if len(new) > 6 else "")
               + " — if you added it: security-check.sh --accept")
        (fail if k in severe else warn)(msg)


def main():
    print("  --- intrusion & malware check ---", flush=True)
    for step in (login_audit, app_logins, lambda: compare(*snapshot()),
                 rootkit_scan, malware_scan):
        try:
            step()
        except Exception as e:
            warn(f"{getattr(step, '__name__', 'check')} crashed: {type(e).__name__}: {e}")
    if fails and SEND_ALERT.exists() and not ACCEPT:
        run([str(SEND_ALERT), f"SECURITY: intrusion/malware check on {os.uname().nodename}",
             "The nightly intrusion & malware check flagged:\n\n- " + "\n- ".join(fails) +
             "\n\nIf a flagged item is something you added yourself: security-check.sh --accept\n"],
            timeout=60)


if __name__ == "__main__":
    main()
