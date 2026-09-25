#!/usr/bin/env python3
"""Standalone installer for Last Call: a thin wrapper around
`lastcall install --method hooks`.

The machine-wide install is `plugins/lastcall/bin/lastcall install`, which
registers the plugin with Claude Code and Codex through their own plugin
commands. This script keeps the older, hooks-only entry point working for the
cases the plugin route does not cover well:

  - you want the hook in one specific project and nowhere else
  - you are on Windows, where "python3" is frequently not on PATH and the
    interpreter has to be detected rather than assumed

It writes absolute paths into .claude/settings.json. Move the checkout and you
must re-run it.

    python3 install.py                 install into the current project
    python3 install.py --global        install for every project (user level)
    python3 install.py --dir PATH      install into a specific project
    python3 install.py --uninstall     remove what this installed

Any other flag is passed through to `lastcall install` (for example --codex,
--dry-run, --python PATH).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "plugins", "lastcall", "lib"))

from lastcall_core import cli  # noqa: E402

# Kept importable for anything that used them from here.
SCRIPT = cli.HOOK_SCRIPT
EVENTS = cli.EVENTS
MARKER = cli.MARKER
find_interpreter = cli.find_interpreter
strip_existing = cli.strip_existing


def translate(argv):
    """install.py flags -> `lastcall` argv, or None on a usage error."""
    argv = list(argv)
    uninstall = "--uninstall" in argv
    user_level = "--global" in argv
    target = os.getcwd()
    if "--dir" in argv:
        index = argv.index("--dir")
        if index + 1 >= len(argv):
            sys.stderr.write("--dir needs a path\n")
            return None
        target = os.path.abspath(os.path.expanduser(argv[index + 1]))
        del argv[index:index + 2]
        user_level = False
    rest = [a for a in argv if a not in ("--uninstall", "--global")]
    out = ["uninstall" if uninstall else "install", "--method", "hooks"]
    if not any(a in rest for a in ("--claude", "--codex", "--all")):
        out.append("--claude")
    if not user_level:
        out += ["--project", target]
    return out + rest


def main(argv):
    translated = translate(argv)
    if translated is None:
        return 1
    return cli.main(translated)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
