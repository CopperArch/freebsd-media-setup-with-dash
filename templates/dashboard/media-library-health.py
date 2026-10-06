#!/usr/bin/env python3.11
"""media-library-health.py — collections + artwork check for Jellyfin and Plex.

Nightly (daily-routine.sh), after the misfiled-media sort. Three jobs:

1. Jellyfin collections. Jellyfin only auto-adds a movie to its TMDB
   collection when that movie's metadata is refreshed, so most of the library
   (scanned before the option was on) never got grouped: 2026-10-05 only 242
   of 1534 movies were in any of the 65 collections. Here every movie's TMDB
   collection is looked up (via Radarr's metadata proxy, cached in
   ~/.hermes/state/tmdb-collections.json so a normal night only looks up new
   movies), and for every collection with 2+ movies in the library the
   matching Jellyfin collection is created or topped up. Members are only ever
   added — a hand-built collection or hand-added movie is never removed.
2. Plex collections. Keeps the Movies library's "Minimum automatic
   collection size" at 2 (it was Disabled, so Plex showed no collections at
   all). Plex then builds and maintains them itself.
3. Artwork that is set but doesn't load. A populated thumb/ImageTags field
   says nothing about whether the image renders, so every poster in both apps
   is actually fetched; a broken one gets an image refresh. (Items with no
   poster at all are media-stack-selfheal.py's fix_missing_artwork.)
"""
import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# FreeBSD edition: the jail endpoints, API keys and Plex token helper live in
# the dashboard collector (status-collect.py); this stands in for the few
# helpers the Linux edition borrows from media-stack-selfheal.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))      # status_keys
_spec = importlib.util.spec_from_file_location(
    "statuscollect", Path(__file__).resolve().with_name("status-collect.py"))
_sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sc)


class _sh:
    RADARR, SONARR, JELLYFIN = _sc.RADARR, _sc.SONARR, _sc.JELLYFIN
    _alerts = []

    @staticmethod
    def request(method, url, headers=None, data=None, timeout=60):
        return urllib.request.urlopen(urllib.request.Request(
            url, data=data, method=method, headers=headers or {}), timeout=timeout)

    @staticmethod
    def arr_get(arr, path):
        return json.load(_sh.request("GET", f"{arr['url']}/api/v3{path}",
                                     {"X-Api-Key": arr["key"]}))

    @staticmethod
    def alert(msg):
        _sh._alerts.append(msg)

    @staticmethod
    def flush_alerts():
        for m in _sh._alerts:
            print(f"    [ALERT] {m}", flush=True)


RADARR, JELLYFIN = _sh.RADARR, _sh.JELLYFIN

PLEX_URL = _sc.PLEX["url"]
CACHE = Path("{{HOME}}") / ".hermes/state/tmdb-collections.json"
CACHE_DAYS = 60          # re-check a movie's collection membership this often
MIN_COLLECTION = 2       # same threshold Plex is set to


def log(msg):
    print(f"    {msg}", flush=True)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and"))


# ── Jellyfin ─────────────────────────────────────────────────────────────────
def jf(method, path, body=None, **q):
    url = f"{JELLYFIN['url']}{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
    h = {"Authorization": f"MediaBrowser Token={JELLYFIN['key']}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""
    r = urllib.request.urlopen(urllib.request.Request(url, data, h, method=method),
                               timeout=120)
    raw = r.read()
    return json.loads(raw) if raw and raw[:1] in b"{[" else None


def jf_admin():
    return next(u["Id"] for u in jf("GET", "/Users")
                if u["Policy"].get("IsAdministrator"))


def jf_user_items(uid, itemtype, **q):
    items = jf("GET", f"/Users/{uid}/Items", Recursive="true",
               IncludeItemTypes=itemtype, **q)["Items"]
    # IncludeItemTypes=Movie also returns BoxSets on this version — exact match.
    return [i for i in items if i.get("Type") == itemtype]


def tmdb_collection(tmdb_id):
    """(collection tmdbId, title) for a movie, None if it has none."""
    url = f"{RADARR['url']}/api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}"
    d = json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"X-Api-Key": RADARR["key"]}),
        timeout=60))
    c = d.get("collection") or {}
    return (c["tmdbId"], c["title"]) if c.get("tmdbId") else None


# ── identifying movies Jellyfin couldn't match ───────────────────────────────
# Names like "Home Alone 2 Lost In New York - Family Comedy ... Eng Subs" never
# match TMDB, and an unmatched movie can't join its collection (2026-10-06: 37
# such, incl. all of Home Alone 1-5). A few also carry a wrong TMDB id that
# TMDB rejects. Title matches are only trusted when IMDb independently agrees.
JUNK = re.compile(r"(?i)\b(eng(lish)?|rus|multi|subs?|uncut|extended|integral|"
                  r"producers?|directors?|cut|edition|reboot|remastered|"
                  r"alternate|ending|imax|unrated|theatrical|"
                  r"\d{3,4}p|x26[45]|h26[45]|brrip|bluray|web-?dl|hdtv|yify)\b")
GENRE_TAIL = re.compile(r"\s+-\s+[A-Za-z][A-Za-z -]{2,25}$")   # " - Family Comedy"


def clean_title(name):
    t = re.sub(r"[._]", " ", name)
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)
    t = GENRE_TAIL.sub("", t.strip())
    t = JUNK.sub(" ", t)
    t = re.sub(r"\bKings\b", "King's", t)
    return re.sub(r"\s+", " ", t).strip(" -")


def radarr_get(path, **q):
    url = f"{RADARR['url']}/api/v3{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"X-Api-Key": RADARR["key"]}),
        timeout=60))


def imdb_search(title):
    """IMDb's own search suggestions: [(imdb id, title, year)] for films."""
    q = urllib.parse.quote(title.lower()[:60])
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://v3.sg.media-imdb.com/suggestion/x/{q}.json",
            headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
    except Exception:               # noqa: BLE001
        return []
    return [(x["id"], x.get("l"), x.get("y")) for x in d.get("d", [])
            if x.get("qid") in ("movie", "tvMovie", "video")]


def similar(a, b):
    from difflib import SequenceMatcher
    a, b = norm(a), norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0


def identify(title, year):
    """TMDB match for a cleaned title, confirmed by IMDb. Returns the Radarr
    lookup record or None."""
    try:
        results = radarr_get("/movie/lookup", term=f"{title} {year or ''}".strip())
    except Exception:               # noqa: BLE001
        return None
    cands = [r for r in results[:10] if r.get("imdbId") and r.get("tmdbId") and
             (not year or abs((r.get("year") or 0) - year) <= 1)]
    cands.sort(key=lambda r: -similar(title, r["title"]))
    imdb = imdb_search(title) + (imdb_search(f"{title} {year}") if year else [])
    for r in cands[:3]:
        if similar(title, r["title"]) < 0.5:
            break
        # IMDb, searched with OUR title, must offer the same film and year.
        if any(i == r["imdbId"] and (not year or abs((y or 0) - year) <= 1)
               for i, _, y in imdb):
            return r
    return None


def identify_unmatched(uid, movies, cache):
    """Tag unmatched/badly-matched movies with IMDb-confirmed TMDB ids."""
    fixed, unsure = [], []
    now = time.time()
    _, manual_titles = load_manual()
    for m in movies:
        if match_key(m.get("Path"), manual_titles):
            continue                # identified by hand in manual-collections.json
        pid = m.get("ProviderIds") or {}
        t = pid.get("Tmdb")
        bad_id = t and (cache.get(t) or {}).get("bad")
        if t and not bad_id:
            continue
        key = f"jf:{m['Id']}"
        if now - cache.get(key, {}).get("t", 0) < 7 * 86400:
            continue                # tried recently and couldn't confirm
        # Jellyfin's own title may itself be a wrong match (2026-10-06: the
        # "Kraven the Hunter (2024)" file was labelled as a 1974 fan film),
        # so also try the folder and file names, each with its own year.
        tries = [(clean_title(m["Name"]), m.get("ProductionYear"))]
        path = Path(m.get("Path") or "")
        for raw in (path.parent.name, path.stem):
            if not raw or raw.lower() in ("movies", "data", "media"):
                continue
            yr = re.search(r"\b(19[2-9]\d|20[0-4]\d)\b", raw)
            name = raw[:yr.start()] if yr else raw
            tries.append((clean_title(name), int(yr[1]) if yr else None))
        r = None
        for title, year in dict.fromkeys(tries):
            if title and (r := identify(title, year)):
                break
        cache[key] = {"t": now}
        if not r:
            unsure.append(f"{m['Name']} ({m.get('ProductionYear') or '?'})")
            continue
        try:
            item = jf("GET", f"/Users/{uid}/Items/{m['Id']}")
            item.setdefault("ProviderIds", {}).update(
                {"Tmdb": str(r["tmdbId"]), "Imdb": r["imdbId"]})
            jf("POST", f"/Items/{m['Id']}", item)
            # Provider ids are now right, so a full metadata refresh fetches the
            # real title/poster (it's the id-less FullRefresh that renames
            # things to the raw filename — see fix_missing_artwork's notes).
            jf("POST", f"/Items/{m['Id']}/Refresh", metadataRefreshMode="FullRefresh",
               imageRefreshMode="FullRefresh", replaceAllMetadata="true",
               replaceAllImages="false")
            m["ProviderIds"] = item["ProviderIds"]
            c = r.get("collection") or {}
            cache[str(r["tmdbId"])] = {"c": [c["tmdbId"], c["title"]]
                                       if c.get("tmdbId") else None, "t": now}
            fixed.append(f"{m['Name']} -> {r['title']} ({r.get('year')})")
        except Exception as e:      # noqa: BLE001
            unsure.append(f"{m['Name']} ({e})")
    for f in fixed:
        log(f"  [FIX ] identified {f} (TMDB + IMDb agree)")
    if unsure:
        log(f"  [WARN] {len(unsure)} movie(s) couldn't be identified with "
            f"confidence — rename the file to 'Title (Year)' or match it in "
            f"Jellyfin: " + "; ".join(unsure[:15]) + (" …" if len(unsure) > 15 else ""))


def sync_jellyfin_collections():
    log("Jellyfin collections (TMDB franchises with 2+ movies in the library):")
    try:
        uid = jf_admin()
        movies = jf_user_items(uid, "Movie", Fields="ProviderIds,Path")
    except Exception as e:
        log(f"  [FAIL] can't read Jellyfin ({e})")
        return

    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    now = time.time()
    tmdb_ids = {m["Id"]: (m.get("ProviderIds") or {}).get("Tmdb") for m in movies}
    todo = sorted({t for t in tmdb_ids.values() if t and
                   now - cache.get(t, {}).get("t", 0) > CACHE_DAYS * 86400})

    def look(t):
        try:
            return t, tmdb_collection(t), None
        except Exception as e:      # noqa: BLE001
            return t, None, e
    failed = 0
    with ThreadPoolExecutor(6) as ex:
        for t, c, err in ex.map(look, todo):
            if isinstance(err, urllib.error.HTTPError) and err.code in (404, 500):
                # TMDB rejects this id outright — Jellyfin matched the movie
                # wrongly. identify_unmatched() re-identifies it below.
                cache[t] = {"c": None, "bad": True, "t": now}
            elif err:
                failed += 1
                continue
            else:
                cache[t] = {"c": list(c) if c else None, "t": now}
    if todo:
        log(f"  [INFO] looked up {len(todo) - failed} movie(s)"
            + (f", {failed} lookup(s) failed (retried next run)" if failed else ""))

    identify_unmatched(uid, movies, cache)
    tmdb_ids = {m["Id"]: (m.get("ProviderIds") or {}).get("Tmdb") for m in movies}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))

    # Keep each movie's TmdbCollection tag right. 2026-10-06: 1268 movies
    # carried their own movie id there (2 Fast 2 Furious = 584, its own id,
    # not the franchise's 9485). That starved the "TMDb Box Sets" plugin,
    # which also deleted any collection created over the API within seconds
    # ("orphaned box set") — the plugin was uninstalled the same day at the
    # user's request, so this script now owns the collections outright.
    changes = []
    for m in movies:
        t = tmdb_ids.get(m["Id"])
        if not t or t not in cache or cache[t].get("bad"):
            continue
        c = cache[t].get("c")
        want = str(c[0]) if c else None
        have = (m.get("ProviderIds") or {}).get("TmdbCollection")
        if want != have:
            changes.append((m, want))

    def retag(job):
        m, want = job
        try:
            item = jf("GET", f"/Users/{uid}/Items/{m['Id']}")
            pids = item.setdefault("ProviderIds", {})
            if want:
                pids["TmdbCollection"] = want
            else:
                pids.pop("TmdbCollection", None)
            jf("POST", f"/Items/{m['Id']}", item)
            return None
        except Exception as e:      # noqa: BLE001
            return f"{m['Name']}: {e}"
    with ThreadPoolExecutor(4) as ex:
        errs = [e for e in ex.map(retag, changes) if e]
    if changes:
        log(f"  [FIX ] corrected the franchise tag on {len(changes) - len(errs)} "
            f"movie(s)" + (f", {len(errs)} failed: {'; '.join(errs[:5])}" if errs else ""))

    groups = {}
    for mid, t in tmdb_ids.items():
        c = (cache.get(t) or {}).get("c") if t else None
        if c:
            groups.setdefault(c[0], [c[1], []])[1].append(mid)
    groups = {k: v for k, v in groups.items() if len(v[1]) >= MIN_COLLECTION}

    boxsets = jf_user_items(uid, "BoxSet", Fields="ProviderIds")
    by_tmdb = {(b.get("ProviderIds") or {}).get("Tmdb"): b for b in boxsets}
    by_name = {norm(b["Name"]): b for b in boxsets}
    created = topped = added = 0
    for cid, (title, mids) in sorted(groups.items(), key=lambda x: x[1][0]):
        b = by_tmdb.get(str(cid)) or by_name.get(norm(title))
        try:
            if b is None:
                bid = jf("POST", "/Collections", Name=title, Ids=",".join(mids))["Id"]
                created += 1
                added += len(mids)
                log(f"  [FIX ] created '{title}' ({len(mids)} movies)")
            else:
                bid = b["Id"]
                have = {i["Id"] for i in
                        jf("GET", f"/Users/{uid}/Items", ParentId=bid)["Items"]}
                missing = [m for m in mids if m not in have]
                if missing:
                    jf("POST", f"/Collections/{bid}/Items", Ids=",".join(missing))
                    topped += 1
                    added += len(missing)
                    log(f"  [FIX ] '{b['Name']}': added {len(missing)} movie(s)")
                if (b.get("ProviderIds") or {}).get("Tmdb") == str(cid) \
                        and not missing:
                    continue
            # Tag it with its TMDB id so the poster/overview can be fetched,
            # then pull metadata (only fills gaps) and artwork for it.
            item = jf("GET", f"/Users/{uid}/Items/{bid}")
            if (item.get("ProviderIds") or {}).get("Tmdb") != str(cid):
                item.setdefault("ProviderIds", {})["Tmdb"] = str(cid)
                jf("POST", f"/Items/{bid}", item)
            jf("POST", f"/Items/{bid}/Refresh", metadataRefreshMode="Default",
               imageRefreshMode="Default", replaceAllImages="false")
        except Exception as e:      # noqa: BLE001
            log(f"  [WARN] '{title}': {e}")

    # Verify against what Jellyfin actually holds now, not what was sent.
    time.sleep(5)
    boxsets = jf_user_items(uid, "BoxSet", Fields="ProviderIds")
    present = {(b.get("ProviderIds") or {}).get("Tmdb") for b in boxsets} | \
              {norm(b["Name"]) for b in boxsets}
    lost = [t for cid, (t, _) in groups.items()
            if str(cid) not in present and norm(t) not in present]
    if lost:
        log(f"  [WARN] {len(lost)} collection(s) missing right after creation "
            f"— something is removing them: " + ", ".join(sorted(lost)[:15]))
        _sh.alert(f"{len(lost)} Jellyfin collection(s) vanished after creation")
    elif created or topped:
        log(f"  [OK]   {created} collection(s) created, {topped} topped up, "
            f"{added} movie(s) added — all {len(groups)} franchise collections present")
    else:
        log(f"  [OK]   all {len(groups)} franchise collections present and complete")


# ── hand-made groupings (no TMDB franchise) ──────────────────────────────────
MANUAL = Path("{{HOME}}") / ".config/media-library/manual-collections.json"


def load_manual():
    try:
        d = json.loads(MANUAL.read_text())
        return d.get("collections", {}), d.get("titles", {})
    except (OSError, ValueError):
        return {}, {}


def match_key(path, keys):
    return next((k for k in keys if k.lower() in (path or "").lower()), None)


def apply_manual_jellyfin():
    colls, titles = load_manual()
    if not colls and not titles:
        return
    log("Jellyfin hand-made collections (manual-collections.json):")
    try:
        uid = jf_admin()
        movies = jf_user_items(uid, "Movie", Fields="Path")
        boxsets = {norm(b["Name"]): b for b in jf_user_items(uid, "BoxSet")}
    except Exception as e:          # noqa: BLE001
        log(f"  [FAIL] can't read Jellyfin ({e})")
        return
    fixes = 0
    for m in movies:
        k = match_key(m.get("Path"), titles)
        if not k:
            continue
        name, year = titles[k]
        if m["Name"] == name and m.get("ProductionYear") == year:
            continue
        item = jf("GET", f"/Users/{uid}/Items/{m['Id']}")
        item.update({"Name": name, "ProductionYear": year})
        item["LockedFields"] = sorted(set(item.get("LockedFields") or []) | {"Name"})
        jf("POST", f"/Items/{m['Id']}", item)
        fixes += 1
    for cname, keys in colls.items():
        ids = [m["Id"] for m in movies if match_key(m.get("Path"), keys)]
        if not ids:
            log(f"  [WARN] '{cname}': none of its files are in the library")
            continue
        b = boxsets.get(norm(cname))
        if b is None:
            jf("POST", "/Collections", Name=cname, Ids=",".join(ids))
            fixes += 1
            log(f"  [FIX ] created '{cname}' ({len(ids)} items)")
            continue
        have = {i["Id"] for i in jf("GET", f"/Users/{uid}/Items", ParentId=b["Id"])["Items"]}
        missing = [i for i in ids if i not in have]
        if missing:
            jf("POST", f"/Collections/{b['Id']}/Items", Ids=",".join(missing))
            fixes += 1
            log(f"  [FIX ] '{cname}': added {len(missing)} item(s)")
    if not fixes:
        log(f"  [OK]   {len(colls)} hand-made collection(s) and titles in place")


def apply_manual_plex(tok):
    colls, titles = load_manual()
    if not colls and not titles:
        return
    log("Plex hand-made collections (manual-collections.json):")
    fixes = 0
    for s in plex(tok, "/library/sections").get("Directory", []):
        if s["type"] != "movie":
            continue
        sec = s["key"]
        for i in plex(tok, f"/library/sections/{sec}/all?type=1").get("Metadata", []):
            files = [p.get("file", "") for md in i.get("Media") or []
                     for p in md.get("Part") or []]
            tkeys = sorted({k for f in files if (k := match_key(f, titles))})
            tkey = tkeys[0] if tkeys else None
            ckeys = {c for f in files for c, keys in colls.items()
                     if match_key(f, keys)}
            if not tkey and not ckeys:
                continue
            rk = i["ratingKey"]
            q = {}
            if tkey:
                name, year = titles[tkey]
                if len(tkeys) > 1:
                    # Plex stacks "Part 1"/"Part 2" files into one entry, so
                    # it gets the shared title rather than the first part's.
                    name = re.sub(r"\s*-\s*Part\s*\d+$", "", name)
                if i.get("title") != name or i.get("year") != year \
                        or not i.get("originallyAvailableAt"):
                    # A wrong online match (e.g. a Police Squad! episode
                    # matched to "A Dinner Date") would keep pulling that
                    # film's poster/summary back — unmatch it first.
                    if i.get("guid", "").startswith("plex://"):
                        plex(tok, f"/library/metadata/{rk}/unmatch", method="PUT", raw=True)
                    # Plex only locks the year through the release date;
                    # a bare year.locked is silently ignored and the next
                    # metadata refresh wipes it.
                    q.update({"title.value": name, "title.locked": 1,
                              "titleSort.value": name,
                              "originallyAvailableAt.value": f"{year}-01-01",
                              "originallyAvailableAt.locked": 1})
            if ckeys:
                cur = [c["tag"] for c in (plex(tok, f"/library/metadata/{rk}")
                                          .get("Metadata", [{}])[0].get("Collection") or [])]
                if not ckeys <= set(cur):
                    for n, tag in enumerate(sorted(set(cur) | ckeys)):
                        q[f"collection[{n}].tag.tag"] = tag
                    q["collection.locked"] = 1
            if q:
                q.update({"type": 1, "id": rk})
                plex(tok, f"/library/sections/{sec}/all?{urllib.parse.urlencode(q)}",
                     method="PUT", raw=True)
                fixes += 1
                log(f"  [FIX ] {i.get('title')} -> "
                    + ", ".join(filter(None, [q.get('title.value', ''),
                                              ' + '.join(sorted(ckeys)) if any(k.startswith('collection[') for k in q) else ''])))
    if not fixes:
        log(f"  [OK]   {len(colls)} hand-made collection(s) and titles in place")


def check_jellyfin_images():
    log("Jellyfin artwork (every poster actually fetched):")
    try:
        uid = jf_admin()
        items = [i for t in ("Movie", "Series", "Season", "Episode", "BoxSet")
                 for i in jf_user_items(uid, t, Fields="ImageTags")]
    except Exception as e:
        log(f"  [FAIL] can't read Jellyfin ({e})")
        return

    def ok(i):
        tag = (i.get("ImageTags") or {}).get("Primary")
        if not tag:
            return i, True          # no poster at all: fix_missing_artwork's job
        try:
            r = urllib.request.urlopen(
                f"{JELLYFIN['url']}/Items/{i['Id']}/Images/Primary"
                f"?maxWidth=80&tag={tag}", timeout=60)
            return i, r.status == 200 and len(r.read(64)) > 0
        except Exception:           # noqa: BLE001
            return i, False
    with ThreadPoolExecutor(8) as ex:
        bad = [i for i, good in ex.map(ok, items) if not good]
    for i in bad:
        try:
            jf("POST", f"/Items/{i['Id']}/Refresh", metadataRefreshMode="None",
               imageRefreshMode="FullRefresh", replaceAllImages="true")
        except Exception:           # noqa: BLE001
            pass
    if bad:
        log(f"  [FIX ] {len(bad)} broken poster(s) re-fetched: "
            + ", ".join(i["Name"] for i in bad[:10]))
        _sh.alert(f"{len(bad)} Jellyfin poster(s) were broken and re-fetched")
    else:
        log(f"  [OK]   all {len(items)} posters load")


# ── Plex ─────────────────────────────────────────────────────────────────────
def plex_token():
    return _sc.plex_token()


def plex(tok, path, method="GET", raw=False):
    sep = "&" if "?" in path else "?"
    r = urllib.request.urlopen(urllib.request.Request(
        f"{PLEX_URL}{path}{sep}X-Plex-Token={tok}",
        headers={"Accept": "application/json"}, method=method), timeout=60)
    return r if raw else json.load(r).get("MediaContainer", {})


def check_plex(tok):
    log("Plex collections + artwork (every thumbnail actually fetched):")
    sections = plex(tok, "/library/sections").get("Directory", [])
    items = []
    for s in sections:
        k = s["key"]
        if s["type"] == "movie":
            pref = {p["id"]: p["value"] for p in
                    plex(tok, f"/library/sections/{k}/prefs").get("Setting", [])}
            if str(pref.get("autoCollectionThreshold")) != str(MIN_COLLECTION):
                plex(tok, f"/library/sections/{k}/prefs?autoCollectionThreshold="
                          f"{MIN_COLLECTION}", method="PUT", raw=True)
                # Collections are only built when metadata is refreshed.
                plex(tok, f"/library/sections/{k}/refresh?force=1", raw=True)
                log(f"  [FIX ] '{s['title']}': automatic collections were off "
                    f"— set to {MIN_COLLECTION}+ movies, full metadata refresh started")
            types = [1]
        elif s["type"] == "show":
            types = [2, 3, 4]
        else:
            continue
        for t in types:
            items += plex(tok, f"/library/sections/{k}/all?type={t}").get("Metadata", [])
        items += plex(tok, f"/library/sections/{k}/collections").get("Metadata", [])
        ncol = len(plex(tok, f"/library/sections/{k}/collections").get("Metadata", []))
        log(f"  [INFO] '{s['title']}': {ncol} collection(s)")

    def ok(i):
        if not i.get("thumb"):
            return i, False
        try:
            r = plex(tok, i["thumb"], raw=True)
            return i, r.status == 200 and len(r.read(64)) > 0
        except Exception:           # noqa: BLE001
            return i, False
    with ThreadPoolExecutor(12) as ex:
        bad = [i for i, good in ex.map(ok, items) if not good]
    for i in bad:
        try:
            plex(tok, f"/library/metadata/{i['ratingKey']}/refresh",
                 method="PUT", raw=True)
        except Exception:           # noqa: BLE001
            pass
    if bad:
        log(f"  [FIX ] {len(bad)} missing/broken thumbnail(s) refreshed: "
            + ", ".join(i.get("title", "?") for i in bad[:10]))
        _sh.alert(f"{len(bad)} Plex thumbnail(s) were missing/broken and refreshed")
    else:
        log(f"  [OK]   all {len(items)} thumbnails load")


def main():
    print("  --- media library health ---", flush=True)
    sync_jellyfin_collections()
    try:
        apply_manual_jellyfin()
    except Exception as e:          # noqa: BLE001
        log(f"  [FAIL] hand-made Jellyfin collections ({e})")
    check_jellyfin_images()
    tok = plex_token()
    if tok:
        try:
            apply_manual_plex(tok)
            check_plex(tok)
        except Exception as e:      # noqa: BLE001
            log(f"  [FAIL] Plex check failed ({e})")
    else:
        log("  [FAIL] Plex token not found (container down?) — skipped")
    _sh.flush_alerts()
    return 0


if __name__ == "__main__":
    sys.exit(main())
