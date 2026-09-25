#!/usr/bin/env python3
"""Tests for the `lastcall` command: install, uninstall and the delegates.

Nothing here touches the real ~/.claude, ~/.codex, ~/.claude.json or
~/.lastcall, and nothing runs the real agent CLIs. Every run gets a synthetic
HOME / CLAUDE_CONFIG_DIR / CODEX_HOME / LASTCALL_HOME, and `claude` / `codex`
are fakes on PATH that record their argv and keep a little state so the
installer's "already installed?" queries see what earlier calls did.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "plugins", "lastcall", "lib")
LAUNCHER = os.path.join(ROOT, "plugins", "lastcall", "bin", "lastcall")
HOOK_SCRIPT = os.path.join(ROOT, "plugins", "lastcall", "scripts", "lastcall.py")
PLUGIN_ID = "lastcall@claude-lastcall"
MARKET = "claude-lastcall"

if LIB not in sys.path:
    sys.path.insert(0, LIB)

from lastcall_core import cli  # noqa: E402

posix_only = unittest.skipUnless(os.name == "posix",
                                 "fake agent executables and symlinks are POSIX-only")

EXPECTED_EVENTS = {"Stop": 15, "SessionStart": 10, "PostCompact": 10,
                   "PostToolUse": 5, "UserPromptSubmit": 5}

# One script plays both agents; it decides which from the name it was run as.
FAKE_AGENT = r'''#!%(python)s
import json, os, sys
name = os.path.basename(sys.argv[0])
folder = os.environ["FAKE_AGENT_DIR"]
args = sys.argv[1:]
with open(os.path.join(folder, name + ".argv.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
fail = os.environ.get("FAKE_AGENT_FAIL")
if fail and fail in args:
    sys.stderr.write("fake failure\n")
    sys.exit(3)
once = os.environ.get("FAKE_AGENT_FAIL_ONCE")
if once and once in args and not os.path.exists(os.path.join(folder, name + ".failed")):
    open(os.path.join(folder, name + ".failed"), "w").close()
    sys.stderr.write("fake failure\n")
    sys.exit(3)
state_path = os.path.join(folder, name + ".state.json")
try:
    with open(state_path) as fh:
        state = json.load(fh)
except (OSError, ValueError):
    state = {"markets": {}, "plugins": {}}

def save():
    with open(state_path, "w") as fh:
        json.dump(state, fh)

def market_name(src):
    with open(os.path.join(src, ".claude-plugin", "marketplace.json")) as fh:
        return json.load(fh)["name"]

def source_of(pid):
    """(plugin dir, version) in the marketplace's checkout, like the agents."""
    src = state["markets"][pid.split("@")[1]]
    if not os.path.isdir(src):          # a GitHub-style source: pretend
        return None, os.environ.get("FAKE_REMOTE_VERSION", "1.7.0")
    with open(os.path.join(src, ".claude-plugin", "marketplace.json")) as fh:
        entry = json.load(fh)["plugins"][0]
    root = os.path.normpath(os.path.join(src, entry["source"]))
    with open(os.path.join(root, ".claude-plugin", "plugin.json")) as fh:
        return root, json.load(fh)["version"]

def copy_to_cache(pid):
    """Codex copies the plugin into $CODEX_HOME/plugins/cache/<m>/<p>/<ver>."""
    import shutil
    root, ver = source_of(pid)
    plugin, market = pid.split("@")
    dest = os.path.join(os.environ["CODEX_HOME"], "plugins", "cache", market, plugin, ver)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(root, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return ver

if args[:1] != ["plugin"]:
    sys.exit(2)
rest = args[1:]
if rest[:2] == ["marketplace", "list"]:
    if name == "claude":
        out = [{"name": n, "source": "directory", "path": p} for n, p in state["markets"].items()]
    else:
        out = {"marketplaces": [{"name": n, "root": p,
                                 "marketplaceSource": {
                                     "sourceType": "local" if os.path.isdir(p) else "git",
                                     "source": p}}
                                for n, p in state["markets"].items()]}
    print(json.dumps(out))
elif rest[:2] == ["marketplace", "add"]:
    src = rest[2]
    state["markets"][market_name(src)] = src
    save()
elif rest[:2] == ["marketplace", "remove"]:
    if state["markets"].pop(rest[2], None) is None:
        sys.exit(1)
    save()
elif rest[:1] == ["list"]:
    if name == "claude":
        out = [{"id": pid, "version": p.get("version", "1.7.0"), "scope": "user",
                "enabled": p["enabled"],
                "installPath": "/nowhere"} for pid, p in state["plugins"].items()]
    else:
        out = {"installed": [{"pluginId": pid, "name": pid.split("@")[0],
                              "marketplaceName": pid.split("@")[1], "installed": True,
                              "version": p.get("version", "1.7.0"),
                              "enabled": p["enabled"]} for pid, p in state["plugins"].items()]}
    print(json.dumps(out))
elif rest[:1] in (["install"], ["add"]):
    pid = rest[1]
    if pid.split("@")[1] not in state["markets"]:
        sys.stderr.write("unknown marketplace\n")
        sys.exit(1)
    if name == "codex" and os.environ.get("FAKE_CODEX_CACHE"):
        ver = copy_to_cache(pid)
    else:
        ver = source_of(pid)[1]
    if name == "claude" and pid in state["plugins"]:
        pass  # claude: "already installed", changes nothing
    else:
        state["plugins"][pid] = {"enabled": True, "version": ver}
    save()
elif rest[:2] == ["marketplace", "update"] and name == "claude":
    if rest[2:3] and rest[2] not in state["markets"]:
        sys.exit(1)
elif rest[:2] == ["marketplace", "upgrade"] and name == "codex":
    if os.path.isdir(state["markets"].get(rest[2], "")):
        sys.stderr.write("marketplace is not configured as a Git marketplace\n")
        sys.exit(1)
elif rest[:1] == ["update"] and name == "claude":
    pid = rest[1]
    if pid not in state["plugins"]:
        sys.exit(1)
    state["plugins"][pid]["version"] = source_of(pid)[1]
    save()
elif rest[:1] == ["enable"]:
    state["plugins"][rest[1]]["enabled"] = True
    save()
elif rest[:1] in (["uninstall"], ["remove"]):
    if state["plugins"].pop(rest[1], None) is None:
        sys.exit(1)
    save()
else:
    sys.exit(2)
'''


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def our_commands(settings):
    return [e["command"] for groups in settings.get("hooks", {}).values()
            for g in groups for e in g.get("hooks", [])
            if cli.MARKER.search(e.get("command", ""))]


class Sandbox(unittest.TestCase):
    """A fake machine: its own HOME and agent config dirs."""

    agents_on_path = ()

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="lastcall-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.claude_home = os.path.join(self.home, ".claude")
        self.codex_home = os.path.join(self.home, ".codex")
        self.lastcall_home = os.path.join(self.home, ".lastcall")
        self.local_bin = os.path.join(self.home, ".local", "bin")
        self.fake_dir = os.path.join(self.tmp, "fake")
        self.fake_bin = os.path.join(self.tmp, "fakebin")
        for d in (self.home, self.fake_dir, self.fake_bin):
            os.makedirs(d)
        for agent in self.agents_on_path:
            self.add_fake(agent)
        self.claude_settings = os.path.join(self.claude_home, "settings.json")
        self.codex_hooks = os.path.join(self.codex_home, "hooks.json")

    def add_fake(self, agent):
        path = os.path.join(self.fake_bin, agent)
        with open(path, "w") as fh:
            fh.write(FAKE_AGENT % {"python": sys.executable})
        os.chmod(path, 0o755)

    def env(self, path_extra=()):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "LASTCALL_HOME",
                            "LASTCALL_CLAUDE_HOME", "LASTCALL_CODEX_HOME", "LASTCALL_AGENT")}
        system = os.pathsep.join(p for p in ("/usr/bin", "/bin") if os.path.isdir(p))
        env.update({
            "HOME": self.home, "USERPROFILE": self.home,
            "CLAUDE_CONFIG_DIR": self.claude_home, "CODEX_HOME": self.codex_home,
            "LASTCALL_HOME": self.lastcall_home, "FAKE_AGENT_DIR": self.fake_dir,
            "PATH": os.pathsep.join([self.fake_bin] + list(path_extra) + [system]),
        })
        return env

    def run_cli(self, *args, path_extra=(), extra_env=None):
        env = self.env(path_extra)
        env.update(extra_env or {})
        proc = subprocess.run([sys.executable, LAUNCHER] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=120)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def call(self, *args, path_extra=()):
        """In process, for tests that must run on every OS."""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, self.env(path_extra), clear=True), \
                contextlib.redirect_stdout(buf):
            code = cli.main(list(args))
        return code, buf.getvalue()

    def argv_log(self, agent):
        path = os.path.join(self.fake_dir, agent + ".argv.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def mutations(self, agent):
        return [a for a in self.argv_log(agent) if "--json" not in a]

    def state(self, agent):
        path = os.path.join(self.fake_dir, agent + ".state.json")
        return load(path) if os.path.isfile(path) else {"markets": {}, "plugins": {}}

    def set_state(self, agent, state):
        dump(os.path.join(self.fake_dir, agent + ".state.json"), state)


# ---------------------------------------------------------------- plugin method

@posix_only
class TestPluginMethod(Sandbox):
    agents_on_path = ("claude", "codex")

    def test_claude_uses_its_own_plugin_commands_at_user_scope(self):
        code, out = self.run_cli("install", "--claude", "--no-link-bin")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("claude"), [
            ["plugin", "marketplace", "add", ROOT, "--scope", "user"],
            ["plugin", "install", PLUGIN_ID, "--scope", "user"],
        ])
        self.assertEqual(self.argv_log("codex"), [], "codex was not asked for")

    def test_codex_uses_marketplace_add_then_plugin_add(self):
        code, out = self.run_cli("install", "--codex", "--no-link-bin")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("codex"), [
            ["plugin", "marketplace", "add", ROOT],
            ["plugin", "add", PLUGIN_ID],
        ])
        self.assertIn("/hooks", out, "Codex hook trust must be spelled out")
        self.assertEqual(self.argv_log("claude"), [])

    def test_default_is_every_agent_on_path(self):
        code, out = self.run_cli("install", "--no-link-bin")
        self.assertEqual(code, 0, out)
        self.assertIn(PLUGIN_ID, self.state("claude")["plugins"])
        self.assertIn(PLUGIN_ID, self.state("codex")["plugins"])
        self.assertIn("Desktop apps", out)

    def test_second_install_changes_nothing(self):
        self.run_cli("install", "--no-link-bin")
        before = {a: len(self.mutations(a)) for a in ("claude", "codex")}
        code, out = self.run_cli("install", "--no-link-bin")
        self.assertEqual(code, 0, out)
        for agent in ("claude", "codex"):
            self.assertEqual(len(self.mutations(agent)), before[agent],
                             "%s got mutating calls on a re-install" % agent)
        self.assertIn("already installed", out)
        self.assertIn("already registered from this checkout", out)

    def test_a_disabled_plugin_is_enabled_not_reinstalled(self):
        self.set_state("claude", {"markets": {MARKET: ROOT},
                                  "plugins": {PLUGIN_ID: {"enabled": False}}})
        code, out = self.run_cli("install", "--claude", "--no-link-bin")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("claude"),
                         [["plugin", "enable", PLUGIN_ID, "--scope", "user"]])

    def test_a_marketplace_registered_elsewhere_is_used_and_left_alone(self):
        self.set_state("claude", {"markets": {MARKET: "vy-developer/claude-lastcall"},
                                  "plugins": {}})
        code, out = self.run_cli("install", "--claude", "--no-link-bin")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("claude"),
                         [["plugin", "install", PLUGIN_ID, "--scope", "user"]])
        code, out = self.run_cli("uninstall", "--claude")
        self.assertEqual(code, 0, out)
        self.assertNotIn(["plugin", "marketplace", "remove", MARKET], self.mutations("claude"))
        self.assertIn(MARKET, self.state("claude")["markets"])
        self.assertIn("leaving marketplace", out)

    def test_uninstall_reverses_both_steps_for_both_agents(self):
        self.run_cli("install", "--no-link-bin")
        code, out = self.run_cli("uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("claude")[-2:], [
            ["plugin", "uninstall", PLUGIN_ID, "--scope", "user"],
            ["plugin", "marketplace", "remove", MARKET],
        ])
        self.assertEqual(self.mutations("codex")[-2:], [
            ["plugin", "remove", PLUGIN_ID],
            ["plugin", "marketplace", "remove", MARKET],
        ])
        for agent in ("claude", "codex"):
            self.assertEqual(self.state(agent), {"markets": {}, "plugins": {}})

    def test_plugin_install_removes_a_leftover_hooks_install(self):
        """Hooks-method entries left behind would fire every event twice."""
        foreign = {"type": "command", "command": "echo mine"}
        dump(self.claude_settings, {"env": {"K": "v"}, "hooks": {"Stop": [{"hooks": [foreign]}]}})
        self.run_cli("install", "--claude", "--method", "hooks", "--no-link-bin")
        self.assertTrue(our_commands(load(self.claude_settings)))
        code, out = self.run_cli("install", "--claude", "--no-link-bin")
        self.assertEqual(code, 0, out)
        after = load(self.claude_settings)
        self.assertEqual(our_commands(after), [])
        self.assertEqual(after["hooks"]["Stop"][0]["hooks"], [foreign])
        self.assertEqual(after["env"], {"K": "v"})
        self.assertTrue(os.path.isfile(self.claude_settings + ".lastcall.bak"))

    def test_dry_run_only_asks_and_writes_nothing(self):
        code, out = self.run_cli("install", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would run: claude plugin marketplace add", out)
        self.assertIn("would run: codex plugin add %s" % PLUGIN_ID, out)
        for agent in ("claude", "codex"):
            self.assertEqual(self.mutations(agent), [], "dry run mutated %s" % agent)
            self.assertFalse(os.path.exists(os.path.join(self.fake_dir, agent + ".state.json")))
        # macOS's system Python (running the fakes) may create ~/Library/Caches.
        self.assertEqual(set(os.listdir(self.home)) - {"Library"}, set(),
                         "dry run wrote into HOME")

    def test_an_explicit_agent_missing_from_path_fails_clearly(self):
        os.remove(os.path.join(self.fake_bin, "codex"))
        code, out = self.run_cli("install", "--codex", "--no-link-bin")
        self.assertEqual(code, 1)
        self.assertIn("not on PATH", out)
        self.assertIn("--method hooks", out)

    def test_a_failing_agent_command_is_reported_and_the_other_agent_still_installs(self):
        code, out = self.run_cli("install", "--no-link-bin",
                                 extra_env={"FAKE_AGENT_FAIL": "install"})
        self.assertEqual(code, 1, out)
        self.assertIn("Failed for: claude", out)
        self.assertIn(PLUGIN_ID, self.state("codex")["plugins"])


@posix_only
class TestRefresh(Sandbox):
    """`install --refresh` after a `git pull` of the checkout."""
    agents_on_path = ("claude", "codex")

    def refresh(self, *extra, **kw):
        return self.run_cli("install", "--refresh", "--no-link-bin", *extra, **kw)

    def installed(self, agent, version="1.7.0", enabled=True, market=ROOT):
        self.set_state(agent, {"markets": {MARKET: market},
                               "plugins": {PLUGIN_ID: {"enabled": enabled,
                                                       "version": version}}})

    def test_claude_same_version_reads_the_catalog_and_says_it_loads_in_place(self):
        self.installed("claude", version=cli.version())
        for _ in range(2):
            code, out = self.refresh("--claude")
            self.assertEqual(code, 0, out)
            self.assertIn("in place from %s" % cli.PLUGIN_ROOT, out)
            self.assertIn("/reload-plugins", out)
        self.assertEqual(self.mutations("claude"),
                         [["plugin", "marketplace", "update", MARKET]] * 2)

    def test_claude_bumped_version_runs_plugin_update_once(self):
        self.installed("claude", version="0.0.1")
        code, out = self.refresh("--claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("claude"), [
            ["plugin", "marketplace", "update", MARKET],
            ["plugin", "update", PLUGIN_ID, "--scope", "user"],
        ])
        self.assertEqual(self.state("claude")["plugins"][PLUGIN_ID]["version"], cli.version())
        code, out = self.refresh("--claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("claude")[2:],
                         [["plugin", "marketplace", "update", MARKET]])

    def test_codex_stale_cache_is_re_added_then_left_alone(self):
        env = {"FAKE_CODEX_CACHE": "1"}
        code, out = self.run_cli("install", "--codex", "--no-link-bin", extra_env=env)
        self.assertEqual(code, 0, out)
        cache = os.path.join(self.codex_home, "plugins", "cache", MARKET, "lastcall",
                             cli.version())
        with open(os.path.join(cache, "scripts", "lastcall.py"), "a") as fh:
            fh.write("# stale\n")                    # the checkout moved on
        code, out = self.refresh("--codex", extra_env=env)
        self.assertEqual(code, 0, out)
        self.assertIn("no update command for a local marketplace", out)
        self.assertEqual(self.mutations("codex")[-1], ["plugin", "add", PLUGIN_ID])
        self.assertNotIn(["plugin", "marketplace", "upgrade", MARKET], self.mutations("codex"))
        self.assertEqual(cli.tree_digest(cache), cli.tree_digest(cli.PLUGIN_ROOT))
        before = len(self.mutations("codex"))
        code, out = self.refresh("--codex", extra_env=env)
        self.assertEqual(code, 0, out)
        self.assertIn("already matches this checkout", out)
        self.assertEqual(len(self.mutations("codex")), before, "second refresh mutated")

    def test_codex_older_version_is_re_added(self):
        self.installed("codex", version="0.0.1")
        code, out = self.refresh("--codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("codex"), [["plugin", "add", PLUGIN_ID]])

    def test_codex_disabled_plugin_is_not_silently_re_enabled(self):
        self.installed("codex", version="0.0.1", enabled=False)
        code, out = self.refresh("--codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("codex"), [])
        self.assertIn("disabled; not refreshed", out)

    def test_codex_falls_back_to_remove_and_add_of_the_plugin_only(self):
        self.installed("codex", version="0.0.1")
        code, out = self.refresh("--codex", extra_env={"FAKE_AGENT_FAIL_ONCE": "add"})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("codex"), [
            ["plugin", "add", PLUGIN_ID],
            ["plugin", "remove", PLUGIN_ID],
            ["plugin", "add", PLUGIN_ID],
        ])
        self.assertIn("falling back to remove + add", out)
        self.assertIn(MARKET, self.state("codex")["markets"])

    def test_codex_git_marketplace_is_upgraded_first(self):
        self.installed("codex", market="vy-developer/claude-lastcall")
        code, out = self.refresh("--codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutations("codex"), [
            ["plugin", "marketplace", "upgrade", MARKET],
            ["plugin", "add", PLUGIN_ID],
        ])

    def test_refresh_installs_what_is_missing(self):
        code, out = self.refresh()
        self.assertEqual(code, 0, out)
        self.assertIn(PLUGIN_ID, self.state("claude")["plugins"])
        self.assertIn(PLUGIN_ID, self.state("codex")["plugins"])
        self.assertIn("Refreshing Last Call", out)

    def test_dry_run_prints_the_refresh_commands_and_changes_nothing(self):
        self.installed("claude", version="0.0.1")
        self.installed("codex", version="0.0.1")
        code, out = self.refresh("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would run: claude plugin marketplace update %s" % MARKET, out)
        self.assertIn("would run: claude plugin update %s --scope user" % PLUGIN_ID, out)
        self.assertIn("would run: codex plugin add %s" % PLUGIN_ID, out)
        for agent in ("claude", "codex"):
            self.assertEqual(self.mutations(agent), [], "dry run mutated %s" % agent)
            self.assertEqual(self.state(agent)["plugins"][PLUGIN_ID]["version"], "0.0.1")

    def test_refresh_with_the_hooks_method_is_refused(self):
        code, out = self.refresh("--claude", "--method", "hooks")
        self.assertEqual(code, 2, out)
        self.assertIn("nothing to refresh", out)


class TestTreeDigest(unittest.TestCase):
    def test_ignores_bytecode_and_sees_content_and_deletions(self):
        a = tempfile.mkdtemp(prefix="lastcall-tree-")
        self.addCleanup(shutil.rmtree, a, True)
        dump(os.path.join(a, "x", "one.json"), {"v": 1})
        b = a + "-copy"
        shutil.copytree(a, b)
        self.addCleanup(shutil.rmtree, b, True)
        os.makedirs(os.path.join(b, "x", "__pycache__"))
        open(os.path.join(b, "x", "__pycache__", "one.cpython-39.pyc"), "w").close()
        open(os.path.join(b, ".DS_Store"), "w").close()
        self.assertEqual(cli.tree_digest(a), cli.tree_digest(b))
        dump(os.path.join(b, "x", "one.json"), {"v": 2})
        self.assertNotEqual(cli.tree_digest(a), cli.tree_digest(b))
        dump(os.path.join(a, "x", "one.json"), {"v": 2})
        dump(os.path.join(b, "x", "gone.json"), {})
        self.assertNotEqual(cli.tree_digest(a), cli.tree_digest(b))
        self.assertIsNone(cli.tree_digest(os.path.join(a, "missing")))


@posix_only
class TestAgentDetection(Sandbox):
    agents_on_path = ("claude",)

    def test_only_agents_on_path_are_installed_by_default(self):
        code, out = self.run_cli("install", "--no-link-bin")
        self.assertEqual(code, 0, out)
        self.assertIn("for: claude\n", out)
        self.assertNotIn("/hooks", out, "Codex was not installed, so no trust note")

    def test_no_agent_on_path_is_an_error_not_a_silent_success(self):
        os.remove(os.path.join(self.fake_bin, "claude"))
        code, out = self.run_cli("install", "--no-link-bin")
        self.assertEqual(code, 1)
        self.assertIn("No agent found on PATH", out)


# ---------------------------------------------------------------- hooks method

class TestHooksMethod(Sandbox):
    def install(self, *extra):
        return self.call("install", "--claude", "--codex", "--method", "hooks",
                         "--no-link-bin", "--python", "python3", *extra)

    def test_writes_every_event_to_both_agents(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        for path in (self.claude_settings, self.codex_hooks):
            hooks = load(path)["hooks"]
            self.assertEqual(set(hooks), set(EXPECTED_EVENTS))
            for event, timeout in EXPECTED_EVENTS.items():
                (group,) = hooks[event]
                (entry,) = group["hooks"]
                self.assertEqual(entry["type"], "command")
                self.assertEqual(entry["timeout"], timeout)
                self.assertEqual(entry["command"],
                                 'python3 "%s" %s' % (HOOK_SCRIPT, event))
            self.assertEqual(hooks["PostToolUse"][0]["matcher"], "*")

    def test_merges_into_existing_files_without_losing_anything(self):
        foreign = {"type": "command", "command": "/opt/tools/my-lastcall.py-wrapper --verbose"}
        dump(self.claude_settings, {"permissions": {"allow": ["Bash"]},
                                    "hooks": {"Stop": [{"hooks": [foreign]}]}})
        dump(self.codex_hooks, {"hooks": {"SessionStart": [{"hooks": [foreign]}]}})
        self.install()
        claude = load(self.claude_settings)
        self.assertEqual(claude["permissions"], {"allow": ["Bash"]})
        self.assertEqual(claude["hooks"]["Stop"][0]["hooks"], [foreign])
        self.assertEqual(len(claude["hooks"]["Stop"]), 2)
        codex = load(self.codex_hooks)
        self.assertEqual(codex["hooks"]["SessionStart"][0]["hooks"], [foreign])
        self.assertEqual(len(our_commands(codex)), len(EXPECTED_EVENTS))

    def test_reinstall_replaces_instead_of_duplicating_and_skips_identical_writes(self):
        self.install()
        first = load(self.codex_hooks)
        os.remove(self.codex_hooks + ".lastcall.bak") if os.path.exists(
            self.codex_hooks + ".lastcall.bak") else None
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(load(self.codex_hooks), first)
        self.assertIn("already in place", out)
        self.assertFalse(os.path.exists(self.codex_hooks + ".lastcall.bak"),
                         "an unchanged file was rewritten (Codex re-asks for trust)")

    def test_replaces_entries_left_by_the_1x_installer(self):
        old = {"type": "command", "timeout": 15,
               "command": '/usr/local/bin/python3 "/old/place/plugins/lastcall/scripts/lastcall.py" Stop'}
        dump(self.claude_settings, {"hooks": {"Stop": [{"hooks": [old]}]}})
        self.install()
        commands = our_commands(load(self.claude_settings))
        self.assertEqual(len(commands), len(EXPECTED_EVENTS))
        self.assertNotIn(old["command"], commands)

    def test_backups_are_owner_only(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions")
        dump(self.codex_hooks, {"hooks": {}, "note": "sk-live-example"})
        os.chmod(self.codex_hooks, 0o644)
        self.install()
        backup = self.codex_hooks + ".lastcall.bak"
        self.assertEqual(load(backup), {"hooks": {}, "note": "sk-live-example"})
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode) & 0o077, 0)

    def test_uninstall_strips_only_ours_and_leaves_backups(self):
        foreign = {"type": "command", "command": "echo mine"}
        dump(self.claude_settings, {"hooks": {"Stop": [{"hooks": [foreign]}]}})
        dump(self.codex_hooks, {"hooks": {}})
        self.install()
        code, out = self.call("uninstall", "--claude", "--codex", "--method", "hooks")
        self.assertEqual(code, 0, out)
        self.assertEqual(load(self.claude_settings), {"hooks": {"Stop": [{"hooks": [foreign]}]}})
        self.assertEqual(load(self.codex_hooks), {})
        for path in (self.claude_settings, self.codex_hooks):
            self.assertTrue(os.path.isfile(path + ".lastcall.bak"))

    def test_an_unreadable_file_is_refused_not_overwritten(self):
        os.makedirs(self.codex_home)
        with open(self.codex_hooks, "w") as fh:
            fh.write("{ not json")
        code, out = self.install()
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", out)
        with open(self.codex_hooks) as fh:
            self.assertEqual(fh.read(), "{ not json")
        self.assertTrue(our_commands(load(self.claude_settings)),
                        "one agent's broken file must not block the other")

    def test_dry_run_prints_and_writes_nothing(self):
        code, out = self.install("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would write 5 hook entries to %s" % self.claude_settings, out)
        self.assertIn("would write 5 hook entries to %s" % self.codex_hooks, out)
        self.assertIn("would create %s" % os.path.join(self.lastcall_home, "config.json"), out)
        self.assertEqual(os.listdir(self.home), [])

    def test_project_install_writes_into_the_project_only(self):
        project = os.path.join(self.tmp, "proj")
        os.makedirs(project)
        code, out = self.call("install", "--project", project, "--python", "python3")
        self.assertEqual(code, 0, out)
        self.assertTrue(our_commands(load(os.path.join(project, ".claude", "settings.json"))))
        self.assertEqual(os.listdir(self.home), [],
                         "a project install must not touch HOME")

    def test_project_install_refuses_the_plugin_method(self):
        code, out = self.call("install", "--project", self.tmp, "--method", "plugin")
        self.assertEqual(code, 2)
        self.assertIn("--method hooks", out)


class TestInstallPyWrapper(Sandbox):
    def run_install_py(self, *args, cwd=None):
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "install.py")] + list(args),
                              env=self.env(), cwd=cwd or self.tmp,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def test_default_targets_the_current_project(self):
        code, out = self.run_install_py("--python", "python3")
        self.assertEqual(code, 0, out)
        self.assertTrue(our_commands(load(os.path.join(self.tmp, ".claude", "settings.json"))))

    def test_global_targets_the_user_level_claude_settings(self):
        code, out = self.run_install_py("--global", "--no-link-bin", "--python", "python3")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(our_commands(load(self.claude_settings))), len(EXPECTED_EVENTS))
        self.assertFalse(os.path.exists(self.codex_hooks), "--global is Claude-only, as before")
        code, out = self.run_install_py("--global", "--uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(our_commands(load(self.claude_settings)), [])

    def test_dir_without_a_path_is_a_usage_error(self):
        code, out = self.run_install_py("--dir")
        self.assertEqual(code, 1)
        self.assertIn("--dir needs a path", out)


# ---------------------------------------------------------------- machine setup

class TestMachineSetup(Sandbox):
    def test_global_config_is_created_once_with_nothing_active(self):
        self.call("install", "--claude", "--method", "hooks", "--python", "python3", "--no-link-bin")
        path = os.path.join(self.lastcall_home, "config.json")
        doc = load(path)
        self.assertEqual([k for k in doc if not k.startswith("_")], [],
                         "the example must not switch anything on")
        self.assertIn("yellow_percent", doc["_example"])
        with open(path, "w") as fh:
            fh.write('{"yellow_percent": 30}\n')
        self.call("install", "--claude", "--method", "hooks", "--python", "python3", "--no-link-bin")
        self.assertEqual(load(path), {"yellow_percent": 30}, "user edits were overwritten")

    @posix_only
    def test_links_into_local_bin_only_when_it_is_on_path(self):
        code, out = self.call("install", "--claude", "--method", "hooks", "--python", "python3")
        self.assertIn("is not on PATH", out)
        self.assertFalse(os.path.lexists(os.path.join(self.local_bin, "lastcall")))
        os.makedirs(self.local_bin)
        code, out = self.call("install", "--claude", "--method", "hooks", "--python", "python3",
                              path_extra=[self.local_bin])
        link = os.path.join(self.local_bin, "lastcall")
        self.assertTrue(os.path.islink(link), out)
        self.assertEqual(os.path.realpath(link), os.path.realpath(LAUNCHER))

    @posix_only
    def test_an_existing_foreign_command_is_not_clobbered(self):
        os.makedirs(self.local_bin)
        other = os.path.join(self.local_bin, "lastcall")
        with open(other, "w") as fh:
            fh.write("#!/bin/sh\necho someone else\n")
        code, out = self.call("install", "--claude", "--method", "hooks", "--python", "python3",
                              "--link-bin", self.local_bin)
        self.assertIn("not ours", out)
        with open(other) as fh:
            self.assertIn("someone else", fh.read())

    @posix_only
    def test_uninstall_removes_the_link_and_keeps_the_config(self):
        bindir = os.path.join(self.tmp, "bin")
        self.call("install", "--claude", "--method", "hooks", "--python", "python3", "--link-bin", bindir)
        self.assertTrue(os.path.islink(os.path.join(bindir, "lastcall")))
        code, out = self.call("uninstall", "--claude", "--method", "hooks", "--link-bin", bindir)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.lexists(os.path.join(bindir, "lastcall")))
        self.assertTrue(os.path.isfile(os.path.join(self.lastcall_home, "config.json")))

    @posix_only
    def test_launcher_runs_through_a_symlink(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        link = os.path.join(bindir, "lastcall")
        os.symlink(LAUNCHER, link)
        env = self.env()
        env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), env["PATH"]])
        proc = subprocess.run([link, "version"], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)
        out = proc.stdout.decode()
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("lastcall %s" % cli.version(), out)
        self.assertIn(ROOT, out)

    def test_launcher_and_windows_shim_exist(self):
        self.assertTrue(os.path.isfile(LAUNCHER))
        if os.name == "posix":
            self.assertTrue(os.access(LAUNCHER, os.X_OK), "bin/lastcall is not executable")
        with open(LAUNCHER, "rb") as fh:
            self.assertTrue(fh.readline().startswith(b"#!/usr/bin/env python3"))
        with open(LAUNCHER + ".cmd", encoding="utf-8") as fh:
            self.assertIn('"%~dp0lastcall"', fh.read())


# ---------------------------------------------------------------- the other subcommands

class TestSubcommands(Sandbox):
    def test_version_matches_the_plugin_manifest(self):
        manifest = load(os.path.join(ROOT, "plugins", "lastcall", ".claude-plugin", "plugin.json"))
        code, out = self.call("version")
        self.assertEqual(code, 0)
        self.assertIn("lastcall %s" % manifest["version"], out)

    def test_help_lists_every_subcommand(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--help"])
        for name in ("install", "uninstall", "status", "tidy", "doctor", "setup", "version"):
            self.assertIn(name, buf.getvalue())

    def test_doctor_and_setup_run_the_hook_script(self):
        for name in ("doctor", "setup"):
            with mock.patch.object(cli.subprocess, "call", return_value=7) as call:
                self.assertEqual(cli.main([name, "--flag"]), 7)
            call.assert_called_once_with([sys.executable, cli.HOOK_SCRIPT, name, "--flag"])

    def test_status_and_tidy_delegate_with_their_own_flags(self):
        code, out = self.run_cli("status", "--json", "--claude-home", self.claude_home,
                                 "--codex-home", self.codex_home)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out), [])
        code, out = self.run_cli("tidy", "--json", "--claude-home", self.claude_home,
                                 "--codex-home", self.codex_home)
        self.assertEqual(code, 0, out)
        self.assertIsInstance(json.loads(out), dict)

    def test_status_context_column_comes_from_the_agent_adapter(self):
        sid = "11111111-2222-3333-4444-555555555555"
        dump(os.path.join(self.claude_home, "sessions", "%d.json" % os.getpid()),
             {"pid": os.getpid(), "sessionId": sid, "cwd": self.tmp, "status": "idle",
              "entrypoint": "cli"})
        transcript = os.path.join(self.claude_home, "projects", "-synthetic", sid + ".jsonl")
        os.makedirs(os.path.dirname(transcript))
        with open(transcript, "w") as fh:
            fh.write(json.dumps({
                "type": "assistant", "isSidechain": False, "sessionId": sid,
                "timestamp": "2026-09-25T10:00:00.000Z",
                "message": {"id": "msg_1", "model": "claude-synthetic",
                            "usage": {"input_tokens": 1000, "cache_read_input_tokens": 50000,
                                      "cache_creation_input_tokens": 0, "output_tokens": 10}},
            }) + "\n")
        code, out = self.run_cli("status", "--agent", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("CONTEXT", out)
        self.assertIn("51k", out, "the usage provider was not registered")

    def test_register_usage_providers_covers_both_agents(self):
        from lastcall_core import sessions
        with mock.patch.dict(sessions.USAGE_PROVIDERS, {}, clear=True):
            cli.register_usage_providers()
            registered = sys.modules["lastcall_core.sessions"].USAGE_PROVIDERS
            self.assertEqual(set(registered) & {"claude", "codex"}, {"claude", "codex"})
            rec = sessions.SessionRecord(agent="codex", session_id="x", transcript_path=None)
            self.assertIsNone(registered["codex"](rec))


if __name__ == "__main__":
    unittest.main(verbosity=2)
