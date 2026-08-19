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

    def test_refuses_a_non_git_directory(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        result = self.relay(plain, "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"not a git worktree", result.stdout)

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
