#!/usr/bin/env python3
"""Last Call — a hook that watches how full a coding agent's context is.

Works under Claude Code and Codex. Sessions degrade quietly: once the context
window fills, older material is summarized away and the assistant carries on
from a lossy memory of files it *thinks* it read. This hook measures the real
number and, when it crosses a threshold, tells the assistant to stop starting
new work and wrap up.

This file is the stable entry point every installed hook calls:

    python3 .../scripts/lastcall.py <Event>      (payload on stdin)
    python3 .../scripts/lastcall.py doctor [transcript.jsonl]
    python3 .../scripts/lastcall.py setup
    python3 .../scripts/lastcall.py --version

The engine lives in ../lib/lastcall_core (config, zones, state, render,
engine, doctor, wizard) and is standard library only, Python 3.9+. The names
below are re-exported so code that imported this module keeps working.
"""

import os
import sys

__version__ = "1.7.0"

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

try:
    from lastcall_core.agents.claude import count_tokens, latest_usage  # noqa: F401
    from lastcall_core.config import (DEFAULTS, ENV_PREFIX, _coerce,  # noqa: F401
                                      global_config_path, lastcall_home,
                                      load_config, project_dir, state_dir,
                                      validate)
    from lastcall_core.engine import (COMPACTION_DROP_RATIO,  # noqa: F401
                                      handle_event, handle_reset, handle_stop,
                                      measure, run_hook)
    from lastcall_core.render import (DEFAULT_TEMPLATE,  # noqa: F401
                                      HANDOFF_SKELETON, ONBOARDING,
                                      RELAY_COMMAND, RELAY_SCRIPT,
                                      RELAY_TEMPLATE, fill,
                                      format_gates, format_verifier,
                                      read_template, render, zone_body)
    from lastcall_core.state import (prune_state, read_state,  # noqa: F401
                                     update_state, write_debug, write_state)
    from lastcall_core.tail import iter_lines_reverse  # noqa: F401
    from lastcall_core.zones import (DEFAULT_HEADLINES,  # noqa: F401
                                     EXTENDED_WINDOW, KNOWN_WINDOWS,
                                     STANDARD_WINDOW, band_for,
                                     describe_threshold, resolve_window,
                                     resolve_zones, window_from_evidence,
                                     zone_for)
except ImportError:  # a broken install must never break the session
    if __name__ != "__main__" or os.environ.get("LASTCALL_TRACE"):
        raise
    sys.exit(0)

_DEFAULT_HEADLINES = DEFAULT_HEADLINES

# Imported on first use: the hook path never needs them, and shutil alone
# costs a few milliseconds on every tool call.
_LAZY = {
    "handover_status": "lastcall_core.doctor",
    "VERIFIERS": "lastcall_core.wizard",
    "detect_verifiers": "lastcall_core.wizard",
    "parse_answer": "lastcall_core.wizard",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    import importlib
    return getattr(importlib.import_module(module), name)


def doctor(argv):
    from lastcall_core.doctor import doctor as _doctor
    return _doctor(argv, version=__version__)


def setup(argv):
    from lastcall_core.wizard import setup as _setup
    return _setup(argv)


def main(argv):
    # setup and doctor print em dashes and arrows. A Windows console running
    # cp437 cannot encode those, and print() would raise UnicodeEncodeError.
    # Degrade the character instead of the command. The hook path is
    # unaffected: json.dumps escapes non-ASCII.
    if argv and argv[0] in ("doctor", "--doctor", "setup", "--setup"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    if argv and argv[0] in ("doctor", "--doctor"):
        return doctor(argv[1:])
    if argv and argv[0] in ("setup", "--setup"):
        return setup(argv[1:])
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return 0
    return run_hook(argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # noqa: BLE001 - a guard must never take the session down
        if os.environ.get(ENV_PREFIX + "TRACE"):
            raise
        sys.exit(0)
