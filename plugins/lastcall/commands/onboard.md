---
description: Interview the user and write this project's Last Call configuration
---

You are onboarding this project onto Last Call, a hook that warns when the
context window is filling and hands work over to a fresh session.

Do NOT write any configuration until you have asked the questions below and the
user has answered them. Ask them conversationally, a few at a time, not as a
wall of text. Recommend an answer for each and say why in one line. If the user
says "you decide", take your recommendation and tell them what you chose.

## First, gather facts — do not ask the user things you can check

Run these and use the answers to shape your questions:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lastcall.py doctor
git rev-parse --show-toplevel 2>/dev/null || echo "not a git repo"
command -v tmux git codex gemini claude
ls docs/handoff .claude/work 2>/dev/null
```

Read the project's CLAUDE.md, AGENTS.md, README and any docs/ index if they
exist. A project that already documents its test command should not be asked
what its test command is — propose the one you found and ask only for
confirmation.

## What you need to establish

1. **Context window.** 200,000 or 1,000,000 tokens. Last Call refuses to guess,
   because guessing low makes it fire at 15% full and wreck good sessions.

2. **When to warn.** Defaults are 40% and 55%. Ask whether the user wants
   earlier or later. If they want more than two steps, use `zones` — but keep
   the names `yellow` and `red` for the first two, because only those carry
   built-in wording.

3. **What "wrap up" means here.** This is the important one and the one people
   skip. Find out what this project actually requires before work can be handed
   over: which docs get updated, what must be committed, whether pushing is
   allowed. Write it into a template file rather than leaving the generic text.

4. **Gates.** The commands that must PASS before handing over — tests, linters,
   type checks. Propose what you found in the repo. These are shown to the
   assistant; the hook never runs them itself.

5. **A second opinion.** If `codex` or `gemini` is on PATH, ask whether to use
   it as a verification gate: a different model reading the diff against the
   plan documents, reporting what was specified but not implemented, and what
   was claimed but not evidenced. Explain that this is what catches the errors
   the session that wrote the code cannot see. If they say yes, set `verifier`.

6. **Automatic handover.** Whether a fresh session should be spawned when the
   context runs out. This needs tmux, git and the claude CLI. If the user says
   yes, ask whether the successor should run UNATTENDED — remote control on and
   permission prompts skipped — and be explicit that unattended means it runs
   tools without asking. Never enable that without a clear yes.

## Then write the configuration

Write `.claude/lastcall.json` in THIS project only. Never write to a parent
directory and never to `~/.claude`; each project carries its own configuration
and they must not leak into one another.

Include only what the user actually chose. Then write the wrap-up template you
drafted in step 3, and point `template` at it.

If automatic handover was chosen, also create the handoff directory and write
`docs/handoff/TEMPLATE.md` with these sections, filled in for THIS project:

```
0. Step 0 — bring the environment up, and PROVE it
1. Where things stand
2. Your first work
3. What the last session did
4. How to work here
5. Decided — do not re-ask
6. Where everything is
```

Section 0 must state the EXPECTED result of each command, not just the command.
"`GET /health` returns 200" is a proof; "check the server is running" is not. A
Step 0 that cannot fail lets the successor start work on a broken environment
believing it verified one.

Section 4 must say how work should be parallelised here — when to use a subagent
for one-shot work whose context can be discarded, when a teammate whose context
must persist, and when a workflow for stages running in parallel. A fresh
session without that guidance does everything sequentially and decays.

## Finally, prove it

Run `doctor` again and show the user the real output. If it reports any PROBLEM
line, or `automatic handover: NOT SET UP` when they asked for handover, fix it
before telling them you are done. Do not describe what the configuration will do
— show them what it resolved to.
