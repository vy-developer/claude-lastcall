#!/usr/bin/env python3
"""Last Call — a Claude Code hook that watches how full the session is.

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
`lastcall.py doctor` to see what it actually resolved.

Events (all optional, register what you want):
  Stop         measure and warn/block                     [the actual guard]
  SessionStart clear stale state for a fresh session
  PostCompact  re-arm after compaction dropped the usage

No third-party dependencies. Python 3.9+.
"""

import json
import os
import re
import string
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
    #
    # 40/55 is not a guess. It is the ladder from the hook this was rewritten
    # from, which has run ~50 unattended session handoffs over a fortnight, and
    # its author's reasoning is worth repeating: long-context quality degrades
    # well before the window is full, and Anthropic's own agent harness compacts
    # its orchestrator at 100k while capping subagents at 200k. Firing late is
    # the failure that actually costs you a session — by the time the model is
    # at 85% it has already been working from a lossy memory for a while.
    # The ~15-point gap gives in-flight work room to land before red.
    "yellow_percent": 40,
    "red_percent": 55,
    # Define your own zones and you get full control: as many as you like, your
    # names, your thresholds, your instructions, and which ones hold the stop.
    # None means "build the standard two from the percentages above".
    #   [{"name": "wind-down", "at": 60, "template": ".claude/winddown.md"},
    #    {"name": "closing",   "at": 85, "block": true}]
    "zones": None,
    # Commands that must pass before handing over — tests, linters, a review
    # gate. They are surfaced to the assistant as {gates} in your wrap-up
    # template; nothing here executes them, because a hook that runs your test
    # suite at Stop time is a hook that hangs your session.
    "gates": None,
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
    "state_dir": None,          # default: ~/.claude/lastcall
    "state_ttl_days": 14,
    "disabled": False,
}

ENV_PREFIX = "LASTCALL_"

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

# The optional relay ships alongside this file. Templates get its absolute path
# as {relay} so a wrap-up can say "run this" without the user hand-editing a
# path that changes with every plugin update.
RELAY_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "relay", "handoff.sh")
HANDOFF_SKELETON = """\
# Session handoff — {date}

Written for someone with ZERO context. Not a summary of what happened: the
next session's instructions. If a line here would not change what they do,
cut it.

## 0. Step 0 — bring the environment up, and PROVE it

{verify_block}

State the EXPECTED result of each command, not just the command. "It should
return 200" is a proof; "check the server is running" is not. If a check can
pass while the system is broken, say so explicitly.

## 1. Where things stand

What is finished and committed. What is half-done and where. One paragraph.

## 2. Your first work

The single next thing to do, and WHERE. Be specific enough that they can start
without reading anything else.

## 3. What the last session did

Only what changes what happens next. Include what was attempted and abandoned,
and why — otherwise it gets attempted again.

## 4. How to work here

The rules of this repository: how to test, what gates a change, what must never
be done. Point at the files rather than restating them.

Include how work should be PARALLELISED here, because a fresh session will
otherwise do everything sequentially in one context and decay:

  - a subagent for a one-shot task — run it, return the result, context
    discarded. Use it to keep exploration and research out of the main window.
  - a teammate when the context must persist and you will come back to it.
  - a workflow when many things run in parallel across distinct stages.

Name the concrete gates a change must pass, and who runs them.

## 5. Decided — do not re-ask

Decisions already taken, so the next session does not reopen them. This section
is what stops a fresh context relitigating settled questions.

## 6. Where everything is

The handful of paths worth knowing on day one.
"""

RELAY_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "handoff-relay.md")

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

AUTOMATIC HANDOVER IS NOT SET UP for this project, so nothing will start a
successor session or carry this work forward — when you stop, the work stops.
Tell the user that, once, and point them at:

    python3 {setup} setup

Configure this text: set "template" in .claude/lastcall.json."""


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
# environment override like LASTCALL_STATE_DIR=/tmp/x parses as a failed
# number and silently becomes None — the override vanishes without a word.
_FLOAT_KEYS = frozenset(("yellow_percent", "red_percent"))
_INT_KEYS = frozenset(("context_window_tokens", "state_ttl_days"))
_BOOL_KEYS = frozenset(("include_output_tokens", "debug", "disabled"))
_JSON_KEYS = frozenset(("zones", "gates"))


def _coerce(key, value):
    """Environment variables arrive as strings; config values arrive typed."""
    if key in _JSON_KEYS:
        if isinstance(value, (list, tuple)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return DEFAULTS[key]
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
    """defaults < .claude/lastcall.json < environment."""
    config = dict(DEFAULTS)
    root = project_dir(payload)

    path = os.path.join(root, ".claude", "lastcall.json")
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
    # config, so LASTCALL_RED_PERCENT=90 for one run keeps everything else.
    #
    # Both cases are accepted. The config table documents field names in
    # lowercase, so someone copying a name straight out of it gets
    # LASTCALL_red_percent — which used to be ignored in silence, costing a
    # real user three rounds of debugging. For a tool whose whole value is
    # speaking up, silently dropping an override is the worst available
    # behaviour.
    for key in config:
        env_value = os.environ.get(ENV_PREFIX + key.upper())
        if env_value is None:
            env_value = os.environ.get(ENV_PREFIX + key)
        if env_value is not None:
            config[key] = _coerce(key, env_value)

    config["_project_dir"] = root
    config["_config_path"] = path if os.path.isfile(path) else None
    config["_problems"] = validate(config)
    return config


_EXPECTED_TYPES = {
    "yellow_percent": (int, float),
    "red_percent": (int, float),
    "context_window_tokens": (int,),
    "state_ttl_days": (int,),
    "mode": (str,),
    "template": (str,),
    "state_dir": (str,),
    "zones": (list, tuple),
    "gates": (list, tuple, str),
    "include_output_tokens": (bool,),
    "debug": (bool,),
    "disabled": (bool,),
}


def validate(config):
    """Replace unusable values with their defaults and say what was wrong.

    A misconfigured guard that goes silent is indistinguishable from a healthy
    one with nothing to report. Every problem found here is surfaced by doctor.
    """
    problems = []
    for key, types in _EXPECTED_TYPES.items():
        value = config.get(key)
        if value is None:
            continue
        if isinstance(value, bool) and bool not in types:
            problems.append("%s should be %s, got a boolean" % (key, types[0].__name__))
            config[key] = DEFAULTS[key]
        elif not isinstance(value, types):
            problems.append("%s should be %s, got %s"
                            % (key, types[0].__name__, type(value).__name__))
            config[key] = DEFAULTS[key]

    for key in ("yellow_percent", "red_percent"):
        value = config.get(key)
        if isinstance(value, (int, float)) and not 0 <= value <= 100:
            problems.append("%s is %s; it is a PERCENTAGE of the window, not a "
                            "token count" % (key, value))
            config[key] = DEFAULTS[key]

    if config.get("mode") not in ("advisory", "block_once"):
        problems.append('mode should be "advisory" or "block_once", got %r'
                        % config.get("mode"))
        config["mode"] = DEFAULTS["mode"]

    template = config.get("template")
    if template:
        resolved = template if os.path.isabs(template) else os.path.join(
            config["_project_dir"], template)
        if not os.path.isfile(resolved):
            problems.append("template does not exist: %s" % resolved)

    for zone in (config.get("zones") or []):
        if isinstance(zone, dict) and zone.get("name") not in _DEFAULT_HEADLINES \
                and not zone.get("headline") and not zone.get("message") \
                and not zone.get("template"):
            problems.append('zone "%s" has no headline, message or template of '
                            "its own, and only the names %s carry built-in "
                            "wording" % (zone.get("name"),
                                         "/".join(sorted(_DEFAULT_HEADLINES))))
    return problems


def state_dir(config):
    override = config.get("state_dir")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.claude/lastcall")


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
                # rstrip the CR: a transcript written on Windows is CRLF, and
                # splitting on LF alone leaves it dangling on every line.
                line = line.rstrip(b"\r")
                if line.strip():
                    yield line
        if position == 0:
            pending = pending.rstrip(b"\r")
            if pending.strip():
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


_DEFAULT_HEADLINES = {
    "yellow": ("Finish what is in flight; start nothing new. This is an alarm, "
               "not a decision — you judge what still fits."),
    "red": ("Closing time. Wrap-up only — do not start, resume, or 'quickly "
            "finish' anything."),
}


def resolve_zones(config):
    """The thresholds, lowest first.

    Two zones named yellow and red are just the default arrangement, not a
    built-in limit. A project that wants one gentle nudge at 50% and a hard
    stop at 90%, or four escalating steps, says so in config and nothing here
    treats that as unusual.
    """
    declared = config.get("zones")
    if not declared:
        declared = [
            {"name": "yellow", "at": config.get("yellow_percent")},
            {"name": "red", "at": config.get("red_percent"), "block": True},
        ]
    if not isinstance(declared, (list, tuple)):
        return []

    zones = []
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        try:
            at = float(entry.get("at"))
        except (TypeError, ValueError):
            continue  # a malformed zone is dropped, not fatal
        name = str(entry.get("name") or ("%g%%" % at))
        zones.append({
            "name": name,
            "at": at,
            # Replaces the generic body for this zone only. Inline text or a
            # file; the file wins if both are given.
            "message": entry.get("message"),
            "template": entry.get("template"),
            # One line after the numbers, before the instructions.
            "headline": entry.get("headline") or _DEFAULT_HEADLINES.get(name),
            "block": bool(entry.get("block", False)),
        })
    zones.sort(key=lambda zone: zone["at"])
    return zones


def zone_for(percent, zones):
    """The highest zone this reading has reached, or None while below them all."""
    current = None
    for zone in zones:  # ascending, so the last match is the highest
        if percent >= zone["at"]:
            current = zone
    return current


def band_for(percent, config):
    """Zone name for a reading, or "green" below every zone."""
    zone = zone_for(percent, resolve_zones(config))
    return zone["name"] if zone else "green"


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
            if os.path.getmtime(target) >= cutoff:
                continue
            # Delete only what this tool wrote. state_dir is user-settable, and
            # pointing it at ~/.claude used to mean settings.json was removed
            # after the TTL. Extension is not ownership.
            if not _is_our_state(target):
                continue
            os.remove(target)
        except OSError:
            pass


_STATE_KEYS = frozenset(("band", "peak", "max_observed", "epoch", "updated",
                        "window_from_statusline"))


def _is_our_state(path):
    """True only for a file this tool created."""
    if os.path.basename(path) == "last-payload.json":
        return True
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(_STATE_KEYS & set(data))


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

def read_template(config, path):
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(config["_project_dir"], path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    # Strip blank lines off each end, NOT whitespace. A plain .strip() eats the
    # leading indentation of the first line, so a template that opens with an
    # indented list item silently loses its alignment on that one line and
    # keeps it on every other — which reads as a corrupted message.
    return text.strip("\r\n").rstrip() or None


def zone_body(config, zone):
    """What this zone actually tells the assistant to do.

    Most specific first: this zone's own file, this zone's inline text, the
    project-wide template, then the generic built-in.
    """
    return (read_template(config, zone.get("template"))
            or zone.get("message")
            or read_template(config, config.get("template"))
            or DEFAULT_TEMPLATE)


class _Sensible(string.Formatter):
    """Format numbers the way a reader expects when no spec is given.

    A template that says "{percent}" wants "88", not "87.8746", and "{tokens}"
    wants "439,373", not "439373". Explicit specs still win, so "{percent:.1f}"
    and "{tokens:,}" keep working exactly as before.
    """

    def format_field(self, value, format_spec):
        if not format_spec:
            if isinstance(value, bool):
                pass
            elif isinstance(value, float):
                return format(value, ".0f")
            elif isinstance(value, int):
                return format(value, ",")
        return super(_Sensible, self).format_field(value, format_spec)


_FORMATTER = _Sensible()


def fill(text, values):
    """Interpolate a template, leaving it intact if the template is malformed.

    A stray brace in someone's wrap-up is their problem to fix; it is never a
    reason to withhold the warning entirely.
    """
    try:
        return _FORMATTER.vformat(text, (), values)
    except (KeyError, IndexError, ValueError, AttributeError):
        return text


def format_gates(config):
    gates = config.get("gates")
    if not gates:
        return "(none configured — set \"gates\" in .claude/lastcall.json)"
    if isinstance(gates, str):
        gates = [gates]
    return "\n".join("       %s" % str(gate) for gate in gates)


def render(config, zone, tokens, window, transcript=None):
    percent = (tokens * 100.0) / window
    values = {
        "percent": percent,
        "tokens": tokens,
        "window": window,
        "remaining": max(0, window - tokens),
        "band": zone["name"],
        "zone": zone["name"],
        "at": zone["at"],
        "relay": RELAY_SCRIPT,
        "setup": os.path.abspath(__file__),
        "gates": format_gates(config),
        # The assistant's own raw transcript. This is what makes "audit the
        # handoff against what actually happened" a real instruction rather
        # than a pious one: it can read the file instead of trusting the
        # memory that is, by definition, running out.
        "transcript": transcript or "(this session's transcript)",
    }
    header = (
        "LAST CALL — %s. {percent:.0f}%% of the context window is in use "
        "({tokens:,} of {window:,} tokens; {remaining:,} left)."
        % zone["name"].upper()
    )
    if zone.get("headline"):
        header += "\n" + zone["headline"]

    body = fill(zone_body(config, zone), values)
    return fill(header, values) + "\n\n" + body


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
    zone = zone_for(percent, resolve_zones(config))
    band = zone["name"] if zone else "green"
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

    if zone is None:
        # Recorded even below every zone — this is what re-arms them. The
        # original version returned early here, so the state never cleared and
        # a session that crossed yellow once never warned again.
        write_state(config, session_id, state)
        return 0

    if band == previous:
        write_state(config, session_id, state)
        return 0  # already said this; do not repeat it every turn

    message = render(config, zone, tokens, window,
                     transcript=payload.get("transcript_path"))
    reason = None
    if zone.get("block") and config.get("mode") == "block_once":
        reason = ("Context has reached the %s zone — write the handoff before "
                  "stopping." % zone["name"])

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

def handover_status(config):
    """What would actually happen at the end of a session, as facts.

    The whole point of this tool is that silent failure is the enemy. A guard
    that tells the assistant to hand over, in a project where nothing can
    receive the handover, is precisely that failure wearing a different hat.
    """
    import shutil

    template = config.get("template")
    body = None
    if template:
        body = read_template(config, template)
    zone_templates = [z.get("template") for z in resolve_zones(config)]
    for zone_template in zone_templates:
        if zone_template and not body:
            body = read_template(config, zone_template)

    # "{relay}" counts: it is the placeholder that BECOMES the relay path at
    # render time. Checking only for the resolved path reported a correctly
    # configured project as broken, which is the exact false alarm this
    # section exists to avoid.
    wired = bool(body and ("{relay}" in body
                           or RELAY_SCRIPT in body
                           or "handoff.sh" in body))
    checks = {
        "template configured": bool(template or any(zone_templates)),
        "template invokes the relay": wired,
        "relay script present": os.path.isfile(RELAY_SCRIPT),
        "tmux on PATH": bool(shutil.which("tmux")),
        "git on PATH": bool(shutil.which("git")),
        "claude CLI on PATH": bool(shutil.which("claude")),
    }
    ready = all(checks.values())
    return ready, checks


def setup(argv):
    """Interactive first-run configuration.

    Asks only what cannot be worked out from the machine, recommends an answer
    for each, and writes the handoff skeleton — because the document is the
    half of a handover that no script can produce for you.
    """
    import shutil

    payload = {"cwd": os.getcwd()}
    config = load_config(payload)
    root = config["_project_dir"]
    target = os.path.join(root, ".claude", "lastcall.json")
    interactive = sys.stdin.isatty()

    existing = {}
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as handle:
                existing = json.load(handle) or {}
        except (OSError, ValueError):
            existing = {}

    # Detect first, so the recommendations are about THIS machine.
    has_git_repo = os.path.isdir(os.path.join(root, ".git"))
    tools = {name: bool(shutil.which(name)) for name in ("tmux", "git", "claude")}
    relay_possible = has_git_repo and all(tools.values())

    def ask(question, options, default, why=None):
        """options: list of (key, label). default is the recommendation."""
        if not interactive:
            return default
        print("\n" + question)
        for key, label in options:
            mark = "  <- recommended" if key == default else ""
            print("  %s) %s%s" % (key, label, mark))
        if why:
            print("     %s" % why)
        while True:
            try:
                answer = input("  [%s] " % default).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return default
            if not answer:
                return default
            if answer in dict(options):
                return answer

    def ask_text(question, hint, default=""):
        if not interactive:
            return default
        print("\n" + question)
        print("  %s" % hint)
        try:
            return input("  > ").strip() or default
        except (EOFError, KeyboardInterrupt):
            print()
            return default

    print("Last Call setup — %s" % root)
    print("  git repository : %s" % ("yes" if has_git_repo else "no"))
    print("  tmux / git / claude on PATH : %s"
          % ", ".join("%s %s" % (n, "ok" if ok else "MISSING")
                      for n, ok in tools.items()))

    window = ask(
        "1/4  How big is this project's context window?",
        [("1", "200,000 tokens — standard"),
         ("2", "1,000,000 tokens — extended"),
         ("3", "work it out automatically (needs the bundled status line)")],
        "1",
        why="Getting this wrong is the one thing that makes Last Call useless, "
            "so it refuses to guess.")
    existing["context_window_tokens"] = {
        "1": 200_000, "2": 1_000_000, "3": None}[window]

    relay_default = "y" if relay_possible else "n"
    relay_why = ("tmux, git and the claude CLI are all present."
                 if relay_possible else
                 "Not available here: " + ", ".join(
                     [n for n, ok in tools.items() if not ok]
                     + ([] if has_git_repo else ["this is not a git repository"])))
    relay = ask(
        "2/4  Hand over to a fresh session automatically when context runs low?",
        [("y", "yes — write a handoff, then spawn a successor in tmux"),
         ("n", "no  — just warn me; the session ends there")],
        relay_default,
        why=relay_why)
    # A recommendation is for a human to accept. With no tty there is nobody to
    # accept it, and enabling a relay that will later launch sessions is not
    # something a piped or scripted run gets to decide on your behalf.
    if not interactive:
        relay = "n"

    notes = []
    handoff_dir = os.path.join(root, "docs", "handoff")

    if relay == "y":
        existing["template"] = RELAY_TEMPLATE
        verify = ask_text(
            "3/4  What command proves this project's environment is actually up?",
            "The successor runs this FIRST and must not start work until it "
            "passes.\n  Examples: 'npm test', 'make dev && curl -sf "
            "localhost:3000/health'.\n  Leave blank to fill in later.")
        block = ("```\n%s\n```" % verify) if verify else (
            "```\nTODO: the command(s) that prove this environment works.\n```")
        try:
            os.makedirs(handoff_dir, exist_ok=True)
            skeleton = os.path.join(handoff_dir, "TEMPLATE.md")
            if os.path.exists(skeleton):
                notes.append("kept the existing %s" % skeleton)
            else:
                with open(skeleton, "w", encoding="utf-8") as handle:
                    handle.write(HANDOFF_SKELETON.format(
                        date="YYYY-MM-DD", verify_block=block))
                notes.append("wrote %s — the shape each handoff should take"
                             % skeleton)
        except OSError as error:
            notes.append("could NOT create %s (%s)" % (handoff_dir, error))
        for name, ok in tools.items():
            if not ok:
                notes.append("MISSING: %s is not on PATH — handover will not work"
                             % name)
        if not has_git_repo:
            notes.append("MISSING: %s is not a git repository — the relay "
                         "refuses to spawn without one" % root)

        gates = ask_text(
            "4/4  What must PASS before this project hands over?",
            "Tests, linters, a review gate — comma separated. The wrap-up shows\n"
            "  these to the assistant so it cannot hand over unverified work.\n"
            "  Examples: 'npm test, npm run lint'. Leave blank to fill in later.")
        if gates:
            existing["gates"] = [g.strip() for g in gates.split(",") if g.strip()]

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isfile(target):
            shutil.copy2(target, target + ".bak")
            notes.append("backed up the previous config to %s.bak" % target)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        print("\ncould not write %s: %s" % (target, error))
        return 1

    print("\nwrote %s" % target)
    for note in notes:
        print("  %s" % note)

    fresh = load_config({"cwd": root})
    ready, checks = handover_status(fresh)
    print("\nautomatic handover: %s" % ("READY" if ready else "NOT SET UP"))
    for label, ok in checks.items():
        print("  %s %s" % ("ok  " if ok else "MISS", label))
    if relay == "y":
        print("\nNext: fill in Step 0 of %s/TEMPLATE.md with real commands and"
              % handoff_dir)
        print("their EXPECTED results. A handoff whose Step 0 cannot fail is a")
        print("handoff that proves nothing.")
    if not ready and relay == "y":
        print("\nFix the MISS lines above, then re-run: lastcall.py doctor")
    return 0


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

    print("lastcall %s" % __version__)
    print("  project dir   : %s" % config["_project_dir"])
    print("  config file   : %s" % (config["_config_path"] or "(none — using defaults)"))
    print("  state dir     : %s" % state_dir(config))
    for problem in config.get("_problems") or []:
        print("  PROBLEM       : %s" % problem)

    zones = resolve_zones(config)
    if zones:
        print("  zones         : %s" % ("  ".join(
            "%s@%g%%%s%s" % (
                zone["name"], zone["at"],
                "[block]" if zone["block"] else "",
                "[own text]" if (zone["template"] or zone["message"]) else "",
            ) for zone in zones)))
    else:
        print("  zones         : NONE CONFIGURED — nothing can ever fire")
    print("  mode          : %s" % config["mode"])
    print("  include output: %s" % bool(config["include_output_tokens"]))
    print("  template      : %s" % (config["template"] or "(built-in default)"))
    print("  disabled      : %s" % bool(config["disabled"]))

    ready, checks = handover_status(config)
    print("\n  automatic handover: %s" % ("READY" if ready else "NOT SET UP"))
    for label, ok in checks.items():
        print("    %s %s" % ("ok  " if ok else "MISS", label))
    if not ready:
        print("    -> Last Call will warn, but nothing will carry the work")
        print("       forward; the session just ends. Run: lastcall.py setup")

    if not transcript:
        print("\nPass a transcript path to measure a real session, e.g.")
        print("  lastcall.py doctor ~/.claude/projects/<project>/<session>.jsonl")
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
        print("       1. echo '{\"context_window_tokens\": 200000}' > .claude/lastcall.json")
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
    # setup and doctor print em dashes and arrows. A Windows console running
    # cp437 cannot encode those, and print() would raise UnicodeEncodeError —
    # turning "show me what is configured" into a crash. Degrade the character
    # instead of the command. The hook path is unaffected: json.dumps escapes
    # non-ASCII, so the payload Claude Code receives is pure ASCII regardless.
    if argv and argv[0] in ("doctor", "--doctor", "setup", "--setup"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    if argv and argv[0] in ("doctor", "--doctor"):
        return doctor(argv[1:])
    if argv and argv[0] in ("setup", "--setup"):
        return setup(argv[1:])
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
