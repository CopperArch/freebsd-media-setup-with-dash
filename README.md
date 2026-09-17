# freebsd-media-setup-with-dash

A FreeBSD port of [`linux-media-setup-with-dash`](https://github.com/CopperArch/linux-media-setup-with-dash):
the same self-healing home media server — media/VPN stacks, a desktop status
dashboard, a nightly self-heal routine — rebuilt on **FreeBSD 15.1** with a
**KDE Plasma 6** desktop and an installer, using the OS's native primitives
instead of Docker.

The Linux edition targets a Docker-capable systemd distro. FreeBSD has no
Docker, so this isn't a reskin — the whole runtime substrate is different. What
carries over unchanged is the *architecture*: a profile-driven, accept-gated,
idempotent installer with `{{PLACEHOLDER}}` templating, plus a dashboard and a
nightly routine that keep the box healed.

## The port, in one table

| Concern | Linux edition | FreeBSD edition |
|---|---|---|
| Containers | Docker + Compose | **Bastille jails** (POSIX sh, zero deps; the 2026 consensus) |
| Media apps | linuxserver.io images | **native pkg** (`jellyfin`, `plexmediaserver-plexpass`, `sonarr`, `radarr`, `prowlarr`, `bazarr`, `qbittorrent-nox`) — all in the official ports tree, one app per jail |
| Service mgmt | systemd units | **rc.d** (`service`, `sysrc`) |
| VPN | gluetun container | **kernel WireGuard** (`if_wg`) in a `vpn` jail |
| Kill-switch | gluetun `FIREWALL_*` | **pf** ruleset — drops all egress that isn't the tunnel |
| "route through VPN" | `network_mode: service:gluetun` | confined jails default-route through the `vpn` jail; pf leaves no other path |
| Firewall | ufw / firewalld | **pf** |
| Reverse proxy | Caddy container | **Caddy** (native pkg) |
| Photos/files (Immich) | Immich containers (no FreeBSD build) | **Nextcloud** native — `nextcloud-php83` + php-fpm + PostgreSQL jail + Photos/Memories |
| Volumes | compose `volumes:` | **nullfs** mounts (`bastille mount`) |
| Storage/health | mdadm + SMART | **ZFS** (`zpool status`) + SMART |
| Updates | `apt/dnf/pacman` | **pkg** + `freebsd-update`, with ZFS-snapshot rollback |
| OS installer | (n/a — app only) | **bsdinstall** (scripted `installerconfig`) + first-boot KDE build |
| Desktop | your existing session | **KDE Plasma 6** on X11 via SDDM |

## Why base FreeBSD + KDE rather than GhostBSD

You asked for "the best desktop FreeBSD" with KDE. GhostBSD is the flagship
desktop FreeBSD, but it ships MATE/XFCE and its KDE story isn't first-class.
The FreeBSD KDE team targets **upstream FreeBSD** directly: the `kde6`
meta-package is Plasma 6 + Frameworks + SDDM, installed straight from pkg. So
the cleanest KDE-on-FreeBSD is base **FreeBSD 15.1-RELEASE** (which already
ships `bsdinstall`, satisfying "has an installer") plus `kde6`. That also keeps
you on the same upstream you built CopperArch BSD on, and the dashboard's
below-layer KWin window rule ports 1:1 because Plasma uses the same KWin.

X11 is the default session here: Plasma Wayland on FreeBSD is close but not yet
the safe daily-driver in 2026 (SDDM/Wayland quirks, NVIDIA still experimental).
Flip to Wayland by editing `/usr/local/etc/sddm.conf.d/session.conf`.

## Three ways in

**1 — Full ISO (unattended).** Build a bootable image that installs the base OS
on ZFS and brings up KDE + Bastille on first boot:
```sh
./build-iso.sh 15.1-RELEASE      # run on a FreeBSD host; emits CopperArch-Media-*.iso
```

**2 — First-boot on an existing base install.** On a fresh FreeBSD 15.1 box:
```sh
sh bootstrap/firstboot-kde.sh    # KDE Plasma 6 + SDDM + drm-kmod + Bastille
```

**3 — The stack installer** (once KDE + prereqs exist):
```sh
python3.11 install.py --cli                         # interactive wizard
python3.11 install.py --cli --profile profiles/example.yaml
python3.11 install.py --cli --profile my.yaml --dry-run   # change nothing
./rebuild my.yaml                                   # idempotent re-apply / self-heal on demand
```
Every system-changing step shows an explicit **accept** prompt, exactly like
the Linux edition. Re-running is safe: existing jails are detected, rc knobs
re-asserted, the kill-switch and routine re-rendered.

## The VPN, done the FreeBSD way

There is no gluetun. Instead:

1. A `vpn` jail loads kernel WireGuard (`if_wg`) and brings up `wg0` from
   `vpn/wg0.conf` (rendered from your provider's keys in the profile).
2. `qbittorrent`, `sonarr`, `radarr` are created with **no default route of
   their own** — they route through the `vpn` jail.
3. `vpn/pf-killswitch.conf` blocks all egress except the WireGuard handshake,
   the LAN, and traffic on `wg0`. If the tunnel drops, the `wg0` pass-rule
   matches nothing and every torrent/indexer packet is dropped — **no fallback
   to your real IP**, ever. That's the gluetun guarantee, enforced by the
   kernel firewall.

The nightly routine checks the tunnel from *inside* the jail (compares exit IP
to the host WAN IP) rather than trusting a wrapper healthcheck — the same
lesson the Linux edition learned when gluetun reported "healthy" while OpenVPN
looped on auth.

## Self-healing (three layers, ported)

1. **rc.d** — services run under `service`/`sysrc`; jails restart on boot.
2. **Dashboard repairs** — the loopback web dashboard exposes the same closed
   whitelist of one-click fixes; the collector's data source changes from
   `docker ps` to `bastille list -a` / `jls`.
3. **Nightly `daily-routine.sh` (03:00)** — POSIX sh: checks rc services, ZFS
   pool health, SMART, jail liveness, VPN tunnel reality + up-to-3 restarts,
   re-asserts the pf kill-switch, then `pkg upgrade` per jail behind a ZFS
   snapshot with **verify + auto-rollback**, host `pkg` + `freebsd-update`, and
   log pruning.

## Repository layout

```
install.py                 # entry point (CLI; tkinter GUI mirrors the Linux one)
rebuild                    # idempotent re-apply from a profile (CopperArch-style)
build-iso.sh               # bake an unattended KDE install ISO
lib/
  platform.py              # THE PORT: pkg / bastille / rc.d / pf helpers
  steps.py                 # ordered, accept-gated steps (reads stack.yaml)
  profile.py               # profile schema (WireGuard fields, not gluetun creds)
  render.py  util.py       # reused verbatim from the Linux edition
stacks/
  stack.yaml               # the whole server as jails — replaces both compose files
vpn/
  wg0.conf.tmpl            # WireGuard tunnel (gluetun replacement)
  pf-killswitch.conf.tmpl  # leak-proof pf ruleset
templates/
  daily-routine.sh         # ported nightly self-heal
  dashboard/               # loopback dashboard (collector retargeted to jls)
bootstrap/
  installerconfig          # scripted bsdinstall (ZFS root)
  firstboot-kde.sh         # fresh base -> KDE Plasma 6 + SDDM + Bastille
profiles/example.yaml      # fill-in template (secrets blank, chmod 600)
```

## Honest caveats / things to confirm on your hardware

- **Dashboard** is fully ported (`templates/dashboard/`): collector, loopback
  repair server, KWin rule, cron/`daemon(8)` supervision and the page itself.
  The collector emits the same `status.json` schema, so the page is unchanged
  bar the VPN panel relabel (gluetun→tunnel) and repair tooltips keyed to the
  FreeBSD fix ids, and the AI panes (Claude Code, opencode, local Ollama,
  DeepSeek/Ox Alpha/Minimax free-tier and paid ChatGPT/Gemini/Hy4) are at
  full parity, with `ai-panes-check.py` refreshing model slugs + live
  pricing nightly. Fill `media-keys.env` (per-app API keys) and
  `deepseek.env` (one OpenRouter key) after first run to light everything up.
- **rc knob names.** A few ports name their `_enable` knob differently from the
  app (Plex is the classic case). They're all centralised in
  `Platform.RC` / `stack.yaml`; after install, `pkg info -D <pkg>` prints the
  port's pkg-message with the exact knob. Confirm and adjust in one place.
- **`jellyseerr`** may not always be a first-class port; if `pkg install
  jellyseerr` misses, it falls back to a Node build (`www/node`) — or run Seerr
  in a Linux jail if you prefer parity with the compose version.
- **GPU module.** `firstboot-kde.sh` auto-loads `amdgpu` (your RDNA work) or
  `i915kms`. NVIDIA on FreeBSD is the rough edge — the `nvidia-drm` kmod works
  for display but hardware transcode in Jellyfin/Plex is far less mature than
  VAAPI on AMD/Intel. If this box has an NVIDIA GPU, budget time there.
- **Nextcloud** ships in the default stack as the Immich replacement (Immich
  has no FreeBSD build): a `nextcloud-db` jail (PostgreSQL) plus a `nextcloud`
  jail (php-fpm + in-jail Caddy + redis), fronted by the reverse-proxy jail,
  with the Photos/Memories/Recognize apps giving the phone-photo-backup +
  timeline Immich provided. Toggle with `use_nextcloud` in the profile.
- **Kasm-Chrome** from the Linux edition isn't ported (Docker-only).
- **Wayland.** X11 is the default for the reasons above; the desktop and the
  dashboard's KWin rule are tested against the X11 session.

## License

MIT — same as the upstream Linux edition.
