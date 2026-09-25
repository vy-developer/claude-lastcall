#!/usr/bin/env python3
"""Tests for relay v2 (plugins/lastcall/lib/lastcall/relay.py).

`claude`, `codex` and `tmux` are fake executables on PATH, HOME is a temp dir,
and every session variable of whatever session runs the suite is scrubbed —
nothing here spawns a real session or can retire a real one.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "plugins", "lastcall", "lib")
RELAY = os.path.join(LIB, "lastcall", "relay.py")
# Loaded by path, not as `lastcall.relay`: test_lastcall.py puts scripts/ on
# sys.path, and its `lastcall` module shadows the `lastcall` package.
_spec = importlib.util.spec_from_file_location("lastcall_relay_under_test", RELAY)
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

posix_only = unittest.skipUnless(
    os.name == "posix" and shutil.which("git"), "relay is POSIX + git only")

FAKE_CLAUDE = r'''#!%(python)s
import json, os, re, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd(),
                         "leak": os.environ.get("CLAUDE_CODE_SESSION_ID"),
                         "bedrock": os.environ.get("CLAUDE_CODE_USE_BEDROCK"),
                         "chain": os.environ.get("LASTCALL_RELAY_CHAIN")}) + "\n")
if args[:1] and args[0] in ("stop", "rm", "logs", "attach"):
    sys.exit(0)
if "--bg" not in args:
    sys.exit(0)
if os.environ.get("FAKE_UNTRUSTED"):
    print("Workspace not trusted. Run `claude` in %%s once and accept the trust prompt, then retry." %% os.getcwd())
    sys.exit(1)
sid = args[args.index("--session-id") + 1]
settings = json.loads(args[args.index("--settings") + 1])
home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
folder = os.path.join(home, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
os.makedirs(folder, exist_ok=True)
transcript = os.path.join(folder, sid + ".jsonl")
if not os.environ.get("FAKE_NO_TRANSCRIPT"):
    with open(transcript, "w") as fh:
        fh.write(json.dumps({"type": "custom-title", "sessionId": sid,
                             "customTitle": args[args.index("-n") + 1]}) + "\n")
        if "--remote-control" in args and not os.environ.get("FAKE_NO_BRIDGE"):
            fh.write(json.dumps({"type": "bridge-session", "sessionId": sid,
                                 "bridgeSessionId": "cse_fake", "lastSequenceNum": 0}) + "\n")
if not os.environ.get("FAKE_NO_HOOK"):
    payload = json.dumps({"session_id": sid, "transcript_path": transcript, "cwd": os.getcwd(),
                          "source": "startup", "hook_event_name": "SessionStart"})
    for group in settings["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            subprocess.run(hook["command"], shell=True, input=payload, universal_newlines=True)
print("Started background session %%s" %% sid[:8])
'''

FAKE_CODEX = r'''#!%(python)s
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
if args[:1] == ["exec"]:
    if os.environ.get("FAKE_CODEX_DIE"):
        print("boom")
        sys.exit(3)
    tid = "01a0d9b2-0000-7000-8000-%%012d" %% os.getpid()
    home = os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex")
    day = os.path.join(home, "sessions", "2026", "09", "25")
    os.makedirs(day, exist_ok=True)
    with open(os.path.join(day, "rollout-2026-09-25T00-00-00-%%s.jsonl" %% tid), "w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {
            "id": tid, "cwd": os.getcwd(), "source": "exec", "originator": "codex_exec"}}) + "\n")
    print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
    time.sleep(0.2)
    print(json.dumps({"type": "turn.completed"}), flush=True)
    sys.exit(0)
if args[:1] == ["app-server"]:
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if message.get("method") == "thread/name/set":
            with open(os.environ["FAKE_NAMES"], "a") as fh:
                fh.write(json.dumps(message["params"]) + "\n")
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
'''


def scrubbed_environ():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "CODEX", "LASTCALL", "TMUX"))}
    return env


@posix_only
class RelayV2Case(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="relay2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "fake.log")
        self.names = os.path.join(self.tmp, "names.log")
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
                    "FAKE_LOG": self.log, "FAKE_NAMES": self.names})
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

    def ledger(self):
        folder = os.path.join(self.tmp, ".lastcall", "relay")
        records = []
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if name.endswith(".jsonl"):
                records += relay.read_ledger(os.path.join(folder, name))
        return records


class TestHandoffAndNaming(RelayV2Case):
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


class TestCommands(RelayV2Case):
    def opts(self, **kw):
        base = dict(claude_bin="claude", codex_bin="codex", remote_control=True, model=None,
                    fallback_model=None, skip_permissions=False, permission_mode=None,
                    codex_mode="exec", codex_sandbox="workspace-write")
        base.update(kw)
        return type("Opts", (), base)()

    def test_claude_argv_is_background_named_remote_controlled_and_pinned(self):
        argv = relay.claude_argv(self.opts(model="sonnet", fallback_model="haiku"),
                                 "p · handoff 1", "SID", "PROMPT", "{}")
        self.assertEqual(argv[:4], ["claude", "--bg", "-n", "p · handoff 1"])
        self.assertEqual(argv[argv.index("--remote-control") + 1], "p · handoff 1")
        self.assertEqual(argv[argv.index("--session-id") + 1], "SID")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "haiku")
        self.assertEqual(argv[-1], "PROMPT")
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_claude_argv_without_remote_control_and_unattended(self):
        argv = relay.claude_argv(self.opts(remote_control=False, skip_permissions=True),
                                 "n", "SID", "P", "{}")
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

    def test_successor_env_drops_session_identity_but_keeps_provider_settings(self):
        env = relay.successor_env({"CLAUDE_CODE_SESSION_ID": "x", "CLAUDECODE": "1",
                                   "CLAUDE_CODE_MESSAGING_SOCKET": "/s", "CODEX_THREAD_ID": "t",
                                   "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/bin",
                                   "LASTCALL_RELAY_CHAIN": "old"},
                                  {"LASTCALL_RELAY_CHAIN": "new"})
        self.assertEqual(env, {"CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/bin",
                               "LASTCALL_RELAY_CHAIN": "new"})


class TestPreconditions(RelayV2Case):
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


class TestClaudeRelay(RelayV2Case):
    def test_successor_checks_in_through_its_hook_and_remote_control_is_proven(self):
        result = self.relay(self.repo(), extra={"CLAUDE_CODE_SESSION_ID": "leaky",
                                                "CLAUDE_CODE_USE_BEDROCK": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via hook", result.stdout)
        self.assertIn("remote control: connected", result.stdout)
        spawn = [c for c in self.calls() if "--bg" in c["argv"]][0]
        self.assertIsNone(spawn["leak"], "predecessor session id leaked into the successor")
        self.assertEqual(spawn["bedrock"], "1")
        events = [r["event"] for r in self.ledger()]
        self.assertEqual(events, ["spawn", "checkin", "verified"])

    def test_missing_bridge_session_is_reported_loudly(self):
        result = self.relay(self.repo(), extra={"FAKE_NO_BRIDGE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("remote control did NOT connect", result.stdout)

    def test_required_remote_control_turns_that_into_exit_2(self):
        result = self.relay(self.repo(), "--require-remote-control",
                            extra={"FAKE_NO_BRIDGE": "1"})
        self.assertEqual(result.returncode, 2, result.stdout)

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


class TestCodexRelay(RelayV2Case):
    def test_exec_successor_checks_in_and_is_named(self):
        result = self.relay(self.repo(), "--agent", "codex", "--model", "gpt-x")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("via exec-json", result.stdout)
        with open(self.names) as fh:
            named = json.loads(fh.readline())
        self.assertEqual(named["name"], "proj · handoff 1 · Ship the parser")
        checkin = [r for r in self.ledger() if r["event"] == "checkin"][0]
        self.assertTrue(checkin["transcript_path"].endswith(checkin["session_id"] + ".jsonl"))

    def test_exec_that_dies_before_a_thread_is_exit_2(self):
        result = self.relay(self.repo(), "--agent", "codex", extra={"FAKE_CODEX_DIE": "1"})
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("exited before starting a thread", result.stdout)


class TestCheckinAndRetirement(RelayV2Case):
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
        self.assertEqual(relay.plan_retirement({"tmux_session": "t", "agent": "claude",
                                                "entrypoint": "cli"})["method"], "tmux")
        self.assertEqual(relay.plan_retirement({"agent": "claude", "entrypoint": "cli",
                                                "pid": 42, "kind": "interactive"})["pid"], 42)
        self.assertEqual(relay.plan_retirement({"agent": "codex"},
                                               find_codex=lambda: None)["method"], "none")
        self.assertEqual(relay.plan_retirement({})["method"], "none")


if __name__ == "__main__":
    unittest.main()
