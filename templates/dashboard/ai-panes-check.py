#!/usr/bin/env python3.11
"""
ai-panes-check.py — nightly sanity/refresh pass for the desktop dashboard's
online AI chat panes: Ox Alpha, DeepSeek, Minimax M3 (nominally free) and
ChatGPT, Gemini, Hy4 (deliberately paid — one flagship per remaining major
provider not already covered by DeepSeek/Ox Alpha/Minimax or native Claude
Code).

What it actually checks, against OpenRouter's public /api/v1/models list:
  - Each of the three free-tier model slugs still exists and is still
    priced at $0 (OpenRouter free tiers do get retired/renamed). If one
    breaks, this WARNS rather than guessing a replacement — "best free
    model" isn't something OpenRouter's models API exposes (no usage-rank
    field), and picking a wrong slug unattended would just swap one broken
    pane for a different broken pane.
  - Each of the three paid slots IS re-derived deterministically every run:
    among that provider's chat models (excluding mini/nano/instruct/batch/
    audio/image/etc. variants), providers ship each generation as a same-day
    family and price the flagship highest — so "newest generation, highest
    completion price in that generation" reliably tracks the flagship
    without hardcoding a tier name like "sol-pro" that stops matching next
    generation.

Writes results into the model-slug lines in
~/.config/status-dashboard/deepseek.env, inside a clearly marked block —
dashboard-pane.sh sources this file fresh on every pane open, so a change
here takes effect on the next click with no service restart needed. Also
writes ~/.config/status-dashboard/model-pricing.json (current $/M-token
pricing for all six slots) so the dashboard can show live price next to each
paid pane instead of a number that goes stale the moment a model gets repriced.

Since 2026-10-09 it also, every run:
  - Discovers EVERY model that is currently $0 on OpenRouter and usable in a
    pane (text output, tool-calling so opencode can drive it) and writes the
    list to ~/.config/status-dashboard/free-models.json. The dashboard's
    picker shows these under its FREE tier and dashboard-pane.sh only ever
    launches a "free:<id>" pane for an id in that list, so the tier follows
    the catalogue by itself — models appear when they go free and drop off
    the night they stop being free.
  - Fetches the USD->GBP rate and stamps it on every pricing entry, so the
    dashboard shows prices in pounds (OpenRouter itself bills in USD).

`ai-panes-check.py --is-free <model id>` is the live guard dashboard-pane.sh
runs before opening a FREE-tier pane: exit 0 = still $0 right now, 1 = no
longer free (pane refuses to start rather than bill), 2 = couldn't check.

Never fails the caller (daily-routine.sh): network errors and unresolvable
models are reported as warnings, exit code is always 0.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / ".config/status-dashboard/deepseek.env"
PRICING_FILE = Path.home() / ".config/status-dashboard/model-pricing.json"
FREE_FILE = Path.home() / ".config/status-dashboard/free-models.json"
MODELS_URL = "https://openrouter.ai/api/v1/models"
# USD->GBP, tried in order (both keyless). (url, path to the rate in the reply)
FX_SOURCES = (
    ("https://api.frankfurter.dev/v1/latest?base=USD&symbols=GBP", ("rates", "GBP")),
    ("https://open.er-api.com/v6/latest/USD", ("rates", "GBP")),
)
# $0 models that aren't a chat/agent pane even though they emit text.
FREE_EXCLUDE_TOKENS = ("content-safety", "guard", "moderation", "embed", "rerank")

# Known-good values as of 2026-09-03 — used to seed the managed block the
# first time this runs, and as the fallback if a lookup can't be resolved.
DEFAULTS = {
    "OXALPHA_MODEL":  "stealth/ox-alpha",
    "DEEPSEEK_MODEL": "deepseek/deepseek-v4-flash:free",
    "MINIMAX_MODEL":  "minimax/minimax-m3:free",
    "CHATGPT_MODEL":  "openai/gpt-5.6-sol-pro",
    "GEMINI_MODEL":   "google/gemini-3.7-flash",
    "HY4_MODEL":      "tencent/hy4-preview",
    # Added 2026-10-06 at the user's request. No :free variant existed, so
    # this starts on the cheapest paid one; check_free_slot moves it to a
    # :free id automatically if one ever appears in the same family.
    "QWEN_MODEL":     "qwen/qwen3.8-flash",
}
FREE_KEYS = ("OXALPHA_MODEL", "DEEPSEEK_MODEL", "MINIMAX_MODEL", "QWEN_MODEL")
# (env key, OpenRouter provider prefix) — one flagship auto-picked per provider.
PAID_SLOTS = (
    ("CHATGPT_MODEL", "openai/"),
    ("GEMINI_MODEL",  "google/"),
    ("HY4_MODEL",     "tencent/"),
)
# Fallback substrings to search by if a free slug's exact id 404s (providers
# occasionally rev a free slug's suffix, e.g. a date stamp).
FAMILY_HINT = {
    # Ox Alpha was de-anonymised as GLM 5.3 Flash (see KNOWN_RENAMES) and the
    # pane is labelled "GLM 5.3" since 2026-10-06; search that family so a
    # future glm-5.3-flash:free is picked up.
    "OXALPHA_MODEL":  "glm-5.3-flash",
    "DEEPSEEK_MODEL": "deepseek-v4-flash",
    "MINIMAX_MODEL":  "minimax-m3",
    "QWEN_MODEL":     "qwen3.8-flash",
}
# Stealth-model de-anonymizations confirmed by reporting, consulted only when
# both the exact id and the family-substring search find nothing at all —
# stealth slugs vanish outright rather than reving a suffix, so there's no
# substring left to search by. Add an entry here if another stealth pane's
# identity gets revealed and the old slug disappears.
KNOWN_RENAMES = {
    "stealth/ox-alpha": "z-ai/glm-5.3-flash",  # confirmed by Bloomberg/TechCrunch, 2026-08-23
}

START = "# --- ai-panes-check.py managed block — edit by re-running the script, not by hand ---"
END = "# --- end ai-panes-check.py managed block ---"

# Hyphen-anchored: a bare "mini" would also match inside "ge-mini", silently
# excluding every Gemini model and falling through to Google's unrelated
# Lyria (music-generation) line — caught 2026-09-03 when that's exactly what
# happened. Keep every size/variant token here anchored the same way.
EXCLUDE_TOKENS = ("-mini", "-nano", "instruct", "realtime", "-audio", "-image",
                   "transcribe", "-search", "embed", "whisper", "tts",
                   "moderation", "chat-latest", "codex", "-lite", "gemma",
                   "lyria", "-clip")
# Only these output modalities count as a chat pane candidate — excludes
# audio/image-output models (like Lyria) that EXCLUDE_TOKENS might miss by
# name alone. Checked directly against the model's own architecture field
# rather than guessed from its id.
ALLOWED_OUTPUT_MODALITIES = {"text"}


def fetch_models():
    req = urllib.request.Request(MODELS_URL, headers={"User-Agent": "ai-panes-check/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["data"]


def is_free(model):
    # Every price component has to be 0, not just prompt/completion — a
    # per-request or per-image fee would still bill the account.
    p = model.get("pricing") or {}
    if "prompt" not in p or "completion" not in p:
        return False
    try:
        return all(float(v or 0) == 0.0 for v in p.values())
    except (TypeError, ValueError):
        return False


def fetch_usd_gbp():
    """Today's USD->GBP rate, or None if every source is unreachable."""
    for url, path in FX_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ai-panes-check/1"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            for k in path:
                d = d[k]
            rate = float(d)
            if 0.3 < rate < 2.0:   # sanity: a parse slip must not reprice everything
                return rate
        except Exception:  # noqa: BLE001 — try the next source
            continue
    return None


def discover_free(models, slot_ids):
    """Every currently-$0 model usable as a dashboard pane, newest first.

    slot_ids are the models the fixed panes (GLM/DeepSeek/...) already point
    at — left out so a model never appears twice in the picker.
    """
    out = []
    for m in models:
        mid = m["id"]
        if not is_free(m) or mid in slot_ids or mid.endswith(":batch"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._~:/-]+", mid):
            continue
        if any(tok in mid for tok in FREE_EXCLUDE_TOKENS):
            continue
        out_mod = set(m.get("architecture", {}).get("output_modalities") or [])
        if out_mod != ALLOWED_OUTPUT_MODALITIES:
            continue
        # The panes run the model through opencode, which needs tool calls.
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        name = re.sub(r"\s*\(free\)\s*$", "", m.get("name") or mid).strip()
        out.append({"id": mid, "name": name,
                    "context": m.get("context_length"),
                    "expires": m.get("expiration_date"),
                    "created": m.get("created") or 0})
    # OpenRouter's own free router first (it never expires), then newest.
    out.sort(key=lambda e: (e["id"] != "openrouter/free", -e["created"]))
    return out


def read_free_file():
    try:
        return json.loads(FREE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def live_is_free(mid):
    """--is-free: 0 still free, 1 not free / gone, 2 couldn't check."""
    try:
        models = fetch_models()
    except Exception:  # noqa: BLE001
        return 2
    m = next((x for x in models if x["id"] == mid), None)
    return 0 if m and is_free(m) else 1


def check_free_slot(key, current, models_by_id, warnings):
    m = models_by_id.get(current)
    if m and is_free(m):
        return current, None

    hint = FAMILY_HINT.get(key, "")
    same_family = {mid: mm for mid, mm in models_by_id.items()
                   if hint in mid and not mid.endswith(":batch")}
    free_candidates = [mid for mid, mm in same_family.items() if is_free(mm)]
    if free_candidates:
        new = sorted(free_candidates)[-1]
        why = "is no longer free" if m else "no longer exists"
        warnings.append(f"{key}: {current} {why} — switched to {new} "
                         f"(same family, still free). Verify the pane still works.")
        return new, "replaced (free)"

    # No $0 option left anywhere in the family — this is what actually
    # happened to Ox Alpha and DeepSeek's stealth/promo pricing within
    # hours of being wired up. Rather than leave the pane permanently
    # broken, fall back to the cheapest paid variant available (these run a
    # few hundredths of a cent per query) and say so loudly — this silently
    # turns a "free" pane into a billed one otherwise.
    #
    # Candidates: any same-family match, PLUS the exact id itself if it still
    # resolves (`m`) or its known rename does. Without including the exact/
    # renamed match here, a *second* run against an already-downgraded value
    # (e.g. current == "z-ai/glm-5.3-flash", which contains no "ox-alpha"
    # substring and isn't a KNOWN_RENAMES key itself) would find an empty
    # same_family and wrongly report "broken" even though the model is
    # perfectly resolvable — caught 2026-09-03 on exactly this id.
    # 2026-10-09: if the current id still resolves, KEEP it. This used to
    # re-pick "cheapest in the family" on every run, and OpenRouter reprices
    # these daily — so the DeepSeek pane hopped flash -> vision-exp ->
    # flash-latest -> vision-exp, printing this same "no free tier left"
    # warning each time for a pane that had already been paid for weeks.
    # A slot only moves now when a free variant appears (above) or its
    # current model disappears (below).
    if m:
        return current, "paid (unchanged)"

    fallback_candidates = dict(same_family)
    if current in KNOWN_RENAMES:
        renamed = KNOWN_RENAMES[current]
        rm = models_by_id.get(renamed)
        if rm:
            fallback_candidates[renamed] = rm

    # Experimental/preview variants get withdrawn without notice — only use
    # one if the family has nothing else. A "~...-latest" alias is preferred
    # outright: it follows the provider's current release by itself.
    stable = {k: v for k, v in fallback_candidates.items()
              if not re.search(r"-exp\b|-preview\b", k)}
    fallback_candidates = stable or fallback_candidates
    aliases = {k: v for k, v in fallback_candidates.items()
               if k.startswith("~") and k.endswith("-latest")}
    fallback_candidates = aliases or fallback_candidates

    if fallback_candidates:
        cheapest = min(fallback_candidates.items(),
                        key=lambda kv: float(kv[1].get("pricing", {}).get("completion", 0) or 0))
        new, mm = cheapest
        p = mm.get("pricing", {})
        if new != current:
            warnings.append(f"{key}: no free tier left for this model "
                             f"(was {current}) — falling back to the cheapest paid "
                             f"variant {new} (${p.get('prompt')}/${p.get('completion')} "
                             f"per token). This pane now bills the OpenRouter account, "
                             f"even though it's still listed as free-tier in the dashboard.")
        return new, "downgraded to paid"

    warnings.append(f"{key}: {current} no longer exists on OpenRouter and no "
                     f"replacement was found at all — pane will fail until "
                     f"fixed by hand.")
    return current, "broken"


def pick_best_paid(key, prefix, models_by_id, warnings):
    candidates = []
    for mid, m in models_by_id.items():
        if not mid.startswith(prefix) or mid.endswith(":batch"):
            continue
        if any(tok in mid for tok in EXCLUDE_TOKENS):
            continue
        out_mod = set(m.get("architecture", {}).get("output_modalities") or [])
        if out_mod and not out_mod <= ALLOWED_OUTPUT_MODALITIES:
            continue
        created = m.get("created")
        if not created:
            continue
        candidates.append((mid, created, m))
    if not candidates:
        warnings.append(f"{key}: could not list any {prefix}* models — "
                         f"keeping the current value.")
        return None
    newest = max(c[1] for c in candidates)
    generation = [c for c in candidates if newest - c[1] <= 7 * 86400]

    def price(c):
        try:
            return float(c[2].get("pricing", {}).get("completion", 0))
        except (TypeError, ValueError):
            return 0.0

    best = max(generation, key=price)
    return best[0]


def price_entry(mid, models_by_id, usd_gbp=None):
    # prompt/completion stay in USD per token (what OpenRouter bills in);
    # usd_gbp is the rate the dashboard multiplies by to show pounds.
    m = models_by_id.get(mid)
    if not m:
        return {"id": mid, "prompt": None, "completion": None, "free": None,
                "usd_gbp": usd_gbp}
    p = m.get("pricing", {})
    try:
        prompt, completion = float(p.get("prompt", 0)), float(p.get("completion", 0))
    except (TypeError, ValueError):
        prompt = completion = None
    free = is_free(m) if prompt is not None else None
    return {"id": mid, "prompt": prompt, "completion": completion, "free": free,
            "usd_gbp": usd_gbp}


def gbp_per_m(usd_per_token, rate):
    v = usd_per_token * 1e6 * rate
    return f"£{v:.2f}" if v >= 0.1 else f"£{v:.3f}"


def read_env():
    if not ENV_FILE.exists():
        # First run, before the managed block has ever been written: seed
        # from DEFAULTS like the normal path below does. Returning {} here
        # crashes main() with a KeyError on the very first install, since
        # every lookup below assumes all six DEFAULTS keys are present.
        return "", dict(DEFAULTS)
    text = ENV_FILE.read_text()
    values = dict(DEFAULTS)
    m = re.search(re.escape(START) + r"\n(.*?)\n" + re.escape(END), text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return text, values


def write_env(text, values):
    block = START + "\n" + "\n".join(f"{k}={values[k]}" for k in DEFAULTS) + "\n" + END
    if START in text:
        text = re.sub(re.escape(START) + r"\n.*?\n" + re.escape(END), block, text, flags=re.S)
    else:
        sep = "\n" if text and not text.endswith("\n") else ""
        text = text + sep + "\n" + block + "\n"
    ENV_FILE.write_text(text)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--is-free":
        return live_is_free(sys.argv[2])

    warnings = []
    text, values = read_env()

    try:
        models = fetch_models()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] ai-panes-check: couldn't reach OpenRouter ({type(e).__name__}: {e}) "
              f"— skipping tonight, keeping existing config unchanged.")
        return 0

    models_by_id = {m["id"]: m for m in models}
    new_values = dict(values)

    for key in FREE_KEYS:
        new_values[key], status = check_free_slot(key, values[key], models_by_id, warnings)

    for key, prefix in PAID_SLOTS:
        best = pick_best_paid(key, prefix, models_by_id, warnings)
        if best and best != values[key]:
            warnings.append(f"{key}: {values[key]} -> {best} "
                             f"(newer/pricier flagship found — still PAID).")
            new_values[key] = best

    changed = new_values != values
    if changed:
        write_env(text, new_values)

    # USD->GBP. If tonight's lookup fails, keep showing pounds at the last
    # rate we got rather than flipping the whole picker back to dollars.
    prev_free = read_free_file()
    usd_gbp = fetch_usd_gbp()
    if usd_gbp is None:
        usd_gbp = prev_free.get("usd_gbp")
        warnings.append("USD->GBP rate lookup failed — " +
                        (f"reusing the last known rate ({usd_gbp})." if usd_gbp
                         else "prices stay in USD until it works."))

    pricing = {key: price_entry(new_values[key], models_by_id, usd_gbp) for key in DEFAULTS}
    PRICING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRICING_FILE.write_text(json.dumps(pricing, indent=2) + "\n")

    # FREE tier: everything that is $0 tonight, minus what the fixed panes
    # already cover.
    free_models = discover_free(models, set(new_values.values()))
    FREE_FILE.write_text(json.dumps({
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "usd_gbp": usd_gbp,
        "models": free_models,
    }, indent=2) + "\n")
    old_ids = {m["id"] for m in prev_free.get("models", [])}
    new_ids = {m["id"] for m in free_models}

    print("--- ai-panes-check ---")
    for key in DEFAULTS:
        mark = " (changed)" if new_values[key] != values.get(key) else ""
        pe = pricing[key]
        if pe["free"]:
            price_str = "free"
        elif pe["prompt"] is None:
            price_str = "price unknown"
        elif usd_gbp:
            price_str = (f"{gbp_per_m(pe['prompt'], usd_gbp)}/"
                         f"{gbp_per_m(pe['completion'], usd_gbp)} per M tokens in/out")
        else:
            price_str = f"${pe['prompt'] * 1e6:.2f}/${pe['completion'] * 1e6:.2f} per M tokens in/out"
        print(f"  {key} = {new_values[key]}{mark}  ({price_str})")
    if usd_gbp:
        print(f"  USD->GBP rate: {usd_gbp}")
    print(f"  FREE tier: {len(free_models)} free model(s) on OpenRouter tonight")
    for m in free_models:
        tag = " (new)" if old_ids and m["id"] not in old_ids else ""
        exp = f" — free until {m['expires']}" if m.get("expires") else ""
        print(f"    + {m['id']}{tag}{exp}")
    for gone in sorted(old_ids - new_ids):
        print(f"    - {gone} (no longer free — removed from the FREE tier)")
    if warnings:
        for w in warnings:
            print(f"  [WARN] {w}")
    else:
        print(f"  [OK] all {len(DEFAULTS)} models still resolve as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
