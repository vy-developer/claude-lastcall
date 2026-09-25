"""Claude Code adapter.

Transcripts live at ~/.claude/projects/<slug>/<session-id>.jsonl (or under
$CLAUDE_CONFIG_DIR). Context in use is the newest main-session assistant
entry's usage: input + cache_read + cache_creation. Compaction writes a
{"type":"system","subtype":"compact_boundary"} entry whose
compactMetadata.postTokens is the size of the context that survived.

Claude Code flushes the transcript asynchronously: when a hook starts, the
entry for the response that triggered it is usually not written yet and
appears about 0.3 s later. read_usage can poll briefly for it (fresh_timeout).
"""

import json
import os
import re
import time

from ..tail import TAIL_LIMIT_BYTES, iter_lines_reverse
from .base import (EVIDENCE_ENV, EVIDENCE_NONE, EVIDENCE_PATH, EVIDENCE_PAYLOAD,
                   UUID_RE, Agent, Usage, as_int, expand_home, path_is_under)

# The window CANNOT be inferred from the model identifier: a session running
# the 1M-context model records itself under the same name as the 200K one.
# The one inference made here is a proof, not a guess: tokens already in the
# window are a hard lower bound on its size.
STANDARD_WINDOW = 200_000
EXTENDED_WINDOW = 1_000_000
KNOWN_WINDOWS = (STANDARD_WINDOW, EXTENDED_WINDOW)

# Entries Claude Code writes itself (API errors, interrupted turns) carry this
# model and an all-zero usage block. Read as a measurement they claim the
# context is empty, which re-arms every zone for a session that is nearly full.
SYNTHETIC_MODEL = "<synthetic>"

_ENV_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR",
                "CLAUDE_PID")
# Stdin keys Claude Code sends and Codex does not (Claude 2.1.281, Codex
# 0.153.4). SessionStart carries none of them; path and env decide that one.
_PAYLOAD_MARKERS = ("prompt_id", "session_crons", "background_tasks", "duration_ms")

# Polling for the entry a hook fired for: cheap (a few KB from the end of the
# file per poll) and bounded by the caller's timeout.
DEFAULT_POLL_INTERVAL = 0.05
_TRANSCRIPT_NAME = re.compile(r"^" + UUID_RE.pattern + r"\.jsonl$")
_ONE_M_MARKER = re.compile(r"\[1m\]", re.I)


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
    """The only window claim made without being told.

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


def _measurable(entry):
    """(usage, model) when ``entry`` is a real main-session measurement,
    else (None, None)."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return None, None
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None, None
    model = message.get("model")
    if model == SYNTHETIC_MODEL:
        return None, None
    try:
        if count_tokens(usage) <= 0:
            return None, None
    except (TypeError, ValueError):
        return None, None
    return usage, model


def _is_boundary(entry):
    return entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"


def _message_id(entry):
    message = entry.get("message")
    if isinstance(message, dict) and isinstance(message.get("id"), str):
        return message["id"]
    return None


def _text_of(entry):
    """The text blocks of an assistant entry, joined."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block.get("text") for block in content
                     if isinstance(block, dict) and block.get("type") == "text"
                     and isinstance(block.get("text"), str))


def _normalise(text):
    return " ".join((text or "").split())


def _text_matches(transcript_text, hook_text):
    """Whether the transcript's newest message is the one the hook reports.
    Whitespace is normalised; either may be a suffix of the other, because a
    reply split over several entries is joined here but may be reported whole
    or only in its last block."""
    a, b = _normalise(transcript_text), _normalise(hook_text)
    if not a or not b:
        return False
    return a == b or a.endswith(b) or b.endswith(a)


def _scan(transcript, session_id=None, limit=TAIL_LIMIT_BYTES):
    """Walk the transcript backwards.

    Returns a dict with:
      latest     (entry, usage, model) of the newest usable assistant entry
      text       text of every entry in that same message (one API response
                 is written as several entries: thinking, text, tool_use)
      boundary   a compact boundary newer than latest, if any
      compacted  whether a compaction separates latest from the previous
                 API response
      compaction the compact boundary behind ``compacted`` (``boundary``,
                 or the one between latest and the previous response)
    """
    latest = None
    latest_id = None
    texts = []
    boundary = None
    compacted = False
    between = None
    for raw in iter_lines_reverse(transcript, limit=limit):
        # Cheap pre-filter: almost every line is a tool result or a user turn,
        # and parsing them all is where the time would go.
        if b'"assistant"' not in raw and b"compact_boundary" not in raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        # Subagents and sidechains have their own usage blocks and their own
        # windows; counting one of those as the main session's context reports
        # a number belonging to a different conversation entirely.
        if entry.get("isSidechain"):
            continue
        entry_session = entry.get("sessionId")
        if session_id and entry_session and entry_session != session_id:
            continue
        if _is_boundary(entry):
            if latest is None:
                if boundary is None:
                    boundary = entry
                continue
            compacted = True
            between = entry
            break
        if entry.get("type") != "assistant":
            continue
        if latest is not None and latest_id is not None and _message_id(entry) == latest_id:
            # Another block of the same response: same usage, more text.
            texts.append(_text_of(entry))
            continue
        usage, model = _measurable(entry)
        if usage is None:
            continue
        if latest is None:
            latest = (entry, usage, model)
            latest_id = _message_id(entry)
            texts.append(_text_of(entry))
            if boundary is not None:
                compacted = True
                break
            continue
        break  # the previous response, with no compaction in between
    text = "\n".join(t for t in reversed(texts) if t)
    return {"latest": latest, "text": text, "boundary": boundary,
            "compacted": compacted, "compaction": boundary or between}


def compaction_identity(boundary):
    """What names one compact boundary: its uuid, else its timestamp."""
    if not isinstance(boundary, dict):
        return None
    for key in ("uuid", "timestamp"):
        if isinstance(boundary.get(key), str) and boundary[key]:
            return "%s:%s" % (key, boundary[key])
    return None


def _is_fresh(found, last_assistant_message, previous_record_id):
    """True / False when there is a way to tell, None when there is not."""
    if last_assistant_message is None and previous_record_id is None:
        return None
    if found["boundary"] is not None:
        return True  # a compaction is newer than anything the hook could await
    latest = found["latest"]
    if latest is None:
        return False
    if previous_record_id is not None and _message_id(latest[0]) == previous_record_id:
        return False
    if last_assistant_message is not None:
        return _text_matches(found["text"], last_assistant_message)
    return True


def latest_usage(transcript, session_id=None):
    """The newest main-session usage record, as (usage, model), skipping
    sidechains, other sessions and synthetic/zero-usage entries. Kept with the
    same shape as the function it was ported from."""
    latest = _scan(transcript, session_id)["latest"]
    if latest is None:
        return None, None
    return latest[1], latest[2]


class ClaudeAgent(Agent):
    name = "claude"

    # additionalContext is accepted on all of these (verified live on
    # SessionStart, UserPromptSubmit, PostToolUse and Stop). On Stop it makes
    # the model continue exactly as a block does, which is why format_output
    # drops it when stop_hook_active is set.
    context_events = frozenset((
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "Stop", "SubagentStop", "PostCompact",
    ))

    def projects_dir(self, env=None):
        env = os.environ if env is None else env
        return os.path.join(expand_home(env, "CLAUDE_CONFIG_DIR", "~/.claude"), "projects")

    def evidence(self, payload, env):
        payload = payload if isinstance(payload, dict) else {}
        transcript = payload.get("transcript_path")
        if transcript and isinstance(transcript, str):
            if path_is_under(transcript, self.projects_dir(env)):
                return EVIDENCE_PATH
            parent = os.path.dirname(os.path.dirname(transcript))
            if (os.path.basename(parent) == "projects"
                    and _TRANSCRIPT_NAME.match(os.path.basename(transcript))):
                return EVIDENCE_PATH
        if any(key in payload for key in _PAYLOAD_MARKERS):
            return EVIDENCE_PAYLOAD
        session_id = payload.get("session_id")
        if session_id and env.get("CLAUDE_CODE_SESSION_ID") == session_id:
            return EVIDENCE_PAYLOAD
        if any(env.get(name) for name in _ENV_MARKERS):
            return EVIDENCE_ENV
        return EVIDENCE_NONE

    def read_usage(self, transcript_path, session_id=None, include_output=False,
                   fresh_timeout=0.0, last_assistant_message=None,
                   previous_record_id=None, poll_interval=DEFAULT_POLL_INTERVAL,
                   limit=TAIL_LIMIT_BYTES, **hints):
        """The newest measurement, or None.

        With fresh_timeout > 0 and a way to recognise the response the hook
        fired for (last_assistant_message from the Stop payload, and/or the
        record_id of the previous reading), polls up to fresh_timeout seconds
        for it to be written, then settles for what is there. Usage.fresh
        says which happened.
        """
        if not transcript_path or not os.path.isfile(transcript_path):
            return None
        deadline = time.monotonic() + max(0.0, float(fresh_timeout or 0))
        while True:
            try:
                found = _scan(transcript_path, session_id, limit=limit)
            except OSError:
                return None
            fresh = _is_fresh(found, last_assistant_message, previous_record_id)
            remaining = deadline - time.monotonic()
            if fresh is None or fresh or remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        return self._usage(found, session_id, include_output, fresh, hints.get("model"))

    def _usage(self, found, session_id, include_output, fresh, model_hint):
        latest, boundary = found["latest"], found["boundary"]
        if latest is None and boundary is None:
            return None
        entry, usage, model = latest if latest else ({}, None, None)
        stale = False
        measured_at = entry.get("timestamp")
        if boundary is not None:
            # The compaction is newer than every measurement. Its own record
            # says how much survived; without that, the newest measurement
            # describes a context that no longer exists.
            post = as_int((boundary.get("compactMetadata") or {}).get("postTokens"))
            if post is not None and post > 0:
                tokens = post
                measured_at = boundary.get("timestamp")
            elif usage is not None:
                tokens = count_tokens(usage, include_output)
                stale = True
            else:
                return None
        else:
            tokens = count_tokens(usage, include_output)

        model = model or model_hint
        window, source = self.known_window(model)
        if window is None:
            window = window_from_evidence(tokens)
            source = "evidence" if window else "unknown"
        return Usage(
            tokens=tokens,
            window=window,
            window_source=source,
            model=model,
            compacted=bool(found["compacted"] or boundary is not None),
            session_id=session_id or entry.get("sessionId")
            or (boundary or {}).get("sessionId"),
            agent=self.name,
            turn_id=None,
            measured_at=measured_at,
            stale=stale,
            record_id=_message_id(entry) if entry else None,
            fresh=fresh,
            compaction_id=compaction_identity(found.get("compaction")),
        )

    def format_output(self, event, message=None, block_reason=None,
                      stop_hook_active=False):
        looping = event in ("Stop", "SubagentStop") and stop_hook_active
        block = bool(block_reason) and event in self.block_events and not looping
        if not message and not block:
            return None
        output = {"suppressOutput": True}
        if message:
            if event in self.context_events and not looping:
                output["hookSpecificOutput"] = {
                    # REQUIRED. Without hookEventName the CLI rejects the whole
                    # payload, the hook still reports success, and nothing
                    # reaches the model.
                    "hookEventName": event,
                    "additionalContext": message,
                }
            else:
                # Shown to the user, not the model, and does not re-invoke it.
                output["systemMessage"] = message
        if block:
            output["decision"] = "block"
            output["reason"] = block_reason
        return output

    def known_window(self, model, env=None):
        # Only an explicit marker counts. The plain identifier is shared by
        # the 200K and 1M variants.
        if isinstance(model, str) and _ONE_M_MARKER.search(model):
            return EXTENDED_WINDOW, "model-name"
        return None, "unknown"

    def settings_window(self, model, env=None):
        """(window, source) from the model the user CONFIGURED, when that
        carries an explicit "[1m]" and names this session's model.

        The transcript records "claude-opus-5" for the 1M variant too, but a
        user who picked "opus[1m]" has it in $ANTHROPIC_MODEL or in
        settings.json's "model" (/model writes it there). That is a strong
        hint rather than a proof — the session may have been started with
        --model — so it ranks below every exact source. Read-only.
        """
        if not isinstance(model, str) or not model:
            return None, "unknown"
        env = os.environ if env is None else env
        candidates = []
        if env.get("ANTHROPIC_MODEL"):
            candidates.append(("$ANTHROPIC_MODEL", env.get("ANTHROPIC_MODEL")))
        settings = os.path.join(expand_home(env, "CLAUDE_CONFIG_DIR", "~/.claude"),
                                "settings.json")
        try:
            with open(settings, encoding="utf-8") as handle:
                configured = json.load(handle).get("model")
            if isinstance(configured, str):
                candidates.append(("settings.json", configured))
        except (OSError, ValueError, AttributeError):
            pass
        lowered = model.lower()
        for origin, configured in candidates:
            if not isinstance(configured, str) or not _ONE_M_MARKER.search(configured):
                continue
            base = _ONE_M_MARKER.sub("", configured).strip().lower()
            if base and (base in lowered or lowered in base):
                return EXTENDED_WINDOW, "configured model %s (%s)" % (configured, origin)
        return None, "unknown"
