"""Per-session state, kept per (agent, session id).

Two writers touch the same file: the hooks and, on Claude Code, the optional
status line (which caches the exact window). Each writer re-reads the file just
before writing and applies only the keys it changed, then swaps the file in
with os.replace, so neither can wipe out the other's fields — in particular the
hooks never clobber window_from_statusline.

State from before 1.8 lived in ~/.claude/lastcall/<session>.json. It is read
when a Claude session has no new-style file yet, and written back to the new
location, so an upgrade mid-session does not re-warn.
"""

import json
import os
import re
import time

from .config import legacy_state_dir, state_dir

ONBOARDED_FILE = "onboarded.json"
# Learned model windows (windows.py). Machine-wide knowledge, not a session's
# state, so it is never pruned.
LEARNED_FILE = "windows.json"
DEBUG_FILE = "last-payload.json"

# What marks a JSON file as one this tool wrote. Pruning deletes only those:
# state_dir is user-settable, and pointing it at ~/.claude used to mean
# settings.json was removed after the TTL. Extension is not ownership, and
# neither is a generic key such as "updated": every state file carries this
# marker. A file from before the marker counts only with the "agent" key the
# hooks have always written plus one of these.
OWNER_KEY = "_lastcall"
_LEGACY_KEYS = frozenset(("sig", "band", "epoch"))
# The pre-2.0 directory (~/.claude/lastcall) is Last Call's own; its files
# predate "agent" too.
_PRE_2_KEYS = frozenset(("band", "peak", "epoch", "max_observed"))


def _safe(session_id):
    return re.sub(r"[^A-Za-z0-9._-]", "-", session_id or "unknown")


def state_path(config, session_id, agent="claude", env=None):
    return os.path.join(state_dir(config, env),
                        "%s-%s.json" % (agent or "agent", _safe(session_id)))


def legacy_state_path(config, session_id, agent="claude"):
    """The pre-1.8 file for this session, or None when it cannot apply."""
    if config.get("state_dir") or agent != "claude":
        return None
    return os.path.join(legacy_state_dir(), "%s.json" % _safe(session_id))


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    """Atomic replace. A half-written state file reads as corrupt on the next
    turn, and corrupt state silences the guard for the rest of the session."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = "%s.tmp%d" % (path, os.getpid())
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temporary, path)
        return True
    except OSError:
        return False


class SessionState(dict):
    """A session's state that remembers which keys this process changed."""

    def __init__(self, config, session_id, agent="claude"):
        super(SessionState, self).__init__()
        self.config = config
        self.session_id = session_id
        self.agent = agent
        self.path = state_path(config, session_id, agent)
        data = _load(self.path)
        self.migrated = False
        if not data and not os.path.exists(self.path):
            legacy = legacy_state_path(config, session_id, agent)
            if legacy and os.path.isfile(legacy):
                data = _load(legacy)
                self.migrated = bool(data)
        dict.update(self, data)
        self._dirty = set(data) if self.migrated else set()

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        self._dirty.add(key)

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._dirty.add(key)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def number(self, key, default=0):
        try:
            return int(self.get(key) or default)
        except (TypeError, ValueError):
            return default

    def save(self):
        """Merge this process's changes into whatever is on disk now."""
        current = _load(self.path)
        for key in self._dirty:
            if key in self:
                current[key] = self[key]
            else:
                current.pop(key, None)
        current["updated"] = int(time.time())
        current["agent"] = self.agent
        current[OWNER_KEY] = 1
        ok = _write(self.path, current)
        if ok:
            self._dirty = set()
        return ok


# How long a hook waits for another hook of the same session to finish judging
# a reading. Judging takes milliseconds; a holder this slow is stuck, and the
# waiter stays silent rather than risk saying the same thing twice.
LOCK_TIMEOUT = 2.0


class SessionLock(object):
    """An exclusive lock on one session's state, around decide -> emit -> save.

    Parallel tool calls fire PostToolUse hooks at the same moment; each read
    the same "not announced yet" state and each emitted the same warning.
    Under this lock the second one re-reads the state the first one saved and
    stays silent.

    flock (POSIX) or msvcrt.locking (Windows) on a sidecar file — the state
    file itself is swapped by os.replace, so it cannot carry a lock. Where
    neither exists, an O_CREAT|O_EXCL marker. ``acquired`` is True when held,
    False when another holder kept it past ``timeout`` (the caller should stay
    silent), and None when no lock could be made at all (an unwritable state
    dir: the caller carries on unlocked, as it would have before).
    """

    def __init__(self, path, timeout=LOCK_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self.handle = None
        self.marker = False
        self.acquired = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            return self
        deadline = time.monotonic() + self.timeout
        try:
            import fcntl
        except ImportError:
            fcntl = None
        msvcrt = None
        if fcntl is None:
            try:
                import msvcrt
            except ImportError:
                msvcrt = None
        if fcntl is None and msvcrt is None:
            return self._marker(deadline)
        try:
            self.handle = open(self.path, "a+", encoding="utf-8")
        except OSError:
            return self
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                self.acquired = True
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.acquired = False
                    return self
                time.sleep(0.002)

    def _marker(self, deadline):
        marker = self.path + ".held"
        while True:
            try:
                os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.marker, self.acquired = True, True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(marker) > 10 * self.timeout:
                        os.remove(marker)       # left behind by a killed hook
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    self.acquired = False
                    return self
                time.sleep(0.002)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.marker:
            try:
                os.remove(self.path + ".held")
            except OSError:
                pass
        if self.handle is not None:
            try:
                if self.acquired and os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                pass
            try:
                self.handle.close()  # closing releases a flock
            except OSError:
                pass
        return False


def session_lock(config, session_id, agent="claude"):
    return SessionLock(state_path(config, session_id, agent) + ".lock")


def read_state(config, session_id, agent="claude"):
    return dict(SessionState(config, session_id, agent))


def write_state(config, session_id, state, agent="claude"):
    """Write ``state`` over the session's state, keeping any field another
    writer set that ``state`` does not mention (window_from_statusline)."""
    current = SessionState(config, session_id, agent)
    current.update(state)
    ok = current.save()
    state["updated"] = current.get("updated", int(time.time()))
    return ok


def update_state(config, session_id, changes, agent="claude"):
    """Apply ``changes`` to the session's state, touching nothing else."""
    current = SessionState(config, session_id, agent)
    current.update(changes)
    return current.save()


# The session's permission mode as its hooks last saw it: every payload but
# Claude's SessionStart carries "permission_mode" (Claude: default, acceptEdits,
# plan, auto, dontAsk, bypassPermissions; Codex: default or bypassPermissions,
# the latter only with approvals and the sandbox both off). The relay reads it
# to let a successor inherit bypass mode.
PERMISSION_MODE_KEY = "permission_mode"


def note_permission_mode(state, payload):
    """Record ``payload``'s permission_mode in ``state``; True when it changed.
    Costs nothing when the mode is unchanged: no read, and nothing to save."""
    mode = payload.get(PERMISSION_MODE_KEY) if isinstance(payload, dict) else None
    if not isinstance(mode, str) or not mode or len(mode) > 64:
        return False
    if state.get(PERMISSION_MODE_KEY) == mode:
        return False
    state[PERMISSION_MODE_KEY] = mode
    return True


def recorded_permission_mode(config, session_id, agent, env=None):
    """The permission mode a session's hooks last recorded, or None."""
    if not session_id:
        return None
    mode = _load(state_path(config, session_id, agent, env)).get(PERMISSION_MODE_KEY)
    return mode if isinstance(mode, str) and mode else None


def prune_state(config):
    """Old sessions never come back; their state files should not outlive them."""
    ttl_days = config.get("state_ttl_days") or 0
    if ttl_days <= 0:
        return
    cutoff = time.time() - (ttl_days * 86400)
    directories = [(state_dir(config), False)]
    if not config.get("state_dir"):
        directories.append((legacy_state_dir(), True))
    for directory, pre_2 in directories:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if name.endswith(".json.lock"):
                _prune_lock(os.path.join(directory, name), cutoff)
                continue
            if not name.endswith(".json") or name in (ONBOARDED_FILE, LEARNED_FILE):
                continue
            target = os.path.join(directory, name)
            try:
                if os.path.getmtime(target) >= cutoff:
                    continue
                if not _is_our_state(target, pre_2):
                    continue
                os.remove(target)
            except OSError:
                pass


def _prune_lock(path, cutoff):
    """A session's lock sidecar: always empty, and as dead as its session."""
    try:
        if os.path.getmtime(path) < cutoff and os.path.getsize(path) == 0:
            os.remove(path)
    except OSError:
        pass


def _is_our_state(path, pre_2=False):
    """True only for a file this tool created."""
    if os.path.basename(path) == DEBUG_FILE:
        return True
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get(OWNER_KEY) == 1:
        return True
    if data.get("agent") in ("claude", "codex") and _LEGACY_KEYS & set(data):
        return True
    return bool(pre_2 and "updated" in data and _PRE_2_KEYS & set(data))


# --------------------------------------------------------------------------
# Onboarding: once per project, not once per session
# --------------------------------------------------------------------------

def _onboarded_path(config):
    return os.path.join(state_dir(config), ONBOARDED_FILE)


def _project_key(project):
    try:
        return os.path.realpath(project)
    except (OSError, ValueError):
        return project


def was_onboarded(config, project):
    return _project_key(project) in _load(_onboarded_path(config))


def mark_onboarded(config, project, agent=None):
    path = _onboarded_path(config)
    data = _load(path)
    data[_project_key(project)] = {"at": int(time.time()), "agent": agent}
    return _write(path, data)


# --------------------------------------------------------------------------

def write_debug(config, payload):
    """Opt-in, redacted, capped. The raw payload contains the entire text of the
    last assistant message, so it is never written unless explicitly asked for."""
    if not config.get("debug"):
        return
    redacted = {
        key: value
        for key, value in payload.items()
        if key not in ("last_assistant_message", "prompt", "tool_response",
                       "tool_input")
    }
    redacted["_redacted"] = ["last_assistant_message", "prompt",
                             "tool_response", "tool_input"]
    try:
        os.makedirs(state_dir(config), exist_ok=True)
        target = os.path.join(state_dir(config), DEBUG_FILE)
        text = json.dumps(redacted, indent=2)[:100_000]
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass
