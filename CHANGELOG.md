# Changelog

Project version lives in `VERSION`. Bumped on every change made to this
source tree from here on, per the user's request (2026-09-26).

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
