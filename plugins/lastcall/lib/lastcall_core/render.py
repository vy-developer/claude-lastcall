"""What the assistant is told: the warning, the onboarding prompt, the
post-compaction note, and the templates behind them."""

import os
import re
import shlex

# lib/lastcall_core/render.py -> plugins/lastcall
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The hook entry point. Templates get it as {setup} so a message can say
# "run this" without the user hand-editing a path that changes with every
# plugin update.
SCRIPT_PATH = os.path.join(PLUGIN_ROOT, "scripts", "lastcall.py")
# The optional relay ships alongside; templates get it as {relay}: the whole
# command, `python3 <plugin>/bin/lastcall relay`, which runs relay.py. An older
# template that says "bash {relay}" still works — render drops the "bash" —
# and relay/handoff.sh is a deprecated shim that execs relay.py.
RELAY_SCRIPT = os.path.join(PLUGIN_ROOT, "lib", "lastcall_core", "relay.py")
RELAY_LAUNCHER = os.path.join(PLUGIN_ROOT, "bin", "lastcall")
RELAY_COMMAND = "python3 %s relay" % shlex.quote(RELAY_LAUNCHER)


def lastcall_linked(which=None):
    """True when `lastcall` on PATH is this checkout's launcher (the symlink,
    or the .cmd shim on Windows, that `lastcall install` makes)."""
    if which is None:
        import shutil  # lazily: the hook path rarely gets here
        which = shutil.which
    found = which("lastcall")
    if not found:
        return False
    try:
        if os.path.realpath(found) == os.path.realpath(RELAY_LAUNCHER):
            return True
        if os.name == "nt" and os.path.isfile(found):
            with open(found, encoding="utf-8", errors="replace") as handle:
                return RELAY_LAUNCHER + ".cmd" in handle.read()
    except (OSError, ValueError):
        pass
    return False


def cli_command(subcommand, which=None):
    """How to tell a person to run ``subcommand``: `lastcall setup` when that
    command reaches this checkout, else a path that works without the link
    (the hook script for setup and doctor, the launcher for the rest)."""
    if lastcall_linked(which):
        return "lastcall %s" % subcommand
    word = subcommand.split(" ", 1)[0]
    target = SCRIPT_PATH if word in ("setup", "doctor") else RELAY_LAUNCHER
    return "python3 %s %s" % (shlex.quote(target), subcommand)
LEGACY_RELAY_SCRIPT = os.path.join(PLUGIN_ROOT, "relay", "handoff.sh")
_SHELL_BEFORE_RELAY = re.compile(r"\b(?:bash|sh)[ \t]+(?=\{relay\})")
RELAY_TEMPLATE = os.path.join(PLUGIN_ROOT, "templates", "handoff-relay.md")

HANDOFF_SKELETON = """\
# Session handoff — {date}

Written for someone with ZERO context. Not a summary of what happened: the
next session's instructions. If a line here would not change what they do,
cut it.

## 0. Step 0 — bring the environment up, and PROVE it

{verify_block}

State the EXPECTED result of each command, not just the command. "It should
return 200" is a proof; "check the server is running" is not. If a check can
pass while the system is broken, say so explicitly.

## 1. Where things stand

What is finished and committed. What is half-done and where. One paragraph.

## 2. Your first work

The single next thing to do, and WHERE. Be specific enough that they can start
without reading anything else.

## 3. What the last session did

Only what changes what happens next. Include what was attempted and abandoned,
and why — otherwise it gets attempted again.

## 4. How to work here

The rules of this repository: how to test, what gates a change, what must never
be done. Point at the files rather than restating them.

Include how work should be PARALLELISED here, because a fresh session will
otherwise do everything sequentially in one context and decay:

  - a subagent for a one-shot task — run it, return the result, context
    discarded. Use it to keep exploration and research out of the main window.
  - a teammate when the context must persist and you will come back to it.
  - a workflow when many things run in parallel across distinct stages.

Name the concrete gates a change must pass, and who runs them.

## 5. Decided — do not re-ask

Decisions already taken, so the next session does not reopen them. This section
is what stops a fresh context relitigating settled questions.

## 6. Where everything is

The handful of paths worth knowing on day one.
"""

# The generic wrap-up, in two parts: the steps (which `setup` also puts under
# a project's own rules when handover is off), and the notice that nothing
# carries the work forward, which only the unconfigured default shows.
WRAPUP_STEPS = """\
Wrap up this session rather than starting anything new.

  1. FINISH what is already in flight. Use your judgment about what is small
     enough to land — a two-line fix is fine, a new phase is not.
  2. RECORD the state of the work somewhere durable, so it survives this
     session ending.
  3. WRITE the next session's starting instructions. Not a summary of what
     happened — instructions, written for someone with zero context: how to
     bring the environment up and prove it, what was done, what was left out
     and why, what to do next and where, and which decisions are already
     settled so they do not get re-litigated.
  4. VERIFY that record against the actual state of the repository, not
     against your memory of the session. Your memory is the thing that is
     running out."""

HANDOVER_NOT_SET_UP = """\
AUTOMATIC HANDOVER IS NOT SET UP for this project, so nothing will start a
successor session or carry this work forward — when you stop, the work stops.
Tell the user that, once, and point them at:

    {setup_command}

Configure this text: set "template" in .lastcall.json."""

DEFAULT_TEMPLATE = WRAPUP_STEPS + "\n\n" + HANDOVER_NOT_SET_UP


# --------------------------------------------------------------------------
# Onboarding: ONE list of questions behind three front ends
#
#   the SessionStart prompt   ONBOARDING, once per unconfigured project
#   /lastcall:onboard         commands/onboard.md (Codex turns it into a skill)
#   `lastcall setup`          wizard.py, the terminal wizard
#
# Both conversational texts embed onboarding_block() verbatim: the prompt
# builds itself from it, and onboard.md carries it between the GENERATED
# markers (tests/test_docs.py fails when they drift; regenerate with
# `python3 plugins/lastcall/lib/lastcall_core/render.py --write-onboard`).
# The wizard asks ONBOARDING_QUESTIONS in this order, numbers them from this
# tuple, and takes its recommendations from ONBOARDING_RECOMMENDED.
# --------------------------------------------------------------------------

ONBOARDING_RECOMMENDED = {
    "warn": "percent",              # the built-in ladder works on every model
    "percent": (40, 55),            # yellow_percent, red_percent
    "tokens": (400_000, 550_000),   # the example for at_tokens
    "windows": {"claude-*": 200_000},
    "handoff_dir": "docs/handoff",
    "skip_permissions": False,      # only ever on an explicit yes
    "remote_control": True,
    # kill_predecessor: recommended exactly when skip_permissions is on
}


class OnboardingQuestion(object):
    """key: stable id; title: the heading both conversational texts show;
    ask: the wizard's prompt; body: what the assistant is told to settle;
    handover_only: asked only once automatic handover was chosen."""

    __slots__ = ("key", "title", "ask", "body", "handover_only")

    def __init__(self, key, title, ask, body, handover_only=False):
        self.key, self.title, self.ask = key, title, ask
        self.body, self.handover_only = body, handover_only


ONBOARDING_QUESTIONS = (
    OnboardingQuestion(
        "warn", "WHEN TO WARN",
        "When should Last Call warn that the context is filling?",
        """\
Offer both. Percentages of the window ("at" on each zone; the built-in
yellow_percent 40 and red_percent 55) are recommended: they work on every
model. Absolute token counts ("at_tokens" on each zone, e.g. 400000 and
550000) need no window at all. Keep the zone names "yellow" and "red", and
"block": true on red.
- Percentages under Claude Code: ask which Claude models the project runs and
  store their windows in "windows", e.g. {"claude-opus-5-5": 1000000,
  "claude-sonnet-*": 200000}. Accept ANY number, never argue with it: a
  session that proves a bigger window corrects it by itself. Unsure?
  {"claude-*": 200000}. Prefer this map to one "context_window_tokens",
  which overrides even the status line's exact figure. Codex reports its own
  window: never ask for it.
- Token counts: if they sometimes run a smaller-window model, set
  "min_window_tokens" so the guard stays silent there."""),
    OnboardingQuestion(
        "wrapup", "WHAT WRAP-UP MEANS HERE",
        "What does wrap-up mean in this project?",
        """\
The important one. Which documents get updated, what must be committed,
whether pushing is allowed, what the next session must be told. Write it
into a template file (e.g. .lastcall/wrapup.md) and point "template" at it;
do not leave the generic text. With automatic handover, build it on the
shipped relay template ({relay_template}) so it
still ends by running the relay: the "{relay}" placeholder."""),
    OnboardingQuestion(
        "gates", "GATES",
        "What must PASS before this project hands over?",
        """\
"gates": the commands that must PASS before handing over. SHELL COMMANDS,
not descriptions: "pytest -q", not "run the tests". Turn an intention into
the real command, show it, and confirm. Nothing runs them automatically;
the wrap-up shows them to the agent."""),
    OnboardingQuestion(
        "verifier", "A SECOND OPINION",
        "Have a SECOND model check the work before handing over?",
        """\
If another agent CLI is on PATH (codex, gemini, claude), offer it as
"verifier": a different model reading the diff against the plan documents,
reporting what was specified but not implemented and what was claimed
without evidence. Recommend one that is not the agent running now."""),
    OnboardingQuestion(
        "handover", "AUTOMATIC HANDOVER",
        "Hand over to a fresh session automatically when context runs low?",
        """\
Whether a successor session starts when context runs out. Recommend yes if
the claude or codex CLI is on PATH; git is optional and only proves the
handoff is committed. If yes, settle under "relay": "repo" (only if not
this directory), "handoff_dir" (default docs/handoff), and "agent": "claude"
or "codex". Leave "agent" unset to hand over to whichever agent is running;
set it to hand over ACROSS agents. Ask what command proves the environment
is up: it becomes Step 0 of the handoff TEMPLATE.md."""),
    OnboardingQuestion(
        "models", "MODELS",
        "Which model should drive the successor?",
        """\
"model" and "fallback_model" for a Claude successor, e.g. "opus" with
"fable,sonnet" (Claude Code switches by itself when one is overloaded);
"codex_model" for a Codex successor. Unset means the CLI's default.""",
        handover_only=True),
    OnboardingQuestion(
        "unattended", "UNATTENDED",
        "Should the successor run UNATTENDED (skip permission prompts)?",
        """\
"skip_permissions". Recommend no. Be explicit that the successor then runs
tools without asking, and never enable it without a clear yes.""",
        handover_only=True),
    OnboardingQuestion(
        "remote_control", "REMOTE CONTROL",
        "Start a Claude successor with Remote Control?",
        """\
"remote_control": on by default, so you can reach a Claude successor from
anywhere. Recommend yes.""",
        handover_only=True),
    OnboardingQuestion(
        "retire", "RETIRE THE PREDECESSOR",
        "Retire the OLD session once the successor has checked in?",
        """\
"kill_predecessor" (same as "retire_predecessor"): retire the old session
once the successor has checked in; a desktop-app session is never killed.
Off by default. Recommend it whenever the handover is unattended, because
otherwise every handover leaves another session running forever.""",
        handover_only=True),
)

ONBOARDING_LOOK_FIRST = """\
Look first, so you ask about what is actually here: read AGENTS.md, CLAUDE.md,
README and any docs index; look for a test command in package.json, Makefile,
pyproject.toml or CI config; check `git rev-parse --show-toplevel`, what is on
PATH (claude, codex, gemini, git), and what `{doctor}` reports. Never ask what
you can check: propose what you found and ask only for confirmation.

Ask a few questions at a time, in this order. Recommend an answer for each and
say why in one line; if the user says "you decide", take the recommendation
and say what you chose. Ask questions {handover_questions} only if they want
automatic handover."""

ONBOARDING_WRITE = """\
Then write .lastcall.json at the root of THIS project only, or update its
existing config file in place. Never a parent directory, never your home
directory: each project carries its own configuration. Include only what the
user chose. Write the wrap-up template and point "template" at it. If handover
was chosen, create the handoff directory and its TEMPLATE.md, whose Step 0
states the EXPECTED result of each command, so it can actually fail.

If the user does not want Last Call here, write {"disabled": true} to
.lastcall.json and stop.

Finally run `{doctor}` and show the user the real output. If it prints any
PROBLEM line, or "automatic handover: NOT SET UP" when they asked for
handover, fix it before saying you are done."""


def onboarding_questions_text():
    """The numbered questions, as both conversational texts show them."""
    out = []
    for number, question in enumerate(ONBOARDING_QUESTIONS, 1):
        body = "\n".join(("   " + line) if line else ""
                         for line in question.body.split("\n"))
        out.append("%d. %s\n%s" % (number, question.title, body))
    return "\n\n".join(out)


def onboarding_block(doctor, relay_template):
    """Everything the SessionStart prompt and /lastcall:onboard share: how to
    look, the questions, and how to write and prove the result. ``doctor``
    runs doctor, and ``relay_template`` names the shipped relay template, as
    seen from where the text will be read."""
    numbers = [str(n) for n, q in enumerate(ONBOARDING_QUESTIONS, 1)
               if q.handover_only]
    text = "\n\n".join((ONBOARDING_LOOK_FIRST, onboarding_questions_text(),
                        ONBOARDING_WRITE))
    return (text.replace("{doctor}", doctor)
            .replace("{relay_template}", relay_template)
            .replace("{handover_questions}", "%s-%s" % (numbers[0], numbers[-1])))


# The prompt knows the plugin's real path; a command file shipped in the
# plugin does not, so it names the CLI and paths inside the plugin instead.
PROMPT_DOCTOR = "python3 {setup} doctor"
COMMAND_DOCTOR = "lastcall doctor"
COMMAND_RELAY_TEMPLATE = "templates/handoff-relay.md in the plugin"

ONBOARDING = """\
LAST CALL IS INSTALLED HERE BUT NOT CONFIGURED for this project. It already
runs on its defaults (a warning at 40% and 55% of the context window), so
nothing is broken. This note appears once per project: mention it to the user
briefly, and configure it only if they want to — WITH the user,
in this conversation. Do not interrupt the task they asked for, do not send
them to a terminal wizard, and do not write anything until they have answered.

""" + onboarding_block(PROMPT_DOCTOR, RELAY_TEMPLATE)

ONBOARD_COMMAND = os.path.join(PLUGIN_ROOT, "commands", "onboard.md")
GENERATED_BEGIN = ("<!-- BEGIN GENERATED from ONBOARDING_* in "
                   "lib/lastcall_core/render.py. Edit it there, then run:\n"
                   "     python3 plugins/lastcall/lib/lastcall_core/render.py "
                   "--write-onboard -->")
GENERATED_END = "<!-- END GENERATED -->"


def onboard_command_block():
    """What onboard.md must carry between its GENERATED markers."""
    return onboarding_block(COMMAND_DOCTOR, COMMAND_RELAY_TEMPLATE)


def write_onboard_command(path=ONBOARD_COMMAND):
    """Rewrite the generated part of onboard.md in place. True if it changed."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    start = text.index(GENERATED_BEGIN) + len(GENERATED_BEGIN)
    end = text.index(GENERATED_END)
    updated = text[:start] + "\n\n" + onboard_command_block() + "\n\n" + text[end:]
    if updated == text:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    return True


COMPACTION_NOTE = """\
LAST CALL — CONTEXT COMPACTED. The earlier part of this session is now a
summary, and details you remember from it may be missing or wrong. Before you
continue:
  - re-read the plan, handoff and status documents you were working from, and
    any file you are about to edit, instead of trusting your memory of them;
  - check the repository's actual state (git status, recent commits) against
    what you believe you already did."""

COMPACTION_AFTER_WARNING = """\
Before the compaction Last Call had issued the {zone} warning. The wrap-up it
asked for is still expected unless the user has said otherwise."""

ASSUMED_WINDOW_NOTE = """\
(This session does not report its window size, so Last Call assumed the
standard {window:,} tokens. If you know your context window is larger — a
1M-token model, say — this came early: carry on with the task, and tell the
user once that setting "context_window_tokens" in ~/.lastcall/config.json, or
installing the Last Call status line, makes the measurement exact.)"""


def read_template(config, path):
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(config.get("_project_dir") or os.getcwd(), path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    # Strip blank lines off each end, NOT whitespace. A plain .strip() eats the
    # leading indentation of the first line.
    return text.strip("\r\n").rstrip() or None


def zone_body(config, zone):
    """What this zone actually tells the assistant to do.

    Most specific first: this zone's own file, this zone's inline text, the
    project-wide template, then the generic built-in.
    """
    return (read_template(config, zone.get("template"))
            or zone.get("message")
            or read_template(config, config.get("template"))
            or DEFAULT_TEMPLATE)


_FORMATTER = None


def _formatter():
    """Format numbers the way a reader expects when no spec is given.

    A template that says "{percent}" wants "88", not "87.8746", and "{tokens}"
    wants "439,373", not "439373". Explicit specs still win.
    """
    global _FORMATTER
    if _FORMATTER is None:
        import string

        class _Sensible(string.Formatter):
            def format_field(self, value, format_spec):
                if not format_spec:
                    if isinstance(value, bool):
                        pass
                    elif isinstance(value, float):
                        return format(value, ".0f")
                    elif isinstance(value, int):
                        return format(value, ",")
                return super(_Sensible, self).format_field(value, format_spec)

        _FORMATTER = _Sensible()
    return _FORMATTER


def fill(text, values):
    """Interpolate a template, leaving it intact if the template is malformed.

    A stray brace in someone's wrap-up is their problem to fix; it is never a
    reason to withhold the warning entirely.
    """
    try:
        return _formatter().vformat(text, (), values)
    except (KeyError, IndexError, ValueError, AttributeError):
        return text


def format_verifier(config):
    verifier = config.get("verifier")
    if not verifier:
        return ("(none configured — a second model reading the diff against the "
                "plan catches what you cannot)")
    return str(verifier)


def format_gates(config):
    gates = config.get("gates")
    if not gates:
        return "(none configured — set \"gates\" in .lastcall.json)"
    if isinstance(gates, str):
        gates = [gates]
    return "\n".join("       %s" % str(gate) for gate in gates)


def render(config, zone, tokens, window, transcript=None, assumed=False,
           agent=None):
    percent = (tokens * 100.0) / window if window else 0.0
    values = {
        "percent": percent,
        "tokens": tokens,
        "window": window or 0,
        "remaining": max(0, window - tokens) if window else 0,
        "band": zone["name"],
        "zone": zone["name"],
        "at": zone["at"],
        "relay": RELAY_COMMAND,
        "setup": SCRIPT_PATH,
        "gates": format_gates(config),
        "verifier": format_verifier(config),
        "agent": agent or "claude",
        # The assistant's own raw transcript, so "audit the handoff against
        # what actually happened" is an instruction it can carry out.
        "transcript": transcript or "(this session's transcript)",
    }
    if window:
        header = (
            "LAST CALL — %s. {percent:.0f}%% of the context window is in use "
            "({tokens:,} of {window:,} tokens; {remaining:,} left)."
            % zone["name"].upper()
        )
    else:
        # An absolute zone fired without a known window. Report what is true.
        header = ("LAST CALL — %s. {tokens:,} tokens in use."
                  % zone["name"].upper())
    if zone.get("headline"):
        header += "\n" + zone["headline"]
    if assumed and window:
        header += "\n" + ASSUMED_WINDOW_NOTE

    text = _SHELL_BEFORE_RELAY.sub("", zone_body(config, zone))
    if "{setup_command}" in text:  # a PATH lookup, only when it is shown
        values["setup_command"] = cli_command("setup")
    body = fill(text, values)
    return fill(header, values) + "\n\n" + body


def block_reason(zone):
    return ("Context has reached the %s zone — write the handoff before "
            "stopping." % zone["name"])


def onboarding_message():
    # replace, not format: the text is full of JSON braces.
    return (ONBOARDING.replace(PROMPT_DOCTOR, cli_command("doctor"))
            .replace("{setup}", SCRIPT_PATH))


def compaction_message(config, previous_zone=None):
    """The post-compaction note, or None when it is switched off."""
    note = config.get("compaction_note")
    if note is False:
        return None
    text = note if isinstance(note, str) and note.strip() else COMPACTION_NOTE
    if previous_zone and previous_zone != "green":
        text += "\n\n" + COMPACTION_AFTER_WARNING.format(zone=previous_zone.upper())
    return text


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--write-onboard"]:
        sys.exit("usage: render.py --write-onboard")
    print("%s %s" % ("rewrote" if write_onboard_command() else "unchanged",
                     ONBOARD_COMMAND))
