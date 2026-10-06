#!/usr/bin/env python3.11
"""media-library-sort.py — move misfiled movies/episodes to the right library.

A TV episode that lands in Movies/ (or a movie in Tv Shows/) shows up in the
wrong Plex/Jellyfin library, or not at all. This finds them and moves them:

  Movies/ -> Tv Shows/   a video whose name has a real episode tag (S05E06,
                         5x06) AND that Sonarr's parser reads as an episode.
                         Goes into the show's existing folder (matched via
                         Sonarr, else by normalised folder name), following
                         that show's own layout (Season N/ vs loose files).
  Tv Shows/ -> Movies/   a loose video, or a show folder holding exactly one
                         video with no episode/part tag, no season folders,
                         not a Sonarr series, and that Radarr parses as a
                         movie with a year.

Safety:
  * dry run unless --apply
  * every move is a rename on the physical disk the file already lives on
    (a mergerfs branch, or the pool dataset itself), never a copy through a
    union mount
  * never overwrites — a name clash is skipped and reported
  * skips anything modified in the last 2h (may still be importing)
  * skips files Radarr/Sonarr manage (moving those makes the *arr think the
    file vanished and re-download it) — reported for a manual look instead
  * same-name sidecars (.nfo, .srt, -thumb.jpg, ...) travel with the video
"""
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

POOL = Path("{{MEDIA_POOL}}")


def pool_disks(pool):
    """The physical branches behind a mergerfs pool, or just the pool itself
    when it's a plain disk (renames then simply happen on it)."""
    try:
        raw = os.getxattr(pool / ".mergerfs", "user.mergerfs.srcmounts")
        return [Path(p) for p in raw.decode().split(":") if p]
    except OSError:
        return [pool]


def library_dir(pool, names, default):
    """Find the library folder whatever its capitalisation/wording."""
    try:
        for d in sorted(pool.iterdir()):
            if d.is_dir() and d.name.lower() in names:
                return d.name
    except OSError:
        pass
    return default


DISKS = pool_disks(POOL)
MOVIES = library_dir(POOL, ("movies", "films"), "Movies")
TV = library_dir(POOL, ("tv shows", "tv", "shows", "tv series", "series"),
                 "Tv Shows")
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm",
         ".mpg", ".mpeg"}
MIN_AGE = 2 * 3600
# Episode tag with a clear boundary: S05E06 / S5.E6 / 5x06 (not 1920x1080).
EP_TAG = re.compile(r"(?i)(?<![a-z0-9])(s\d{1,2}[ ._-]?e\d{1,3}|\d{1,2}x\d{2,3})"
                    r"(?![a-z0-9])")
# Names that look like one piece of a multi-part show rather than a movie.
PART_TAG = re.compile(r"(?i)(?<![a-z])(part|pt|episode|ep|chapter|vol)[ ._-]*\d")
SEASON_DIR = re.compile(r"(?i)^(season[ ._-]*\d+|s\d{1,2}$|specials$)")

APPLY = "--apply" in sys.argv

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


RADARR, SONARR = _sh.RADARR, _sh.SONARR


def log(msg):
    print(msg, flush=True)


def arr_get(arr, path, **params):
    q = f"?{urllib.parse.urlencode(params)}" if params else ""
    return _sh.arr_get(arr, f"{path}{q}")


def arr_command(arr, name, **body):
    _sh.request("POST", f"{arr['url']}/api/v3/command",
                {"X-Api-Key": arr["key"], "Content-Type": "application/json"},
                json.dumps({"name": name, **body}).encode())


def to_host(p):
    """*arr container path -> host path (see docker-compose volume mounts)."""
    m = re.match(r"^/data/disk(\d)/(.*)$", p)
    if m and 0 < int(m[1]) <= len(DISKS):
        return DISKS[int(m[1]) - 1] / m[2]
    if p.startswith("/data/media/"):
        return POOL / p[len("/data/media/"):]
    return Path(p)


def to_pool(p):
    """Any host path (per-disk or pool) -> the pool path mergerfs shows."""
    p = Path(p)
    for d in DISKS:
        if p.is_relative_to(d):
            return POOL / p.relative_to(d)
    return p


def norm(title):
    t = re.sub(r"\(\d{4}\)|\b(19|20)\d{2}\b", "", title.lower())
    t = re.sub(r"^the\b|&|\band\b", "", t.strip())
    return re.sub(r"[^a-z0-9]", "", t)


def is_video(p):
    return p.suffix.lower() in VIDEO and "sample" not in p.name.lower()


def videos(folder):
    return [p for p in folder.rglob("*") if p.is_file() and is_video(p)]


def too_new(p):
    return time.time() - p.stat().st_mtime < MIN_AGE


def disk_of(pool_path):
    """Physical disk holding this file (mergerfs keeps one copy per file)."""
    rel = pool_path.relative_to(POOL)
    for d in DISKS:
        if (d / rel).exists():
            return d
    return None


def sidecars(video):
    """Files sharing the video's stem: .nfo, .srt, .en.srt, -thumb.jpg, ..."""
    stem = video.stem
    return [p for p in video.parent.iterdir()
            if p != video and p.is_file() and p.name.startswith(stem)
            and p.name[len(stem):len(stem) + 1] in (".", "-", "_")]


def move(src_pool, dst_pool):
    """Rename on the file's own disk. Returns True if moved (or would be)."""
    disk = disk_of(src_pool)
    if disk is None:
        log(f"  [WARN] {src_pool}: not found on any disk, skipped")
        return False
    src = disk / src_pool.relative_to(POOL)
    dst = disk / dst_pool.relative_to(POOL)
    if dst_pool.exists() or dst.exists():
        log(f"  [WARN] {dst_pool} already exists — left {src_pool.name} "
            f"where it is (duplicate check will compare them)")
        return False
    if APPLY:
        make_dirs(dst.parent)
        os.rename(src, dst)
    return True


def make_dirs(d):
    """mkdir -p, giving each new folder its parent's owner — the FreeBSD
    edition runs this as root, and a root-owned season folder would lock the
    jailed Sonarr/Radarr out of it."""
    missing = []
    while not d.exists():
        missing.append(d)
        d = d.parent
    st = d.stat()
    for m in reversed(missing):
        m.mkdir()
        try:
            os.chown(m, st.st_uid, st.st_gid)
        except PermissionError:
            pass                    # not root: it's ours already


# ── what the *arrs manage ────────────────────────────────────────────────────
def managed_files():
    """Host paths of every file Radarr/Sonarr track, pool-normalised."""
    out = set()
    for m in arr_get(RADARR, "/movie"):
        if m.get("movieFile", {}).get("path"):
            out.add(to_pool(to_host(m["movieFile"]["path"])))
    for s in arr_get(SONARR, "/series"):
        if s.get("statistics", {}).get("episodeFileCount"):
            for f in arr_get(SONARR, "/episodefile", seriesId=s["id"]):
                out.add(to_pool(to_host(f["path"])))
    return out


def sonarr_series():
    """normalised title -> (series id, pool path) for every Sonarr series."""
    out = {}
    for s in arr_get(SONARR, "/series"):
        p = to_pool(to_host(s["path"]))
        for t in [s["title"]] + [a["title"] for a in s.get("alternateTitles", [])]:
            out.setdefault(norm(t), (s["id"], p))
    return out


# ── Movies -> Tv Shows ───────────────────────────────────────────────────────
def show_folder(series_title, by_sonarr, folders):
    """Existing show folder for this title, or None."""
    key = norm(series_title)
    if key in by_sonarr:
        return by_sonarr[key]
    hits = folders.get(key, [])
    if len(hits) == 1:
        return None, hits[0]
    if len(hits) > 1:
        # Split folders — pick the one with the most episodes; the Jellyfin
        # duplicate step already merges these, so this is rare.
        return None, max(hits, key=lambda f: len(videos(f)))
    return None


def season_dir(show, season):
    """Where this season's episodes go, following the show's own layout."""
    if show.exists():
        subs = [d for d in show.iterdir() if d.is_dir() and SEASON_DIR.match(d.name)]
        for d in subs:
            n = re.search(r"\d+", d.name)
            if (season == 0 and d.name.lower() == "specials") or \
               (n and int(n[0]) == season):
                return d
        loose = [p for p in show.iterdir() if p.is_file() and is_video(p)]
        if loose and not subs:
            return show
    return show / ("Specials" if season == 0 else f"Season {season}")


def sort_episodes(managed, by_sonarr, rescan):
    folders = {}
    for f in (POOL / TV).iterdir():
        if f.is_dir():
            folders.setdefault(norm(f.name), []).append(f)

    moved = 0
    for v in sorted(videos(POOL / MOVIES)):
        if not v.exists() or not EP_TAG.search(v.name) or too_new(v):
            continue
        p = arr_get(SONARR, "/parse", title=v.name).get("parsedEpisodeInfo") or {}
        if not p.get("seriesTitle") or not p.get("episodeNumbers") \
                or p.get("seasonNumber") is None:
            continue
        if v in managed:
            log(f"  [WARN] {v.relative_to(POOL)} looks like a TV episode but "
                f"Radarr manages it — fix it in Radarr, not moved")
            continue
        found = show_folder(p["seriesTitle"], by_sonarr, folders)
        sid, show = found if found else (None, POOL / TV / re.sub(
            r'[\\/:*?"<>|]', "", p["seriesTitle"]))
        dest = season_dir(show, p["seasonNumber"])
        src_dir = v.parent
        items = [v] + sidecars(v)
        if move(v, dest / v.name):
            for s in items[1:]:
                move(s, dest / s.name)
            moved += 1
            log(f"  [{'MOVE' if APPLY else 'DRY '}] {v.relative_to(POOL)} "
                f"-> {dest.relative_to(POOL)}/")
            if sid:
                rescan["sonarr"].add(sid)
            # A per-release folder that only held this episode is now junk.
            if APPLY and src_dir != POOL / MOVIES:
                remove_if_leftover(src_dir)
    return moved


# ── Tv Shows -> Movies ───────────────────────────────────────────────────────
def movie_parse(name):
    p = arr_get(RADARR, "/parse", title=name) or {}
    info = p.get("parsedMovieInfo") or {}
    return (info.get("movieTitles") or [None])[0], info.get("year"), p.get("movie")


def sort_movies(managed, by_sonarr, rescan):
    sonarr_paths = {p for _, p in by_sonarr.values()}
    moved = 0
    for e in sorted((POOL / TV).iterdir()):
        if not e.exists():      # a sidecar already moved with its video
            continue
        if e.is_file():
            if not is_video(e) or EP_TAG.search(e.name) or too_new(e):
                continue
            vids = [e]
        else:
            if e in sonarr_paths:
                continue
            if any(d.is_dir() and SEASON_DIR.match(d.name) for d in e.iterdir()):
                continue
            vids = videos(e)
            # Exactly one real video; extras/trailers under 100 MB don't count.
            vids = [v for v in vids if v.stat().st_size > 100 * 2**20]
            if len(vids) != 1:
                continue
            if any(too_new(v) for v in vids):
                continue
        v = vids[0]
        if EP_TAG.search(v.name) or PART_TAG.search(v.name) or \
           PART_TAG.search(e.name):
            continue
        # Radarr must see a dated movie. (Sonarr's parser is no use as a
        # negative check here: it reads "Movie.1999.1080p" as S19E99.)
        title, year, rmovie = movie_parse(v.name)
        if not title or not year:
            title, year, rmovie = movie_parse(e.name)
        if not title or not year:
            continue
        if v in managed:
            log(f"  [WARN] {v.relative_to(POOL)} looks like a movie but Sonarr "
                f"manages it — fix it in Sonarr, not moved")
            continue

        if e.is_file():
            # Loose file: give it its own folder, like Radarr would.
            dest_dir = (to_pool(to_host(rmovie["path"])) if rmovie else
                        POOL / MOVIES / re.sub(r'[\\/:*?"<>|]', "",
                                               f"{title} ({year})"))
            ok = move(v, dest_dir / v.name)
            if ok:
                for s in sidecars(v):
                    move(s, dest_dir / s.name)
        else:
            dest_dir = POOL / MOVIES / e.name
            ok = move_folder(e, dest_dir)
        if ok:
            moved += 1
            log(f"  [{'MOVE' if APPLY else 'DRY '}] {e.relative_to(POOL)} "
                f"-> {dest_dir.relative_to(POOL)}"
                f"{'/' if e.is_file() else ''}")
            if rmovie:
                rescan["radarr"].add(rmovie["id"])
    return moved


def move_folder(src_pool, dst_pool):
    """Rename a folder on every disk that holds part of it (mergerfs can
    spread one folder across disks). All-or-nothing on name clashes."""
    rel_s, rel_d = src_pool.relative_to(POOL), dst_pool.relative_to(POOL)
    parts = [d for d in DISKS if (d / rel_s).is_dir()]
    if dst_pool.exists() or any((d / rel_d).exists() for d in parts):
        log(f"  [WARN] {dst_pool} already exists — left {src_pool.name} "
            f"where it is")
        return False
    if APPLY:
        for d in parts:
            make_dirs((d / rel_d).parent)
            os.rename(d / rel_s, d / rel_d)
    return True


def remove_if_leftover(folder):
    """Drop a folder only if nothing but metadata/art is left in it."""
    junk = {".nfo", ".jpg", ".jpeg", ".png", ".txt", ".url", ".exe", ".sfv"}
    for disk in DISKS:
        d = disk / folder.relative_to(POOL)
        if not d.is_dir():
            continue
        files = [p for p in d.rglob("*") if p.is_file()]
        if any(p.suffix.lower() not in junk for p in files):
            return
    for disk in DISKS:
        d = disk / folder.relative_to(POOL)
        if d.is_dir():
            for p in sorted(d.rglob("*"), reverse=True):
                p.rmdir() if p.is_dir() else p.unlink()
            d.rmdir()


def main():
    if not (POOL / MOVIES).is_dir() or not (POOL / TV).is_dir():
        log(f"  [FAIL] {POOL} not mounted — skipped")
        return 1
    try:
        managed = managed_files()
        by_sonarr = sonarr_series()
    except Exception as e:
        log(f"  [FAIL] Radarr/Sonarr API unreachable ({e}) — skipped, nothing moved")
        return 1
    rescan = {"sonarr": set(), "radarr": set()}
    eps = sort_episodes(managed, by_sonarr, rescan)
    movies = sort_movies(managed, by_sonarr, rescan)
    if APPLY:
        for sid in rescan["sonarr"]:
            arr_command(SONARR, "RescanSeries", seriesId=sid)
        for mid in rescan["radarr"]:
            arr_command(RADARR, "RescanMovie", movieId=mid)
    verb = "moved" if APPLY else "would move (dry run)"
    if eps or movies:
        log(f"  [OK]   {verb}: {eps} episode(s) Movies->Tv Shows, "
            f"{movies} movie(s) Tv Shows->Movies")
    else:
        log("  [OK]   no misfiled movies or episodes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
