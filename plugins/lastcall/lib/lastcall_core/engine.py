"""The hook engine: one entry point for every event, for every agent.

DESIGN — silent while green. It must not spend context warning about context,
so below every zone it prints nothing and nothing reaches the model. It speaks
when the zone CHANGES, once, on whichever hook sees the change first.

DESIGN — output goes through the agent adapter. Claude Code and Codex accept
different JSON from the same hook (Codex rejects the whole payload over one
unexpected key), so nothing here builds hook output by hand.

Delivery (what reaches the model, per event):

  PostToolUse, UserPromptSubmit   additionalContext, both agents. Arrives
                                  mid-turn and does not force a continuation.
  Stop (warning)                  Claude: additionalContext, which makes the
                                  model continue once. Codex: decision "block"
                                  with the message as the reason, the only Stop
                                  output Codex shows the model.
  Stop (zone with "block" in      both: decision "block" once per zone per
   block_once mode)               compaction epoch, with the full wrap-up.
  Stop with stop_hook_active      nothing, ever: the reading is recorded and
                                  a pending warning waits for the next event.
  SessionStart source=compact     the post-compaction note (additionalContext).
  SessionStart, unconfigured      the onboarding prompt, once per project.

DESIGN — fail passive. Unreadable transcript, stale reading, anything at all
unexpected: stay quiet rather than guess. `lastcall.py doctor` shows what was
resolved.
"""

import json
import os
import sys
import time

from .agents import detect_agent
from .config import home_dir, load_config
from .render import (block_reason, compaction_message, onboarding_message,
                     render)
from .state import (SessionState, mark_onboarded, prune_state, session_lock,
                    was_onboarded, write_debug)
from .windows import learn, load_learned
from .zones import effective_window as _effective_window
from .zones import resolve_window, resolve_zones, zone_for, zone_threshold

# A drop this steep can only be compaction — normal turns add tokens, they do
# not remove two fifths of them.
COMPACTION_DROP_RATIO = 0.6

# Claude Code writes the transcript entry for the response a Stop hook fired
# for about 0.3 s after the hook starts. Wait up to this long for it.
CLAUDE_STOP_FRESH_TIMEOUT = 0.8

# A re-arm this recent is the same compaction seen by a second hook (PostCompact
# and then SessionStart source=compact), not a new one.
REARM_DEDUP_SECONDS = 120

MEASURE_EVENTS = frozenset(("Stop", "PostToolUse", "UserPromptSubmit"))


class Reading(object):
    """A measurement with the window it is judged against."""

    __slots__ = ("usage", "tokens", "window", "source", "assumed")

    def __init__(self, usage, window, source, assumed=False):
        self.usage = usage
        self.tokens = usage.tokens if usage is not None else None
        self.window = window
        self.source = source
        self.assumed = assumed


def transcript_signature(path):
    """[size, mtime_ns] of the transcript, or None when it cannot be read."""
    try:
        stat = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return [stat.st_size, stat.st_mtime_ns]


def read_usage(agent, config, payload, state=None, fresh=False):
    """The agent's newest usage record for this session, or None."""
    transcript = payload.get("transcript_path")
    session_id = payload.get("session_id")
    if agent.name == "claude":
        kwargs = {"include_output": bool(config.get("include_output_tokens"))}
        if fresh:
            last = payload.get("last_assistant_message")
            kwargs.update(
                fresh_timeout=CLAUDE_STOP_FRESH_TIMEOUT,
                last_assistant_message=last if isinstance(last, str) and last.strip() else None,
                previous_record_id=(state or {}).get("record_id"))
        return agent.read_usage(transcript, session_id, **kwargs)
    model = payload.get("model")
    return agent.read_usage(transcript, session_id,
                            model=model if isinstance(model, str) else None)


def effective_window(agent, config, state, usage, env=None, learned=None):
    """(window, source, assumed) for judging ``usage``.

    Exact sources and proof first, then the `windows` map and the windows
    learned from earlier sessions, then a "[1m]" model the user configured,
    then the fallback window, flagged as assumed (zones.effective_window has
    the whole order).
    """
    return _effective_window(config, state, usage, agent.name, env, learned)


def measure(config, payload, state=None, agent=None, env=None):
    """(tokens, window, source, model), strict: no fallback window.

    tokens is None only when the transcript cannot be read at all. window is
    None when the size is genuinely unknown.
    """
    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        # Deliberately no path derivation from cwd: the project slug replaces
        # every non-alphanumeric character and cwd may be a subdirectory, so a
        # derived path is wrong in two independent ways.
        return None, None, "no-transcript", None
    agent = agent or detect_agent(payload, env)
    try:
        usage = read_usage(agent, config, payload)
    except OSError:
        return None, None, "unreadable-transcript", None
    if usage is None:
        return None, None, "no-usage-record", None
    evidence = dict(state or {})
    evidence["max_observed"] = max(int(evidence.get("max_observed") or 0), usage.tokens)
    window, source = resolve_window(config, evidence, usage, agent.name,
                                    learned=lambda: load_learned(config))
    return usage.tokens, window, source, usage.model


def _emit(out, output):
    out.write(json.dumps(output))
    out.flush()


def _rearm(state, now=None):
    """Forget the announced zone so the next climb warns again. The window's
    size (max_observed, window_from_statusline) deliberately survives:
    compaction changes how full the window is, never how big it is."""
    band = state.get("band")
    if band and band != "green":
        state["band_before_compaction"] = band
    state["band"] = "green"
    state["peak"] = 0
    state["epoch"] = state.number("epoch") + 1
    state["rearmed_at"] = int(now if now is not None else time.time())


def _blocked(state, zone):
    blocked = state.get("blocked")
    if not isinstance(blocked, dict) or blocked.get("epoch") != state.number("epoch"):
        return False
    return zone["name"] in (blocked.get("zones") or [])


def _mark_blocked(state, zone):
    blocked = state.get("blocked")
    epoch = state.number("epoch")
    zones = []
    if isinstance(blocked, dict) and blocked.get("epoch") == epoch:
        zones = list(blocked.get("zones") or [])
    zones.append(zone["name"])
    state["blocked"] = {"epoch": epoch, "zones": zones}


def on_measure(agent, config, payload, event, env=None, out=None):
    """PostToolUse, UserPromptSubmit and Stop: measure, and warn on a change."""
    out = out or sys.stdout
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not transcript or not isinstance(transcript, str):
        return 0
    stop = event == "Stop"
    looping = stop and bool(payload.get("stop_hook_active"))
    if not stop and payload.get("agent_id"):
        # Fired for a subagent's tool call: additionalContext would land in
        # the subagent's conversation, and the zone would be marked announced
        # without the session that needs the warning ever seeing it.
        return 0

    signature = transcript_signature(transcript)
    if signature is None:
        return 0  # cannot measure -> stay silent rather than guess
    state = SessionState(config, session_id, agent.name)
    # Fast path. PostToolUse fires on every tool call; an unchanged transcript
    # holds the same reading as last time, so there is nothing to decide.
    if not stop and state.get("sig") == signature:
        return 0

    try:
        usage = read_usage(agent, config, payload, state, fresh=stop and not looping)
    except OSError:
        usage = None
    # Decide, emit and save under the session's lock: parallel tool calls fire
    # PostToolUse hooks together, and without it every one of them read the
    # same "not announced yet" state and emitted the same warning.
    with session_lock(config, session_id, agent.name) as lock:
        if lock.acquired is False:
            return 0  # another hook is judging this session; it speaks, not us
        state = SessionState(config, session_id, agent.name)
        if not stop and state.get("sig") == signature:
            return 0  # a concurrent hook already judged this very reading
        return _judge(agent, config, payload, event, env, out, state, usage,
                      signature, stop, looping, transcript)


def _judge(agent, config, payload, event, env, out, state, usage, signature,
           stop, looping, transcript):
    """on_measure's decision, with the session's state freshly read under its
    lock. Always returns 0."""
    state["sig"] = signature
    if usage is None:
        state.save()
        return 0

    tokens = usage.tokens
    peak = state.number("peak")
    if usage.compacted:
        key = "%s|%s" % (usage.record_id, usage.measured_at)
        if state.get("compaction_key") != key:
            state["compaction_key"] = key
            _rearm(state)
            peak = 0
    if usage.stale:
        # A compaction is newer than every usage record: the number describes
        # a context that no longer exists.
        state.save()
        return 0
    if peak and tokens < peak * COMPACTION_DROP_RATIO:
        # Compaction detected from the drop itself, so the zones re-arm even
        # where no compaction hook is registered.
        _rearm(state)
        peak = 0
    state["max_observed"] = max(state.number("max_observed"), tokens)
    state["peak"] = max(peak, tokens)
    state["tokens"] = tokens
    if usage.record_id:
        state["record_id"] = usage.record_id

    window, source, assumed = effective_window(agent, config, state, usage, env)
    state["window"] = window
    state["window_source"] = source
    # What this session has proved about its model (tokens beyond 200K, the
    # status line's figure, Codex's rollout) is remembered for the next
    # session on the same model. Once per session per proof.
    try:
        learn(config, agent.name, state, usage)
    except Exception:  # noqa: BLE001 - learning is a bonus, never a failure
        pass
    zones = resolve_zones(config)
    if window is None and not any(z["at_tokens"] is not None for z in zones):
        # Size unknown and every zone is a percentage of it. The reading is
        # still recorded: it is the evidence that may resolve the window later.
        state.save()
        return 0
    floor = config.get("min_window_tokens")
    if floor and window and window < int(floor):
        state.save()
        return 0

    zone = zone_for(tokens, window, zones)
    if zone is None:
        # Recorded even below every zone — this is what re-arms them.
        state["band"] = "green"
        state.save()
        return 0

    announced = state.get("band") or "green"
    previous = next((z for z in zones if z["name"] == announced), None)
    if previous is not None:
        before = zone_threshold(previous, window)
        if before is not None and zone_threshold(zone, window) < before:
            # Moved down a zone (a smaller reading, or a window that turned
            # out bigger): re-arm quietly, never warn on the way down.
            state["band"] = zone["name"]
            state.save()
            return 0

    changed = zone["name"] != announced
    # Only a percentage zone depends on the window; one written in tokens
    # means the same whatever the window turns out to be.
    assumed = assumed and zone["at_tokens"] is None
    blocking = (zone["block"] and config.get("mode") == "block_once"
                and not assumed)
    need_block = stop and blocking and not _blocked(state, zone)
    if not changed and not need_block:
        state.save()
        return 0
    if looping:
        # The model is already continuing because a Stop hook asked it to.
        # Saying anything now risks a loop (Codex has no cap); the zone stays
        # unannounced and the next event delivers it.
        state.save()
        return 0

    message = render(config, zone, tokens, window, transcript=transcript,
                     assumed=assumed, agent=agent.name)
    if stop and need_block:
        output = agent.format_output("Stop", message, block_reason(zone))
    elif stop and "Stop" not in agent.context_events:
        # Codex: a block with the warning as its reason is the only Stop
        # output the model ever sees.
        output = agent.format_output("Stop", None, block_reason=message)
    else:
        output = agent.format_output(event, message)
    if not output:
        state.save()
        return 0

    _emit(out, output)
    # Bookkeeping only after the payload is out. Recording the zone before
    # delivering it means a failed write marks it "already announced" and the
    # guard goes silent at exactly the moment it matters.
    state["band"] = zone["name"]
    state["announced_on"] = event
    if stop and need_block:
        _mark_blocked(state, zone)
    state.save()
    return 0


RELAY_ENV = ("LASTCALL_RELAY_LEDGER", "LASTCALL_RELAY_CHAIN", "LASTCALL_RELAY_GENERATION")


def relay_successor_note(agent, payload, env=None):
    """A session the relay spawned (the LASTCALL_RELAY_* variables are set)
    checks in on the relay's ledger from here, whatever started it, and gets a
    short note saying which handoff to read. None otherwise. Fail-passive."""
    env = os.environ if env is None else env
    if not all(env.get(name) for name in RELAY_ENV):
        return None
    try:
        from . import relay
        return relay.successor_session_start(payload, env, agent.name)
    except Exception:  # noqa: BLE001 - never break a session over the relay
        return None


def on_session_start(agent, config, payload, env=None, out=None):
    """Reset for a new conversation, keep state for a resumed one, re-arm and
    remind after a compaction, and offer onboarding once per project."""
    out = out or sys.stdout
    source = payload.get("source")
    state = SessionState(config, payload.get("session_id"), agent.name)
    now = time.time()
    messages = []
    onboard_project = None
    relay_note = relay_successor_note(agent, payload, env)
    if relay_note:
        messages.append(relay_note)

    if source == "resume":
        pass  # same conversation: a zone already announced stays announced
    elif source == "compact":
        if now - state.number("rearmed_at") > REARM_DEDUP_SECONDS:
            _rearm(state, now)
        recent = now - state.number("rearmed_at") <= REARM_DEDUP_SECONDS
        note = compaction_message(
            config, state.get("band_before_compaction") if recent else None)
        if note:
            messages.append(note)
        if "band_before_compaction" in state:
            del state["band_before_compaction"]
    else:
        # startup / clear / an agent that does not say: a fresh context.
        state["band"] = "green"
        state["peak"] = 0
        state["epoch"] = state.number("epoch") + 1
        project = config.get("_project_dir")
        # Installed but unconfigured still works (the defaults run), so this
        # is an offer, made once per project rather than every session.
        if (not config.get("_configured") and project
                and os.path.realpath(project) != home_dir()
                and not was_onboarded(config, project)):
            messages.append(onboarding_message())
            onboard_project = project

    state.save()
    prune_state(config)
    if not messages:
        return 0
    output = agent.format_output("SessionStart", "\n\n".join(messages))
    if output:
        _emit(out, output)
        if onboard_project:
            mark_onboarded(config, onboard_project, agent.name)
    return 0


def on_compacted(agent, config, payload, env=None, out=None):
    """PostCompact: re-arm. Nothing is printed — Codex accepts no context on
    this event, and SessionStart(source=compact) carries the note on both."""
    state = SessionState(config, payload.get("session_id"), agent.name)
    now = time.time()
    if now - state.number("rearmed_at") > REARM_DEDUP_SECONDS:
        _rearm(state, now)
        state.save()
    return 0


def handle_event(event, payload, env=None, out=None, config=None, agent=None):
    """Dispatch one hook invocation. Returns the exit code (always 0)."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    agent = agent or detect_agent(payload, env)
    config = config or load_config(payload, env)
    if config.get("disabled"):
        return 0
    write_debug(config, payload)
    if event == "SessionStart":
        return on_session_start(agent, config, payload, env, out)
    if event == "PostCompact":
        return on_compacted(agent, config, payload, env, out)
    if event in MEASURE_EVENTS:
        return on_measure(agent, config, payload, event, env, out)
    return 0  # PreCompact, SubagentStop, anything else: nothing to do


def run_hook(argv, stdin=None, stdout=None, env=None):
    """The hook entry point: JSON payload on stdin, JSON (or nothing) on
    stdout, exit code 0 whatever happens."""
    stdin = stdin or sys.stdin
    try:
        raw = stdin.read()
    except (OSError, ValueError):
        return 0
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return 0  # never break a session over a parse failure
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name") or (argv[0] if argv else "Stop")
    return handle_event(event, payload, env, stdout)


# --------------------------------------------------------------------------
# Compatibility with the pre-2.0 script API
# --------------------------------------------------------------------------

def handle_stop(config, payload, agent=None, env=None, out=None):
    event = payload.get("hook_event_name") or "Stop"
    agent = agent or detect_agent(payload, env)
    return on_measure(agent, config, payload, event, env, out)


def handle_reset(config, payload, agent=None, env=None, out=None):
    """SessionStart / PostCompact."""
    agent = agent or detect_agent(payload, env)
    if (payload.get("hook_event_name") or "SessionStart") == "SessionStart":
        return on_session_start(agent, config, payload, env, out)
    return on_compacted(agent, config, payload, env, out)
