#!/usr/bin/env bash
# dashboard-pane.sh — FreeBSD edition. Decides what runs in the dashboard's ttyd
# terminal pane. Full parity with the Linux edition: same AI panes (Claude Code,
# opencode, local Ollama, DeepSeek/Ox Alpha/Minimax free-tier, and the paid
# ChatGPT/Gemini/Hy4 flagships), same one-shot "Ask X" variants, same
# opencode-backed agentic sessions. Retargeted bits: bash lives in pkg
# (/usr/local/bin/bash), python3 -> python3.11, and the system panes use
# jls/tail instead of docker/journalctl.
#
# ttyd is started with --url-arg, so the page selects a program by URL:
#     http://127.0.0.1:7682/?arg=claude
#     http://127.0.0.1:7682/?arg=ask&arg=why+is+radarr+failing
# The first arg is matched against the fixed whitelist below — the URL can only
# ever pick one of these, never smuggle in a command. Every pane falls back to
# an interactive shell so it's never a dead black rectangle.
set -uo pipefail
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin"
cd "$HOME" || exit 1

PROG="${1:-claude}"
shift || true

# Agent panes run real, possibly long fixes -- closing the dock drops ttyd's
# websocket and sends the child SIGHUP. Running these under tmux means that
# SIGHUP just detaches the tmux client; the session (and whatever the agent
# is doing) keeps running headless, and reopening the same pane reattaches
# instead of starting over. One session per agent id so different panes never
# share state. Mirrors the Linux dashboard-pane.sh fix (2026-09-26) -- tmux
# is installed alongside ttyd, see lib/steps.py's dashboard pkg install.
AGENT_PANES=(claude opencode oa mm gpt gm hy ds)
if [[ -z "${DASHBOARD_PANE_TMUX:-}" ]] && command -v tmux >/dev/null 2>&1 \
   && printf '%s\n' "${AGENT_PANES[@]}" | grep -qx "$PROG"; then
    export DASHBOARD_PANE_TMUX=1
    # Highlight-to-copy. The agents turn on mouse tracking and tmux passes
    # that through to ttyd's xterm.js, so a drag went to the app and nothing
    # could ever be selected. With tmux owning the mouse, a left-drag (or
    # double/triple click) always selects, and releasing copies straight to
    # the desktop clipboard for pasting into a terminal, editor, etc. Plain
    # clicks and the scroll wheel still reach the app.
    CLIP=""
    if command -v wl-copy >/dev/null 2>&1; then CLIP="wl-copy"
    elif command -v xclip >/dev/null 2>&1; then CLIP="xclip -selection clipboard"
    elif command -v xsel  >/dev/null 2>&1; then CLIP="xsel -ib"
    fi
    # ';' separates top-level tmux commands; '\;' chains inside a binding.
    COPY_OPTS=(';' set-option mouse on)
    if [[ -n "$CLIP" ]]; then
        COPY_OPTS+=(
          ';' bind-key -T root MouseDrag1Pane select-pane -t = '\;' copy-mode -M
          ';' bind-key -T copy-mode    MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "$CLIP"
          ';' bind-key -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "$CLIP"
          ';' bind-key -T root DoubleClick1Pane select-pane -t = '\;' copy-mode -M '\;' send-keys -X select-word '\;' send-keys -X copy-pipe-and-cancel "$CLIP"
          ';' bind-key -T root TripleClick1Pane select-pane -t = '\;' copy-mode -M '\;' send-keys -X select-line '\;' send-keys -X copy-pipe-and-cancel "$CLIP"
        )
    fi
    exec tmux new-session -A -s "dash-$PROG" "$0" "$PROG" "$@" "${COPY_OPTS[@]}"
fi

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

# Local GLM pane -- Z.ai's official open-weights model via ollama. The
# installer writes local-ai.env (GLM_MODEL=, sized to this machine's RAM) only
# when "Local AI" was chosen; without it the GLM panes are hidden/refuse.
AI_ENV="$HOME/.config/status-dashboard/local-ai.env"
[ -f "$AI_ENV" ] && . "$AI_ENV"
GLM_MODEL="${GLM_MODEL:-glm-4.7-flash}"
GLM_KEEPALIVE="${GLM_KEEPALIVE:-10m}"

# Model slugs + OpenRouter key. ai-panes-check.py keeps the managed block in
# this same file current nightly (from daily-routine.sh), swapping a free tier
# to a cheap paid variant rather than dropping a pane, so any of these can be
# briefly non-free between runs; the dashboard shows live pricing per pane.
DS_ENV="$HOME/.config/status-dashboard/deepseek.env"
[ -f "$DS_ENV" ] && . "$DS_ENV"
DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash:free}"
OXALPHA_MODEL="${OXALPHA_MODEL:-stealth/ox-alpha}"
MINIMAX_MODEL="${MINIMAX_MODEL:-minimax/minimax-m3:free}"
# Paid flagships — one per remaining major provider; every query bills OpenRouter.
CHATGPT_MODEL="${CHATGPT_MODEL:-openai/gpt-5.6-sol-pro}"   # ~$2 / $10 per M tokens
GEMINI_MODEL="${GEMINI_MODEL:-google/gemini-3.7-flash}"     # ~$0.75 / $3.75 per M tokens
HY4_MODEL="${HY4_MODEL:-tencent/hy4-preview}"               # ~$0.83 / $2.50 per M tokens

hr() { printf '── %s %s\n' "$1" "$(printf '─%.0s' $(seq 1 $((60 - ${#1}))))"; }
fallback()   { echo; hr "$1 exited — dropping to a shell"; exec bash -il; }
press_enter(){ echo; hr "done — press enter for a shell"; read -r _ 2>/dev/null; exec bash -il; }

need_key() {
    if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
        echo "no API key — create ~/.config/status-dashboard/deepseek.env with:"
        echo "  DEEPSEEK_API_KEY=<your key>"
        echo "Get one free at openrouter.ai/keys ($1)"
        read -r -p "Press Enter to open that page in the browser now (any other key to skip): " _reply
        if [[ -z "$_reply" ]]; then
            # Same whitelisted /api/open the dashboard's own app tiles use --
            # never a raw URL, and it reuses that persistent browser profile
            # so signing in there sticks for next time too.
            curl -s -m 5 -X POST http://127.0.0.1:8099/api/open \
                -H 'Content-Type: application/json' -d '{"id":"openrouter_keys"}' >/dev/null 2>&1
        fi
        return 1
    fi
}
ask_curl() {
    curl -s -m 120 "$DEEPSEEK_BASE_URL/chat/completions" \
        -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":$(python3.11 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$2")}]}" \
    | python3.11 -c '
import json,sys
try:
    d=json.load(sys.stdin)
    print(d["choices"][0]["message"]["content"])
except Exception as e:
    print("request failed:", e)
'
}

case "$PROG" in
    claude)
        hr "Claude Code"
        if command -v claude >/dev/null 2>&1; then claude; else echo "claude not installed"; fi
        fallback "claude" ;;
    opencode)
        hr "opencode"
        if command -v opencode >/dev/null 2>&1; then opencode; else echo "opencode not installed"; fi
        fallback "opencode" ;;
    ask)
        QUERY="$*"; hr "Ask Claude"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif command -v claude >/dev/null 2>&1; then echo "> $QUERY"; echo; claude -p "$QUERY"
        else echo "claude not installed"; fi
        press_enter ;;
    llm)
        hr "Local LLM — $OLLAMA_MODEL"; export OLLAMA_HOST=127.0.0.1:11434
        if ! curl -sf -m 5 -o /dev/null "http://$OLLAMA_HOST/api/version"; then
            echo "ollama is not running — start it with:  service ollama start"
            echo "(or run 'ollama serve' if installed outside rc.d)"
        elif ! ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
            echo "model $OLLAMA_MODEL not pulled yet — fetching it now"
            ollama pull "$OLLAMA_MODEL" && ollama run "$OLLAMA_MODEL"
        else ollama run "$OLLAMA_MODEL"; fi
        fallback "ollama" ;;
    askllm)
        QUERY="$*"; export OLLAMA_HOST=127.0.0.1:11434; hr "Ask $OLLAMA_MODEL"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif ! curl -sf -m 5 -o /dev/null "http://$OLLAMA_HOST/api/version"; then
            echo "ollama is not running (service ollama start)"
        else echo "> $QUERY"; echo; ollama run "$OLLAMA_MODEL" "$QUERY"; fi
        press_enter ;;
    glm)
        hr "GLM (local) — $GLM_MODEL"; export OLLAMA_HOST=127.0.0.1:11434
        if [[ ! -f "$AI_ENV" ]]; then
            echo "Local AI isn't enabled on this machine — re-run the installer"
            echo "with use_local_glm: true to download it here."
        elif ! curl -sf -m 5 -o /dev/null "http://$OLLAMA_HOST/api/version"; then
            echo "ollama is not running — start it with:  service ollama start"
        else ollama run --keepalive "$GLM_KEEPALIVE" "$GLM_MODEL"; fi
        fallback "ollama" ;;
    askglm)
        QUERY="$*"; export OLLAMA_HOST=127.0.0.1:11434; hr "Ask $GLM_MODEL"
        if [[ ! -f "$AI_ENV" ]]; then echo "Local AI isn't enabled on this machine"
        elif [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif ! curl -sf -m 5 -o /dev/null "http://$OLLAMA_HOST/api/version"; then
            echo "ollama is not running (service ollama start)"
        else echo "> $QUERY"; echo; ollama run --keepalive "$GLM_KEEPALIVE" "$GLM_MODEL" "$QUERY"; fi
        press_enter ;;
    askds)
        QUERY="$*"; hr "Ask DeepSeek — $DEEPSEEK_MODEL (near-free, verify at openrouter.ai)"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif need_key "model $DEEPSEEK_MODEL"; then echo "> $QUERY"; echo; ask_curl "$DEEPSEEK_MODEL" "$QUERY"; fi
        press_enter ;;
    ds)
        hr "DeepSeek — $DEEPSEEK_MODEL (near-free) — file/shell access via opencode"
        if need_key "model $DEEPSEEK_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$DEEPSEEK_MODEL"; fi
        fallback "deepseek" ;;
    askoa)
        QUERY="$*"; hr "Ask Ox Alpha — $OXALPHA_MODEL (near-free, verify at openrouter.ai)"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif need_key "this pane uses model $OXALPHA_MODEL"; then echo "> $QUERY"; echo; ask_curl "$OXALPHA_MODEL" "$QUERY"; fi
        press_enter ;;
    oa)
        hr "Ox Alpha — $OXALPHA_MODEL (near-free) — file/shell access via opencode"
        if need_key "this pane uses model $OXALPHA_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$OXALPHA_MODEL"; fi
        fallback "oxalpha" ;;
    askmm)
        QUERY="$*"; hr "Ask Minimax M3 (free)"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif need_key "this pane uses model $MINIMAX_MODEL"; then echo "> $QUERY"; echo; ask_curl "$MINIMAX_MODEL" "$QUERY"; fi
        press_enter ;;
    mm)
        hr "Minimax M3 (free) — file/shell access via opencode"
        if need_key "this pane uses model $MINIMAX_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$MINIMAX_MODEL"; fi
        fallback "minimax" ;;
    askgpt)
        QUERY="$*"; hr "Ask ChatGPT ($CHATGPT_MODEL) — PAID, bills OpenRouter"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif need_key "this pane is PAID, model $CHATGPT_MODEL"; then echo "> $QUERY"; echo; ask_curl "$CHATGPT_MODEL" "$QUERY"; fi
        press_enter ;;
    gpt)
        hr "ChatGPT — $CHATGPT_MODEL (PAID, bills OpenRouter) — file/shell access via opencode"
        if need_key "this pane is PAID, model $CHATGPT_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$CHATGPT_MODEL"; fi
        fallback "chatgpt" ;;
    askgm)
        QUERY="$*"; hr "Ask Gemini ($GEMINI_MODEL) — PAID, bills OpenRouter"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif need_key "this pane is PAID, model $GEMINI_MODEL"; then echo "> $QUERY"; echo; ask_curl "$GEMINI_MODEL" "$QUERY"; fi
        press_enter ;;
    gm)
        hr "Gemini — $GEMINI_MODEL (PAID, bills OpenRouter) — file/shell access via opencode"
        if need_key "this pane is PAID, model $GEMINI_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$GEMINI_MODEL"; fi
        fallback "gemini" ;;
    askhy)
        QUERY="$*"; hr "Ask Hy4 ($HY4_MODEL) — PAID, bills OpenRouter"
        if [[ -z "${QUERY// }" ]]; then echo "no question given"
        elif need_key "this pane is PAID, model $HY4_MODEL"; then echo "> $QUERY"; echo; ask_curl "$HY4_MODEL" "$QUERY"; fi
        press_enter ;;
    hy)
        hr "Hy4 — $HY4_MODEL (PAID, bills OpenRouter) — file/shell access via opencode"
        if need_key "this pane is PAID, model $HY4_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$HY4_MODEL"; fi
        fallback "hy4" ;;
    shell)
        hr "Shell"; exec bash -il ;;
    htop)
        hr "Processes"
        if command -v htop >/dev/null 2>&1; then htop; else top; fi
        fallback "htop" ;;
    jails)
        hr "Jail stats (Ctrl-C to exit)"
        while true; do clear; jls; echo; bastille list all 2>/dev/null; sleep 5; done
        fallback "jls" ;;
    logs)
        hr "System log (Ctrl-C to exit)"; tail -F /var/log/messages; fallback "tail" ;;
    dashlog)
        hr "Dashboard collector + server log"
        tail -F "$HOME/.local/share/status-dashboard/server.log" 2>/dev/null || echo "no server log yet"
        fallback "tail" ;;
    disk)
        hr "Disk usage — {{MEDIA_POOL}}"
        if command -v ncdu >/dev/null 2>&1; then ncdu {{MEDIA_POOL}}; else du -h -d2 {{MEDIA_POOL}} | sort -h; fi
        fallback "ncdu" ;;
    routine)
        hr "Latest daily-routine log"
        L=$(ls -t "$HOME"/.hermes/maintenance-logs/daily-routine-*.log 2>/dev/null | head -1)
        [[ -n "$L" ]] && less +G "$L" || echo "no daily-routine logs yet"
        fallback "log viewer" ;;
    *)
        hr "Unknown pane '$PROG' — shell"; exec bash -il ;;
esac
