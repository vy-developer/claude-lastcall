#!/usr/bin/env python3
"""Context Guard — a Claude Code hook that watches how full the session is.

Claude Code sessions degrade quietly. Once the context window fills, older
material is summarized away and the assistant carries on working from a lossy
memory of files it *thinks* it read. The failure looks like confidence.

This hook measures the real number and, when it crosses a threshold, tells the
assistant to stop starting new work and wrap up.

DESIGN — silent while green. It must not spend context warning about context,
so below the threshold it exits 0 with no output and nothing reaches the model.
It speaks only when the BAND CHANGES, so the instruction lands once, at the
moment it matters.

DESIGN — fail passive, never fail green. If the window size is unknown, or the
transcript cannot be read, or anything at all goes wrong, this stays quiet
rather than guessing. A guard that guesses is worse than no guard, because its
wrong answer is indistinguishable from "nothing to warn about". Run
`context_guard.py doctor` to see what it actually resolved.

Events (all optional, register what you want):
  Stop         measure and warn/block                     [the actual guard]
  SessionStart clear stale state for a fresh session
  PostCompact  re-arm after compaction dropped the usage

No third-party dependencies. Python 3.9+.
"""

import json
import os
import re
import sys
import time

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# Defaults. Every one of these is overridable by config file or environment.
# --------------------------------------------------------------------------

DEFAULTS = {
    # Percentages of the context window, not absolute tokens. Absolute
    # thresholds are why the original version of this tool was a silent no-op
    # on any model that wasn't the one its author used.
    "yellow_percent": 70,
    "red_percent": 85,
    # None means "work it out from an exact source, or stay silent". Setting it
    # explicitly is the escape hatch when no exact source is available — see
    # resolve_window for why this is never inferred from the model name.
    "context_window_tokens": None,
    # "advisory"   — warn at both bands, never block.
    # "block_once" — warn at yellow; at red also block the stop ONE time so the
    #                wrap-up actually gets written before the session ends.
    "mode": "block_once",
    # Path to your own wrap-up instructions. Relative paths resolve against the
    # project directory. Without one you get a short generic message.
    "template": None,
    # Claude Code reports context-used as input + cache_read + cache_creation.
    # Output is excluded because it is not yet in the window. Turn this on if
    # you would rather budget for the NEXT turn's input, which does include it.
    "include_output_tokens": False,
    # Opt-in, redacted, size-capped. Never on by default: hook payloads carry
    # the full text of the last assistant message.
    "debug": False,
    "state_dir": None,          # default: ~/.claude/context-guard
    "state_ttl_days": 14,
    "disabled": False,
}

ENV_PREFIX = "CONTEXT_GUARD_"

# The window CANNOT be inferred from the model identifier. Measured on a real
# transcript: a session running the 1M-context Opus records itself as plain
# "claude-opus-5", identical to the 200K variant, and reported 743,106 tokens in
# use — 371% of the window that identifier implies. There is no marker in the
# transcript that distinguishes them.
#
# So this guesses at nothing. The window comes from an exact source or the guard
# stays silent. The one inference it will make is a proof, not a guess: tokens
# already in the window are a hard lower bound on the window's size.
STANDARD_WINDOW = 200_000
EXTENDED_WINDOW = 1_000_000
KNOWN_WINDOWS = (STANDARD_WINDOW, EXTENDED_WINDOW)

# How far back to read a transcript before giving up. The newest usage record
# is almost always within a few KB of the end; this bound exists so a
# multi-hundred-MB transcript cannot stall the hook.
_TAIL_LIMIT_BYTES = 8 * 1024 * 1024
_TAIL_CHUNK_BYTES = 256 * 1024

# A drop this steep can only be compaction — normal turns add tokens, they do
# not remove three fifths of them.
_COMPACTION_DROP_RATIO = 0.6

DEFAULT_TEMPLATE = """\
Wrap up this session rather than starting anything new.

  1. FINISH what is already in flight. Use your judgment about what is small
     enough to land — a two-line fix is fine, a new phase is not.
  2. RECORD the state of the work somewhere durable, so it survives this
     session ending.
  3. WRITE the next session's starting instructions. Not a summary of what
     happened — instructions, written for someone with zero context: how to
     bring the environment up and prove it, what was done, what was left out
     and why, what to do next and where, and which decisions are already
     settled so they do not get re-litigated.
  4. VERIFY that record against the actual state of the repository, not
     against your memory of the session. Your memory is the thing that is
     running out.

Configure this text: set "template" in .claude/context-guard.json."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def project_dir(payload):
    """Where the user's config lives.

    CLAUDE_PROJECT_DIR is set by Claude Code for hooks and is the project root.
    `cwd` in the payload is wherever the session happens to be *now*, which may
    be a subdirectory, so it is only a starting point to search upward from.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isdir(env):
        return env
    start = payload.get("cwd") or os.getcwd()
    path = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(path, ".claude")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.abspath(start)
        path = parent


# Coercion is driven by the key, never by the default value. Inferring it from
# the default means every field defaulting to None looks numeric, so an
# environment override like CONTEXT_GUARD_STATE_DIR=/tmp/x parses as a failed
# number and silently becomes None — the override vanishes without a word.
_FLOAT_KEYS = frozenset(("yellow_percent", "red_percent"))
_INT_KEYS = frozenset(("context_window_tokens", "state_ttl_days"))
_BOOL_KEYS = frozenset(("include_output_tokens", "debug", "disabled"))


def _coerce(key, value):
    """Environment variables arrive as strings; config values arrive typed."""
    if key in _BOOL_KEYS:
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if key in _FLOAT_KEYS or key in _INT_KEYS:
        text = str(value).strip()
        if text == "" or text.lower() in ("none", "null", "auto"):
            return None
        try:
            number = float(text)
        except ValueError:
            return DEFAULTS[key]
        return number if key in _FLOAT_KEYS else int(number)
    text = str(value).strip()
    return text or None


def load_config(payload):
    """defaults < .claude/context-guard.json < environment."""
    config = dict(DEFAULTS)
    root = project_dir(payload)

    path = os.path.join(root, ".claude", "context-guard.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for key, value in (json.load(handle) or {}).items():
                    if key in config:
                        config[key] = value
        except (OSError, ValueError):
            # A broken config must not take the session with it. The doctor
            # command reports this loudly; the hook path stays quiet.
            pass

    # Environment overrides individual fields rather than replacing the whole
    # config, so CONTEXT_GUARD_RED=90 for one run keeps everything else.
    for key in config:
        env_value = os.environ.get(ENV_PREFIX + key.upper())
        if env_value is not None:
            config[key] = _coerce(key, env_value)

    config["_project_dir"] = root
    config["_config_path"] = path if os.path.isfile(path) else None
    return config


def state_dir(config):
    override = config.get("state_dir")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.claude/context-guard")


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def iter_lines_reverse(path, limit=_TAIL_LIMIT_BYTES, chunk=_TAIL_CHUNK_BYTES):
    """Yield non-empty lines from the end of a file backwards.

    The record we want is the newest one, so reading forward means parsing the
    entire session history — every tool result, every file read — on every
    single Stop. That cost grows without bound over a long session, which is
    exactly when the guard matters most.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        pending = b""
        consumed = 0
        while position > 0 and consumed < limit:
            step = min(chunk, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            consumed += step
            pending = block + pending
            parts = pending.split(b"\n")
            # parts[0] may be a fragment of a line whose start is further back,
            # so it is held over until the next block completes it.
            pending = parts.pop(0)
            for line in reversed(parts):
                if line.strip():
                    yield line
        if position == 0 and pending.strip():
            yield pending


def latest_usage(transcript, session_id=None):
    """The newest main-agent usage record, as (usage, model).

    Filtered deliberately. Subagents and sidechains have their own usage blocks
    and their own windows; counting one of those as the main session's context
    reports a number belonging to a different conversation entirely.
    """
    for raw in iter_lines_reverse(transcript):
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain"):
            continue
        entry_session = entry.get("sessionId")
        if session_id and entry_session and entry_session != session_id:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if isinstance(usage, dict) and usage:
            return usage, message.get("model")
    return None, None


def count_tokens(usage, include_output=False):
    """Sum a usage block the way Claude Code measures context.

    cache_read dominates and is the reason a naive read of input_tokens alone
    reports something like 2 on a session actually holding 690,000.
    """
    total = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
    if include_output:
        total += int(usage.get("output_tokens") or 0)
    return total


def window_from_evidence(observed_peak):
    """The only window claim this tool will make without being told.

    A session cannot hold more tokens than its window, so an observed count is
    a floor. Above 200K the window is provably not the standard one, which
    leaves exactly one known option. Below that, 200K and 1M are
    indistinguishable and the honest answer is "I do not know".
    """
    if not observed_peak or observed_peak <= STANDARD_WINDOW:
        return None
    for window in KNOWN_WINDOWS:
        if observed_peak <= window:
            return window
    return None


def resolve_window(config, state=None):
    """Exact sources first, proof second, silence third."""
    configured = config.get("context_window_tokens")
    if configured:
        return int(configured), "config"

    # Written by the optional status-line helper, which receives the real
    # context_window_size from Claude Code. This is the only automatic source
    # that is exact rather than deduced.
    state = state or {}
    observed = state.get("window_from_statusline")
    if observed:
        return int(observed), "statusline"

    # max_observed is monotonic for the life of the session. It must NOT be the
    # same counter compaction resets: compaction changes how full the window is,
    # never how big it is, and throwing the evidence away would un-learn the
    # window every time the session compacted.
    observed_peak = state.get("max_observed")
    proven = window_from_evidence(observed_peak)
    if proven:
        return proven, "proven by %s tokens observed" % "{:,}".format(observed_peak)

    return None, "unknown"


def band_for(percent, config):
    yellow = float(config["yellow_percent"])
    red = float(config["red_percent"])
    if percent >= red:
        return "red"
    if percent >= yellow:
        return "yellow"
    return "green"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def state_path(config, session_id):
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", session_id or "unknown")
    return os.path.join(state_dir(config), "%s.json" % safe)


def read_state(config, session_id):
    try:
        with open(state_path(config, session_id), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(config, session_id, state):
    """Atomic replace. A half-written state file reads as corrupt on the next
    turn, and corrupt state silences the guard for the rest of the session."""
    directory = state_dir(config)
    path = state_path(config, session_id)
    state["updated"] = int(time.time())
    try:
        os.makedirs(directory, exist_ok=True)
        temporary = path + ".tmp%d" % os.getpid()
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, path)
    except OSError:
        pass


def prune_state(config):
    """Old sessions never come back; their state files should not outlive them."""
    ttl_days = config.get("state_ttl_days") or 0
    if ttl_days <= 0:
        return
    cutoff = time.time() - (ttl_days * 86400)
    directory = state_dir(config)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        target = os.path.join(directory, name)
        try:
            if os.path.getmtime(target) < cutoff:
                os.remove(target)
        except OSError:
            pass


def write_debug(config, payload):
    """Opt-in, redacted, capped. The raw payload contains the entire text of the
    last assistant message, so it is never written unless explicitly asked for."""
    if not config.get("debug"):
        return
    redacted = {
        key: value
        for key, value in payload.items()
        if key not in ("last_assistant_message",)
    }
    redacted["_redacted"] = ["last_assistant_message"]
    try:
        os.makedirs(state_dir(config), exist_ok=True)
        target = os.path.join(state_dir(config), "last-payload.json")
        text = json.dumps(redacted, indent=2)[:100_000]
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Message
# --------------------------------------------------------------------------

def load_template(config):
    path = config.get("template")
    if not path:
        return DEFAULT_TEMPLATE
    if not os.path.isabs(path):
        path = os.path.join(config["_project_dir"], path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return DEFAULT_TEMPLATE


def render(config, band, tokens, window):
    percent = (tokens * 100.0) / window
    if band == "red":
        header = (
            "CONTEXT GUARD — RED. {percent:.0f}% of the context window is in use "
            "({tokens:,} of {window:,} tokens; {remaining:,} left).\n"
            "Do not continue normal work. Wrap-up only — do not start, resume, "
            "or 'quickly finish' anything."
        )
    else:
        header = (
            "CONTEXT GUARD — YELLOW. {percent:.0f}% of the context window is in "
            "use ({tokens:,} of {window:,} tokens; {remaining:,} left).\n"
            "Wrap up; do not stop dead. This is an alarm, not a decision — you "
            "judge what still fits — but start no new phase or feature."
        )
    values = {
        "percent": percent,
        "tokens": tokens,
        "window": window,
        "remaining": max(0, window - tokens),
        "band": band,
    }
    body = load_template(config)
    try:
        body = body.format(**values)
    except (KeyError, IndexError, ValueError):
        # A template with a stray brace is the user's problem to fix, not a
        # reason to withhold the warning entirely.
        pass
    return header.format(**values) + "\n\n" + body


def emit(event, message, block_reason=None):
    output = {
        "suppressOutput": True,
        "hookSpecificOutput": {
            # REQUIRED. Without it the CLI rejects the whole payload with a
            # validation error, the hook still reports success, and nothing
            # reaches the model — a warning system whose failure mode is
            # indistinguishable from having nothing to warn about.
            "hookEventName": event,
            "additionalContext": message,
        },
    }
    if block_reason:
        output["decision"] = "block"
        output["reason"] = block_reason
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

def measure(config, payload, state=None):
    """Returns (tokens, window, source, model).

    tokens is None only when the transcript cannot be read at all. window is
    None when the size is genuinely unknown — the caller must treat that as
    "stay silent", never as "green"."""
    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        # Deliberately no path derivation from cwd. Claude Code's project slug
        # replaces every non-alphanumeric character, and `cwd` may be a
        # subdirectory of the project anyway, so a derived path is wrong in two
        # independent ways and lands on a file that does not exist.
        return None, None, "no-transcript", None
    try:
        usage, model = latest_usage(transcript, payload.get("session_id"))
    except OSError:
        return None, None, "unreadable-transcript", None
    if not usage:
        return None, None, "no-usage-record", None

    tokens = count_tokens(usage, config.get("include_output_tokens"))
    # Fold the current reading into the evidence before resolving, so a session
    # that is already past 200K proves its own window on the very first Stop.
    evidence = dict(state or {})
    evidence["max_observed"] = max(int(evidence.get("max_observed") or 0), tokens)
    window, source = resolve_window(config, evidence)
    return tokens, window, source, model


def handle_stop(config, payload):
    event = payload.get("hook_event_name") or "Stop"
    session_id = payload.get("session_id")

    # A Stop hook that blocks repeatedly gets overridden by Claude Code, and a
    # blocked stop re-enters here. Short-circuit so the guard can never trap a
    # session in a loop of its own making.
    if payload.get("stop_hook_active"):
        return 0

    state = read_state(config, session_id)
    tokens, window, _source, _model = measure(config, payload, state)
    if tokens is None:
        return 0  # cannot measure -> stay silent rather than guess

    peak = int(state.get("peak") or 0)
    state["max_observed"] = max(int(state.get("max_observed") or 0), tokens)
    if window is None:
        # Size unknown, so no band can be computed. Still record the reading:
        # it is the evidence that may resolve the window on a later turn.
        state["peak"] = max(peak, tokens)
        write_state(config, session_id, state)
        return 0

    percent = (tokens * 100.0) / window
    band = band_for(percent, config)
    previous = state.get("band", "green")

    # Compaction re-arm. After a compact the live usage drops back to green,
    # and the guard has to be able to warn again on the way back up. Detecting
    # it here means the guard re-arms correctly even where the PostCompact hook
    # is not registered.
    if peak and tokens < peak * _COMPACTION_DROP_RATIO:
        previous = "green"
        peak = tokens

    state["peak"] = max(peak, tokens)
    state["band"] = band

    if band == "green":
        # Recorded even on green — this is what re-arms the bands. The original
        # version returned early here, so the state never cleared and a session
        # that crossed yellow once never warned again.
        write_state(config, session_id, state)
        return 0

    if band == previous:
        write_state(config, session_id, state)
        return 0  # already said this; do not repeat it every turn

    message = render(config, band, tokens, window)
    reason = None
    if band == "red" and config.get("mode") == "block_once":
        reason = "Context is in the red band — write the handoff before stopping."

    emit(event, message, reason)
    # Bookkeeping only after the payload is out, and only after a successful
    # flush. Recording the band before delivering it means a rejected payload
    # marks the band as "already announced" and the guard goes permanently
    # silent — the bug becomes unobservable at exactly the moment it starts.
    write_state(config, session_id, state)
    return 0


def handle_reset(config, payload):
    """SessionStart / PostCompact: forget the band so it can fire again."""
    session_id = payload.get("session_id")
    state = read_state(config, session_id)
    state["band"] = "green"
    state["peak"] = 0
    state["epoch"] = int(state.get("epoch") or 0) + 1
    # max_observed and window_from_statusline deliberately survive: they
    # describe the window's SIZE, which compaction does not change.
    write_state(config, session_id, state)
    prune_state(config)
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def doctor(argv):
    """Show exactly what the guard resolves, so a silent guard is diagnosable.

    The whole failure mode of this class of tool is looking healthy while doing
    nothing. This is the antidote: run it and see the real numbers.
    """
    payload = {"cwd": os.getcwd()}
    transcript = None
    for arg in argv:
        if not arg.startswith("-"):
            transcript = arg
    config = load_config(payload)

    print("context-guard %s" % __version__)
    print("  project dir   : %s" % config["_project_dir"])
    print("  config file   : %s" % (config["_config_path"] or "(none — using defaults)"))
    print("  state dir     : %s" % state_dir(config))
    print("  thresholds    : yellow %s%%  red %s%%" % (config["yellow_percent"], config["red_percent"]))
    print("  mode          : %s" % config["mode"])
    print("  include output: %s" % bool(config["include_output_tokens"]))
    print("  template      : %s" % (config["template"] or "(built-in default)"))
    print("  disabled      : %s" % bool(config["disabled"]))

    if not transcript:
        print("\nPass a transcript path to measure a real session, e.g.")
        print("  context_guard.py doctor ~/.claude/projects/<project>/<session>.jsonl")
        return 0

    payload["transcript_path"] = transcript
    tokens, window, source, model = measure(config, payload)
    print("\n  transcript    : %s" % transcript)
    if tokens is None:
        print("  RESULT        : cannot measure (%s)" % source)
        print("  -> the guard would stay SILENT for this session.")
        return 1
    print("  model         : %s" % (model or "(unknown)"))
    print("  in use        : %s tokens" % "{:,}".format(tokens))
    if window is None:
        print("  window        : UNKNOWN")
        print("\n  -> the guard stays SILENT: it will not guess the window size.")
        print("     A model identifier does not reveal it — the 1M and 200K")
        print("     variants record the same name. Fix it either way:")
        print("       1. echo '{\"context_window_tokens\": 200000}' > .claude/context-guard.json")
        print("       2. or install the status line, which is handed the real")
        print("          number by Claude Code (see README).")
        return 1
    percent = (tokens * 100.0) / window
    print("  window        : %s tokens (%s)" % ("{:,}".format(window), source))
    print("  percent       : %.1f%%" % percent)
    print("  band          : %s" % band_for(percent, config).upper())
    print("  headroom      : %s tokens" % "{:,}".format(max(0, window - tokens)))
    return 0


# --------------------------------------------------------------------------

def main(argv):
    if argv and argv[0] in ("doctor", "--doctor"):
        return doctor(argv[1:])
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return 0

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return 0
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return 0  # never break a session over a parse failure
    if not isinstance(payload, dict):
        return 0

    config = load_config(payload)
    if config.get("disabled"):
        return 0
    write_debug(config, payload)

    event = payload.get("hook_event_name") or (argv[0] if argv else "Stop")
    if event in ("SessionStart", "PostCompact", "PreCompact"):
        return handle_reset(config, payload)
    return handle_stop(config, payload)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # noqa: BLE001 - a guard must never take the session down
        if os.environ.get(ENV_PREFIX + "TRACE"):
            raise
        sys.exit(0)
