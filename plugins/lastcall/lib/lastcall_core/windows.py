"""Model -> context window: the `windows` map and the windows learned so far.

Claude Code transcripts never say how big a session's window is, and the same
model identifier is used for the 200K and the 1M variant. Two things close
that gap without guessing:

  the `windows` config key   {"claude-opus-5-5": 1000000, "claude-*": 200000,
                              "claude:opus": 1000000}
                             matched against the session's model: an exact key
                             beats a pattern, a longer pattern beats a shorter
                             one, and a key qualified with its agent
                             ("claude:...") beats an unqualified one. A key
                             without "*" or "?" is a prefix; with them it is a
                             glob ("[" is literal, so "opus[1m]" is a key).

  learned windows            <state_dir>/windows.json, written whenever a
                             session PROVES its window: more than 200K tokens
                             in use (so 1M), the status line reporting the
                             exact figure, or a Codex rollout stating it.
                             Keyed "agent:model", latest proof wins, with the
                             source and time of that proof kept for doctor.

Both are advisory data about a MODEL, not about a session, so both rank below
anything that describes the session itself (explicit config, the status line
cache) — see zones.resolve_window for the full order.
"""

import json
import os
import re
import time

from .config import state_dir

LEARNED_FILE = "windows.json"
AGENTS = ("claude", "codex")

# A model that names its window ("opus[1m]") says more than a key that does
# not, so such a model is only matched by keys that carry the marker too.
_ONE_M_MARKER = re.compile(r"\[1m\]", re.I)

# Waiting longer than this for another writer is not worth holding a hook up:
# the write goes ahead unlocked (still atomic) and at worst one concurrent
# learning is lost and re-learned by that session's next hook.
LOCK_TIMEOUT = 1.0


# --------------------------------------------------------------------------
# The `windows` config map
# --------------------------------------------------------------------------

def _split_key(key):
    """(agent or None, pattern) for a map key."""
    head, sep, tail = key.partition(":")
    if sep and head.strip().lower() in AGENTS:
        return head.strip().lower(), tail.strip()
    return None, key.strip()


def _is_glob(pattern):
    return "*" in pattern or "?" in pattern


def _glob_regex(pattern):
    """fnmatch without character classes: only * and ? are special."""
    parts = []
    for char in pattern:
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts) + r"\Z", re.I)


def validate_windows(value):
    """(clean map or None, [problems]). Bad entries are dropped one by one;
    a value that is not an object at all is dropped whole."""
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, ['windows should be an object like {"claude-opus-5-5": '
                      '1000000}, got %s — ignored' % type(value).__name__]
    clean = {}
    problems = []
    for key, window in value.items():
        if not isinstance(key, str) or str(key).startswith("_"):
            continue  # "_comment" keys, as everywhere else in the config
        head, sep, _tail = key.partition(":")
        agent, pattern = _split_key(key)
        if not pattern:
            problems.append('windows key "%s" names no model — ignored' % key)
            continue
        if sep and agent is None and re.match(r"[A-Za-z]+\Z", head.strip()):
            problems.append('windows key "%s": "%s" is not an agent (%s) — the '
                            "whole key is matched as a model name"
                            % (key, head, "/".join(AGENTS)))
        if isinstance(window, bool) or not isinstance(window, int):
            problems.append('windows["%s"] should be a number of tokens, got %r '
                            "— ignored" % (key, window))
            continue
        if window <= 0:
            problems.append('windows["%s"] should be a positive number of '
                            "tokens, got %s — ignored" % (key, window))
            continue
        clean[key] = window
    return clean, problems


def match_window(windows, agent, model):
    """(window, key) of the best `windows` entry for ``model``, or
    (None, None).

    Ranking: exact beats pattern; among patterns the longer (more literal
    characters) wins; at equal rank an agent-qualified key beats a bare one.
    Case-insensitive.
    """
    if not windows or not isinstance(model, str) or not model:
        return None, None
    lowered = model.strip().lower()
    marked = bool(_ONE_M_MARKER.search(lowered))
    agent = (agent or "").lower()
    best = None
    for key, window in windows.items():
        if not isinstance(key, str) or isinstance(window, bool) \
                or not isinstance(window, int) or window <= 0:
            continue
        key_agent, pattern = _split_key(key)
        if key_agent and key_agent != agent:
            continue
        pattern = pattern.lower()
        if not pattern:
            continue
        if marked and not _ONE_M_MARKER.search(pattern):
            continue
        if pattern == lowered:
            rank = (2, len(pattern))
        elif _is_glob(pattern):
            if not _glob_regex(pattern).match(lowered):
                continue
            rank = (1, len(pattern.replace("*", "").replace("?", "")))
        elif lowered.startswith(pattern):
            rank = (1, len(pattern))
        else:
            continue
        rank = rank + (1 if key_agent else 0,)
        if best is None or rank > best[0]:
            best = (rank, window, key)
    if best is None:
        return None, None
    return best[1], best[2]


# --------------------------------------------------------------------------
# Learned windows
# --------------------------------------------------------------------------

def learned_path(config):
    return os.path.join(state_dir(config), LEARNED_FILE)


def learned_key(agent, model):
    return "%s:%s" % ((agent or "claude").lower(), model.strip().lower())


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def load_learned(config):
    """{"agent:model": {"window", "source", "at", "agent", "model", "seen"}}.
    Empty when there is nothing learned or the file is unreadable."""
    return _read(learned_path(config))


def learned_window(learned, agent, model):
    """(window, entry) learned for ``model`` under ``agent``, or (None, None)."""
    if not learned or not isinstance(model, str) or not model.strip():
        return None, None
    entry = learned.get(learned_key(agent, model))
    if not isinstance(entry, dict):
        return None, None
    window = entry.get("window")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        return None, None
    return window, entry


class _Lock(object):
    """An exclusive advisory lock on ``path`` where the platform has one.
    Never raises and never waits longer than LOCK_TIMEOUT."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:  # Windows: atomic replace alone
            return self
        try:
            self.handle = open(self.path, "a+")
        except OSError:
            return self
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= deadline:
                    return self
                time.sleep(0.005)

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                self.handle.close()  # closing releases the flock
            except OSError:
                pass
        return False


def record_learned(config, agent, model, window, source, now=None):
    """Merge one proof into the learned map. True when it was written.

    Read-modify-write under a lock, and swapped in with os.replace, so two
    sessions learning at once keep both entries and a reader never sees half
    a file. Only this one key is touched.
    """
    if not isinstance(model, str) or not model.strip() or not window:
        return False
    try:
        window = int(window)
    except (TypeError, ValueError):
        return False
    if window <= 0:
        return False
    path = learned_path(config)
    now = int(now if now is not None else time.time())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return False
    key = learned_key(agent, model)
    with _Lock(path + ".lock"):
        models = _read(path)
        entry = models.get(key) if isinstance(models.get(key), dict) else {}
        seen = entry.get("seen") if isinstance(entry.get("seen"), dict) else {}
        seen[str(window)] = now
        models[key] = {"agent": (agent or "claude").lower(), "model": model.strip(),
                       "window": window, "source": source, "at": now, "seen": seen}
        temporary = "%s.tmp%d.%s" % (path, os.getpid(), os.urandom(4).hex())
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "models": models}, handle, indent=1,
                          sort_keys=True)
            os.replace(temporary, path)
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass
            return False
    return True


def proof_for(agent_name, state, usage):
    """(window, source) this session has PROVED for its model, or
    (None, None). Only exact reports and hard evidence count: config, the
    map, settings and the fallback are claims, and learning from a claim
    would turn one wrong setting into a machine-wide belief."""
    from .agents.claude import window_from_evidence
    state = state or {}
    if agent_name == "codex":
        if usage is not None and usage.window and usage.window_source == "transcript":
            return int(usage.window), "rollout"
        return None, None
    statusline = state.get("window_from_statusline")
    if statusline:
        try:
            return int(statusline), "statusline"
        except (TypeError, ValueError):
            pass
    # This reading only, not the session's max_observed: after a /model
    # switch the peak belongs to the previous model's window.
    proven = window_from_evidence(int(getattr(usage, "tokens", 0) or 0))
    if proven:
        return proven, "evidence"
    return None, None


def learn(config, agent_name, state, usage):
    """Record what this session proved about its model, once per session per
    (model, window): the session state remembers it, so the hook on every
    tool call does not touch windows.json again. Returns True on a write."""
    model = getattr(usage, "model", None)
    if not isinstance(model, str) or not model.strip():
        return False
    window, source = proof_for(agent_name, state, usage)
    if not window:
        return False
    marker = [model, window, source]
    if state.get("learned") == marker:
        return False
    if record_learned(config, agent_name, model, window, source):
        state["learned"] = marker
        return True
    return False
