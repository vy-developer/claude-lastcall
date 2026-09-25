"""`lastcall.py setup`: interactive first-run configuration of one project."""

import json
import os
import re
import shutil
import sys

from .config import PROJECT_CONFIG_NAME, find_project_config, home_dir, load_config
from .doctor import handover_status
from .render import HANDOFF_SKELETON, RELAY_TEMPLATE

VERIFIERS = (
    ("codex", "codex exec \"Review the changes on this branch against the plan "
              "and spec documents in docs/. Report anything specified that was "
              "NOT implemented, anything implemented that was NOT specified, "
              "and any claim in the handoff the diff does not support.\"",
     "OpenAI Codex CLI"),
    ("gemini", "gemini -p \"Review the changes on this branch against the plan "
               "documents in docs/ and report what was skipped or claimed "
               "without evidence.\"",
     "Google Gemini CLI"),
)


def parse_answer(answer, valid_keys, accept_number=False):
    """One typed answer -> a key, a number, or None for "not understood".

    Returning None matters: an answer that is neither a listed option nor a
    number used to fall through to the default silently, so someone who typed
    "500,000" got 200,000 and was never told.
    """
    answer = (answer or "").strip().lower()
    if not answer:
        return "__default__"
    if answer in valid_keys:
        return answer
    if accept_number:
        digits = answer.replace(",", "").replace("_", "").replace(" ", "")
        if digits.isdigit() and int(digits) > 0:
            return int(digits)
    return None


def detect_verifiers():
    """Second-opinion CLIs available on this machine, as (name, command, label)."""
    return [(name, command, label) for name, command, label in VERIFIERS
            if shutil.which(name)]


def config_target(root):
    """The file setup writes: the project's existing config if it has one
    (a legacy .claude/lastcall.json keeps working), else .lastcall.json."""
    directory, found = find_project_config(root)
    if found and os.path.realpath(directory) == os.path.realpath(root):
        return found[0]
    return os.path.join(root, PROJECT_CONFIG_NAME)


def setup(argv):
    """Asks only what cannot be worked out from the machine, recommends an
    answer for each, and writes the handoff skeleton — because the document is
    the half of a handover that no script can produce for you."""
    payload = {"cwd": os.getcwd()}
    config = load_config(payload)
    root = config["_project_dir"]
    target = config_target(root)
    interactive = sys.stdin.isatty()

    # project_dir() never walks up TO home, but run from home itself, or with
    # CLAUDE_PROJECT_DIR pointing there, it still lands on it. A config there
    # would apply to everything below it; refuse rather than write it.
    if os.path.realpath(root) == home_dir():
        print("Last Call setup refuses to configure your home directory:")
        print("  %s would apply to every directory below it, not to one" % target)
        print("  project. cd into the project you want to configure and run")
        print("  setup again. Machine-wide settings belong in ~/.lastcall/config.json.")
        return 1

    existing = {}
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as handle:
                existing = json.load(handle) or {}
        except (OSError, ValueError):
            existing = {}

    has_git_repo = os.path.isdir(os.path.join(root, ".git"))
    tools = {name: bool(shutil.which(name)) for name in ("tmux", "git", "claude")}
    relay_possible = has_git_repo and all(tools.values())

    def ask(question, options, default, why=None, accept_number=False):
        """options: list of (key, label). default is the recommendation."""
        if not interactive:
            return default
        print("\n" + question)
        for key, label in options:
            mark = "  <- recommended" if key == default else ""
            print("  %s) %s%s" % (key, label, mark))
        if why:
            print("     %s" % why)
        while True:
            try:
                answer = input("  [%s] " % default).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return default
            parsed = parse_answer(answer, dict(options), accept_number)
            if parsed == "__default__":
                return default
            if parsed is not None:
                return parsed
            print("  '%s' is not one of the options%s."
                  % (answer, " and is not a number" if accept_number else ""))

    def ask_text(question, hint, default=""):
        if not interactive:
            return default
        print("\n" + question)
        print("  %s" % hint)
        try:
            return input("  > ").strip() or default
        except (EOFError, KeyboardInterrupt):
            print()
            return default

    print("Last Call setup — %s" % root)
    print("  git repository : %s" % ("yes" if has_git_repo else "no"))
    print("  tmux / git / claude on PATH : %s"
          % ", ".join("%s %s" % (n, "ok" if ok else "MISSING")
                      for n, ok in tools.items()))

    window = ask(
        "1/6  How big is this project's context window (Claude Code)?",
        [("1", "200,000 tokens — standard"),
         ("2", "1,000,000 tokens — extended"),
         ("3", "work it out automatically (needs the bundled status line)")],
        "1",
        why="Or type any number of tokens, e.g. 500000. Codex reports its own "
            "window, so this only matters for Claude Code sessions.",
        accept_number=True)
    if isinstance(window, int):
        existing["context_window_tokens"] = window
    else:
        existing["context_window_tokens"] = {
            "1": 200_000, "2": 1_000_000, "3": None}[window]

    relay_default = "y" if relay_possible else "n"
    relay_why = ("tmux, git and the claude CLI are all present."
                 if relay_possible else
                 "Not available here: " + ", ".join(
                     [n for n, ok in tools.items() if not ok]
                     + ([] if has_git_repo else ["this is not a git repository"])))
    relay = ask(
        "2/6  Hand over to a fresh session automatically when context runs low?",
        [("y", "yes — write a handoff, then spawn a successor in tmux"),
         ("n", "no  — just warn me; the session ends there")],
        relay_default,
        why=relay_why)
    # A recommendation is for a human to accept. With no tty there is nobody to
    # accept it, and enabling a relay that will later launch sessions is not
    # something a piped or scripted run gets to decide on your behalf.
    if not interactive:
        relay = "n"

    notes = []
    handoff_dir = os.path.join(root, "docs", "handoff")

    if relay == "y":
        existing["template"] = RELAY_TEMPLATE
        verify = ask_text(
            "3/6  What command proves this project's environment is actually up?",
            "The successor runs this FIRST and must not start work until it "
            "passes.\n  Examples: 'npm test', 'make dev && curl -sf "
            "localhost:3000/health'.\n  Leave blank to fill in later.")
        block = ("```\n%s\n```" % verify) if verify else (
            "```\nTODO: the command(s) that prove this environment works.\n```")
        try:
            os.makedirs(handoff_dir, exist_ok=True)
            skeleton = os.path.join(handoff_dir, "TEMPLATE.md")
            if os.path.exists(skeleton):
                notes.append("kept the existing %s" % skeleton)
            else:
                with open(skeleton, "w", encoding="utf-8") as handle:
                    handle.write(HANDOFF_SKELETON.format(
                        date="YYYY-MM-DD", verify_block=block))
                notes.append("wrote %s — the shape each handoff should take"
                             % skeleton)
        except OSError as error:
            notes.append("could NOT create %s (%s)" % (handoff_dir, error))
        installers = {
            "tmux": "sudo apt install tmux   (or: brew install tmux)",
            "git": "sudo apt install git     (or: brew install git)",
            "claude": "see https://claude.com/claude-code for the CLI",
        }
        for name, ok in tools.items():
            if not ok:
                notes.append("MISSING: %s — install it with: %s"
                             % (name, installers[name]))
        if not has_git_repo:
            notes.append("MISSING: %s is not a git repository — the relay "
                         "refuses to spawn without one" % root)

        gates = ask_text(
            "4/6  What must PASS before this project hands over?",
            "Tests, linters, a review gate — comma separated. The wrap-up shows\n"
            "  these to the assistant so it cannot hand over unverified work.\n"
            "  Examples: 'npm test, npm run lint'. Leave blank to fill in later.")
        if gates:
            existing["gates"] = [g.strip() for g in gates.split(",") if g.strip()]

        available = detect_verifiers()
        if available:
            print("\nFound on this machine: %s"
                  % ", ".join(label for _n, _c, label in available))
            options = [(str(i + 1), "use %s" % label)
                       for i, (_n, _c, label) in enumerate(available)]
            options.append(("n", "no second opinion"))
            choice = ask(
                "5/6  Have a SECOND model check the work before handing over?",
                options, "1",
                why="A different model reading the diff against your plan "
                    "documents catches what the session that wrote them cannot. "
                    "It runs as part of the wrap-up, not automatically.")
            if choice != "n":
                index = int(choice) - 1
                if 0 <= index < len(available):
                    existing["verifier"] = available[index][1]

        unattended = ask(
            "6/6  Should the successor run UNATTENDED?",
            [("y", "yes — remote control on, permission prompts skipped"),
             ("n", "no  — successor waits for permission like a normal session")],
            "y",
            why="Unattended means the successor runs tools without asking. It is "
                "what lets a chain of sessions continue while you are away.")
        relay_cfg = dict(existing.get("relay") or {})
        relay_cfg["handoff_dir"] = "docs/handoff"
        relay_cfg["remote_control"] = True
        relay_cfg["skip_permissions"] = (unattended == "y")
        prefix = os.path.basename(root.rstrip("/"))
        if prefix:
            relay_cfg["name_prefix"] = re.sub(r"[^A-Za-z0-9_-]", "-", prefix)
        existing["relay"] = relay_cfg

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isfile(target):
            shutil.copy2(target, target + ".bak")
            notes.append("backed up the previous config to %s.bak" % target)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        print("\ncould not write %s: %s" % (target, error))
        return 1

    print("\nwrote %s" % target)
    for note in notes:
        print("  %s" % note)

    fresh = load_config({"cwd": root})
    ready, checks = handover_status(fresh)
    print("\nautomatic handover: %s" % ("READY" if ready else "NOT SET UP"))
    for label, ok in checks.items():
        print("  %s %s" % ("ok  " if ok else "MISS", label))
    if relay == "y":
        print("\nNext: fill in Step 0 of %s/TEMPLATE.md with real commands and"
              % handoff_dir)
        print("their EXPECTED results. A handoff whose Step 0 cannot fail is a")
        print("handoff that proves nothing.")
    if not ready and relay == "y":
        print("\nFix the MISS lines above, then re-run: lastcall.py doctor")
    return 0
