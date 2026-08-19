#!/usr/bin/env python3
"""Tests for the optional relay (plugins/lastcall/relay/handoff.sh).

POSIX only — the relay is tmux-only by design and the guard does not need it.
tmux and the claude CLI are stubbed, so nothing here spawns a real session; the
tests drive the precondition logic, which is the part that protects you from
handing over work that was never committed.
"""

import json
import stat
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "plugins", "lastcall", "relay", "handoff.sh")

posix_only = unittest.skipUnless(
    os.name == "posix" and shutil.which("bash") and shutil.which("git"),
    "relay is POSIX + bash + git only")


@posix_only
class RelayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        # tmux stub: has-session always fails, so a fresh name is always free.
        self.stub("tmux", "#!/bin/sh\ncase \"$1\" in has-session) exit 1 ;; esac\nexit 0\n")
        self.stub("claude", "#!/bin/sh\nexit 0\n")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)

    def repo(self, name="myproject", handoff=True, commit=True, dirty=False):
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
        if handoff:
            with open(os.path.join(path, "docs", "handoff", "2026-08-18.md"), "w") as fh:
                fh.write("do the thing\n")
            if commit:
                run("git", "add", "-A")
                run("git", "commit", "-qm", "handoff")
        if dirty:
            with open(os.path.join(path, "README.md"), "a") as fh:
                fh.write("uncommitted\n")
        return path

    def relay(self, repo, *args, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["HOME"] = self.tmp
        env.pop("TMUX_PANE", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.update(env_extra or {})
        return subprocess.run(["bash", RELAY, "--repo", repo] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, cwd=self.tmp)


class TestPreconditions(RelayCase):
    def test_clean_repo_with_committed_handoff_resolves(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(b"dry run", result.stdout)

    def test_refuses_when_the_handoff_is_not_committed(self):
        """The load-bearing rule: never spawn before the handoff is durable."""
        result = self.relay(self.repo(commit=False), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"not committed", result.stdout)

    def test_refuses_when_there_is_no_handoff_at_all(self):
        result = self.relay(self.repo(handoff=False), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"no handoff files", result.stdout)

    def test_refuses_a_dirty_tree(self):
        result = self.relay(self.repo(dirty=True), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"tree is dirty", result.stdout)

    def test_allow_dirty_overrides(self):
        result = self.relay(self.repo(dirty=True), "--dry-run", "--allow-dirty")
        self.assertEqual(result.returncode, 0, result.stdout.decode())

    def test_dirty_baseline_exempts_named_paths(self):
        repo = self.repo(dirty=True)
        result = self.relay(repo, "--dry-run",
                            env_extra={"LASTCALL_DIRTY_BASELINE": "README.md"})
        self.assertEqual(result.returncode, 0, result.stdout.decode())

    def test_a_non_git_directory_is_no_longer_refused_outright(self):
        """Changed deliberately: git is how the handoff is verified durable, not
        a requirement to hand over at all. A plain directory now proceeds with a
        loud warning, and fails only for a real reason — here, no handoff."""
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        result = self.relay(plain, "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"not a git worktree", result.stdout)
        self.assertIn(b"no handoff files", result.stdout)
        self.assertIn(b"not a git repository", result.stdout)

    def test_refuses_when_tmux_is_missing(self):
        os.remove(os.path.join(self.bin, "tmux"))
        env = dict(os.environ)
        result = self.relay(self.repo(), "--dry-run",
                            env_extra={"TMUX_BIN": "definitely-not-tmux"})
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"tmux not found", result.stdout)

    def test_rejects_a_nonsense_timeout_before_spawning(self):
        result = self.relay(self.repo(), "--dry-run", "--timeout", "abc")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"whole number", result.stdout)

    def test_rejects_a_wrapping_timeout(self):
        """Bash integer arithmetic is bounded; a huge value wraps negative and
        would otherwise fail at `sleep` with a session already spawned."""
        result = self.relay(self.repo(), "--dry-run",
                            "--timeout", "9223372036854775808")
        self.assertEqual(result.returncode, 1)


class TestResolution(RelayCase):
    def test_slug_replaces_every_non_alphanumeric_not_just_slashes(self):
        """A repo named with an underscore must map to the same project dir
        Claude Code uses, or the launcher watches a transcript that never
        appears and reports a false timeout."""
        repo = self.repo("my_project.v2")
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        out = result.stdout.decode()
        self.assertIn("my-project-v2", out)
        self.assertNotIn("my_project.v2/", out.split("transcript:")[1][:200])

    def test_session_name_is_derived_from_the_repo(self):
        result = self.relay(self.repo("widgets"), "--dry-run")
        self.assertRegex(result.stdout.decode(), r"successor:\s+widgets-\d{4}-\d{4}")

    def test_permissions_are_normal_unless_explicitly_skipped(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertIn(b"permissions: normal", result.stdout)
        self.assertNotIn(b"--dangerously-skip-permissions", result.stdout)

    def test_skip_permissions_is_opt_in_and_visible(self):
        result = self.relay(self.repo(), "--dry-run", "--skip-permissions")
        self.assertIn(b"permissions: SKIPPED", result.stdout)
        self.assertIn(b"--dangerously-skip-permissions", result.stdout)

    def test_custom_handoff_dir(self):
        repo = self.repo()
        os.makedirs(os.path.join(repo, "notes"))
        with open(os.path.join(repo, "notes", "next.md"), "w") as fh:
            fh.write("go\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-qm", "notes"], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL)
        result = self.relay(repo, "--dry-run", "--handoff-dir", "notes")
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(b"notes/next.md", result.stdout)

    def test_nothing_is_spawned_on_a_dry_run(self):
        result = self.relay(self.repo(), "--dry-run")
        self.assertNotIn(b"spawned", result.stdout.replace(b"nothing spawned", b""))


if __name__ == "__main__":
    unittest.main(verbosity=2)


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


@posix_only
class TestWorkspaceTrust(RelayCase):
    """Measured 2026-08-19 with a real spawn: Claude Code asks "is this a
    project you trust?" the first time it opens a directory, and
    --dangerously-skip-permissions does NOT bypass it. The successor sat on
    that prompt for 150 seconds, produced no transcript, and the launcher
    reported a timeout that said nothing about the cause."""

    def relay(self, repo, *args, env_extra=None):
        # Point HOME at the sandbox so ~/.claude.json is ours, not the user's.
        extra = {"HOME": self.tmp}
        extra.update(env_extra or {})
        return super(TestWorkspaceTrust, self).relay(repo, *args, env_extra=extra)

    def write_claude_json(self, entries):
        with open(os.path.join(self.tmp, ".claude.json"), "w") as handle:
            json.dump({"projects": entries}, handle)

    def test_untrusted_workspace_aborts_before_spawning(self):
        repo = self.repo()
        self.write_claude_json({})
        result = self.relay(repo)
        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertIn(b"untrusted workspace", result.stdout)
        self.assertIn(b"does not cover it", result.stdout)

    def test_trusted_workspace_passes_the_check(self):
        repo = self.repo()
        self.write_claude_json({os.path.realpath(repo): {"hasTrustDialogAccepted": True}})
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(b"trust: already accepted", result.stdout)

    def test_trust_flag_records_it(self):
        repo = self.repo()
        self.write_claude_json({})
        result = self.relay(repo, "--dry-run", "--trust")
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(b"trust: granted", result.stdout)
        with open(os.path.join(self.tmp, ".claude.json")) as handle:
            saved = json.load(handle)
        self.assertTrue(
            saved["projects"][os.path.realpath(repo)]["hasTrustDialogAccepted"])

    def test_trust_flag_preserves_everything_else(self):
        """~/.claude.json holds the user's entire Claude Code state. Writing one
        key must not lose the rest."""
        repo = self.repo()
        with open(os.path.join(self.tmp, ".claude.json"), "w") as handle:
            json.dump({"projects": {"/other": {"hasTrustDialogAccepted": True}},
                       "numStartups": 42, "oauthAccount": {"x": 1}}, handle)
        self.relay(repo, "--dry-run", "--trust")
        with open(os.path.join(self.tmp, ".claude.json")) as handle:
            saved = json.load(handle)
        self.assertEqual(saved["numStartups"], 42)
        self.assertEqual(saved["oauthAccount"], {"x": 1})
        self.assertTrue(saved["projects"]["/other"]["hasTrustDialogAccepted"])

    def test_unreadable_claude_json_does_not_block(self):
        """Cannot tell is not the same as untrusted; do not refuse on a guess."""
        repo = self.repo()
        with open(os.path.join(self.tmp, ".claude.json"), "w") as handle:
            handle.write("{not json")
        result = self.relay(repo, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(b"could not read", result.stdout)


@posix_only
class TestConfigResolution(RelayCase):
    """Where the config lives and which repo to hand over are DIFFERENT
    questions. Deriving one from the other meant a session run from a parent
    directory holding two repos read no config at all — and because
    remote_control defaults to on, the dry run still printed "remote control:
    on" and looked correct. Reported from real use."""

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
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["HOME"] = self.tmp
        env.pop("TMUX_PANE", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        with open(os.path.join(self.tmp, ".claude.json"), "w") as handle:
            json.dump({"projects": {}}, handle)
        return subprocess.run(["bash", RELAY, "--dry-run", "--trust"] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, cwd=cwd)

    def test_config_is_found_by_walking_up_from_the_working_directory(self):
        parent, repo = self.parent_layout(
            {"name_prefix": "amos", "skip_permissions": True})
        result = self.run_from(parent)
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        out = result.stdout.decode()
        self.assertIn("workspace/.claude/lastcall.json", out)
        self.assertIn("permissions: SKIPPED", out)
        self.assertRegex(out, r"successor:\s+amos-")

    def test_repo_can_come_from_the_config(self):
        parent, repo = self.parent_layout({"name_prefix": "amos"})
        result = self.run_from(parent)
        self.assertIn("repo: %s" % os.path.realpath(repo), result.stdout.decode())

    def test_config_dir_flag_wins(self):
        parent, repo = self.parent_layout({"name_prefix": "fromparent"})
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(elsewhere, ".claude"))
        with open(os.path.join(elsewhere, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"repo": repo, "name_prefix": "fromflag"}}, fh)
        result = self.run_from(parent, "--config-dir", elsewhere)
        self.assertRegex(result.stdout.decode(), r"successor:\s+fromflag-")

    def test_config_beside_the_repo_still_works(self):
        """Backwards compatibility with the layout the old code assumed."""
        repo = self.repo()
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"name_prefix": "besiderepo"}}, fh)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-qm", "cfg"], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL)
        result = self.run_from(self.tmp, "--repo", repo)
        self.assertRegex(result.stdout.decode(), r"successor:\s+besiderepo-")

    def test_no_config_anywhere_still_runs_on_defaults(self):
        repo = self.repo()
        result = self.run_from(self.tmp, "--repo", repo)
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("<none found>", result.stdout.decode())


@posix_only
class TestGitIsOptional(RelayCase):
    """Requiring a git worktree turned "I cannot verify this" into "you may not
    proceed". A session run from a plain directory — the common case when one
    session owns several repos — could not hand over at all."""

    def plain_dir(self, name="workspace"):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(path, "docs", "handoff"))
        os.makedirs(os.path.join(path, ".claude"))
        with open(os.path.join(path, "docs", "handoff", "2026-08-19.md"), "w") as fh:
            fh.write("next steps\n")
        with open(os.path.join(path, ".claude", "lastcall.json"), "w") as fh:
            json.dump({"relay": {"name_prefix": "plain"}}, fh)
        return path

    def run_from(self, cwd, *args):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["HOME"] = self.tmp
        env.pop("TMUX_PANE", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        with open(os.path.join(self.tmp, ".claude.json"), "w") as handle:
            json.dump({"projects": {}}, handle)
        return subprocess.run(["bash", RELAY, "--dry-run", "--trust"] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, cwd=cwd)

    def test_a_plain_directory_can_hand_over(self):
        path = self.plain_dir()
        result = self.run_from(path)
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertRegex(result.stdout.decode(), r"successor:\s+plain-")

    def test_it_says_loudly_what_it_could_not_verify(self):
        result = self.run_from(self.plain_dir())
        out = result.stdout.decode()
        self.assertIn("not a git repository", out)
        self.assertIn("SKIPPED", out)

    def test_running_from_the_directory_needs_no_flags(self):
        """The obvious case: hand over the folder you are sitting in."""
        path = self.plain_dir()
        result = self.run_from(path)
        # realpath, because the relay resolves the physical directory and macOS
        # symlinks /var to /private/var. Resolving is correct; comparing the
        # unresolved path is what was wrong.
        self.assertIn("repo: %s" % os.path.realpath(path), result.stdout.decode())

    def test_require_git_restores_the_strict_behaviour(self):
        result = self.run_from(self.plain_dir(), "--require-git")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"not a git worktree", result.stdout)

    def test_a_git_repo_still_enforces_the_committed_handoff_rule(self):
        """Making git optional must not weaken the guarantee where git exists."""
        repo = self.repo(commit=False)
        result = self.run_from(self.tmp, "--repo", repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"not committed", result.stdout)
