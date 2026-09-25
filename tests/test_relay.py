#!/usr/bin/env python3
"""Tests for relay/handoff.sh — now a deprecated POSIX-sh shim that
maps the old flags and environment onto the relay (lastcall_core/relay.py) and
execs it.

These keep the old behaviour that still exists, driven through the shim:
the durability preconditions, handoff selection, config discovery, git being
optional, model selection and predecessor retirement. What the bash relay did
internally (tmux panes, remain-on-exit, transcript polling for a tool call,
editing ~/.claude.json for workspace trust, bash integer wrapping) is gone;
the relay's own behaviour is covered by test_relay_core.py.

`claude` and `tmux` are fakes on PATH, HOME is a temp dir, and every session
variable of whatever session runs the suite is scrubbed.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "plugins", "lastcall", "relay", "handoff.sh")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_relay_core import FAKE_CLAUDE, scrubbed_environ  # noqa: E402

posix_only = unittest.skipUnless(
    os.name == "posix" and shutil.which("sh") and shutil.which("git"),
    "relay is POSIX + git only")

FAST = ["--poll", "0.05", "--timeout", "5", "--rc-timeout", "1", "--hook-grace", "0.3"]


@posix_only
class RelayCase(unittest.TestCase):
    shell = "sh"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="relay-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.fake_log = os.path.join(self.tmp, "fake.log")
        # tmux stub: names the predecessor's pane process and session,
        # records every kill.
        self.stub("tmux", r'''#!/bin/sh
case "$1" in
    display-message) [ -n "$OLD_SESSION" ] && printf '%s\t%s\n' "$PANE_PID" "$OLD_SESSION" ;;
    kill-session|kill-pane)
        printf '%s\n' "$3" >> "$KILLS"
        echo "no such session" >&2
        exit "${KILL_EXIT:-0}" ;;
esac
exit 0
''')
        self.stub("claude", FAKE_CLAUDE % {"python": sys.executable})

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)

    def git(self, path, *args):
        subprocess.run(("git",) + args, cwd=path, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)

    def repo(self, name="myproject", handoff=True, commit=True, dirty=False):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(path, "docs", "handoff"))
        self.git(path, "init", "-q")
        self.git(path, "config", "user.email", "t@example.com")
        self.git(path, "config", "user.name", "t")
        with open(os.path.join(path, "README.md"), "w") as fh:
            fh.write("seed\n")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-qm", "seed")
        if handoff:
            with open(os.path.join(path, "docs", "handoff", "2026-08-18.md"), "w") as fh:
                fh.write("do the thing\n")
            if commit:
                self.git(path, "add", "-A")
                self.git(path, "commit", "-qm", "handoff")
        if dirty:
            with open(os.path.join(path, "README.md"), "a") as fh:
                fh.write("uncommitted\n")
        return path

    def configured(self, name="myproject", **relay):
        repo = self.repo(name)
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": relay}, fh)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "cfg")
        return repo

    def environ(self, extra=None):
        env = scrubbed_environ()
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["HOME"] = self.tmp
        env["FAKE_LOG"] = self.fake_log
        env.update(extra or {})
        return env

    def run_shim(self, args, cwd=None, env_extra=None):
        result = subprocess.run([self.shell, RELAY] + list(args),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.environ(env_extra), cwd=cwd or self.tmp,
                                universal_newlines=True)
        result.out = result.stdout + result.stderr
        return result

    def relay(self, repo, *args, env_extra=None):
        return self.run_shim(["--repo", repo] + list(args), env_extra=env_extra)

    def ledger(self):
        folder = os.path.join(self.tmp, ".lastcall", "relay")
        records = []
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if name.endswith(".jsonl"):
                with open(os.path.join(folder, name)) as fh:
                    records += [json.loads(line) for line in fh if line.strip()]
        return records


class TestShim(RelayCase):
    def test_it_runs_the_relay(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("relay — claude successor", result.stdout)
        self.assertIn("dry run", result.stdout)

    def test_it_says_once_on_stderr_that_it_is_deprecated(self):
        result = self.relay(self.repo(), "--dry-run")
        notices = [line for line in result.stderr.splitlines() if "deprecated" in line]
        self.assertEqual(len(notices), 1, result.stderr)
        self.assertIn("relay.py", notices[0])
        self.assertNotIn("deprecated", result.stdout)

    def test_exit_codes_pass_through(self):
        self.assertEqual(self.relay(self.repo(commit=False), "--dry-run").returncode, 1)
        result = self.relay(self.repo("p2"), *FAST + ["--timeout", "0.5"],
                            env_extra={"FAKE_NO_HOOK": "1", "FAKE_NO_TRANSCRIPT": "1"})
        self.assertEqual(result.returncode, 2, result.out)

    def test_values_with_spaces_and_quotes_arrive_intact(self):
        result = self.relay(self.repo(), "--dry-run", "--model", "a b'c")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("model:       a b'c", result.stdout)

    def test_new_flags_pass_straight_through(self):
        result = self.relay(self.repo(), "--dry-run", "--agent", "codex",
                            "--codex-mode", "exec")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("relay — codex successor", result.stdout)
        self.assertIn("codex exec --json", result.stdout)

    def test_help_is_the_relays(self):
        result = self.run_shim(["--help"])
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("--retire-predecessor", result.stdout)

    def test_trust_is_ignored_and_claude_json_is_never_touched(self):
        claude_json = os.path.join(self.tmp, ".claude.json")
        with open(claude_json, "w") as handle:
            json.dump({"projects": {}, "numStartups": 42}, handle)
        result = self.relay(self.repo(), "--dry-run", "--trust")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("--trust is ignored", result.stderr)
        with open(claude_json) as handle:
            self.assertEqual(json.load(handle), {"projects": {}, "numStartups": 42})

    def test_environment_maps_to_flags_and_a_flag_wins(self):
        repo = self.repo()
        out = self.relay(repo, "--dry-run", env_extra={"LASTCALL_MODEL": "fable"}).stdout
        self.assertIn("--model fable", out)
        out = self.relay(repo, "--dry-run", "--model", "opus",
                         env_extra={"LASTCALL_MODEL": "fable"}).stdout
        self.assertIn("--model opus", out)
        self.assertNotIn("--model fable", out)

    def test_on_off_environment_never_collides_with_the_flag(self):
        """relay.py refuses --x together with --no-x; the shim must not
        produce that pair from an environment variable plus a flag."""
        repo = self.repo()
        env = {"LASTCALL_SKIP_PERMISSIONS": "1"}
        self.assertIn("permissions: SKIPPED",
                      self.relay(repo, "--dry-run", env_extra=env).stdout)
        result = self.relay(repo, "--dry-run", "--no-skip-permissions", env_extra=env)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("permissions: normal", result.stdout)
        off = self.relay(repo, "--dry-run", env_extra={"LASTCALL_REMOTE_CONTROL": "0"})
        self.assertIn("remote ctl:  off", off.stdout)

    def test_a_nonsense_timeout_is_a_precondition_failure(self):
        result = self.relay(self.repo(), "--dry-run", "--timeout", "abc")
        self.assertEqual(result.returncode, 1, result.out)
        result = self.relay(self.repo("p2"), "--dry-run", env_extra={"TIMEOUT": "abc"})
        self.assertEqual(result.returncode, 1, result.out)

    def test_missing_python_is_reported(self):
        result = self.relay(self.repo(), "--dry-run",
                            env_extra={"PYTHON_BIN": "definitely-not-python"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("python3 not found", result.stderr)


@unittest.skipUnless(shutil.which("dash"), "dash not installed")
class TestShimUnderDash(TestShim):
    """dash is the strictest common /bin/sh: no arrays, no [[ ]], no ${!x}."""
    shell = "dash"


class TestPreconditions(RelayCase):
    def test_clean_repo_with_committed_handoff_resolves(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.out)

    def test_refuses_when_the_handoff_is_not_committed(self):
        """The load-bearing rule: never spawn before the handoff is durable."""
        result = self.relay(self.repo(commit=False), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not committed", result.stdout)

    def test_refuses_when_there_is_no_handoff_at_all(self):
        result = self.relay(self.repo(handoff=False), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no handoff files", result.stdout)

    def test_refuses_a_dirty_tree(self):
        result = self.relay(self.repo(dirty=True), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tree is dirty", result.stdout)

    def test_allow_dirty_overrides(self):
        result = self.relay(self.repo(dirty=True), "--dry-run", "--allow-dirty")
        self.assertEqual(result.returncode, 0, result.out)

    def test_dirty_baseline_exempts_named_paths(self):
        result = self.relay(self.repo(dirty=True), "--dry-run",
                            env_extra={"LASTCALL_DIRTY_BASELINE": "README.md"})
        self.assertEqual(result.returncode, 0, result.out)

    def test_a_non_git_directory_is_not_refused_outright(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        result = self.relay(plain, "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("not a git worktree", result.stdout)
        self.assertIn("no handoff files", result.stdout)


class TestResolution(RelayCase):
    def test_session_name_is_derived_from_the_repo(self):
        result = self.relay(self.repo("widgets"), "--dry-run")
        self.assertIn("name:        widgets · handoff 1", result.stdout)

    def test_permissions_are_normal_unless_explicitly_skipped(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertIn("permissions: normal", result.stdout)
        self.assertNotIn("--dangerously-skip-permissions", result.stdout)

    def test_skip_permissions_is_opt_in_and_visible(self):
        result = self.relay(self.repo(), "--dry-run", "--skip-permissions")
        self.assertIn("permissions: SKIPPED", result.stdout)
        self.assertIn("--dangerously-skip-permissions", result.stdout)

    def test_custom_handoff_dir(self):
        repo = self.repo()
        os.makedirs(os.path.join(repo, "notes"))
        with open(os.path.join(repo, "notes", "next.md"), "w") as fh:
            fh.write("go\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "notes")
        result = self.relay(repo, "--dry-run", "--handoff-dir", "notes")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("notes/next.md", result.stdout)

    def add_template(self, repo, commit=True):
        """A TEMPLATE.md edited after the real handoff, so it is the newest."""
        path = os.path.join(repo, "docs", "handoff", "TEMPLATE.md")
        with open(path, "w") as fh:
            fh.write("# Session handoff — YYYY-MM-DD\n")
        later = os.path.getmtime(os.path.join(repo, "README.md")) + 60
        os.utime(path, (later, later))
        if commit:
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-qm", "template")

    def test_the_template_is_never_picked_as_the_newest_handoff(self):
        repo = self.repo()
        self.add_template(repo)
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("handoff:     %s" % os.path.join(
            repo, "docs", "handoff", "2026-08-18.md"), result.stdout)
        self.assertNotIn("TEMPLATE.md", result.stdout)

    def test_a_template_alone_is_not_a_handoff(self):
        repo = self.repo(handoff=False)
        self.add_template(repo)
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no handoff files", result.stdout)

    def test_the_committed_fallback_never_suggests_the_template(self):
        repo = self.repo()
        self.add_template(repo)
        with open(os.path.join(repo, "docs", "handoff", "2026-08-19.md"), "w") as fh:
            fh.write("not yet committed\n")
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 1, result.out)
        self.assertIn("newest committed handoff is docs/handoff/2026-08-18.md", result.stdout)
        self.assertNotIn("TEMPLATE.md", result.stdout)

    def test_nothing_is_spawned_on_a_dry_run(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertNotIn("spawned:", result.stdout)
        self.assertFalse(os.path.exists(self.fake_log))


class TestConfigResolution(RelayCase):
    """Where the config lives and which repo to hand over are DIFFERENT
    questions: a session run from a parent directory holding two repos must
    still read the parent's config."""

    def parent_layout(self, config):
        parent = os.path.join(self.tmp, "workspace")
        os.makedirs(os.path.join(parent, ".claude"))
        repo = self.repo("frontend")
        os.rename(repo, os.path.join(parent, "frontend"))
        repo = os.path.join(parent, "frontend")
        config.setdefault("repo", repo)
        with open(os.path.join(parent, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": config}, fh)
        return parent, repo

    def run_from(self, cwd, *args):
        return self.run_shim(["--dry-run"] + list(args), cwd=cwd)

    def test_config_is_found_by_walking_up_from_the_working_directory(self):
        parent, _repo = self.parent_layout({"name_prefix": "amos", "skip_permissions": True})
        result = self.run_from(parent)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("workspace/.claude/lastcall.json", result.stdout)
        self.assertIn("permissions: SKIPPED", result.stdout)
        self.assertIn("name:        amos · handoff 1", result.stdout)

    def test_repo_can_come_from_the_config(self):
        parent, repo = self.parent_layout({"name_prefix": "amos"})
        result = self.run_from(parent)
        self.assertIn("repo:        %s" % os.path.realpath(repo), result.stdout)

    def test_config_dir_flag_wins(self):
        parent, repo = self.parent_layout({"name_prefix": "fromparent"})
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(elsewhere, ".claude"))
        with open(os.path.join(elsewhere, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"repo": repo, "name_prefix": "fromflag"}}, fh)
        result = self.run_from(parent, "--config-dir", elsewhere)
        self.assertIn("name:        fromflag · handoff 1", result.stdout)
        env = self.run_shim(["--dry-run"], cwd=parent,
                            env_extra={"LASTCALL_CONFIG_DIR": elsewhere})
        self.assertIn("name:        fromflag · handoff 1", env.stdout)

    def test_config_beside_the_repo_still_works(self):
        repo = self.configured(name_prefix="besiderepo")
        result = self.run_from(self.tmp, "--repo", repo)
        self.assertIn("name:        besiderepo · handoff 1", result.stdout)

    def test_a_config_in_the_home_claude_directory_is_not_picked_up(self):
        """~/.claude is Claude Code's user directory, not a project."""
        os.makedirs(os.path.join(self.tmp, ".claude"))
        with open(os.path.join(self.tmp, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"name_prefix": "fromhome"}}, fh)
        repo = self.repo()
        result = self.run_from(repo, "--repo", repo)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("<none found>", result.stdout)
        self.assertNotIn("fromhome", result.stdout)

    def test_no_config_anywhere_still_runs_on_defaults(self):
        repo = self.repo()
        result = self.run_from(self.tmp, "--repo", repo)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("<none found>", result.stdout)


class TestGitIsOptional(RelayCase):
    def plain_dir(self, name="workspace"):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(path, "docs", "handoff"))
        os.makedirs(os.path.join(path, ".claude"))
        with open(os.path.join(path, "docs", "handoff", "2026-08-19.md"), "w") as fh:
            fh.write("next steps\n")
        with open(os.path.join(path, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"name_prefix": "plain"}}, fh)
        return path

    def run_from(self, cwd, *args, env_extra=None):
        return self.run_shim(["--dry-run"] + list(args), cwd=cwd, env_extra=env_extra)

    def test_a_plain_directory_can_hand_over(self):
        result = self.run_from(self.plain_dir())
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("name:        plain · handoff 1", result.stdout)

    def test_it_says_loudly_what_it_could_not_verify(self):
        out = self.run_from(self.plain_dir()).stdout
        self.assertIn("not a git repository", out)
        self.assertIn("SKIPPED", out)

    def test_running_from_the_directory_needs_no_flags(self):
        path = self.plain_dir()
        self.assertIn("repo:        %s" % os.path.realpath(path), self.run_from(path).stdout)

    def test_require_git_restores_the_strict_behaviour(self):
        path = self.plain_dir()
        for result in (self.run_from(path, "--require-git"),
                       self.run_from(path, env_extra={"LASTCALL_REQUIRE_GIT": "1"})):
            self.assertEqual(result.returncode, 1, result.out)
            self.assertIn("not a git worktree", result.stdout)

    def test_a_git_repo_still_enforces_the_committed_handoff_rule(self):
        repo = self.repo(commit=False)
        result = self.run_from(self.tmp, "--repo", repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not committed", result.stdout)


class TestModelSelection(RelayCase):
    def test_no_model_configured_means_no_flag(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertNotIn("--model", result.stdout)
        self.assertIn("model:       <default>", result.stdout)

    def test_model_from_config_reaches_argv(self):
        self.assertIn("--model fable",
                      self.relay(self.configured(model="fable"), "--dry-run").stdout)

    def test_fallback_list_reaches_argv(self):
        out = self.relay(self.configured(model="fable", fallback_model="opus,sonnet"),
                         "--dry-run").stdout
        self.assertIn("--model fable --fallback-model opus,sonnet", out)

    def test_flag_overrides_the_config(self):
        out = self.relay(self.configured(model="fable"), "--dry-run", "--model", "opus").stdout
        self.assertIn("--model opus", out)
        self.assertNotIn("--model fable", out)

    def test_model_is_reported_before_spawning(self):
        out = self.relay(self.configured(model="opus", fallback_model="sonnet"),
                         "--dry-run").stdout
        self.assertIn("model:       opus  fallback: sonnet", out)


class TestPredecessorRetirement(RelayCase):
    """Retirement happens only after the successor checked in, detached,
    because the relay runs inside the session it retires."""

    # A Claude CLI predecessor that really runs in the pane: the pane's
    # process is an ancestor of (here: the same as) CLAUDE_PID.
    TMUX = {"TMUX_PANE": "%1", "OLD_SESSION": "old-session", "PANE_PID": str(os.getpid()),
            "CLAUDE_CODE_SESSION_ID": "pred-session", "CLAUDE_PID": str(os.getpid()),
            "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def test_off_by_default(self):
        out = self.relay(self.repo(), "--dry-run", env_extra=self.TMUX).stdout
        self.assertIn("retire:      no — not requested", out)

    def test_config_turns_it_on(self):
        out = self.relay(self.configured(kill_predecessor=True), "--dry-run",
                         env_extra=self.TMUX).stdout
        self.assertIn("tmux kill-pane -t %1", out)

    def test_flag_and_environment_map_to_retire_predecessor(self):
        repo = self.repo()
        for args, env in ((["--kill-predecessor"], {}),
                          ([], {"LASTCALL_KILL_PREDECESSOR": "1"})):
            env = dict(self.TMUX, **env)
            out = self.relay(repo, "--dry-run", *args, env_extra=env).stdout
            self.assertIn("tmux kill-pane -t %1", out, (args, env))

    def test_flag_can_turn_it_back_off(self):
        env = dict(self.TMUX, LASTCALL_KILL_PREDECESSOR="1")
        result = self.relay(self.configured(kill_predecessor=True), "--dry-run",
                            "--no-kill-predecessor", env_extra=env)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("retire:      no — not requested", result.stdout)

    def test_the_successor_is_not_asked_to_kill(self):
        out = self.relay(self.configured(kill_predecessor=True), "--dry-run",
                         env_extra=self.TMUX).stdout
        self.assertIn("being retired automatically", out)
        self.assertNotIn("and only then run: tmux kill-session", out)

    def handover(self, pane, kill_exit=0):
        self.kills = os.path.join(self.tmp, "kills")
        repo = self.configured(kill_predecessor=True)
        return self.relay(repo, *FAST, env_extra=dict(
            self.TMUX, TMUX_PANE=pane, KILLS=self.kills,
            KILL_EXIT=str(kill_exit), LASTCALL_KILL_DELAY="0"))

    def outcome(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            done = [r for r in self.ledger() if r["event"] in ("retired", "retire-failed")]
            if done:
                return done[0]
            time.sleep(0.05)
        self.fail("no retirement outcome in the ledger: %s" % self.ledger())

    def test_the_predecessor_is_actually_retired(self):
        result = self.handover("%1")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertEqual(self.outcome()["event"], "retired")
        with open(self.kills) as handle:
            self.assertEqual(handle.read(), "%1\n")

    def test_a_session_name_is_never_run_as_shell(self):
        marker = os.path.join(self.tmp, "pwned")
        old = "x'; touch %s; '" % marker
        result = self.handover(old)
        self.assertEqual(result.returncode, 0, result.out)
        self.outcome()
        with open(self.kills) as handle:
            self.assertEqual(handle.read(), "%s\n" % old)
        self.assertFalse(os.path.exists(marker))

    def test_a_failed_kill_is_logged_as_a_failure(self):
        result = self.handover("%1", kill_exit=1)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("retirement outcome will be logged", result.stdout)
        outcome = self.outcome()
        self.assertEqual((outcome["event"], outcome["exit"]), ("retire-failed", 1))
        self.assertIn("no such session", outcome["detail"])

    def test_nothing_is_retired_on_a_dry_run(self):
        out = self.relay(self.configured(kill_predecessor=True), "--dry-run",
                         env_extra=self.TMUX).stdout
        self.assertNotIn("retiring predecessor", out)
        self.assertIn("nothing spawned", out)


@posix_only
class TestInstallerSafety(unittest.TestCase):
    """Both reported from real use against a settings.json holding a live key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, ".claude"))
        self.settings = os.path.join(self.tmp, ".claude", "settings.json")

    def write(self, data):
        with open(self.settings, "w") as handle:
            json.dump(data, handle)
        os.chmod(self.settings, 0o644)

    def install(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "install.py"), "--dir", self.tmp]
            + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_backup_of_a_secret_bearing_settings_is_owner_only(self):
        """--global targets the file most likely to hold an API key. Copying it
        at 644 would leave a second world-readable copy of a live secret."""
        self.write({"env": {"OPENAI_API_KEY": "sk-live-example"}})
        self.install()
        backup = self.settings + ".lastcall.bak"
        self.assertTrue(os.path.isfile(backup))
        mode = stat.S_IMODE(os.stat(backup).st_mode)
        self.assertEqual(mode & 0o077, 0, "backup is readable by others: %o" % mode)

    def test_an_unrelated_hook_mentioning_the_script_name_survives(self):
        """Matching the bare string 'lastcall.py' stripped any hook containing
        it, including someone else's wrapper."""
        foreign = "/opt/tools/my-lastcall.py-wrapper --verbose"
        self.write({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": foreign}]}]}})
        self.install()
        self.install("--uninstall")
        with open(self.settings) as handle:
            after = json.load(handle)
        commands = [entry["command"]
                    for groups in after.get("hooks", {}).values()
                    for group in groups for entry in group["hooks"]]
        self.assertIn(foreign, commands)

    def test_install_then_uninstall_leaves_no_trace_of_ours(self):
        self.write({"permissions": {"allow": ["Bash"]}})
        self.install()
        self.install("--uninstall")
        with open(self.settings) as handle:
            after = json.load(handle)
        self.assertNotIn("hooks", after)
        self.assertEqual(after["permissions"], {"allow": ["Bash"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
