"""`lastcall doctor`: show exactly what the guard resolves.

The whole failure mode of this class of tool is looking healthy while doing
nothing. This is the antidote: run it and see the real numbers, for either
agent's transcript.
"""

import json
import os
import shutil

from .agents import detect_agent
from .config import load_config, state_dir
from .engine import effective_window, read_usage
from .render import RELAY_SCRIPT, cli_command, read_template
from .state import read_state
from .windows import (learned_conflict, learned_path, learned_window, load_learned,
                      match_window)
from .zones import describe_threshold, resolve_zones, zone_for


def handover_status(config):
    """What would actually happen at the end of a session, as facts.

    A guard that tells the assistant to hand over, in a project where nothing
    can receive the handover, is silent failure wearing a different hat.
    """
    # EVERY body the session could be shown, not the first one found. Zones
    # sort ascending, so stopping at the first readable template only ever
    # inspected the lowest zone — which deliberately has no relay in it.
    template = config.get("template")
    bodies = []
    if template:
        bodies.append(read_template(config, template))
    for zone in resolve_zones(config):
        if zone.get("template"):
            bodies.append(read_template(config, zone["template"]))
        if zone.get("message"):
            bodies.append(zone["message"])
    bodies = [b for b in bodies if b]

    def invokes_relay(text):
        # "{relay}" counts: it is the placeholder that BECOMES the relay command.
        return ("{relay}" in text or RELAY_SCRIPT in text or "relay.py" in text
                or "lastcall relay" in text or "handoff.sh" in text)

    from .relay import readiness
    wired = any(invokes_relay(b) for b in bodies)
    checks = {
        "template configured": bool(bodies),
        "template invokes the relay": wired,
    }
    # relay.py, git, and the successor's CLI (claude or codex); tmux only for
    # codex_mode "tmux".
    checks.update(readiness(config.get("relay"), shutil.which))
    ready = all(checks.values())
    return ready, checks


def _fmt(number):
    return "{:,}".format(number)


def _when(epoch):
    import time
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def windows_report(config):
    """Lines describing the `windows` map and the learned windows."""
    lines = ["\n  windows (model -> context window)"]
    mapped = config.get("windows") or {}
    if mapped:
        for key in sorted(mapped, key=lambda k: k.lower()):
            lines.append("    map      %-34s %s" % (key, _fmt(mapped[key])))
    else:
        lines.append('    map      (none — set "windows" in your config, e.g. '
                     '{"claude-opus-5-5": 1000000})')
    learned = load_learned(config)
    entries = [(key, entry) for key, entry in learned.items()
               if isinstance(entry, dict) and isinstance(entry.get("window"), int)]
    if entries:
        for key, entry in sorted(entries):
            seen = entry.get("seen") if isinstance(entry.get("seen"), dict) else {}
            others = sorted(int(w) for w in seen if str(w).isdigit()
                            and int(w) != entry["window"])
            note = ""
            if others:
                note = "  (also seen: %s — pin it in \"windows\")" % ", ".join(
                    _fmt(w) for w in others)
            lines.append("    learned  %-34s %s  from %s, %s%s" % (
                key, _fmt(entry["window"]), entry.get("source") or "?",
                _when(entry.get("at")), note))
            conflict = entry.get("conflict")
            if isinstance(conflict, dict):
                lines.append("             CONFLICT: a session on this model auto-compacted at "
                             "%s tokens (%s), so it also runs with a smaller window." % (
                                 _fmt(conflict.get("pre_tokens") or 0), _when(conflict.get("at"))))
                lines.append("             The learned window is IGNORED (assumed fallback "
                             "instead): pin the model in \"windows\".")
    else:
        lines.append("    learned  (none yet: %s)" % learned_path(config))
    return lines


# ---------------------------------------------------------------- install state
#
# Read from the agents' own files, never by running them: `claude plugin list`
# alone takes most of a second, and doctor has to stay quick. The formats were
# checked against Claude Code 2.1.281 and Codex CLI 0.153.4 installing into
# throwaway config dirs:
#   claude  settings.json "enabledPlugins": {"<id>": true};
#           plugins/installed_plugins.json {"plugins": {"<id>": [{"scope": ...}]}}
#   codex   config.toml [plugins."<id>"] enabled = true, and one
#           [hooks.state."<source>:<event>:<group>:<index>"] trusted_hash per
#           hook trusted through /hooks, where <source> is "<id>:hooks/hooks.json"
#           for a plugin's hooks and the hooks.json path for the hooks method.

_SNAKE_EVENTS = {"Stop": "stop", "SessionStart": "session_start",
                 "PostCompact": "post_compact", "PostToolUse": "post_tool_use",
                 "UserPromptSubmit": "user_prompt_submit"}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def agent_install_state(agent):
    """What is installed for ``agent`` at user level, from its files alone:
    {"plugin": None | "enabled" | "disabled", "hooks": <our hooks-method
    entries>, "hooks_file": path, "trusted": <events trusted> | None}."""
    from . import cli, tomlish

    pid = cli.plugin_id()
    hooks_path = cli.hooks_file(agent)
    state = {"hooks_file": hooks_path, "trusted": None,
             "hooks": cli.count_ours(_read_json(hooks_path))}
    if agent == cli.CLAUDE:
        home = cli.claude_home()
        installed = (_read_json(os.path.join(home, "plugins", "installed_plugins.json"))
                     .get("plugins") or {})
        enabled = (_read_json(os.path.join(home, "settings.json"))
                   .get("enabledPlugins") or {})
        if isinstance(installed, dict) and installed.get(pid):
            state["plugin"] = ("enabled" if isinstance(enabled, dict)
                               and enabled.get(pid) is True else "disabled")
        else:
            state["plugin"] = None
        return state

    flat = tomlish.load(os.path.join(cli.codex_home(), "config.toml"))
    if any(key[:2] == ("plugins", pid) for key in flat):
        state["plugin"] = ("disabled" if flat.get(("plugins", pid, "enabled")) is False
                           else "enabled")
    else:
        state["plugin"] = None
    sources = []
    if state["plugin"]:
        sources.append(lambda source: source.startswith(pid + ":"))
    if state["hooks"]:
        paths = {os.path.normcase(os.path.abspath(hooks_path)),
                 os.path.normcase(os.path.realpath(hooks_path))}
        sources.append(lambda source: os.path.normcase(source) in paths)
    if sources:
        wanted = set(_SNAKE_EVENTS.values())
        trusted = set()
        for key, value in flat.items():
            if len(key) != 4 or key[:2] != ("hooks", "state") or key[3] != "trusted_hash":
                continue
            parts = key[2].rsplit(":", 3)
            if value and len(parts) == 4 and parts[1] in wanted \
                    and any(match(parts[0]) for match in sources):
                trusted.add(parts[1])
        state["trusted"] = len(trusted)
    return state


def install_report(which=shutil.which):
    """Lines describing how Last Call is installed for each agent on PATH."""
    from . import cli

    pid = cli.plugin_id()
    total = len(_SNAKE_EVENTS)
    lines = ["\n  install (user level)"]
    for agent in cli.AGENT_ORDER:
        label = "    %-9s" % agent
        pad = " " * len(label)
        if not which(agent):
            lines.append("%snot on PATH (skipped)" % label)
            continue
        state = agent_install_state(agent)
        plugin = state["plugin"]
        if plugin == "enabled":
            lines.append("%sok   plugin %s enabled" % (label, pid))
        elif plugin == "disabled":
            if agent == cli.CLAUDE:
                fix = "claude plugin enable %s" % pid
            else:
                fix = 'set enabled = true under [plugins."%s"] in %s' % (
                    pid, os.path.join(cli.codex_home(), "config.toml"))
            lines.append("%sMISS plugin %s installed but DISABLED -> %s" % (label, pid, fix))
        else:
            lines.append("%s---- plugin %s not installed" % (label, pid))
        if state["hooks"]:
            lines.append("%sok   hooks method: %d entries in %s"
                         % (pad, state["hooks"], state["hooks_file"]))
        else:
            lines.append("%s---- hooks method: none in %s" % (pad, state["hooks_file"]))
        if plugin and state["hooks"]:
            lines.append("%sPROBLEM both are installed, so every hook fires twice -> %s"
                         % (pad, cli_command("install --%s" % agent)))
        elif not plugin and not state["hooks"]:
            lines.append("%sMISS Last Call is not installed for %s -> %s"
                         % (pad, agent, cli_command("install --%s" % agent)))
        if state["trusted"] is not None:
            if state["trusted"] >= total:
                lines.append("%sok   hook trust: %d/%d Last Call hooks trusted"
                             % (pad, total, total))
            else:
                lines.append("%sMISS hook trust: %d/%d Last Call hooks trusted -> run "
                             "/hooks in Codex to trust them (until then they do not run)"
                             % (pad, state["trusted"], total))
    return lines


def doctor(argv, version="?", env=None):
    env = os.environ if env is None else env
    payload = {"cwd": os.getcwd()}
    transcript = None
    for arg in argv:
        if not arg.startswith("-"):
            transcript = arg
    config = load_config(payload, env)

    print("lastcall %s" % version)
    print("  project dir   : %s" % config["_project_dir"])
    print("  config file   : %s" % (config["_config_path"] or "(none — using defaults)"))
    print("  global config : %s" % (config["_global_config_path"]
                                    or "(none)"))
    print("  state dir     : %s" % state_dir(config, env))
    for problem in config.get("_problems") or []:
        print("  PROBLEM       : %s" % problem)

    zones = resolve_zones(config)
    if zones:
        print("  zones         : %s" % ("  ".join(
            "%s@%s%s%s" % (
                zone["name"], describe_threshold(zone),
                "[block]" if zone["block"] else "",
                "[own text]" if (zone["template"] or zone["message"]) else "",
            ) for zone in zones)))
    else:
        print("  zones         : NONE CONFIGURED — nothing can ever fire")
    print("  mode          : %s" % config["mode"])
    print("  include output: %s" % bool(config["include_output_tokens"]))
    print("  template      : %s" % (config["template"] or "(built-in default)"))
    fallback = config.get("fallback_window_tokens")
    print("  fallback window: %s" % (
        "%s tokens (Claude Code, when nothing reports the window)" % _fmt(fallback)
        if fallback else "off — stay silent while the window is unknown"))
    print("  disabled      : %s" % bool(config["disabled"]))
    for line in windows_report(config):
        print(line)
    try:
        for line in install_report():
            print(line)
    except Exception as error:  # noqa: BLE001 - one broken file must not end doctor
        print("\n  install (user level): could not read (%s)" % error)

    ready, checks = handover_status(config)
    print("\n  automatic handover: %s" % ("READY" if ready else "NOT SET UP"))
    for label, ok in checks.items():
        print("    %s %s" % ("ok  " if ok else "MISS", label))
    if not ready:
        print("    -> Last Call will warn, but nothing will carry the work")
        print("       forward; the session just ends. Run: %s" % cli_command("setup"))

    if not transcript:
        doctor_command = cli_command("doctor")
        print("\nPass a transcript path to measure a real session, e.g.")
        print("  %s ~/.claude/projects/<project>/<session>.jsonl" % doctor_command)
        print("  %s ~/.codex/sessions/YYYY/MM/DD/rollout-<...>.jsonl" % doctor_command)
        return 0

    agent = detect_agent({"transcript_path": transcript}, env)
    print("\n  transcript    : %s" % transcript)
    print("  agent         : %s" % agent.name)
    if not os.path.isfile(transcript):
        print("  RESULT        : cannot measure (no such file)")
        print("  -> the guard would stay SILENT for this session.")
        return 1
    try:
        usage = read_usage(agent, config, {"transcript_path": transcript})
    except OSError as error:
        print("  RESULT        : cannot measure (%s)" % error)
        return 1
    if usage is None:
        print("  RESULT        : cannot measure (no usage record)")
        print("  -> the guard would stay SILENT for this session.")
        return 1

    state = read_state(config, usage.session_id, agent.name) if usage.session_id else {}
    window, source, assumed = effective_window(agent, config, state, usage, env)
    tokens = usage.tokens
    print("  session       : %s" % (usage.session_id or "(unknown)"))
    print("  model         : %s" % (usage.model or "(unknown)"))
    print("  in use        : %s tokens" % _fmt(tokens))
    if usage.stale:
        print("  compaction    : NEWER than the last usage record — the count "
              "above is pre-compaction, so the guard waits for a fresh one")
    elif usage.compacted:
        print("  compaction    : yes, since the previous response — zones re-arm")
    else:
        print("  compaction    : none since the previous response")

    zone = zone_for(tokens, window, zones)
    band = zone["name"] if zone else "green"
    if window is None and any(z["at_tokens"] is not None for z in zones):
        # Absolute zones need no window, and the hook fires them without one.
        print("  window        : UNKNOWN — not needed, zones in tokens fire "
              "on the count alone")
        if any(z["at_tokens"] is None for z in zones):
            print("  PROBLEM       : the percentage zones are skipped until the "
                  "window is known")
        print("  band          : %s" % band.upper())
        return 0
    if window is None:
        print("  window        : UNKNOWN")
        print("\n  -> the guard stays SILENT: the window is not known and no")
        print("     fallback applies. Fix it either way:")
        print("       1. set \"context_window_tokens\" in .lastcall.json or")
        print("          ~/.lastcall/config.json")
        print("       2. or install the status line, which is handed the real")
        print("          number by Claude Code (see README).")
        return 1
    percent = (tokens * 100.0) / window
    conflict = learned_conflict(load_learned(config), agent.name, usage.model) \
        if agent.name == "claude" else None
    if assumed and source == "learned":
        print("  window        : %s tokens (LEARNED from another session on this "
              "model, not proven by this one; zones warn but never block)" % _fmt(window))
        print("                  The same model id runs with 200K or 1M: pin it in "
              "\"windows\" to make it exact.")
    elif assumed:
        print("  window        : %s tokens (ASSUMED — nothing reported the "
              "window; zones warn but never block)" % _fmt(window))
        print("                  Set context_window_tokens or install the "
              "status line to make it exact.")
    else:
        print("  window        : %s tokens (%s)" % (_fmt(window), source))
    print("  window source : %s" % source)
    if conflict and source not in ("map", "config", "statusline"):
        print("  NOTE          : %s has been seen with more than one window (a session "
              "auto-compacted at %s), so what was learned about it is ignored."
              % (usage.model, _fmt(conflict.get("pre_tokens") or 0)))
        print("                  Pin it in \"windows\", e.g. {\"%s\": 200000}."
              % usage.model)
    if source == "map":
        _w, key = match_window(config.get("windows"), agent.name, usage.model)
        print("                  matched windows[\"%s\"]" % key)
    elif source == "learned":
        _w, entry = learned_window(load_learned(config), agent.name, usage.model)
        if entry:
            print("                  learned from %s on %s"
                  % (entry.get("source") or "?", _when(entry.get("at"))))
    if source.startswith("config says"):
        print("  PROBLEM       : context_window_tokens in your config is wrong.")
        print("                  Set it to %s, or delete it and let the status"
              % _fmt(window))
        print("                  line report the real figure.")
    floor = config.get("min_window_tokens")
    if floor and window < int(floor):
        print("  floor         : window below min_window_tokens (%s) — SILENT"
              % _fmt(int(floor)))
    print("  percent       : %.1f%%" % percent)
    print("  band          : %s" % band.upper())
    print("  headroom      : %s tokens" % _fmt(max(0, window - tokens)))
    return 0
