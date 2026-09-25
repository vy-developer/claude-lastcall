"""The agent adapter protocol.

Everything Last Call needs from a coding agent goes through one of these:
which agent fired the hook, how full its context is, and what JSON it will
accept back. Keeping those three answers per agent is what lets one hook
script serve Claude Code and Codex without either one's quirks leaking into
the other.
"""

import os
import re

# How strongly a hook invocation points at one agent. The registry picks the
# highest; ties go to registration order.
EVIDENCE_NONE = 0
EVIDENCE_ENV = 1        # an environment variable the agent sets
EVIDENCE_PAYLOAD = 2    # a stdin field only that agent sends
EVIDENCE_PATH = 3       # the transcript lives where only that agent writes

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(_UUID)


class Usage(object):
    """One measurement of how full a session's context is.

    tokens         context in use, as the agent itself counts it.
    window         usable window size, or None when genuinely unknown. None
                   means "stay silent", never "plenty of room".
    window_source  where window came from: "transcript" (the agent wrote it),
                   "models-cache" (the agent's own model catalogue),
                   "model-name" (an explicit marker such as "[1m]"),
                   "evidence" (tokens in use prove it), or "unknown".
    model          model identifier as recorded, if any.
    compacted      the context was compacted since the previous usage record,
                   so a drop in tokens is compaction, not an error.
    session_id     the session this belongs to.
    agent          "claude" or "codex".
    turn_id        the agent's turn identifier, where it has one (Codex).
    measured_at    ISO-8601 timestamp of the record, as the agent wrote it.
    stale          True when a compaction is newer than the newest usage
                   record, so tokens still describe the pre-compaction
                   context. Callers should not warn on a stale reading.
    record_id      identifier of the measured API response (Claude
                   message.id, Codex response_id), for telling a new reading
                   from a repeat of the last one.
    fresh          whether the reading is known to include the response the
                   hook fired for: True / False when read_usage was given a
                   way to tell (see ClaudeAgent.read_usage), else None.
    compaction_id  identity of the compaction behind ``compacted`` (Claude's
                   compact_boundary uuid or timestamp, Codex's compacted
                   line timestamp): the same compaction reads the same
                   whether it is newer than every reading or sits between the
                   newest two, so it re-arms the zones exactly once.
    """

    # A plain class rather than a dataclass: hooks run on every tool call, and
    # importing dataclasses (and with it inspect and typing) costs more than
    # the whole measurement.
    __slots__ = ("tokens", "window", "window_source", "model", "compacted",
                 "session_id", "agent", "turn_id", "measured_at", "stale",
                 "record_id", "fresh", "compaction_id")

    def __init__(self, tokens, window, window_source, model, compacted,
                 session_id, agent, turn_id=None, measured_at=None, stale=False,
                 record_id=None, fresh=None, compaction_id=None):
        self.tokens = tokens
        self.window = window
        self.window_source = window_source
        self.model = model
        self.compacted = compacted
        self.session_id = session_id
        self.agent = agent
        self.turn_id = turn_id
        self.measured_at = measured_at
        self.stale = stale
        self.record_id = record_id
        self.fresh = fresh
        self.compaction_id = compaction_id

    def __eq__(self, other):
        if other.__class__ is not self.__class__:
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    __hash__ = None

    def __repr__(self):
        return "Usage(%s)" % ", ".join("%s=%r" % (name, getattr(self, name))
                                       for name in self.__slots__)

    @property
    def percent(self):
        """Percentage of the window in use, or None when the window is
        unknown."""
        if not self.window:
            return None
        return self.tokens * 100.0 / self.window

    @property
    def remaining(self):
        if not self.window:
            return None
        return max(0, self.window - self.tokens)

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


class Agent:
    """Base class for an agent adapter. Subclasses override every method."""

    #: Short identifier: "claude" or "codex".
    name = "agent"

    #: Hook events whose output may carry hookSpecificOutput.additionalContext.
    context_events = frozenset()

    #: Hook events that may return decision:"block".
    block_events = frozenset(("Stop",))

    def evidence(self, payload, env):
        """How strongly this invocation points at this agent: one of the
        EVIDENCE_* levels."""
        return EVIDENCE_NONE

    def detect(self, payload, env=None):
        """True when this agent plausibly fired the hook."""
        return self.evidence(payload or {}, os.environ if env is None else env) > EVIDENCE_NONE

    def read_usage(self, transcript_path, session_id=None, **hints):
        """The newest usage record as a Usage, or None when there is none to
        read. Must not raise for a missing or malformed file."""
        raise NotImplementedError

    def format_output(self, event, message=None, block_reason=None,
                      stop_hook_active=False):
        """The JSON object this agent accepts on stdout for ``event``, or None
        when there is nothing to say (print nothing at all in that case).

        block_reason is honoured on Stop only. stop_hook_active is the Stop
        payload's flag: when it is set, nothing that makes the model continue
        is returned, because both agents would otherwise loop (Claude until
        its cap of 9, Codex without any cap)."""
        raise NotImplementedError

    def known_window(self, model, env=None):
        """(window, source) for ``model`` from a source that is not a guess,
        or (None, "unknown")."""
        return None, "unknown"

    def __repr__(self):
        return "<%s agent>" % self.name


# --------------------------------------------------------------------------
# Helpers shared by adapters
# --------------------------------------------------------------------------

def expand_home(env, var, default):
    """A directory from ``env[var]``, else ``default``, with ~ expanded."""
    value = env.get(var) if env is not None and hasattr(env, "get") else None
    return os.path.expanduser(value or default)


def path_is_under(path, root):
    """True when ``path`` is inside ``root``. Both are resolved, so symlinked
    homes and relative paths compare correctly."""
    if not path or not root:
        return False
    try:
        path = os.path.realpath(os.path.expanduser(path))
        root = os.path.realpath(os.path.expanduser(root))
    except (OSError, ValueError):
        return False
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def as_int(value):
    """int(value) for real numbers, else None. bool is not a number here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    return None
