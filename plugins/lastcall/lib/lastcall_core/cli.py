"""The `lastcall` command: install once per machine, then look around.

    lastcall install [--claude] [--codex] [--method plugin|hooks] [--dry-run]
    lastcall install --refresh [--claude] [--codex] [--dry-run]   after git pull
    lastcall uninstall [--claude] [--codex] [--method plugin|hooks] [--dry-run]
    lastcall status [--json] ...        live sessions of both agents
    lastcall tidy ...                   propose names for old chats
    lastcall doctor | setup ...         the hook script's own checks / wizard
    lastcall relay [--agent claude|codex] [--dry-run] ...
                                        hand over to a fresh session
    lastcall version

Standard library only, Python 3.9+. Reached through plugins/lastcall/bin/lastcall
(which puts this package's parent on sys.path) or, from a checkout,
`python3 -m lastcall_core.cli` with PYTHONPATH=plugins/lastcall/lib.

Two ways to install, both user-level so the CLIs and the desktop apps (which
read the same ~/.claude and ~/.codex) pick them up:

  plugin  (default) register this checkout as a local marketplace and enable
          the plugin through each agent's own `plugin` command. Nothing on
          disk holds an absolute path of ours; the agent owns the state.
  hooks   write the hook entries straight into ~/.claude/settings.json and
          ~/.codex/hooks.json. For machines where the plugin route is not
          available. Absolute paths: move the checkout and re-run.

Every write backs the file up first, is atomic, and only ever removes entries
this tool recognises as its own.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------- layout

PKG_DIR = os.path.dirname(os.path.abspath(__file__))          # .../lib/lastcall_core
LIB_DIR = os.path.dirname(PKG_DIR)                            # .../lib
PLUGIN_ROOT = os.path.dirname(LIB_DIR)                        # plugins/lastcall
REPO_ROOT = os.path.dirname(os.path.dirname(PLUGIN_ROOT))     # the checkout
HOOK_SCRIPT = os.path.join(PLUGIN_ROOT, "scripts", "lastcall.py")
LAUNCHER = os.path.join(PLUGIN_ROOT, "bin", "lastcall")
LAUNCHER_CMD = os.path.join(PLUGIN_ROOT, "bin", "lastcall.cmd")
PLUGIN_MANIFEST = os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json")
MARKETPLACE_MANIFEST = os.path.join(REPO_ROOT, ".claude-plugin", "marketplace.json")
EXAMPLE_CONFIG = os.path.join(PLUGIN_ROOT, "lastcall.example.json")

PLUGIN_NAME = "lastcall"
DEFAULT_MARKETPLACE = "claude-lastcall"

CLAUDE = "claude"
CODEX = "codex"
AGENT_ORDER = (CLAUDE, CODEX)

#: (event, timeout seconds). PostToolUse gets an explicit catch-all matcher:
#: Codex's own examples use "*" and Claude treats "*" and "no matcher" alike.
EVENTS = (("Stop", 15), ("SessionStart", 10), ("PostCompact", 10),
          ("PostToolUse", 5), ("UserPromptSubmit", 5))
MATCHERS = {"PostToolUse": "*"}

# Matching the bare string "lastcall.py" would strip any unrelated hook whose
# command happened to contain it. Ours always ends with the script followed by
# one of our event names, so require that shape. The event list is the union
# of every version's: the 1.x installer wrote only Stop/SessionStart/PostCompact.
MARKER = re.compile(r"lastcall\.py[\"']?\s+(?:%s)\s*$"
                    % "|".join(name for name, _ in EVENTS))

BACKUP_SUFFIX = ".lastcall.bak"


class InstallError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def version():
    try:
        with open(PLUGIN_MANIFEST, encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "unknown")
    except (OSError, ValueError):
        return "unknown"


def marketplace_name():
    try:
        with open(MARKETPLACE_MANIFEST, encoding="utf-8") as fh:
            return str(json.load(fh).get("name") or DEFAULT_MARKETPLACE)
    except (OSError, ValueError):
        return DEFAULT_MARKETPLACE


def plugin_id():
    return "%s@%s" % (PLUGIN_NAME, marketplace_name())


def home():
    return os.path.expanduser("~")


def claude_home():
    return os.path.abspath(os.path.expanduser(
        os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home(), ".claude")))


def codex_home():
    return os.path.abspath(os.path.expanduser(
        os.environ.get("CODEX_HOME") or os.path.join(home(), ".codex")))


def lastcall_home():
    return os.path.abspath(os.path.expanduser(
        os.environ.get("LASTCALL_HOME") or os.path.join(home(), ".lastcall")))


def hooks_file(agent, project=None):
    """Where the hooks method writes for ``agent``: user level, or inside
    ``project`` when one is given."""
    if agent == CLAUDE:
        base = os.path.join(project, ".claude") if project else claude_home()
        return os.path.join(base, "settings.json")
    base = os.path.join(project, ".codex") if project else codex_home()
    return os.path.join(base, "hooks.json")


def quote(text):
    return '"%s"' % text


def _same_path(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (TypeError, ValueError):
        return False


def _points_here(entry):
    """True when any string inside ``entry`` is a path to this checkout: the
    marketplace was registered from here, not from GitHub or elsewhere."""
    if isinstance(entry, str):
        return os.path.isabs(entry) and _same_path(entry, REPO_ROOT)
    if isinstance(entry, dict):
        return any(_points_here(v) for v in entry.values())
    if isinstance(entry, list):
        return any(_points_here(v) for v in entry)
    return False


def _describe_source(entry):
    if not isinstance(entry, dict):
        return "?"
    for key in ("path", "repo", "url", "source", "root", "installLocation"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            inner = _describe_source(value)
            if inner != "?":
                return inner
    for value in entry.values():
        if isinstance(value, dict):
            inner = _describe_source(value)
            if inner != "?":
                return inner
    return "?"


def find_interpreter():
    """Pick an interpreter that actually exists on this machine.

    `python3` is a safe assumption on macOS and Linux and a bad one on Windows,
    where the python.org installer ships `python.exe` and the `py` launcher but
    no `python3`. Verifying by execution beats trusting a name.
    """
    if os.name == "nt":
        candidates = [["py", "-3"], ["python"], ["python3"]]
    else:
        candidates = [["python3"], ["python"]]
    for candidate in candidates:
        binary = shutil.which(candidate[0])
        if not binary:
            continue
        try:
            probe = subprocess.run(
                candidate + ["-c", "import sys; print(sys.version_info[0])"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == b"3":
            return [binary] + candidate[1:]
    return None


def hook_interpreter(explicit=None):
    """The interpreter the hook command names. `python3` by name on POSIX, so
    the entry keeps working when Python is upgraded; detected on Windows."""
    if explicit:
        return [explicit]
    if os.name != "nt" and shutil.which("python3"):
        return ["python3"]
    return find_interpreter()


def hook_command(interpreter, event):
    parts = [quote(p) if " " in p else p for p in interpreter]
    return " ".join(parts + [quote(HOOK_SCRIPT), event])


# ---------------------------------------------------------------- JSON files

def load_json(path):
    """{} for a missing file; InstallError for one we cannot read, because
    overwriting a file we could not parse would lose the user's settings."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        data = json.loads(text) if text.strip() else {}
    except (OSError, ValueError) as error:
        raise InstallError("refusing to overwrite unreadable %s: %s" % (path, error))
    if not isinstance(data, dict):
        raise InstallError("refusing to overwrite %s: top level is not an object" % path)
    return data


def save_json(path, data):
    """Back up, then write atomically. Returns the backup path or None.

    settings.json routinely holds API keys, so the backup is owner-only: a
    copy at the original 644 would be a second world-readable live secret.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    backup = None
    if os.path.isfile(path):
        backup = path + BACKUP_SUFFIX
        shutil.copy2(path, backup)
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass
    temporary = path + ".lastcall.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    if backup:
        try:
            shutil.copymode(path, temporary)
        except OSError:
            pass
    os.replace(temporary, path)
    return backup


def count_ours(settings):
    n = 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(entries, list):
                n += sum(1 for e in entries if isinstance(e, dict)
                         and MARKER.search(str(e.get("command", ""))))
    return n


def strip_existing(settings):
    """Remove only our own entries, leaving every other hook untouched."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            kept = [
                entry for entry in entries
                if not (isinstance(entry, dict)
                        and MARKER.search(str(entry.get("command", ""))))
            ]
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def add_ours(settings, interpreter):
    hooks = settings.setdefault("hooks", {})
    for event, timeout in EVENTS:
        group = {"hooks": [{
            "type": "command",
            "command": hook_command(interpreter, event),
            "timeout": timeout,
        }]}
        if event in MATCHERS:
            group = dict([("matcher", MATCHERS[event])] + list(group.items()))
        hooks.setdefault(event, []).append(group)
    return settings


# ---------------------------------------------------------------- output

class Out:
    def __init__(self, dry_run=False, stream=None):
        self.dry_run = dry_run
        self.stream = stream or sys.stdout

    def say(self, text=""):
        self.stream.write(text + "\n")
        self.stream.flush()

    def step(self, text):
        self.say(("  would " if self.dry_run else "  ") + text)


# ---------------------------------------------------------------- hooks method

def hooks_install(agent, out, interpreter, project=None):
    path = hooks_file(agent, project)
    before = load_json(path)
    after = add_ours(strip_existing(json.loads(json.dumps(before))), interpreter)
    if after == before:
        out.say("  %s: hooks already in place in %s" % (agent, path))
        return True
    if out.dry_run:
        out.step("write %d hook entries to %s (backup %s)"
                 % (len(EVENTS), path, path + BACKUP_SUFFIX if os.path.isfile(path) else "none"))
        for event, _t in EVENTS:
            out.say("      %s: %s" % (event, hook_command(interpreter, event)))
        return True
    backup = save_json(path, after)
    out.say("  %s: installed %d hooks into %s" % (agent, len(EVENTS), path))
    if backup:
        out.say("      backup: %s" % backup)
    return True


def hooks_uninstall(agent, out, project=None, quiet_if_absent=False):
    path = hooks_file(agent, project)
    if not os.path.isfile(path):
        if not quiet_if_absent:
            out.say("  %s: no %s, nothing to remove" % (agent, path))
        return True
    before = load_json(path)
    n = count_ours(before)
    if not n:
        if not quiet_if_absent:
            out.say("  %s: no Last Call hooks in %s" % (agent, path))
        return True
    if out.dry_run:
        out.step("remove %d Last Call hook entries from %s" % (n, path))
        return True
    backup = save_json(path, strip_existing(before))
    out.say("  %s: removed %d Last Call hook entries from %s" % (agent, n, path))
    if backup:
        out.say("      backup: %s" % backup)
    return True


# ---------------------------------------------------------------- plugin method

def _run(argv, out, capture=False, timeout=180):
    """Run an agent CLI. Returns (returncode, stdout-or-None)."""
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        if not capture:
            out.say("  ! %s failed: %s" % (" ".join(argv[:4]), error))
        return 127, None
    text = proc.stdout.decode("utf-8", "replace") if capture and proc.stdout else None
    return proc.returncode, text


def _query_json(argv, out):
    code, text = _run(argv, out, capture=True, timeout=60)
    if code != 0 or not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # Some builds print a banner line before the JSON; take the last
        # line that parses.
        for line in reversed(text.strip().splitlines()):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


class PluginState:
    def __init__(self, known, market=None, plugin=None):
        self.known = known        # both queries answered
        self.market = market      # marketplace entry named ours, or None
        self.plugin = plugin      # installed plugin entry, or None

    @property
    def market_is_ours(self):
        return self.market is not None and _points_here(self.market)

    @property
    def enabled(self):
        return self.plugin is not None and self.plugin.get("enabled", True) is not False


def plugin_state(agent, binary, out):
    """What the agent already has, from its own `list --json` commands."""
    name, pid = marketplace_name(), plugin_id()
    markets = _query_json([binary, "plugin", "marketplace", "list", "--json"], out)
    plugins = _query_json([binary, "plugin", "list", "--json"], out)
    if agent == CODEX:
        markets = markets.get("marketplaces") if isinstance(markets, dict) else None
        plugins = plugins.get("installed") if isinstance(plugins, dict) else None
    known = isinstance(markets, list) and isinstance(plugins, list)
    market = next((m for m in markets or [] if isinstance(m, dict)
                   and m.get("name") == name), None)
    plugin = None
    for p in plugins or []:
        if not isinstance(p, dict):
            continue
        ident = p.get("id") or p.get("pluginId")
        if not ident and p.get("name") == PLUGIN_NAME:
            ident = "%s@%s" % (PLUGIN_NAME, p.get("marketplaceName") or p.get("marketplace"))
        if ident == pid and p.get("installed", True) is not False:
            plugin = p
            break
    return PluginState(known, market, plugin)


def plugin_commands(agent, binary):
    """The exact argv this tool uses, per agent. Verified against
    `claude plugin ... --help` (2.1.281) and `codex plugin ... --help`
    (0.153.4)."""
    name, pid = marketplace_name(), plugin_id()
    if agent == CLAUDE:
        return {
            "market_add": [binary, "plugin", "marketplace", "add", REPO_ROOT, "--scope", "user"],
            "install": [binary, "plugin", "install", pid, "--scope", "user"],
            "enable": [binary, "plugin", "enable", pid, "--scope", "user"],
            "uninstall": [binary, "plugin", "uninstall", pid, "--scope", "user"],
            "market_remove": [binary, "plugin", "marketplace", "remove", name],
        }
    return {
        "market_add": [binary, "plugin", "marketplace", "add", REPO_ROOT],
        "install": [binary, "plugin", "add", pid],
        "enable": None,
        "uninstall": [binary, "plugin", "remove", pid],
        "market_remove": [binary, "plugin", "marketplace", "remove", name],
    }


def _do(argv, out, what):
    # Show the agent by name; the resolved path is noise to a reader.
    shown = " ".join(quote(a) if " " in a else a
                     for a in [os.path.basename(argv[0])] + list(argv[1:]))
    if out.dry_run:
        out.step("run: %s" % shown)
        return True
    out.say("  $ %s" % shown)
    code, _ = _run(argv, out)
    if code != 0:
        out.say("  ! %s failed (exit %s)" % (what, code))
        return False
    return True


def plugin_install(agent, binary, out, refresh=False):
    cmds = plugin_commands(agent, binary)
    state = plugin_state(agent, binary, out)
    name = marketplace_name()
    if not state.known:
        out.say("  %s: could not read its plugin list; installing anyway" % agent)
    if state.market is None:
        if not _do(cmds["market_add"], out, "adding the marketplace"):
            return False
    elif state.market_is_ours:
        out.say("  %s: marketplace %s already registered from this checkout" % (agent, name))
    else:
        out.say("  %s: marketplace %s already registered from %s; using it"
                % (agent, name, _describe_source(state.market)))
    if state.plugin is None:
        return _do(cmds["install"], out, "installing the plugin")
    if refresh:
        return plugin_refresh(agent, binary, out, state)
    if not state.enabled and cmds["enable"]:
        return _do(cmds["enable"], out, "enabling the plugin")
    out.say("  %s: %s already installed%s" % (
        agent, plugin_id(), "" if state.enabled else " (disabled)"))
    return True


# ---------------------------------------------------------------- refresh
#
# After `git pull` in this checkout, what each agent runs can be stale. What
# the agents actually do with a LOCAL-directory marketplace (checked against
# Claude Code 2.1.281 and Codex CLI 0.153.4 in throwaway config dirs):
#
#   claude  `plugin install` copies the plugin into plugins/cache/<m>/<p>/<ver>,
#           but a plugin from a local-directory marketplace "loads in place"
#           from the checkout (the CLI says so, and 2.1.281 computes
#           loadsInPlaceFrom for local sources; not yet confirmed from inside
#           a live session), so pulled edits apply at the next session start
#           or /reload-plugins. `plugin update` is gated on
#           the version: same version -> "already at the latest version", a
#           bumped one -> re-recorded. `plugin marketplace update <name>`
#           re-reads the catalog ("Validating local marketplace").
#   codex   copies the plugin into $CODEX_HOME/plugins/cache/<m>/<p>/<ver> and
#           runs that copy. There is no update command for a local marketplace:
#           `plugin marketplace upgrade` refreshes Git marketplaces only (a
#           local one fails with "is not configured as a Git marketplace").
#           Re-running `plugin add` re-copies the cache from the source, deleted
#           files included, but also re-enables a disabled plugin.

_TREE_SKIP_DIRS = frozenset(("__pycache__", ".git", ".pytest_cache"))
_TREE_SKIP_FILES = frozenset((".DS_Store",))


def tree_digest(root):
    """{relative path: sha256} of the files under ``root``, bytecode and OS
    litter left out; None when ``root`` is not a directory."""
    if not os.path.isdir(root):
        return None
    digest = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _TREE_SKIP_DIRS)
        for name in files:
            if name in _TREE_SKIP_FILES or name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                with open(path, "rb") as fh:
                    digest[rel] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                digest[rel] = None
    return digest


def codex_cache_dir(version_text):
    return os.path.join(codex_home(), "plugins", "cache", marketplace_name(),
                        PLUGIN_NAME, version_text)


def _codex_market_is_git(market):
    """From `codex plugin marketplace list --json`: marketplaceSource.sourceType."""
    if not isinstance(market, dict):
        return False
    kind = (market.get("marketplaceSource") or {}).get("sourceType")
    return kind not in (None, "local")


def refresh_commands(agent, binary):
    """Exact argv for `install --refresh`, per agent (see the notes above)."""
    name, pid = marketplace_name(), plugin_id()
    if agent == CLAUDE:
        return {
            "market_update": [binary, "plugin", "marketplace", "update", name],
            "update": [binary, "plugin", "update", pid, "--scope", "user"],
        }
    return {
        "market_update": [binary, "plugin", "marketplace", "upgrade", name],
        "readd": [binary, "plugin", "add", pid],
        "remove": [binary, "plugin", "remove", pid],
    }


def plugin_refresh(agent, binary, out, state):
    """Bring an installed plugin up to this checkout. Idempotent: a second run
    finds nothing to do and changes nothing."""
    cmds = refresh_commands(agent, binary)
    ours = state.market_is_ours
    current = version()
    installed = str(state.plugin.get("version") or "") or None
    if agent == CLAUDE:
        # Cheap and harmless for a local catalog; pulls a Git one.
        if not _do(cmds["market_update"], out, "updating the marketplace"):
            return False
        if not ours or (installed and installed != current):
            argv = list(cmds["update"])
            scope = state.plugin.get("scope")
            if scope in ("user", "project", "local"):
                argv[-1] = scope
            if not _do(argv, out, "updating the plugin"):
                return False
        if ours:
            out.say("  %s: loads Last Call in place from %s; restart open sessions "
                    "(or run /reload-plugins) to pick up the pull" % (agent, PLUGIN_ROOT))
        return True

    if not state.enabled:
        out.say("  %s: %s is disabled; not refreshed (re-adding it would enable it)"
                % (agent, plugin_id()))
        return True
    if not ours:
        if _codex_market_is_git(state.market):
            if not _do(cmds["market_update"], out, "upgrading the marketplace"):
                return False
    else:
        cache = codex_cache_dir(installed or current)
        if installed == current and tree_digest(cache) == tree_digest(PLUGIN_ROOT):
            out.say("  %s: plugin cache already matches this checkout (%s)" % (agent, cache))
            return True
        out.say("  %s: no update command for a local marketplace (`plugin marketplace "
                "upgrade` is Git-only); re-adding the plugin to re-copy its cache" % agent)
    if _do(cmds["readd"], out, "re-adding the plugin"):
        return True
    out.say("  %s: falling back to remove + add of the plugin (the marketplace stays)"
            % agent)
    return (_do(cmds["remove"], out, "removing the plugin")
            and _do(cmds["readd"], out, "adding the plugin"))


def plugin_uninstall(agent, binary, out):
    cmds = plugin_commands(agent, binary)
    state = plugin_state(agent, binary, out)
    ok = True
    if state.plugin is not None or not state.known:
        argv = list(cmds["uninstall"])
        scope = state.plugin.get("scope") if state.plugin else None
        if agent == CLAUDE and scope in ("user", "project", "local"):
            argv[-1] = scope
        ok = _do(argv, out, "uninstalling the plugin") and ok
    else:
        out.say("  %s: %s is not installed" % (agent, plugin_id()))
    if state.market is not None:
        if state.market_is_ours:
            ok = _do(cmds["market_remove"], out, "removing the marketplace") and ok
        else:
            out.say("  %s: leaving marketplace %s (registered from %s, not by this checkout)"
                    % (agent, marketplace_name(), _describe_source(state.market)))
    return ok


# ---------------------------------------------------------------- machine setup

GLOBAL_CONFIG_NAME = "config.json"


def global_config_text():
    try:
        with open(EXAMPLE_CONFIG, encoding="utf-8") as fh:
            example = json.load(fh)
    except (OSError, ValueError):
        example = {}
    options = {k: v for k, v in example.items() if not k.startswith("_")}
    doc = {
        "_comment": ("Last Call machine-wide settings. Nothing here is active yet: "
                     "move a key out of _example (to the top level) to turn it on. "
                     "A project's .claude/lastcall.json overrides this file; "
                     "LASTCALL_<FIELD> in the environment overrides both."),
        "_comment_docs": "Every option is explained in %s" % EXAMPLE_CONFIG,
        "_example": options,
    }
    return json.dumps(doc, indent=2) + "\n"


def ensure_global_dir(out):
    base = lastcall_home()
    path = os.path.join(base, GLOBAL_CONFIG_NAME)
    if os.path.exists(path):
        out.say("  config: %s already exists, left as is" % path)
        return
    if out.dry_run:
        out.step("create %s with a commented example" % path)
        return
    os.makedirs(base, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(global_config_text())
    out.say("  config: created %s (commented example, nothing active)" % path)


def _on_path(directory):
    target = os.path.normcase(os.path.realpath(directory))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.normcase(os.path.realpath(os.path.expanduser(entry))) == target:
            return True
    return False


def default_bin_dir():
    return os.path.join(home(), ".local", "bin")


def link_name(directory):
    return os.path.join(directory, "lastcall.cmd" if os.name == "nt" else "lastcall")


def _link_is_ours(path):
    if os.path.islink(path):
        return _same_path(path, LAUNCHER)
    if os.name == "nt" and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return LAUNCHER_CMD in fh.read()
        except OSError:
            return False
    return False


def link_bin(directory, out, explicit):
    path = link_name(directory)
    if not explicit and not _on_path(directory):
        out.say("  command: %s is not on PATH, so no `lastcall` link was made.\n"
                "           Use --link-bin DIR, or run %s directly." % (directory, LAUNCHER))
        return True
    if os.path.lexists(path):
        if _link_is_ours(path):
            out.say("  command: %s already points here" % path)
            return True
        out.say("  ! command: %s exists and is not ours; left alone" % path)
        return False
    if out.dry_run:
        out.step("link %s -> %s" % (path, LAUNCHER))
        return True
    os.makedirs(directory, exist_ok=True)
    if os.name == "nt":
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('@echo off\r\ncall "%s" %%*\r\n' % LAUNCHER_CMD)
    else:
        os.symlink(LAUNCHER, path)
    out.say("  command: linked %s -> %s" % (path, LAUNCHER))
    if not _on_path(directory):
        out.say("           (%s is not on PATH yet; add it to use `lastcall`)" % directory)
    return True


def unlink_bin(directory, out):
    path = link_name(directory)
    if not os.path.lexists(path):
        return
    if not _link_is_ours(path):
        out.say("  command: %s is not ours; left alone" % path)
        return
    if out.dry_run:
        out.step("remove %s" % path)
        return
    os.remove(path)
    out.say("  command: removed %s" % path)


# ---------------------------------------------------------------- install / uninstall

def agent_binary(agent):
    return shutil.which(agent)


def choose_agents(args):
    chosen = [a for a in AGENT_ORDER if getattr(args, a)]
    if chosen and not args.all:
        return chosen, True
    found = [a for a in AGENT_ORDER if agent_binary(a)]
    return (found, False)


def trust_note(out):
    out.say("")
    out.say("Codex: new hooks do not run until you trust them once. Open Codex,")
    out.say("run /hooks, and trust the Last Call entries. The trust is stored in")
    out.say("%s/config.toml, so the Codex desktop app shares it." % codex_home())


def desktop_note(out, agents):
    out.say("")
    out.say("Desktop apps use the same user-level configuration as the CLIs:")
    if CLAUDE in agents:
        out.say("  - Claude desktop runs a bundled Claude Code that reads %s" % claude_home())
    if CODEX in agents:
        out.say("  - Codex desktop runs codex with CODEX_HOME=%s" % codex_home())
    out.say("Restart any open app or CLI session to load Last Call.")


def command_name():
    """`lastcall` when that name reaches this checkout, else the launcher's
    full path, so a printed hint always works when pasted."""
    found = shutil.which("lastcall")
    if found and _same_path(found, LAUNCHER):
        return "lastcall"
    return quote(LAUNCHER) if " " in LAUNCHER else LAUNCHER


def cmd_install(args):
    out = Out(dry_run=args.dry_run)
    project = os.path.abspath(os.path.expanduser(args.project)) if args.project else None
    if project and args.method == "plugin":
        if args.method_given:
            out.say("--project installs write hooks into the project; use --method hooks")
            return 2
        args.method = "hooks"
    agents, explicit = choose_agents(args)
    if project and not explicit:
        agents = [CLAUDE]
    if not agents:
        out.say("No agent found on PATH (looked for: %s)." % ", ".join(AGENT_ORDER))
        out.say("Name one explicitly: lastcall install --claude --method hooks")
        return 1
    if args.refresh and args.method != "plugin":
        out.say("--refresh updates the agents' plugin copies; the hooks method runs "
                "this checkout directly, so there is nothing to refresh")
        return 2
    header = "%s Last Call %s (%s method) for: %s%s" % (
        "Refreshing" if args.refresh else "Installing", version(), args.method,
        ", ".join(agents), "  [dry run]" if args.dry_run else "")
    out.say(header)
    if project:
        out.say("  project: %s" % project)

    interpreter = None
    if args.method == "hooks":
        if not os.path.isfile(HOOK_SCRIPT):
            out.say("cannot find the hook script at %s" % HOOK_SCRIPT)
            return 1
        interpreter = hook_interpreter(args.python)
        if not interpreter:
            out.say("no working Python 3 interpreter found on PATH.\n"
                    "Install Python 3.9+ and re-run, or pass --python PATH.")
            return 1

    failed = []
    for agent in agents:
        try:
            if args.method == "hooks":
                ok = hooks_install(agent, out, interpreter, project)
            else:
                binary = agent_binary(agent)
                if not binary:
                    out.say("  ! %s: `%s` is not on PATH; install it, or use --method hooks"
                            % (agent, agent))
                    ok = False
                else:
                    ok = plugin_install(agent, binary, out, refresh=args.refresh)
                    if ok:
                        # A 1.x hooks-method install left behind would fire
                        # every event twice alongside the plugin.
                        hooks_uninstall(agent, out, quiet_if_absent=True)
        except InstallError as error:
            out.say("  ! %s: %s" % (agent, error))
            ok = False
        if not ok:
            failed.append(agent)

    if not project:
        try:
            ensure_global_dir(out)
        except OSError as error:
            out.say("  ! config: %s" % error)
        if not args.no_link_bin:
            directory = os.path.abspath(os.path.expanduser(args.link_bin or default_bin_dir()))
            try:
                link_bin(directory, out, explicit=bool(args.link_bin))
            except OSError as error:
                out.say("  ! command: %s" % error)

    installed = [a for a in agents if a not in failed]
    if CODEX in installed:
        trust_note(out)
    if installed and not project:
        desktop_note(out, installed)
    if installed and not args.dry_run and not args.refresh:
        out.say("")
        command = command_name()
        out.say("Last Call will WARN when context runs low. Handing work over to a")
        out.say("fresh session is off until you configure it:")
        out.say("  %s setup" % command)
        out.say("Check what is and is not wired up:")
        out.say("  %s doctor" % command)
    if failed:
        out.say("\nFailed for: %s" % ", ".join(failed))
        return 1
    return 0


def cmd_uninstall(args):
    out = Out(dry_run=args.dry_run)
    project = os.path.abspath(os.path.expanduser(args.project)) if args.project else None
    chosen = [a for a in AGENT_ORDER if getattr(args, a)]
    if chosen and not args.all:
        agents = chosen
    elif project:
        agents = [CLAUDE]
    else:
        # Remove whatever is there, whether or not the agent is still on PATH.
        agents = list(AGENT_ORDER)
    methods = [args.method] if args.method_given else (["hooks"] if project else ["plugin", "hooks"])
    out.say("Removing Last Call (%s) for: %s%s" % (
        " + ".join(methods), ", ".join(agents), "  [dry run]" if args.dry_run else ""))
    failed = []
    for agent in agents:
        try:
            ok = True
            if "plugin" in methods:
                binary = agent_binary(agent)
                if binary:
                    ok = plugin_uninstall(agent, binary, out) and ok
                elif args.method_given:
                    out.say("  ! %s: `%s` is not on PATH, cannot remove its plugin" % (agent, agent))
                    ok = False
            if "hooks" in methods:
                ok = hooks_uninstall(agent, out, project,
                                     quiet_if_absent=not args.method_given) and ok
        except InstallError as error:
            out.say("  ! %s: %s" % (agent, error))
            ok = False
        if not ok:
            failed.append(agent)
    if not project:
        unlink_bin(os.path.abspath(os.path.expanduser(args.link_bin or default_bin_dir())), out)
        if os.path.isdir(lastcall_home()):
            out.say("  config: %s left in place (your settings)" % lastcall_home())
    if failed:
        out.say("\nFailed for: %s" % ", ".join(failed))
        return 1
    return 0


# ---------------------------------------------------------------- delegates

def register_usage_providers():
    """Give `status` a CONTEXT column: each live session measured by its own
    agent's adapter."""
    from . import sessions
    from .agents import get_agent

    def provider(rec):
        agent = get_agent(rec.agent)
        if agent is None or not rec.transcript_path:
            return None
        return agent.read_usage(rec.transcript_path, rec.session_id)

    for name in AGENT_ORDER:
        sessions.register_usage_provider(name, provider)


def cmd_sessions(name, rest):
    from . import cli_sessions
    if name == "status":
        register_usage_providers()
    return cli_sessions.main([name] + list(rest))


def cmd_script(name, rest):
    """doctor and setup live in the hook script; run it rather than import it,
    so its module-level state stays its own."""
    if not os.path.isfile(HOOK_SCRIPT):
        sys.stderr.write("cannot find the hook script at %s\n" % HOOK_SCRIPT)
        return 1
    try:
        return subprocess.call([sys.executable, HOOK_SCRIPT, name] + list(rest))
    except KeyboardInterrupt:
        return 130


def cmd_relay(_name, rest):
    """Hand over to a fresh Claude Code or Codex session (relay.py)."""
    from . import relay
    try:
        return relay.main(list(rest), prog="lastcall relay")
    except KeyboardInterrupt:
        return 130


def cmd_version(_args):
    print("lastcall %s" % version())
    print("  checkout : %s" % REPO_ROOT)
    print("  python   : %s (%s)" % (sys.executable, sys.version.split()[0]))
    return 0


DELEGATES = {
    "status": ("live sessions of both agents, with context usage", cmd_sessions),
    "tidy": ("propose names and groups for old chats (read-only unless --apply)", cmd_sessions),
    "doctor": ("check what is and is not wired up", cmd_script),
    "setup": ("configure the handoff (interactive wizard)", cmd_script),
    "relay": ("hand over to a fresh Claude Code or Codex session (--dry-run to preview)",
              cmd_relay),
}


def build_parser():
    p = argparse.ArgumentParser(
        prog="lastcall",
        description="Last Call: context warnings and handoffs for Claude Code and Codex.",
        epilog="Run `lastcall <command> --help` for a command's options.")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True

    def agent_flags(sp, verb):
        sp.add_argument("--claude", action="store_true", help="%s for Claude Code" % verb)
        sp.add_argument("--codex", action="store_true", help="%s for Codex" % verb)
        sp.add_argument("--all", action="store_true",
                        help="every agent found on PATH (the default)")
        sp.add_argument("--method", choices=("plugin", "hooks"), default=None,
                        help="plugin: register this checkout as a local marketplace "
                             "(default). hooks: write user-level hook entries directly")
        sp.add_argument("--dry-run", action="store_true",
                        help="print what would change, change nothing")
        sp.add_argument("--project", metavar="DIR",
                        help="hooks method only: write into DIR/.claude (and DIR/.codex) "
                             "instead of the user-level files")

    ins = sub.add_parser("install", help="install for Claude Code and/or Codex (CLI and desktop)")
    agent_flags(ins, "install")
    ins.add_argument("--link-bin", metavar="DIR",
                     help="symlink the `lastcall` command into DIR "
                          "(default: ~/.local/bin, when it is on PATH)")
    ins.add_argument("--no-link-bin", action="store_true", help="do not link the command")
    ins.add_argument("--refresh", action="store_true",
                     help="plugin method, after `git pull`: bring each agent's installed "
                          "copy up to this checkout (installs it where missing)")
    ins.add_argument("--python", metavar="PATH",
                     help="hooks method: interpreter the hook commands name (default: python3)")
    ins.set_defaults(func=cmd_install)

    un = sub.add_parser("uninstall", help="reverse install (backups are kept)")
    agent_flags(un, "uninstall")
    un.add_argument("--link-bin", metavar="DIR", help="where the command was linked")
    un.set_defaults(func=cmd_uninstall)

    for name, (text, _fn) in DELEGATES.items():
        sub.add_parser(name, help=text, add_help=False)

    ver = sub.add_parser("version", help="print the version and where it runs from")
    ver.set_defaults(func=cmd_version)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in DELEGATES:
        return DELEGATES[argv[0]][1](argv[0], argv[1:])
    if argv and argv[0] in ("-V", "--version"):
        argv = ["version"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "method"):
        args.method_given = args.method is not None
        args.method = args.method or "plugin"
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
