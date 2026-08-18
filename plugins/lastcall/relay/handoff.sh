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
# Requires: bash, git, tmux, python3, and the `claude` CLI.
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
SKIP_PERMISSIONS=0
NEW=""

usage() {
    cat <<'EOF'
usage: handoff.sh [options]

  --repo <path>          repository to hand over (default: $CLAUDE_PROJECT_DIR,
                         else the git toplevel of the working directory)
  --handoff <path>       use this handoff instead of the newest
  --handoff-dir <path>   where handoffs live, repo-relative (default docs/handoff)
  --allow-dirty          spawn even though the tree has uncommitted work
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
        --handoff)      HANDOFF=${2:?--handoff needs a path}; shift 2 ;;
        --handoff-dir)  HANDOFF_DIR=${2:?--handoff-dir needs a path}; shift 2 ;;
        --allow-dirty)  ALLOW_DIRTY=1; shift ;;
        --skip-permissions) SKIP_PERMISSIONS=1; shift ;;
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

# Resolve the repo before anything else needs it.
if [ -z "$REPO" ]; then
    REPO=${CLAUDE_PROJECT_DIR:-}
fi
if [ -z "$REPO" ]; then
    REPO=$(git rev-parse --show-toplevel 2>/dev/null || true)
fi
[ -n "$REPO" ] || { echo "cannot work out which repo to hand over — pass --repo" >&2; exit 1; }

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

[ -d "$REPO" ] || die 1 "no such repo: $REPO"
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die 1 "not a git worktree: $REPO"
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

if [ "$ALLOW_DIRTY" -eq 0 ]; then
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
git -C "$REPO" symbolic-ref -q HEAD >/dev/null || say "WARNING: detached HEAD"
for m in rebase-merge rebase-apply MERGE_HEAD; do
    [ -e "$(git -C "$REPO" rev-parse --git-dir)/$m" ] && say "WARNING: $m in progress"
done

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
if [ -n "$OLD" ]; then
    # $OLD is quoted inside the instruction: the successor pastes this into a
    # shell, and a session name it cannot quote is a command it cannot run.
    PROMPT="$PROMPT When you have finished the first step and confirmed the environment is up: commit a checkpoint recording that it passed and carrying anything you have changed (use --allow-empty if nothing has), and only then run: tmux kill-session -t $(printf '%q' "$OLD") . If that first step fails, do NOT kill it — report instead."
fi

# Quote per argument. Hand-concatenating this is how the prompt arrives split
# across several argv slots, or how a backtick in a path gets executed.
if [ "$SKIP_PERMISSIONS" -eq 1 ]; then
    CMD=$(printf '%q ' "$CLAUDE_BIN" --session-id "$SID" \
        --dangerously-skip-permissions "$PROMPT")
else
    CMD=$(printf '%q ' "$CLAUDE_BIN" --session-id "$SID" "$PROMPT")
fi

say "successor:  $NEW"
say "session-id: $SID"
say "transcript: $TRANSCRIPT"
say "permissions: $([ "$SKIP_PERMISSIONS" -eq 1 ] && echo skipped || echo "normal (successor may block on a prompt)")"
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
[ -n "$OLD" ] && say "it will retire $OLD once it has proved its first step and committed a checkpoint"
report_respawn
exit 0
