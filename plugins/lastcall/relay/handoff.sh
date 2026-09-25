#!/bin/sh
# DEPRECATED — relay/handoff.sh is now a thin shim over the relay.
#
# The relay is plugins/lastcall/lib/lastcall_core/relay.py (also reachable as
# `lastcall relay`). It hands over to Claude Code or Codex, needs neither tmux
# nor a TTY, and proves the successor by a check-in on a ledger. This shim
# exists so wrap-up templates and habits that still say `bash handoff.sh`
# keep working: it maps the old flags and environment variables onto relay.py
# and execs it. Exit codes are relay.py's: 0 checked in, 1 precondition
# failure (nothing spawned), 2 spawned but never checked in.
#
# POSIX sh; needs python3 (or $PYTHON_BIN).
#
#   handoff.sh                   relay.py
#   --kill-predecessor           --retire-predecessor
#   --no-kill-predecessor        --no-retire-predecessor
#   --config-dir DIR             --config-dir DIR
#   --trust                      dropped: relay.py never edits ~/.claude.json
#   everything else              passed through unchanged (so --agent codex,
#                                --codex-mode ... work here too)
#
#   LASTCALL_REPO LASTCALL_HANDOFF_DIR LASTCALL_NAME_PREFIX LASTCALL_MODEL
#   LASTCALL_FALLBACK_MODEL LASTCALL_DIRTY_BASELINE LASTCALL_KILL_DELAY
#   TIMEOUT                      -> the matching flag (a flag given wins)
#   LASTCALL_SKIP_PERMISSIONS LASTCALL_REMOTE_CONTROL LASTCALL_KILL_PREDECESSOR
#   LASTCALL_REQUIRE_GIT         -> the matching on/off flag, unless given
#   LASTCALL_CONFIG_DIR          -> --config-dir, unless given
#   CLAUDE_BIN TMUX_BIN          read by relay.py itself
#   SETTLE LOG_DIR PROJECTS_DIR GIT_BIN   no longer used

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
RELAY="$HERE/../lib/lastcall_core/relay.py"
PYTHON_BIN=${PYTHON_BIN:-python3}

echo "handoff.sh is deprecated: it now runs relay.py (\`lastcall relay\`); use that directly." >&2

skip_set='' remote_set='' retire_set='' config_dir_set=''

# Rewrite "$@" in place: each original argument is shifted off the front and
# its translation appended to the back, so after $n rounds only the
# translation is left — no arrays, and no word splitting of any value.
n=$#
while [ "$n" -gt 0 ]; do
    arg=$1; shift; n=$((n - 1))
    case $arg in
        --kill-predecessor|--retire-predecessor)
            retire_set=1; set -- "$@" --retire-predecessor ;;
        --no-kill-predecessor|--no-retire-predecessor)
            retire_set=1; set -- "$@" --no-retire-predecessor ;;
        --skip-permissions|--no-skip-permissions)
            skip_set=1; set -- "$@" "$arg" ;;
        --remote-control|--no-remote-control)
            remote_set=1; set -- "$@" "$arg" ;;
        --trust)
            echo "handoff.sh: --trust is ignored — relay.py never edits ~/.claude.json;" \
                 "run \`claude\` once in the repo and accept the trust prompt" >&2 ;;
        --config-dir)
            config_dir_set=1
            if [ "$n" -gt 0 ]; then
                set -- "$@" --config-dir "$1"; shift; n=$((n - 1))
            else
                set -- "$@" --config-dir
            fi ;;
        --repo|--handoff|--handoff-dir|--model|--fallback-model|--timeout|--config|\
        --agent|--name-prefix|--topic|--dirty-baseline|--kill-delay|--codex-mode|\
        --codex-sandbox|--codex-approval|--permission-mode|--chain|--ledger-dir)
            # Options that take a value: carry the value across untouched.
            if [ "$n" -gt 0 ]; then
                set -- "$@" "$arg" "$1"; shift; n=$((n - 1))
            else
                set -- "$@" "$arg"
            fi ;;
        *)
            set -- "$@" "$arg" ;;
    esac
done

truthy() {
    case $1 in 1|true|TRUE|True|yes|on) return 0 ;; *) return 1 ;; esac
}

# Environment first, so a flag given on the command line (later) wins.
[ -n "${TIMEOUT:-}" ] && set -- --timeout "$TIMEOUT" "$@"
[ -n "${LASTCALL_KILL_DELAY:-}" ] && set -- --kill-delay "$LASTCALL_KILL_DELAY" "$@"
[ -n "${LASTCALL_DIRTY_BASELINE:-}" ] && set -- --dirty-baseline "$LASTCALL_DIRTY_BASELINE" "$@"
[ -n "${LASTCALL_FALLBACK_MODEL:-}" ] && set -- --fallback-model "$LASTCALL_FALLBACK_MODEL" "$@"
[ -n "${LASTCALL_MODEL:-}" ] && set -- --model "$LASTCALL_MODEL" "$@"
[ -n "${LASTCALL_NAME_PREFIX:-}" ] && set -- --name-prefix "$LASTCALL_NAME_PREFIX" "$@"
[ -n "${LASTCALL_HANDOFF_DIR:-}" ] && set -- --handoff-dir "$LASTCALL_HANDOFF_DIR" "$@"
[ -n "${LASTCALL_REPO:-}" ] && set -- --repo "$LASTCALL_REPO" "$@"
if [ -z "$config_dir_set" ] && [ -n "${LASTCALL_CONFIG_DIR:-}" ]; then
    set -- --config-dir "$LASTCALL_CONFIG_DIR" "$@"
fi
# On/off settings: only when the command line did not say, because relay.py
# refuses --x together with --no-x.
if [ -z "$skip_set" ] && [ -n "${LASTCALL_SKIP_PERMISSIONS:-}" ]; then
    if truthy "$LASTCALL_SKIP_PERMISSIONS"; then set -- --skip-permissions "$@"
    else set -- --no-skip-permissions "$@"; fi
fi
if [ -z "$remote_set" ] && [ -n "${LASTCALL_REMOTE_CONTROL:-}" ]; then
    if truthy "$LASTCALL_REMOTE_CONTROL"; then set -- --remote-control "$@"
    else set -- --no-remote-control "$@"; fi
fi
if [ -z "$retire_set" ] && [ -n "${LASTCALL_KILL_PREDECESSOR:-}" ]; then
    if truthy "$LASTCALL_KILL_PREDECESSOR"; then set -- --retire-predecessor "$@"
    else set -- --no-retire-predecessor "$@"; fi
fi
if truthy "${LASTCALL_REQUIRE_GIT:-0}"; then
    set -- --require-git "$@"
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "handoff.sh: python3 not found: $PYTHON_BIN" >&2
    exit 1
fi
exec "$PYTHON_BIN" "$RELAY" "$@"
