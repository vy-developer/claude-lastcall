"""Cross-agent session index: live and historical Claude Code / Codex sessions.

Powers `lastcall status` (what is running now) and `lastcall tidy` (propose,
and on request apply, better names for old chats). Standard library only.

Reading is cheap on purpose. Claude transcripts can be tens of megabytes, so
they are memory-mapped and searched for byte markers such as
``"type":"custom-title"``; only the handful of lines that match are
json-parsed. Nothing here writes anywhere except `apply_plan`, which only
appends a title line to an agent's own title store after backing it up.

Conversation text is never written to disk: a title derived from a first
prompt exists only in memory (and on the terminal) until `apply_plan` puts it
into the agent's own title store. Plans carry a hash instead.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import glob
import hashlib
import json
import mmap
import os
import re
import shutil
import subprocess
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

CLAUDE = "claude"
CODEX = "codex"

PLAN_MARKER = "lastcall_tidy_plan"
PLAN_VERSION = 1
SEP = " \u00b7 "  # " · " between project and title

# Title markers as Claude Code writes them (compact JSON, no spaces). A marker
# inside a string value is escaped (\"type\":...), so it cannot false-match;
# every hit is still parsed and its top-level type checked.
_M_CUSTOM = b'"type":"custom-title"'
_M_AI = b'"type":"ai-title"'
_M_BRIDGE = b'"type":"bridge-session"'
_M_USER = b'"type":"user"'
_M_CWD = b'"cwd":"'
_M_TS = b'"timestamp":"'
_M_ENTRY = b'"entrypoint":"'

_ROLLOUT_RE = re.compile(
    r"rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(_[^/]*)?\.jsonl$")

# How a session is driven. Claude: registry/transcript `entrypoint`.
# Codex: session_meta `originator`.
SURFACES = ("cli", "desktop", "ide", "sdk", "unknown")
_CLAUDE_ENTRYPOINTS = {"cli": "cli", "claude-desktop": "desktop", "sdk-cli": "sdk",
                       "sdk-ts": "sdk", "sdk-py": "sdk"}
_CODEX_ORIGINATORS = {"codex-tui": "cli", "codex_cli_rs": "cli", "codex_exec": "cli",
                      "Codex Desktop": "desktop", "codex_work_desktop": "desktop",
                      "codex_vscode": "ide"}


@dataclasses.dataclass
class SessionRecord:
    agent: str                          # "claude" | "codex"
    session_id: str
    title: Optional[str] = None
    title_source: str = "none"          # "custom" | "ai" | "codex-index" | "none"
    cwd: Optional[str] = None
    project: Optional[str] = None       # git toplevel (main repo for worktrees) or cwd
    started_at: Optional[float] = None  # epoch seconds
    updated_at: Optional[float] = None  # epoch seconds (transcript mtime / registry)
    transcript_path: Optional[str] = None
    live: bool = False
    status: Optional[str] = None        # busy / idle / ... (live only)
    pid: Optional[int] = None
    remote_control: Optional[bool] = None  # None = unknown / not applicable
    bridge_session_id: Optional[str] = None
    handoff_chain: Optional[list] = None   # placeholder for the relay layer
    surface: str = "unknown"            # see SURFACES

    @property
    def project_name(self) -> str:
        return project_label(self.project or self.cwd)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["project_name"] = self.project_name
        return d


# ---------------------------------------------------------------- homes

def claude_home(explicit: Optional[str] = None) -> str:
    return os.path.abspath(os.path.expanduser(
        explicit or os.environ.get("LASTCALL_CLAUDE_HOME")
        or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def codex_home(explicit: Optional[str] = None) -> str:
    return os.path.abspath(os.path.expanduser(
        explicit or os.environ.get("LASTCALL_CODEX_HOME")
        or os.environ.get("CODEX_HOME") or "~/.codex"))


# ---------------------------------------------------------------- helpers

def parse_iso(value) -> Optional[float]:
    """ISO-8601 (Z or offset, any fraction length) -> epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:
        frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
        s = "%s.%s%s" % (m.group(1), frac, m.group(3))
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def iso_utc(epoch: Optional[float] = None) -> str:
    dt = datetime.datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                         datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class _Mapped:
    """Read-only mmap of a file; behaves like b"" when empty or unreadable."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None
        self.buf = b""

    def __enter__(self):
        try:
            self._fh = open(self.path, "rb")
            if os.fstat(self._fh.fileno()).st_size:
                self.buf = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            self.buf = b""
        return self.buf

    def __exit__(self, *exc):
        if isinstance(self.buf, mmap.mmap):
            self.buf.close()
        if self._fh:
            self._fh.close()


def _line_bounds(buf, pos: int) -> Tuple[int, int]:
    start = buf.rfind(b"\n", 0, pos) + 1
    end = buf.find(b"\n", pos)
    return start, (len(buf) if end == -1 else end)


def _parse(buf, start: int, end: int) -> Optional[dict]:
    try:
        d = json.loads(bytes(buf[start:end]).decode("utf-8", "replace"))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _last_entry(buf, marker: bytes, pred: Callable[[dict], bool], tries: int = 50):
    """Latest line containing `marker` whose parsed object satisfies `pred`."""
    pos = len(buf)
    for _ in range(tries):
        i = buf.rfind(marker, 0, pos)
        if i < 0:
            return None
        start, end = _line_bounds(buf, i)
        d = _parse(buf, start, end)
        if d is not None and pred(d):
            return d
        pos = start
    return None


def _entries(buf, marker: bytes, pred: Callable[[dict], bool], tries: int = 50):
    """Lines containing `marker` (earliest first) whose objects satisfy `pred`."""
    pos = 0
    for _ in range(tries):
        i = buf.find(marker, pos)
        if i < 0:
            return
        start, end = _line_bounds(buf, i)
        d = _parse(buf, start, end)
        if d is not None and pred(d):
            yield d
        pos = end + 1


def _first_entry(buf, marker, pred, tries=50):
    for d in _entries(buf, marker, pred, tries):
        return d
    return None


def _is_type(t: str) -> Callable[[dict], bool]:
    return lambda d: d.get("type") == t


def _has_str(key: str) -> Callable[[dict], bool]:
    return lambda d: isinstance(d.get(key), str) and bool(d.get(key))


@functools.lru_cache(maxsize=4096)
def project_root(cwd: Optional[str]) -> Optional[str]:
    """Git toplevel for cwd; a linked worktree resolves to its main repo so
    worktree sessions group with their project. No git subprocess: walks up
    looking for .git. Falls back to cwd (or a /.claude/worktrees/ prefix)."""
    if not cwd:
        return None
    path = os.path.abspath(cwd)
    probe = path
    while True:
        dot = os.path.join(probe, ".git")
        if os.path.isdir(dot):
            return probe
        if os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8") as fh:
                    head = fh.read(4096).strip()
            except OSError:
                head = ""
            if head.startswith("gitdir:"):
                gitdir = head[len("gitdir:"):].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(probe, gitdir))
                marker = os.sep + ".git" + os.sep + "worktrees" + os.sep
                if marker in gitdir:
                    return gitdir.split(marker)[0]
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    marker = os.sep + os.path.join(".claude", "worktrees") + os.sep
    if marker in path:
        return path.split(marker)[0]
    return path


def project_label(root: Optional[str]) -> str:
    if not root:
        return "?"
    root = root.rstrip(os.sep) or os.sep
    if root == os.path.expanduser("~").rstrip(os.sep):
        return "~"
    return os.path.basename(root) or root


def claude_surface(entrypoint: Optional[str]) -> str:
    if not entrypoint:
        return "unknown"
    if entrypoint in _CLAUDE_ENTRYPOINTS:
        return _CLAUDE_ENTRYPOINTS[entrypoint]
    e = entrypoint.lower()
    if any(k in e for k in ("vscode", "jetbrains", "ide", "cursor", "zed")):
        return "ide"
    if "desktop" in e:
        return "desktop"
    if e.startswith("sdk"):
        return "sdk"
    return "unknown"


def codex_surface(originator: Optional[str]) -> str:
    if not originator:
        return "unknown"
    if originator in _CODEX_ORIGINATORS:
        return _CODEX_ORIGINATORS[originator]
    o = originator.lower()
    if "desktop" in o:
        return "desktop"
    if "vscode" in o or "ide" in o:
        return "ide"
    if "tui" in o or "cli" in o or "exec" in o:
        return "cli"
    return "unknown"


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- Claude

def remote_control_connected(transcript_path: Optional[str]) -> Optional[str]:
    """bridgeSessionId of the latest bridge-session entry (truthy: Remote
    Control connected at some point in this session), else None."""
    if not transcript_path:
        return None
    with _Mapped(transcript_path) as buf:
        d = _last_entry(buf, _M_BRIDGE, _is_type("bridge-session"))
    if d is None:
        return None
    return d.get("bridgeSessionId") or ""


def scan_claude_transcript(path: str) -> SessionRecord:
    """Everything cheap to know about one transcript. Never parses the whole file."""
    sid = os.path.basename(path)[:-len(".jsonl")]
    rec = SessionRecord(agent=CLAUDE, session_id=sid, transcript_path=path,
                        updated_at=_mtime(path), remote_control=False)
    with _Mapped(path) as buf:
        custom = _last_entry(buf, _M_CUSTOM, lambda d: d.get("type") == "custom-title"
                             and isinstance(d.get("customTitle"), str))
        if custom and custom["customTitle"].strip():
            rec.title, rec.title_source = custom["customTitle"], "custom"
        else:
            ai = _last_entry(buf, _M_AI, lambda d: d.get("type") == "ai-title"
                             and isinstance(d.get("aiTitle"), str))
            if ai and ai["aiTitle"].strip():
                rec.title, rec.title_source = ai["aiTitle"], "ai"
        bridge = _last_entry(buf, _M_BRIDGE, _is_type("bridge-session"))
        if bridge is not None:
            rec.remote_control = True
            rec.bridge_session_id = bridge.get("bridgeSessionId")
        first = _first_entry(buf, _M_CWD, _has_str("cwd"))
        if first:
            rec.cwd = first["cwd"]
            entry = first.get("entrypoint")
        else:
            entry = None
        if not isinstance(entry, str):
            e = _first_entry(buf, _M_ENTRY, _has_str("entrypoint"), tries=10)
            entry = e.get("entrypoint") if e else None
        rec.surface = claude_surface(entry)
        ts = _first_entry(buf, _M_TS, _has_str("timestamp"), tries=10)
        rec.started_at = parse_iso(ts.get("timestamp")) if ts else None
    rec.project = project_root(rec.cwd)
    return rec


def claude_transcripts(home: Optional[str] = None) -> List[str]:
    """Main-session transcripts only (projects/<slug>/<id>.jsonl, not subagents)."""
    return sorted(glob.glob(os.path.join(claude_home(home), "projects", "*", "*.jsonl")))


def find_claude_transcript(session_id: str, home: Optional[str] = None) -> Optional[str]:
    if not session_id or "/" in session_id or session_id.startswith("."):
        return None
    hits = glob.glob(os.path.join(claude_home(home), "projects", "*",
                                  glob.escape(session_id) + ".jsonl"))
    return max(hits, key=lambda p: _mtime(p) or 0) if hits else None


def claude_registry(home: Optional[str] = None) -> List[dict]:
    """Entries of <home>/sessions/<pid>.json. Only *.json is opened, never *.key."""
    out = []
    folder = os.path.join(claude_home(home), "sessions")
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            d.setdefault("pid", int(name[:-5]))
            out.append(d)
    return out


def claude_live(home: Optional[str] = None) -> List[SessionRecord]:
    out = []
    for reg in claude_registry(home):
        sid = reg.get("sessionId")
        if not isinstance(sid, str) or not pid_alive(reg.get("pid")):
            continue
        path = find_claude_transcript(sid, home)
        rec = scan_claude_transcript(path) if path else SessionRecord(
            agent=CLAUDE, session_id=sid, remote_control=False)
        rec.live = True
        rec.pid = int(reg["pid"])
        rec.status = reg.get("status") if isinstance(reg.get("status"), str) else None
        if isinstance(reg.get("cwd"), str):
            rec.cwd = reg["cwd"]
            rec.project = project_root(rec.cwd)
        if isinstance(reg.get("entrypoint"), str):
            rec.surface = claude_surface(reg["entrypoint"])
        if rec.title is None and isinstance(reg.get("name"), str) and reg["name"].strip():
            rec.title = reg["name"]
            rec.title_source = "custom" if reg.get("nameSource") == "user" else "ai"
        if rec.started_at is None and isinstance(reg.get("startedAt"), (int, float)):
            rec.started_at = reg["startedAt"] / 1000.0
        if rec.updated_at is None and isinstance(reg.get("updatedAt"), (int, float)):
            rec.updated_at = reg["updatedAt"] / 1000.0
        out.append(rec)
    return out


def claude_sessions(home: Optional[str] = None) -> List[SessionRecord]:
    """All main-session transcripts, with live ones marked from the registry."""
    live = {r.session_id: r for r in claude_live(home)}
    out = []
    for path in claude_transcripts(home):
        sid = os.path.basename(path)[:-len(".jsonl")]
        if sid in live and live[sid].transcript_path == path:
            out.append(live.pop(sid))
        else:
            out.append(scan_claude_transcript(path))
    out.extend(live.values())  # live sessions with no transcript yet
    return out


def claude_first_prompt(path: str) -> Optional[str]:
    """First real user prompt (not meta, command output or tool result). In memory only."""
    def ok(d):
        if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
            return False
        return "toolUseResult" not in d
    with _Mapped(path) as buf:
        for d in _entries(buf, _M_USER, ok, tries=80):
            content = (d.get("message") or {}).get("content")
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
            else:
                texts = []
            for t in texts:
                if isinstance(t, str) and _is_prompt(t):
                    return t
    return None


def _is_prompt(text: str) -> bool:
    t = text.lstrip()
    return bool(t) and not t.startswith(("<", "Caveat:", "# AGENTS.md", "[Request interrupted"))


# ---------------------------------------------------------------- Codex

def codex_index(home: Optional[str] = None) -> Dict[str, dict]:
    """session_index.jsonl: id -> latest {"thread_name", "updated_at"} (last line wins)."""
    out: Dict[str, dict] = {}
    try:
        fh = open(os.path.join(codex_home(home), "session_index.jsonl"), "rb")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("id"), str) \
                    and isinstance(d.get("thread_name"), str):
                out[d["id"]] = d
    return out


def _rollout_groups(home: Optional[str] = None) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    pattern = os.path.join(codex_home(home), "sessions", "**", "rollout-*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        m = _ROLLOUT_RE.search(os.path.basename(path))
        if m:
            groups.setdefault(m.group(2), []).append(path)
    return groups


def _read_first_line(path: str, limit: int = 4 << 20) -> Optional[dict]:
    try:
        with open(path, "rb") as fh:
            line = fh.readline(limit)
    except OSError:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _codex_record(sid: str, paths: List[str], index: Dict[str, dict]) -> Optional[SessionRecord]:
    """None for subagent threads, which are not user chats."""
    base = sorted(paths, key=lambda p: (_ROLLOUT_RE.search(p).group(3) is not None, p))[0]
    meta = _read_first_line(base) or {}
    payload = meta.get("payload") if meta.get("type") == "session_meta" else {}
    payload = payload if isinstance(payload, dict) else {}
    source = payload.get("source")
    if payload.get("parent_thread_id") or (isinstance(source, dict) and "subagent" in source) \
            or payload.get("thread_source") == "subagent":
        return None
    newest = max(paths, key=lambda p: _mtime(p) or 0)
    rec = SessionRecord(agent=CODEX, session_id=sid, transcript_path=newest,
                        updated_at=_mtime(newest), remote_control=None)
    if isinstance(payload.get("cwd"), str):
        rec.cwd = payload["cwd"]
        rec.project = project_root(rec.cwd)
    rec.surface = codex_surface(payload.get("originator")
                                if isinstance(payload.get("originator"), str) else None)
    rec.started_at = parse_iso(payload.get("timestamp")) or parse_iso(meta.get("timestamp"))
    if rec.started_at is None:
        stamp = _ROLLOUT_RE.search(base).group(1)
        rec.started_at = parse_iso(stamp[:13] + ":" + stamp[14:16] + ":" + stamp[17:])
    entry = index.get(sid)
    if entry and entry["thread_name"].strip():
        rec.title, rec.title_source = entry["thread_name"], "codex-index"
    return rec


_UNSET = object()


def codex_sessions(home: Optional[str] = None, open_files=_UNSET) -> List[SessionRecord]:
    """All non-subagent Codex sessions, with live ones marked (see codex_live).
    open_files: {realpath: pid} of open rollouts; default asks lsof, None
    forces the recency fallback."""
    index = codex_index(home)
    groups = _rollout_groups(home)
    out = []
    for sid, paths in groups.items():
        rec = _codex_record(sid, paths, index)
        if rec is not None:
            out.append(rec)
    if open_files is _UNSET:
        open_files = codex_open_rollouts()
    _mark_codex_live(out, groups, open_files)
    return out


def codex_open_rollouts(timeout: float = 5.0) -> Optional[Dict[str, int]]:
    """{realpath: pid} of rollout files held open by codex processes (lsof).
    None when lsof is unavailable or fails, so callers can fall back."""
    try:
        proc = subprocess.run(["lsof", "-w", "-c", "codex", "-Fpn"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    out: Dict[str, int] = {}
    pid = None
    for line in proc.stdout.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and "rollout-" in line and line.endswith(".jsonl"):
            out[os.path.realpath(line[1:])] = pid
    return out


def codex_running() -> bool:
    try:
        proc = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(os.path.basename(l.strip()).startswith("codex")
               for l in proc.stdout.splitlines())


def _codex_turn_status(path: str) -> str:
    """busy if the last task_started has no later task_complete/turn_aborted."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - (256 << 10)))
            tail = fh.read()
    except OSError:
        return "unknown"
    started = tail.rfind(b'"type":"task_started"')
    ended = max(tail.rfind(b'"type":"task_complete"'), tail.rfind(b'"type":"turn_aborted"'))
    return "busy" if started > ended else "idle"


RECENT_SECONDS = 15 * 60


def _mark_codex_live(records: List[SessionRecord], groups: Dict[str, List[str]],
                     open_files: Optional[Dict[str, int]], now=None) -> None:
    if open_files is not None:
        by_path = {}
        for rec in records:
            for p in groups.get(rec.session_id, ()):
                by_path[os.path.realpath(p)] = rec
        for path, pid in open_files.items():
            rec = by_path.get(path)
            if rec is not None:
                rec.live, rec.pid = True, pid
    elif codex_running():  # best effort: recently written while codex runs
        now = time.time() if now is None else now
        for rec in records:
            if rec.updated_at and now - rec.updated_at < RECENT_SECONDS:
                rec.live = True
    for rec in records:
        if rec.live:
            rec.status = _codex_turn_status(rec.transcript_path)


def codex_live(home: Optional[str] = None, open_files=_UNSET) -> List[SessionRecord]:
    """Live Codex sessions. Uses lsof (rollouts held open by a codex process);
    without it, rollouts written in the last 15 minutes while codex runs."""
    if open_files is _UNSET:
        open_files = codex_open_rollouts()
    groups = _rollout_groups(home)
    if open_files is not None:
        wanted = {}
        for sid, paths in groups.items():
            if any(os.path.realpath(p) in open_files for p in paths):
                wanted[sid] = paths
        groups = wanted
    else:
        now = time.time()
        groups = {sid: ps for sid, ps in groups.items()
                  if any(now - (_mtime(p) or 0) < RECENT_SECONDS for p in ps)}
    index = codex_index(home) if groups else {}
    recs = [r for r in (_codex_record(s, p, index) for s, p in groups.items()) if r]
    _mark_codex_live(recs, groups, open_files)
    return [r for r in recs if r.live]


def codex_first_prompt(path: str) -> Optional[str]:
    with _Mapped(path) as buf:
        d = _first_entry(buf, b'"type":"user_message"', lambda d: d.get("type") == "event_msg"
                         and isinstance((d.get("payload") or {}).get("message"), str)
                         and _is_prompt(d["payload"]["message"]), tries=20)
        if d:
            return d["payload"]["message"]
        for d in _entries(buf, b'"role":"user"', lambda d: d.get("type") == "response_item",
                          tries=40):
            for block in (d.get("payload") or {}).get("content") or []:
                t = block.get("text") if isinstance(block, dict) else None
                if isinstance(t, str) and _is_prompt(t):
                    return t
    return None


# ---------------------------------------------------------------- combined

def all_sessions(claude_home_dir: Optional[str] = None, codex_home_dir: Optional[str] = None,
                 agents: Iterable[str] = (CLAUDE, CODEX),
                 codex_open_files=_UNSET) -> List[SessionRecord]:
    out: List[SessionRecord] = []
    if CLAUDE in agents:
        out.extend(claude_sessions(claude_home_dir))
    if CODEX in agents:
        out.extend(codex_sessions(codex_home_dir, codex_open_files))
    out.sort(key=lambda r: r.updated_at or 0, reverse=True)
    return out


def live_sessions(claude_home_dir: Optional[str] = None, codex_home_dir: Optional[str] = None,
                  agents: Iterable[str] = (CLAUDE, CODEX),
                  codex_open_files=_UNSET) -> List[SessionRecord]:
    """Only what is running; touches only the live sessions' transcripts."""
    out: List[SessionRecord] = []
    if CLAUDE in agents:
        out.extend(claude_live(claude_home_dir))
    if CODEX in agents:
        out.extend(codex_live(codex_home_dir, codex_open_files))
    out.sort(key=lambda r: r.updated_at or 0, reverse=True)
    return out


# ---------------------------------------------------------------- status

# Hook for the adapter layer: agent -> fn(record) -> Usage-like object with
# .tokens and .window (or None). Registered by lastcall_core.agents when present.
USAGE_PROVIDERS: Dict[str, Callable[[SessionRecord], object]] = {}


def register_usage_provider(agent: str, fn: Callable[[SessionRecord], object]) -> None:
    USAGE_PROVIDERS[agent] = fn


def _usage_cell(rec: SessionRecord) -> str:
    fn = USAGE_PROVIDERS.get(rec.agent)
    if fn is None:
        return "\u2013"
    try:
        usage = fn(rec)
    except Exception:  # an adapter bug must not break status
        return "?"
    tokens = getattr(usage, "tokens", None)
    if not isinstance(tokens, int):
        return "\u2013"
    window = getattr(usage, "window", None)
    if isinstance(window, int) and window > 0:
        return "%s/%s %d%%" % (_k(tokens), _k(window), round(100.0 * tokens / window))
    return _k(tokens)


def _k(n: int) -> str:
    return "%dk" % round(n / 1000.0) if n >= 1000 else str(n)


def age(epoch: Optional[float], now: Optional[float] = None) -> str:
    if epoch is None:
        return "\u2013"
    s = max(0, int((time.time() if now is None else now) - epoch))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return "%d%s" % (s // size, unit)
    return "%ds" % s


def rc_mark(value: Optional[bool]) -> str:
    return {True: "\u2713", False: "\u2717"}.get(value, "\u2013")


def _clip(text: Optional[str], width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def render_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for r in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


STATUS_HEADERS = ["AGENT", "SURFACE", "PROJECT", "TITLE", "STATUS", "RC", "AGE", "CONTEXT"]


def status_rows(records: List[SessionRecord], now: Optional[float] = None) -> List[List[str]]:
    return [[r.agent, r.surface, _clip(r.project_name, 24), _clip(r.title or "(untitled)", 48),
             r.status or "?", rc_mark(r.remote_control), age(r.updated_at, now),
             _usage_cell(r)] for r in records]


def render_status(records: List[SessionRecord], now: Optional[float] = None) -> str:
    if not records:
        return "No live sessions."
    return render_table(STATUS_HEADERS, status_rows(records, now))


# ---------------------------------------------------------------- tidy

VAGUE_TITLES = {
    "untitled", "new chat", "new session", "new conversation", "hello", "hi", "hey", "test",
    "testing", "help", "question", "continue", "fix", "fix bug", "bug", "debug", "misc",
    "chat", "session", "todo", "wip", "stuff", "quick question", "untitled session",
}
DERIVE_WORDS = 8
MAX_TITLE = 100


def is_vague(title: Optional[str]) -> bool:
    if not title or not title.strip():
        return True
    norm = _norm(title)
    if norm in VAGUE_TITLES or re.match(r"^(untitled|new (chat|session|conversation))\b", norm):
        return True
    return len(re.findall(r"[^\W_]+", norm)) < 2


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


def derive_title(prompt: Optional[str], words: int = DERIVE_WORDS) -> Optional[str]:
    """First ~8 words of a prompt, one line, no markup. In memory only."""
    if not prompt:
        return None
    text = re.sub(r"https?://\S+", "", prompt)
    text = re.sub(r"[`*_#>\[\]]+", " ", text)
    toks = text.split()
    if not toks:
        return None
    out = " ".join(toks[:words]).strip(" ,;:-")
    if len(toks) > words:
        out += "\u2026"
    return _clip(out, 60) or None


def clean_title(title: str) -> str:
    return _clip(title, MAX_TITLE)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def first_prompt(rec: SessionRecord) -> Optional[str]:
    if not rec.transcript_path:
        return None
    return (claude_first_prompt if rec.agent == CLAUDE else codex_first_prompt)(rec.transcript_path)


def build_plan(records: List[SessionRecord], claude_home_dir: Optional[str] = None,
               codex_home_dir: Optional[str] = None, include_desktop: bool = False,
               prompt_reader: Callable[[SessionRecord], Optional[str]] = first_prompt,
               now: Optional[float] = None) -> dict:
    """Propose "<project> · <title>" names. Read-only.

    Items whose title is derived from a first prompt carry proposed_title=None
    and a `derived` {prefix, suffix, sha256} so the plan file never holds prompt
    text; the derived title rides along in the private "_display" key (dropped
    by plan_to_json) for the terminal table."""
    items = []
    for rec in records:
        name = rec.project_name
        prefix = name + SEP
        flags = []
        current = rec.title if rec.title and rec.title.strip() else None
        if current is None:
            flags.append("untitled")
        base, derived = current, False
        if current and current.startswith(prefix):
            base = current[len(prefix):]
        else:
            flags.append("unprefixed")
        if current is not None and is_vague(base):
            flags.append("vague")
        if base is None or is_vague(base):
            d = derive_title(prompt_reader(rec))
            if d:
                base, derived = d, True
            elif base is None:
                when = rec.started_at or rec.updated_at
                base = "session " + (datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%d")
                                     if when else rec.session_id[:8])
        if rec.surface == "desktop":
            flags.append("desktop")
        if rec.live:
            flags.append("live")
        items.append({
            "agent": rec.agent, "session_id": rec.session_id, "surface": rec.surface,
            "project": name, "project_root": rec.project or rec.cwd,
            "current_title": current, "title_source": rec.title_source, "flags": flags,
            "transcript_path": rec.transcript_path,
            "started_at": iso_utc(rec.started_at) if rec.started_at else None,
            "updated_at": iso_utc(rec.updated_at) if rec.updated_at else None,
            "_prefix": prefix, "_base": clean_title(base), "_derived": derived,
            "_live": rec.live,
        })

    # duplicates: same final name within a project -> add start date, then a counter
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for it in items:
        groups.setdefault((it["project_root"] or "", _norm(it["_base"])), []).append(it)
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda it: it["started_at"] or "")
        seen: Dict[str, int] = {}
        for it in group:
            it["flags"].append("duplicate")
            suffix = SEP + (it["started_at"] or "")[:10] if it["started_at"] else ""
            n = seen.get(suffix, 0) + 1
            seen[suffix] = n
            it["_suffix"] = suffix + (" (%d)" % n if n > 1 else "")

    for it in items:
        suffix = it.pop("_suffix", "")
        final = it["_prefix"] + it["_base"] + suffix
        action, reason = "rename", None
        if it.pop("_live"):
            action, reason = "skip", "live session"
        elif it["surface"] == "desktop" and not include_desktop:
            action, reason = "skip", "desktop app keeps its own title; rename it in the app"
        elif not it["transcript_path"]:
            action, reason = "skip", "no transcript"
        elif final == it["current_title"]:
            action, reason = "keep", "already tidy"
        it["action"], it["reason"] = action, reason
        it["_display"] = final
        if it.pop("_derived"):
            it["proposed_title"] = None
            it["derived"] = {"from": "first-prompt", "prefix": it["_prefix"], "suffix": suffix,
                             "sha256": _sha(final)}
        else:
            it["proposed_title"] = final
        del it["_prefix"], it["_base"]

    items.sort(key=lambda it: (it["project"].lower(), it["agent"], it["started_at"] or ""))
    return {
        PLAN_MARKER: PLAN_VERSION,
        "created_at": iso_utc(now),
        "claude_home": claude_home(claude_home_dir),
        "codex_home": codex_home(codex_home_dir),
        "include_desktop": bool(include_desktop),
        "note": ("Review, then run `tidy --apply <this file>`. Set an item's action to "
                 "\"skip\" to leave it alone, or set proposed_title to any name. Items "
                 "with proposed_title null are named from their first prompt at apply "
                 "time (shown in the tidy table, never stored here)."),
        "items": items,
    }


def plan_to_json(plan: dict) -> str:
    public = dict(plan)
    public["items"] = [{k: v for k, v in it.items() if not k.startswith("_")}
                       for it in plan["items"]]
    return json.dumps(public, indent=2, ensure_ascii=False) + "\n"


TIDY_HEADERS = ["AGENT", "SURFACE", "PROJECT", "ACTION", "FLAGS", "CURRENT", "PROPOSED"]


def render_plan(plan: dict, group_by: str = "project") -> str:
    items = plan["items"]
    if not items:
        return "Nothing to tidy."
    key = (lambda it: it["surface"]) if group_by == "surface" else (lambda it: it["project"])
    items = sorted(items, key=lambda it: (key(it).lower(), it["project"].lower(),
                                          it["started_at"] or ""))
    blocks, current, rows = [], None, []

    def flush():
        if rows:
            blocks.append("[%s]\n%s" % (current, render_table(TIDY_HEADERS, rows)))

    for it in items:
        if key(it) != current:
            flush()
            current, rows = key(it), []
        proposed = it.get("_display") or it.get("proposed_title") or "(from first prompt)"
        rows.append([it["agent"], it["surface"], _clip(it["project"], 20),
                     it["action"], ",".join(it["flags"]) or "-",
                     _clip(it["current_title"] or "(untitled)", 40),
                     _clip(proposed, 56) if it["action"] == "rename" else
                     "(%s)" % (it["reason"] or it["action"])])
    flush()
    counts: Dict[str, int] = {}
    for it in items:
        counts[it["action"]] = counts.get(it["action"], 0) + 1
    summary = ", ".join("%d %s" % (n, a) for a, n in sorted(counts.items()))
    return "\n\n".join(blocks) + "\n\n%d sessions: %s" % (len(items), summary)


class PlanError(Exception):
    pass


def load_plan(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PlanError("cannot read plan %s: %s" % (path, exc))
    if not isinstance(plan, dict) or plan.get(PLAN_MARKER) != PLAN_VERSION \
            or not isinstance(plan.get("items"), list):
        raise PlanError("%s is not a lastcall tidy plan (make one with `tidy --plan`)" % path)
    return plan


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root


def _append_line(path: str, obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    with open(path, "ab+") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                data = b"\n" + data
        fh.seek(0, os.SEEK_END)
        fh.write(data)


def _resolve_title(it: dict) -> Tuple[Optional[str], Optional[str]]:
    proposed = it.get("proposed_title")
    if isinstance(proposed, str) and proposed.strip():
        return clean_title(proposed), None
    derived = it.get("derived")
    if not isinstance(derived, dict):
        return None, "no proposed_title"
    rec = SessionRecord(agent=it.get("agent"), session_id=it.get("session_id") or "",
                        transcript_path=it.get("transcript_path"))
    base = derive_title(first_prompt(rec))
    if not base:
        return None, "first prompt not found"
    title = str(derived.get("prefix", "")) + clean_title(base) + str(derived.get("suffix", ""))
    if _sha(title) != derived.get("sha256"):
        return None, "transcript changed since the plan was made"
    return title, None


def apply_plan(plan_path: str, claude_home_dir: Optional[str] = None,
               codex_home_dir: Optional[str] = None, dry_run: bool = False,
               now: Optional[float] = None, codex_open_files=_UNSET) -> dict:
    """Apply an approved plan file. Claude: append a custom-title line to the
    transcript. Codex: append an entry to session_index.jsonl. Live sessions
    are re-checked and skipped, as are desktop-app sessions unless the plan
    was made with include_desktop (the apps keep their own titles). Every file is copied to <home>/lastcall-backups/<stamp>/
    before its first write. Returns {"applied": [...], "skipped": [...], "backups": [...]}."""
    plan = load_plan(plan_path)
    c_home, x_home = claude_home(claude_home_dir), codex_home(codex_home_dir)
    for key, home in (("claude_home", c_home), ("codex_home", x_home)):
        if os.path.realpath(str(plan.get(key))) != os.path.realpath(home):
            raise PlanError("plan was made for %s=%s, but this run uses %s"
                            % (key, plan.get(key), home))
    stamp = datetime.datetime.fromtimestamp(time.time() if now is None else now) \
        .strftime("%Y%m%d-%H%M%S")
    include_desktop = bool(plan.get("include_desktop"))
    live = {(r.agent, r.session_id) for r in
            live_sessions(c_home, x_home, codex_open_files=codex_open_files)}
    result = {"applied": [], "skipped": [], "backups": [], "dry_run": dry_run}
    backed_up: Dict[str, str] = {}

    def backup(path: str, home: str) -> None:
        if path in backed_up or dry_run:
            return
        folder = os.path.join(home, "lastcall-backups", stamp)
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, os.path.basename(path))
        shutil.copy2(path, dest)
        backed_up[path] = dest
        result["backups"].append(dest)

    def skip(it, why):
        result["skipped"].append({"agent": it.get("agent"), "session_id": it.get("session_id"),
                                  "reason": why})

    codex_titles = None
    for it in plan["items"]:
        if not isinstance(it, dict) or it.get("action") != "rename":
            continue
        agent, sid = it.get("agent"), it.get("session_id")
        if not isinstance(sid, str) or not sid:
            skip(it, "missing session_id")
            continue
        if (agent, sid) in live:
            skip(it, "live session")
            continue
        if it.get("surface") == "desktop" and not include_desktop:
            skip(it, "desktop session")
            continue
        title, why = _resolve_title(it)
        if title is None:
            skip(it, why)
            continue
        if agent == CLAUDE:
            path = it.get("transcript_path")
            if not isinstance(path, str) or os.path.basename(path) != sid + ".jsonl" \
                    or not _inside(path, os.path.join(c_home, "projects")) \
                    or not os.path.isfile(path):
                skip(it, "transcript missing or outside claude_home")
                continue
            if scan_claude_transcript(path).title == title:
                skip(it, "already has this title")
                continue
            if not dry_run:
                backup(path, c_home)
                _append_line(path, {"type": "custom-title", "customTitle": title,
                                    "sessionId": sid})
        elif agent == CODEX:
            index_path = os.path.join(x_home, "session_index.jsonl")
            if codex_titles is None:
                codex_titles = {k: v["thread_name"] for k, v in codex_index(x_home).items()}
            if codex_titles.get(sid) == title:
                skip(it, "already has this title")
                continue
            if not dry_run:
                if os.path.exists(index_path):
                    backup(index_path, x_home)
                _append_line(index_path, {"id": sid, "thread_name": title,
                                          "updated_at": iso_utc(now)})
            codex_titles[sid] = title
        else:
            skip(it, "unknown agent")
            continue
        result["applied"].append({"agent": agent, "session_id": sid})
    return result
