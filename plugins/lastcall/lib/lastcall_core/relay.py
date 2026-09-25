#!/usr/bin/env python3
"""The relay — hand a session over to a fresh Claude Code or Codex successor.

Stdlib only, Python 3.9+, POSIX. Nothing here assumes a terminal: the
predecessor may be a CLI session in tmux, a `claude --bg` job, or a desktop-app
session with no TTY at all.

What it does, in order, refusing at the first failure:

  1. picks the handoff (newest non-TEMPLATE .md in the handoff dir) and, when
     the repo is a git worktree, refuses unless it is committed;
  2. names the successor "<prefix> · handoff N · <topic>";
  3. spawns it detached:
       claude  `claude --bg -n NAME --remote-control NAME [--model M]
                [--fallback-model F] --settings JSON PROMPT` (--bg assigns the
                session id itself and ignores --session-id, so none is passed);
       codex   app mode (default): a detached `relay.py codex-app-runner` that
               drives `codex app-server` over stdio JSON-RPC — the interface the
               Codex desktop app uses — so the thread is listed in the app's
               sidebar and in `codex resume` (source "vscode");
               exec mode: `codex exec --json -C REPO [-m M] -s workspace-write
               PROMPT` (hidden from both unless --include-non-interactive);
               tmux mode: an interactive `codex` inside `tmux new-session -d`;
  4. waits for the successor to CHECK IN on a ledger
     (~/.lastcall/relay/<chain>.jsonl), matched by chain + generation, instead
     of guessing a transcript path:
       claude  its SessionStart hook (injected via --settings) runs
               `relay.py checkin ...` which appends session_id + transcript_path
       codex   app: the runner, once turn/start is accepted; exec: the
               `thread.started` event on `codex exec --json`; tmux: the
               plugin's check-in, else the one rollout created after the
               spawn whose cwd is the repo (never the predecessor's);
       any     with the Last Call plugin installed, the engine's SessionStart
               hook also checks in (successor_session_start) and tells the
               successor which generation it is and which handoff to read;
     app mode falls back to exec, loudly, when it fails before the successor's
     turn starts;
  5. claude: verifies Remote Control actually connected (bridgeSessionId in
     ~/.claude/jobs/<short>/state.json for a --bg session, else a
     `bridge-session` transcript entry or ~/.claude/sessions) and says
     "remote control did NOT connect" when it did not;
     codex: names the thread through `codex app-server` (thread/name/set);
  6. optionally retires the predecessor — `claude stop <id>` for a background
     job, `tmux kill-pane` for the pane the predecessor really runs in (the
     whole session only when a relay created it), a delayed SIGTERM for a
     plain CLI process — from a detached Python child (no `setsid` needed).
     A desktop-app session is never killed; the relay says so instead.

Settings: the `relay` block of the layered Last Call config (lastcall_core.
config: ~/.lastcall/config.json < the project's .lastcall.json, .lastcall/
config.json, .claude/lastcall.json or .codex/lastcall.json < LASTCALL_RELAY),
merged key by key; flags win over all of it. The successor's agent defaults to
the one running the predecessor; `--agent codex|claude` hands over across.
`lastcall relay ...` runs this same main().

Exit codes: 0 successor checked in; 1 precondition failure, nothing spawned;
2 spawned but it never checked in (or remote control was required and absent).

`relay.py --dry-run` prints every command without running any of them.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

EXIT_OK = 0
EXIT_PRECONDITION = 1
EXIT_UNPROVEN = 2

CHAIN_ENV = "LASTCALL_RELAY_CHAIN"
GENERATION_ENV = "LASTCALL_RELAY_GENERATION"
LEDGER_ENV = "LASTCALL_RELAY_LEDGER"
HANDOFF_ENV = "LASTCALL_RELAY_HANDOFF"
AGENT_ENV = "LASTCALL_RELAY_AGENT"
# One per spawn attempt. A check-in only counts when it carries the nonce of
# the attempt being waited for: chain + generation + agent alone also match a
# stale successor from an earlier (timed-out, retried) attempt.
NONCE_ENV = "LASTCALL_RELAY_NONCE"
LEDGER_DIR_ENV = "LASTCALL_RELAY_DIR"

AGENTS = ("claude", "codex")
SEP = " · "

# The successor's permission mode. Claude Code takes any of these as
# --permission-mode (it lists "default" as "manual" since 2.1.x and still
# accepts "default"); bypassPermissions is passed as
# --dangerously-skip-permissions. A Codex successor maps bypassPermissions to
# full access (no sandbox, approvals bypassed) and keeps codex_sandbox /
# codex_approval for every other mode. "inherit" is the default behaviour:
# auto, or bypass when the predecessor itself runs in bypass mode.
BYPASS = "bypassPermissions"
DEFAULT_PERMISSION_MODE = "auto"
PERMISSION_MODES = ("auto", "default", "acceptEdits", "plan", "dontAsk", BYPASS)
PERMISSION_CHOICES = PERMISSION_MODES + ("inherit",)

# Every wait the relay does (the spawn, the check-in, remote control, naming
# the thread) comes out of one budget. Claude Code's Bash tool gives a command
# 2 minutes by default; the default budget fits inside that with room to spare.
DEFAULT_MAX_WAIT = 105.0

# Session-identity variables a predecessor leaks into anything it spawns. A
# successor that inherits CLAUDE_CODE_SESSION_ID or the desktop host's
# messaging socket believes it is part of the predecessor; one that inherits
# CODEX_SANDBOX_NETWORK_DISABLED believes it has no network. Provider and auth
# settings (CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_OAUTH_TOKEN, ...) are kept.
_SCRUB_EXACT = frozenset((
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_EFFORT",
    "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR",
    "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
    "TMUX", "TMUX_PANE",
))
_CODEX_SESSION_VARS = ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")
_KEEP_CLAUDE_CODE = (
    "CLAUDE_CODE_USE_", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY",
    "CLAUDE_CODE_SKIP_", "CLAUDE_CODE_CLIENT_", "CLAUDE_CODE_MAX_",
    "CLAUDE_CODE_EXTRA_", "CLAUDE_CODE_PROXY",
)

# -------------------------------------------------------------------- paths


def claude_home(env=None):
    env = os.environ if env is None else env
    return env.get("CLAUDE_CONFIG_DIR") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".claude")


def codex_home(env=None):
    env = os.environ if env is None else env
    return env.get("CODEX_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".codex")


def codex_trusted_projects(env=None):
    """The project paths ~/.codex/config.toml marks trusted: `[projects."<path>"]`
    with `trust_level = "trusted"`, or the inline form under `[projects]`.
    Read-only; an empty set when the file is missing or unreadable."""
    flat = _core_module("tomlish").load(os.path.join(codex_home(env), "config.toml"))
    return {key[1] for key, value in flat.items()
            if len(key) == 3 and key[0] == "projects" and key[2] == "trust_level"
            and value == "trusted"}


def codex_project_trusted(repo, env=None):
    def norm(path):
        return os.path.normcase(os.path.normpath(path))
    trusted = {norm(t) for t in codex_trusted_projects(env)}
    return any(norm(p) in trusted for p in (repo, os.path.realpath(repo)))


def ledger_dir(env=None):
    env = os.environ if env is None else env
    home = env.get("LASTCALL_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".lastcall")
    return env.get(LEDGER_DIR_ENV) or os.path.join(os.path.expanduser(home), "relay")


def running_agent(env):
    """The agent running THIS process (the predecessor), or None: Claude Code
    exports CLAUDE_CODE_SESSION_ID / CLAUDECODE to its tools, Codex its
    CODEX_THREAD_ID / sandbox variables; a relay successor also carries
    LASTCALL_RELAY_AGENT."""
    if env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDECODE"):
        return "claude"
    if any(k in env for k in _CODEX_SESSION_VARS):
        return "codex"
    agent = env.get(AGENT_ENV)
    return agent if agent in AGENTS else None


def claude_slug(path):
    """Claude Code replaces EVERY non-alphanumeric character, not just '/'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def claude_transcript_path(repo, session_id, env=None):
    return os.path.join(claude_home(env), "projects", claude_slug(repo),
                        session_id + ".jsonl")


# ------------------------------------------------------------------ handoff


def pick_handoff(directory):
    """Newest .md in `directory` that is not a TEMPLATE; None when there is none.

    Newest by mtime, ties broken by name — a fresh git checkout gives every file
    the same mtime, and dated names then still sort the right way round."""
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    found = []
    for name in names:
        if not name.lower().endswith(".md") or name.upper().startswith("TEMPLATE"):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            found.append((os.path.getmtime(path), name, path))
    return max(found)[2] if found else None


def handoff_topic(path, limit=60):
    """The handoff's first heading, else its filename without the date."""
    topic = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh):
                if number > 40:
                    break
                match = re.match(r"\s{0,3}#{1,3}\s+(.+?)\s*#*\s*$", line)
                if match:
                    topic = match.group(1)
                    break
    except OSError:
        pass
    if not topic:
        stem = os.path.splitext(os.path.basename(path))[0]
        stem = re.sub(r"^\d{4}-?\d{2}-?\d{2}(?:[T_-]?\d{2}-?\d{2}(?:-?\d{2})?)?[-_ ]*", "", stem)
        topic = stem.replace("-", " ").replace("_", " ")
    topic = re.sub(r"\s+", " ", topic).strip()
    topic = re.sub(r"^(?:handoff|hand-off|handover)\b[\s:\-\u2013\u2014]*", "", topic,
                   flags=re.I).strip()
    if len(topic) > limit:
        topic = topic[:limit - 1].rstrip() + "\u2026"
    return topic


def successor_name(prefix, generation, topic=None):
    parts = [prefix, "handoff %d" % generation]
    if topic:
        parts.append(topic)
    return SEP.join(parts)


def tmux_safe(name):
    """tmux treats ':' and '.' as target separators."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "lastcall"


def build_prompt(handoff, retiring):
    prompt = "read %s and follow it." % handoff
    if retiring:
        prompt += (" The previous session is being retired automatically once"
                   " you have checked in; you do not need to stop it.")
    return prompt


# ------------------------------------------------------------------- ledger


def read_ledger(path):
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


def append_record(path, record):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    record = dict(record)
    record.setdefault("ts", time.time())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def valid_chain(chain):
    return bool(chain) and re.match(r"^[A-Za-z0-9._-]{1,80}$", chain) is not None


def new_chain_id(prefix="lastcall"):
    base = re.sub(r"[^A-Za-z0-9]+", "-", prefix).strip("-").lower()[:30] or "lastcall"
    return "%s-%s-%s" % (base, time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])


def next_generation(env, records):
    """One past both the predecessor's own generation and every generation
    the ledger has already spawned: a retry from the same predecessor must
    not reuse the generation of a successor that is still out there."""
    candidates = [1]
    try:
        candidates.append(int(env.get(GENERATION_ENV, "")) + 1)
    except ValueError:
        pass
    candidates += [r["generation"] + 1 for r in records
                   if r.get("event") == "spawn" and isinstance(r.get("generation"), int)]
    return max(candidates)


def new_nonce():
    return uuid.uuid4().hex


def checkin_record(payload, chain, generation, agent, handoff, via="hook", nonce=None):
    payload = payload if isinstance(payload, dict) else {}
    return {
        "event": "checkin", "via": via, "chain": chain, "generation": generation,
        "agent": agent, "handoff": handoff, "nonce": nonce,
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "cwd": payload.get("cwd"), "source": payload.get("source"),
        "model": payload.get("model"), "pid": os.getppid(),
    }


def checkin_from_hook(payload, env=None, agent=None, via="hook"):
    """For a SessionStart hook: append a check-in when this session was spawned
    by the relay (the LASTCALL_RELAY_* variables are set). Returns the record,
    or None when this is not a relay successor. Never raises.

    Idempotent per session: the injected --settings hook and the installed
    plugin hook both run in a Claude successor, and a resumed successor fires
    SessionStart again, so an existing check-in by the same session is
    returned instead of duplicated. A check-in by a DIFFERENT session for the
    same spawn (same nonce; same generation when there is no nonce) means
    this one merely inherited the variables (a verifier `codex exec` run by
    the successor, say): it is not the successor, so nothing is written and
    None is returned. A stale check-in from another attempt at the same
    generation carries another nonce and does not get in the way."""
    env = os.environ if env is None else env
    ledger, chain = env.get(LEDGER_ENV), env.get(CHAIN_ENV)
    if not ledger or not valid_chain(chain):
        return None
    try:
        generation = int(env.get(GENERATION_ENV, ""))
    except ValueError:
        generation = None
    nonce = env.get(NONCE_ENV) or None
    record = checkin_record(payload, chain, generation, agent or env.get(AGENT_ENV),
                            env.get(HANDOFF_ENV), via=via, nonce=nonce)
    session = record.get("session_id")
    try:
        for old in read_ledger(ledger):
            if (old.get("event") == "checkin" and old.get("chain") == chain
                    and old.get("generation") == generation and old.get("session_id")
                    and (old.get("nonce") or None) == nonce):
                if old.get("session_id") == session:
                    return old
                if session:
                    return None
        return append_record(ledger, record)
    except (OSError, TypeError, ValueError):
        return None


def successor_note(record):
    """The short SessionStart context for a relay successor."""
    note = "LAST CALL RELAY — you are generation %s of relay chain %s." % (
        record.get("generation") if record.get("generation") is not None else "?",
        record.get("chain"))
    if record.get("handoff"):
        note += " Read %s first and follow it." % record["handoff"]
    return note


def successor_session_start(payload, env=None, agent=None):
    """The installed plugin's SessionStart hook, for a session the relay
    spawned: check in (whatever mode started it — Codex exec, tmux or app
    successors have no injected hook) and return the note to inject, or None.
    The note is only for a fresh context; a resumed or compacted session
    already has it. Never raises."""
    try:
        payload = payload if isinstance(payload, dict) else {}
        record = checkin_from_hook(payload, env, agent, via="session-start")
        if record is None or payload.get("source") in ("resume", "compact"):
            return None
        return successor_note(record)
    except Exception:       # a hook must never break the session
        return None


# ----------------------------------------------------------- preconditions


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, encoding="utf-8", errors="replace")


def is_git_repo(repo):
    if not shutil.which("git"):
        return False
    return _git(repo, "rev-parse", "--git-dir").returncode == 0


def durability(repo, handoff, allow_dirty=False, allow_uncommitted=False, baseline=()):
    """(problems, warnings). Problems refuse the handover; warnings do not."""
    problems, warnings = [], []
    if not is_git_repo(repo):
        warnings.append("%s is not a git repository: cannot verify the handoff is "
                        "committed, so that check is SKIPPED" % repo)
        return problems, warnings
    if not allow_uncommitted:
        # Tracked AND identical to HEAD. `git status` alone says nothing about
        # a gitignored file, so a handoff that was never committed passed.
        tracked = _git(repo, "ls-files", "--error-unmatch", "--", handoff)
        if tracked.returncode != 0:
            problems.append("handoff is not committed: %s — commit it first%s" % (
                handoff, " (it is gitignored)" if _git(
                    repo, "check-ignore", "-q", "--", handoff).returncode == 0 else ""))
        else:
            changed = _git(repo, "diff", "--quiet", "HEAD", "--", handoff)
            if changed.returncode == 1:
                problems.append("handoff is not committed: %s — commit it first" % handoff)
            elif changed.returncode != 0:
                problems.append("cannot compare the handoff with HEAD — refusing to "
                                "assume it is committed")
    if not allow_dirty:
        status = _git(repo, "status", "--porcelain", "--ignore-submodules=dirty")
        dirty = [line for line in status.stdout.splitlines()
                 if line.strip() and line[3:] not in baseline]
        if dirty:
            problems.append("tree is dirty (%d path%s) — commit it, or pass --allow-dirty"
                            % (len(dirty), "" if len(dirty) == 1 else "s"))
    return problems, warnings


def newest_committed_handoff(repo, handoff_dir):
    """The newest committed handoff (by name — they are dated), or None."""
    listed = _git(repo, "ls-files", "--", handoff_dir)
    if listed.returncode != 0:
        return None
    names = [line for line in listed.stdout.splitlines()
             if line.lower().endswith(".md")
             and not os.path.basename(line).upper().startswith("TEMPLATE")]
    return max(names, key=os.path.basename) if names else None


# ------------------------------------------------------------------ config


def _core_module(name):
    """lastcall_core.<name> — relatively when imported as part of the package,
    else (run as a script, or loaded by path) from this file's own lib/."""
    import importlib
    if __package__:
        return importlib.import_module("." + name, __package__)
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("lastcall_core." + name)   # our own package


def _config_module():
    return _core_module("config")


def _relay_block(path):
    try:
        with open(path, encoding="utf-8") as fh:
            relay = (json.load(fh) or {}).get("relay") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return relay if isinstance(relay, dict) else {}


def load_relay_config(paths):
    """The `relay` block of the first readable file in `paths` (--config)."""
    for path in paths:
        if path and os.path.isfile(path):
            return path, _relay_block(path)
    return None, {}


def layered_relay_config(start, env, forget_project_env=False):
    """(files, project_dir, relay) through lastcall_core.config.load_config —
    ~/.lastcall/config.json, then the nearest .lastcall.json /
    .lastcall/config.json / .claude/lastcall.json / .codex/lastcall.json, then
    LASTCALL_RELAY (JSON) in the environment. Unlike the top-level keys, the
    `relay` blocks merge key by key, so a global "agent" survives a project
    that only sets "handoff_dir"."""
    config_mod = _config_module()
    env = dict(env)
    if forget_project_env:
        env.pop("CLAUDE_PROJECT_DIR", None)
    loaded = config_mod.load_config({"cwd": start}, env)
    relay = {}
    for path in loaded.get("_config_files") or []:
        relay.update(_relay_block(path))
    raw = env.get(config_mod.ENV_PREFIX + "RELAY", env.get(config_mod.ENV_PREFIX + "relay"))
    if raw is not None and isinstance(loaded.get("relay"), dict):
        relay.update(loaded["relay"])
    project = loaded.get("_config_path") and loaded.get("_project_dir")
    return list(loaded.get("_config_files") or []), project, relay


def resolve_repo(explicit, config, env, cwd, base=None):
    if explicit:
        return explicit
    if config.get("repo"):
        repo = os.path.expanduser(config["repo"])
        return repo if os.path.isabs(repo) or not base else os.path.join(base, repo)
    if env.get("CLAUDE_PROJECT_DIR"):
        return env["CLAUDE_PROJECT_DIR"]
    if shutil.which("git"):
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             encoding="utf-8", errors="replace")
        if top.returncode == 0 and top.stdout.strip():
            return top.stdout.strip()
    return cwd


# ---------------------------------------------------------------- commands


def successor_env(base, relay_env):
    env = {}
    for key, value in base.items():
        if key in _SCRUB_EXACT or key.startswith("LASTCALL_RELAY_"):
            continue
        if key.startswith("CLAUDE_CODE_") and not key.startswith(_KEEP_CLAUDE_CODE):
            continue
        env[key] = value
    env.update(relay_env)
    return env


def checkin_command(python_bin, ledger, chain, generation, agent, handoff, nonce=None):
    argv = [python_bin, os.path.abspath(__file__), "checkin",
            "--ledger", ledger, "--chain", chain,
            "--generation", str(generation), "--agent", agent, "--handoff", handoff]
    if nonce:
        argv += ["--nonce", nonce]
    return shlex.join(argv)


def claude_settings(relay_env, hook_command):
    """Inline --settings: the check-in hook, plus the relay variables as session
    env. The hook carries its arguments itself, so the handshake does not depend
    on the process environment surviving the hop into the background daemon."""
    return json.dumps({
        "env": relay_env,
        "hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": hook_command, "timeout": 15}]}]},
    }, sort_keys=True)


def claude_argv(opts, name, prompt, settings):
    """No --session-id: `claude --bg` assigns its own ("--bg manages the session
    id; ignoring --session-id") — the id comes back through the check-in."""
    argv = [opts.claude_bin, "--bg", "-n", name]
    if opts.remote_control:
        argv += ["--remote-control", name]
    if opts.model:
        argv += ["--model", opts.model]
    if opts.fallback_model:
        argv += ["--fallback-model", opts.fallback_model]
    if opts.skip_permissions:
        argv.append("--dangerously-skip-permissions")
    elif opts.permission_mode:
        argv += ["--permission-mode", opts.permission_mode]
    argv += ["--settings", settings, prompt]
    return argv


_BG_LINE = re.compile(r"backgrounded\W+([0-9a-f]{6,})\b", re.I)


def parse_bg_short(output):
    """The short id from `claude --bg` output ("backgrounded · c0815a37 · NAME"),
    else the first 8-hex word on a line that is not a warning; None if absent."""
    match = _BG_LINE.search(output or "")
    if match:
        return match.group(1)
    for line in (output or "").splitlines():
        if line.strip().lower().startswith("warning"):
            continue
        found = re.findall(r"\b([0-9a-f]{8})\b", line)
        if found:
            return found[0]
    return None


def claude_agents(claude_bin, env=None, timeout=15.0):
    """`claude agents --json` as a list of {id, sessionId, name, state, kind};
    [] when it cannot be read."""
    try:
        out = subprocess.run([claude_bin, "agents", "--json"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout, env=env, encoding="utf-8", errors="replace")
        data = json.loads(out.stdout or "null")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    if isinstance(data, dict):
        data = data.get("agents") or data.get("data") or []
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def bg_job_state(short, env=None):
    """~/.claude/jobs/<short>/state.json of a background session, or {}."""
    if not short:
        return {}
    try:
        with open(os.path.join(claude_home(env), "jobs", short, "state.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def codex_argv(opts, repo, prompt, have_git, mode=None):
    """`codex exec` or the interactive TUI (tmux mode)."""
    mode = mode or opts.codex_mode
    argv = [opts.codex_bin]
    if mode == "exec":
        argv += ["exec", "--json"]
    argv += ["-C", repo]
    if opts.model:
        argv += ["-m", opts.model]
    if opts.skip_permissions:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += ["-s", opts.codex_sandbox]
    if mode == "exec" and not have_git:
        argv.append("--skip-git-repo-check")
    if mode == "tmux":
        argv.append("--no-alt-screen")
    argv.append(prompt)
    return argv


def codex_app_sandbox(opts):
    return "danger-full-access" if opts.skip_permissions else opts.codex_sandbox


def codex_app_runner_argv(opts, repo, prompt, name, ledger, chain, generation, handoff,
                          nonce=None):
    """The detached runner that keeps `codex app-server` alive for the turn."""
    argv = [opts.python_bin, os.path.abspath(__file__), "codex-app-runner",
            "--ledger", ledger, "--chain", chain, "--generation", str(generation),
            "--handoff", handoff, "--repo", repo, "--name", name,
            "--sandbox", codex_app_sandbox(opts), "--approval", opts.codex_approval,
            "--codex-bin", opts.codex_bin, "--max-seconds", "%g" % opts.codex_app_max]
    if opts.model:
        argv += ["--model", opts.model]
    if opts.skip_permissions:
        argv.append("--auto-approve")
    if not getattr(opts, "name_thread", True):
        argv.append("--no-name")
    if nonce:
        argv += ["--nonce", nonce]
    argv += ["--prompt", prompt]
    return argv


def tmux_argv(tmux_bin, session, repo, inner, relay_env):
    shell = " ".join(["env"] + [shlex.quote("%s=%s" % kv) for kv in sorted(relay_env.items())]
                     + [shlex.join(inner)])
    return [tmux_bin, "new-session", "-d", "-s", session, "-c", repo, shell]


# ------------------------------------------------------ remote control proof


def remote_control_evidence(session_id, transcript=None, env=None, short=None):
    """Where Remote Control is proven connected for `session_id`, or None.

    A `claude --bg` session records it in ~/.claude/jobs/<short>/state.json
    (bridgeSessionId) and — observed on 2.1.281 — NOT as a bridge-session
    transcript entry, so the job state is checked first."""
    shorts = []
    for candidate in (short, session_id[:8] if session_id else None):
        if candidate and candidate not in shorts:
            shorts.append(candidate)
    for candidate in shorts:
        data = bg_job_state(candidate, env)
        if data.get("bridgeSessionId") and (not session_id or data.get("sessionId")
                                            in (None, session_id)):
            return "jobs/%s/state.json bridgeSessionId %s" % (candidate, data["bridgeSessionId"])
    if transcript and os.path.isfile(transcript):
        try:
            with open(transcript, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"bridge-session"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if (entry.get("type") == "bridge-session"
                            and entry.get("sessionId", session_id) == session_id):
                        return "transcript bridge-session %s" % entry.get("bridgeSessionId", "?")
        except OSError:
            pass
    if not session_id:
        return None
    for path in sorted(glob.glob(os.path.join(claude_home(env), "sessions", "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") == session_id \
                and data.get("bridgeSessionId"):
            return "sessions registry bridgeSessionId %s" % data["bridgeSessionId"]
    return None


# ----------------------------------------------------------------- codex


def find_codex_rollout(thread_id, env=None):
    root = os.path.join(codex_home(env), "sessions")
    hits = sorted(glob.glob(os.path.join(root, "*", "*", "*", "rollout-*%s*.jsonl" % thread_id)))
    return hits[0] if hits else None


def _session_meta(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = json.loads(fh.readline() or "{}")
    except (OSError, ValueError):
        return None
    if first.get("type") == "session_meta":
        meta = dict(first.get("payload") or {})
        if not meta.get("timestamp") and first.get("timestamp"):
            meta["timestamp"] = first["timestamp"]     # the line was written at creation
        return meta
    return None


def _epoch(value):
    """ISO-8601 (Z or offset, any fraction length) -> epoch seconds, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    match = re.match(r"^(.*?T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
    if match:
        text = "%s.%s%s" % (match.group(1), (match.group(2) or ".0")[1:7].ljust(6, "0"),
                            match.group(3))
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def rollout_created(meta):
    """When the session a rollout belongs to was CREATED (session_meta's
    timestamp), as epoch seconds; None when the rollout does not say."""
    return _epoch((meta or {}).get("timestamp"))


def scan_new_rollouts(repo, since, env=None, exclude=()):
    """Interactive rollouts whose session was CREATED at or after `since`
    and whose cwd is `repo` (tmux mode), oldest first.

    Creation, not mtime: the predecessor's own rollout — and any other thread
    active in the same repo — is written to all the time, so a fresh mtime
    proves nothing. A rollout that does not say when it was created cannot
    be proven new and is skipped. Ids in `exclude` (the predecessor's) are
    never a successor."""
    root = os.path.join(codex_home(env), "sessions")
    excluded = set(i for i in exclude if i)
    found = []
    for path in glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < since - 1:
                continue    # cheap pre-filter: not even written since the spawn
        except OSError:
            continue
        meta = _session_meta(path)
        if not meta or (meta.get("id") or meta.get("session_id")) in excluded:
            continue
        created = rollout_created(meta)
        if created is None or created < since - 0.5:
            continue
        if os.path.realpath(meta.get("cwd") or "") == os.path.realpath(repo) \
                and meta.get("source") != "exec" and not isinstance(meta.get("source"), dict):
            found.append((created, path, meta))
    return [(p, m) for _, p, m in sorted(found)]


def exec_thread_started(log_path):
    """thread_id from a `codex exec --json` log, or None."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"thread.started"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    return event["thread_id"]
    except OSError:
        pass
    return None


# Who the relay is to `codex app-server`. `name` becomes the thread's
# originator (rollout session_meta); the thread's source is decided by the
# server — the CLI's `codex app-server` always records "vscode", which is one
# of the interactive sources that the desktop app's sidebar, a default
# thread/list and `codex resume` show.
CLIENT_INFO = {"name": "lastcall-relay", "title": "Last Call relay", "version": "2"}

# Streaming chatter the runner never looks at; opting out keeps the pipe quiet.
QUIET_NOTIFICATIONS = (
    "item/agentMessage/delta", "item/reasoning/textDelta",
    "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
    "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
    "item/plan/delta", "mcpServer/startupStatus/updated",
    "thread/tokenUsage/updated", "account/rateLimits/updated",
)

APPROVAL_POLICIES = ("never", "on-request", "untrusted")
CODEX_MODES = ("app", "exec", "tmux")


class AppServerError(Exception):
    pass


def _rpc_error(error):
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error, sort_keys=True)
    return str(error)


class AppServerClient:
    """A minimal JSON-RPC client for `codex app-server` on stdio (one JSON
    object per line). A reader thread feeds a queue: select() on a buffered
    text pipe misses lines already sitting in Python's buffer."""

    def __init__(self, argv, cwd=None, env=None, stderr=subprocess.DEVNULL):
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=stderr,
                                     universal_newlines=True, encoding="utf-8",
                                     errors="replace", bufsize=1)
        self.inbox = queue.Queue()
        self.ids = 0
        self.gone = False
        reader = threading.Thread(target=self._read, name="app-server-reader")
        reader.daemon = True
        reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self.inbox.put(message)
        except (OSError, ValueError):
            pass
        self.inbox.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def respond(self, request_id, result=None, error=None):
        if error is not None:
            self.send({"id": request_id, "error": error})
        else:
            self.send({"id": request_id, "result": result if result is not None else {}})

    def next_message(self, timeout):
        """The next message, None on timeout; EOFError once the server is gone."""
        if self.gone:
            raise EOFError("codex app-server exited")
        try:
            message = self.inbox.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if message is None:
            self.gone = True
            raise EOFError("codex app-server exited")
        return message

    def request(self, method, params=None, timeout=30.0, on_message=None):
        """Send a request and return its result. Everything that arrives
        meanwhile (notifications, server->client requests) goes to on_message."""
        self.ids += 1
        request_id = self.ids
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AppServerError("no answer to %s within %gs" % (method, timeout))
            try:
                message = self.next_message(min(remaining, 1.0))
            except EOFError:
                raise AppServerError("codex app-server exited before answering %s" % method)
            if message is None:
                continue
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise AppServerError("%s failed: %s" % (method, _rpc_error(message["error"])))
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            if on_message:
                on_message(message)

    def initialize(self, timeout=30.0, on_message=None, quiet=True):
        params = {"clientInfo": dict(CLIENT_INFO)}
        if quiet:
            params["capabilities"] = {"optOutNotificationMethods": list(QUIET_NOTIFICATIONS)}
        result = self.request("initialize", params, timeout, on_message)
        self.notify("initialized")
        return result

    def close(self, timeout=10.0):
        """Closing stdin is the polite way to stop it; then escalate."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def codex_set_thread_name(codex_bin, thread_id, name, env=None, timeout=20.0):
    """Name a Codex thread through the app-server protocol (thread/name/set) —
    the same call the TUI's /rename makes. Returns (ok, detail)."""
    try:
        client = AppServerClient([codex_bin, "app-server"], env=env)
    except OSError as exc:
        return False, "could not start codex app-server: %s" % exc
    try:
        client.initialize(timeout)
        client.request("thread/name/set", {"threadId": thread_id, "name": name}, timeout)
        return True, "named via codex app-server"
    except AppServerError as exc:
        return False, str(exc)
    except (OSError, ValueError) as exc:
        return False, "codex app-server: %s" % exc
    finally:
        client.close(5)


def approval_answer(method, params, allow):
    """(result, error) for a server->client request. Nobody watches an
    unattended successor, so every prompt is answered at once: allowed only
    when the relay was told to skip permissions, declined otherwise."""
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "accept" if allow else "decline"}, None
    if method in ("execCommandApproval", "applyPatchApproval"):
        return {"decision": "approved" if allow else "denied"}, None
    if method == "item/permissions/requestApproval":
        granted = params.get("permissions") if allow else None
        return {"permissions": granted if isinstance(granted, dict) else {},
                "scope": "turn"}, None
    if method == "mcpServer/elicitation/request":
        return {"action": "decline"}, None
    if method == "item/tool/requestUserInput":
        return {"answers": {}}, None
    return None, {"code": -32601,
                  "message": "the Last Call relay runner cannot answer %s" % method}


class AppRunner:
    """`relay.py codex-app-runner`: start the successor through `codex
    app-server` and keep that server alive until the successor's first turn
    completes (or --max-seconds passes). Writes to the ledger:
      app-thread       thread/start succeeded (thread id, rollout path)
      checkin          turn/start was accepted — the successor is working
      app-failed       failed before the turn started (stage, error)
      app-runner-exit  the turn ended (status) or the runner gave up (reason)"""

    def __init__(self, args, out=None, env=None):
        self.a = args
        self.out = out or sys.stdout
        self.env = dict(os.environ if env is None else env)
        self.client = None
        self.thread_id = None
        self.turn_id = None
        self.finished = None
        self.answered = 0

    def log(self, message):
        print("%s runner: %s" % (time.strftime("%H:%M:%S"), message), file=self.out)
        self.out.flush()

    def record(self, event, **fields):
        record = {"event": event, "chain": self.a.chain, "generation": self.a.generation,
                  "agent": "codex", "runner_pid": os.getpid(), "nonce": self.a.nonce}
        record.update(fields)
        try:
            return append_record(self.a.ledger, record)
        except OSError as exc:
            self.log("could not write the ledger: %s" % exc)
            return record

    def fail(self, stage, error, **extra):
        self.log("FAILED at %s: %s" % (stage, error))
        self.record("app-failed", stage=stage, error=str(error), thread_id=self.thread_id, **extra)
        return 1

    def on_message(self, message):
        method = message.get("method")
        if not method:
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" in message:
            result, error = approval_answer(method, params, self.a.auto_approve)
            self.answered += 1
            self.log("answered %s: %s" % (method, json.dumps(result if error is None else error,
                                                             sort_keys=True)[:200]))
            try:
                self.client.respond(message["id"], result, error)
            except (OSError, ValueError):
                pass
            return
        if method == "turn/completed" and params.get("threadId") == self.thread_id:
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            if self.turn_id is None or turn.get("id") == self.turn_id:
                self.finished = turn
                self.log("turn/completed status=%s" % turn.get("status"))
        elif method in ("turn/started", "thread/status/changed", "thread/name/updated"):
            self.log("%s %s" % (method, json.dumps(params.get("status") or params.get("threadName")
                                                   or params.get("turn", {}).get("id"))))
        elif method == "error":
            self.log("error notification: %s" % json.dumps(params, sort_keys=True)[:400])

    def run(self):
        a = self.a
        try:
            self.client = AppServerClient([a.codex_bin, "app-server"], cwd=a.repo, env=self.env,
                                          stderr=None)
        except OSError as exc:
            return self.fail("spawn", "could not start codex app-server: %s" % exc)
        try:
            return self._drive()
        except (OSError, ValueError) as exc:
            return self.fail("runner", exc)
        finally:
            self.client.close(10)
            self.log("app-server stopped")

    def _archive(self):
        try:
            self.client.request("thread/archive", {"threadId": self.thread_id},
                                self.a.rpc_timeout, self.on_message)
            return True
        except (AppServerError, OSError, ValueError):
            return False

    def _drive(self):
        a, client = self.a, self.client
        stage = "initialize"
        try:
            client.initialize(a.rpc_timeout, self.on_message)
            stage = "thread/start"
            params = {"cwd": a.repo, "sandbox": a.sandbox, "approvalPolicy": a.approval}
            if a.model:
                params["model"] = a.model
            started = client.request("thread/start", params, a.rpc_timeout, self.on_message)
            thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
            self.thread_id = thread.get("id")
            if not self.thread_id:
                raise AppServerError("thread/start returned no thread id")
        except AppServerError as exc:
            return self.fail(stage, exc)
        path = thread.get("path")
        self.record("app-thread", thread_id=self.thread_id, source=thread.get("source"),
                    transcript_path=path)
        self.log("thread %s started (source %s)" % (self.thread_id, thread.get("source")))

        named, name_error = False, None
        if a.name_thread:
            try:
                client.request("thread/name/set", {"threadId": self.thread_id, "name": a.name},
                               a.rpc_timeout, self.on_message)
                named = True
            except AppServerError as exc:
                name_error = str(exc)
                self.log("thread/name/set failed: %s" % exc)
        try:
            turn = client.request("turn/start", {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": a.prompt}]},
                a.rpc_timeout, self.on_message).get("turn") or {}
        except AppServerError as exc:
            # An empty thread helps nobody: archive it (not delete) and let
            # the relay fall back to another mode.
            return self.fail("turn/start", exc, archived=self._archive())
        self.turn_id = turn.get("id") if isinstance(turn, dict) else None
        record = checkin_record(
            {"session_id": self.thread_id,
             "transcript_path": path or find_codex_rollout(self.thread_id, self.env),
             "cwd": thread.get("cwd") or a.repo, "source": thread.get("source"),
             "model": started.get("model") or a.model},
            a.chain, a.generation, "codex", a.handoff, via="app-server", nonce=a.nonce)
        record.update(pid=os.getpid(), runner_pid=os.getpid(), app_server_pid=client.proc.pid,
                      turn_id=self.turn_id, named=named, name_error=name_error,
                      max_seconds=a.max_seconds)
        try:
            append_record(a.ledger, record)
        except OSError as exc:
            self.log("could not write the check-in: %s" % exc)
        self.log("checked in; turn %s running" % self.turn_id)
        return self._await_turn()

    def _await_turn(self):
        a, client = self.a, self.client
        deadline = time.time() + a.max_seconds
        reason = None
        while self.finished is None:
            remaining = deadline - time.time()
            if remaining <= 0:
                reason = "max-duration"
                break
            try:
                message = client.next_message(min(remaining, 5.0))
            except EOFError:
                reason = "app-server exited"
                break
            if message is not None:
                self.on_message(message)
        if reason == "max-duration" and self.turn_id:
            self.log("still running after %gs — interrupting the turn" % a.max_seconds)
            try:
                client.request("turn/interrupt", {"threadId": self.thread_id,
                                                  "turnId": self.turn_id},
                               a.rpc_timeout, self.on_message)
                grace = time.time() + 30
                while self.finished is None and time.time() < grace:
                    message = client.next_message(1.0)
                    if message is not None:
                        self.on_message(message)
            except (AppServerError, EOFError, OSError, ValueError) as exc:
                self.log("turn/interrupt: %s" % exc)
        status = (self.finished or {}).get("status") or "unknown"
        self.record("app-runner-exit", thread_id=self.thread_id, turn_id=self.turn_id,
                    status=status, reason=reason or "turn-completed", answered=self.answered)
        return 0 if reason is None else 3


def codex_app_runner_main(argv, out=None):
    p = argparse.ArgumentParser(prog="relay.py codex-app-runner")
    p.add_argument("--ledger", required=True)
    p.add_argument("--chain", required=True)
    p.add_argument("--generation", type=int, required=True)
    p.add_argument("--nonce")
    p.add_argument("--handoff")
    p.add_argument("--repo", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--model")
    p.add_argument("--sandbox", default="workspace-write",
                   choices=("read-only", "workspace-write", "danger-full-access"))
    p.add_argument("--approval", default="never", choices=APPROVAL_POLICIES)
    p.add_argument("--auto-approve", action="store_true",
                   help="accept approval requests instead of declining them")
    p.add_argument("--no-name", dest="name_thread", action="store_false")
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--max-seconds", type=float, default=21600.0)
    p.add_argument("--rpc-timeout", type=float, default=60.0)
    return AppRunner(p.parse_args(argv), out).run()


# ------------------------------------------------------------ permissions


def resolve_permission_mode(cli_skip=None, cli_mode=None, config_skip=None,
                            config_mode=None, predecessor_mode=None):
    """(mode, why) for the successor, first match wins:

      1. flags:  --skip-permissions, else --permission-mode
      2. config: relay.skip_permissions true, else relay.permission_mode
      3. the predecessor runs in bypass mode -> bypass
      4. auto

    Within a level skipping permissions beats a mode, as it always has. An
    explicit "no" (--no-skip-permissions, or skip_permissions false in the
    config) rules out bypass from every level below it, the predecessor's
    included. "inherit" at a level skips the levels below it straight to 3."""
    refused = None      # the explicit "no" that rules bypass out below it
    ruled_out = False   # a lower level asked for bypass and was overruled
    for skip_on, skip_off, mode_label, skip, mode in (
            ("--skip-permissions", "--no-skip-permissions", "--permission-mode",
             cli_skip, cli_mode),
            ("from config: skip_permissions", "skip_permissions false in the config",
             "from config: permission_mode", config_skip, config_mode)):
        if mode is not None and mode not in PERMISSION_CHOICES:
            raise ValueError("unknown permission mode %r (%s)"
                             % (mode, "|".join(PERMISSION_CHOICES)))
        if skip is False and mode == BYPASS:
            raise ValueError("%s contradicts permission mode %s" % (skip_off, BYPASS))
        if not refused and skip is True:
            return BYPASS, skip_on
        if not refused and mode == BYPASS:
            return BYPASS, mode_label
        if skip is True or mode == BYPASS:
            ruled_out = True
        if skip is False:
            refused = refused or skip_off
        if mode == "inherit":
            break
        if mode is not None and mode != BYPASS:
            return mode, mode_label
    if predecessor_mode == BYPASS:
        if refused:
            return DEFAULT_PERMISSION_MODE, ("default; the predecessor's bypass is not "
                                             "inherited: %s" % refused)
        return BYPASS, "predecessor is in bypass mode"
    if ruled_out:
        return DEFAULT_PERMISSION_MODE, "default; bypass ruled out: %s" % refused
    return DEFAULT_PERMISSION_MODE, "default"


def predecessor_permission_mode(pred, env, cwd=None):
    """The permission mode the predecessor's own hooks last recorded in its
    Last Call state (every hook payload carries it), or None when unknown."""
    if not pred.get("session_id") or pred.get("agent") not in AGENTS:
        return None
    try:
        config = _config_module().load_config({"cwd": cwd or os.getcwd()}, env)
        return _core_module("state").recorded_permission_mode(
            config, pred["session_id"], pred["agent"], env)
    except Exception:  # noqa: BLE001 - unknown means the default, never a failure
        return None


def permission_summary(opts, agent, why):
    """The dry-run / plan line: the mode that was chosen, and why."""
    if agent == "claude":
        if opts.skip_permissions:
            return "bypass (%s) — no permission prompts" % why
        return "%s (%s)" % (opts.permission_mode, why)
    sandbox = codex_app_sandbox(opts) if opts.codex_mode == "app" else opts.codex_sandbox
    if opts.skip_permissions:
        return "bypass (%s) — sandbox %s, approvals bypassed" % (why, sandbox)
    line = "sandbox %s, approval %s" % (sandbox, opts.codex_approval)
    if why.startswith("default"):
        return "%s (%s)" % (line, why)
    return "%s (default; %s is a Claude mode — %s)" % (line, opts.permission_mode, why)


# ------------------------------------------------------------ predecessor


def _ps(pid, field):
    out = subprocess.run(["ps", "-o", "%s=" % field, "-p", str(pid)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         encoding="utf-8", errors="replace")
    return out.stdout.strip() if out.returncode == 0 else ""


def find_codex_ancestor(start=None, hops=30):
    """PID of the nearest ancestor that is the `codex` CLI (never app-server)."""
    pid = start or os.getppid()
    for _ in range(hops):
        if pid <= 1:
            return None
        command = _ps(pid, "command")
        if os.path.basename((command.split() or [""])[0]) == "codex":
            return None if "app-server" in command else pid
        try:
            pid = int(_ps(pid, "ppid") or 0)
        except ValueError:
            return None
    return None


def pid_ancestors(pid, hops=40):
    """`pid` and every ancestor of it, nearest first (stops at init)."""
    chain = []
    while pid and pid > 1 and len(chain) < hops and pid not in chain:
        chain.append(pid)
        try:
            pid = int(_ps(pid, "ppid") or 0)
        except ValueError:
            break
    return chain


def _relay_made_tmux(env, session):
    """Whether `session` is a tmux session an earlier relay created for this
    very successor (its spawn record on the chain's ledger names it)."""
    ledger = env.get(LEDGER_ENV)
    if not ledger or not session:
        return False
    return any(r.get("event") == "spawn" and r.get("tmux_session") == session
               and r.get("chain") == env.get(CHAIN_ENV)
               and str(r.get("generation")) == str(env.get(GENERATION_ENV))
               for r in read_ledger(ledger))


def detect_predecessor(env, agent=None, session_id=None, tmux_bin="tmux",
                       find_codex=find_codex_ancestor, ancestors=pid_ancestors):
    """Who is handing over. Everything is optional: no TTY, no tmux is fine.

    TMUX_PANE is only believed when that pane's process is an ancestor of the
    predecessor's: the variable is inherited by anything started from a tmux
    shell — a desktop app, an IDE — and retiring "the pane" of a session that
    does not live in it would close some unrelated part of the user's
    workspace."""
    pred = {"agent": agent, "session_id": session_id, "pid": None,
            "entrypoint": None, "kind": None, "tmux_session": None, "tmux_pane": None,
            "tmux_owned": False, "bg_short": None}
    if not pred["agent"]:
        if env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDECODE"):
            pred["agent"] = "claude"
        elif any(k in env for k in _CODEX_SESSION_VARS):
            pred["agent"] = "codex"
    if pred["agent"] == "claude":
        pred["session_id"] = pred["session_id"] or env.get("CLAUDE_CODE_SESSION_ID")
        pred["entrypoint"] = env.get("CLAUDE_CODE_ENTRYPOINT")
        try:
            pred["pid"] = int(env.get("CLAUDE_PID", ""))
        except ValueError:
            pass
        home = claude_home(env)
        if pred["pid"]:
            try:
                with open(os.path.join(home, "sessions", "%d.json" % pred["pid"]),
                          encoding="utf-8") as fh:
                    registry = json.load(fh)
                if registry.get("sessionId") == pred["session_id"] or not pred["session_id"]:
                    pred["session_id"] = registry.get("sessionId")
                    pred["kind"] = registry.get("kind")
                    pred["entrypoint"] = registry.get("entrypoint") or pred["entrypoint"]
                else:
                    pred["pid"] = None     # the pid is not this session's
            except (OSError, ValueError, AttributeError):
                pass
        sid = pred["session_id"]
        if sid:
            try:
                with open(os.path.join(home, "jobs", sid[:8], "state.json"),
                          encoding="utf-8") as fh:
                    if json.load(fh).get("sessionId") == sid:
                        pred["kind"], pred["bg_short"] = "background", sid[:8]
            except (OSError, ValueError, AttributeError):
                pass
    elif pred["agent"] == "codex":
        pred["session_id"] = pred["session_id"] or env.get("CODEX_THREAD_ID")
        pred["pid"] = find_codex()     # None under the desktop app / app-server
    pane = env.get("TMUX_PANE")
    if pane and pred["pid"] and shutil.which(tmux_bin):
        shown = subprocess.run([tmux_bin, "display-message", "-p", "-t", pane,
                                "#{pane_pid}\t#S"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               encoding="utf-8", errors="replace").stdout.strip()
        pane_pid, _tab, name = shown.partition("\t")
        try:
            pane_pid = int(pane_pid)
        except ValueError:
            pane_pid = None
        if pane_pid and name and pane_pid in ancestors(pred["pid"]):
            pred["tmux_pane"], pred["tmux_session"] = pane, name
            pred["tmux_owned"] = _relay_made_tmux(env, name)
    return pred


def plan_retirement(pred, claude_bin="claude", tmux_bin="tmux", find_codex=find_codex_ancestor):
    """How to retire the predecessor: {"method", "why", "argv"|"pid"}.

    What the app owns is ruled out first, for both agents: a Claude desktop /
    IDE session, a Codex thread with no `codex` CLI process above it (the
    desktop app or app-server). Only then tmux — the predecessor's own pane,
    or the whole session when an earlier relay created it — and signals."""
    if pred.get("bg_short"):
        return {"method": "claude-stop", "argv": [claude_bin, "stop", pred["bg_short"]],
                "why": "background Claude session %s" % pred["bg_short"]}
    entry = pred.get("entrypoint") or ""
    if pred.get("agent") == "claude" and entry and entry != "cli":
        return {"method": "none",
                "why": "predecessor is a %s session — the app owns that process, so the "
                       "relay will not kill it; close or archive it in the app" % entry}
    codex_pid = None
    if pred.get("agent") == "codex":
        codex_pid = pred.get("pid") or find_codex()
        if not codex_pid:
            return {"method": "none", "why": "no codex CLI process found above this one "
                    "(desktop app or app-server) — close the predecessor thread yourself"}
    if pred.get("tmux_session") and pred.get("tmux_owned"):
        return {"method": "tmux", "argv": [tmux_bin, "kill-session", "-t",
                                           "=" + pred["tmux_session"]],
                "why": "tmux session %s (created by the relay)" % pred["tmux_session"]}
    if pred.get("tmux_pane"):
        return {"method": "tmux", "argv": [tmux_bin, "kill-pane", "-t", pred["tmux_pane"]],
                "why": "tmux pane %s in session %s" % (pred["tmux_pane"],
                                                        pred.get("tmux_session") or "?")}
    if pred.get("agent") == "claude" and pred.get("pid") and pred.get("kind") == "interactive":
        return {"method": "signal", "pid": pred["pid"],
                "why": "Claude CLI process %d" % pred["pid"]}
    if codex_pid:
        return {"method": "signal", "pid": codex_pid, "why": "codex CLI process %d" % codex_pid}
    return {"method": "none", "why": "no predecessor to retire could be identified"}


# argv: plan, delay[, ledger, record]. The outcome — "retired" or
# "retire-failed" with the exit status and stderr — is appended to the ledger,
# because the relay itself has long exited by the time it is known.
_RETIRE_SCRIPT = r"""
import json, os, signal, subprocess, sys, time
plan = json.loads(sys.argv[1])
time.sleep(float(sys.argv[2]))
code, detail = 0, ""
if plan.get("argv"):
    try:
        done = subprocess.run(plan["argv"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        code, detail = done.returncode, done.stderr.decode("utf-8", "replace").strip()
    except OSError as exc:
        code, detail = -1, str(exc)
elif plan.get("pid"):
    try:
        os.kill(int(plan["pid"]), signal.SIGTERM)
    except OSError as exc:
        code, detail = -1, str(exc)
if len(sys.argv) > 3 and sys.argv[3]:
    record = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
    record.update(event="retired" if code == 0 else "retire-failed", exit=code,
                  detail=detail[:500], ts=time.time())
    try:
        with open(sys.argv[3], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass
"""


_LAUNCH_SCRIPT = r"""
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", sys.argv[1]] + sys.argv[2:],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
print(child.pid)
"""


def schedule_retirement(plan, delay, python_bin=None, ledger=None, record=None):
    """Detached and delayed: this very process may be running INSIDE the
    session being retired. A short-lived launcher starts the real worker in a
    new session (start_new_session is the setsid() syscall, which macOS has —
    unlike the `setsid` binary the bash relay depended on) and exits, so the
    worker is re-parented away from the predecessor. With `ledger`, the
    worker appends the outcome there. Returns the worker pid."""
    if plan.get("method") in (None, "none"):
        return None
    extra = [ledger, json.dumps(record or {})] if ledger else []
    launched = subprocess.run([python_bin or sys.executable, "-c", _LAUNCH_SCRIPT,
                               _RETIRE_SCRIPT, json.dumps(plan), str(delay)] + extra,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    try:
        return int(launched.stdout.strip())
    except ValueError:
        return None


def readiness(relay_config=None, which=shutil.which):
    """{check: ok} for doctor and setup: what a handover needs on this machine.
    The successor's agent defaults to whichever agent runs the predecessor, so
    either CLI will do unless the config pins one; tmux matters only for
    codex_mode "tmux"."""
    cfg = relay_config if isinstance(relay_config, dict) else {}
    agent = cfg.get("agent") if cfg.get("agent") in AGENTS else None
    checks = {"relay script present": os.path.isfile(os.path.abspath(__file__)),
              "git on PATH": bool(which("git"))}
    if agent:
        checks["%s CLI on PATH" % agent] = bool(which(agent))
    else:
        checks["claude or codex CLI on PATH"] = bool(which("claude") or which("codex"))
    if cfg.get("codex_mode") == "tmux" and agent in (None, "codex"):
        checks["tmux on PATH (codex_mode tmux)"] = bool(which("tmux"))
    return checks


# ------------------------------------------------------------------- run


class Relay:
    def __init__(self, opts, env=None, out=None):
        self.o = opts
        self.env = dict(os.environ if env is None else env)
        self.out = out or sys.stdout
        self.plan = {}

    def say(self, message=""):
        print(message, file=self.out)
        self.out.flush()

    def fail(self, code, message):
        self.say("ABORT(%d): %s" % (code, message))
        return code

    # -- plan ---------------------------------------------------------------

    def load_settings(self, cwd):
        """(config files, relay block, repo). Settings come from the layered
        Last Call config (see layered_relay_config); --config-dir names the
        directory to look from, and --config adds one file on top. A project
        whose config sits beside the repo rather than beside the session is
        still found once the repo is known."""
        o, env = self.o, self.env
        start = o.config_dir or cwd
        files, project, config = layered_relay_config(start, env,
                                                      forget_project_env=bool(o.config_dir))
        if o.config:
            path, extra = load_relay_config([o.config])
            if path:
                files.append(path)
                config.update(extra)
        repo = os.path.realpath(resolve_repo(o.repo, config, env, cwd, base=project))
        if not project and not o.config and not o.config_dir:
            more, beside, extra = layered_relay_config(repo, env, forget_project_env=True)
            if beside:      # `extra` layers global < that project < env again
                files, config = more, extra
        return files, config, repo

    def resolve(self):
        o, env = self.o, self.env
        files, config, repo = self.load_settings(os.getcwd())
        config_path = ", ".join(files) if files else None

        def pick(flag, key, default=None):
            return flag if flag is not None else config.get(key, default)

        detected = running_agent(env)
        if o.agent:
            self.agent_source = "--agent"
        elif config.get("agent"):
            o.agent, self.agent_source = config["agent"], "config"
        elif detected:
            o.agent, self.agent_source = detected, "same as the predecessor"
        else:
            o.agent, self.agent_source = "claude", "default"
        if o.agent not in AGENTS:
            raise ValueError("unknown agent %r (claude|codex)" % o.agent)
        o.handoff_dir = pick(o.handoff_dir, "handoff_dir", "docs/handoff")
        o.name_prefix = pick(o.name_prefix, "name_prefix") or os.path.basename(repo) or "lastcall"
        o.model = o.model if o.model is not None else config.get(
            "model" if o.agent == "claude" else "codex_model")
        o.fallback_model = pick(o.fallback_model, "fallback_model") if o.agent == "claude" else None
        o.remote_control = bool(pick(o.remote_control, "remote_control", True))
        # Permissions are settled in build(), once the predecessor is known.
        # "when set": a skip_permissions of false in the config is a choice too.
        skip = config.get("skip_permissions")
        mode = config.get("permission_mode")
        if mode is not None and mode not in PERMISSION_CHOICES:
            raise ValueError("unknown relay.permission_mode %r (%s)"
                             % (mode, "|".join(PERMISSION_CHOICES)))
        self.permission_inputs = dict(cli_skip=o.skip_permissions, cli_mode=o.permission_mode,
                                      config_skip=None if skip is None else bool(skip),
                                      config_mode=mode)
        # "kill_predecessor" is the name handoff.sh used; either one works.
        o.retire = bool(pick(o.retire, "retire_predecessor",
                             config.get("kill_predecessor", False)))
        o.kill_delay = float(pick(o.kill_delay, "kill_delay", 5.0))
        o.require_git = bool(o.require_git or config.get("require_git"))
        o.max_wait = float(pick(o.max_wait, "max_wait_seconds", DEFAULT_MAX_WAIT))
        for label, value in (("--timeout", o.timeout), ("--kill-delay", o.kill_delay),
                             ("--max-wait", o.max_wait)):
            if not 0 <= value <= 86400 or (label in ("--timeout", "--max-wait") and value <= 0):
                raise ValueError("%s must be between 0 and 86400 seconds, got %g"
                                 % (label, value))
        o.codex_sandbox = pick(o.codex_sandbox, "codex_sandbox", "workspace-write")
        o.codex_mode = pick(o.codex_mode, "codex_mode", "app")
        if o.codex_mode not in CODEX_MODES:
            raise ValueError("unknown codex mode %r (app|exec|tmux)" % o.codex_mode)
        o.codex_approval = pick(o.codex_approval, "codex_approval", "never")
        if o.codex_approval not in APPROVAL_POLICIES:
            raise ValueError("unknown codex approval policy %r (never|on-request|untrusted)"
                             % o.codex_approval)
        o.codex_app_max = float(pick(o.codex_app_max, "codex_app_max_seconds", 21600))
        baseline = pick(o.dirty_baseline, "dirty_baseline", "") or ""
        o.dirty_baseline = tuple(p.strip() for p in baseline.split(",") if p.strip()) \
            if isinstance(baseline, str) else tuple(baseline)
        return repo, config_path

    def build(self):
        o, env = self.o, self.env
        repo, config_path = self.resolve()
        if not os.path.isdir(repo):
            return self.fail(EXIT_PRECONDITION, "no such directory: %s" % repo)
        handoff_dir = o.handoff_dir if os.path.isabs(o.handoff_dir) \
            else os.path.join(repo, o.handoff_dir)
        handoff = os.path.abspath(o.handoff) if o.handoff else pick_handoff(handoff_dir)
        if not handoff:
            return self.fail(EXIT_PRECONDITION,
                             "no handoff files in %s — write one first" % handoff_dir)
        if not os.path.isfile(handoff):
            return self.fail(EXIT_PRECONDITION, "no such handoff: %s" % handoff)
        if o.require_git and not is_git_repo(repo):
            return self.fail(EXIT_PRECONDITION,
                             "not a git worktree: %s (--require-git is set)" % repo)
        problems, warnings = durability(repo, handoff, o.allow_dirty,
                                        o.allow_uncommitted, o.dirty_baseline)
        for warning in warnings:
            self.say("WARNING: " + warning)
        if problems:
            for problem in problems[1:]:
                self.say("refused: " + problem)
            if problems[0].startswith("handoff is not committed"):
                committed = newest_committed_handoff(repo, handoff_dir)
                if committed:
                    self.say("the newest committed handoff is %s" % committed)
                    self.say("to hand over with that one instead: --handoff %s"
                             % shlex.quote(os.path.join(repo, committed)))
            return self.fail(EXIT_PRECONDITION, problems[0])

        chain = o.chain or env.get(CHAIN_ENV)
        if not valid_chain(chain):
            chain = new_chain_id(o.name_prefix)
        ledger = os.path.join(o.ledger_dir or ledger_dir(env), chain + ".jsonl")
        generation = next_generation(env, read_ledger(ledger))
        topic = o.topic if o.topic is not None else handoff_topic(handoff)
        name = successor_name(o.name_prefix, generation, topic)
        pred = detect_predecessor(env, o.predecessor_agent, o.predecessor, o.tmux_bin)
        pred["permission_mode"] = predecessor_permission_mode(pred, env)
        o.permission_mode, self.permission_why = resolve_permission_mode(
            predecessor_mode=pred["permission_mode"], **self.permission_inputs)
        o.skip_permissions = o.permission_mode == BYPASS
        retirement = plan_retirement(pred, o.claude_bin, o.tmux_bin) if o.retire \
            else {"method": "none", "why": "not requested (pass --retire-predecessor)"}
        prompt = build_prompt(handoff, o.retire and retirement["method"] != "none")
        nonce = new_nonce()
        relay_env = {CHAIN_ENV: chain, GENERATION_ENV: str(generation), LEDGER_ENV: ledger,
                     HANDOFF_ENV: handoff, AGENT_ENV: o.agent, NONCE_ENV: nonce}
        have_git = is_git_repo(repo)
        plan = {"repo": repo, "config": config_path, "handoff": handoff, "chain": chain,
                "ledger": ledger, "generation": generation, "name": name, "agent": o.agent,
                "relay_env": relay_env, "predecessor": pred, "retirement": retirement,
                "prompt": prompt, "session_id": None, "log": None, "tmux_session": None,
                "nonce": nonce}
        if o.agent == "claude":
            hook = checkin_command(o.python_bin, ledger, chain, generation, "claude", handoff,
                                   nonce)
            plan.update(hook=hook, bg_short=None, transcript=None,
                        argv=claude_argv(o, name, prompt, claude_settings(relay_env, hook)))
        elif o.codex_mode == "tmux":
            session = tmux_safe(name)
            plan.update(tmux_session=session,
                        argv=tmux_argv(o.tmux_bin, session, repo,
                                       codex_argv(o, repo, prompt, have_git), relay_env))
        else:
            # One log per spawn attempt (the nonce), written from scratch: an
            # appended log from an earlier attempt would hand over ITS thread.
            logs = os.path.join(os.path.dirname(ledger), "%s-%d" % (chain, generation))
            exec_argv = codex_argv(o, repo, prompt, have_git, mode="exec")
            if o.codex_mode == "app":
                fallback_nonce = new_nonce()
                plan.update(argv=codex_app_runner_argv(o, repo, prompt, name, ledger, chain,
                                                       generation, handoff, nonce),
                            log="%s-%s-app.log" % (logs, nonce[:8]), fallback_argv=exec_argv,
                            fallback_log="%s-%s.log" % (logs, fallback_nonce[:8]),
                            fallback_nonce=fallback_nonce)
            else:
                plan.update(argv=exec_argv, log="%s-%s.log" % (logs, nonce[:8]))
        self.plan = plan
        return None

    def describe(self):
        p, o = self.plan, self.o
        self.say("relay — %s successor" % p["agent"])
        self.say("  agent:       %s (%s)" % (p["agent"], getattr(self, "agent_source", "--agent")))
        self.say("  repo:        %s" % p["repo"])
        self.say("  config:      %s" % (p["config"] or "<none found>"))
        self.say("  handoff:     %s" % p["handoff"])
        self.say("  name:        %s" % p["name"])
        self.say("  chain:       %s (generation %d)" % (p["chain"], p["generation"]))
        self.say("  ledger:      %s" % p["ledger"])
        if p["agent"] == "claude":
            self.say("  session-id:  assigned by `claude --bg`; taken from the check-in")
            self.say("  remote ctl:  %s" % ("on as %s" % p["name"] if o.remote_control else "off"))
            self.say("  check-in:    SessionStart hook -> %s" % p["hook"])
        elif o.codex_mode == "app":
            self.say("  log:         %s" % p["log"])
            self.say("  runner:      detached; drives `%s app-server` (JSON-RPC: initialize, "
                     "thread/start, thread/name/set, turn/start) and stays up until the turn "
                     "completes (max %gs)" % (o.codex_bin, o.codex_app_max))
            self.say("  visible:     Codex app sidebar and `codex resume` (source vscode)")
            self.say("  check-in:    the runner, once turn/start is accepted")
            self.say("  approvals:   %s, sandbox %s%s" % (
                o.codex_approval, codex_app_sandbox(o),
                "; any request is accepted" if o.skip_permissions
                else "; any request is declined"))
            self.say("  fallback:    %s  (if app mode fails before the turn starts)"
                     % shlex.join(p["fallback_argv"]))
            sandbox = codex_app_sandbox(o)
            if sandbox != "read-only" and not codex_project_trusted(p["repo"], self.env):
                self.say("NOTE: Codex will mark %s as a trusted project in %s (Codex does this "
                         "itself when the app-server starts a %s thread)"
                         % (os.path.realpath(p["repo"]),
                            os.path.join(codex_home(self.env), "config.toml"), sandbox))
        elif o.codex_mode == "exec":
            self.say("  log:         %s" % p["log"])
            self.say("  check-in:    thread.started on `codex exec --json` (hidden from the "
                     "Codex app and the default `codex resume` list)")
        else:
            self.say("  tmux:        %s" % p["tmux_session"])
            self.say("  check-in:    the plugin's SessionStart hook; without it, the one "
                     "rollout under %s CREATED after the spawn whose cwd is the repo "
                     "(never the predecessor's)" % os.path.join(codex_home(self.env), "sessions"))
        self.say("  permissions: %s" % permission_summary(o, p["agent"], self.permission_why))
        self.say("  model:       %s%s" % (o.model or "<default>",
                                         "  fallback: %s" % o.fallback_model
                                         if o.fallback_model else ""))
        self.say("  env:         %s" % " ".join("%s=%s" % (k, shlex.quote(v))
                                                for k, v in sorted(p["relay_env"].items())))
        self.say("  spawn:       %s" % shlex.join(p["argv"]))
        pred, ret = p["predecessor"], p["retirement"]
        self.say("  predecessor: %s" % (" ".join("%s=%s" % (k, v) for k, v in sorted(pred.items())
                                                 if v) or "<none detected>"))
        if ret["method"] == "none":
            self.say("  retire:      no — %s" % ret["why"])
        else:
            what = shlex.join(ret["argv"]) if ret.get("argv") else "kill -TERM %d" % ret["pid"]
            self.say("  retire:      after check-in, in %gs: %s  (%s)" % (o.kill_delay, what, ret["why"]))
        if pred.get("agent") == "codex" and self.env.get("CODEX_SANDBOX"):
            self.say("WARNING: running inside the Codex sandbox (%s): the successor inherits it."
                     " Run the relay with escalated permissions." % self.env["CODEX_SANDBOX"])

    # -- spawn & prove --------------------------------------------------------

    def worst_case(self):
        """The longest this run can take, in seconds: every wait it may do,
        capped by the --max-wait budget."""
        o = self.o
        if o.agent == "claude":
            total = o.spawn_timeout + o.timeout + (o.rc_timeout if o.remote_control else 0)
        else:
            total = o.timeout * (2 if o.codex_mode == "app" else 1) + 20
        return min(total, o.max_wait)

    def left(self, cap):
        """What remains of the budget, at most ``cap`` seconds."""
        deadline = getattr(self, "deadline", None)
        if deadline is None:
            return cap
        return max(0.0, min(cap, deadline - time.time()))

    def run(self):
        code = self.build()
        if code is not None:
            return code
        self.say("this can take up to %ds — run it with a tool timeout above that (Claude "
                 "Code's Bash tool defaults to 120s) or in the background; --max-wait "
                 "(relay.max_wait_seconds) sets the budget" % int(self.worst_case() + 0.999))
        self.deadline = time.time() + self.o.max_wait
        self.describe()
        if self.o.dry_run:
            self.say("dry run — nothing spawned")
            return EXIT_OK
        for binary in (self.plan["argv"][0],
                       self.o.codex_bin if self.o.agent == "codex" else None):
            if binary and not shutil.which(binary):
                return self.fail(EXIT_PRECONDITION, "not found on PATH: %s" % binary)
        if self.o.agent == "claude":
            return self.run_claude()
        return self.run_codex()

    def _spawn_record(self, **extra):
        p = self.plan
        record = {"event": "spawn", "chain": p["chain"], "generation": p["generation"],
                  "nonce": p["nonce"],
                  "agent": p["agent"], "name": p["name"], "handoff": p["handoff"],
                  "repo": p["repo"], "session_id": p["session_id"],
                  "predecessor": p["predecessor"].get("session_id")}
        record.update(extra)
        append_record(p["ledger"], record)

    def _wait(self, match, extra=None):
        """First ledger check-in of THIS spawn (its nonce) satisfying `match`,
        else what `extra` finds."""
        p = self.plan
        deadline = time.time() + self.left(self.o.timeout)
        while True:
            for record in read_ledger(p["ledger"]):
                if record.get("event") == "checkin" and record.get("chain") == p["chain"] \
                        and record.get("nonce") == p["nonce"] and match(record):
                    return record
            if extra:
                found = extra()
                if found is not None:
                    return found
            if time.time() >= deadline:
                return None
            time.sleep(self.o.poll)

    def run_claude(self):
        p, o = self.plan, self.o
        env = successor_env(self.env, p["relay_env"])
        generation = p["generation"]
        # Recorded BEFORE the spawn: the successor's hook can check in before
        # `claude --bg` has even returned. --bg picks the session id itself, so
        # the check-in is matched by chain + generation, and the id comes from it.
        self._spawn_record()
        spawn_timeout = max(1.0, self.left(o.spawn_timeout))
        try:
            spawned = subprocess.run(p["argv"], cwd=p["repo"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     timeout=spawn_timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return self.fail(EXIT_UNPROVEN, "`claude --bg` did not return in %ds" % spawn_timeout)
        output = spawned.stdout.strip()
        if spawned.returncode != 0:
            append_record(p["ledger"], {"event": "spawn-failed", "chain": p["chain"],
                                        "generation": generation,
                                        "exit": spawned.returncode})
        for line in output.splitlines()[:10]:
            self.say("  claude: %s" % line)
        if spawned.returncode != 0:
            if "not trusted" in output.lower():
                return self.fail(EXIT_PRECONDITION,
                                 "untrusted workspace — run `claude` once in %s and accept the "
                                 "trust prompt (the relay does not edit ~/.claude.json)" % p["repo"])
            return self.fail(EXIT_PRECONDITION, "`claude --bg` refused (exit %d)" % spawned.returncode)
        short = parse_bg_short(output)
        agent_entry = {}
        if not short:
            for entry in claude_agents(o.claude_bin, env):
                if entry.get("name") == p["name"] and entry.get("id"):
                    short, agent_entry = entry["id"], entry
        p["bg_short"] = short
        append_record(p["ledger"], {"event": "bg", "chain": p["chain"], "generation": generation,
                                    "bg_id": short})
        self.say("spawned: background session %s" % (short or "<id not printed>"))

        seen = {}

        def transcript_fallback():
            # The hook is the proof we want. A transcript without a hook
            # check-in means the session runs but the hook did not fire (e.g.
            # --settings hooks ignored) — accept it after a grace period, and
            # say so. The session id comes from the job state (or `claude
            # agents --json`), since --bg chose it.
            sid = bg_job_state(short, self.env).get("sessionId") or agent_entry.get("sessionId")
            if not sid and short and time.time() - seen.get("agents", 0) >= 5:
                seen["agents"] = time.time()
                for entry in claude_agents(o.claude_bin, env):
                    if entry.get("id") == short and entry.get("sessionId"):
                        agent_entry.update(entry)
                sid = agent_entry.get("sessionId")
            if not sid:
                return None
            transcript = claude_transcript_path(p["repo"], sid, self.env)
            if not os.path.isfile(transcript):
                return None
            seen.setdefault("at", time.time())
            if time.time() - seen["at"] < o.hook_grace:
                return None
            return append_record(p["ledger"], dict(checkin_record(
                {"session_id": sid, "transcript_path": transcript, "cwd": p["repo"]},
                p["chain"], generation, "claude", p["handoff"], via="transcript",
                nonce=p["nonce"])))

        record = self._wait(lambda r: r.get("agent") == "claude"
                            and r.get("generation") == generation, transcript_fallback)
        if record is None:
            if short:
                self.say("attach:  %s attach %s" % (o.claude_bin, short))
                self.say("logs:    %s logs %s" % (o.claude_bin, short))
            return self.fail(EXIT_UNPROVEN, "successor never checked in within %ds"
                             % min(o.timeout, o.max_wait))
        sid = record.get("session_id")
        if record.get("via") == "transcript":
            self.say("WARNING: no hook check-in — proved by the transcript existing instead")
        self.say("checked in: %s (via %s)" % (sid, record.get("via")))
        short = short or (sid[:8] if sid else None)
        transcript = record.get("transcript_path") or (
            claude_transcript_path(p["repo"], sid, self.env) if sid else None)
        p.update(session_id=sid, transcript=transcript, bg_short=short)

        rc_ok = True
        if o.remote_control:
            evidence = None
            rc_wait = self.left(o.rc_timeout)
            deadline = time.time() + rc_wait
            while True:
                evidence = remote_control_evidence(sid, transcript, self.env, short=short)
                if evidence or time.time() >= deadline:
                    break
                time.sleep(o.poll)
            if evidence:
                self.say("remote control: connected (%s)" % evidence)
            else:
                rc_ok = False
                self.say("remote control did NOT connect — no bridgeSessionId for %s after %ds"
                         % (sid, rc_wait))
        append_record(p["ledger"], {"event": "verified", "chain": p["chain"],
                                    "generation": generation, "session_id": sid,
                                    "bg_id": short,
                                    "remote_control": rc_ok if o.remote_control else None})
        if not rc_ok and o.require_remote_control:
            return self.fail(EXIT_UNPROVEN, "remote control required but not connected — "
                             "predecessor NOT retired")
        self.retire()
        self.say("OK — %s is up (%s attach %s)" % (p["name"], o.claude_bin, short or sid))
        return EXIT_OK

    def _mine(self, record, event):
        p = self.plan
        return (record.get("event") == event and record.get("chain") == p["chain"]
                and record.get("generation") == p["generation"]
                and record.get("nonce") == p["nonce"])

    def _codex_app(self, env):
        """The checked-in record; "fallback" when app mode failed before the
        successor's turn started; an exit code when it failed after that."""
        p, o = self.plan, self.o
        os.makedirs(os.path.dirname(p["log"]), exist_ok=True)
        self._spawn_record(log=p["log"], mode="app")
        try:
            with open(p["log"], "wb") as log:
                proc = subprocess.Popen(p["argv"], cwd=p["repo"], env=env,
                                        stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            self.say("WARNING: could not start the app-server runner: %s" % exc)
            return "fallback"
        self.say("spawned: codex app-server runner pid %d (log %s)" % (proc.pid, p["log"]))

        def is_checkin(record):
            return record.get("agent") == "codex" and record.get("via") == "app-server" \
                and record.get("generation") == p["generation"]

        def check():
            records = read_ledger(p["ledger"])
            for record in records:
                if self._mine(record, "app-failed"):
                    return {"app_failed": record}
            if proc.poll() is not None:
                # It may have checked in and finished between two polls.
                for record in records:
                    if self._mine(record, "checkin") and is_checkin(record):
                        return record
                return {"died": proc.returncode}
            return None

        record = self._wait(is_checkin, check)
        if record is not None and "app_failed" not in record and "died" not in record:
            return record
        thread = [r for r in read_ledger(p["ledger"]) if self._mine(r, "app-thread")]
        if record is None:
            if thread:
                self.say("log: %s" % p["log"])
                return self.fail(EXIT_UNPROVEN, "app-server thread %s started but the runner never "
                                 "checked in within %ds" % (thread[-1].get("thread_id"), o.timeout))
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            why = "no thread within %ds" % o.timeout
        elif "app_failed" in record:
            failed = record["app_failed"]
            why = "%s: %s" % (failed.get("stage"), failed.get("error"))
            if failed.get("thread_id"):
                why += " (empty thread %s %s)" % (failed["thread_id"], "archived"
                                                  if failed.get("archived") else "NOT archived")
        else:
            why = "runner exited %s before the turn started" % record["died"]
        append_record(p["ledger"], {"event": "fallback", "chain": p["chain"],
                                    "generation": p["generation"], "from": "app", "to": "exec",
                                    "why": why})
        self.say("WARNING: codex app-server mode failed (%s); log %s" % (why, p["log"]))
        self.say("WARNING: falling back to `codex exec` — this successor will NOT show in the "
                 "Codex app sidebar or the default `codex resume` list "
                 "(`codex resume --include-non-interactive` finds it)")
        return "fallback"

    def _codex_exec(self, env, argv, log_path):
        p = self.plan
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._spawn_record(log=log_path, mode="exec")
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(argv, cwd=p["repo"], env=env,
                                    stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        self.say("spawned: codex exec pid %d (log %s)" % (proc.pid, log_path))

        def check():
            thread = exec_thread_started(log_path)
            if thread:
                return append_record(p["ledger"], checkin_record(
                    {"session_id": thread, "cwd": p["repo"],
                     "transcript_path": find_codex_rollout(thread, self.env)},
                    p["chain"], p["generation"], "codex", p["handoff"], via="exec-json",
                    nonce=p["nonce"]))
            if proc.poll() is not None:
                return {"died": proc.returncode}
            return None

        record = self._wait(lambda r: r.get("agent") == "codex"
                            and r.get("generation") == p["generation"], check)
        if record is None or "died" in record:
            self.say("log: %s" % log_path)
            return self.fail(EXIT_UNPROVEN, "successor exited before starting a thread (%s)"
                             % record["died"] if record else
                             "successor never checked in within %ds" % self.o.timeout)
        return record

    def _codex_tmux(self, env):
        p, o = self.plan, self.o
        started = time.time()
        self._spawn_record(tmux_session=p["tmux_session"], mode="tmux")
        spawned = subprocess.run(p["argv"], cwd=p["repo"], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 encoding="utf-8", errors="replace")
        if spawned.returncode != 0:
            return self.fail(EXIT_PRECONDITION, "tmux could not start %s: %s"
                             % (p["tmux_session"], spawned.stdout.strip()))
        self.say("spawned: tmux session %s" % p["tmux_session"])
        predecessor = p["predecessor"].get("session_id")
        seen = {}

        def check():
            # The plugin's SessionStart check-in (matched by _wait before this
            # runs) is the proof we want. A scanned rollout is only a stand-in
            # when that hook is not installed: it must be unambiguous, not the
            # predecessor's, and survive a grace period in which the hook
            # could still check in.
            found = scan_new_rollouts(p["repo"], started, self.env, exclude=(predecessor,))
            if len(found) > 1:
                seen.pop("at", None)
                seen.pop("path", None)
                if not seen.get("warned"):
                    seen["warned"] = True
                    self.say("WARNING: %d new Codex threads in %s since the spawn — waiting for "
                             "the successor's own check-in instead of guessing" % (
                                 len(found), p["repo"]))
            elif found:
                path, meta = found[0]
                if seen.get("path") != path:
                    seen.update(path=path, at=time.time())
                elif time.time() - seen["at"] >= o.hook_grace:
                    return append_record(p["ledger"], checkin_record(
                        {"session_id": meta.get("id") or meta.get("session_id"),
                         "transcript_path": path, "cwd": meta.get("cwd")},
                        p["chain"], p["generation"], "codex", p["handoff"], via="rollout-scan",
                        nonce=p["nonce"]))
            dead = subprocess.run([o.tmux_bin, "display-message", "-p", "-t",
                                   "=%s:0.0" % p["tmux_session"], "#{pane_dead}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  encoding="utf-8", errors="replace")
            if dead.returncode != 0 or dead.stdout.strip() == "1":
                return {"died": "pane"}
            return None

        record = self._wait(lambda r: r.get("agent") == "codex"
                            and r.get("generation") == p["generation"], check)
        if record is None or "died" in record:
            self.say("attach: %s attach -t %s" % (o.tmux_bin, p["tmux_session"]))
            return self.fail(EXIT_UNPROVEN, "successor exited before starting a thread (pane)"
                             if record else "successor never checked in within %ds" % o.timeout)
        return record

    def run_codex(self):
        p, o = self.plan, self.o
        env = successor_env(self.env, p["relay_env"])
        mode = o.codex_mode
        if mode == "app":
            record = self._codex_app(env)
            if record == "fallback":
                # A new spawn, so a new nonce: a late check-in from the failed
                # app attempt must not pass for the exec successor.
                mode = "exec"
                p["nonce"] = p["fallback_nonce"]
                p["relay_env"][NONCE_ENV] = p["nonce"]
                env = successor_env(self.env, p["relay_env"])
                record = self._codex_exec(env, p["fallback_argv"], p["fallback_log"])
        elif mode == "exec":
            record = self._codex_exec(env, p["argv"], p["log"])
        else:
            record = self._codex_tmux(env)
        if isinstance(record, int):
            return record
        thread = record.get("session_id")
        self.say("checked in: thread %s (via %s)" % (thread, record.get("via")))
        if mode == "app":
            if record.get("named"):
                self.say("thread name: set — by the app-server runner (thread/name/set)")
            elif o.name_thread:
                self.say("thread name: NOT set — %s" % (record.get("name_error") or "unknown"))
            self.say("visible: Codex app sidebar and `codex resume` (source %s); runner pid %s "
                     "keeps `codex app-server` up until the turn completes (max %gs)"
                     % (record.get("source"), record.get("runner_pid"), o.codex_app_max))
        elif thread and o.name_thread:
            budget = self.left(20.0)
            if budget >= 1:
                ok, detail = codex_set_thread_name(o.codex_bin, thread, p["name"], env,
                                                   timeout=budget)
            else:
                ok, detail = False, "the --max-wait budget is spent"
            self.say("thread name: %s — %s" % ("set" if ok else "NOT set", detail))
        append_record(p["ledger"], {"event": "verified", "chain": p["chain"],
                                    "generation": p["generation"], "session_id": thread,
                                    "mode": mode})
        self.retire()
        self.say("OK — %s is up (codex resume %s)" % (p["name"], thread))
        return EXIT_OK

    def retire(self):
        p, ret = self.plan, self.plan["retirement"]
        if ret["method"] == "none":
            if self.o.retire:
                self.say("predecessor kept: %s" % ret["why"])
            return
        pid = schedule_retirement(ret, self.o.kill_delay, self.o.python_bin, ledger=p["ledger"],
                                  record={"chain": p["chain"], "generation": p["generation"],
                                          "method": ret["method"], "why": ret["why"]})
        append_record(p["ledger"], {"event": "retire", "chain": p["chain"],
                                    "generation": p["generation"], "method": ret["method"],
                                    "why": ret["why"], "scheduler_pid": pid})
        self.say("retiring predecessor in %gs: %s" % (self.o.kill_delay, ret["why"]))
        self.say("retirement outcome will be logged to %s" % p["ledger"])


# -------------------------------------------------------------------- CLI


def _bool_flag(parser, name, dest, help_on, help_off):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--" + name, dest=dest, action="store_true", default=None, help=help_on)
    group.add_argument("--no-" + name, dest=dest, action="store_false", help=help_off)


class _Parser(argparse.ArgumentParser):
    """A bad flag is a precondition failure (exit 1, nothing spawned) — not
    argparse's exit 2, which here means "spawned but never checked in"."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_PRECONDITION, "%s: error: %s\n" % (self.prog, message))


def parser(prog="relay.py"):
    p = _Parser(prog=prog, description=__doc__.split("\n\n")[0])
    p.add_argument("--agent", choices=AGENTS,
                   help="successor agent. Default: relay.agent in the config, else the "
                        "agent running this session (claude -> claude, codex -> codex), "
                        "else claude. Pass the other one to hand over across agents")
    p.add_argument("--repo", help="directory to hand over")
    p.add_argument("--config", help="one more lastcall.json whose relay block wins over "
                                    "the layered config")
    p.add_argument("--config-dir", help="find the project config from this directory "
                                        "instead of the working directory")
    p.add_argument("--handoff", help="use this handoff instead of the newest")
    p.add_argument("--handoff-dir", help="where handoffs live, repo-relative (docs/handoff)")
    p.add_argument("--name-prefix", help="successor name prefix (default: repo name)")
    p.add_argument("--topic", help="topic for the name (default: the handoff's heading)")
    p.add_argument("--model", help="successor model")
    p.add_argument("--fallback-model", help="claude only: comma-separated fallbacks")
    _bool_flag(p, "remote-control", "remote_control",
               "claude: --remote-control NAME (default on)", "claude: no remote control")
    p.add_argument("--require-remote-control", action="store_true",
                   help="exit 2 (and retire nothing) if remote control does not connect")
    _bool_flag(p, "skip-permissions", "skip_permissions",
               "run the successor in bypass mode, without permission prompts (dangerous; "
               "codex: full access, approvals bypassed)",
               "never bypass permissions, even when this session runs in bypass mode")
    p.add_argument("--permission-mode", choices=PERMISSION_CHOICES,
                   help="the successor's permission mode. Default (inherit): auto, or bypass "
                        "when this session runs in bypass mode. bypassPermissions is the same "
                        "as --skip-permissions; codex successors use only that one")
    p.add_argument("--codex-mode", choices=CODEX_MODES,
                   help="codex: `app` (default) runs it through `codex app-server` so it shows "
                        "in the Codex app and `codex resume`; `exec` a hidden `codex exec "
                        "--json`; `tmux` the TUI in tmux")
    p.add_argument("--codex-sandbox", help="codex: sandbox (default workspace-write)")
    p.add_argument("--codex-approval", choices=APPROVAL_POLICIES,
                   help="codex app mode: approvalPolicy (default never); any approval request "
                        "is declined, or accepted with --skip-permissions")
    p.add_argument("--codex-app-max-seconds", dest="codex_app_max", type=float,
                   help="codex app mode: interrupt the turn and stop the runner after this "
                        "long (default 21600)")
    p.add_argument("--no-name-thread", dest="name_thread", action="store_false",
                   help="codex: do not name the thread via codex app-server")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--retire-predecessor", "--kill-predecessor", dest="retire",
                       action="store_true", default=None,
                       help="retire this session once the successor checked in "
                            "(config: retire_predecessor, or kill_predecessor)")
    group.add_argument("--no-retire-predecessor", "--no-kill-predecessor", dest="retire",
                       action="store_false", help="keep this session (default)")
    p.add_argument("--predecessor", help="predecessor session id (default: from env)")
    p.add_argument("--predecessor-agent", choices=AGENTS)
    p.add_argument("--kill-delay", type=float,
                   help="seconds between the check-in and the retirement (default 5)")
    p.add_argument("--require-git", action="store_true", default=None,
                   help="refuse unless the repo is a git worktree (default: warn and skip "
                        "the committed-handoff check)")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-uncommitted", action="store_true",
                   help="hand over even though the handoff is not committed")
    p.add_argument("--dirty-baseline", help="comma-separated paths that may be dirty")
    p.add_argument("--chain", help="relay chain id (default: inherited, else new)")
    p.add_argument("--ledger-dir", help="default ~/.lastcall/relay")
    p.add_argument("--timeout", type=float, default=60.0, help="check-in timeout (s)")
    p.add_argument("--rc-timeout", type=float, default=20.0, help="remote-control wait (s)")
    p.add_argument("--hook-grace", type=float, default=20.0,
                   help="claude: how long a transcript may exist without a hook check-in")
    p.add_argument("--spawn-timeout", type=float, default=25.0)
    p.add_argument("--max-wait", type=float,
                   help="total seconds for every wait together — spawn, check-in, remote "
                        "control (default %g, config relay.max_wait_seconds): under Claude "
                        "Code's 2-minute Bash tool timeout" % DEFAULT_MAX_WAIT)
    p.add_argument("--poll", type=float, default=1.0, help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true", help="print the plan, spawn nothing")
    p.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    p.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    p.add_argument("--tmux-bin", default=os.environ.get("TMUX_BIN", "tmux"))
    p.add_argument("--python-bin", default=sys.executable or "python3")
    return p


def checkin_main(argv, stdin=None):
    """`relay.py checkin ...` — the successor's SessionStart hook. Always exit 0
    and print nothing: SessionStart stdout would land in the model's context."""
    p = argparse.ArgumentParser(prog="relay.py checkin")
    for flag in ("--ledger", "--chain", "--agent", "--handoff", "--generation", "--nonce"):
        p.add_argument(flag)
    try:
        args, _ = p.parse_known_args(argv)
        try:
            payload = json.loads((stdin or sys.stdin).read(1 << 20) or "{}")
        except ValueError:
            payload = {}
        env = dict(os.environ)
        for key, value in ((LEDGER_ENV, args.ledger), (CHAIN_ENV, args.chain),
                           (AGENT_ENV, args.agent), (HANDOFF_ENV, args.handoff),
                           (GENERATION_ENV, args.generation), (NONCE_ENV, args.nonce)):
            if value is not None:
                env[key] = value
        checkin_from_hook(payload, env)
    except (Exception, SystemExit):     # a hook must never break the session
        pass
    return 0


def _utf8_stdio():
    """UTF-8 on the standard streams: a redirected stream on Windows is
    cp1252, which cannot encode SEP, and the check-in hook reads the agent's
    UTF-8 JSON. Inline, not lastcall_core.textio: this file also runs as a
    standalone script."""
    streams = [sys.stdout, sys.stderr]
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            streams.append(sys.stdin)
    except (AttributeError, ValueError, OSError):
        pass
    for stream in streams:
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv=None, prog="relay.py"):
    _utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["checkin"]:
        return checkin_main(argv[1:])
    if argv[:1] == ["codex-app-runner"]:
        return codex_app_runner_main(argv[1:])
    opts = parser(prog).parse_args(argv)
    try:
        return Relay(opts).run()
    except ValueError as exc:
        print("ABORT(1): %s" % exc)
        return EXIT_PRECONDITION


if __name__ == "__main__":
    sys.exit(main())
