# Desktop status dashboard (FreeBSD)

A full port of the Linux edition's dashboard. Same page, same loopback security
model, same one-click repair whitelist — retargeted to FreeBSD primitives.

## Files
- `status-collect.py` — 60s collector. Emits the SAME `status.json` schema the
  page consumes, so `index.html` renders unchanged. Sources: sysctl (CPU/mem/
  temps/uptime), `df -k` (disks), `jls`+`bastille` (the "Containers" panel now
  shows jails), `wg`+exit-IP probe (VPN), and the unchanged HTTP calls to the
  *arr/qBittorrent/Jellyfin/Plex APIs (pointed at each jail's bridge IP).
- `status-dashboard-server.py` — loopback repair server. Whitelisted fix ids:
  `jail_start`, `jail_restart`, `vpn_recreate`, `vpn_rotate`, `prowlarr_testall`,
  `cloudflare_unblock`, `pkg_upgrade`, `daily_routine_quick`, `restart_service`.
  Keeps the CSRF guard (Sec-Fetch-Site/Origin), 64 KB cap, argv-only exec,
  async job + `/api/job`, and confirmation-gated reboot.
- `status_keys.py` — API keys (`media-keys.env`, 600) + the VPN-confined jail
  group (`vpn-group.json`) + jail-IP helper (kept in sync with `stack.yaml`).
- `status-dashboard-install.sh` — cron collector + cron server-watchdog (the
  systemd timer/Restart=always replacement), installs ttyd from pkg, writes the
  XDG autostart entry, applies the KWin rule.
- `status-dashboard-run.sh` — launches the Chromium `--app` window and blocks on
  it (no systemd cgroup-escape dance needed on FreeBSD; the browser is a normal
  child).
- `status-dashboard-show.sh` — installs the KWin below-layer rule (byte-identical
  to the Linux edition — same KWin) and reconfigures via `qdbus6`.
- `dashboard-pane.sh` — ttyd pane whitelist; system panes retargeted (`logs` →
  `tail -F /var/log/messages`, `jails` → `jls`/`bastille list`, etc.).
- `index.html` — the page, with the VPN panel relabelled gluetun→tunnel /
  netns→route and the repair tooltips keyed to the FreeBSD fix ids.

## AI panes (full Linux parity)

The terminal pane offers the same AI panes as the Linux edition:
- **Agent:** Claude Code, opencode (native).
- **Local:** Ollama (`llm`).
- **Free tier (OpenRouter):** DeepSeek, Ox Alpha, Minimax M3.
- **Paid flagships (OpenRouter, bills your account):** ChatGPT, Gemini, Hy4.
- Hidden one-shot `Ask X` variants for each, driven from the dashboard search box.

`ai-panes-check.py` runs nightly from `daily-routine.sh`: it re-derives each
paid flagship (newest generation, highest completion price), verifies the free
tiers still exist (falling back to the cheapest paid variant and saying so
loudly if a free tier is retired), and writes `model-pricing.json` so the picker
shows live per-pane pricing. The server's `available_panes()` lets that live
pricing override the static free/paid label, so a free tier that got repriced
overnight moves to the paid section automatically.

Only FreeBSD-specific changes vs. Linux: `bash` and `python3.11` (both from
pkg), and the system panes use `jls`/`tail` instead of `docker`/`journalctl`.
One key powers every online pane — put it in
`~/.config/status-dashboard/deepseek.env` as `DEEPSEEK_API_KEY=`.

## Deploy
`lib/steps.py::install_dashboard` renders the scripts into `~/.local/bin`, drops
the page in `~/.local/share/status-dashboard/`, seeds `~/.config/status-dashboard/`,
and runs the installer. After first run, fill the API keys in
`~/.config/status-dashboard/media-keys.env` (chmod 600) and the panels light up.

If you change `jail_subnet` in the profile, set `STATUS_SUBNET` to match, since
`status_keys.py` defaults to the stack's `10.17.0`.
