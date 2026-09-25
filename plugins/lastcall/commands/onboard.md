---
description: Interview the user and write this project's Last Call configuration
---

You are onboarding this project onto Last Call, a hook that warns the agent
running here (Claude Code or Codex) when its context window is filling, and
can hand the work over to a fresh session.

Do NOT write any configuration until you have asked the questions below and the
user has answered them. Ask them conversationally, a few at a time, not as a
wall of text.

`lastcall doctor` below is `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lastcall.py doctor`
when the `lastcall` command is not on PATH; `lastcall version` prints where
the plugin runs from.

## If this project is ALREADY configured, do not start over

Read the project's config first: `.lastcall.json` (or `.lastcall/config.json`,
or a legacy `.claude/lastcall.json` / `.codex/lastcall.json`), and
`~/.lastcall/config.json` for machine-wide settings. If one exists, this is an
update, not a first run. Show the user what is currently set, in a short table,
and then ask only about what is missing or what they want changed. Do not
re-ask settled questions.

Pay attention to what the current config does NOT use, because those are
usually the things worth offering:

- a single `context_window_tokens`? Offer a `windows` map per Claude model
  instead, or absolute `at_tokens` thresholds, which need no window at all.
- `at_tokens` but no `min_window_tokens`? Offer it if they ever run a
  smaller-window model.
- a `relay` block without `agent`, `model` / `fallback_model` or
  `kill_predecessor`? Offer them.
- no `verifier`, but `codex`, `gemini` or `claude` is on PATH? Offer it.

Then also check for workarounds that newer versions made unnecessary, and offer
to remove them: a duplicated `lastcall.json` inside a subdirectory (the relay
now finds the config by walking up from the working directory), and a git
repository created only to satisfy the old requirement that the target be a
worktree (git is now optional and its absence is merely reported).

## The interview

<!-- BEGIN GENERATED from ONBOARDING_* in lib/lastcall_core/render.py. Edit it there, then run:
     python3 plugins/lastcall/lib/lastcall_core/render.py --write-onboard -->

Look first, so you ask about what is actually here: read AGENTS.md, CLAUDE.md,
README and any docs index; look for a test command in package.json, Makefile,
pyproject.toml or CI config; check `git rev-parse --show-toplevel`, what is on
PATH (claude, codex, gemini, git), and what `lastcall doctor` reports. Never ask what
you can check: propose what you found and ask only for confirmation.

Ask a few questions at a time, in this order. Recommend an answer for each and
say why in one line; if the user says "you decide", take the recommendation
and say what you chose. Ask questions 6-9 only if they want
automatic handover.

1. WHEN TO WARN
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
     "min_window_tokens" so the guard stays silent there.

2. WHAT WRAP-UP MEANS HERE
   The important one. Which documents get updated, what must be committed,
   whether pushing is allowed, what the next session must be told. Write it
   into a template file (e.g. .lastcall/wrapup.md) and point "template" at it;
   do not leave the generic text. With automatic handover, build it on the
   shipped relay template (templates/handoff-relay.md in the plugin) so it
   still ends by running the relay: the "{relay}" placeholder.

3. GATES
   "gates": the commands that must PASS before handing over. SHELL COMMANDS,
   not descriptions: "pytest -q", not "run the tests". Turn an intention into
   the real command, show it, and confirm. Nothing runs them automatically;
   the wrap-up shows them to the agent.

4. A SECOND OPINION
   If another agent CLI is on PATH (codex, gemini, claude), offer it as
   "verifier": a different model reading the diff against the plan documents,
   reporting what was specified but not implemented and what was claimed
   without evidence. Recommend one that is not the agent running now.

5. AUTOMATIC HANDOVER
   Whether a successor session starts when context runs out. Recommend yes if
   the claude or codex CLI is on PATH; git is optional and only proves the
   handoff is committed. If yes, settle under "relay": "repo" (only if not
   this directory), "handoff_dir" (default docs/handoff), and "agent": "claude"
   or "codex". Leave "agent" unset to hand over to whichever agent is running;
   set it to hand over ACROSS agents. Ask what command proves the environment
   is up: it becomes Step 0 of the handoff TEMPLATE.md.

6. MODELS
   "model" and "fallback_model" for a Claude successor, e.g. "opus" with
   "fable,sonnet" (Claude Code switches by itself when one is overloaded);
   "codex_model" for a Codex successor. Unset means the CLI's default.

7. UNATTENDED
   "skip_permissions". Recommend no. Be explicit that the successor then runs
   tools without asking, and never enable it without a clear yes.

8. REMOTE CONTROL
   "remote_control": on by default, so you can reach a Claude successor from
   anywhere. Recommend yes.

9. RETIRE THE PREDECESSOR
   "kill_predecessor" (same as "retire_predecessor"): retire the old session
   once the successor has checked in; a desktop-app session is never killed.
   Off by default. Recommend it whenever the handover is unattended, because
   otherwise every handover leaves another session running forever.

Then write .lastcall.json at the root of THIS project only, or update its
existing config file in place. Never a parent directory, never your home
directory: each project carries its own configuration. Include only what the
user chose. Write the wrap-up template and point "template" at it. If handover
was chosen, create the handoff directory and its TEMPLATE.md, whose Step 0
states the EXPECTED result of each command, so it can actually fail.

If the user does not want Last Call here, write {"disabled": true} to
.lastcall.json and stop.

Finally run `lastcall doctor` and show the user the real output. If it prints any
PROBLEM line, or "automatic handover: NOT SET UP" when they asked for
handover, fix it before saying you are done.

<!-- END GENERATED -->

## The handoff template, in detail

If automatic handover was chosen, `docs/handoff/TEMPLATE.md` (under
`handoff_dir`) gets these sections, filled in for THIS project:

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

Do not describe what the configuration will do — show the user what `doctor`
says it resolved to.
