#!/usr/bin/env python3
"""Standalone installer for Last Call — the fallback when you are not using
Claude Code plugins.

The plugin install is better: it registers hooks through ${CLAUDE_PLUGIN_ROOT},
so nothing on disk holds an absolute path and moving the checkout cannot break
it. This installer exists for two cases the plugin route does not cover well:

  - you want the hook in one specific project and nowhere else
  - you are on Windows, where "python3" is frequently not on PATH and the
    interpreter has to be detected rather than assumed

It writes absolute paths into .claude/settings.json. Move the checkout and you
must re-run it.

    python3 install.py                 install into the current project
    python3 install.py --global        install for every project (~/.claude)
    python3 install.py --dir PATH      install into a specific project
    python3 install.py --uninstall     remove what this installed
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "plugins", "lastcall", "scripts", "lastcall.py")

EVENTS = (("Stop", 15), ("SessionStart", 10), ("PostCompact", 10))
MARKER = "lastcall.py"


def find_interpreter():
    """Pick an interpreter that actually exists on this machine.

    `python3` is a safe assumption on macOS and Linux and a bad one on Windows,
    where the python.org installer ships `python.exe` and the `py` launcher but
    no `python3`. Verifying by execution beats trusting a name.
    """
    candidates = []
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


def quote(text):
    return '"%s"' % text if " " in text else text


def build_command(interpreter, event):
    parts = [quote(p) for p in interpreter] + [quote(SCRIPT), event]
    return " ".join(parts)


def load_settings(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as error:
        sys.stderr.write("refusing to overwrite unreadable settings: %s\n" % error)
        sys.exit(1)


def save_settings(path, settings):
    """Back up before writing. This file may hold hooks and permissions the user
    spent real time on, and this installer is not the thing that gets to lose
    them."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        shutil.copy2(path, path + ".lastcall.bak")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


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
                if not (isinstance(entry, dict) and MARKER in str(entry.get("command", "")))
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


def main(argv):
    uninstall = "--uninstall" in argv
    target_dir = os.path.expanduser("~") if "--global" in argv else os.getcwd()
    if "--dir" in argv:
        index = argv.index("--dir")
        if index + 1 >= len(argv):
            sys.stderr.write("--dir needs a path\n")
            return 1
        target_dir = os.path.abspath(os.path.expanduser(argv[index + 1]))

    settings_path = os.path.join(target_dir, ".claude", "settings.json")
    settings = strip_existing(load_settings(settings_path))

    if uninstall:
        save_settings(settings_path, settings)
        print("removed Last Call hooks from %s" % settings_path)
        return 0

    if not os.path.isfile(SCRIPT):
        sys.stderr.write("cannot find the hook script at %s\n" % SCRIPT)
        return 1

    interpreter = find_interpreter()
    if not interpreter:
        sys.stderr.write(
            "no working Python 3 interpreter found on PATH.\n"
            "Install Python 3.9+ and re-run, or register the hook by hand.\n"
        )
        return 1

    hooks = settings.setdefault("hooks", {})
    for event, timeout in EVENTS:
        hooks.setdefault(event, []).append({
            "hooks": [{
                "type": "command",
                "command": build_command(interpreter, event),
                "timeout": timeout,
            }]
        })

    save_settings(settings_path, settings)
    print("installed Last Call into %s" % settings_path)
    print("  interpreter : %s" % " ".join(interpreter))
    print("  script      : %s" % SCRIPT)
    print("\nRestart Claude Code for the hooks to load.")

    # Say plainly what is NOT configured. A guard that quietly does half its
    # job is the failure mode this whole project exists to prevent, so the
    # installer refuses to imply more than it has actually set up.
    print("\n" + "-" * 68)
    print("Last Call will now WARN when context runs low.")
    print("It will NOT hand work over to a fresh session — that is off until")
    print("you configure it. Without it, a session simply ends at the warning.")
    print("\nSet it up (two questions, takes a moment):")
    print("  %s %s setup" % (" ".join(interpreter), quote(SCRIPT)))
    print("\nOr check what is and is not wired up:")
    print("  %s %s doctor" % (" ".join(interpreter), quote(SCRIPT)))
    print("-" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
