"""Configuration: where it lives, how it is layered, and what is wrong with it.

Agent-neutral. The same file configures Last Call under Claude Code and under
Codex, and the search order is, later wins:

    built-in DEFAULTS
    < ~/.lastcall/config.json                        (global; $LASTCALL_HOME)
    < the nearest project config, walking up from the project directory:
          .lastcall.json
          .lastcall/config.json
          .claude/lastcall.json                      (legacy, still read)
          .codex/lastcall.json
    < LASTCALL_<FIELD> environment variables

The home directory is never a project. ~/.claude and ~/.codex exist on every
machine that runs those agents, so treating either as a project marker used to
resolve every directory without a marker of its own to $HOME.
"""

import json
import os

# --------------------------------------------------------------------------
# Defaults. Every one of these is overridable by config file or environment.
# --------------------------------------------------------------------------

DEFAULTS = {
    # Percentages of the context window, not absolute tokens. Absolute
    # thresholds are why the original version of this tool was a silent no-op
    # on any model that wasn't the one its author used.
    #
    # 40/55 is not a guess. It is the ladder from the hook this was rewritten
    # from, which has run ~50 unattended session handoffs over a fortnight, and
    # its author's reasoning is worth repeating: long-context quality degrades
    # well before the window is full, and Anthropic's own agent harness compacts
    # its orchestrator at 100k while capping subagents at 200k. Firing late is
    # the failure that actually costs you a session — by the time the model is
    # at 85% it has already been working from a lossy memory for a while.
    # The ~15-point gap gives in-flight work room to land before red.
    "yellow_percent": 40,
    "red_percent": 55,
    # Define your own zones and you get full control: as many as you like, your
    # names, your thresholds, your instructions, and which ones hold the stop.
    # None means "build the standard two from the percentages above".
    #   [{"name": "wind-down", "at": 60, "template": ".lastcall/winddown.md"},
    #    {"name": "closing",   "at": 85, "block": true}]
    "zones": None,
    # Stay completely silent when the window is smaller than this. Absolute
    # zones are written for the model you normally run; on a smaller one they
    # would fire immediately and mean nothing. null disables the floor.
    "min_window_tokens": None,
    # Commands that must pass before handing over — tests, linters, a review
    # gate. They are surfaced to the assistant as {gates} in your wrap-up
    # template; nothing here executes them, because a hook that runs your test
    # suite at Stop time is a hook that hangs your session.
    "gates": None,
    # A second model asked to check the work before it is handed over. Rendered
    # into the wrap-up as {verifier}.
    "verifier": None,
    # Settings for the optional relay, read by the relay so one file drives
    # everything: handoff_dir, name_prefix, dirty_baseline, remote_control,
    # permission_mode, skip_permissions, model, fallback_model, kill_predecessor.
    "relay": None,
    # None means "work it out from an exact source". Codex writes the window
    # into its rollout, so it is always known there; Claude Code does not, see
    # zones.resolve_window.
    "context_window_tokens": None,
    # Model -> window, for Claude Code sessions whose window nothing reports:
    #   {"claude-opus-5-5": 1000000, "claude-sonnet-*": 200000,
    #    "claude:opus": 1000000}
    # Exact model beats the longest prefix or glob; "claude:" / "codex:" limit
    # an entry to one agent. Merged across the global and project configs.
    # Codex always uses the window its rollout states. See windows.py.
    "windows": None,
    # The window assumed when nothing exact is available and the tokens in use
    # do not prove a bigger one (Claude Code without the status line). Zones
    # computed against an assumed window warn but never block, and the message
    # says the window was assumed. null restores the old behaviour: stay
    # silent until the window is known.
    "fallback_window_tokens": 200_000,
    # "advisory"   — warn at every zone, never block.
    # "block_once" — at a zone with "block": true, also hold the stop ONE time
    #                so the wrap-up actually gets written before the session ends.
    "mode": "block_once",
    # Path to your own wrap-up instructions. Relative paths resolve against the
    # project directory. Without one you get a short generic message.
    "template": None,
    # Injected after a compaction (SessionStart with source "compact"), telling
    # the model its memory is now a summary and to re-read the files it was
    # working from. null means the built-in text, false turns it off, a string
    # replaces it.
    "compaction_note": None,
    # Claude Code reports context-used as input + cache_read + cache_creation.
    # Output is excluded because it is not yet in the window. Turn this on if
    # you would rather budget for the NEXT turn's input, which does include it.
    "include_output_tokens": False,
    # Opt-in, redacted, size-capped. Never on by default: hook payloads carry
    # the full text of the last assistant message.
    "debug": False,
    "state_dir": None,          # default: ~/.lastcall/state
    "state_ttl_days": 14,
    "disabled": False,
}

ENV_PREFIX = "LASTCALL_"
HOME_ENV = "LASTCALL_HOME"

# Nearest directory wins; within one directory the first name found wins, and
# any other present alongside it is reported by doctor as ignored.
PROJECT_CONFIG_NAMES = (
    ".lastcall.json",
    os.path.join(".lastcall", "config.json"),
    os.path.join(".claude", "lastcall.json"),
    os.path.join(".codex", "lastcall.json"),
)
# Where new project configs are written.
PROJECT_CONFIG_NAME = PROJECT_CONFIG_NAMES[0]
# Directories that mark a project root when no config names one.
PROJECT_MARKERS = (".git", ".lastcall", ".claude", ".codex")

# Coercion is driven by the key, never by the default value. Inferring it from
# the default means every field defaulting to None looks numeric, so an
# environment override like LASTCALL_STATE_DIR=/tmp/x parses as a failed
# number and silently becomes None — the override vanishes without a word.
_FLOAT_KEYS = frozenset(("yellow_percent", "red_percent"))
_INT_KEYS = frozenset(("context_window_tokens", "state_ttl_days",
                       "min_window_tokens", "fallback_window_tokens"))
_BOOL_KEYS = frozenset(("include_output_tokens", "debug", "disabled"))
_JSON_KEYS = frozenset(("zones", "gates", "relay", "windows"))
# Either a string or false.
_TEXT_OR_FALSE_KEYS = frozenset(("compaction_note",))

_EXPECTED_TYPES = {
    "yellow_percent": (int, float),
    "red_percent": (int, float),
    "context_window_tokens": (int,),
    "fallback_window_tokens": (int,),
    "state_ttl_days": (int,),
    "min_window_tokens": (int,),
    "mode": (str,),
    "template": (str,),
    "state_dir": (str,),
    "zones": (list, tuple),
    "gates": (list, tuple, str),
    "verifier": (str,),
    "relay": (dict,),
    "windows": (dict,),
    "include_output_tokens": (bool,),
    "debug": (bool,),
    "disabled": (bool,),
    "compaction_note": (str, bool),
}


def _env(env):
    return os.environ if env is None else env


def home_dir():
    return os.path.realpath(os.path.expanduser("~"))


def lastcall_home(env=None):
    """~/.lastcall, or $LASTCALL_HOME: global config, state, relay ledgers."""
    value = _env(env).get(HOME_ENV)
    return os.path.expanduser(value) if value else os.path.join(
        os.path.expanduser("~"), ".lastcall")


def global_config_path(env=None):
    return os.path.join(lastcall_home(env), "config.json")


def legacy_state_dir():
    """Where state lived before 1.8. Read for migration, never written."""
    return os.path.join(os.path.expanduser("~"), ".claude", "lastcall")


def state_dir(config, env=None):
    override = config.get("state_dir")
    if override:
        return os.path.expanduser(override)
    return os.path.join(lastcall_home(env), "state")


def _is_home(path):
    try:
        return os.path.realpath(path) == home_dir()
    except (OSError, ValueError):
        return False


def _walk_up(start):
    path = os.path.abspath(start)
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def project_configs_in(directory):
    """Every project config file present in ``directory``, in precedence
    order."""
    return [os.path.join(directory, name) for name in PROJECT_CONFIG_NAMES
            if os.path.isfile(os.path.join(directory, name))]


def find_project_config(start):
    """(directory, [config paths]) of the nearest directory at or above
    ``start`` that holds a project config, skipping $HOME. (None, []) when
    there is none."""
    for path in _walk_up(start):
        if _is_home(path):
            continue
        found = project_configs_in(path)
        if found:
            return path, found
    return None, []


def project_dir(payload=None, env=None):
    """Where the project's config and relative template paths live.

    CLAUDE_PROJECT_DIR is set by Claude Code for hooks and is the project root.
    Otherwise `cwd` in the payload (wherever the session is *now*, which may be
    a subdirectory) is a starting point: the nearest directory with a project
    config wins, then the nearest with a project marker (.git, .lastcall,
    .claude, .codex), then cwd itself. $HOME is never a match on the way up.
    """
    env = _env(env)
    payload = payload if isinstance(payload, dict) else {}
    forced = env.get("CLAUDE_PROJECT_DIR")
    if forced and os.path.isdir(forced):
        return forced
    start = payload.get("cwd") or os.getcwd()
    if not isinstance(start, str) or not os.path.isdir(start):
        start = os.getcwd()
    directory, _found = find_project_config(start)
    if directory:
        return directory
    for path in _walk_up(start):
        if _is_home(path):
            continue
        # os.path.exists, not isdir: in a git worktree or submodule .git is a
        # file pointing at the real repository.
        if any(os.path.exists(os.path.join(path, marker)) for marker in PROJECT_MARKERS):
            return path
    return os.path.abspath(start)


def _coerce(key, value):
    """Environment variables arrive as strings; config values arrive typed."""
    if key in _JSON_KEYS:
        if isinstance(value, (list, tuple)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return DEFAULTS[key]
    if key in _BOOL_KEYS:
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if key in _TEXT_OR_FALSE_KEYS:
        text = str(value).strip()
        if text.lower() in ("0", "false", "no", "off"):
            return False
        return text or None
    if key in _FLOAT_KEYS or key in _INT_KEYS:
        text = str(value).strip()
        if text == "" or text.lower() in ("none", "null", "auto"):
            return None
        try:
            number = float(text.replace(",", "").replace("_", ""))
        except ValueError:
            return DEFAULTS[key]
        return number if key in _FLOAT_KEYS else int(number)
    text = str(value).strip()
    return text or None


def _read_json_object(path):
    """(dict, None) or (None, problem)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("expected a JSON object, got %s" % type(loaded).__name__)
        return loaded, None
    except (OSError, ValueError) as error:
        # A broken config must not take the session with it, so the hook
        # path stays quiet. But it is recorded, because doctor has to say it:
        # a config that fails to parse leaves the guard running on defaults,
        # which looks exactly like a config that parsed.
        return None, ("cannot read %s (%s) — every setting in it is ignored"
                      % (path, error))


def _unknown_key_problem(key, path):
    import difflib
    close = difflib.get_close_matches(key, list(DEFAULTS), n=1)
    hint = ' — did you mean "%s"?' % close[0] if close else ""
    return 'unknown setting "%s" in %s is ignored%s' % (key, path, hint)


def load_config(payload=None, env=None):
    """defaults < global < project < environment, validated.

    The returned dict carries every DEFAULTS key plus bookkeeping:
      _project_dir        project root (templates resolve against it)
      _config_path        the project config in effect, or None
      _global_config_path the global config, if it exists
      _config_files       every file that contributed, lowest precedence first
      _configured         whether any config file sets anything: a file holding
                          only "_"-prefixed keys (the commented example the
                          installer writes) does not count; an unreadable one
                          does — someone wrote it
      _problems           human-readable problems, for doctor
    """
    env = _env(env)
    payload = payload if isinstance(payload, dict) else {}
    config = dict(DEFAULTS)
    problems = []
    root = project_dir(payload, env)

    files = []
    global_path = global_config_path(env)
    if os.path.isfile(global_path):
        files.append(global_path)
    _dir, project_files = find_project_config(root)
    project_path = project_files[0] if project_files else None
    for ignored in project_files[1:]:
        problems.append("%s is ignored because %s is in the same directory and "
                        "takes precedence — merge them into one file"
                        % (ignored, project_path))
    if project_path:
        files.append(project_path)

    configured = False
    for path in files:
        loaded, problem = _read_json_object(path)
        if problem:
            problems.append(problem)
            configured = True
            continue
        if any(not str(key).startswith("_") for key in loaded):
            configured = True
        for key, value in loaded.items():
            if key == "windows" and isinstance(value, dict) \
                    and isinstance(config.get(key), dict):
                # A project adds to (and overrides entries of) the global
                # map rather than hiding it: the models are the same machine's.
                merged = dict(config[key])
                merged.update(value)
                config[key] = merged
            elif key in config:
                config[key] = value
            elif not str(key).startswith("_"):
                problems.append(_unknown_key_problem(key, path))

    # Environment overrides individual fields rather than replacing the whole
    # config, so LASTCALL_RED_PERCENT=90 for one run keeps everything else.
    #
    # Both cases are accepted. The config table documents field names in
    # lowercase, so someone copying a name straight out of it gets
    # LASTCALL_red_percent — which used to be ignored in silence.
    for key in DEFAULTS:
        env_value = env.get(ENV_PREFIX + key.upper())
        if env_value is None:
            env_value = env.get(ENV_PREFIX + key)
        if env_value is not None:
            config[key] = _coerce(key, env_value)

    config["_project_dir"] = root
    config["_config_path"] = project_path
    config["_global_config_path"] = global_path if global_path in files else None
    config["_config_files"] = files
    config["_configured"] = configured
    config["_problems"] = problems + validate(config)
    return config


def validate(config):
    """Replace unusable values with their defaults and say what was wrong.

    A misconfigured guard that goes silent is indistinguishable from a healthy
    one with nothing to report. Every problem found here is surfaced by doctor.
    """
    from .zones import DEFAULT_HEADLINES

    problems = []
    for key, types in _EXPECTED_TYPES.items():
        value = config.get(key)
        if value is None:
            continue
        if isinstance(value, bool) and bool not in types:
            problems.append("%s should be %s, got a boolean" % (key, types[0].__name__))
            config[key] = DEFAULTS[key]
        elif not isinstance(value, types):
            problems.append("%s should be %s, got %s"
                            % (key, types[0].__name__, type(value).__name__))
            config[key] = DEFAULTS[key]

    if config.get("compaction_note") is True:
        config["compaction_note"] = None  # true means "yes, the built-in one"

    for key in ("yellow_percent", "red_percent"):
        value = config.get(key)
        if isinstance(value, (int, float)) and not 0 <= value <= 100:
            problems.append("%s is %s; it is a PERCENTAGE of the window, not a "
                            "token count" % (key, value))
            config[key] = DEFAULTS[key]

    for key in ("context_window_tokens", "fallback_window_tokens", "min_window_tokens"):
        value = config.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
            problems.append("%s should be a positive number of tokens, got %s"
                            % (key, value))
            config[key] = DEFAULTS[key]

    from .windows import validate_windows
    config["windows"], window_problems = validate_windows(config.get("windows"))
    problems.extend(window_problems)

    if config.get("mode") not in ("advisory", "block_once"):
        problems.append('mode should be "advisory" or "block_once", got %r'
                        % config.get("mode"))
        config["mode"] = DEFAULTS["mode"]

    template = config.get("template")
    if template and config.get("_project_dir"):
        resolved = template if os.path.isabs(template) else os.path.join(
            config["_project_dir"], template)
        if not os.path.isfile(resolved):
            problems.append("template does not exist: %s" % resolved)

    for zone in (config.get("zones") or []):
        if isinstance(zone, dict) and zone.get("name") not in DEFAULT_HEADLINES \
                and not zone.get("headline") and not zone.get("message") \
                and not zone.get("template"):
            problems.append('zone "%s" has no headline, message or template of '
                            "its own, and only the names %s carry built-in "
                            "wording" % (zone.get("name"),
                                         "/".join(sorted(DEFAULT_HEADLINES))))
    return problems
