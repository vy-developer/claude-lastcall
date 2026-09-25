#!/usr/bin/env python3
"""Tests for the relay (plugins/lastcall/lib/lastcall_core/relay.py).

`claude`, `codex` and `tmux` are fake executables on PATH, HOME is a temp dir,
and every session variable of whatever session runs the suite is scrubbed —
nothing here spawns a real session or can retire a real one.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "plugins", "lastcall", "lib")
RELAY = os.path.join(LIB, "lastcall_core", "relay.py")
# Loaded by path, not as `lastcall.relay`: test_lastcall.py puts scripts/ on
# sys.path, and its `lastcall` module shadows the `lastcall` package.
_spec = importlib.util.spec_from_file_location("lastcall_relay_under_test", RELAY)
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

posix_only = unittest.skipUnless(
    os.name == "posix" and shutil.which("git"), "relay is POSIX + git only")

# Mirrors claude 2.1.281: --bg ignores --session-id (with a warning), picks
# its own id, prints "backgrounded · <short> · <name>", and records Remote
# Control in ~/.claude/jobs/<short>/state.json — not in the transcript.
FAKE_CLAUDE = r'''#!%(python)s
import json, os, re, subprocess, sys, uuid
args = sys.argv[1:]
home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd(),
                         "leak": os.environ.get("CLAUDE_CODE_SESSION_ID"),
                         "bedrock": os.environ.get("CLAUDE_CODE_USE_BEDROCK"),
                         "chain": os.environ.get("LASTCALL_RELAY_CHAIN")}) + "\n")
if args[:2] == ["agents", "--json"]:
    agents = []
    jobs = os.path.join(home, "jobs")
    for short in sorted(os.listdir(jobs)) if os.path.isdir(jobs) else []:
        with open(os.path.join(jobs, short, "state.json")) as fh:
            state = json.load(fh)
        agents.append({"id": short, "sessionId": state.get("sessionId"), "name": state.get("name"),
                       "state": "running", "kind": "background"})
    print(json.dumps(agents))
    sys.exit(0)
if args[:1] and args[0] in ("stop", "rm", "logs", "attach"):
    sys.exit(0)
if "--bg" not in args:
    sys.exit(0)
if os.environ.get("FAKE_UNTRUSTED"):
    print("Workspace not trusted. Run `claude` in %%s once and accept the trust prompt, then retry." %% os.getcwd())
    sys.exit(1)
if "--session-id" in args:
    print("warning: --bg manages the session id; ignoring --session-id (use --resume <id> to continue an existing session)")
sid = str(uuid.uuid4())
short = sid[:8]
name = args[args.index("-n") + 1]
settings = json.loads(args[args.index("--settings") + 1])
state = {"sessionId": sid, "name": name, "respawnFlags": ["--bg"]}
if "--remote-control" in args and not os.environ.get("FAKE_NO_BRIDGE"):
    state["bridgeSessionId"] = "cse_fake"
os.makedirs(os.path.join(home, "jobs", short), exist_ok=True)
with open(os.path.join(home, "jobs", short, "state.json"), "w") as fh:
    json.dump(state, fh)
folder = os.path.join(home, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
os.makedirs(folder, exist_ok=True)
transcript = os.path.join(folder, sid + ".jsonl")
if not os.environ.get("FAKE_NO_TRANSCRIPT"):
    with open(transcript, "w") as fh:
        fh.write(json.dumps({"type": "custom-title", "sessionId": sid, "customTitle": name}) + "\n")
if not os.environ.get("FAKE_NO_HOOK"):
    payload = json.dumps({"session_id": sid, "transcript_path": transcript, "cwd": os.getcwd(),
                          "source": "startup", "hook_event_name": "SessionStart"})
    for group in settings["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            subprocess.run(hook["command"], shell=True, input=payload, universal_newlines=True)
if not os.environ.get("FAKE_QUIET_BG"):
    print("backgrounded · %%s · %%s" %% (short, name))
'''

# `codex exec --json` prints thread.started; `codex app-server` speaks a
# scripted JSON-RPC on stdio. FAKE_APP_FAIL=<method> makes that request fail
# (initialize: the server exits at once), FAKE_APPROVAL=1 sends a command
# approval request mid-turn, FAKE_APP_HANG=1 never completes the turn until
# it is interrupted. Every client message is appended to FAKE_RPC.
FAKE_CODEX = r'''#!%(python)s
import json, os, subprocess, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
home = os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex")
day = os.path.join(home, "sessions", "2026", "09", "25")

def rollout(tid, source):
    os.makedirs(day, exist_ok=True)
    path = os.path.join(day, "rollout-2026-09-25T00-00-00-%%s.jsonl" %% tid)
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {
            "id": tid, "cwd": os.getcwd(), "source": source}}) + "\n")
    return path

if args[:1] == ["exec"]:
    if os.environ.get("FAKE_CODEX_DIE"):
        print("boom")
        sys.exit(3)
    tid = "01a0d9b2-0000-7000-8000-%%012d" %% os.getpid()
    path = rollout(tid, "exec")
    if os.environ.get("FAKE_PLUGIN_HOOK"):
        # The installed Last Call plugin's SessionStart hook, as Codex runs it.
        payload = json.dumps({"session_id": tid, "transcript_path": path, "cwd": os.getcwd(),
                              "hook_event_name": "SessionStart", "source": "startup"})
        out = subprocess.run([sys.executable, os.environ["FAKE_PLUGIN_HOOK"], "SessionStart"],
                             input=payload, stdout=subprocess.PIPE,
                             universal_newlines=True).stdout
        with open(os.environ["FAKE_HOOK_OUT"], "w") as fh:
            fh.write(out)
    if not os.environ.get("FAKE_NO_THREAD_EVENT"):
        print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
    time.sleep(0.2)
    print(json.dumps({"type": "turn.completed"}), flush=True)
    sys.exit(0)
if args[:1] != ["app-server"]:
    sys.exit(0)
fail = os.environ.get("FAKE_APP_FAIL", "")
if fail == "initialize":
    sys.exit(1)

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

def log(message):
    if os.environ.get("FAKE_RPC"):
        with open(os.environ["FAKE_RPC"], "a") as fh:
            fh.write(json.dumps(message) + "\n")

tid, turn = "01a0d9c7-0000-7000-8000-%%012d" %% os.getpid(), None
pending = None
for line in sys.stdin:
    message = json.loads(line)
    log(message)
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message or method is None:
        if message.get("id") == 900 and pending:      # our approval request answered
            send({"method": "turn/completed", "params": {"threadId": tid, "turn": {
                "id": turn, "status": "completed", "items": []}}})
            pending = None
        continue
    if method == fail:
        send({"id": message["id"], "error": {"code": -32603, "message": "scripted failure"}})
        continue
    if method == "thread/name/set" and os.environ.get("FAKE_NAMES"):
        with open(os.environ["FAKE_NAMES"], "a") as fh:
            fh.write(json.dumps(params) + "\n")
    if method == "initialize":
        log({"env": {k: os.environ.get(k) for k in ("LASTCALL_RELAY_CHAIN", "CODEX_THREAD_ID",
                                                     "CODEX_SANDBOX")}})
        if os.environ.get("FAKE_APP_SILENT"):
            time.sleep(60)
        send({"id": message["id"], "result": {"userAgent": "fake", "codexHome": home}})
    elif method == "thread/start":
        path = rollout(tid, "vscode")
        send({"id": message["id"], "result": {"model": params.get("model") or "gpt-default",
             "thread": {"id": tid, "path": path, "cwd": params.get("cwd"), "source": "vscode",
                        "name": None, "status": {"type": "idle"}}}})
        send({"method": "thread/started", "params": {"thread": {"id": tid}}})
    elif method == "turn/start":
        turn = "turn-1"
        send({"id": message["id"], "result": {"turn": {"id": turn, "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": tid, "turn": {"id": turn}}})
        send({"method": "item/agentMessage/delta", "params": {"threadId": tid, "delta": "READY"}})
        if os.environ.get("FAKE_APPROVAL"):
            pending = True
            send({"id": 900, "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": tid, "turnId": turn, "itemId": "i1",
                             "startedAtMs": 0, "command": "rm -rf build"}})
        elif not os.environ.get("FAKE_APP_HANG"):
            time.sleep(0.3)
            send({"method": "turn/completed", "params": {"threadId": tid, "turn": {
                "id": turn, "status": "completed", "items": []}}})
    elif method == "turn/interrupt":
        send({"id": message["id"], "result": {}})
        send({"method": "turn/completed", "params": {"threadId": tid, "turn": {
            "id": turn, "status": "interrupted", "items": []}}})
    else:
        send({"id": message["id"], "result": {}})
'''


HOOK_SCRIPT = os.path.join(ROOT, "plugins", "lastcall", "scripts", "lastcall.py")


def scrubbed_environ():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "CODEX", "LASTCALL", "TMUX"))}
    return env


@posix_only
class RelayCoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="relay2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "fake.log")
        self.names = os.path.join(self.tmp, "names.log")
        self.rpc = os.path.join(self.tmp, "rpc.log")
        self.stub("claude", FAKE_CLAUDE % {"python": sys.executable})
        self.stub("codex", FAKE_CODEX % {"python": sys.executable})
        self.stub("tmux", "#!/bin/sh\nexit 0\n")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)

    def repo(self, name="proj", handoff="# Ship the parser\nbody\n", commit=True, dirty=False):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(path, "docs", "handoff"))
        run = lambda *a: subprocess.run(a, cwd=path, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, check=True)
        run("git", "init", "-q")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "t")
        with open(os.path.join(path, "README.md"), "w") as fh:
            fh.write("seed\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "seed")
        if handoff is not None:
            with open(os.path.join(path, "docs", "handoff", "2026-09-25.md"), "w") as fh:
                fh.write(handoff)
            if commit:
                run("git", "add", "-A")
                run("git", "commit", "-qm", "handoff")
        if dirty:
            with open(os.path.join(path, "README.md"), "a") as fh:
                fh.write("uncommitted\n")
        return path

    def env(self, extra=None):
        env = scrubbed_environ()
        env.update({"PATH": self.bin + os.pathsep + env.get("PATH", ""), "HOME": self.tmp,
                    "FAKE_LOG": self.log, "FAKE_NAMES": self.names, "FAKE_RPC": self.rpc})
        env.update(extra or {})
        return env

    def relay(self, repo, *args, extra=None):
        argv = [sys.executable, RELAY, "--repo", repo, "--poll", "0.05",
                "--timeout", "5", "--rc-timeout", "1", "--hook-grace", "0.3"] + list(args)
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=self.env(extra), cwd=self.tmp, universal_newlines=True)
        return result

    def calls(self):
        try:
            with open(self.log) as fh:
                return [json.loads(line) for line in fh]
        except OSError:
            return []

    def rpc_messages(self, method=None):
        try:
            with open(self.rpc) as fh:
                messages = [json.loads(line) for line in fh]
        except OSError:
            return []
        return [m for m in messages if method is None or m.get("method") == method]

    def wait_for_runner_exit(self, timeout=15):
        """The app runner is detached; wait for it so no process outlives a test."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            done = [r for r in self.ledger() if r["event"] in ("app-runner-exit", "app-failed")]
            if done:
                pid = done[-1].get("runner_pid")
                while pid and time.time() < deadline:
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                    time.sleep(0.05)
                return done[-1]
            time.sleep(0.05)
        self.fail("the app runner never finished: %s" % self.ledger())

    def ledger(self):
        folder = os.path.join(self.tmp, ".lastcall", "relay")
        records = []
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if name.endswith(".jsonl"):
                records += relay.read_ledger(os.path.join(folder, name))
        return records


class TestHandoffAndNaming(RelayCoreCase):
    def test_newest_handoff_wins_and_templates_are_ignored(self):
        folder = os.path.join(self.tmp, "h")
        os.makedirs(folder)
        for name, age in (("2026-09-01.md", 300), ("2026-09-02.md", 200),
                          ("TEMPLATE.md", 0), ("notes.txt", 0)):
            path = os.path.join(folder, name)
            with open(path, "w") as fh:
                fh.write("x")
            os.utime(path, (time.time() - age, time.time() - age))
        self.assertEqual(os.path.basename(relay.pick_handoff(folder)), "2026-09-02.md")

    def test_no_handoff_at_all_is_none(self):
        folder = os.path.join(self.tmp, "empty")
        os.makedirs(folder)
        with open(os.path.join(folder, "TEMPLATE.md"), "w") as fh:
            fh.write("x")
        self.assertIsNone(relay.pick_handoff(folder))
        self.assertIsNone(relay.pick_handoff(os.path.join(self.tmp, "missing")))

    def test_topic_comes_from_the_first_heading(self):
        path = os.path.join(self.tmp, "2026-09-25.md")
        with open(path, "w") as fh:
            fh.write("\n# Handoff: wire the Codex adapter\n\nbody\n")
        self.assertEqual(relay.handoff_topic(path), "wire the Codex adapter")

    def test_topic_falls_back_to_the_filename_without_its_date(self):
        path = os.path.join(self.tmp, "2026-09-25-1430-fix-remote-control.md")
        with open(path, "w") as fh:
            fh.write("no heading here\n")
        self.assertEqual(relay.handoff_topic(path), "fix remote control")

    def test_successor_name_shape(self):
        self.assertEqual(relay.successor_name("lastcall", 3, "parser"),
                         "lastcall · handoff 3 · parser")
        self.assertEqual(relay.successor_name("lastcall", 1, ""), "lastcall · handoff 1")

    def test_generation_is_inherited_from_the_predecessor(self):
        self.assertEqual(relay.next_generation({relay.GENERATION_ENV: "4"}, []), 5)
        records = [{"event": "spawn", "generation": 2}, {"event": "checkin", "generation": 9}]
        self.assertEqual(relay.next_generation({}, records), 3)
        self.assertEqual(relay.next_generation({}, []), 1)

    def test_claude_slug_replaces_every_non_alphanumeric(self):
        self.assertEqual(relay.claude_slug("/a/b_c.d"), "-a-b-c-d")


class TestCommands(RelayCoreCase):
    def opts(self, **kw):
        base = dict(claude_bin="claude", codex_bin="codex", remote_control=True, model=None,
                    fallback_model=None, skip_permissions=False, permission_mode=None,
                    codex_mode="exec", codex_sandbox="workspace-write",
                    codex_approval="never", codex_app_max=60.0, python_bin="py",
                    name_thread=True)
        base.update(kw)
        return type("Opts", (), base)()

    def test_claude_argv_is_background_named_remote_controlled_and_pinned(self):
        argv = relay.claude_argv(self.opts(model="sonnet", fallback_model="haiku"),
                                 "p · handoff 1", "PROMPT", "{}")
        self.assertEqual(argv[:4], ["claude", "--bg", "-n", "p · handoff 1"])
        self.assertEqual(argv[argv.index("--remote-control") + 1], "p · handoff 1")
        self.assertNotIn("--session-id", argv, "--bg ignores --session-id; do not pass it")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "haiku")
        self.assertEqual(argv[-1], "PROMPT")
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_claude_argv_without_remote_control_and_unattended(self):
        argv = relay.claude_argv(self.opts(remote_control=False, skip_permissions=True),
                                 "n", "P", "{}")
        self.assertNotIn("--remote-control", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_settings_carry_the_checkin_hook_and_env(self):
        settings = json.loads(relay.claude_settings({"LASTCALL_RELAY_CHAIN": "c"}, "HOOK"))
        self.assertEqual(settings["env"], {"LASTCALL_RELAY_CHAIN": "c"})
        hook = settings["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual((hook["type"], hook["command"]), ("command", "HOOK"))

    def test_codex_exec_argv(self):
        argv = relay.codex_argv(self.opts(model="gpt-x"), "/r", "P", have_git=False)
        self.assertEqual(argv[:3], ["codex", "exec", "--json"])
        self.assertEqual(argv[argv.index("-C") + 1], "/r")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-x")
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[-1], "P")

    def test_codex_tmux_argv_runs_the_tui_with_the_relay_env(self):
        inner = relay.codex_argv(self.opts(codex_mode="tmux", skip_permissions=True), "/r", "P", True)
        self.assertNotIn("exec", inner)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", inner)
        argv = relay.tmux_argv("tmux", "s", "/r", inner, {"LASTCALL_RELAY_CHAIN": "c"})
        self.assertEqual(argv[:7], ["tmux", "new-session", "-d", "-s", "s", "-c", "/r"])
        self.assertTrue(argv[7].startswith("env LASTCALL_RELAY_CHAIN=c codex"))

    def test_bg_short_id_is_parsed_past_the_session_id_warning(self):
        output = ("warning: --bg manages the session id; ignoring --session-id (use --resume "
                  "<id> to continue an existing session)\nbackgrounded \u00b7 c0815a37 \u00b7 "
                  "proj \u00b7 handoff 1")
        self.assertEqual(relay.parse_bg_short(output), "c0815a37")
        self.assertEqual(relay.parse_bg_short("Started background session abcdef12"), "abcdef12")
        self.assertIsNone(relay.parse_bg_short("warning: deadbeef is not an id\n"))

    def test_codex_app_runner_argv(self):
        argv = relay.codex_app_runner_argv(self.opts(model="gpt-x", codex_app_max=90.0), "/r",
                                           "P", "N", "/l.jsonl", "c", 2, "/h.md")
        self.assertEqual(argv[2], "codex-app-runner")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("--approval") + 1], "never")
        self.assertEqual(argv[argv.index("--max-seconds") + 1], "90")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-x")
        self.assertEqual(argv[-2:], ["--prompt", "P"])
        self.assertNotIn("--auto-approve", argv)
        unattended = relay.codex_app_runner_argv(self.opts(skip_permissions=True), "/r", "P",
                                                 "N", "/l", "c", 1, "/h")
        self.assertEqual(unattended[unattended.index("--sandbox") + 1], "danger-full-access")
        self.assertIn("--auto-approve", unattended)

    def test_approval_requests_are_declined_unless_permissions_are_skipped(self):
        answer = relay.approval_answer
        self.assertEqual(answer("item/commandExecution/requestApproval", {}, False),
                         ({"decision": "decline"}, None))
        self.assertEqual(answer("item/fileChange/requestApproval", {}, True),
                         ({"decision": "accept"}, None))
        self.assertEqual(answer("execCommandApproval", {}, False), ({"decision": "denied"}, None))
        self.assertEqual(answer("applyPatchApproval", {}, True), ({"decision": "approved"}, None))
        self.assertEqual(answer("item/permissions/requestApproval",
                                {"permissions": {"network": {"enabled": True}}}, False)[0],
                         {"permissions": {}, "scope": "turn"})
        self.assertEqual(answer("mcpServer/elicitation/request", {}, True)[0],
                         {"action": "decline"})
        result, error = answer("item/tool/call", {}, True)
        self.assertIsNone(result)
        self.assertEqual(error["code"], -32601)

    def test_successor_env_drops_session_identity_but_keeps_provider_settings(self):
        env = relay.successor_env({"CLAUDE_CODE_SESSION_ID": "x", "CLAUDECODE": "1",
                                   "CLAUDE_CODE_MESSAGING_SOCKET": "/s", "CODEX_THREAD_ID": "t",
                                   "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/bin",
                                   "LASTCALL_RELAY_CHAIN": "old"},
                                  {"LASTCALL_RELAY_CHAIN": "new"})
        self.assertEqual(env, {"CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/bin",
                               "LASTCALL_RELAY_CHAIN": "new"})


class TestPreconditions(RelayCoreCase):
    def test_dry_run_prints_the_commands_and_spawns_nothing(self):
        result = self.relay(self.repo(), "--dry-run", "--model", "sonnet")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("dry run", result.stdout)
        self.assertIn("--bg -n 'proj · handoff 1 · Ship the parser'", result.stdout)
        self.assertIn("--remote-control", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_refuses_an_uncommitted_handoff(self):
        result = self.relay(self.repo(commit=False), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not committed", result.stdout)

    def test_refuses_a_dirty_tree_unless_allowed(self):
        repo = self.repo(dirty=True)
        self.assertEqual(self.relay(repo, "--dry-run").returncode, 1)
        self.assertEqual(self.relay(repo, "--dry-run", "--allow-dirty").returncode, 0)

    def test_refuses_when_there_is_no_handoff(self):
        result = self.relay(self.repo(handoff=None), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no handoff files", result.stdout)

    def test_codex_dry_run_defaults_to_app_mode_with_an_exec_fallback(self):
        result = self.relay(self.repo(), "--agent", "codex", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("codex-app-runner", result.stdout)
        self.assertIn("Codex app sidebar", result.stdout)
        self.assertIn("fallback:    codex exec --json", result.stdout)
        self.assertEqual(self.calls(), [])

    def write_codex_config(self, text):
        folder = os.path.join(self.tmp, ".codex")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "config.toml")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_app_mode_discloses_that_codex_will_trust_the_repo(self):
        """Live-QA finding: the app-server marks the successor's cwd trusted
        in ~/.codex/config.toml on a workspace-write thread/start. Say so
        before spawning, and only while the repo is not trusted yet."""
        repo = self.repo()
        config = self.write_codex_config('model = "gpt-x"\n[projects."/elsewhere"]\n'
                                         'trust_level = "trusted"\n')
        with open(config) as fh:
            before = fh.read()
        result = self.relay(repo, "--agent", "codex", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("NOTE: Codex will mark %s as a trusted project in %s (Codex does this "
                      "itself when the app-server starts a workspace-write thread)"
                      % (repo, config), result.stdout)
        with open(config) as fh:
            self.assertEqual(fh.read(), before, "the check must be read-only")
        self.write_codex_config('[projects."%s"] # added by Codex\ntrust_level = "trusted"\n'
                                % repo)
        result = self.relay(repo, "--agent", "codex", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("trusted project", result.stdout)

    def test_the_trust_note_is_only_for_writable_app_threads(self):
        repo = self.repo()
        for args in (("--codex-mode", "exec"), ("--codex-sandbox", "read-only")):
            result = self.relay(repo, "--agent", "codex", "--dry-run", *args)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("trusted project", result.stdout, args)
        result = self.relay(repo, "--agent", "claude", "--dry-run")
        self.assertNotIn("trusted project", result.stdout)

    def test_trusted_projects_parser_is_tolerant(self):
        self.write_codex_config("\n".join([
            'notify = ["x"]',
            '[projects."/a/double"]',
            'trust_level = "trusted"',
            "[projects.'/b/literal']",
            "trust_level = 'trusted'",
            '[projects."/c/untrusted"]',
            'trust_level = "untrusted"',
            '[projects."C:\\\\Users\\\\me\\\\repo"]',
            'trust_level = "trusted"',
            '[hooks.state."lastcall@claude-lastcall:Stop"]',
            'trust_level = "trusted"',
            "[projects]",
            '"/d/inline" = { trust_level = "trusted" }',
            '"/e/inline-no" = { trust_level = "untrusted" }',
            "[[weird]]",
            'trust_level = "trusted"',
            "not toml at all = = [",
        ]) + "\n")
        env = {"HOME": self.tmp}
        self.assertEqual(relay.codex_trusted_projects(env),
                         {"/a/double", "/b/literal", "C:\\Users\\me\\repo", "/d/inline"})
        self.assertEqual(relay.codex_trusted_projects({"CODEX_HOME": "/nonexistent"}), set())

    def test_unknown_codex_mode_in_config_is_refused(self):
        repo = self.repo()
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"agent": "codex", "codex_mode": "desktop"}}, fh)
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "cfg"], check=True)
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("unknown codex mode", result.stdout)

    def test_untrusted_workspace_is_a_precondition_failure(self):
        result = self.relay(self.repo(), extra={"FAKE_UNTRUSTED": "1"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("untrusted workspace", result.stdout)

    def test_config_supplies_model_and_prefix(self):
        repo = self.repo()
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"model": "fable", "fallback_model": "opus",
                                 "name_prefix": "lc"}}, fh)
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "cfg"], check=True)
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("--model fable --fallback-model opus", result.stdout)
        self.assertIn("lc · handoff 1", result.stdout)


class TestClaudeRelay(RelayCoreCase):
    def test_successor_checks_in_through_its_hook_and_remote_control_is_proven(self):
        result = self.relay(self.repo(), extra={"CLAUDE_CODE_SESSION_ID": "leaky",
                                                "CLAUDE_CODE_USE_BEDROCK": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via hook", result.stdout)
        self.assertIn("remote control: connected (jobs/", result.stdout)
        spawn = [c for c in self.calls() if "--bg" in c["argv"]][0]
        self.assertIsNone(spawn["leak"], "predecessor session id leaked into the successor")
        self.assertEqual(spawn["bedrock"], "1")
        self.assertNotIn("warning: --bg manages the session id", result.stdout)
        records = self.ledger()
        self.assertEqual([r["event"] for r in records], ["spawn", "checkin", "bg", "verified"])
        checkin, bg, verified = records[1], records[2], records[3]
        self.assertEqual(bg["bg_id"], checkin["session_id"][:8])
        self.assertEqual(verified["session_id"], checkin["session_id"])
        self.assertTrue(verified["remote_control"])
        self.assertIn("attach %s" % bg["bg_id"], result.stdout)

    def test_missing_bridge_session_is_reported_loudly(self):
        result = self.relay(self.repo(), extra={"FAKE_NO_BRIDGE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("remote control did NOT connect", result.stdout)

    def test_required_remote_control_passes_on_job_state_alone(self):
        # 2.1.281 writes bridgeSessionId to jobs/<short>/state.json and no
        # bridge-session transcript entry: that must count.
        result = self.relay(self.repo(), "--require-remote-control")
        self.assertEqual(result.returncode, 0, result.stdout)
        transcript = [r for r in self.ledger() if r["event"] == "checkin"][0]["transcript_path"]
        with open(transcript) as fh:
            self.assertNotIn("bridge-session", fh.read())

    def test_short_id_falls_back_to_claude_agents_json(self):
        result = self.relay(self.repo(), extra={"FAKE_QUIET_BG": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(any(c["argv"] == ["agents", "--json"] for c in self.calls()))
        bg = [r for r in self.ledger() if r["event"] == "bg"][0]
        checkin = [r for r in self.ledger() if r["event"] == "checkin"][0]
        self.assertEqual(bg["bg_id"], checkin["session_id"][:8])

    def test_required_remote_control_turns_that_into_exit_2(self):
        result = self.relay(self.repo(), "--require-remote-control",
                            extra={"FAKE_NO_BRIDGE": "1"})
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_bridge_in_the_job_state_is_found_by_short_id(self):
        sid = "c0815a37-2222-3333-4444-555555555555"
        folder = os.path.join(self.tmp, ".claude", "jobs", "c0815a37")
        os.makedirs(folder)
        with open(os.path.join(folder, "state.json"), "w") as fh:
            json.dump({"sessionId": sid, "bridgeSessionId": "cse_job"}, fh)
        env = {"HOME": self.tmp}
        self.assertIn("cse_job", relay.remote_control_evidence(sid, None, env, short="c0815a37"))
        self.assertIn("cse_job", relay.remote_control_evidence(sid, None, env))
        self.assertIsNone(relay.remote_control_evidence("other-session", None, env,
                                                        short="c0815a37"))

    def test_bridge_in_the_sessions_registry_also_counts(self):
        sid = "11111111-2222-3333-4444-555555555555"
        folder = os.path.join(self.tmp, ".claude", "sessions")
        os.makedirs(folder)
        with open(os.path.join(folder, "123.json"), "w") as fh:
            json.dump({"sessionId": sid, "bridgeSessionId": "cse_x"}, fh)
        evidence = relay.remote_control_evidence(sid, None, {"HOME": self.tmp})
        self.assertIn("cse_x", evidence)
        self.assertIsNone(relay.remote_control_evidence("other", None, {"HOME": self.tmp}))

    def test_no_checkin_and_no_transcript_is_exit_2(self):
        result = self.relay(self.repo(), "--timeout", "0.5",
                            extra={"FAKE_NO_HOOK": "1", "FAKE_NO_TRANSCRIPT": "1"})
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("never checked in", result.stdout)

    def test_transcript_without_hook_is_accepted_but_flagged(self):
        result = self.relay(self.repo(), extra={"FAKE_NO_HOOK": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("no hook check-in", result.stdout)

    def test_chain_and_generation_are_inherited(self):
        ledger_root = os.path.join(self.tmp, ".lastcall", "relay")
        result = self.relay(self.repo(), extra={relay.CHAIN_ENV: "chainA",
                                                relay.GENERATION_ENV: "3"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("handoff 4", result.stdout)
        self.assertTrue(os.path.isfile(os.path.join(ledger_root, "chainA.jsonl")))

    def test_background_predecessor_is_stopped_with_claude_stop(self):
        pred = "abcdef12-0000-4000-8000-000000000000"
        jobs = os.path.join(self.tmp, ".claude", "jobs", pred[:8])
        os.makedirs(jobs)
        with open(os.path.join(jobs, "state.json"), "w") as fh:
            json.dump({"sessionId": pred}, fh)
        result = self.relay(self.repo(), "--retire-predecessor", "--kill-delay", "0",
                            extra={"CLAUDE_CODE_SESSION_ID": pred})
        self.assertEqual(result.returncode, 0, result.stdout)
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(c["argv"] == ["stop", pred[:8]] for c in self.calls()):
                break
            time.sleep(0.1)
        else:
            self.fail("claude stop was never called: %s" % self.calls())

    def test_a_desktop_predecessor_is_never_killed(self):
        result = self.relay(self.repo(), "--dry-run", "--retire-predecessor",
                            extra={"CLAUDE_CODE_SESSION_ID": "d", "CLAUDE_CODE_ENTRYPOINT":
                                   "claude-desktop"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("will not kill it", result.stdout)


class TestCodexRelay(RelayCoreCase):
    def test_exec_successor_checks_in_and_is_named(self):
        result = self.relay(self.repo(), "--agent", "codex", "--codex-mode", "exec",
                            "--model", "gpt-x")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via exec-json", result.stdout)
        with open(self.names) as fh:
            named = json.loads(fh.readline())
        self.assertEqual(named["name"], "proj · handoff 1 · Ship the parser")
        checkin = [r for r in self.ledger() if r["event"] == "checkin"][0]
        self.assertTrue(checkin["transcript_path"].endswith(checkin["session_id"] + ".jsonl"))

    def test_exec_that_dies_before_a_thread_is_exit_2(self):
        result = self.relay(self.repo(), "--agent", "codex", "--codex-mode", "exec",
                            extra={"FAKE_CODEX_DIE": "1"})
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("exited before starting a thread", result.stdout)


class TestCodexAppMode(RelayCoreCase):
    def test_app_mode_is_the_default_and_the_thread_is_visible_and_named(self):
        repo = self.repo()
        result = self.relay(repo, "--agent", "codex", "--model", "gpt-x")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via app-server", result.stdout)
        self.assertIn("thread name: set", result.stdout)
        self.assertIn("source vscode", result.stdout)
        done = self.wait_for_runner_exit()
        self.assertEqual((done["event"], done["status"], done["reason"]),
                         ("app-runner-exit", "completed", "turn-completed"))
        init = self.rpc_messages("initialize")[0]["params"]
        self.assertEqual(init["clientInfo"]["name"], "lastcall-relay")
        self.assertIn("item/agentMessage/delta",
                      init["capabilities"]["optOutNotificationMethods"])
        self.assertTrue(self.rpc_messages("initialized"))
        start = self.rpc_messages("thread/start")[0]["params"]
        self.assertEqual(start, {"cwd": repo, "model": "gpt-x", "sandbox": "workspace-write",
                                 "approvalPolicy": "never"})
        methods = [m.get("method") for m in self.rpc_messages() if m.get("method")]
        self.assertLess(methods.index("thread/name/set"), methods.index("turn/start"))
        named = self.rpc_messages("thread/name/set")[0]["params"]
        self.assertEqual(named["name"], "proj · handoff 1 · Ship the parser")
        turn = self.rpc_messages("turn/start")[0]["params"]
        self.assertEqual(turn["threadId"], named["threadId"])
        self.assertIn("docs/handoff/2026-09-25.md and follow it", turn["input"][0]["text"])
        checkin = [r for r in self.ledger() if r["event"] == "checkin"][0]
        self.assertEqual((checkin["via"], checkin["source"], checkin["session_id"]),
                         ("app-server", "vscode", named["threadId"]))
        self.assertTrue(checkin["transcript_path"].endswith(checkin["session_id"] + ".jsonl"))
        self.assertTrue(os.path.isfile(checkin["transcript_path"]))
        self.assertFalse(any(c["argv"][:1] == ["exec"] for c in self.calls()))

    def test_runner_env_carries_the_relay_chain_to_the_successor(self):
        result = self.relay(self.repo(), "--agent", "codex",
                            extra={"CODEX_THREAD_ID": "pred", "CODEX_SANDBOX": "seatbelt"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.wait_for_runner_exit()
        env = [m["env"] for m in self.rpc_messages() if "env" in m][0]
        chain = [r for r in self.ledger() if r["event"] == "spawn"][0]["chain"]
        self.assertEqual(env, {"LASTCALL_RELAY_CHAIN": chain, "CODEX_THREAD_ID": None,
                               "CODEX_SANDBOX": None})
        spawn = [r for r in self.ledger() if r["event"] == "spawn"][0]
        with open(spawn["log"]) as fh:
            runner_log = fh.read()
        self.assertIn("checked in", runner_log)
        self.assertIn("app-server stopped", runner_log)

    def test_a_runner_stuck_before_any_thread_is_killed_and_exec_takes_over(self):
        # --no-name-thread: naming the exec fallback would meet the same silent server.
        result = self.relay(self.repo(), "--agent", "codex", "--timeout", "1.5",
                            "--no-name-thread", extra={"FAKE_APP_SILENT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("no thread within", result.stdout)
        self.assertIn("via exec-json", result.stdout)
        runner = int(result.stdout.split("runner pid ")[1].split()[0])
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(runner, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail("the stuck runner was not killed")

    def test_skip_permissions_means_full_access_and_approvals_accepted(self):
        result = self.relay(self.repo(), "--agent", "codex", "--skip-permissions",
                            extra={"FAKE_APPROVAL": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        done = self.wait_for_runner_exit()
        self.assertEqual(done["answered"], 1)
        start = self.rpc_messages("thread/start")[0]["params"]
        self.assertEqual(start["sandbox"], "danger-full-access")
        answer = [m for m in self.rpc_messages() if m.get("id") == 900][0]
        self.assertEqual(answer["result"], {"decision": "accept"})

    def test_approval_requests_are_declined_by_default(self):
        result = self.relay(self.repo(), "--agent", "codex", extra={"FAKE_APPROVAL": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.wait_for_runner_exit()
        answer = [m for m in self.rpc_messages() if m.get("id") == 900][0]
        self.assertEqual(answer["result"], {"decision": "decline"})

    def test_thread_start_failure_falls_back_to_exec_loudly(self):
        result = self.relay(self.repo(), "--agent", "codex",
                            extra={"FAKE_APP_FAIL": "thread/start"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("codex app-server mode failed (thread/start", result.stdout)
        self.assertIn("falling back to `codex exec`", result.stdout)
        self.assertIn("via exec-json", result.stdout)
        events = [r["event"] for r in self.ledger()]
        self.assertIn("app-failed", events)
        self.assertIn("fallback", events)
        self.assertTrue(any(c["argv"][:1] == ["exec"] for c in self.calls()))

    def test_app_server_that_dies_at_once_falls_back_to_exec(self):
        result = self.relay(self.repo(), "--agent", "codex",
                            extra={"FAKE_APP_FAIL": "initialize"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("falling back to `codex exec`", result.stdout)
        failed = [r for r in self.ledger() if r["event"] == "app-failed"][0]
        self.assertEqual(failed["stage"], "initialize")

    def test_turn_start_failure_archives_the_empty_thread_then_falls_back(self):
        result = self.relay(self.repo(), "--agent", "codex",
                            extra={"FAKE_APP_FAIL": "turn/start"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("archived", result.stdout)
        self.assertIn("via exec-json", result.stdout)
        archived = self.rpc_messages("thread/archive")
        self.assertEqual(len(archived), 1)
        self.assertFalse(self.rpc_messages("thread/delete"))

    def test_fallback_that_also_fails_is_exit_2(self):
        result = self.relay(self.repo(), "--agent", "codex",
                            extra={"FAKE_APP_FAIL": "initialize", "FAKE_CODEX_DIE": "1"})
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("exited before starting a thread", result.stdout)

    def test_a_turn_that_outlives_max_seconds_is_interrupted_and_the_runner_exits(self):
        result = self.relay(self.repo(), "--agent", "codex", "--codex-app-max-seconds", "0.5",
                            extra={"FAKE_APP_HANG": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        done = self.wait_for_runner_exit()
        self.assertEqual((done["reason"], done["status"]), ("max-duration", "interrupted"))
        self.assertTrue(self.rpc_messages("turn/interrupt"))


class TestCheckinAndRetirement(RelayCoreCase):
    def test_checkin_subcommand_appends_a_record_and_prints_nothing(self):
        ledger = os.path.join(self.tmp, "l.jsonl")
        payload = json.dumps({"session_id": "S", "transcript_path": "/t", "cwd": "/c",
                              "source": "startup"})
        result = subprocess.run([sys.executable, RELAY, "checkin", "--ledger", ledger,
                                 "--chain", "c1", "--generation", "2", "--agent", "codex"],
                                input=payload, stdout=subprocess.PIPE, universal_newlines=True,
                                env=scrubbed_environ())
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        record = relay.read_ledger(ledger)[0]
        self.assertEqual((record["session_id"], record["chain"], record["generation"],
                          record["agent"]), ("S", "c1", 2, "codex"))

    def test_checkin_with_garbage_stdin_still_exits_0(self):
        self.assertEqual(relay.checkin_main(["--ledger", os.path.join(self.tmp, "x.jsonl"),
                                             "--chain", "c"], stdin=io.StringIO("not json")), 0)

    def test_checkin_from_hook_is_a_no_op_outside_a_relay(self):
        self.assertIsNone(relay.checkin_from_hook({"session_id": "S"}, {}))

    def test_delayed_signal_retirement_works_without_setsid(self):
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: victim.poll() is None and victim.kill())
        relay.schedule_retirement({"method": "signal", "pid": victim.pid}, 0.1)
        self.assertEqual(victim.wait(timeout=10), -15)

    def test_plan_prefers_claude_stop_then_tmux_then_signal(self):
        self.assertEqual(relay.plan_retirement({"bg_short": "abc"})["argv"],
                         ["claude", "stop", "abc"])
        self.assertEqual(relay.plan_retirement({"tmux_session": "t", "tmux_pane": "%1",
                                                "agent": "claude",
                                                "entrypoint": "cli"})["method"], "tmux")
        self.assertEqual(relay.plan_retirement({"agent": "claude", "entrypoint": "cli",
                                                "pid": 42, "kind": "interactive"})["pid"], 42)
        self.assertEqual(relay.plan_retirement({"agent": "codex"},
                                               find_codex=lambda: None)["method"], "none")
        self.assertEqual(relay.plan_retirement({})["method"], "none")



class TestLayeredConfig(RelayCoreCase):
    """relay.py reads its settings through lastcall_core.config.load_config,
    so the relay block lives wherever the rest of Last Call's config does."""

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)

    def committed(self, repo, rel, data):
        self.write(os.path.join(repo, rel), data)
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "cfg"], check=True)

    def test_global_config_is_read(self):
        self.write(os.path.join(self.tmp, ".lastcall", "config.json"),
                   {"relay": {"name_prefix": "globalprefix", "model": "opus"}})
        result = self.relay(self.repo(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("globalprefix · handoff 1", result.stdout)
        self.assertIn("--model opus", result.stdout)
        self.assertIn(os.path.join(".lastcall", "config.json"), result.stdout)

    def test_project_relay_block_merges_over_the_global_one_key_by_key(self):
        self.write(os.path.join(self.tmp, ".lastcall", "config.json"),
                   {"relay": {"name_prefix": "globalprefix", "model": "opus"}})
        repo = self.repo()
        self.committed(repo, ".lastcall.json", {"relay": {"model": "sonnet"}})
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("globalprefix · handoff 1", result.stdout)
        self.assertIn("--model sonnet", result.stdout)

    def test_every_project_config_name_is_read(self):
        for index, rel in enumerate((".lastcall.json", os.path.join(".lastcall", "config.json"),
                                     os.path.join(".claude", "lastcall.json"),
                                     os.path.join(".codex", "lastcall.json"))):
            name = "p%d" % index
            repo = self.repo(name=name)
            self.committed(repo, rel, {"relay": {"name_prefix": "from-" + name}})
            result = self.relay(repo, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("from-%s · handoff 1" % name, result.stdout, rel)

    def test_environment_overrides_the_files(self):
        repo = self.repo()
        self.committed(repo, ".lastcall.json", {"relay": {"name_prefix": "fromfile"}})
        result = self.relay(repo, "--dry-run",
                            extra={"LASTCALL_RELAY": json.dumps({"name_prefix": "fromenv"})})
        self.assertIn("fromenv · handoff 1", result.stdout)

    def test_flags_override_the_config(self):
        repo = self.repo()
        self.committed(repo, ".lastcall.json", {"relay": {"model": "opus", "agent": "codex"}})
        result = self.relay(repo, "--dry-run", "--model", "haiku", "--agent", "claude")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("--model haiku", result.stdout)
        self.assertIn("claude successor", result.stdout)

    def test_config_found_from_the_working_directory_names_the_repo(self):
        parent = os.path.join(self.tmp, "workspace")
        repo = self.repo(name=os.path.join("workspace", "frontend"))
        self.write(os.path.join(parent, ".lastcall.json"),
                   {"relay": {"repo": "frontend", "name_prefix": "ws"}})
        result = subprocess.run([sys.executable, RELAY, "--dry-run"], cwd=parent,
                                env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("repo:        %s" % repo, result.stdout)
        self.assertIn("ws · handoff 1", result.stdout)

    def test_retire_predecessor_and_kill_predecessor_are_the_same_setting(self):
        for key in ("retire_predecessor", "kill_predecessor"):
            repo = self.repo(name=key)
            self.committed(repo, ".lastcall.json", {"relay": {key: True, "kill_delay": 7}})
            result = self.relay(repo, "--dry-run", extra={"TMUX_PANE": "%1"})
            self.assertNotIn("not requested", result.stdout, key)
            off = self.relay(repo, "--dry-run", "--no-kill-predecessor")
            self.assertIn("not requested", off.stdout, key)

    def test_kill_delay_comes_from_the_config(self):
        self.stub("tmux", "#!/bin/sh\nprintf '%d\\told-session\\n'\n" % os.getpid())
        repo = self.repo()
        self.committed(repo, ".lastcall.json", {"relay": {"kill_predecessor": True,
                                                          "kill_delay": 7}})
        result = self.relay(repo, "--dry-run", extra={
            "TMUX_PANE": "%1", "CLAUDE_CODE_SESSION_ID": "p", "CLAUDE_PID": str(os.getpid()),
            "CLAUDE_CODE_ENTRYPOINT": "cli"})
        self.assertIn("in 7s: tmux kill-pane -t %1", result.stdout)

    def test_a_bad_flag_is_a_precondition_failure_not_exit_2(self):
        result = self.relay(self.repo(), "--dry-run", "--timeout", "abc")
        self.assertEqual(result.returncode, 1, result.stdout)
        result = self.relay(self.repo(name="p2"), "--dry-run", "--timeout", "-1")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_require_git(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(os.path.join(plain, "docs", "handoff"))
        with open(os.path.join(plain, "docs", "handoff", "next.md"), "w") as fh:
            fh.write("go\n")
        self.assertEqual(self.relay(plain, "--dry-run").returncode, 0)
        result = self.relay(plain, "--dry-run", "--require-git")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not a git worktree", result.stdout)

    def test_uncommitted_handoff_suggests_the_newest_committed_one(self):
        repo = self.repo()
        with open(os.path.join(repo, "docs", "handoff", "2026-09-26.md"), "w") as fh:
            fh.write("# newer\n")
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("newest committed handoff is docs/handoff/2026-09-25.md", result.stdout)


class TestAgentDefault(RelayCoreCase):
    """The successor is the agent that runs the predecessor unless something
    says otherwise; saying otherwise is a cross-agent handover."""

    def test_a_claude_predecessor_gets_a_claude_successor(self):
        result = self.relay(self.repo(), "--dry-run", extra={"CLAUDE_CODE_SESSION_ID": "s"})
        self.assertIn("claude successor", result.stdout)
        self.assertIn("(same as the predecessor)", result.stdout)

    def test_a_codex_predecessor_gets_a_codex_successor(self):
        result = self.relay(self.repo(), "--dry-run", extra={"CODEX_THREAD_ID": "t"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("codex successor", result.stdout)
        self.assertIn("codex-app-runner", result.stdout)

    def test_a_relay_successor_is_recognised_by_its_relay_agent(self):
        self.assertEqual(relay.running_agent({relay.AGENT_ENV: "codex"}), "codex")
        self.assertIsNone(relay.running_agent({relay.AGENT_ENV: "nonsense"}))
        self.assertIsNone(relay.running_agent({}))

    def test_nothing_detected_means_claude(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertIn("claude successor", result.stdout)
        self.assertIn("(default)", result.stdout)

    def test_config_agent_beats_detection(self):
        repo = self.repo()
        with open(os.path.join(repo, ".lastcall.json"), "w") as fh:
            json.dump({"relay": {"agent": "codex"}}, fh)
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "cfg"], check=True)
        result = self.relay(repo, "--dry-run", extra={"CLAUDE_CODE_SESSION_ID": "s"})
        self.assertIn("codex successor", result.stdout)
        self.assertIn("(config)", result.stdout)

    def test_claude_hands_over_to_codex(self):
        result = self.relay(self.repo(), "--agent", "codex", "--codex-mode", "exec",
                            extra={"CLAUDE_CODE_SESSION_ID": "pred", "CLAUDECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via exec-json", result.stdout)
        self.assertEqual([r["agent"] for r in self.ledger() if r["event"] == "spawn"], ["codex"])
        self.assertEqual(self.ledger()[0]["predecessor"], "pred")

    def test_codex_hands_over_to_claude(self):
        result = self.relay(self.repo(), "--agent", "claude", extra={"CODEX_THREAD_ID": "t"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via hook", result.stdout)
        spawn = [c for c in self.calls() if "--bg" in c["argv"]][0]
        self.assertIsNotNone(spawn["chain"])


class TestPluginCheckin(RelayCoreCase):
    """A successor started by ANY mode checks in through the normally installed
    plugin's SessionStart hook, and is told which handoff to read."""

    def relay_env(self, ledger, generation="2", handoff="/r/docs/handoff/h.md"):
        return {relay.LEDGER_ENV: ledger, relay.CHAIN_ENV: "chainX",
                relay.GENERATION_ENV: generation, relay.HANDOFF_ENV: handoff,
                relay.AGENT_ENV: "codex"}

    def test_checkin_is_idempotent_per_session(self):
        ledger = os.path.join(self.tmp, "l.jsonl")
        env = self.relay_env(ledger)
        first = relay.checkin_from_hook({"session_id": "S"}, env, "claude")
        again = relay.checkin_from_hook({"session_id": "S", "source": "resume"}, env, "claude")
        self.assertEqual(first, again)
        self.assertEqual(len(relay.read_ledger(ledger)), 1)

    def test_another_session_inheriting_the_variables_is_not_the_successor(self):
        ledger = os.path.join(self.tmp, "l.jsonl")
        env = self.relay_env(ledger)
        relay.checkin_from_hook({"session_id": "S"}, env, "claude")
        self.assertIsNone(relay.checkin_from_hook({"session_id": "verifier"}, env, "codex"))
        self.assertIsNone(relay.successor_session_start({"session_id": "verifier"}, env, "codex"))
        self.assertEqual(len(relay.read_ledger(ledger)), 1)

    def test_note_names_the_generation_chain_and_handoff(self):
        ledger = os.path.join(self.tmp, "l.jsonl")
        note = relay.successor_session_start({"session_id": "S", "source": "startup"},
                                             self.relay_env(ledger), "codex")
        self.assertIn("generation 2 of relay chain chainX", note)
        self.assertIn("Read /r/docs/handoff/h.md first", note)
        self.assertLess(len(note), 300)
        record = relay.read_ledger(ledger)[0]
        self.assertEqual((record["via"], record["agent"], record["session_id"]),
                         ("session-start", "codex", "S"))

    def test_resumed_successor_checks_in_but_gets_no_note(self):
        ledger = os.path.join(self.tmp, "l.jsonl")
        self.assertIsNone(relay.successor_session_start(
            {"session_id": "S", "source": "resume"}, self.relay_env(ledger), "claude"))
        self.assertEqual(len(relay.read_ledger(ledger)), 1)

    def test_unwritable_ledger_is_silent(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as fh:
            fh.write("x")
        env = self.relay_env(os.path.join(blocker, "sub", "l.jsonl"))
        self.assertIsNone(relay.successor_session_start({"session_id": "S"}, env, "claude"))

    def run_hook(self, env_extra, payload):
        env = self.env(env_extra)
        return subprocess.run([sys.executable, HOOK_SCRIPT, "SessionStart"],
                              input=json.dumps(payload), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, env=env,
                              cwd=self.tmp)

    def test_engine_session_start_checks_in_and_injects_the_note(self):
        ledger = os.path.join(self.tmp, "relay", "chainX.jsonl")
        for agent, transcript in (("claude", os.path.join(self.tmp, ".claude", "projects",
                                                          "p", "S1.jsonl")),
                                  ("codex", os.path.join(self.tmp, ".codex", "sessions",
                                                         "2026", "09", "25",
                                                         "rollout-x-S2.jsonl"))):
            sid = "S1" if agent == "claude" else "S2"
            env = self.relay_env(ledger, generation="3" if agent == "claude" else "4")
            env["LASTCALL_AGENT"] = agent
            result = self.run_hook(env, {"hook_event_name": "SessionStart", "source": "startup",
                                         "session_id": sid, "transcript_path": transcript,
                                         "cwd": self.tmp})
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("generation %s of relay chain chainX" % env[relay.GENERATION_ENV],
                          context, agent)
            record = [r for r in relay.read_ledger(ledger) if r["session_id"] == sid][0]
            self.assertEqual((record["agent"], record["via"]), (agent, "session-start"))

    def test_engine_without_relay_variables_writes_no_ledger(self):
        result = self.run_hook({}, {"hook_event_name": "SessionStart", "source": "startup",
                                    "session_id": "S", "cwd": self.tmp})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("LAST CALL RELAY", result.stdout)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, ".lastcall", "relay")))

    def test_engine_survives_a_broken_ledger(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as fh:
            fh.write("x")
        result = self.run_hook(self.relay_env(os.path.join(blocker, "l.jsonl")),
                               {"hook_event_name": "SessionStart", "source": "startup",
                                "session_id": "S", "cwd": self.tmp})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("LAST CALL RELAY", result.stdout)

    def test_codex_exec_successor_checks_in_through_the_plugin_hook(self):
        """No thread.started on stdout: the only proof is the plugin's
        SessionStart hook, which the relay accepts."""
        hook_out = os.path.join(self.tmp, "hook.out")
        result = self.relay(self.repo(), "--agent", "codex", "--codex-mode", "exec",
                            "--no-name-thread",
                            extra={"FAKE_PLUGIN_HOOK": HOOK_SCRIPT, "FAKE_HOOK_OUT": hook_out,
                                   "FAKE_NO_THREAD_EVENT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via session-start", result.stdout)
        with open(hook_out) as fh:
            context = json.loads(fh.read())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("generation 1 of relay chain", context)
        self.assertIn("docs/handoff/2026-09-25.md first", context)


class TestReadiness(RelayCoreCase):
    def test_either_cli_will_do_unless_the_config_pins_one(self):
        only_codex = lambda name: "/x/codex" if name in ("codex", "git") else None
        checks = relay.readiness(None, only_codex)
        self.assertTrue(checks["claude or codex CLI on PATH"])
        self.assertTrue(checks["relay script present"])
        self.assertNotIn("tmux on PATH (codex_mode tmux)", checks)
        pinned = relay.readiness({"agent": "claude"}, only_codex)
        self.assertFalse(pinned["claude CLI on PATH"])

    def test_tmux_matters_only_for_codex_tmux_mode(self):
        checks = relay.readiness({"codex_mode": "tmux"}, lambda name: None)
        self.assertFalse(checks["tmux on PATH (codex_mode tmux)"])


class TestLastcallRelayCommand(RelayCoreCase):
    def test_lastcall_relay_runs_relay_py(self):
        launcher = os.path.join(ROOT, "plugins", "lastcall", "bin", "lastcall")
        result = subprocess.run([sys.executable, launcher, "relay", "--repo", self.repo(),
                                 "--dry-run", "--agent", "codex"], env=self.env(),
                                cwd=self.tmp, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("relay — codex successor", result.stdout)
        help_text = subprocess.run([sys.executable, launcher, "relay", "--help"],
                                   stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertIn("usage: lastcall relay", help_text)



# tmux that really runs the command it is given (in the background, as
# `new-session -d` would) and reports every pane alive.
RUNNING_TMUX = """#!/bin/sh
case "$1" in
new-session) eval "last=\\${$#}"; sh -c "$last" >/dev/null 2>&1 & exit 0 ;;
display-message) echo 0; exit 0 ;;
esac
exit 0
"""

# The interactive Codex TUI, as far as the relay can see it: a rollout whose
# session_meta says when the session was created, written a moment after start.
TUI_CODEX = r'''#!%(python)s
import datetime, json, os, sys, time, uuid
args = sys.argv[1:]
if "--no-alt-screen" not in args:
    sys.exit(0)
time.sleep(0.2)
home = os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex")
day = os.path.join(home, "sessions", "2026", "09", "25")
os.makedirs(day, exist_ok=True)
tid = os.environ.get("FAKE_TUI_ID") or str(uuid.uuid4())
now = datetime.datetime.now(datetime.timezone.utc).strftime("%%Y-%%m-%%dT%%H:%%M:%%S.%%fZ")
with open(os.path.join(day, "rollout-2026-09-25T00-00-01-%%s.jsonl" %% tid), "w") as fh:
    fh.write(json.dumps({"timestamp": now, "type": "session_meta", "payload": {
        "id": tid, "timestamp": now, "cwd": os.getcwd(), "source": "cli"}}) + "\n")
time.sleep(5)
'''


def write_rollout(home, tid, cwd, created, source="cli"):
    day = os.path.join(home, ".codex", "sessions", "2026", "09", "25")
    os.makedirs(day, exist_ok=True)
    path = os.path.join(day, "rollout-2026-09-25T00-00-00-%s.jsonl" % tid)
    meta = {"id": tid, "cwd": cwd, "source": source}
    if created is not None:
        meta["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(created))
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
    return path


class TestTmuxSuccessorIdentity(RelayCoreCase):
    """Review finding: the tmux-mode scan filtered by mtime, so the
    predecessor's own (constantly written) rollout was taken as the successor."""

    PRED = "01a0d9aa-0000-7000-8000-000000000001"
    SUCC = "01a0d9bb-0000-7000-8000-000000000002"

    def test_scan_needs_creation_after_the_spawn_and_skips_the_predecessor(self):
        repo = self.repo()
        start = time.time()
        write_rollout(self.tmp, self.PRED, repo, start - 3600)       # old, but just written
        write_rollout(self.tmp, "01a0d9cc-0000-7000-8000-000000000003", repo, None)
        write_rollout(self.tmp, "01a0d9dd-0000-7000-8000-000000000004", repo, start + 1)
        found = relay.scan_new_rollouts(repo, start, {"HOME": self.tmp})
        self.assertEqual([m["id"] for _, m in found], ["01a0d9dd-0000-7000-8000-000000000004"])
        write_rollout(self.tmp, self.PRED, repo, start + 1)
        found = relay.scan_new_rollouts(repo, start, {"HOME": self.tmp}, exclude=(self.PRED,))
        self.assertNotIn(self.PRED, [m["id"] for _, m in found])

    def test_the_predecessors_busy_rollout_is_never_the_successor(self):
        repo = self.repo()
        pred = write_rollout(self.tmp, self.PRED, repo, time.time() - 3600)
        os.utime(pred, None)
        self.stub("tmux", RUNNING_TMUX)
        self.stub("codex", TUI_CODEX % {"python": sys.executable})
        result = self.relay(repo, "--agent", "codex", "--codex-mode", "tmux", "--no-name-thread",
                            extra={"CODEX_THREAD_ID": self.PRED, "FAKE_TUI_ID": self.SUCC})
        self.assertEqual(result.returncode, 0, result.stdout)
        checkin = [r for r in self.ledger() if r["event"] == "checkin"]
        self.assertEqual([r["session_id"] for r in checkin], [self.SUCC], result.stdout)



class TestRetirementScope(RelayCoreCase):
    """Review finding: retirement trusted an inherited TMUX_PANE and ran
    `tmux kill-session` — the user's whole workspace — and the Codex
    desktop/app-server exclusion only ran after the tmux branch."""

    def fake_tmux(self, pane_pid, session="work"):
        self.stub("tmux", "#!/bin/sh\nprintf '%s\\t%s\\n'\n" % (pane_pid, session))
        return os.path.join(self.bin, "tmux")

    def test_a_codex_desktop_thread_in_a_tmux_shell_is_never_killed(self):
        plan = relay.plan_retirement({"agent": "codex", "tmux_session": "work",
                                      "tmux_pane": "%3"}, find_codex=lambda: None)
        self.assertEqual(plan["method"], "none")

    def test_the_predecessors_pane_is_killed_not_the_users_session(self):
        plan = relay.plan_retirement({"agent": "claude", "entrypoint": "cli",
                                      "tmux_session": "work", "tmux_pane": "%3"})
        self.assertEqual(plan["argv"], ["tmux", "kill-pane", "-t", "%3"])
        owned = relay.plan_retirement({"agent": "claude", "entrypoint": "cli",
                                       "tmux_session": "work", "tmux_pane": "%3",
                                       "tmux_owned": True})
        self.assertEqual(owned["argv"], ["tmux", "kill-session", "-t", "=work"])

    def test_an_inherited_tmux_pane_is_ignored_when_the_predecessor_is_not_in_it(self):
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: victim.poll() is None and victim.kill())
        env = {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PID": str(victim.pid),
               "CLAUDE_CODE_ENTRYPOINT": "cli", "TMUX_PANE": "%3", "HOME": self.tmp}
        # The pane's shell is not above the predecessor: an inherited variable.
        pred = relay.detect_predecessor(env, tmux_bin=self.fake_tmux(999999))
        self.assertIsNone(pred["tmux_session"])
        self.assertIsNone(pred["tmux_pane"])
        # The pane's shell IS above it (this test process is its parent).
        pred = relay.detect_predecessor(env, tmux_bin=self.fake_tmux(os.getpid()))
        self.assertEqual((pred["tmux_session"], pred["tmux_pane"], pred["tmux_owned"]),
                         ("work", "%3", False))

    def test_a_tmux_session_the_relay_created_is_recognised(self):
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: victim.poll() is None and victim.kill())
        ledger = os.path.join(self.tmp, "chainT.jsonl")
        relay.append_record(ledger, {"event": "spawn", "chain": "chainT", "generation": 2,
                                     "tmux_session": "work", "mode": "tmux"})
        env = {"CODEX_THREAD_ID": "t", "TMUX_PANE": "%3", "HOME": self.tmp,
               relay.LEDGER_ENV: ledger, relay.CHAIN_ENV: "chainT",
               relay.GENERATION_ENV: "2"}
        pred = relay.detect_predecessor(env, tmux_bin=self.fake_tmux(os.getpid()),
                                        find_codex=lambda: victim.pid)
        self.assertTrue(pred["tmux_owned"])
        self.assertEqual(relay.plan_retirement(pred)["argv"][:2], ["tmux", "kill-session"])



class TestRetriesNeverVerifyAStaleSuccessor(RelayCoreCase):
    """Review finding: a retry from the same predecessor reused its
    generation, _wait took ANY check-in with chain + generation + agent, and
    the exec log was appended to — so a retry verified the stale successor
    of the earlier attempt and the new one's check-in was suppressed."""

    def seed(self, *records):
        folder = os.path.join(self.tmp, ".lastcall", "relay")
        for record in records:
            relay.append_record(os.path.join(folder, "chainR.jsonl"),
                                dict(record, chain="chainR"))
        return folder

    def verified(self):
        return [r for r in self.ledger() if r["event"] == "verified"]

    def test_a_retry_gets_a_new_generation_and_verifies_the_new_successor(self):
        self.seed({"event": "spawn", "generation": 2, "agent": "claude"},
                  {"event": "checkin", "generation": 2, "agent": "claude",
                   "session_id": "stale-session", "via": "hook"})
        result = self.relay(self.repo(), extra={relay.CHAIN_ENV: "chainR",
                                                relay.GENERATION_ENV: "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("handoff 3", result.stdout)
        self.assertNotEqual(self.verified()[0]["session_id"], "stale-session")

    def test_a_stale_checkin_neither_passes_nor_blocks_the_new_one(self):
        self.seed({"event": "checkin", "generation": 2, "agent": "claude",
                   "session_id": "stale-session", "via": "hook"})
        result = self.relay(self.repo(), extra={relay.CHAIN_ENV: "chainR",
                                                relay.GENERATION_ENV: "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("no hook check-in", result.stdout)
        verified = self.verified()[0]["session_id"]
        self.assertNotEqual(verified, "stale-session")
        spawn = [r for r in self.ledger() if r["event"] == "spawn"][0]
        checkin = [r for r in self.ledger() if r["event"] == "checkin"
                   and r["session_id"] == verified][0]
        self.assertTrue(spawn["nonce"])
        self.assertEqual((checkin["nonce"], checkin["via"]), (spawn["nonce"], "hook"))

    def test_an_old_exec_log_is_never_read_for_the_new_thread(self):
        folder = self.seed({"event": "note"})
        with open(os.path.join(folder, "chainR-1.log"), "w") as fh:
            fh.write(json.dumps({"type": "thread.started", "thread_id": "stale-thread"}) + "\n")
        result = self.relay(self.repo(), "--agent", "codex", "--codex-mode", "exec",
                            "--no-name-thread", extra={relay.CHAIN_ENV: "chainR",
                                                       relay.GENERATION_ENV: "0"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotEqual(self.verified()[0]["session_id"], "stale-thread")

    def test_the_exec_fallback_is_a_new_spawn_with_its_own_nonce(self):
        result = self.relay(self.repo(), "--agent", "codex",
                            extra={"FAKE_APP_FAIL": "thread/start"})
        self.assertEqual(result.returncode, 0, result.stdout)
        spawns = [r for r in self.ledger() if r["event"] == "spawn"]
        self.assertEqual([r["mode"] for r in spawns], ["app", "exec"])
        self.assertNotEqual(spawns[0]["nonce"], spawns[1]["nonce"])
        checkin = [r for r in self.ledger() if r["event"] == "checkin"][-1]
        self.assertEqual((checkin["via"], checkin["nonce"]), ("exec-json", spawns[1]["nonce"]))



class TestCommittedMeansCommitted(RelayCoreCase):
    """Review finding: `git status --porcelain` is silent about a gitignored
    file, so a handoff that was never committed passed the check."""

    def test_a_gitignored_handoff_is_not_committed(self):
        repo = self.repo(handoff=None)
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write("docs/handoff/\n")
        subprocess.run(["git", "-C", repo, "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "ignore"], check=True)
        with open(os.path.join(repo, "docs", "handoff", "2026-09-25.md"), "w") as fh:
            fh.write("# Secretly local\n")
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("handoff is not committed", result.stdout)
        self.assertIn("gitignored", result.stdout)

    def test_staged_but_uncommitted_changes_do_not_count(self):
        repo = self.repo()
        with open(os.path.join(repo, "docs", "handoff", "2026-09-25.md"), "a") as fh:
            fh.write("more\n")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        result = self.relay(repo, "--dry-run", "--allow-dirty")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("handoff is not committed", result.stdout)



class TestTheRelayFitsInABashCall(RelayCoreCase):
    """Review finding: the relay's waits (spawn 60s + check-in 180s + remote
    control 45s) could run ~285s, past Claude Code's 2-minute default Bash
    tool timeout, which kills it mid-handover."""

    def plain(self, repo, *args, extra=None):
        return subprocess.run([sys.executable, RELAY, "--repo", repo] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=self.env(extra), cwd=self.tmp, universal_newlines=True)

    def test_by_default_every_wait_together_fits_under_two_minutes(self):
        for agent in ("claude", "codex"):
            result = self.plain(self.repo(name=agent), "--dry-run", "--agent", agent)
            self.assertEqual(result.returncode, 0, result.stdout)
            first = result.stdout.splitlines()[0]
            self.assertIn("run it with a tool timeout above that", first)
            self.assertIn("or in the background", first)
            seconds = int(re.search(r"take up to (\d+)s", first).group(1))
            self.assertLessEqual(seconds, 110, first)

    def test_the_budget_caps_every_wait_and_is_configurable(self):
        repo = self.repo()
        started = time.time()
        result = self.plain(repo, "--timeout", "30", "--max-wait", "1", "--poll", "0.05",
                            extra={"FAKE_NO_HOOK": "1", "FAKE_NO_TRANSCRIPT": "1"})
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertLess(time.time() - started, 15)
        self.assertIn("take up to 1s", result.stdout)
        with open(os.path.join(repo, ".lastcall.json"), "w") as fh:
            json.dump({"relay": {"max_wait_seconds": 50}}, fh)
        result = self.plain(repo, "--dry-run", "--allow-dirty")
        self.assertIn("take up to 50s", result.stdout)


if __name__ == "__main__":
    unittest.main()
