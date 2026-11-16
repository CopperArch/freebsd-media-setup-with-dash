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
  exe       Fake-release scan: a torrent whose file list holds an executable
            (.exe, .scr, .msi, ...) is removed and blocklisted through the
            *arr queue so it searches again. Also runs hourly.
  imax      IMAX upgrades: a few owned movies a night are searched for an
            IMAX release, on Radarr profiles that upgrade to IMAX (custom-
            format cutoff 100+). Nothing to do on a stock install.

With no argument all of them run. Jails are reached on their bastille0 IPs, API
keys come from ~/.config/status-dashboard/media-keys.env.
"""
import base64
import json
import os
import re
import subprocess
import shutil
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

from status_keys import installed_jails, jail_ip, load_env

KEYS = load_env()
QBIT = {"url": f"http://{jail_ip('qbittorrent')}:8080",
        "user": KEYS.get("QBIT_USER", "admin"), "pass": KEYS.get("QBIT_PASS", "")}
RADARR = {"url": f"http://{jail_ip('radarr')}:7878", "key": KEYS.get("RADARR_KEY", "")}
SONARR = {"url": f"http://{jail_ip('sonarr')}:8989", "key": KEYS.get("SONARR_KEY", "")}
JELLYFIN = {"url": f"http://{jail_ip('jellyfin')}:8096", "key": KEYS.get("JELLYFIN_KEY", "")}


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


def jf_request(method, path, headers=None, body=None):
    h = {"Authorization": f'MediaBrowser Token="{JELLYFIN['key']}"'}
    h.update(headers or {})
    return request(method, f"{JELLYFIN['url']}{path}", h, body)


def jf_items(itemtype, fields=""):
    f = f"&Fields={fields}" if fields else ""
    items = json.load(jf_request(
        "GET", f"/Items?Recursive=true&IncludeItemTypes={itemtype}"
               f"&CollapseBoxSetItems=false&Limit=30000{f}")).get("Items", [])
    return [i for i in items if i.get("Type") == itemtype]


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


def _qbit_relogin(opener):
    """Log in again through the SAME opener (its cookie jar takes the new SID).

    2026-11-10: the nightly run hit "can't list torrents for cleanup: HTTP
    Error 403" part-way through — the WebUI session had been dropped (qBit
    restart, session timeout or similar; a second login does NOT kill the
    first). On a 403, log in once more and retry the call."""
    body = urllib.parse.urlencode(
        {"username": QBIT["user"], "password": QBIT["pass"]}).encode()
    request("POST", f"{QBIT['url']}/api/v2/auth/login",
            {"Referer": QBIT["url"]}, body, opener, timeout=15)


def _qbit_call(opener, method, path, body=None):
    try:
        return request(method, f"{QBIT['url']}{path}",
                       {"Referer": QBIT["url"]}, body, opener)
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
        _qbit_relogin(opener)
        return request(method, f"{QBIT['url']}{path}",
                       {"Referer": QBIT["url"]}, body, opener)


def qbit_get(opener, path):
    return json.load(_qbit_call(opener, "GET", path))


def qbit_post(opener, path, fields):
    return _qbit_call(opener, "POST", path,
                      urllib.parse.urlencode(fields).encode())


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
    # Paired dual-language cam tags like "CAM.1080p.IT.EN" / "AR.EN": Radarr
    # parses them as English only, so they slipped through.
    r"\b(it|ar|fr|es|de|ru|hi|tr|pl|pt|ua|ko|ja|zh|nl|sv|fa|ur)[\s._-]en\b",
    r"\ben[\s._-](it|ar|fr|es|de|ru|hi|tr|pl|pt|ua|ko|ja|zh|nl|sv|fa|ur)\b",
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


def _spec_value(sp):
    """The "value" field of a custom-format specification (regex, language id)."""
    for f in sp.get("fields", []):
        if f.get("name") == "value":
            return f.get("value")
    return None


def reconcile_english_only(arr, name):
    """Ensure the English-only custom formats exist and are scored
    ENGLISH_ONLY_SCORE in every quality profile, so no release with a
    non-English audio track is ever grabbed."""
    hdr = {"X-Api-Key": arr["key"], "Content-Type": "application/json"}
    try:
        all_cfs = arr_get(arr, "/customformat")
        cfs = {c["name"]: c["id"] for c in all_cfs}
        full = {c["name"]: c for c in all_cfs}
    except Exception as e:
        log(f"[FAIL] {name}: can't read custom formats ({e})")
        return
    for fmt, specs in ENGLISH_ONLY_FORMATS.items():
        if fmt in cfs:
            # Existing format: add any pattern added here since it was
            # created (e.g. the paired cam tags "IT.EN"), or older installs
            # never get them. Patterns a user added are left alone.
            cur = full[fmt]
            have = {(sp.get("implementation"), _spec_value(sp))
                    for sp in cur.get("specifications", [])}
            add = [sp for sp in specs
                   if (sp["implementation"], _spec_value(sp)) not in have]
            if add:
                cur["specifications"] = cur.get("specifications", []) + add
                try:
                    request("PUT", f"{arr['url']}/api/v3/customformat/{cur['id']}",
                            hdr, json.dumps(cur).encode())
                    log(f"[FIX ] {name}: added {len(add)} new pattern(s) to '{fmt}'")
                except Exception as e:
                    log(f"[WARN] {name}: could not update '{fmt}' ({e})")
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

# Owned movies are re-searched for an IMAX release in small nightly batches
# (Radarr only sees upgrades on RSS, and old IMAX releases never reappear
# there). Only profiles that allow upgrades AND have a custom-format cutoff of
# 100+ (an "IMAX" format scored +100) take part, so on a stock install this
# does nothing. 10/night with a 30-day per-movie cooldown.
IMAX_UPGRADE_BATCH = 10
IMAX_RETRY_DAYS = 30


def _wanted_state():
    try:
        with open(WANTED_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_wanted_state(st):
    def ttl(k):
        if k.startswith("radarr-imax:"):
            return IMAX_RETRY_DAYS * 86400
        return DUAL_FALLBACK_RETRY_DAYS * 86400
    now = time.time()
    st = {k: v for k, v in st.items() if v > now - ttl(k)}
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


def _arr_get_slow(arr, path):
    # The fallback runs right after the wanted-search has queued a burst of
    # searches, and a busy Sonarr took >60 s to answer /wanted/missing
    # (2026-11-10 "Sonarr dual-audio fallback failed (timed out)").
    return json.load(request("GET", f"{arr['url']}/api/v3{path}",
                             {"X-Api-Key": arr["key"]}, timeout=240))


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
                  _arr_get_slow(SONARR, "/queue?pageSize=1000").get("records", [])}
        w = _arr_get_slow(SONARR, "/wanted/missing?pageSize=500&monitored=true"
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
                  _arr_get_slow(RADARR, "/queue?pageSize=1000").get("records", [])}
        w = _arr_get_slow(RADARR, "/wanted/missing?pageSize=500&monitored=true")
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



EXEC_FILE_RE = re.compile(
    r"(?i)\.(exe|scr|bat|cmd|com|pif|vbs|vbe|js|jse|wsf|msi|ps1|lnk)$")


def purge_executable_torrents(opener):
    """Remove + blocklist any torrent whose file list contains an executable.

    Some indexers strip punctuation from release names ("...WEB DL ... FLUX"),
    so the *arr title filter never sees the ".exe" -- it only shows up in the
    torrent's own file list. These fakes appear before a film's digital
    release. Removing the torrent through the *arr queue blocklists it and
    lets the *arr search again for a genuine release. The torrent's hash is
    looked up in both queues, so it works whatever categories are set up.
    """
    log("Executable (fake release) scan:")
    try:
        torrents = qbit_get(opener, "/api/v2/torrents/info")
    except Exception as e:
        log(f"  [WARN] can't list torrents for executable scan: {e}")
        return
    hits = []
    for t in torrents:
        if t.get("state") in ("metaDL", "forcedMetaDL"):
            continue
        try:
            files = qbit_get(opener, f"/api/v2/torrents/files?hash={t['hash']}")
        except Exception:
            continue
        bad = [f["name"] for f in files if EXEC_FILE_RE.search(f.get("name", ""))]
        if bad:
            hits.append((t, bad))
    if not hits:
        log("  [OK]   no executables in any torrent")
        return
    queues = []
    for arr, name in ((RADARR, "Radarr"), (SONARR, "Sonarr")):
        if not arr["key"]:
            continue
        try:
            queues.append((arr, name, arr_get(arr, "/queue?pageSize=500")
                           .get("records", [])))
        except Exception as e:
            log(f"  [WARN] {name}: can't read queue ({e})")
    for t, bad in hits:
        removed_via_arr = False
        for arr, name, recs in queues:
            rec = next((r for r in recs if (r.get("downloadId") or "").lower()
                        == t["hash"].lower()), None)
            if not rec:
                continue
            try:
                request("DELETE", f"{arr['url']}/api/v3/queue/{rec['id']}"
                        "?removeFromClient=true&blocklist=true&skipRedownload=false",
                        {"X-Api-Key": arr["key"]})
                removed_via_arr = True
            except Exception as e:
                log(f"  [WARN] {name}: couldn't blocklist {t['name']}: {e}")
            break
        if not removed_via_arr:
            qbit_post(opener, "/api/v2/torrents/delete",
                      {"hashes": t["hash"], "deleteFiles": "true"})
        how = ("removed + blocklisted, re-search queued" if removed_via_arr
               else "deleted from qBittorrent")
        log(f"  [FIX ] MALWARE torrent {t['name']!r} contained {bad[:3]} -- {how}")
        alert(f"Malware torrent caught: {t['name']} contained executable(s) "
              f"{', '.join(bad[:3])}. {how}.")


def imax_upgrade_trickle():
    """Search a few owned movies a night for an IMAX release (see constants).

    Radarr's /wanted/cutoff only judges the QUALITY cutoff, not the custom-
    format one, so build the list here: owned, monitored, on an upgrade
    profile with a custom-format cutoff of 100+, current file not IMAX."""
    log("IMAX upgrades:")
    try:
        profiles = {p["id"] for p in arr_get(RADARR, "/qualityprofile")
                    if p.get("upgradeAllowed")
                    and (p.get("cutoffFormatScore") or 0) >= 100}
        if not profiles:
            log("  [OK]   no Radarr profile upgrades to IMAX -- nothing to do")
            return
        imax_re = re.compile(r"\bIMAX\b", re.I)
        cands = []
        for m in _arr_get_slow(RADARR, "/movie"):
            mf = m.get("movieFile") or {}
            if not (m.get("hasFile") and m.get("monitored")
                    and m.get("qualityProfileId") in profiles):
                continue
            if imax_re.search(f"{mf.get('relativePath', '')} {mf.get('sceneName') or ''}"):
                continue
            cands.append(m)
        st, now, out = _wanted_state(), time.time(), []
        for m in cands:
            if len(out) >= IMAX_UPGRADE_BATCH:
                break
            key = f"radarr-imax:{m['id']}"
            if now - st.get(key, 0) < IMAX_RETRY_DAYS * 86400:
                continue
            st[key] = now
            out.append(m)
        if out:
            request("POST", f"{RADARR['url']}/api/v3/command",
                    {"X-Api-Key": RADARR["key"], "Content-Type": "application/json"},
                    json.dumps({"name": "MoviesSearch",
                                "movieIds": [m["id"] for m in out]}).encode())
            _save_wanted_state(st)
            log(f"  [OK]   IMAX-upgrade search for {len(out)} owned movie(s) "
                f"({len(cands)} without IMAX): "
                + ", ".join(m["title"][:24] for m in out[:5])
                + (" ..." if len(out) > 5 else ""))
        else:
            log(f"  [OK]   IMAX-upgrade batch idle ({len(cands)} without IMAX, "
                "all on cooldown)")
    except Exception as e:
        log(f"  [WARN] Radarr IMAX-upgrade search failed ({e})")


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
    # 2026-11-16: debounce flag -- a persistent fault used to send one email
    # every hour. Now: one "can't reach peers" mail per outage, one on recovery.
    down_flag = os.path.expanduser("~/.local/state/qbit-peers-down.flag")
    was_down = os.path.exists(down_flag)

    def recovered():
        if was_down:
            try:
                os.remove(down_flag)
            except OSError:
                pass
            alert(f"qBittorrent peer connectivity recovered "
                  f"(connection={conn}, DHT nodes={dht})")

    dht_on = prefs.get("dht", False)
    healthy = conn != "disconnected" and (dht > 0 or not dht_on)
    if healthy:
        log(f"[OK]   connection={conn}, DHT nodes={dht}")
        recovered()
        return True
    port = prefs.get("listen_port", 6881)
    for attempt in range(2):
        # 2026-11-16: after a failed vpn handshake (tunnel down ~75s) libtorrent's
        # DHT stopped bootstrapping for good: sockets rebound fine on the new
        # tunnel IP but DHT stayed at 0 nodes for hours of hourly rebinds.
        # Toggling DHT off/on restarts the DHT node; it re-bootstraps in ~20s.
        log(f"[FIX ] connection={conn}, DHT nodes={dht} — rebinding listen "
            f"port {port} + restarting DHT (attempt {attempt + 1})")
        try:
            qbit_post(opener, "/api/v2/app/setPreferences",
                      {"json": json.dumps({"listen_port": port + 1})})
            time.sleep(3)
            qbit_post(opener, "/api/v2/app/setPreferences",
                      {"json": json.dumps({"listen_port": port})})
            if dht_on:
                time.sleep(3)
                qbit_post(opener, "/api/v2/app/setPreferences",
                          {"json": json.dumps({"dht": False})})
                time.sleep(3)
                qbit_post(opener, "/api/v2/app/setPreferences",
                          {"json": json.dumps({"dht": True})})
            time.sleep(60)
            conn, dht = state()
        except Exception as e:
            log(f"[FAIL] rebind failed: {e}")
            continue
        if conn != "disconnected" and (dht > 0 or not dht_on):
            log(f"[OK]   recovered: connection={conn}, DHT nodes={dht}")
            recovered()
            return True
    log(f"[WARN] qBittorrent still has no peers (connection={conn}, "
        f"DHT nodes={dht})")
    if was_down:
        log("[SKIP] alert already sent for this outage")
    else:
        try:
            os.makedirs(os.path.dirname(down_flag), exist_ok=True)
            open(down_flag, "w").write(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
        except OSError:
            pass
        alert(f"qBittorrent can't reach any peers (connection={conn}, DHT "
              f"nodes={dht}) even after rebinding its port and restarting "
              f"DHT — check the vpn jail")
    return False


# 2026-11-16 (user: "on jellyfin ... Cam on the early release ... once it
# updates it removes the cam on the thumbnail", then "add the imdb score at
# the bottom ... yellow star ... so it will show on tv versions of jellyfin").
# Both are burned into the poster image itself so every client (TV apps
# included, which can't run injected JS) shows them.
#   * CAM tag (top-left, red) on movies whose Radarr file is a CAM/TS-class
#     quality. Radarr's quality is the truth: filenames say "TS", "camrip",
#     "CAM.1080p" inconsistently.
#   * IMDb rating (bottom-left, yellow star) on every movie and series, from
#     IMDb's free daily title.ratings dataset matched on the Jellyfin IMDb id.
CAM_QUALITIES = {"CAM", "TELESYNC", "TELECINE", "WORKPRINT", "DVDSCR",
                 "REGIONAL"}
# Under DASHBOARD_HOME: the nightly/hourly routine runs this as root, a manual
# run is the desktop user, and both must share one state dir or a poster whose
# state is missing would be taken as "clean" and badged twice.
POSTER_STATE_DIR = os.path.join(
    os.environ.get("DASHBOARD_HOME") or os.path.expanduser("~"),
    ".local/state/poster-badges")
IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
IMDB_RATINGS_FILE = os.path.join(POSTER_STATE_DIR, "title.ratings.tsv.gz")
IMDB_MIN_VOTES = 10
# Bump when the badge layout changes: every badged poster is redrawn once.
BADGE_STYLE = 2
BADGE_TYPES = ("Movie", "Series")


def _badge_font():
    """A bold TTF for ffmpeg drawtext (DejaVu has the star glyph)."""
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
              "/usr/local/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(f):
            return f
    try:
        f = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"],
                           capture_output=True, text=True, timeout=10).stdout
        return f if f and os.path.exists(f) else None
    except Exception:
        return None


def _render_badges(src, dst, cam, rating, font):
    """Burn the wanted badges into a poster with ffmpeg."""
    f = []
    if cam:
        f += ["drawbox=x=iw*0.05:y=iw*0.05:w=iw*0.36:h=iw*0.15:"
              "color=black@0.45:t=fill",
              "drawbox=x=iw*0.04:y=iw*0.04:w=iw*0.36:h=iw*0.15:"
              "color=0xE50914@1:t=fill",
              "drawbox=x=iw*0.04:y=iw*0.04:w=iw*0.36:h=iw*0.15:"
              "color=white@0.9:t=max(2\\,iw*0.006)",
              f"drawtext=fontfile={font}:text=CAM:fontcolor=white:"
              "fontsize=w*0.105:x=w*0.04+(w*0.36-tw)/2:y=w*0.04+(w*0.15-th)/2"]
    if rating:
        # Top-left (under the CAM tag when there is one), not the bottom: TV
        # apps (Jellyfin Android TV, Wholphin) cover the bottom strip of a
        # poster card with the title/progress overlay, so a bottom badge was
        # invisible there while the CAM tag at the top showed (2026-11-16).
        by = "iw*0.215" if cam else "iw*0.04"
        ty = ("w*0.215" if cam else "w*0.04") + "+(w*0.13-th)/2"
        f += [f"drawbox=x=iw*0.04:y={by}:w=iw*0.30:h=iw*0.13:"
              "color=black@0.72:t=fill",
              f"drawtext=fontfile={font}:text='★':fontcolor=0xF5C518:"
              f"fontsize=w*0.095:x=w*0.065:y={ty}",
              f"drawtext=fontfile={font}:text='{rating}':fontcolor=white:"
              f"fontsize=w*0.085:x=w*0.17:y={ty}"]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", src, "-vf", ",".join(f), "-q:v", "3",
                    "-frames:v", "1", dst], check=True, timeout=60)


def _imdb_ratings(wanted):
    """{tconst: "7.4"} for the ids in `wanted`, from IMDb's daily dataset
    (re-downloaded when older than 20h; a stale copy is used if that fails)."""
    import gzip
    fresh = (os.path.exists(IMDB_RATINGS_FILE) and
             time.time() - os.path.getmtime(IMDB_RATINGS_FILE) < 20 * 3600)
    if not fresh:
        try:
            req = urllib.request.Request(IMDB_RATINGS_URL,
                                         headers={"User-Agent": "media-stack-selfheal"})
            data = urllib.request.urlopen(req, timeout=120).read()
            tmp = IMDB_RATINGS_FILE + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, IMDB_RATINGS_FILE)
        except Exception as e:
            log(f"[WARN] IMDb ratings download failed ({e})"
                + ("; using the previous copy" if os.path.exists(IMDB_RATINGS_FILE) else ""))
    out = {}
    if not os.path.exists(IMDB_RATINGS_FILE):
        return out
    with gzip.open(IMDB_RATINGS_FILE, "rt", encoding="utf-8") as f:
        next(f, None)
        for line in f:
            t, avg, votes = line.rstrip("\n").split("\t")
            if t in wanted and int(votes) >= IMDB_MIN_VOTES:
                out[t] = f"{float(avg):.1f}"
    return out


def poster_badges():
    """Keep Jellyfin posters wearing the right CAM / IMDb-rating badges.

    The clean poster is kept once in POSTER_STATE_DIR/<id>.jpg; the badged
    copy is always rendered from it, so badges never stack. <id>.json holds
    the ImageTag of our upload and the badges it carries. Badges are redrawn
    when they should change (CAM upgraded away, rating moved); a poster with
    no badges wanted gets its original back. If Jellyfin replaced our poster
    itself (new ImageTag), the new image becomes the clean original.
    """
    log("Poster badges (CAM early releases + IMDb rating):")
    os.makedirs(POSTER_STATE_DIR, exist_ok=True)
    import fcntl
    lock = open(os.path.join(POSTER_STATE_DIR, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("[SKIP] another poster-badge run is in progress")
        return
    if not JELLYFIN["key"]:
        log("[SKIP] no JELLYFIN_KEY in media-keys.env")
        return
    font = _badge_font()
    if not font or not shutil.which("ffmpeg"):
        log("[SKIP] needs ffmpeg and a DejaVu Sans Bold font")
        return
    try:
        items = {i["Id"]: i for t in BADGE_TYPES
                 for i in jf_items(t, "ProviderIds")}
    except Exception as e:
        log(f"[FAIL] can't read Jellyfin ({e})")
        return
    try:
        cam = {m["tmdbId"] for m in arr_get(RADARR, "/movie")
               if (((m.get("movieFile") or {}).get("quality") or {})
                   .get("quality") or {}).get("name") in CAM_QUALITIES}
    except Exception as e:
        # No Radarr: rating badges still work; CAM badges stay as they are.
        log(f"[WARN] can't read Radarr ({e}); CAM badges unchanged")
        cam = None
    ratings = _imdb_ratings({(i.get("ProviderIds") or {}).get("Imdb")
                             for i in items.values()} - {None})
    if not ratings:
        log("[WARN] no IMDb ratings available; leaving rating badges as they are")

    state_cam = {}
    if cam is None:
        for fn in os.listdir(POSTER_STATE_DIR):
            if fn.endswith(".json"):
                try:
                    state_cam[fn[:-5]] = json.load(
                        open(os.path.join(POSTER_STATE_DIR, fn))).get("cam")
                except ValueError:
                    pass

    def want(i):
        p = i.get("ProviderIds") or {}
        tm = p.get("Tmdb")
        if cam is None:
            is_cam = bool(state_cam.get(i["Id"]))
        else:
            is_cam = (i.get("Type") == "Movie" and str(tm or "").isdigit()
                      and int(tm) in cam)
        r = ratings.get(p.get("Imdb")) if ratings else None
        return {"cam": is_cam, "rating": r}

    def upload(iid, path):
        with open(path, "rb") as f:
            jf_request("POST", f"/Items/{iid}/Images/Primary",
                       {"Content-Type": "image/jpeg"}, base64.b64encode(f.read()))
        got = json.load(jf_request("GET", f"/Items?Ids={iid}&Fields=ImageTags"))
        return ((got.get("Items") or [{}])[0].get("ImageTags") or {}).get("Primary")

    done = {"badged": 0, "restored": 0, "failed": 0}
    for iid, item in items.items():
        tag = (item.get("ImageTags") or {}).get("Primary")
        if not tag:
            continue
        meta_p = os.path.join(POSTER_STATE_DIR, iid + ".json")
        orig = os.path.join(POSTER_STATE_DIR, iid + ".jpg")
        meta = {}
        if os.path.exists(meta_p):
            try:
                meta = json.load(open(meta_p))
            except ValueError:
                meta = {}
        w = want(item)
        if not ratings and meta.get("rating"):
            w["rating"] = meta["rating"]       # dataset missing: keep as is
        ours = meta.get("tag") == tag
        if (ours and meta.get("cam") == w["cam"] and meta.get("rating") == w["rating"]
                and meta.get("style") == BADGE_STYLE):
            continue
        if not ours and not (w["cam"] or w["rating"]):
            continue                            # untouched poster, nothing wanted
        try:
            if not ours or not os.path.exists(orig):
                # current image is clean (new item, or Jellyfin replaced ours)
                img = jf_request("GET", f"/Items/{iid}/Images/Primary"
                                        "?maxWidth=1000&quality=92&format=Jpg").read()
                with open(orig, "wb") as f:
                    f.write(img)
            if w["cam"] or w["rating"]:
                out = os.path.join(POSTER_STATE_DIR, iid + ".badged.jpg")
                try:
                    _render_badges(orig, out, w["cam"], w["rating"], font)
                    new_tag = upload(iid, out)
                finally:
                    if os.path.exists(out):
                        os.remove(out)
                json.dump({"tag": new_tag, "name": item.get("Name"),
                           "style": BADGE_STYLE, **w},
                          open(meta_p, "w"))
                done["badged"] += 1
                if bool(meta.get("cam")) != w["cam"] and item.get("Type") == "Movie":
                    log(f"[FIX ] {item.get('Name')}: CAM badge "
                        + ("added" if w["cam"] else "removed (upgraded)"))
            else:
                upload(iid, orig)
                for p in (meta_p, orig):
                    os.remove(p)
                done["restored"] += 1
                if meta.get("cam"):
                    log(f"[FIX ] {item.get('Name')}: upgraded from CAM, "
                        "original poster restored")
        except Exception as e:
            done["failed"] += 1
            if done["failed"] <= 5:
                log(f"[WARN] couldn't badge {item.get('Name')} ({e})")
    log(f"[OK]   {len(items)} poster(s) checked: {done['badged']} redrawn, "
        f"{done['restored']} restored, {done['failed']} failed; "
        f"{len(cam) if cam is not None else '?'} CAM movie(s), {len(ratings)} with an IMDb rating")


def _give_state_to_owner():
    """Run as root (the routine): hand the badge state back to the owner of
    DASHBOARD_HOME so the desktop user's own runs can still write it."""
    home = os.environ.get("DASHBOARD_HOME")
    if os.geteuid() != 0 or not home or not os.path.isdir(POSTER_STATE_DIR):
        return
    st = os.stat(home)
    paths = [os.path.dirname(POSTER_STATE_DIR), POSTER_STATE_DIR]
    paths += [os.path.join(POSTER_STATE_DIR, n) for n in os.listdir(POSTER_STATE_DIR)]
    for p in paths:
        try:
            if os.stat(p).st_uid == 0:
                os.chown(p, st.st_uid, st.st_gid)
        except OSError:
            pass


def main():
    # exe + badges also run hourly (daily-routine.sh --quick); the rest nightly.
    what = sys.argv[1:] or ["english", "qbit", "exe", "dual", "imax", "badges"]
    jails = installed_jails(["qbittorrent", "sonarr", "radarr"])
    if "qbit" in what:
        if "qbittorrent" in jails:
            o = qbit_login()
            if o:
                qbit_peer_health(o)
        else:
            log("[SKIP] no qbittorrent jail")
    if "exe" in what:
        if "qbittorrent" in jails:
            o = qbit_login()
            if o:
                purge_executable_torrents(o)
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
    if "imax" in what:
        if "radarr" in jails and RADARR["key"]:
            imax_upgrade_trickle()
        else:
            log("[SKIP] IMAX upgrades need the radarr jail with an API key")
    if "badges" in what:
        # Poster badges (CAM + IMDb rating); also hourly from --quick so an
        # upgraded movie loses its CAM tag within the hour.
        if "jellyfin" in installed_jails(["jellyfin"]):
            try:
                poster_badges()
            finally:
                _give_state_to_owner()
        else:
            log("[SKIP] poster badges need the jellyfin jail")


if __name__ == "__main__":
    main()
