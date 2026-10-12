#!/usr/bin/env python3.11
"""jellyfin-duplicates.py — find & prune duplicate movies / TV episodes, and
de-duplicate repeated collections, via the Jellyfin REST API.

  Report only (default, safe):   jellyfin-duplicates.py
  Actually delete losing files:  jellyfin-duplicates.py --apply

"Best" version kept = highest resolution, tie-broken by bitrate, then size,
then "the copy Sonarr/Radarr tracks". Movies are matched on TMDb id (name +
year only as a fallback); episodes on series + SxxEyy, and only files whose
own filename carries that SxxEyy. A group whose running times disagree is a
misidentified title, not a duplicate, and is never touched.
Losing copies are DELETED FROM DISK (irreversible). Collections that repeat
by name are removed via the API (metadata only — no media is touched).

Auth: set JELLYFIN_API_KEY, or put the key in ~/.dbmedia/jellyfin-api-key
(Jellyfin Dashboard -> Administration -> API Keys -> +). Without one this
does nothing, so the nightly routine stays hands-off until you opt in.
"""
import os, re, sys, json, time, importlib.util, urllib.parse, urllib.request
from pathlib import Path

# FreeBSD edition: jail endpoints and the Sonarr/Radarr keys live in the
# dashboard collector (status-collect.py), installed next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))      # status_keys
_spec = importlib.util.spec_from_file_location(
    "statuscollect", Path(__file__).resolve().with_name("status-collect.py"))
_sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sc)

KEY_FILE = Path("{{HOME}}") / ".dbmedia/jellyfin-api-key"
BASE = os.environ.get("JELLYFIN_URL", _sc.JELLYFIN["url"]).rstrip("/")
KEY = os.environ.get("JELLYFIN_API_KEY", "")
if not KEY and KEY_FILE.is_file():
    KEY = KEY_FILE.read_text().strip()
if not KEY:
    # Deliberately NOT falling back to the collector's Jellyfin key: creating
    # this file is the opt-in for a job that deletes media.
    print(f"  [SKIP] No Jellyfin API key. Set JELLYFIN_API_KEY or create {KEY_FILE}")
    print("         (Dashboard -> Administration -> API Keys -> +). Skipping.")
    sys.exit(0)
APPLY = "--apply" in sys.argv[1:]

# Jail path -> host path: every jail mounts the media pool at /data/media.
PATH_MAP = [(c, h) for c, h in (("/data/media", "{{MEDIA_POOL}}"),) if h.startswith("/")]
# rm is only ever allowed under these roots.
SAFE_ROOTS = tuple(sorted({h.rstrip("/") + "/" for _, h in PATH_MAP}))

def api(path, params=None, method="GET"):
    q = dict(params or {})
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}" if q else f"{BASE}{path}"
    # Jellyfin 12 dropped both the X-Emby-Token header and ?api_key= query
    # param auth methods (both silently 401 now) — the only method still
    # accepted is the Authorization header's Token= field.
    req = urllib.request.Request(url, method=method,
                                 headers={"Authorization": f'MediaBrowser Token="{KEY}"'})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else None

def to_host(path):
    if not path:
        return None
    for c, h in PATH_MAP:
        if path.startswith(c):
            return h + path[len(c):]
    return path  # already a host path (or unmapped)

def video_stream(ms):
    for s in ms.get("MediaStreams") or []:
        if s.get("Type") == "Video":
            return s
    return {}

def collect_versions(item):
    """Yield one record per physical media file backing an item."""
    for ms in item.get("MediaSources") or []:
        vs = video_stream(ms)
        w = vs.get("Width") or 0
        h = vs.get("Height") or 0
        yield {
            "item_id":   item["Id"],
            "name":      item.get("Name", "?"),
            "path":      ms.get("Path"),
            "host":      to_host(ms.get("Path")),
            "width":     w,
            "height":    h,
            "res":       w * h,
            "bitrate":   ms.get("Bitrate") or vs.get("BitRate") or 0,
            "size":      ms.get("Size") or 0,
            "runtime":   (ms.get("RunTimeTicks") or 0) / 6e8,   # minutes
        }

def rank(v):
    # Higher is better: resolution, then bitrate, then size; on a dead tie
    # (e.g. two hardlinks of one file) keep the copy Sonarr/Radarr tracks.
    return (v["res"], v["bitrate"], v["size"], v["host"] in TRACKED)

def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"

def norm(s):
    return (s or "").strip().lower()

# ── Sonarr/Radarr: which files do they actually track? ───────────────────────
# Guessing this from the folder name (anything under "Season N/" or
# "Title (Year)/" counts as *arr-managed and is never deleted) skips nearly
# every losing copy, since most libraries are laid out exactly that way. So
# the *arrs are asked directly: an untracked loser is simply deleted; a tracked loser is
# removed through the *arr API and the parent rescanned so it adopts the
# keeper instead of marking the title missing.
TRACKED = {}        # host path -> (arr name, file id, parent id, parent host folder)
ARR = {}            # arr name -> {"url":…, "key":…}; empty = *arrs unreachable

def arr(name, path, method="GET", body=None):
    a = ARR[name]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{a['url']}/api/v3{path}", data=data, method=method,
                                 headers={"X-Api-Key": a["key"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = r.read()
        return json.loads(out) if out else None

def load_tracked():
    try:
        ARR["radarr"], ARR["sonarr"] = _sc.RADARR, _sc.SONARR
        for m in arr("radarr", "/movie"):
            mf = m.get("movieFile")
            if mf and mf.get("path"):
                TRACKED[to_host(mf["path"])] = ("radarr", mf["id"], m["id"], to_host(m["path"]))
        for s in arr("sonarr", "/series"):
            for f in arr("sonarr", f"/episodefile?seriesId={s['id']}"):
                TRACKED[to_host(f["path"])] = ("sonarr", f["id"], s["id"], to_host(s["path"]))
        print(f"  Sonarr/Radarr track {len(TRACKED)} files")
    except Exception as ex:
        ARR.clear(); TRACKED.clear()
        print(f"  [WARN] Sonarr/Radarr unreachable ({ex}) — falling back to the "
              f"folder-name guess, *arr-style paths will be skipped")

_SEASON_DIR_RE = re.compile(r"(^|/)season\s*\d+(/|$)", re.IGNORECASE)
_YEAR_PARENT_RE = re.compile(r"\(\d{4}\)$")

def looks_arr_managed(path):
    """Fallback only, used when the *arr APIs can't be reached."""
    if _SEASON_DIR_RE.search(path):
        return True
    return bool(_YEAR_PARENT_RE.search(os.path.basename(os.path.dirname(path))))

def same_file(a, b):
    try:
        sa, sb = os.stat(a), os.stat(b)
        return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)
    except OSError:
        return False

# Two real copies of one title run the same length. A bigger gap means Jellyfin
# matched two different films/episodes to one entry (a remake and its original).
RUNTIME_TOLERANCE = 0.05

reclaim = 0
to_delete = []          # dicts: host, size, label, keeper, how ("rm" | "arr")
keep_paths = set()      # never delete something we've chosen to keep

def process_group(label, versions):
    """versions: list of file records that are the same logical title."""
    global reclaim
    # De-duplicate by host path (same file referenced twice = not a dup).
    seen, uniq = set(), []
    for v in versions:
        if v["host"] and v["host"] not in seen:
            seen.add(v["host"]); uniq.append(v)
    if len(uniq) < 2:
        return
    rts = [v["runtime"] for v in uniq if v["runtime"]]
    if len(rts) >= 2 and max(rts) - min(rts) > RUNTIME_TOLERANCE * max(rts):
        print(f"  MISMATCH  {label}: running times differ — different titles "
              f"matched to one entry, nothing deleted (re-identify in Jellyfin)")
        for v in uniq:
            print(f"        {v['runtime']:.0f}min  {v['host']}")
        return
    ranked = sorted(uniq, key=rank, reverse=True)
    best = ranked[0]
    if best["res"] == 0:
        print(f"  [WARN] {label}: cannot determine resolution for any copy — skipping")
        return
    keep_paths.add(best["host"])
    print(f"  DUP  {label}")
    print(f"        KEEP  {best['width']}x{best['height']} {human(best['bitrate'])}/s "
          f"{human(best['size'])}  {best['host']}")
    for v in ranked[1:]:
        losing = v["host"]
        if not losing or losing in keep_paths:
            continue
        if not any(losing.startswith(r) for r in SAFE_ROOTS):
            print(f"        SKIP  outside safe roots: {losing}")
            continue
        if not os.path.isfile(losing):
            print(f"        SKIP  file not found on disk: {losing}")
            continue
        how, size, note = "rm", v["size"], ""
        if same_file(losing, best["host"]):
            size, note = 0, "  (hardlink of the kept file — frees no space)"
        if not ARR:
            if looks_arr_managed(losing):
                print(f"        SKIP  Sonarr/Radarr-style path, not deleting: {losing}")
                continue
        elif losing in TRACKED:
            name, _, _, folder = TRACKED[losing]
            if best["host"] not in TRACKED and name == "sonarr" \
                    and not best["host"].startswith(folder.rstrip("/") + "/"):
                print(f"        SKIP  Sonarr tracks this copy and the better one is "
                      f"outside its series folder: {losing}")
                continue
            how, note = "arr", note + f"  (via {name}, which then adopts the kept copy)"
        print(f"        {'DEL ' if APPLY else 'WOULD DEL'}  "
              f"{v['width']}x{v['height']} {human(v['bitrate'])}/s "
              f"{human(v['size'])}  {losing}{note}")
        to_delete.append({"host": losing, "size": size, "label": label,
                          "keeper": best["host"], "how": how})
        reclaim += size

load_tracked()

# ── Movies ───────────────────────────────────────────────────────────────────
print("── Movies ──")
movies = api("/Items", {
    "IncludeItemTypes": "Movie", "Recursive": "true",
    "Fields": "MediaSources,Path,ProductionYear,ProviderIds",
    # Jellyfin 12 folds every movie that belongs to a collection into its
    # BoxSet unless told not to — without this a large share of the library
    # is invisible here and movie duplicates are never found.
    "CollapseBoxSetItems": "false",
}).get("Items", [])
groups, labels = {}, {}
for m in movies:
    if m.get("Type") != "Movie":
        continue
    # Same TMDb id = same film, whatever the two files happen to be titled.
    tmdb = (m.get("ProviderIds") or {}).get("Tmdb")
    key = ("tmdb", tmdb) if tmdb else (norm(m.get("Name")), m.get("ProductionYear"))
    labels.setdefault(key, f"{m.get('Name')} ({m.get('ProductionYear')})")
    groups.setdefault(key, []).extend(collect_versions(m))
for key, vers in sorted(groups.items(), key=lambda kv: labels[kv[0]].lower()):
    process_group(labels[key], vers)

# ── TV episodes ──────────────────────────────────────────────────────────────
print("── TV Episodes ──")
eps = api("/Items", {
    "IncludeItemTypes": "Episode", "Recursive": "true",
    "Fields": "MediaSources,Path,SeriesName,ParentIndexNumber,IndexNumber",
}).get("Items", [])
_EP_TAG_RE = re.compile(r"s(\d{1,2})[ ._-]?e(\d{1,3})|(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)",
                        re.IGNORECASE)

def name_says(path, s, ep):
    for m in _EP_TAG_RE.finditer(os.path.basename(path or "")):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if (int(a), int(b)) == (s, ep):
            return True
    return False

groups = {}
for e in eps:
    s, ep = e.get("ParentIndexNumber"), e.get("IndexNumber")
    if s is None or ep is None:
        continue  # can't safely identify the episode
    key = (norm(e.get("SeriesName")), s, ep)
    # Only trust files whose own name says SxxEyy: a file Jellyfin had to guess
    # at ("possession-0102-720-web" -> S07E20) can collide with other guesses
    # that are really different episodes.
    groups.setdefault(key, []).extend(
        v for v in collect_versions(e) if name_says(v["host"], s, ep))
for (series, s, ep), vers in sorted(groups.items()):
    process_group(f"{series} S{s:02d}E{ep:02d}", vers)

# ── Collections (BoxSets) ────────────────────────────────────────────────────
print("── Collections ──")
boxsets = api("/Items", {
    "IncludeItemTypes": "BoxSet", "Recursive": "true",
    "Fields": "ChildCount",
}).get("Items", [])
cgroups = {}
for b in boxsets:
    cgroups.setdefault(norm(b.get("Name")), []).append(b)
coll_deleted = 0
for name, sets in sorted(cgroups.items()):
    if len(sets) < 2:
        continue
    # Keep the one with the most children; delete the rest.
    sets.sort(key=lambda b: b.get("ChildCount") or 0, reverse=True)
    keep = sets[0]
    print(f"  DUP COLLECTION '{keep.get('Name')}' x{len(sets)} — "
          f"keeping id {keep['Id']} ({keep.get('ChildCount',0)} items)")
    for b in sets[1:]:
        print(f"        {'DEL ' if APPLY else 'WOULD DEL'} collection "
              f"id {b['Id']} ({b.get('ChildCount',0)} items)")
        if APPLY:
            try:
                api(f"/Items/{b['Id']}", method="DELETE")
                coll_deleted += 1
            except Exception as ex:
                print(f"        ERROR deleting collection {b['Id']}: {ex}")

# ── Apply deletions ──────────────────────────────────────────────────────────
print("── Summary ──")
print(f"  Duplicate media files: {len(to_delete)}  |  "
      f"reclaimable: {human(reclaim)}")
print(f"  Duplicate collections: "
      f"{sum(len(v)-1 for v in cgroups.values() if len(v) > 1)}")

SIDECAR_EXT = (".nfo", ".jpg", ".png")

def remove_sidecars(path):
    """Drop the artwork/.nfo that belonged to a deleted file (never subtitles)."""
    d, stem = os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]
    for f in os.listdir(d):
        if f.startswith(stem) and f[len(stem):len(stem) + 1] in (".", "-") \
                and f.lower().endswith(SIDECAR_EXT):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

if APPLY:
    freed = 0
    rescan = {}         # (arr name, parent id) -> [keeper paths to verify]
    for d in to_delete:
        path = d["host"]
        try:
            if d["how"] == "arr":
                name, file_id, parent_id, folder = TRACKED[path]
                kind = "moviefile" if name == "radarr" else "episodefile"
                arr(name, f"/{kind}/{file_id}", method="DELETE")
                keeper = d["keeper"]
                # Radarr only adopts a file that sits inside the movie's folder.
                if name == "radarr" and not keeper.startswith(folder.rstrip("/") + "/"):
                    dest = os.path.join(folder, os.path.basename(keeper))
                    if not os.path.exists(dest):
                        os.makedirs(folder, exist_ok=True)
                        os.rename(keeper, dest)
                        print(f"  MOVED   {keeper} -> {dest}")
                        keeper = dest
                if keeper not in TRACKED:
                    rescan.setdefault((name, parent_id), []).append(keeper)
            else:
                os.remove(path)
                remove_sidecars(path)
            freed += d["size"]
            print(f"  DELETED {path}")
        except Exception as ex:
            print(f"  ERROR deleting {path}: {ex}")
    # Have the *arrs pick up the kept copies, then check that they did — an
    # unadopted keeper leaves the title "missing" and it gets downloaded again.
    for (name, parent_id) in rescan:
        cmd = {"name": "RescanMovie", "movieId": parent_id} if name == "radarr" \
            else {"name": "RescanSeries", "seriesId": parent_id}
        try:
            arr(name, "/command", method="POST", body=cmd)
        except Exception as ex:
            print(f"  [WARN] {name} rescan failed: {ex}")
    if rescan:
        time.sleep(45)
    for (name, parent_id), keepers in rescan.items():
        try:
            if name == "radarr":
                now = {to_host(f["path"]) for f in arr(name, f"/moviefile?movieId={parent_id}")}
            else:
                now = {to_host(f["path"]) for f in arr(name, f"/episodefile?seriesId={parent_id}")}
            for k in keepers:
                print(f"  {'[OK]   ' + name + ' adopted' if k in now else '[WARN] ' + name + ' has NOT adopted'} {k}")
        except Exception as ex:
            print(f"  [WARN] could not verify {name} adoption: {ex}")
    print(f"  Freed {human(freed)} from disk; removed {coll_deleted} collection(s).")
    # Nudge Jellyfin to reconcile its library with what's now on disk.
    try:
        api("/Library/Refresh", method="POST")
        print("  Triggered Jellyfin library refresh.")
    except Exception as ex:
        print(f"  Could not trigger library refresh: {ex}")
else:
    print("  DRY RUN — nothing deleted. Re-run with --apply to delete.")
