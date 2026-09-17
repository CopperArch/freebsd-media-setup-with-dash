#!/usr/bin/env python3.11
"""freebsd-media-setup-with-dash — entry point.

    python3.11 install.py --cli                 # interactive terminal wizard
    python3.11 install.py --cli --profile p.yaml  # reproduce from a profile
    python3.11 install.py --cli --profile p.yaml --dry-run
    python3.11 install.py --cli --profile p.yaml --yes   # no prompts

A tkinter GUI mirrors the Linux edition (py311-tkinter) and calls the same
steps; the CLI is the reliable path over SSH, which is how you'll usually
reproduce a box.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.platform import Platform          # noqa: E402
from lib.profile import Profile            # noqa: E402
from lib.steps import Ctx, ORDER           # noqa: E402
from lib.util import Sudo, log             # noqa: E402


def accept_factory(assume_yes: bool):
    def accept(prompt: str) -> bool:
        if assume_yes:
            log(f"[auto-yes] {prompt}")
            return True
        try:
            return input(f"\n{prompt} [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    return accept


def ask_factory(interactive: bool):
    """Collect an app login detail, or let the person installing skip it for
    now. Every call already shows what happens if you just hit Enter -- never
    silently keep a leftover default without saying so."""
    def ask(prompt: str, default: str = "", secret: bool = False) -> str:
        if not interactive:
            return default
        hint = (" [Enter to auto-generate]" if secret and not default
                else f" [{default}]" if default else " [Enter to leave blank, set up later]")
        try:
            val = input(f"{prompt}{hint}: ").strip()
        except EOFError:
            return default
        return val or default
    return ask


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", action="store_true", help="terminal installer")
    ap.add_argument("--profile", type=Path, help="load choices from a YAML profile")
    ap.add_argument("--save-profile", type=Path, help="write the profile back out")
    ap.add_argument("--dry-run", action="store_true", help="show, change nothing")
    ap.add_argument("--yes", action="store_true", help="accept every step")
    args = ap.parse_args()

    plat = Platform.detect()
    log(f"Detected: {plat.pretty}")

    if args.profile and args.profile.exists():
        profile = Profile.from_yaml(args.profile)
        log(f"Loaded profile {args.profile}")
    else:
        profile = Profile()
        log("No profile — using defaults "
            "(edit profiles/example.yaml or pass --profile)")

    # Every app login below: type your own now, or just hit Enter and deal
    # with it later (a real password gets auto-generated so nothing is ever
    # left blank/insecure; Plex just stays unclaimed until you claim it
    # yourself). Only runs with --cli and without --yes -- an unattended
    # run (ISO first boot, a scripted --profile --yes re-install) never
    # blocks waiting on a prompt.
    interactive = args.cli and not args.yes
    ask = ask_factory(interactive)
    if interactive:
        log("\n=== App logins (Enter to skip any of these for now) ===")
        if profile.use_nextcloud:
            profile.nc_admin_user = ask("Nextcloud admin username",
                                        profile.nc_admin_user or "admin")
            profile.nc_admin_password = ask("Nextcloud admin password",
                                            profile.nc_admin_password, secret=True)
        if profile.use_vpn_stack:
            profile.qbit_user = ask("qBittorrent username",
                                    profile.qbit_user or "admin")
            profile.qbit_password = ask("qBittorrent password",
                                        profile.qbit_password, secret=True)
        if profile.use_media_stack:
            profile.plex_claim = ask(
                "Plex claim token (get one at https://plex.tv/claim, valid "
                "~4 minutes -- leave blank to claim Plex yourself later)",
                profile.plex_claim)
            log("\nOne OpenRouter key powers every dashboard AI pane -- "
                "get one (free tier available) at https://openrouter.ai/keys")
            profile.openrouter_api_key = ask("OpenRouter API key",
                                             profile.openrouter_api_key, secret=True)

        if profile.use_vpn_stack and not (profile.wg_private_key
                                          and profile.wg_peer_public_key
                                          and profile.wg_endpoint):
            log("\n=== VPN provider (needed for qBittorrent/Sonarr/Radarr to "
                "reach the internet) ===\n"
                "Any provider works as long as it gives you a WireGuard config "
                "-- three with real free/cheap tiers:\n"
                "  ProtonVPN (free):  https://protonvpn.com/support/wireguard-configuration-generator/\n"
                "  Windscribe (free): https://windscribe.com/getconfig/wireguard\n"
                "  Surfshark:         https://surfshark.com/download/router  "
                "(Manual setup -> WireGuard)\n"
                "Or paste values from any other provider's WireGuard config. "
                "Leave all three blank to set this up later -- qBittorrent/"
                "Sonarr/Radarr just won't have internet access until you do.")
            profile.wg_private_key = ask("WireGuard private key",
                                         profile.wg_private_key, secret=True)
            profile.wg_peer_public_key = ask("WireGuard peer (provider) public key",
                                             profile.wg_peer_public_key)
            profile.wg_endpoint = ask("WireGuard endpoint (host:port)",
                                      profile.wg_endpoint)

        log("\n=== Public access (reach your apps from outside the LAN) ===\n"
            "Every app gets reverse-proxied through Caddy under its own "
            "hostname. Pick one, or leave blank to stay LAN-only for now "
            "(configure later from the dashboard):\n"
            "  duckdns    - free, one subdomain per app, needs port-forwarding\n"
            "  cloudflare - free Cloudflare Tunnel, no port-forwarding, but "
            "needs a domain you already own and added to Cloudflare\n"
            "  noip       - free rival to DuckDNS (Jellyfin only -- see notes below)\n"
            "  dynu       - another free rival (Jellyfin only -- see notes below)")
        profile.public_access = ask("Provider (duckdns/cloudflare/noip/dynu)",
                                    profile.public_access)
        if profile.public_access == "duckdns":
            profile.ddns_base = ask("Subdomain base (e.g. 'coppermedia' -> "
                                    "coppermedia-jellyfin.duckdns.org)", profile.ddns_base)
            profile.ddns_token = ask("DuckDNS token (from duckdns.org after signing in)",
                                     profile.ddns_token, secret=True)
        elif profile.public_access == "cloudflare":
            profile.cloudflare_domain = ask("Your domain, already added to Cloudflare "
                                            "(e.g. example.com)", profile.cloudflare_domain)
            profile.cloudflare_tunnel_token = ask(
                "Cloudflare Tunnel token (Zero Trust -> Networks -> Tunnels -> "
                "your tunnel -> copy the token from the install command)",
                profile.cloudflare_tunnel_token, secret=True)
            profile.cloudflare_api_token = ask(
                "Cloudflare API token, Zone:DNS:Edit scope (dash.cloudflare.com "
                "-> My Profile -> API Tokens)", profile.cloudflare_api_token, secret=True)
            profile.cloudflare_zone_id = ask("Zone ID for that domain (shown on the "
                                             "domain's Cloudflare overview page)",
                                             profile.cloudflare_zone_id)
        elif profile.public_access in ("noip", "dynu"):
            log(f"Note: {profile.public_access}'s free tier can't auto-create new "
                "hostnames the way DuckDNS does, so only Jellyfin will be public "
                "under this provider -- create the hostname on their dashboard first.")
            profile.ddns_base = ask("The hostname you created on their dashboard "
                                    "(e.g. yourname.ddns.net)", profile.ddns_base)
            profile.ddns_user = ask(f"{profile.public_access} account username",
                                    profile.ddns_user)
            profile.ddns_token = ask(f"{profile.public_access} account password",
                                     profile.ddns_token, secret=True)

    generated = profile.fill_generated_secrets()
    if generated:
        log(f"\nAuto-generated (left blank): {', '.join(generated)}")

    errs = profile.validate()
    if errs and not args.dry_run:
        for e in errs:
            log(f"  [profile] {e}")
        log("Fix the profile (or --dry-run to preview anyway).")
        return 2

    sudo = Sudo()
    ctx = Ctx(profile, plat, sudo, dry_run=args.dry_run)
    accept = accept_factory(args.yes)

    log("\n=== freebsd-media-setup-with-dash ===")
    failed = []
    for title, step in ORDER:
        log(f"\n--- {title} ---")
        ok, summary = step(ctx, accept)
        log(f"    {'OK ' if ok else 'ERR'}: {summary}")
        if not ok and summary not in ("declined",) and "disabled" not in summary:
            failed.append(title)

    # Always saved, whether or not --save-profile was passed: a credential
    # that only ever existed in this process's memory (typed in, or
    # auto-generated) is one bad Ctrl-C away from being lost for good.
    default_save = Path("/root/.config/freebsd-media-setup/profile-used.yaml")
    default_save.parent.mkdir(parents=True, exist_ok=True)
    profile.save(default_save)
    saved_at = [default_save]
    if args.save_profile:
        profile.save(args.save_profile)
        saved_at.append(args.save_profile)

    log("\n=== Credentials ===")
    log(f"Full profile (everything below, plus internal-only secrets) saved "
        f"to: {', '.join(str(p) for p in saved_at)} (chmod 600)")
    if profile.use_nextcloud:
        tag = "auto-generated" if "nc_admin_password" in generated else "you set this"
        log(f"  Nextcloud admin -> user: {profile.nc_admin_user}  "
            f"password: {profile.nc_admin_password}  ({tag})")
    if profile.use_vpn_stack:
        tag = "auto-generated" if "qbit_password" in generated else "you set this"
        log(f"  qBittorrent     -> user: {profile.qbit_user}  "
            f"password: {profile.qbit_password}  ({tag})")
        if profile.wg_private_key and profile.wg_peer_public_key and profile.wg_endpoint:
            log("  VPN             -> WireGuard config set, tunnel started")
        else:
            log("  VPN             -> NOT configured; qBittorrent/Sonarr/Radarr have "
                "no internet access until you add a WireGuard config and re-run. "
                "Free options: https://protonvpn.com/support/wireguard-configuration-generator/ "
                "or https://windscribe.com/getconfig/wireguard")
    if profile.use_media_stack:
        if profile.plex_claim:
            log("  Plex            -> claimed automatically with the token you gave")
        else:
            log("  Plex            -> not claimed; open its web UI once to claim/sign in "
                "(https://plex.tv/claim if asked for a token)")
        log("  Jellyfin        -> no account created by this installer; its own "
            "first-run web wizard (open its URL in a browser) is where you "
            "create the admin account, same as any fresh Jellyfin install")
        if profile.openrouter_api_key:
            log("  AI panes        -> OpenRouter key set, dashboard AI panes enabled")
        else:
            log("  AI panes        -> NOT configured; get a free-tier key at "
                "https://openrouter.ai/keys and re-run, or add it later to "
                "~/.config/status-dashboard/deepseek.env")
    if profile.public_access:
        log(f"  Public access   -> {profile.public_access} (see the 'Public access' "
            "step's own output above for the exact hostnames and their status)")
    else:
        log("  Public access   -> NOT configured; every app stays LAN-only until "
            "you set one up (dashboard or re-run this installer)")

    log("\nDone." if not failed else f"\nDone with issues: {', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
