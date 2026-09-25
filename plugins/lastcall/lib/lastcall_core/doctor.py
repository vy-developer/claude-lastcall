"""`lastcall.py doctor`: show exactly what the guard resolves.

The whole failure mode of this class of tool is looking healthy while doing
nothing. This is the antidote: run it and see the real numbers, for either
agent's transcript.
"""

import os
import shutil

from .agents import detect_agent
from .config import load_config, state_dir
from .engine import effective_window, read_usage
from .render import RELAY_SCRIPT, read_template
from .state import read_state
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

    ready, checks = handover_status(config)
    print("\n  automatic handover: %s" % ("READY" if ready else "NOT SET UP"))
    for label, ok in checks.items():
        print("    %s %s" % ("ok  " if ok else "MISS", label))
    if not ready:
        print("    -> Last Call will warn, but nothing will carry the work")
        print("       forward; the session just ends. Run: lastcall.py setup")

    if not transcript:
        print("\nPass a transcript path to measure a real session, e.g.")
        print("  lastcall.py doctor ~/.claude/projects/<project>/<session>.jsonl")
        print("  lastcall.py doctor ~/.codex/sessions/YYYY/MM/DD/rollout-<...>.jsonl")
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
    if assumed:
        print("  window        : %s tokens (ASSUMED — nothing reported the "
              "window; zones warn but never block)" % _fmt(window))
        print("                  Set context_window_tokens or install the "
              "status line to make it exact.")
    else:
        print("  window        : %s tokens (%s)" % (_fmt(window), source))
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
