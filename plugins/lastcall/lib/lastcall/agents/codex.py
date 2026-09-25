"""OpenAI Codex CLI adapter.

Rollouts live at $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<session>.jsonl
(a resumed session may continue in rollout-<ts>-<session>_<segment>.jsonl).
Each line is {"timestamp", "ordinal", "type", "payload"}.

Context in use is the newest event_msg/token_count's
payload.info.last_token_usage.total_tokens, and payload.info.model_context_window
is the usable window. total_token_usage is cumulative over the whole session
and is NOT the context. A {"type":"compacted"} line marks a compaction; the
token_count after it is small again.

A token_count is written only after a response's tool calls have run, so at
PostToolUse the newest one is a response behind. Each response also gets a
{"type":"token_usage_record"} line straight away, whose payload.usage.total_tokens
equals the later token_count's last_token_usage.total_tokens; whichever of the
two is newer is the reading. It carries no window, so that still comes from the
newest token_count or task_started.
"""

import glob
import json
import os
import re

from ..tail import TAIL_CHUNK_BYTES, iter_lines_reverse
from .base import (EVIDENCE_ENV, EVIDENCE_NONE, EVIDENCE_PATH, EVIDENCE_PAYLOAD,
                   UUID_RE, Agent, Usage, as_int, expand_home, path_is_under)

# A compaction record carries the replacement history and runs to several MB,
# so looking past one for the previous token_count needs more room than the
# Claude reader does. The newest token_count itself is normally a few KB from
# the end; this only bounds the worst case.
CODEX_TAIL_LIMIT_BYTES = 24 * 1024 * 1024

# Codex's own default when a model entry does not state one.
DEFAULT_EFFECTIVE_WINDOW_PERCENT = 95

# How many rollout files of one session to try when the given one has no
# token_count (a fresh resume segment, say).
MAX_SIBLINGS = 6

ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session>%s)(?:_(?P<segment>%s))?\.jsonl$"
    % (UUID_RE.pattern, UUID_RE.pattern))

# Codex 0.153.4 hooks see only CODEX_MANAGED_BY_* and CODEX_MANAGED_PACKAGE_ROOT
# (npm installs). The others are set for the agent's own shell commands, so
# they only mean "somewhere inside Codex".
_ENV_MARKERS = ("CODEX_MANAGED_PACKAGE_ROOT", "CODEX_MANAGED_BY_NPM",
                "CODEX_MANAGED_BY_BUN", "CODEX_MANAGED_BY_PNPM",
                "CODEX_MANAGED_BY_VITE_PLUS", "CODEX_THREAD_ID",
                "CODEX_SESSION_ID", "CODEX_SANDBOX", "CODEX_CI")
# Stdin keys Codex sends and Claude Code does not: model on every event,
# turn_id on everything but SessionStart.
_PAYLOAD_MARKERS = ("turn_id", "model")

# The top-level "type" precedes "payload" in every rollout line, and the
# payload's own "type" precedes any nested object. Reading both from the first
# few hundred bytes means the multi-MB lines (compactions, tool output) are
# never parsed just to learn they are irrelevant.
_TOP_TYPE = re.compile(rb'^\{[^{]*?"type"\s*:\s*"([A-Za-z_]+)"')
_SUB_TYPE = re.compile(rb'"payload"\s*:\s*\{[^{]*?"type"\s*:\s*"([A-Za-z_]+)"')
_SNIFF_BYTES = 512
_WANTED_TOP = frozenset(("event_msg", "turn_context", "compacted", "token_usage_record"))
_WANTED_EVENTS = frozenset(("token_count", "task_started"))


def session_id_from_path(path):
    """The session UUID in a rollout file name, or None."""
    if not path:
        return None
    match = ROLLOUT_RE.match(os.path.basename(path))
    return match.group("session") if match else None


def _sniff(raw):
    head = raw[:_SNIFF_BYTES]
    top = _TOP_TYPE.match(head)
    sub = _SUB_TYPE.search(head)
    return (top.group(1).decode("ascii") if top else None,
            sub.group(1).decode("ascii") if sub else None)


def scan_rollout(path, limit=CODEX_TAIL_LIMIT_BYTES, want_model=True):
    """Walk one rollout backwards.

    Returns a dict:
      tokens, measured_at  from the newest token_usage_record or token_count
                           (tokens None when neither is within ``limit``)
      record_id            response_id of that response, when known
      window               from the newest token_count
      task_window          from the newest task_started
      model, turn_id       from the newest turn_context
      compacted            a compaction since the previous response's reading
      stale                a compaction newer than the newest reading
    """
    found = {"tokens": None, "window": None, "measured_at": None,
             "record_id": None, "task_window": None, "model": None,
             "turn_id": None, "compacted": False, "stale": False}
    previous_seen = False
    for raw in iter_lines_reverse(path, limit=limit, chunk=TAIL_CHUNK_BYTES):
        top, sub = _sniff(raw)
        if top is not None:
            if top not in _WANTED_TOP:
                continue
            if top == "event_msg" and sub is not None and sub not in _WANTED_EVENTS:
                continue
        if top == "compacted":
            entry = {"type": "compacted"}
        else:
            try:
                entry = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
        kind = entry.get("type")
        payload = entry.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        if kind == "compacted":
            if found["tokens"] is None:
                found["stale"] = True
                found["compacted"] = True
            elif not previous_seen:
                found["compacted"] = True
                previous_seen = True  # the compaction settles the question
        elif kind == "turn_context":
            if found["model"] is None and isinstance(payload.get("model"), str):
                found["model"] = payload["model"]
            if found["turn_id"] is None and isinstance(payload.get("turn_id"), str):
                found["turn_id"] = payload["turn_id"]
        elif kind == "event_msg" and payload.get("type") == "task_started":
            if found["task_window"] is None:
                found["task_window"] = as_int(payload.get("model_context_window"))
            if found["turn_id"] is None and isinstance(payload.get("turn_id"), str):
                found["turn_id"] = payload["turn_id"]
        elif kind == "token_usage_record":
            usage = payload.get("usage")
            tokens = as_int(usage.get("total_tokens")) if isinstance(usage, dict) else None
            if not tokens or tokens <= 0:
                continue
            response_id = payload.get("response_id")
            response_id = response_id if isinstance(response_id, str) else None
            if found["tokens"] is None:
                found["tokens"] = tokens
                found["measured_at"] = entry.get("timestamp")
                found["record_id"] = response_id
                if found["turn_id"] is None and isinstance(payload.get("turn_id"), str):
                    found["turn_id"] = payload["turn_id"]
            elif not previous_seen and found["record_id"] is None:
                # The record written just before the newest token_count is the
                # same response: it names it, and is not a previous reading.
                found["record_id"] = response_id
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                continue  # rate-limit-only update, no usage
            last = info.get("last_token_usage")
            tokens = as_int(last.get("total_tokens")) if isinstance(last, dict) else None
            if not tokens or tokens <= 0:
                continue
            if found["window"] is None:
                found["window"] = as_int(info.get("model_context_window"))
            if found["tokens"] is None:
                found["tokens"] = tokens
                found["measured_at"] = entry.get("timestamp")
            else:
                # Only a token_count marks the previous response: records come
                # in pairs with them, and the newest reading's own pair would
                # otherwise hide a compaction between the two responses.
                previous_seen = True

        if (found["tokens"] is not None and previous_seen
                and (found["model"] is not None or not want_model)
                and (found["window"] or found["task_window"])):
            break
    return found


class CodexAgent(Agent):
    name = "codex"

    # Codex validates hook output with deny_unknown_fields: an unexpected key
    # rejects the whole payload. Stop in particular accepts only continue,
    # stopReason, suppressOutput, systemMessage, decision and reason.
    context_events = frozenset((
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "SubagentStart",
    ))

    def home(self, env=None):
        env = os.environ if env is None else env
        return expand_home(env, "CODEX_HOME", "~/.codex")

    def sessions_dir(self, env=None):
        return os.path.join(self.home(env), "sessions")

    def evidence(self, payload, env):
        payload = payload if isinstance(payload, dict) else {}
        transcript = payload.get("transcript_path")
        if transcript and isinstance(transcript, str):
            if (ROLLOUT_RE.match(os.path.basename(transcript))
                    or path_is_under(transcript, self.sessions_dir(env))):
                return EVIDENCE_PATH
        if any(payload.get(key) for key in _PAYLOAD_MARKERS):
            return EVIDENCE_PAYLOAD
        session_id = payload.get("session_id")
        if session_id and session_id in (env.get("CODEX_THREAD_ID"),
                                         env.get("CODEX_SESSION_ID")):
            return EVIDENCE_PAYLOAD
        if any(env.get(name) for name in _ENV_MARKERS):
            return EVIDENCE_ENV
        return EVIDENCE_NONE

    def rollouts_for(self, session_id, near=None, env=None):
        """Rollout files for ``session_id``, newest first: the session's own
        file and any resume segments, wherever in the date tree they landed."""
        if not session_id or not UUID_RE.fullmatch(session_id):
            return []  # never glob on an arbitrary string
        pattern = "rollout-*-%s*.jsonl" % session_id
        roots = []
        if near:
            day = os.path.dirname(os.path.abspath(near))
            roots.append(os.path.join(day, pattern))
            tree = os.path.dirname(os.path.dirname(os.path.dirname(day)))
            if all(part.isdigit() for part in day.split(os.sep)[-3:]):
                roots.append(os.path.join(tree, "*", "*", "*", pattern))
        roots.append(os.path.join(self.sessions_dir(env), "*", "*", "*", pattern))
        paths = set()
        for root in roots:
            paths.update(glob.glob(root))

        def mtime(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0
        return sorted(paths, key=mtime, reverse=True)

    def read_usage(self, transcript_path, session_id=None, model=None, env=None,
                   limit=CODEX_TAIL_LIMIT_BYTES, **hints):
        env = os.environ if env is None else env
        session_id = session_id or session_id_from_path(transcript_path)
        candidates = []
        if transcript_path and os.path.isfile(transcript_path):
            candidates.append(transcript_path)
        for path in self.rollouts_for(session_id, near=transcript_path, env=env):
            if path not in candidates:
                candidates.append(path)
        for path in candidates[:MAX_SIBLINGS + 1]:
            try:
                found = scan_rollout(path, limit=limit, want_model=not model)
            except OSError:
                continue
            if found["tokens"] is None:
                continue
            model_seen = found["model"] or model
            window = found["window"] or found["task_window"]
            source = "transcript" if window else "unknown"
            if not window:
                window, source = self.known_window(model_seen, env)
            return Usage(
                tokens=found["tokens"],
                window=window,
                window_source=source,
                model=model_seen,
                compacted=found["compacted"],
                session_id=session_id or session_id_from_path(path),
                agent=self.name,
                turn_id=found["turn_id"],
                measured_at=found["measured_at"],
                stale=found["stale"],
                record_id=found["record_id"],
            )
        return None

    def format_output(self, event, message=None, block_reason=None,
                      stop_hook_active=False):
        looping = event in ("Stop", "SubagentStop") and stop_hook_active
        block = bool(block_reason) and event in self.block_events and not looping
        if block:
            # A block is the only Stop output the model sees: its reason
            # becomes the next prompt. So the warning goes into the reason,
            # and nothing else rides along — hookSpecificOutput on Stop gets
            # the whole payload rejected (verified live: silently, block and
            # all). Codex does not cap block loops, hence stop_hook_active.
            reason = block_reason if not message else "%s\n\n%s" % (message, block_reason)
            return {"decision": "block", "reason": reason}
        if not message:
            return None
        if event in self.context_events:
            return {"hookSpecificOutput": {"hookEventName": event,
                                           "additionalContext": message}}
        # Stop without a block, PreCompact, PostCompact, ...: no field reaches
        # the model, so show it to the user rather than send nothing.
        return {"systemMessage": message}

    def known_window(self, model, env=None):
        """The usable window from Codex's own model catalogue
        ($CODEX_HOME/models_cache.json): context_window scaled by
        effective_context_window_percent, which is exactly the
        model_context_window Codex writes into rollouts."""
        if not model or not isinstance(model, str):
            return None, "unknown"
        path = os.path.join(self.home(env), "models_cache.json")
        try:
            with open(path, encoding="utf-8") as handle:
                cache = json.load(handle)
        except (OSError, ValueError):
            return None, "unknown"
        models = cache.get("models") if isinstance(cache, dict) else cache
        if not isinstance(models, list):
            return None, "unknown"
        for entry in models:
            if not isinstance(entry, dict) or entry.get("slug") != model:
                continue
            window = as_int(entry.get("context_window"))
            if not window:
                return None, "unknown"
            percent = as_int(entry.get("effective_context_window_percent"))
            if percent is None:
                percent = DEFAULT_EFFECTIVE_WINDOW_PERCENT
            return window * percent // 100, "models-cache"
        return None, "unknown"
