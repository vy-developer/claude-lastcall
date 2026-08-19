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

1. **When to warn.** Offer both ways and let them choose:
   - **absolute token counts** — `"at_tokens": 400000` on each zone. No context
     window needed at all, and the simplest answer for anyone who thinks in
     tokens rather than percentages. Prefer this unless they say otherwise.
   - **percentages** — `"at": 40`. Needs the window, so it raises a question
     the token route does not.

   Keep the names `yellow` and `red` for the first two zones; only those carry
   built-in wording, and any other name silently renders without a headline.

2. **The context window (`context_window_tokens`) — only if they chose
   percentages.** Accept ANY figure:
   200000, 500000, 1000000, whatever they say. Do not argue with it; if the
   session later holds more tokens than that, Last Call corrects it by itself.
   If they chose token thresholds, do not ask this at all.

3. **`min_window_tokens`.** If the thresholds are absolute and they sometimes
   run a smaller-window model, set this so the guard stays silent there — a
   400k threshold means nothing on a 200k model.

3. **What "wrap up" means here.** This is the important one and the one people
   skip. Find out what this project actually requires before work can be handed
   over: which docs get updated, what must be committed, whether pushing is
   allowed. Write it into a template file rather than leaving the generic text.

4. **Gates (`gates`).** The commands that must PASS before handing over —
   tests, linters, type checks. These are SHELL COMMANDS, not descriptions:
   `pytest -q`, never "run the tests". If the user describes an intention,
   turn it into the real command, show it to them, and confirm. Propose what
   you found in the repo. They are shown to the assistant during wrap-up; the
   hook never runs them itself.

5. **A second opinion.** If `codex` or `gemini` is on PATH, ask whether to use
   it as a verification gate: a different model reading the diff against the
   plan documents, reporting what was specified but not implemented, and what
   was claimed but not evidenced. Explain that this is what catches the errors
   the session that wrote the code cannot see. If they say yes, set `verifier`.

6. **Automatic handover.** Whether a fresh session should be spawned when the
   context runs out. Needs tmux and the claude CLI; git is optional and only
   used to verify the handoff is committed. If yes, settle the whole `relay`
   block: `repo` if it is not this directory, `handoff_dir`, and `model` /
   `fallback_model` — e.g. `"model": "opus"` with `"fallback_model":
   "fable,sonnet"`, since Claude Code switches by itself when a model is
   overloaded. Ask whether the successor runs UNATTENDED (`skip_permissions`),
   be explicit that it then runs tools without asking, and never enable it
   without a clear yes. `remote_control` is on by default so you can reach the
   successor later.

If the user does not want Last Call in this project, write
`{"disabled": true}` to `.claude/lastcall.json` and stop. That silences it
without uninstalling anything.

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
