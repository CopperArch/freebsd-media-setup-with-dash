# Changelog

Project version lives in `VERSION`. Bumped on every change made to this
source tree from here on, per the user's request (2026-09-26).

Since 2026-10-09 the number updates by itself: every change merged to
`master` gets the next patch number from
`.github/workflows/bump-version.yml`, which also adds the change to this
file. A pull request that edits `VERSION` itself is left as it is.

## 0.6.3 - 2026-10-09
- Version number updates automatically on every merge (#8)

## 0.6.2 - 2026-10-09
- Dashboard: the About panel (click the logo) shows the installed project
  version; the installer copies `VERSION` next to the page. Mirrors
  CopperArch-Linux-Media PR #10.

## 0.6.1 - 2026-10-09
- Dashboard: clicking the green dot on a healthy jail shows the installed
  version of its main package (read with `pkg query` inside the jail);
  clicking again swaps back. The rest of the tile still opens the app.
  Mirrors CopperArch-Linux-Media PR #9. Written on Linux — not yet run on a
  FreeBSD host.

## 0.6.0 - 2026-10-09
Mirrors CopperArch-Linux-Media PR #8 (dashboard parts; the Docker/qBittorrent
warning fixes in that PR have no FreeBSD counterpart).
- Dashboard: the AI picker gains a Free section listing every model that is
  currently free on OpenRouter (text output, tool calling).
  `ai-panes-check.py` rebuilds the list nightly into `free-models.json`; a
  free pane re-checks the price live before starting and refuses to run if
  the model is no longer free.
- Dashboard: model prices are shown in pounds using the day's USD->GBP rate.
- Dashboard: the picker reloads its list each time it is opened.
- `ai-panes-check.py`: a paid slot keeps its model while it still exists
  instead of hopping to whichever variant is cheapest that night (which
  repeated the same "no free tier left" warning).

## 0.5.2 - 2026-10-06
- Dashboard: the repair/update window can be hidden while a fix runs
  (Close reads "Hide"); the job keeps going and a header button shows its
  progress and result, reopening the window on click. Mirrors
  CopperArch-Linux-Media PR #7.

## 0.5.1 - 2026-10-06
- Nightly routine (Log Hygiene section): cleans the host and per-jail `pkg`
  download caches, removes test-VM disks / stock FreeBSD install media left
  in `~/build-iso` for 7+ days (never one a process still holds, never the
  built ISO), and warns once any ZFS pool passes 80% with its biggest
  datasets. Mirrors the Linux edition's root-disk cleanup.

## 0.5.0 - 2026-10-06
Mirrors CopperArch-Linux-Media PR #4.
- Dashboard: the repair/update sheet gets a **Cancel** button. Job commands
  run in their own process group; `POST /api/cancel` stops them and skips
  anything still queued (two clicks to confirm). Static files now send
  `Cache-Control: no-cache` so dashboard updates show after a reload.
- AI panes: new **Qwen 3.8** pane (`qw`/`askqw`, cheapest paid variant until a
  `:free` one appears); "Ox Alpha" relabelled **GLM 5.3** (what it runs).
  One-shot Ask panes cap `max_tokens` (OpenRouter otherwise reserves the full
  131k output against the credit balance and refuses when it runs low) and
  print OpenRouter's real error instead of `'choices'`.
- New nightly `media-library-sort.py` (routine 3b): moves misfiled
  movies/episodes between libraries; renames only, never overwrites, skips
  *arr-managed and recently changed files, new folders inherit their
  parent's owner (the routine runs as root).
- New nightly `media-library-health.py` (routine 3c): Jellyfin franchise
  collections from TMDB (via Radarr), IMDb-confirmed matching for
  unidentified movies, Plex auto-collections kept on, hand-made groupings,
  and a fetch-check of every poster in both apps. Both scripts take the jail
  endpoints/keys from `status-collect.py`.
- Not ported: the Linux edition's Power tile (FreeBSD exposes no RAPL
  counters to read).

## 0.4.0 - 2026-09-29
- New opt-in **Local AI (GLM)** component, mirroring the Linux edition
  (CopperArch-Linux-Media PR #3). Off by default (`use_local_glm: false`);
  the interactive `--cli` run asks. When enabled, `install_local_ai` installs
  FreeBSD's `misc/ollama` package, configures its rc.d service via `sysrc`
  (runs as the profile user, loopback-only, 10m keep-alive, models on the
  media pool) and pulls Z.ai's official GLM sized to RAM: `glm-4.7-flash`
  (19 GB) on >=24 GB, otherwise `glm4:9b` (5.5 GB); `glm_model` overrides.
- Dashboard: new `glm`/`askglm` panes, shown only where the installer wrote
  `~/.config/status-dashboard/local-ai.env`; opening them by URL elsewhere
  explains how to enable instead of starting a multi-GB download.
- `templates/daily-routine.sh` section 7: nightly `ollama pull` of the chosen
  GLM model, only on machines with Local AI enabled.

## 0.3.0 - 2026-09-26
- First GitHub publish: `github.com/CopperArch/CopperArch-FreeBSD-Media`
  (public, `main` default branch). Fixed a stale `LICENSE` copyright line
  that had been copied from the Linux sibling project's boilerplate.
- `templates/dashboard/dashboard-pane.sh`: the tmux wrap added in 0.2.0 had
  no fallback if `tmux` isn't installed — `exec tmux ...` would have failed
  outright instead of degrading gracefully. Added a `command -v tmux`
  guard (mirrors the same fix made to the Linux edition and the live box's
  copy the same day).

## 0.2.0 - 2026-09-26
- `templates/dashboard/dashboard-pane.sh`: agent panes (claude, opencode, oa,
  mm, gpt, gm, hy, ds) now run under `tmux new-session -A`, mirroring the
  Linux edition's fix — closing the dashboard's terminal dock only detaches
  the tmux client instead of killing the running agent, so a fix started in
  a pane keeps running in the background and the pane reattaches to it later.
- `lib/steps.py`: added `tmux` to the host `pkg install` alongside `bash`
  and `ttyd` so the pane fix above has what it needs.
- `templates/daily-routine.sh`: log/`.bak-*` retention shortened from 30 to
  5 days, mirroring the Linux edition.

## 0.1.0 - 2026-09-15 (baseline, retroactive)
First version tag. Prior history (jail/install pipeline, the 2026-09-15
4-bug Nextcloud-corruption chain) predates version tracking — see
`~/.claude/projects/-home-deebee/memory/project_freebsd_copperarch_media.md`
for that history.
