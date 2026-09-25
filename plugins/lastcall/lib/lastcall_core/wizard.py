"""`lastcall setup`: interactive first-run configuration of one project.

It asks the same questions as the onboarding prompt and /lastcall:onboard, in
the same order and with the same recommendations: render.ONBOARDING_QUESTIONS
and render.ONBOARDING_RECOMMENDED are the single source for all three.
"""

import json
import os
import re
import shutil
import sys

from .config import PROJECT_CONFIG_NAME, find_project_config, home_dir, load_config
from .doctor import handover_status
from .render import (HANDOFF_SKELETON, ONBOARDING_QUESTIONS, ONBOARDING_RECOMMENDED,
                     RELAY_TEMPLATE, WRAPUP_STEPS, cli_command)
from .windows import validate_windows

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
    ("claude", "claude -p \"Review the changes on this branch against the plan "
               "and spec documents in docs/. Report anything specified that was "
               "NOT implemented, and any claim in the handoff the diff does not "
               "support.\"",
     "Claude Code CLI"),
)

# Where setup writes a project's own wrap-up, relative to the project.
WRAPUP_FILE = os.path.join(".lastcall", "wrapup.md")


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
    if answer in ("yes", "no") and answer[0] in valid_keys:
        return answer[0]
    if accept_number:
        digits = answer.replace(",", "").replace("_", "").replace(" ", "")
        if digits.isdigit() and int(digits) > 0:
            return int(digits)
    return None


def parse_tokens(text):
    """"400k 550k", "400000, 550000", "1m" -> [ints], or None if any part is
    not a positive count. Parts are split on whitespace, ";" and ", " so
    "400,000, 550,000" is two numbers, not one."""
    parts = [p for p in re.split(r"[\s;]+|,(?=\s)", (text or "").strip()) if p]
    numbers = []
    for part in parts:
        match = re.match(r"^(\d[\d_,]*(?:\.\d+)?)([km]?)$", part.strip(",").lower())
        if not match:
            return None
        value = float(match.group(1).replace(",", "").replace("_", ""))
        value *= {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
        if value <= 0:
            return None
        numbers.append(int(value))
    return numbers


def parse_pair(text, ceiling=None):
    """Two ascending numbers ("40 55", "40%, 55%", "400k 550k"), or None."""
    numbers = parse_tokens((text or "").replace("%", " "))
    if (not numbers or len(numbers) != 2 or numbers[0] >= numbers[1]
            or (ceiling is not None and numbers[1] > ceiling)):
        return None
    return numbers


def parse_windows(text):
    """"claude-opus-5-5=1000000, claude-sonnet-*=200k" -> ({map}, [problems])."""
    mapping, problems = {}, []
    for pair in [p.strip() for p in (text or "").split(",") if p.strip()]:
        key, sep, value = pair.partition("=")
        numbers = parse_tokens(value) if sep else None
        if not key.strip() or not numbers or len(numbers) != 1:
            problems.append("%r is not model=window" % pair)
            continue
        mapping[key.strip()] = numbers[0]
    clean, invalid = validate_windows(mapping)
    return clean or {}, problems + list(invalid)


def detect_verifiers():
    """Second-opinion CLIs available on this machine, as (name, command, label)."""
    return [(name, command, label) for name, command, label in VERIFIERS
            if shutil.which(name)]


def running_agent_name(env=None):
    """The agent this setup runs under, if any — a second opinion should come
    from a different one."""
    from .relay import running_agent
    return running_agent(os.environ if env is None else env)


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
    rec = ONBOARDING_RECOMMENDED

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
    # The relay (relay.py) needs the successor's CLI: Claude Code or Codex.
    # git is optional — it only proves the handoff is committed.
    tools = {"claude or codex": bool(shutil.which("claude") or shutil.which("codex")),
             "git": bool(shutil.which("git"))}
    relay_possible = tools["claude or codex"]

    numbered = {q.key: (n, q) for n, q in enumerate(ONBOARDING_QUESTIONS, 1)}

    def title(key):
        """The numbered question, worded as in render.ONBOARDING_QUESTIONS."""
        number, question = numbered[key]
        return "%d/%d  %s" % (number, len(ONBOARDING_QUESTIONS), question.ask)

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

    def ask_text(question, hint, default="", check=None):
        """Free text. ``check`` returns an error message for an answer it
        cannot use, and the question is asked again."""
        if not interactive:
            return default
        if question:
            print("\n" + question)
        print("  %s" % hint)
        while True:
            try:
                answer = input("  > ").strip() or default
            except (EOFError, KeyboardInterrupt):
                print()
                return default
            problem = check(answer) if (check and answer) else None
            if not problem:
                return answer
            print("  %s" % problem)

    def yes_no(question, yes, no, default, why):
        return ask(question, [("y", "yes — " + yes), ("n", "no  — " + no)],
                   default, why=why) == "y"

    notes = []

    print("Last Call setup — %s" % root)
    print("  git repository : %s" % ("yes" if has_git_repo else "no"))
    print("  on PATH : %s" % ", ".join("%s %s" % (n, "ok" if ok else "MISSING")
                                       for n, ok in tools.items()))

    # 1. When to warn ------------------------------------------------------
    yellow, red = rec["percent"]
    warn = ask(
        title("warn"),
        [("p", "percentages of the window — %d%% and %d%%" % (yellow, red)),
         ("t", "absolute token counts — e.g. %s and %s, no window needed"
               % tuple("{:,}".format(n) for n in rec["tokens"]))],
        {"percent": "p", "tokens": "t"}[rec["warn"]],
        why="Percentages work on every model. Token counts suit anyone who "
            "thinks \"wrap up at 400k\".")
    zones = existing.get("zones")
    if warn == "p":
        if isinstance(zones, list) and any(
                isinstance(z, dict) and z.get("at_tokens") is not None for z in zones):
            existing.pop("zones")
            notes.append("removed the token-count zones: you chose percentages")
        percents = ask_text(
            None, "Warn and stop at which percentages? Enter keeps %s and %s."
            % (existing.get("yellow_percent", yellow), existing.get("red_percent", red)),
            check=lambda a: None if parse_pair(a, 100)
            else "type two percentages, lower first, e.g. 40 55")
        if percents:
            existing["yellow_percent"], existing["red_percent"] = parse_pair(percents, 100)
        standard = next(iter(rec["windows"].values()))
        window = ask(
            "     Which window do this project's Claude models have?",
            [("1", "{:,} tokens for every Claude model — standard".format(standard)),
             ("2", "1,000,000 tokens for every Claude model — extended"),
             ("3", "different per model — type model=window pairs next")],
            "1",
            why="Or type any number of tokens, e.g. 500000. Codex reports its "
                "own window, so this only matters for Claude Code.",
            accept_number=True)
        if window == "3":
            text = ask_text(
                None, "model=window pairs, comma separated, e.g.\n"
                "  claude-opus-5-5=1000000, claude-sonnet-*=200000",
                check=lambda a: "; ".join(parse_windows(a)[1]) or (
                    None if parse_windows(a)[0] else "no model=window pairs found"))
            windows = parse_windows(text)[0] or dict(rec["windows"])
        elif isinstance(window, int):
            windows = {"claude-*": window}
        else:
            windows = {"claude-*": {"1": standard, "2": 1_000_000}[window]}
        existing["windows"] = windows
        if existing.pop("context_window_tokens", None) is not None:
            notes.append("replaced context_window_tokens with the windows map "
                         "(a single figure would override even the status line)")
    else:
        default_tokens = " ".join(str(n) for n in rec["tokens"])
        tokens = ask_text(
            None, "Warn and stop at how many tokens? Enter takes %s."
            % " and ".join("{:,}".format(n) for n in rec["tokens"]),
            default=default_tokens,
            check=lambda a: None if parse_pair(a)
            else "type two token counts, lower first, e.g. 400k 550k")
        low, high = parse_pair(tokens)
        existing["zones"] = [{"name": "yellow", "at_tokens": low},
                             {"name": "red", "at_tokens": high, "block": True}]
        floor = ask_text(
            None, "Stay silent on models with a smaller window than? e.g. 1000000\n"
            "  if these thresholds are for a 1M model. Enter for never.",
            check=lambda a: None if (parse_tokens(a) and len(parse_tokens(a)) == 1)
            else "type one token count, e.g. 1000000")
        if floor:
            existing["min_window_tokens"] = parse_tokens(floor)[0]

    # 2. What wrap-up means here -------------------------------------------
    rules = ask_text(
        title("wrapup"),
        "Which docs get updated, what must be committed, whether pushing is\n"
        "  allowed. Written to %s above the shipped steps; edit it any time.\n"
        "  Enter keeps the shipped wrap-up." % WRAPUP_FILE)

    # 3. Gates ---------------------------------------------------------------
    gates = ask_text(
        title("gates"),
        "Shell commands, comma separated — 'pytest -q', not 'run the tests'.\n"
        "  The wrap-up shows them to the agent; nothing runs them for you.\n"
        "  Examples: 'npm test, npm run lint'. Enter to fill in later.")
    if gates:
        existing["gates"] = [g.strip() for g in gates.split(",") if g.strip()]

    # 4. A second opinion ----------------------------------------------------
    available = detect_verifiers()
    if available:
        options = [(str(i + 1), "use %s" % label)
                   for i, (_n, _c, label) in enumerate(available)]
        options.append(("n", "no second opinion"))
        me = running_agent_name()
        other = [i for i, (name, _c, _l) in enumerate(available) if name != me]
        choice = ask(
            title("verifier"), options, str((other or [0])[0] + 1),
            why="A different model reading the diff against your plan "
                "documents catches what the session that wrote them cannot. "
                "It runs as part of the wrap-up, not automatically.")
        if choice != "n":
            index = int(choice) - 1
            if 0 <= index < len(available):
                existing["verifier"] = available[index][1]
    elif interactive:
        print("\n" + title("verifier"))
        print("  none of %s is on PATH — skipped"
              % ", ".join(name for name, _c, _l in VERIFIERS))

    # 5. Automatic handover --------------------------------------------------
    relay_why = ("the claude/codex CLI is present%s."
                 % ("" if tools["git"] and has_git_repo else
                    "; without git the relay cannot prove the handoff is committed"))
    if not relay_possible:
        relay_why = "Not available here: neither claude nor codex is on PATH."
    handover = yes_no(
        title("handover"),
        "write a handoff, then start a successor session",
        "just warn me; the session ends there",
        "y" if relay_possible else "n", relay_why)
    # A recommendation is for a human to accept. With no tty there is nobody to
    # accept it, and enabling a relay that will later launch sessions is not
    # something a piped or scripted run gets to decide on your behalf.
    if not interactive:
        handover = False

    handoff_dir = os.path.join(root, rec["handoff_dir"])
    if handover:
        relay_cfg = dict(existing.get("relay") or {})
        agent = ask(
            "     Which agent should the successor be?",
            [("same", "whichever agent is handing over"),
             ("claude", "always Claude Code"),
             ("codex", "always Codex")],
            "same",
            why="Pick one to hand over ACROSS agents; the handoff is plain "
                "Markdown either way.")
        if agent == "same":
            relay_cfg.pop("agent", None)
        else:
            relay_cfg["agent"] = agent
        verify = ask_text(
            None, "What command proves this project's environment is actually up?\n"
            "  It becomes Step 0 of %s/TEMPLATE.md: the successor runs it FIRST.\n"
            "  e.g. 'npm test && curl -sf localhost:3000/health'. Enter to fill in later."
            % rec["handoff_dir"])
        write_skeleton(handoff_dir, verify, notes)

        # 6. Models
        first = True
        if agent != "codex":
            models = ask_text(
                title("models"),
                "Claude successor: a model, then fallbacks, e.g. 'opus, fable, sonnet'.\n"
                "  Enter for Claude Code's default.")
            first = False
            names = [m.strip() for m in models.split(",") if m.strip()]
            if names:
                relay_cfg["model"] = names[0]
                if names[1:]:
                    relay_cfg["fallback_model"] = ",".join(names[1:])
        if agent != "claude":
            codex_model = ask_text(
                title("models") if first else None,
                "Codex successor: a model, e.g. 'gpt-5-codex'. Enter for Codex's default.")
            if codex_model:
                relay_cfg["codex_model"] = codex_model

        # 7. Unattended — only ever on an explicit yes.
        unattended = yes_no(
            title("unattended"),
            "the successor runs tools WITHOUT asking",
            "it asks for permission like a normal session",
            "y" if rec["skip_permissions"] else "n",
            "Only a typed yes enables this. It is what lets a chain of sessions "
            "continue while you are away.")

        # 8. Remote control
        remote = yes_no(
            title("remote_control"),
            "reach a Claude successor from anywhere",
            "a Claude successor is reachable only on this machine",
            "y" if rec["remote_control"] else "n",
            "Codex successors show up in the Codex app either way.")

        # 9. Retire the predecessor — recommended exactly when unattended.
        retire = yes_no(
            title("retire"),
            "retire this session once the successor has checked in",
            "leave it open",
            "y" if unattended else "n",
            "Never a desktop-app session. Without it every handover leaves "
            "another session running.")

        relay_cfg["handoff_dir"] = rec["handoff_dir"]
        relay_cfg["skip_permissions"] = unattended
        relay_cfg["remote_control"] = remote
        # relay.py reads retire_predecessor first; keep whichever name is there.
        retire_key = ("retire_predecessor" if "retire_predecessor" in relay_cfg
                      else "kill_predecessor")
        relay_cfg[retire_key] = retire
        prefix = os.path.basename(root.rstrip("/"))
        if prefix:
            relay_cfg["name_prefix"] = re.sub(r"[^A-Za-z0-9_-]", "-", prefix)
        existing["relay"] = relay_cfg

    if rules:
        existing["template"] = write_wrapup(root, rules, handover, notes)
    elif handover:
        existing["template"] = RELAY_TEMPLATE

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
    if handover:
        print("\nNext: fill in Step 0 of %s/TEMPLATE.md with real commands and"
              % handoff_dir)
        print("their EXPECTED results. A handoff whose Step 0 cannot fail is a")
        print("handoff that proves nothing.")
    if not ready and handover:
        print("\nFix the MISS lines above, then re-run: %s" % cli_command("doctor"))
    return 0


def write_skeleton(handoff_dir, verify, notes):
    block = ("```\n%s\n```" % verify) if verify else (
        "```\nTODO: the command(s) that prove this environment works.\n```")
    try:
        os.makedirs(handoff_dir, exist_ok=True)
        skeleton = os.path.join(handoff_dir, "TEMPLATE.md")
        if os.path.exists(skeleton):
            notes.append("kept the existing %s" % skeleton)
            return
        with open(skeleton, "w", encoding="utf-8") as handle:
            handle.write(HANDOFF_SKELETON.format(date="YYYY-MM-DD", verify_block=block))
        notes.append("wrote %s — the shape each handoff should take" % skeleton)
    except OSError as error:
        notes.append("could NOT create %s (%s)" % (handoff_dir, error))


def write_wrapup(root, rules, handover, notes):
    """The project's own wrap-up: its rules first, then the shipped steps —
    the relay template's when handing over, so it still runs the relay.
    Returns the "template" value (relative to the project)."""
    path = os.path.join(root, WRAPUP_FILE)
    if os.path.exists(path):
        notes.append("kept the existing %s — add your rules there" % path)
        return WRAPUP_FILE
    if handover:
        with open(RELAY_TEMPLATE, "r", encoding="utf-8") as handle:
            steps = handle.read().rstrip()
    else:
        steps = WRAPUP_STEPS + "\n\n  Gates that must pass before you stop:\n\n{gates}"
    text = ("This project's wrap-up rules come first:\n\n  %s\n\n%s\n"
            % (rules.replace("{", "{{").replace("}", "}}"), steps))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        notes.append("wrote %s — your wrap-up; edit it any time" % path)
    except OSError as error:
        notes.append("could NOT write %s (%s)" % (path, error))
    return WRAPUP_FILE
