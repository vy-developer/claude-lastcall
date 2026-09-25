#!/usr/bin/env python3
"""Relay v2 — hand a session over to a fresh Claude Code or Codex successor.

Stdlib only, Python 3.9+, POSIX. Nothing here assumes a terminal: the
predecessor may be a CLI session in tmux, a `claude --bg` job, or a desktop-app
session with no TTY at all.

What it does, in order, refusing at the first failure:

  1. picks the handoff (newest non-TEMPLATE .md in the handoff dir) and, when
     the repo is a git worktree, refuses unless it is committed;
  2. names the successor "<prefix> · handoff N · <topic>";
  3. spawns it detached:
       claude  `claude --bg -n NAME --remote-control NAME --session-id UUID
                [--model M] [--fallback-model F] --settings JSON PROMPT`
       codex   `codex exec --json -C REPO [-m M] -s workspace-write PROMPT`
               (or an interactive `codex` inside `tmux new-session -d`);
  4. waits for the successor to CHECK IN on a ledger
     (~/.lastcall/relay/<chain>.jsonl) instead of guessing a transcript path:
       claude  its SessionStart hook (injected via --settings) runs
               `relay.py checkin ...` which appends session_id + transcript_path
       codex   the `thread.started` event on `codex exec --json`, or a new
               rollout whose cwd is the repo (tmux mode);
  5. claude: verifies Remote Control actually connected (a `bridge-session`
     transcript entry, or bridgeSessionId in ~/.claude/jobs|sessions) and says
     "remote control did NOT connect" when it did not;
     codex: names the thread through `codex app-server` (thread/name/set);
  6. optionally retires the predecessor — `claude stop <id>` for a background
     job, `tmux kill-session` for a tmux pane, a delayed SIGTERM for a plain
     CLI process — from a detached Python child (no `setsid` binary needed).
     A desktop-app session is never killed; the relay says so instead.

Exit codes: 0 successor checked in; 1 precondition failure, nothing spawned;
2 spawned but it never checked in (or remote control was required and absent).

`relay.py --dry-run` prints every command without running any of them.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
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
LEDGER_DIR_ENV = "LASTCALL_RELAY_DIR"

AGENTS = ("claude", "codex")
SEP = " · "

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


def ledger_dir(env=None):
    env = os.environ if env is None else env
    return env.get(LEDGER_DIR_ENV) or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".lastcall", "relay")


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
    """The predecessor's own generation + 1; else one past the ledger's newest."""
    try:
        return int(env.get(GENERATION_ENV, "")) + 1
    except ValueError:
        pass
    spawned = [r.get("generation") for r in records
               if r.get("event") == "spawn" and isinstance(r.get("generation"), int)]
    return max(spawned) + 1 if spawned else 1


def checkin_record(payload, chain, generation, agent, handoff, via="hook"):
    payload = payload if isinstance(payload, dict) else {}
    return {
        "event": "checkin", "via": via, "chain": chain, "generation": generation,
        "agent": agent, "handoff": handoff,
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "cwd": payload.get("cwd"), "source": payload.get("source"),
        "model": payload.get("model"), "pid": os.getppid(),
    }


def checkin_from_hook(payload, env=None, agent=None):
    """For a SessionStart hook: append a check-in when this session was spawned
    by the relay (the LASTCALL_RELAY_* variables are set). Returns the record,
    or None when this is not a relay successor. Never raises."""
    env = os.environ if env is None else env
    ledger, chain = env.get(LEDGER_ENV), env.get(CHAIN_ENV)
    if not ledger or not valid_chain(chain):
        return None
    try:
        generation = int(env.get(GENERATION_ENV, ""))
    except ValueError:
        generation = None
    try:
        return append_record(ledger, checkin_record(
            payload, chain, generation, agent or env.get(AGENT_ENV),
            env.get(HANDOFF_ENV)))
    except OSError:
        return None


# ----------------------------------------------------------- preconditions


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


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
        status = _git(repo, "status", "--porcelain", "--", handoff)
        if status.returncode != 0:
            problems.append("cannot read git status for the handoff — refusing to "
                            "assume it is committed")
        elif status.stdout.strip():
            problems.append("handoff is not committed: %s — commit it first" % handoff)
    if not allow_dirty:
        status = _git(repo, "status", "--porcelain", "--ignore-submodules=dirty")
        dirty = [line for line in status.stdout.splitlines()
                 if line.strip() and line[3:] not in baseline]
        if dirty:
            problems.append("tree is dirty (%d path%s) — commit it, or pass --allow-dirty"
                            % (len(dirty), "" if len(dirty) == 1 else "s"))
    return problems, warnings


# ------------------------------------------------------------------ config


def load_relay_config(paths):
    """The `relay` block of the first readable .claude/lastcall.json."""
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                relay = (json.load(fh) or {}).get("relay") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(relay, dict):
            return path, relay
    return None, {}


def resolve_repo(explicit, config, env, cwd):
    if explicit:
        return explicit
    if config.get("repo"):
        return config["repo"]
    if env.get("CLAUDE_PROJECT_DIR"):
        return env["CLAUDE_PROJECT_DIR"]
    if shutil.which("git"):
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True)
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


def checkin_command(python_bin, ledger, chain, generation, agent, handoff):
    return shlex.join([python_bin, os.path.abspath(__file__), "checkin",
                       "--ledger", ledger, "--chain", chain,
                       "--generation", str(generation), "--agent", agent,
                       "--handoff", handoff])


def claude_settings(relay_env, hook_command):
    """Inline --settings: the check-in hook, plus the relay variables as session
    env. The hook carries its arguments itself, so the handshake does not depend
    on the process environment surviving the hop into the background daemon."""
    return json.dumps({
        "env": relay_env,
        "hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": hook_command, "timeout": 15}]}]},
    }, sort_keys=True)


def claude_argv(opts, name, session_id, prompt, settings):
    argv = [opts.claude_bin, "--bg", "-n", name]
    if opts.remote_control:
        argv += ["--remote-control", name]
    argv += ["--session-id", session_id]
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


def codex_argv(opts, repo, prompt, have_git):
    """`codex exec` (default) or the interactive TUI (tmux mode)."""
    argv = [opts.codex_bin]
    if opts.codex_mode == "exec":
        argv += ["exec", "--json"]
    argv += ["-C", repo]
    if opts.model:
        argv += ["-m", opts.model]
    if opts.skip_permissions:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += ["-s", opts.codex_sandbox]
    if opts.codex_mode == "exec" and not have_git:
        argv.append("--skip-git-repo-check")
    if opts.codex_mode == "tmux":
        argv.append("--no-alt-screen")
    argv.append(prompt)
    return argv


def tmux_argv(tmux_bin, session, repo, inner, relay_env):
    shell = " ".join(["env"] + [shlex.quote("%s=%s" % kv) for kv in sorted(relay_env.items())]
                     + [shlex.join(inner)])
    return [tmux_bin, "new-session", "-d", "-s", session, "-c", repo, shell]


# ------------------------------------------------------ remote control proof


def remote_control_evidence(session_id, transcript=None, env=None):
    """Where Remote Control is proven connected for `session_id`, or None."""
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
    home = claude_home(env)
    state = os.path.join(home, "jobs", session_id[:8], "state.json")
    candidates = [state] + sorted(glob.glob(os.path.join(home, "sessions", "*.json")))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") == session_id \
                and data.get("bridgeSessionId"):
            return "%s bridgeSessionId %s" % (os.path.basename(os.path.dirname(path))
                                               if path == state else "sessions registry",
                                               data["bridgeSessionId"])
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
        return first.get("payload") or {}
    return None


def scan_new_rollouts(repo, since, env=None):
    """Rollouts created after `since` whose session cwd is `repo` (tmux mode)."""
    root = os.path.join(codex_home(env), "sessions")
    found = []
    for path in glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < since - 1:
                continue
        except OSError:
            continue
        meta = _session_meta(path)
        if meta and os.path.realpath(meta.get("cwd") or "") == os.path.realpath(repo) \
                and meta.get("source") != "exec" and not isinstance(meta.get("source"), dict):
            found.append((os.path.getmtime(path), path, meta))
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


def codex_set_thread_name(codex_bin, thread_id, name, env=None, timeout=20.0):
    """Name a Codex thread through the app-server protocol (thread/name/set) —
    the same call the TUI's /rename makes. Returns (ok, detail)."""
    try:
        proc = subprocess.Popen([codex_bin, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, bufsize=1, env=env)
    except OSError as exc:
        return False, "could not start codex app-server: %s" % exc
    deadline = time.time() + timeout

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def reply(request_id):
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    return None
                continue
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
        return None

    try:
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "lastcall-relay", "version": "2"}}})
        if reply(1) is None:
            return False, "codex app-server did not answer initialize"
        send({"method": "initialized"})
        send({"id": 2, "method": "thread/name/set",
              "params": {"threadId": thread_id, "name": name}})
        answer = reply(2)
        if answer is None:
            return False, "codex app-server did not answer thread/name/set"
        if "error" in answer:
            return False, "thread/name/set failed: %s" % answer["error"]
        return True, "named via codex app-server"
    except (OSError, ValueError) as exc:
        return False, "codex app-server: %s" % exc
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


# ------------------------------------------------------------ predecessor


def _ps(pid, field):
    out = subprocess.run(["ps", "-o", "%s=" % field, "-p", str(pid)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         universal_newlines=True)
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


def detect_predecessor(env, agent=None, session_id=None, tmux_bin="tmux"):
    """Who is handing over. Everything is optional: no TTY, no tmux is fine."""
    pred = {"agent": agent, "session_id": session_id, "pid": None,
            "entrypoint": None, "kind": None, "tmux_session": None, "bg_short": None}
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
                with open(os.path.join(home, "sessions", "%d.json" % pred["pid"])) as fh:
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
                with open(os.path.join(home, "jobs", sid[:8], "state.json")) as fh:
                    if json.load(fh).get("sessionId") == sid:
                        pred["kind"], pred["bg_short"] = "background", sid[:8]
            except (OSError, ValueError, AttributeError):
                pass
    elif pred["agent"] == "codex":
        pred["session_id"] = pred["session_id"] or env.get("CODEX_THREAD_ID")
    if env.get("TMUX_PANE") and shutil.which(tmux_bin):
        name = subprocess.run([tmux_bin, "display-message", "-p", "-t", env["TMUX_PANE"], "#S"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True).stdout.strip()
        pred["tmux_session"] = name or None
    return pred


def plan_retirement(pred, claude_bin="claude", tmux_bin="tmux", find_codex=find_codex_ancestor):
    """How to retire the predecessor: {"method", "why", "argv"|"pid"}."""
    if pred.get("bg_short"):
        return {"method": "claude-stop", "argv": [claude_bin, "stop", pred["bg_short"]],
                "why": "background Claude session %s" % pred["bg_short"]}
    entry = pred.get("entrypoint") or ""
    if pred.get("agent") == "claude" and entry and entry != "cli":
        return {"method": "none",
                "why": "predecessor is a %s session — the app owns that process, so the "
                       "relay will not kill it; close or archive it in the app" % entry}
    if pred.get("tmux_session"):
        return {"method": "tmux", "argv": [tmux_bin, "kill-session", "-t",
                                           "=" + pred["tmux_session"]],
                "why": "tmux session %s" % pred["tmux_session"]}
    if pred.get("agent") == "claude" and pred.get("pid") and pred.get("kind") == "interactive":
        return {"method": "signal", "pid": pred["pid"],
                "why": "Claude CLI process %d" % pred["pid"]}
    if pred.get("agent") == "codex":
        pid = find_codex()
        if pid:
            return {"method": "signal", "pid": pid, "why": "codex CLI process %d" % pid}
        return {"method": "none", "why": "no codex CLI process found above this one "
                "(desktop app or app-server) — close the predecessor thread yourself"}
    return {"method": "none", "why": "no predecessor to retire could be identified"}


_RETIRE_SCRIPT = r"""
import json, os, signal, subprocess, sys, time
plan = json.loads(sys.argv[1])
time.sleep(float(sys.argv[2]))
if plan.get("argv"):
    subprocess.call(plan["argv"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
elif plan.get("pid"):
    try:
        os.kill(int(plan["pid"]), signal.SIGTERM)
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


def schedule_retirement(plan, delay, python_bin=None):
    """Detached and delayed: this very process may be running INSIDE the
    session being retired. A short-lived launcher starts the real worker in a
    new session (start_new_session is the setsid() syscall, which macOS has —
    unlike the `setsid` binary the bash relay depended on) and exits, so the
    worker is re-parented away from the predecessor. Returns the worker pid."""
    if plan.get("method") in (None, "none"):
        return None
    launched = subprocess.run([python_bin or sys.executable, "-c", _LAUNCH_SCRIPT,
                               _RETIRE_SCRIPT, json.dumps(plan), str(delay)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, universal_newlines=True)
    try:
        return int(launched.stdout.strip())
    except ValueError:
        return None


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

    def resolve(self):
        o, env = self.o, self.env
        cwd = os.getcwd()
        early = [o.config,
                 env.get("CLAUDE_PROJECT_DIR") and os.path.join(
                     env["CLAUDE_PROJECT_DIR"], ".claude", "lastcall.json"),
                 os.path.join(cwd, ".claude", "lastcall.json")]
        config_path, config = load_relay_config(early)
        repo = os.path.realpath(resolve_repo(o.repo, config, env, cwd))
        if not config_path:
            config_path, config = load_relay_config(
                [os.path.join(repo, ".claude", "lastcall.json")])

        def pick(flag, key, default=None):
            return flag if flag is not None else config.get(key, default)

        o.agent = pick(o.agent, "agent", "claude")
        if o.agent not in AGENTS:
            raise ValueError("unknown agent %r (claude|codex)" % o.agent)
        o.handoff_dir = pick(o.handoff_dir, "handoff_dir", "docs/handoff")
        o.name_prefix = pick(o.name_prefix, "name_prefix") or os.path.basename(repo) or "lastcall"
        o.model = o.model if o.model is not None else config.get(
            "model" if o.agent == "claude" else "codex_model")
        o.fallback_model = pick(o.fallback_model, "fallback_model") if o.agent == "claude" else None
        o.remote_control = bool(pick(o.remote_control, "remote_control", True))
        o.skip_permissions = bool(pick(o.skip_permissions, "skip_permissions", False))
        o.retire = bool(pick(o.retire, "kill_predecessor", False))
        o.codex_sandbox = pick(o.codex_sandbox, "codex_sandbox", "workspace-write")
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
        problems, warnings = durability(repo, handoff, o.allow_dirty,
                                        o.allow_uncommitted, o.dirty_baseline)
        for warning in warnings:
            self.say("WARNING: " + warning)
        if problems:
            for problem in problems[1:]:
                self.say("refused: " + problem)
            return self.fail(EXIT_PRECONDITION, problems[0])

        chain = o.chain or env.get(CHAIN_ENV)
        if not valid_chain(chain):
            chain = new_chain_id(o.name_prefix)
        ledger = os.path.join(o.ledger_dir or ledger_dir(env), chain + ".jsonl")
        generation = next_generation(env, read_ledger(ledger))
        topic = o.topic if o.topic is not None else handoff_topic(handoff)
        name = successor_name(o.name_prefix, generation, topic)
        pred = detect_predecessor(env, o.predecessor_agent, o.predecessor, o.tmux_bin)
        retirement = plan_retirement(pred, o.claude_bin, o.tmux_bin) if o.retire \
            else {"method": "none", "why": "not requested (pass --retire-predecessor)"}
        prompt = build_prompt(handoff, o.retire and retirement["method"] != "none")
        relay_env = {CHAIN_ENV: chain, GENERATION_ENV: str(generation), LEDGER_ENV: ledger,
                     HANDOFF_ENV: handoff, AGENT_ENV: o.agent}
        have_git = is_git_repo(repo)
        plan = {"repo": repo, "config": config_path, "handoff": handoff, "chain": chain,
                "ledger": ledger, "generation": generation, "name": name, "agent": o.agent,
                "relay_env": relay_env, "predecessor": pred, "retirement": retirement,
                "prompt": prompt, "session_id": None, "log": None, "tmux_session": None}
        if o.agent == "claude":
            sid = str(uuid.uuid4())
            hook = checkin_command(o.python_bin, ledger, chain, generation, "claude", handoff)
            plan.update(session_id=sid, hook=hook,
                        transcript=claude_transcript_path(repo, sid, env),
                        argv=claude_argv(o, name, sid, prompt, claude_settings(relay_env, hook)))
        else:
            inner = codex_argv(o, repo, prompt, have_git)
            if o.codex_mode == "tmux":
                session = tmux_safe(name)
                plan.update(tmux_session=session,
                            argv=tmux_argv(o.tmux_bin, session, repo, inner, relay_env))
            else:
                plan.update(argv=inner, log=os.path.join(os.path.dirname(ledger),
                                                         "%s-%d.log" % (chain, generation)))
        self.plan = plan
        return None

    def describe(self):
        p, o = self.plan, self.o
        self.say("relay v2 — %s successor" % p["agent"])
        self.say("  repo:        %s" % p["repo"])
        self.say("  config:      %s" % (p["config"] or "<none found>"))
        self.say("  handoff:     %s" % p["handoff"])
        self.say("  name:        %s" % p["name"])
        self.say("  chain:       %s (generation %d)" % (p["chain"], p["generation"]))
        self.say("  ledger:      %s" % p["ledger"])
        if p["agent"] == "claude":
            self.say("  session-id:  %s" % p["session_id"])
            self.say("  transcript:  %s" % p["transcript"])
            self.say("  remote ctl:  %s" % ("on as %s" % p["name"] if o.remote_control else "off"))
            self.say("  check-in:    SessionStart hook -> %s" % p["hook"])
        elif o.codex_mode == "exec":
            self.say("  log:         %s" % p["log"])
            self.say("  check-in:    thread.started on `codex exec --json`")
        else:
            self.say("  tmux:        %s" % p["tmux_session"])
            self.say("  check-in:    new rollout under %s whose cwd is the repo"
                     % os.path.join(codex_home(self.env), "sessions"))
        self.say("  permissions: %s" % ("SKIPPED (unattended)" if o.skip_permissions
                                        else (o.permission_mode or "normal")
                                        if p["agent"] == "claude" else o.codex_sandbox))
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

    def run(self):
        code = self.build()
        if code is not None:
            return code
        self.describe()
        if self.o.dry_run:
            self.say("dry run — nothing spawned")
            return EXIT_OK
        binary = self.plan["argv"][0]
        if not shutil.which(binary):
            return self.fail(EXIT_PRECONDITION, "not found on PATH: %s" % binary)
        if self.o.agent == "claude":
            return self.run_claude()
        return self.run_codex()

    def _spawn_record(self, **extra):
        p = self.plan
        record = {"event": "spawn", "chain": p["chain"], "generation": p["generation"],
                  "agent": p["agent"], "name": p["name"], "handoff": p["handoff"],
                  "repo": p["repo"], "session_id": p["session_id"],
                  "predecessor": p["predecessor"].get("session_id")}
        record.update(extra)
        append_record(p["ledger"], record)

    def _wait(self, match, extra=None):
        """First ledger check-in satisfying `match`, else what `extra` finds."""
        p = self.plan
        deadline = time.time() + self.o.timeout
        while True:
            for record in read_ledger(p["ledger"]):
                if record.get("event") == "checkin" and record.get("chain") == p["chain"] \
                        and match(record):
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
        # Recorded BEFORE the spawn: the successor's hook can check in before
        # `claude --bg` has even returned.
        self._spawn_record(bg_id=p["session_id"][:8])
        try:
            spawned = subprocess.run(p["argv"], cwd=p["repo"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     universal_newlines=True, timeout=o.spawn_timeout)
        except subprocess.TimeoutExpired:
            return self.fail(EXIT_UNPROVEN, "`claude --bg` did not return in %ds" % o.spawn_timeout)
        output = spawned.stdout.strip()
        if spawned.returncode != 0:
            append_record(p["ledger"], {"event": "spawn-failed", "chain": p["chain"],
                                        "generation": p["generation"],
                                        "exit": spawned.returncode})
        for line in output.splitlines()[:10]:
            self.say("  claude: %s" % line)
        if spawned.returncode != 0:
            if "not trusted" in output.lower():
                return self.fail(EXIT_PRECONDITION,
                                 "untrusted workspace — run `claude` once in %s and accept the "
                                 "trust prompt (the relay does not edit ~/.claude.json)" % p["repo"])
            return self.fail(EXIT_PRECONDITION, "`claude --bg` refused (exit %d)" % spawned.returncode)
        sid = p["session_id"]
        shorts = re.findall(r"\b([0-9a-f]{8})\b", output)
        short = sid[:8] if sid[:8] in shorts or not shorts else shorts[0]
        self.say("spawned: background session %s" % short)

        seen = {}

        def transcript_fallback():
            # The hook is the proof we want. A transcript without a hook
            # check-in means the session runs but the hook did not fire (e.g.
            # --settings hooks ignored) — accept it after a grace period, and
            # say so.
            if not os.path.isfile(p["transcript"]):
                return None
            seen.setdefault("at", time.time())
            if time.time() - seen["at"] < o.hook_grace:
                return None
            return append_record(p["ledger"], dict(checkin_record(
                {"session_id": sid, "transcript_path": p["transcript"], "cwd": p["repo"]},
                p["chain"], p["generation"], "claude", p["handoff"], via="transcript")))

        record = self._wait(lambda r: r.get("session_id") == sid, transcript_fallback)
        if record is None:
            self.say("attach:  %s attach %s" % (o.claude_bin, short))
            self.say("logs:    %s logs %s" % (o.claude_bin, short))
            return self.fail(EXIT_UNPROVEN, "successor never checked in within %ds" % o.timeout)
        if record.get("via") == "transcript":
            self.say("WARNING: no hook check-in — proved by the transcript existing instead")
        self.say("checked in: %s (via %s)" % (sid, record.get("via")))
        transcript = record.get("transcript_path") or p["transcript"]

        rc_ok = True
        if o.remote_control:
            evidence = None
            deadline = time.time() + o.rc_timeout
            while True:
                evidence = remote_control_evidence(sid, transcript, self.env)
                if evidence or time.time() >= deadline:
                    break
                time.sleep(o.poll)
            if evidence:
                self.say("remote control: connected (%s)" % evidence)
            else:
                rc_ok = False
                self.say("remote control did NOT connect — no bridge-session for %s after %ds"
                         % (sid, o.rc_timeout))
        append_record(p["ledger"], {"event": "verified", "chain": p["chain"],
                                    "generation": p["generation"], "session_id": sid,
                                    "remote_control": rc_ok if o.remote_control else None})
        if not rc_ok and o.require_remote_control:
            return self.fail(EXIT_UNPROVEN, "remote control required but not connected — "
                             "predecessor NOT retired")
        self.retire()
        self.say("OK — %s is up (%s attach %s)" % (p["name"], o.claude_bin, short))
        return EXIT_OK

    def run_codex(self):
        p, o = self.plan, self.o
        env = successor_env(self.env, p["relay_env"])
        started = time.time()
        if o.codex_mode == "exec":
            os.makedirs(os.path.dirname(p["log"]), exist_ok=True)
            self._spawn_record(log=p["log"])
            with open(p["log"], "ab") as log:
                proc = subprocess.Popen(p["argv"], cwd=p["repo"], env=env,
                                        stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            self.say("spawned: codex exec pid %d (log %s)" % (proc.pid, p["log"]))

            def check():
                thread = exec_thread_started(p["log"])
                if thread:
                    return append_record(p["ledger"], checkin_record(
                        {"session_id": thread, "cwd": p["repo"],
                         "transcript_path": find_codex_rollout(thread, self.env)},
                        p["chain"], p["generation"], "codex", p["handoff"], via="exec-json"))
                if proc.poll() is not None:
                    return {"died": proc.returncode}
                return None
        else:
            self._spawn_record(tmux_session=p["tmux_session"])
            spawned = subprocess.run(p["argv"], cwd=p["repo"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     universal_newlines=True)
            if spawned.returncode != 0:
                return self.fail(EXIT_PRECONDITION, "tmux could not start %s: %s"
                                 % (p["tmux_session"], spawned.stdout.strip()))
            self.say("spawned: tmux session %s" % p["tmux_session"])

            def check():
                for path, meta in scan_new_rollouts(p["repo"], started, self.env):
                    return append_record(p["ledger"], checkin_record(
                        {"session_id": meta.get("id") or meta.get("session_id"),
                         "transcript_path": path, "cwd": meta.get("cwd")},
                        p["chain"], p["generation"], "codex", p["handoff"], via="rollout-scan"))
                dead = subprocess.run([o.tmux_bin, "display-message", "-p", "-t",
                                       "=%s:0.0" % p["tmux_session"], "#{pane_dead}"],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      universal_newlines=True)
                if dead.returncode != 0 or dead.stdout.strip() == "1":
                    return {"died": "pane"}
                return None

        record = self._wait(lambda r: r.get("agent") == "codex"
                            and r.get("generation") == p["generation"], check)
        if record is None or "died" in record:
            if p["log"]:
                self.say("log: %s" % p["log"])
            if p["tmux_session"]:
                self.say("attach: %s attach -t %s" % (o.tmux_bin, p["tmux_session"]))
            why = ("successor exited before starting a thread (%s)" % record["died"]
                   if record else "successor never checked in within %ds" % o.timeout)
            return self.fail(EXIT_UNPROVEN, why)
        thread = record.get("session_id")
        self.say("checked in: thread %s (via %s)" % (thread, record.get("via")))
        if thread and o.name_thread:
            ok, detail = codex_set_thread_name(o.codex_bin, thread, p["name"], env)
            self.say("thread name: %s — %s" % ("set" if ok else "NOT set", detail))
        append_record(p["ledger"], {"event": "verified", "chain": p["chain"],
                                    "generation": p["generation"], "session_id": thread})
        self.retire()
        self.say("OK — %s is up (codex resume %s)" % (p["name"], thread))
        return EXIT_OK

    def retire(self):
        p, ret = self.plan, self.plan["retirement"]
        if ret["method"] == "none":
            if self.o.retire:
                self.say("predecessor kept: %s" % ret["why"])
            return
        pid = schedule_retirement(ret, self.o.kill_delay, self.o.python_bin)
        append_record(p["ledger"], {"event": "retire", "chain": p["chain"],
                                    "generation": p["generation"], "method": ret["method"],
                                    "why": ret["why"], "scheduler_pid": pid})
        self.say("retiring predecessor in %gs: %s" % (self.o.kill_delay, ret["why"]))


# -------------------------------------------------------------------- CLI


def _bool_flag(parser, name, dest, help_on, help_off):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--" + name, dest=dest, action="store_true", default=None, help=help_on)
    group.add_argument("--no-" + name, dest=dest, action="store_false", help=help_off)


def parser():
    p = argparse.ArgumentParser(prog="relay.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--agent", choices=AGENTS, help="successor agent (default claude)")
    p.add_argument("--repo", help="directory to hand over")
    p.add_argument("--config", help="lastcall.json to read the relay block from")
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
               "run the successor without permission prompts (dangerous)",
               "keep permission prompts on")
    p.add_argument("--permission-mode", help="claude: --permission-mode for the successor")
    p.add_argument("--codex-mode", choices=("exec", "tmux"), default="exec",
                   help="codex: detached `codex exec --json` (default) or TUI in tmux")
    p.add_argument("--codex-sandbox", help="codex: -s value (default workspace-write)")
    p.add_argument("--no-name-thread", dest="name_thread", action="store_false",
                   help="codex: do not name the thread via codex app-server")
    _bool_flag(p, "retire-predecessor", "retire",
               "retire this session once the successor checked in",
               "keep this session (default)")
    p.add_argument("--predecessor", help="predecessor session id (default: from env)")
    p.add_argument("--predecessor-agent", choices=AGENTS)
    p.add_argument("--kill-delay", type=float, default=5.0)
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-uncommitted", action="store_true",
                   help="hand over even though the handoff is not committed")
    p.add_argument("--dirty-baseline", help="comma-separated paths that may be dirty")
    p.add_argument("--chain", help="relay chain id (default: inherited, else new)")
    p.add_argument("--ledger-dir", help="default ~/.lastcall/relay")
    p.add_argument("--timeout", type=float, default=180.0, help="check-in timeout (s)")
    p.add_argument("--rc-timeout", type=float, default=45.0, help="remote-control wait (s)")
    p.add_argument("--hook-grace", type=float, default=20.0,
                   help="claude: how long a transcript may exist without a hook check-in")
    p.add_argument("--spawn-timeout", type=float, default=60.0)
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
    for flag in ("--ledger", "--chain", "--agent", "--handoff", "--generation"):
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
                           (GENERATION_ENV, args.generation)):
            if value is not None:
                env[key] = value
        checkin_from_hook(payload, env)
    except (Exception, SystemExit):     # a hook must never break the session
        pass
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["checkin"]:
        return checkin_main(argv[1:])
    opts = parser().parse_args(argv)
    try:
        return Relay(opts).run()
    except ValueError as exc:
        print("ABORT(1): %s" % exc)
        return EXIT_PRECONDITION


if __name__ == "__main__":
    sys.exit(main())
