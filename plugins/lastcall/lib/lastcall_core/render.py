"""What the assistant is told: the warning, the onboarding prompt, the
post-compaction note, and the templates behind them."""

import os

# lib/lastcall_core/render.py -> plugins/lastcall
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The hook entry point. Templates get it as {setup} so a message can say
# "run this" without the user hand-editing a path that changes with every
# plugin update.
SCRIPT_PATH = os.path.join(PLUGIN_ROOT, "scripts", "lastcall.py")
# The optional relay ships alongside; templates get it as {relay}.
RELAY_SCRIPT = os.path.join(PLUGIN_ROOT, "relay", "handoff.sh")
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

DEFAULT_TEMPLATE = """\
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
     running out.

AUTOMATIC HANDOVER IS NOT SET UP for this project, so nothing will start a
successor session or carry this work forward — when you stop, the work stops.
Tell the user that, once, and point them at:

    python3 {setup} setup

Configure this text: set "template" in .lastcall.json."""


ONBOARDING = """\
LAST CALL IS INSTALLED HERE BUT NOT CONFIGURED for this project. It already
runs on its defaults (a warning at 40% and 55% of the context window), so
nothing is broken. This note appears once per project: mention it to the user
briefly, and configure it only if they want to — WITH the user,
in this conversation. Do not interrupt the task they asked for, do not send
them to a terminal wizard, and do not write anything until they have answered.

Look first, so you ask about what is actually here rather than what might be:
read AGENTS.md / CLAUDE.md / README, look for a test command in package.json,
Makefile, pyproject.toml or CI config, check `git rev-parse --show-toplevel`,
and check what is on PATH (tmux, claude, codex, gemini). Then propose answers
and ask the user to confirm or correct them. Ask a few at a time.

What you need to settle:

1. WHEN TO WARN. Offer BOTH ways and let them pick:
   - absolute token counts, e.g. wrap up at 400k and stop at 550k. Use
     "at_tokens" on each zone. This needs NO context window setting at all,
     and is the simplest answer for anyone who thinks in tokens.
   - percentages of the window, e.g. 40% and 55%, using "at" (the default).

2. THE CONTEXT WINDOW ("context_window_tokens"), but ONLY if they chose
   percentages AND this is Claude Code (Codex reports its window itself).
   Accept ANY number — 200000, 500000, 1000000, whatever they say. Never
   argue with it: if the session later holds more tokens than that, Last Call
   corrects it by itself. If they chose token thresholds, do not ask this.

3. WHICH MODELS THIS PROJECT RUNS ON. If the thresholds are absolute and they
   sometimes use a smaller-window model, set "min_window_tokens" so the guard
   stays silent there — 400k means nothing on a 200k model.

4. WHAT WRAP-UP MEANS HERE. The important one. Which documents get updated,
   what must be committed, whether pushing is allowed, what the next session
   must be told. Write it into a template file; do not leave the generic text.

5. GATES ("gates"): the commands that must PASS before handing over. These are SHELL
   COMMANDS, not descriptions — "pytest -q", not "run the tests". If the user
   describes an intention, turn it into the actual command, show them, and
   confirm.

6. A SECOND OPINION, if another agent CLI (codex, claude, gemini) is on PATH:
   a different model reading the diff against the plan documents and reporting
   what was specified but not implemented, and what was claimed without
   evidence. Store it as "verifier".

7. AUTOMATIC HANDOVER: whether a fresh session should be spawned when context
   runs out. If yes, settle all of these under "relay":
   - "repo": which directory to hand over, if not this one
   - "handoff_dir": where handoffs live, default docs/handoff
   - "model" and "fallback_model": which model drives the successor, e.g.
     "opus" with "fable,sonnet" as fallback.
   - "skip_permissions": UNATTENDED. Be explicit that this means the successor
     runs tools without asking, and never enable it without a clear yes.
   - "remote_control": on by default, so you can reach the successor later.
   - "kill_predecessor": retire the OLD session once the successor has proved
     itself. Off by default. Recommend it whenever the handover is unattended,
     because otherwise every handover leaves another session running forever.

Then write .lastcall.json at the root of THIS project only — never a parent
directory, never your home directory. Write the wrap-up template and point
"template" at it. If handover was chosen, create the handoff directory and a
TEMPLATE.md whose Step 0 states the EXPECTED result of each command, so it can
actually fail.

Finally run:  python3 {setup} doctor
and show the user the real output. If it prints any PROBLEM line, fix it.

If the user does not want Last Call here, write {{"disabled": true}} to
.lastcall.json so it stays quiet in this project."""


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
        "relay": RELAY_SCRIPT,
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

    body = fill(zone_body(config, zone), values)
    return fill(header, values) + "\n\n" + body


def block_reason(zone):
    return ("Context has reached the %s zone — write the handoff before "
            "stopping." % zone["name"])


def onboarding_message():
    return ONBOARDING.format(setup=SCRIPT_PATH)


def compaction_message(config, previous_zone=None):
    """The post-compaction note, or None when it is switched off."""
    note = config.get("compaction_note")
    if note is False:
        return None
    text = note if isinstance(note, str) and note.strip() else COMPACTION_NOTE
    if previous_zone and previous_zone != "green":
        text += "\n\n" + COMPACTION_AFTER_WARNING.format(zone=previous_zone.upper())
    return text
