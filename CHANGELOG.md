# Changelog

Project version lives in `VERSION`. Bumped on every change made to this
source tree from here on, per the user's request (2026-09-26).

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
