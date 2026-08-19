#!/usr/bin/env bash
# Hand this session over to a fresh one — the optional relay half of Last Call.
#
# OPT-IN AND UNIX-ONLY. The guard itself is portable Python and does not need
# this. This script spawns a successor Claude Code session in tmux, seeded with
# your newest handoff document, and waits until that successor has PROVEN it is
# working before saying so. It never kills anything: retiring the predecessor is
# the successor's job, after it has proved itself, so a failed spawn leaves the
# old session alive to report the failure.
#
# Nothing invokes this automatically. You reference it from your wrap-up
# template, so the assistant runs it as the last step of winding down. See
# templates/handoff-relay.md.
#
# Requires: bash, tmux, python3 and the `claude` CLI. Git is optional — with
# it, the handoff is verified committed before anything is spawned; without it,
# that check is skipped loudly.
#
# Exit codes: 0 successor up and working
#             1 precondition failure (nothing was spawned)
#             2 spawned but never proved itself

set -euo pipefail

REPO=${LASTCALL_REPO:-}
HANDOFF_DIR=${LASTCALL_HANDOFF_DIR:-docs/handoff}
CLAUDE_BIN=${CLAUDE_BIN:-claude}
TMUX_BIN=${TMUX_BIN:-tmux}
PYTHON_BIN=${PYTHON_BIN:-python3}
TIMEOUT=${TIMEOUT:-180}
SETTLE=${SETTLE:-3}
LOG_DIR=${LOG_DIR:-$HOME/.claude/lastcall/relay}
NAME_PREFIX=${LASTCALL_NAME_PREFIX:-}
# Paths that are dirty as a matter of course and do not mean "uncommitted work"
# — a tracked .env holding a rotating URL, say. Comma-separated, repo-relative.
DIRTY_BASELINE=${LASTCALL_DIRTY_BASELINE:-}

HANDOFF=""
ALLOW_DIRTY=0
DRY_RUN=0
SKIP_PERMISSIONS=${LASTCALL_SKIP_PERMISSIONS:-0}
REMOTE_CONTROL=${LASTCALL_REMOTE_CONTROL:-1}
TRUST=0
# "the flag said 0" and "nobody said anything" are different states, and a
# boolean cannot hold both. Without these, --no-kill-predecessor set the value
# to 0 and the config then set it straight back to 1, because the guard could
# only ask whether the value was empty.
SKIP_PERMISSIONS_SET=""
REMOTE_CONTROL_SET=""
KILL_PREDECESSOR_SET=""
KILL_PREDECESSOR=${LASTCALL_KILL_PREDECESSOR:-0}
KILL_DELAY=${LASTCALL_KILL_DELAY:-5}
MODEL=${LASTCALL_MODEL:-}
FALLBACK_MODEL=${LASTCALL_FALLBACK_MODEL:-}
REQUIRE_GIT=${LASTCALL_REQUIRE_GIT:-0}
GIT_BIN=${GIT_BIN:-git}
NEW=""

usage() {
    cat <<'EOF'
usage: handoff.sh [options]

  --repo <path>          repository to hand over. Default: "repo" in
                         .claude/lastcall.json, else $CLAUDE_PROJECT_DIR, else
                         the git toplevel of the working directory
  --config-dir <path>    where to read .claude/lastcall.json from. Default:
                         $CLAUDE_PROJECT_DIR, else the nearest ancestor of the
                         working directory that has one. This is NOT the same
                         question as --repo: one session can own several repos
  --handoff <path>       use this handoff instead of the newest
  --handoff-dir <path>   where handoffs live, repo-relative (default docs/handoff)
  --allow-dirty          spawn even though the tree has uncommitted work
  --no-remote-control    do not pass --remote-control (on by default, names the
                         successor so you can reach it from anywhere)
  --require-git          refuse unless the directory is a git worktree. Off by
                         default: without git the committed-handoff check is
                         skipped and said so, rather than blocking the handover
  --model <name>         model for the successor: fable, opus, sonnet, or a
                         full model name. Default: whatever the CLI would pick
  --fallback-model <list>  comma-separated models to fall back to when the
                         first is overloaded or unavailable. Claude Code does
                         the switching; this only passes it through
  --kill-predecessor     retire THIS session once the successor has proved
                         itself. Detached and delayed, because this script runs
                         inside the session it kills. Off by default
  --no-kill-predecessor  keep this session alive (the default)
  --trust                record this folder as trusted in ~/.claude.json. Claude
                         Code asks once per directory and skip-permissions does
                         NOT cover it; without trust the successor never acts.
  --no-skip-permissions  force permission prompts on even if config enables them
  --skip-permissions     start the successor with --dangerously-skip-permissions.
                         REQUIRED for a genuinely unattended relay, and it means
                         the successor runs tools without asking. Opt in
                         deliberately; it is never the default.
  --timeout <secs>       how long to wait for the successor to prove itself (180)
  --dry-run              resolve everything, print it, spawn nothing
  -h, --help             this

env: LASTCALL_REPO LASTCALL_HANDOFF_DIR LASTCALL_NAME_PREFIX
     LASTCALL_DIRTY_BASELINE CLAUDE_BIN TMUX_BIN PYTHON_BIN
     PROJECTS_DIR TIMEOUT SETTLE LOG_DIR
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --repo)         REPO=${2:?--repo needs a path}; shift 2 ;;
        --config-dir)   CONFIG_DIR=${2:?--config-dir needs a path}; shift 2 ;;
        --handoff)      HANDOFF=${2:?--handoff needs a path}; shift 2 ;;
        --handoff-dir)  HANDOFF_DIR=${2:?--handoff-dir needs a path}; shift 2 ;;
        --allow-dirty)  ALLOW_DIRTY=1; shift ;;
        --skip-permissions) SKIP_PERMISSIONS=1; SKIP_PERMISSIONS_SET=1; shift ;;
        --no-skip-permissions) SKIP_PERMISSIONS=0; SKIP_PERMISSIONS_SET=1; shift ;;
        --no-remote-control) REMOTE_CONTROL=0; REMOTE_CONTROL_SET=1; shift ;;
        --model)        MODEL=${2:?--model needs a name}; shift 2 ;;
        --fallback-model) FALLBACK_MODEL=${2:?--fallback-model needs a name}; shift 2 ;;
        --kill-predecessor)    KILL_PREDECESSOR=1; KILL_PREDECESSOR_SET=1; shift ;;
        --no-kill-predecessor) KILL_PREDECESSOR=0; KILL_PREDECESSOR_SET=1; shift ;;
        --trust)        TRUST=1; shift ;;
        --require-git)  REQUIRE_GIT=1; shift ;;
        --timeout)      TIMEOUT=${2:?--timeout needs seconds}; shift 2 ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

# Validate the numbers HERE, while a bad value is still a precondition failure.
# The deadline arithmetic runs AFTER the spawn, so a non-numeric --timeout would
# otherwise abort the shell with a session already created and no exit-2 report.
for var in TIMEOUT SETTLE; do
    case "${!var}" in
        ''|*[!0-9]*) echo "$var must be a whole number of seconds, got: ${!var}" >&2; exit 1 ;;
    esac
    # Digits alone are not enough: bash reads a leading zero as octal, so
    # --timeout 08 passes the check above and then dies in the deadline
    # arithmetic — after the spawn. Normalise it while it is still a
    # precondition.
    printf -v "$var" '%d' "$((10#${!var}))"
    # And bash integer arithmetic is bounded, so a big enough value WRAPS.
    if [ "${!var}" -lt 0 ] || [ "${!var}" -gt 86400 ]; then
        echo "$var must be between 0 and 86400 seconds, got: ${!var}" >&2
        exit 1
    fi
done
[ "$TIMEOUT" -gt 0 ] || { echo "TIMEOUT must be at least 1 second" >&2; exit 1; }

# ------------------------------------------------------- config, then repo
# ORDER MATTERS. The config says which repo to hand over, so the config has to
# be found WITHOUT knowing the repo.
#
# This used to derive the config path from $REPO, which conflated two different
# questions: where the user's config lives, and which repository to hand over.
# They are the same directory only when a session owns exactly one repo. A
# session run from a parent directory holding two repos read no config at all
# and silently used defaults — and because remote_control defaults to on, the
# dry run still printed "remote control: on" and looked like the config had
# applied. Reported from real use. A default that matches the value you were
# trying to set is how a broken config path looks like a working one.
find_config_dir() {
    if [ -n "${LASTCALL_CONFIG_DIR:-}" ]; then
        printf '%s' "$LASTCALL_CONFIG_DIR"; return
    fi
    if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -f "$CLAUDE_PROJECT_DIR/.claude/lastcall.json" ]; then
        printf '%s' "$CLAUDE_PROJECT_DIR"; return
    fi
    # Walk up from the working directory, the same way the hook's project_dir()
    # does, because CLAUDE_PROJECT_DIR is not set in a plain shell.
    local dir; dir=$(pwd -P)
    while :; do
        [ -f "$dir/.claude/lastcall.json" ] && { printf '%s' "$dir"; return; }
        [ "$dir" = "/" ] && break
        dir=$(dirname "$dir")
    done
    [ -n "${CLAUDE_PROJECT_DIR:-}" ] && { printf '%s' "$CLAUDE_PROJECT_DIR"; return; }
    printf '%s' ""
}

CONFIG_DIR=${CONFIG_DIR:-$(find_config_dir)}
CONFIG=""
[ -n "$CONFIG_DIR" ] && CONFIG="$CONFIG_DIR/.claude/lastcall.json"

read_config() {
    [ -f "$CONFIG" ] || return 0
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || return 0
    while IFS='=' read -r key value; do
        [ -n "$key" ] || continue
        # Every guard ends in "|| true". Without it, a guard that evaluates
        # FALSE — which is exactly what happens when a command-line flag has
        # already set the value — becomes this function's exit status, and
        # `set -e` then kills the script with no output whatsoever. Overriding
        # a configured model from the command line died in total silence.
        case "$key" in
            repo)              [ -z "$REPO" ] && REPO=$value || true ;;
            model)             [ -z "$MODEL" ] && MODEL=$value || true ;;
            fallback_model)    [ -z "$FALLBACK_MODEL" ] && FALLBACK_MODEL=$value || true ;;
            handoff_dir)       [ "$HANDOFF_DIR" = "docs/handoff" ] && HANDOFF_DIR=$value || true ;;
            name_prefix)       [ -z "$NAME_PREFIX" ] && NAME_PREFIX=$value || true ;;
            dirty_baseline)    [ -z "$DIRTY_BASELINE" ] && DIRTY_BASELINE=$value || true ;;
            skip_permissions)  [ -z "${LASTCALL_SKIP_PERMISSIONS:-}$SKIP_PERMISSIONS_SET" ] && SKIP_PERMISSIONS=$value || true ;;
            remote_control)    [ -z "${LASTCALL_REMOTE_CONTROL:-}$REMOTE_CONTROL_SET" ] && REMOTE_CONTROL=$value || true ;;
            kill_predecessor)  [ -z "${LASTCALL_KILL_PREDECESSOR:-}$KILL_PREDECESSOR_SET" ] && KILL_PREDECESSOR=$value || true ;;
        esac
    done <<EOF
$("$PYTHON_BIN" - "$CONFIG" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        relay = (json.load(fh) or {}).get("relay") or {}
except Exception:
    raise SystemExit(0)
if not isinstance(relay, dict):
    raise SystemExit(0)
for key in ("repo", "handoff_dir", "name_prefix", "dirty_baseline",
            "model", "fallback_model"):
    if relay.get(key):
        print("%s=%s" % (key, relay[key]))
for key in ("skip_permissions", "remote_control", "kill_predecessor"):
    if key in relay:
        print("%s=%d" % (key, 1 if relay[key] else 0))
PY
)
EOF
    return 0
}

read_config

# Now the repo: --repo wins, then the config, then the session's project dir,
# then the git worktree containing the working directory.
if [ -z "$REPO" ]; then
    REPO=${CLAUDE_PROJECT_DIR:-}
fi
if [ -z "$REPO" ]; then
    REPO=$(git rev-parse --show-toplevel 2>/dev/null || true)
fi
# Last resorts, in order: where the config was found, then simply here. Running
# from the directory you want handed over is the obvious case, and it used to
# fail with "cannot work out which repo" purely because that directory was not
# a git worktree.
[ -z "$REPO" ] && [ -n "$CONFIG_DIR" ] && REPO=$CONFIG_DIR
[ -z "$REPO" ] && REPO=$(pwd -P)
[ -n "$REPO" ] || { echo "cannot work out which directory to hand over — pass --repo" >&2; exit 1; }

# Backwards compatibility: a project that put its config beside the repo rather
# than beside the session still works.
if [ -z "$CONFIG" ] || [ ! -f "$CONFIG" ]; then
    if [ -f "$REPO/.claude/lastcall.json" ]; then
        CONFIG_DIR=$REPO
        CONFIG="$REPO/.claude/lastcall.json"
        read_config
    fi
fi

# Fall back rather than abort. An unwritable log directory is a reason to log
# somewhere else, never a reason to refuse a handover — and after the spawn it
# would surface as exit 1, a precondition failure for a session that exists.
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG=$(mktemp "$LOG_DIR/spawn-inprogress-XXXXXX.log" 2>/dev/null || mktemp)
finalise_log() {
    local dest
    if [ -n "$NEW" ]; then
        dest="$LOG_DIR/spawn-$NEW.log"
        # Never clobber an earlier run's log — a previous abort may have claimed
        # this name, and that log is the only record of what it did.
        [ -e "$dest" ] && dest="$LOG_DIR/spawn-$NEW-$$.log"
    else
        dest="$LOG_DIR/spawn-aborted-$(date +%m%d-%H%M%S)-$$.log"
    fi
    if mv -f "$LOG" "$dest" 2>/dev/null; then
        echo "log: $dest" >&2
    else
        # Do not name a file the log is not in. A wrong path sends whoever is
        # diagnosing a failed handover to an empty file and reads as "no log".
        echo "log: $LOG (could not move it to $dest)" >&2
    fi
}
trap finalise_log EXIT

# Logging must never be the thing that kills the run. Under set -e an unwritable
# log would abort the launcher — and after the spawn that surfaces as exit 1,
# "precondition failure", for a session that has already been created.
say() {
    printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >>"$LOG" 2>/dev/null || true
    printf '%s\n' "$*" >&2 || true
}
die() { local code=$1; shift; say "ABORT($code): $*"; exit "$code"; }

say "lastcall handoff starting (pid $$)"

# ---------------------------------------------------------------- preconditions
# Nothing is created until every one of these has passed.

[ -d "$REPO" ] || die 1 "no such directory: $REPO"

# Git is how this checks that the handoff is DURABLE — that it will still exist
# after this session ends. That is the load-bearing rule of the whole design.
#
# But it is the check, not the requirement. A directory that is not a git
# worktree still has the handoff sitting on disk, which is durable enough for
# plenty of people, and refusing to hand over at all was the wrong trade: it
# turned "I cannot verify this" into "you may not proceed". Say what cannot be
# verified, and carry on. --require-git restores the strict behaviour.
HAVE_GIT=1
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    if [ "$REQUIRE_GIT" -eq 1 ]; then
        die 1 "not a git worktree: $REPO (--require-git is set)"
    fi
    HAVE_GIT=0
    say "WARNING: $REPO is not a git repository."
    say "         Cannot verify the handoff is committed, so that check is"
    say "         SKIPPED. The file exists on disk; if something deletes it,"
    say "         the successor gets nothing. Use a git repo, or accept this."
fi
say "config: ${CONFIG:-<none found>}"
say "repo: $REPO"

if [ -z "$HANDOFF" ]; then
    HANDOFF=$(ls -t "$REPO/$HANDOFF_DIR"/*.md 2>/dev/null | head -1 || true)
    [ -n "$HANDOFF" ] || die 1 "no handoff files in $REPO/$HANDOFF_DIR/ — write one first"
fi
[ -f "$HANDOFF" ] || die 1 "no such handoff: $HANDOFF"
say "handoff: $HANDOFF"

# The load-bearing ordering property of the whole design: never spawn before the
# handoff is durable. A rule you must remember at the moment your context is
# exhausted is a rule that gets skipped, so it is a precondition, not a habit.
if [ "$HAVE_GIT" -eq 1 ]; then
handoff_status=$(git -C "$REPO" status --porcelain -- "$HANDOFF") \
    || die 1 "cannot read git status for the handoff — refusing to assume it is committed"
if [ -n "$handoff_status" ]; then
    committed=$(git -C "$REPO" ls-files "$HANDOFF_DIR/*.md" | sort -r | head -1 || true)
    if [ -n "$committed" ]; then
        say "the newest committed handoff is $committed"
        say "to hand over with that one instead:"
        say "  handoff.sh --handoff $(printf '%q' "$REPO/$committed")"
    fi
    die 1 "handoff is not committed: $HANDOFF — commit it first"
fi
fi

if [ "$HAVE_GIT" -eq 1 ] && [ "$ALLOW_DIRTY" -eq 0 ]; then
    # --ignore-submodules=dirty drops submodule *content* noise while still
    # reporting a real gitlink bump.
    dirty=$(git -C "$REPO" status --porcelain --ignore-submodules=dirty)
    if [ -n "$dirty" ]; then
        remaining=""
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            # IFS= matters: porcelain status is two columns then a space, so a
            # bare `read` eats the leading blank of " M .env" and the offset
            # below slices the path into "env".
            path=${line:3}
            skip=0
            if [ -n "$DIRTY_BASELINE" ]; then
                IFS=',' read -ra baseline <<<"$DIRTY_BASELINE"
                for base in "${baseline[@]}"; do
                    [ "$path" = "$base" ] && { skip=1; break; }
                done
            fi
            [ "$skip" -eq 1 ] && continue
            remaining="$remaining$line"$'\n'
        done <<<"$dirty"
        if [ -n "${remaining//[$'\n' ]/}" ]; then
            say "uncommitted work:"
            printf '%s' "$remaining" | while IFS= read -r l; do [ -n "$l" ] && say "    $l"; done
            die 1 "tree is dirty — commit it, or pass --allow-dirty"
        fi
    fi
fi

# Odd, not wrong — say so and carry on.
if [ "$HAVE_GIT" -eq 1 ]; then
    git -C "$REPO" symbolic-ref -q HEAD >/dev/null || say "WARNING: detached HEAD"
    for m in rebase-merge rebase-apply MERGE_HEAD; do
        [ -e "$(git -C "$REPO" rev-parse --git-dir)/$m" ] && say "WARNING: $m in progress"
    done
fi

# Claude Code asks "is this a project you trust?" the first time it opens a
# directory, and --dangerously-skip-permissions does NOT bypass it. A successor
# spawned into an untrusted folder sits on that prompt forever: no tool call, no
# transcript, and the launcher reports a timeout that says nothing about the
# cause. Measured 2026-08-19. Check it BEFORE spawning so this is a precondition
# with an actionable message rather than a mystery three minutes later.
trust_state=$("$PYTHON_BIN" - "$REPO" "$TRUST" <<'PY'
import json, os, sys

repo, want_trust = os.path.realpath(sys.argv[1]), sys.argv[2] == "1"
path = os.path.expanduser("~/.claude.json")
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    print("unknown")           # cannot tell; do not block on a guess
    raise SystemExit(0)

projects = data.get("projects") or {}
entry = projects.get(repo) or projects.get(sys.argv[1]) or {}
if entry.get("hasTrustDialogAccepted"):
    print("trusted")
    raise SystemExit(0)
if not want_trust:
    print("untrusted")
    raise SystemExit(0)

projects.setdefault(repo, {})["hasTrustDialogAccepted"] = True
data["projects"] = projects
tmp = path + ".lastcall.tmp"
try:
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
except OSError:
    print("untrusted")
    raise SystemExit(0)
print("granted")
PY
) || trust_state="unknown"

case "$trust_state" in
    untrusted)
        say "Claude Code has not been told this folder is trusted:"
        say "    $REPO"
        say "A successor spawned here would stop on the trust prompt and never"
        say "act. --dangerously-skip-permissions does not cover it."
        die 1 "untrusted workspace — open it once and accept, or pass --trust"
        ;;
    granted)  say "trust: granted for $REPO (recorded in ~/.claude.json)" ;;
    trusted)  say "trust: already accepted" ;;
    *)        say "trust: could not read ~/.claude.json — continuing" ;;
esac

command -v "$GIT_BIN" >/dev/null 2>&1 || HAVE_GIT=0
command -v "$TMUX_BIN" >/dev/null 2>&1 || die 1 "tmux not found: $TMUX_BIN — the relay is tmux-only"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die 1 "python3 not found: $PYTHON_BIN"
command -v "$CLAUDE_BIN" >/dev/null 2>&1 || die 1 "claude CLI not found: $CLAUDE_BIN"

# The session to retire. Empty simply means there is no predecessor — running
# this outside tmux is legitimate.
OLD=""
if [ -n "${TMUX_PANE:-}" ]; then
    OLD=$("$TMUX_BIN" display-message -p -t "${TMUX_PANE}" '#S' 2>/dev/null || true)
fi
say "predecessor: ${OLD:-<none>}"

if [ -z "$NAME_PREFIX" ]; then
    # Name sessions after the repo, sanitised: tmux treats ':' and '.' as
    # target separators, so a name containing them cannot be addressed.
    NAME_PREFIX=$(basename "$REPO" | tr -c 'A-Za-z0-9_-' '-' | sed 's/-*$//')
    [ -n "$NAME_PREFIX" ] || NAME_PREFIX="lastcall"
fi

# "=" forces an exact match. Without it tmux matches by prefix, so a fresh
# repo-0818-1200 would collide with an existing repo-0818-1200-2.
base="$NAME_PREFIX-$(date +%m%d-%H%M)"
NEW="$base"
n=1
while "$TMUX_BIN" has-session -t "=$NEW" 2>/dev/null; do
    n=$((n + 1))
    [ "$n" -gt 20 ] && die 1 "20 session names taken starting at $base"
    NEW="$base-$n"
done

# /proc/sys/kernel/random/uuid does not exist on macOS, so generate it portably.
SID=$("$PYTHON_BIN" -c 'import uuid; print(uuid.uuid4())')
# Claude Code slugifies the project path by replacing EVERY non-alphanumeric
# character, not just the separators — a path containing "_" or "." lands
# somewhere else entirely if you only substitute "/".
SLUG=$("$PYTHON_BIN" - "$REPO" <<'PY'
import re, sys
print(re.sub(r'[^A-Za-z0-9]', '-', sys.argv[1]))
PY
)
PROJECTS_DIR=${PROJECTS_DIR:-$HOME/.claude/projects/$SLUG}
TRANSCRIPT="$PROJECTS_DIR/$SID.jsonl"

PROMPT="read $HANDOFF and follow it."
if [ -n "$OLD" ] && [ "$KILL_PREDECESSOR" -eq 1 ]; then
    # The launcher retires the predecessor itself, so do not also ask the
    # successor to. Two things racing to kill the same session is confusing to
    # read in a transcript and pointless.
    PROMPT="$PROMPT The previous session is being retired automatically once you have started work; you do not need to kill it."
elif [ -n "$OLD" ]; then
    # $OLD is quoted inside the instruction: the successor pastes this into a
    # shell, and a session name it cannot quote is a command it cannot run.
    PROMPT="$PROMPT When you have finished the first step and confirmed the environment is up: commit a checkpoint recording that it passed and carrying anything you have changed (use --allow-empty if nothing has), and only then run: tmux kill-session -t $(printf '%q' "$OLD") . If that first step fails, do NOT kill it — report instead."
fi

# Quote per argument. Hand-concatenating this is how the prompt arrives split
# across several argv slots, or how a backtick in a path gets executed.
# Build argv piecewise. --remote-control names the successor so you can reach it
# from anywhere rather than only from the pane it was born in; it is what makes
# an unattended relay usable rather than merely alive.
ARGS=("$CLAUDE_BIN")
# Which model drives the successor. --fallback-model is Claude Code's own
# switching: a comma-separated list it tries in turn when the first is
# overloaded or unavailable, so "run on fable, drop to opus when fable is
# full" needs no logic here at all.
[ -n "$MODEL" ] && ARGS+=(--model "$MODEL")
[ -n "$FALLBACK_MODEL" ] && ARGS+=(--fallback-model "$FALLBACK_MODEL")
[ "$REMOTE_CONTROL" -eq 1 ] && ARGS+=(--remote-control "$NEW")
ARGS+=(--session-id "$SID")
[ "$SKIP_PERMISSIONS" -eq 1 ] && ARGS+=(--dangerously-skip-permissions)
ARGS+=("$PROMPT")
CMD=$(printf '%q ' "${ARGS[@]}")

say "successor:  $NEW"
say "session-id: $SID"
say "transcript: $TRANSCRIPT"
say "permissions: $([ "$SKIP_PERMISSIONS" -eq 1 ] && echo "SKIPPED (unattended)" || echo "normal (successor may block on a prompt)")"
say "remote control: $([ "$REMOTE_CONTROL" -eq 1 ] && echo "on as $NEW" || echo off)"
say "retire old:  $([ "$KILL_PREDECESSOR" -eq 1 ] && echo "yes, after the successor proves itself" || echo "no, this session stays")"
say "model:      ${MODEL:-<session default>}${FALLBACK_MODEL:+  fallback: $FALLBACK_MODEL}"
say "argv:       $CMD"

# Absolute and quoted — this line gets copied into a shell, possibly from a
# session that no longer has this working directory or this handoff path intact.
RESPAWN="$(printf '%q' "$0") --repo $(printf '%q' "$REPO") --handoff $(printf '%q' "$HANDOFF")"

report_respawn() {
    say "attach:  $TMUX_BIN attach -t $NEW"
    say "respawn: $RESPAWN"
}

if [ "$DRY_RUN" -eq 1 ]; then
    say "dry run — nothing spawned"
    exit 0
fi

# ----------------------------------------------------------------------- spawn
# Three steps, and the order matters. Setting remain-on-exit after starting the
# real command races it: a command that exits instantly takes the session with
# it before set-option runs, and the launcher then dies on the option failure —
# reporting a precondition failure for what is actually a successor that died.
# The placeholder cannot exit during that window.
"$TMUX_BIN" new-session -d -s "$NEW" -c "$REPO" "sleep 86400" \
    || die 1 "tmux could not create session $NEW"
# From here a session EXISTS, so every failure is exit 2 — "spawned but never
# proved itself" — not exit 1. remain-on-exit is a WINDOW option, so the target
# must be a window and the flag must say so: `set-option -t <session>` fails
# with "no such window".
"$TMUX_BIN" set-option -w -t "=$NEW":0 remain-on-exit on >/dev/null \
    || { report_respawn; die 2 "tmux could not set remain-on-exit on $NEW"; }
"$TMUX_BIN" respawn-pane -k -t "=$NEW":0.0 "$CMD" \
    || { report_respawn; die 2 "tmux could not start the successor in $NEW"; }
say "spawned"

# has-session is NOT liveness: with remain-on-exit on and the command exited it
# still returns success, while pane_dead is 1.
pane_alive() {
    local d
    d=$("$TMUX_BIN" display-message -p -t "=$NEW":0.0 '#{pane_dead}' 2>/dev/null) || return 1
    [ "$d" = "0" ]
}

# An assistant turn is not proof of work — a refusal is an assistant turn. A
# tool_use block is: it means the successor took a concrete action, which a
# process sitting on a permission dialog cannot do.
started_work() {
    "$PYTHON_BIN" - "$TRANSCRIPT" <<'PY'
import json, os, sys

path = sys.argv[1]
if not os.path.exists(path):
    sys.exit(1)
with open(path, errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    sys.exit(0)
sys.exit(1)
PY
}

deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
    if ! pane_alive; then
        report_respawn
        die 2 "successor died before doing anything — attach and read the pane"
    fi
    if started_work; then
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        report_respawn
        die 2 "no tool call from the successor in ${TIMEOUT}s — watched $TRANSCRIPT"
    fi
    sleep 2
done

say "successor took its first action; settling ${SETTLE}s"
sleep "$SETTLE"
if ! pane_alive; then
    report_respawn
    die 2 "successor acted and then died — attach and read the pane"
fi

say "OK — $NEW is up and working"

if [ -n "$OLD" ] && [ "$KILL_PREDECESSOR" -eq 1 ]; then
    # Detached, and only from here: this point is reached only after the
    # successor made a real tool call AND survived the settle, so the session
    # being retired is being replaced by one proven to work.
    #
    # It has to be detached because this script is RUNNING INSIDE the session
    # it is about to kill. Killing it inline would take the launcher with it
    # mid-write: no final log, no exit status, no report. The delay lets this
    # process finish reporting and exit first.
    say "retiring $OLD in ${KILL_DELAY}s (successor proved itself)"
    setsid nohup sh -c \
        "sleep $KILL_DELAY; $TMUX_BIN kill-session -t '=$OLD' >/dev/null 2>&1" \
        >/dev/null 2>&1 </dev/null &
    disown 2>/dev/null || true
elif [ -n "$OLD" ]; then
    say "it will retire $OLD once it has proved its first step and committed a checkpoint"
fi
report_respawn
exit 0
