#!/usr/bin/env python3.11
"""Nightly media self-heal checks, run by daily-routine.sh (ported from the
Linux edition's media-stack-selfheal.py, 2026-11-05).

  english   English-only audio: any release that is not English, or carries
            any non-English audio track (dual/multi, dubs), is rejected in
            every Sonarr/Radarr quality profile. Formats are created if
            missing; scores are re-pinned every night.
  dual      English-dub fallback: a foreign-original series/movie that is
            still missing hours after it was added/aired only exists in its
            original language or as "DUAL" (original + English dub), so the
            English-only rule makes it undownloadable. Grab the best DUAL/ENG
            release instead (never malware-flagged, SD or 4K). Bounded: 3
            searches per *arr per run, 3-day retry per title.
  qbit      qBittorrent peer check: after the vpn jail's tunnel drops and
            comes back, libtorrent can stay bound to the dead tunnel (WebUI
            fine, 0 DHT nodes, every torrent stuck in metaDL). Re-setting
            the listen port makes it rebind.

With no argument all three run. Jails are reached on their bastille0 IPs, API
keys come from ~/.config/status-dashboard/media-keys.env.
"""
import json
import os
import re
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


# ── Dual-audio fallback ──────────────────────────────────────────────────────
# Dual-audio fallback (2026-11-10). English-only rejects every release that
# carries a non-English track, so a foreign-original title (e.g. the French
# "Campus Drivers") that only exists as French or as "DUAL" (original + English
# dub) could never download. When a monitored item is still missing this long
# after it was added/aired, accept the best DUAL/ENG release instead.
DUAL_FALLBACK_AFTER_H = 6
DUAL_FALLBACK_BATCH = 3          # interactive searches per *arr per run
DUAL_FALLBACK_RETRY_DAYS = 3
WANTED_STATE = os.path.expanduser("~/.local/state/media-heal-dual.json")


def _wanted_state():
    try:
        with open(WANTED_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_wanted_state(st):
    cutoff = time.time() - DUAL_FALLBACK_RETRY_DAYS * 86400
    st = {k: v for k, v in st.items() if v > cutoff}
    try:
        os.makedirs(os.path.dirname(WANTED_STATE), exist_ok=True)
        with open(WANTED_STATE, "w") as fh:
            json.dump(st, fh)
    except Exception as e:
        log(f"[WARN] could not write {WANTED_STATE} ({e})")

_FOREIGN_CF_NAMES = set(ENGLISH_ONLY_FORMATS)
# A release that includes an English track. DUAL from a streaming source is
# original + English; "ENG"/"English" as an audio tag. MULTI alone is NOT
# trusted (French groups' MULTI is usually French + original, no English).
_HAS_ENGLISH_RE = re.compile(r"\b(dual|eng|english)\b(?![\s._-]*sub)", re.I)
_CF_REJECT_RE = re.compile(r"Custom Formats? (.+?) ha(?:s|ve) score", re.I)


_QUAL_UNWANTED_RE = re.compile(r"^(\S+) is not wanted in profile")
_SEED_REJECT_RE = re.compile(r"^Not enough seeders: (\d+)")


def _fallback_ok(rel):
    """None if this release is unusable even as a fallback, else a tuple of
    (quality_in_profile, ...) for ranking. Tolerated rejections ONLY:
      - the English-only custom formats (never the malware format etc.);
      - low seeders, as long as there is at least 1 (dubs are rarer);
      - quality outside the profile when it is 720p/1080p (same idea as the
        Sonarr 720p->1080p fallback profile) — never SD, 4K or Unknown.
    Anything else (size, blocklist, sample, already-have...) disqualifies."""
    rej = rel.get("rejections") or []
    if not rej:
        return None
    foreign = False
    qual_ok = True
    for r in rej:
        m = _CF_REJECT_RE.search(r)
        if m:
            names = {n.strip() for n in m.group(1).split(",")}
            if not names <= _FOREIGN_CF_NAMES:
                return None
            foreign = True
            continue
        m = _SEED_REJECT_RE.match(r)
        if m:
            if int(m.group(1)) < 1:
                return None
            continue
        m = _QUAL_UNWANTED_RE.match(r)
        if m and re.search(r"-(720|1080)p$", m.group(1)):
            qual_ok = False
            continue
        return None
    return (qual_ok,) if foreign else None


def _iso_age_h(iso):
    try:
        t = time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        return (time.time() - t) / 3600
    except Exception:
        return 0


def _release_search(arr, query):
    return json.load(request("GET", f"{arr['url']}/api/v3/release?{query}",
                             {"X-Api-Key": arr["key"]}, timeout=300))


def _push_release(arr, rel):
    request("POST", f"{arr['url']}/api/v3/release",
            {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
            json.dumps({"guid": rel["guid"],
                        "indexerId": rel["indexerId"]}).encode(), timeout=120)


def _rank(rel):
    seeds = rel.get("seeders") or 0
    return (_fallback_ok(rel) or (False,))[0], rel.get("qualityWeight") or 0, seeds


def dual_audio_fallback():
    """Grab DUAL/English-dub releases for foreign-original titles that are
    otherwise un-downloadable under the English-only rule. See the constants."""
    log(f"Dual-audio fallback (foreign-original titles missing "
        f">{DUAL_FALLBACK_AFTER_H}h):")
    st = _wanted_state()
    now = time.time()

    # ── Sonarr ───────────────────────────────────────────────────────────────
    try:
        queued = {r.get("episodeId") for r in
                  arr_get(SONARR, "/queue?pageSize=1000").get("records", [])}
        w = arr_get(SONARR, "/wanted/missing?pageSize=500&monitored=true"
                            "&includeSeries=true")
        groups = {}
        for e in w.get("records", []):
            ser = e.get("series") or {}
            lang = (ser.get("originalLanguage") or {}).get("name", "English")
            if lang == "English" or e["id"] in queued or not e.get("airDateUtc"):
                continue
            if min(_iso_age_h(e["airDateUtc"]),
                   _iso_age_h(ser.get("added", "1970-01-01T00:00:00"))) < DUAL_FALLBACK_AFTER_H:
                continue
            groups.setdefault((e["seriesId"], e["seasonNumber"]),
                              (ser.get("title", "?"), {}))[1][e["episodeNumber"]] = e
        done = 0
        for (sid, season), (title, missing) in groups.items():
            if done >= DUAL_FALLBACK_BATCH:
                break
            key = f"sonarr-dual:{sid}:{season}"
            if now - st.get(key, 0) < DUAL_FALLBACK_RETRY_DAYS * 86400:
                continue
            st[key] = now
            done += 1
            try:
                rels = [r for r in _release_search(
                            SONARR, f"seriesId={sid}&seasonNumber={season}")
                        if _HAS_ENGLISH_RE.search(r.get("title", ""))
                        and _fallback_ok(r)]
            except Exception as ex:
                log(f"[WARN] Sonarr: {title} S{season:02d} — search failed "
                    f"({ex}), retry in {DUAL_FALLBACK_RETRY_DAYS}d")
                continue
            need = set(missing)
            got = []
            for r in sorted(rels, key=lambda r: (
                    len(need & (set(missing) if r.get("fullSeason")
                                else set(r.get("mappedEpisodeNumbers")
                                         or r.get("episodeNumbers") or []))),
                    _rank(r)), reverse=True):
                cover = (set(missing) if r.get("fullSeason") else
                         set(r.get("mappedEpisodeNumbers")
                             or r.get("episodeNumbers") or []))
                if not (cover & need):
                    continue
                try:
                    _push_release(SONARR, r)
                    need -= cover
                    got.append(r["title"])
                except Exception as ex:
                    log(f"[WARN] Sonarr: could not grab {r['title'][:60]} ({ex})")
                if not need:
                    break
            if got:
                log(f"[FIX ] Sonarr: {title} S{season:02d} — grabbed "
                    f"{len(got)} English-dub release(s): " + "; ".join(t[:60] for t in got[:3])
                    + (" ..." if len(got) > 3 else ""))
            else:
                log(f"[OK]   Sonarr: {title} S{season:02d} — no English-dub "
                    f"release yet ({len(missing)} ep missing), retry in "
                    f"{DUAL_FALLBACK_RETRY_DAYS}d")
        if not groups:
            log("[OK]   Sonarr: no foreign-original episodes stuck")
    except Exception as e:
        log(f"[WARN] Sonarr dual-audio fallback failed ({e})")

    # ── Radarr ───────────────────────────────────────────────────────────────
    try:
        queued = {r.get("movieId") for r in
                  arr_get(RADARR, "/queue?pageSize=1000").get("records", [])}
        w = arr_get(RADARR, "/wanted/missing?pageSize=500&monitored=true")
        done = 0
        stuck = 0
        for m in w.get("records", []):
            lang = (m.get("originalLanguage") or {}).get("name", "English")
            if lang == "English" or m.get("hasFile") or m["id"] in queued:
                continue
            if not (m.get("status") == "released" or
                    (m.get("status") == "inCinemas" and
                     m.get("minimumAvailability") == "inCinemas")):
                continue
            if _iso_age_h(m.get("added", "1970-01-01T00:00:00")) < DUAL_FALLBACK_AFTER_H:
                continue
            stuck += 1
            if done >= DUAL_FALLBACK_BATCH:
                continue
            key = f"radarr-dual:{m['id']}"
            if now - st.get(key, 0) < DUAL_FALLBACK_RETRY_DAYS * 86400:
                continue
            st[key] = now
            done += 1
            try:
                rels = [r for r in _release_search(RADARR, f"movieId={m['id']}")
                        if _HAS_ENGLISH_RE.search(r.get("title", ""))
                        and _fallback_ok(r)]
            except Exception as ex:
                # One slow interactive search (Radarr drops the connection
                # when an indexer hangs) must not abort the whole pass.
                log(f"[WARN] Radarr: {m['title'][:40]} — search failed "
                    f"({ex}), retry in {DUAL_FALLBACK_RETRY_DAYS}d")
                continue
            if not rels:
                log(f"[OK]   Radarr: {m['title'][:40]} — no English-dub "
                    f"release yet, retry in {DUAL_FALLBACK_RETRY_DAYS}d")
                continue
            best = max(rels, key=_rank)
            try:
                _push_release(RADARR, best)
            except Exception as ex:
                log(f"[WARN] Radarr: could not grab {best['title'][:60]} ({ex})")
                continue
            log(f"[FIX ] Radarr: {m['title'][:40]} — grabbed English-dub "
                f"release {best['title'][:70]}")
        if not stuck:
            log("[OK]   Radarr: no foreign-original movies stuck")
    except Exception as e:
        log(f"[WARN] Radarr dual-audio fallback failed ({e})")

    _save_wanted_state(st)



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
    what = sys.argv[1:] or ["english", "qbit", "dual"]
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

    if "dual" in what:
        if {"sonarr", "radarr"} <= set(jails) and SONARR["key"] and RADARR["key"]:
            dual_audio_fallback()
        else:
            log("[SKIP] dual-audio fallback needs both sonarr and radarr "
                "jails with API keys")


if __name__ == "__main__":
    main()
