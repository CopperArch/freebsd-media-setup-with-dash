#!/usr/bin/env python3.11
"""Nightly media self-heal checks, run by daily-routine.sh (ported from the
Linux edition's media-stack-selfheal.py, 2026-11-05).

  english   English-only audio: any release that is not English, or carries
            any non-English audio track (dual/multi, dubs), is rejected in
            every Sonarr/Radarr quality profile. Formats are created if
            missing; scores are re-pinned every night.
  qbit      qBittorrent peer check: after the vpn jail's tunnel drops and
            comes back, libtorrent can stay bound to the dead tunnel (WebUI
            fine, 0 DHT nodes, every torrent stuck in metaDL). Re-setting
            the listen port makes it rebind.

With no argument both run. Jails are reached on their bastille0 IPs, API
keys come from ~/.config/status-dashboard/media-keys.env.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from status_keys import installed_jails, jail_ip, load_env

KEYS = load_env()
QBIT = {"url": f"http://{jail_ip('qbittorrent')}:8080",
        "user": KEYS.get("QBIT_USER", "admin"), "pass": KEYS.get("QBIT_PASS", "")}
RADARR = {"url": f"http://{jail_ip('radarr')}:7878", "key": KEYS.get("RADARR_KEY", "")}
SONARR = {"url": f"http://{jail_ip('sonarr')}:8989", "key": KEYS.get("SONARR_KEY", "")}


def log(msg):
    print(f"  {msg}", flush=True)


def alert(msg):
    print(f"WARNING: {msg}", flush=True)


def request(method, url, headers=None, data=None, opener=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    return opener.open(req, timeout=timeout) if opener \
        else urllib.request.urlopen(req, timeout=timeout)


def arr_get(arr, path):
    return json.load(request("GET", f"{arr['url']}/api/v3{path}",
                             {"X-Api-Key": arr["key"]}))


def qbit_login():
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))
    body = urllib.parse.urlencode(
        {"username": QBIT["user"], "password": QBIT["pass"]}).encode()
    try:
        request("POST", f"{QBIT['url']}/api/v2/auth/login",
                {"Referer": QBIT["url"]}, body, opener, timeout=15)
        return opener
    except Exception as e:
        log(f"[FAIL] qBittorrent login failed: {e}")
        return None


def qbit_get(opener, path):
    return json.load(request("GET", f"{QBIT['url']}{path}",
                             {"Referer": QBIT["url"]}, None, opener))


def qbit_post(opener, path, fields):
    body = urllib.parse.urlencode(fields).encode()
    return request("POST", f"{QBIT['url']}{path}",
                   {"Referer": QBIT["url"]}, body, opener)


# English-only audio. Language 1 = English in both Radarr and Sonarr.
ENGLISH_ONLY_SCORE = -10000
_FOREIGN_MARKERS = [
    r"\b(ita|italian)\b", r"\b(spanish|castellano|latino|espanol|español)\b",
    r"\b(french|truefrench|vff|vfq|vostfr)\b", r"\b(german|deutsch)\b",
    r"\b(hindi|tamil|telugu|kannada|malayalam|bengali|punjabi|marathi)\b",
    r"\b(dublado|legendado|legenda|legendas)\b", r"\bdual[\s._-]?audio\b",
    r"\b(russian|polish|lektor|ukrainian|rus)\b",
    r"\b(korean|japanese|mandarin|cantonese|chinese)\b", r"\bmulti\b",
    r"\bdual\b", r"\b(nordic|swedish|danish|norwegian|finnish)\b",
    r"\b(turkish|greek|hebrew|thai|vietnamese|indonesian)\b",
    r"\b(hun|hungarian|cze|czech|slovak|romanian|bulgarian)\b",
    r"\b(por|portuguese|brazilian|dublat)\b", r"\b(esp|fra|ger|deu|rus|ita)\b",
]
ENGLISH_ONLY_FORMATS = {
    "Not English": [{
        "name": "English", "implementation": "LanguageSpecification",
        "negate": True, "required": True,
        "fields": [{"name": "value", "value": 1}]}],
    "Foreign Audio Present": [{
        "name": "Any non-English track", "implementation": "LanguageSpecification",
        "negate": False, "required": True,
        "fields": [{"name": "value", "value": 1},
                   {"name": "exceptLanguage", "value": True}]}],
    "Foreign Language Markers": [{
        "name": p, "implementation": "ReleaseTitleSpecification",
        "negate": False, "required": False,
        "fields": [{"name": "value", "value": p}]} for p in _FOREIGN_MARKERS],
}


def reconcile_english_only(arr, name):
    """Ensure the English-only custom formats exist and are scored
    ENGLISH_ONLY_SCORE in every quality profile, so no release with a
    non-English audio track is ever grabbed."""
    hdr = {"X-Api-Key": arr["key"], "Content-Type": "application/json"}
    try:
        cfs = {c["name"]: c["id"] for c in arr_get(arr, "/customformat")}
    except Exception as e:
        log(f"[FAIL] {name}: can't read custom formats ({e})")
        return
    for fmt, specs in ENGLISH_ONLY_FORMATS.items():
        if fmt in cfs:
            continue
        try:
            cf = json.load(request(
                "POST", f"{arr['url']}/api/v3/customformat", hdr,
                json.dumps({"name": fmt, "includeCustomFormatWhenRenaming": False,
                            "specifications": specs}).encode()))
            cfs[fmt] = cf["id"]
            log(f"[FIX ] {name}: recreated missing '{fmt}' custom format")
            alert(f"{name}: English-only custom format '{fmt}' was missing "
                  f"and has been recreated")
        except Exception as e:
            log(f"[FAIL] {name}: could not create '{fmt}' ({e})")
    ids = {cfs[f] for f in ENGLISH_ONLY_FORMATS if f in cfs}
    try:
        profiles = arr_get(arr, "/qualityprofile")
    except Exception as e:
        log(f"[FAIL] {name}: can't read quality profiles ({e})")
        return
    fixed = []
    for qp in profiles:
        changed = False
        for item in qp.get("formatItems", []):
            if item["format"] in ids and item.get("score") != ENGLISH_ONLY_SCORE:
                item["score"] = ENGLISH_ONLY_SCORE
                changed = True
        if not changed:
            continue
        try:
            request("PUT", f"{arr['url']}/api/v3/qualityprofile/{qp['id']}",
                    hdr, json.dumps(qp).encode())
            fixed.append(qp["name"])
        except Exception as e:
            log(f"[FAIL] {name}: could not fix profile '{qp['name']}' ({e})")
    if fixed:
        log(f"[FIX ] {name}: English-only scores re-pinned in: "
            f"{', '.join(fixed)}")
        alert(f"{name}: English-only audio scores had drifted in "
              f"{len(fixed)} profile(s), corrected to {ENGLISH_ONLY_SCORE}")
    else:
        log(f"[OK]   {name}: English-only audio enforced in all "
            f"{len(profiles)} profiles")


def qbit_peer_health(opener):
    """Make sure qBittorrent can actually reach peers, not just that it's up.

    2026-11-05: after a VPN tunnel cycle (hourly rotation, or the one
    daily-routine.sh does right before this) libtorrent can stay bound to the
    dead tunnel: the WebUI answers, the container is "Up", but DHT has 0 nodes
    and every torrent sits in metaDL at 0 B/s forever. Re-setting the listen
    port forces libtorrent to rebind its sockets, which fixed it instantly.
    Returns True if qBittorrent is healthy (or was healed).
    """
    log("qBittorrent peer connectivity:")

    def state():
        info = qbit_get(opener, "/api/v2/transfer/info")
        return info.get("connection_status", "unknown"), info.get("dht_nodes", 0)

    try:
        prefs = qbit_get(opener, "/api/v2/app/preferences")
        conn, dht = state()
    except Exception as e:
        log(f"[FAIL] can't read qBittorrent status: {e}")
        alert(f"qBittorrent API not answering: {e}")
        return False
    dht_on = prefs.get("dht", False)
    healthy = conn != "disconnected" and (dht > 0 or not dht_on)
    if healthy:
        log(f"[OK]   connection={conn}, DHT nodes={dht}")
        return True
    port = prefs.get("listen_port", 6881)
    for attempt in range(2):
        log(f"[FIX ] connection={conn}, DHT nodes={dht} — rebinding listen "
            f"port {port} (attempt {attempt + 1})")
        try:
            qbit_post(opener, "/api/v2/app/setPreferences",
                      {"json": json.dumps({"listen_port": port + 1})})
            time.sleep(3)
            qbit_post(opener, "/api/v2/app/setPreferences",
                      {"json": json.dumps({"listen_port": port})})
            time.sleep(60)
            conn, dht = state()
        except Exception as e:
            log(f"[FAIL] rebind failed: {e}")
            continue
        if conn != "disconnected" and (dht > 0 or not dht_on):
            log(f"[OK]   recovered: connection={conn}, DHT nodes={dht}")
            return True
    log(f"[WARN] qBittorrent still has no peers (connection={conn}, "
        f"DHT nodes={dht})")
    alert(f"qBittorrent can't reach any peers (connection={conn}, DHT "
          f"nodes={dht}) even after rebinding its port — check the vpn jail")
    return False


def main():
    what = sys.argv[1:] or ["english", "qbit"]
    jails = installed_jails(["qbittorrent", "sonarr", "radarr"])
    if "qbit" in what:
        if "qbittorrent" in jails:
            o = qbit_login()
            if o:
                qbit_peer_health(o)
        else:
            log("[SKIP] no qbittorrent jail")
    if "english" in what:
        log("English-only audio:")
        for arr, name, jail in ((RADARR, "Radarr", "radarr"),
                                (SONARR, "Sonarr", "sonarr")):
            if jail not in jails:
                log(f"[SKIP] {name}: no {jail} jail")
            elif not arr["key"]:
                log(f"[SKIP] {name}: no API key in media-keys.env")
            else:
                reconcile_english_only(arr, name)


if __name__ == "__main__":
    main()
