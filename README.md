# Last Call

**Last call for your Claude Code session.** Tells the assistant to finish up
while it still has the room to do it properly — before it starts working from a
compacted memory of files it only thinks it read.

Long sessions fail quietly. Once the context window fills, older material is
summarized away, and the assistant carries on editing against stale state with
complete confidence. Nothing in the transcript announces this.

Most context tools *show you* a number: a bar, a meter, a percentage in the
status line. This one talks to the assistant instead, once, at the threshold —
and if you let it, holds the session open until the handoff is actually
written.

```
LAST CALL — YELLOW. 43% of the context window is in use
(430,000 of 1,000,000 tokens; 570,000 left).
Finish what is in flight; start nothing new. This is an alarm, not a
decision — you judge what still fits.
```

## Contents

- [Install](#install) · [Setting up handover](#setting-up-handover) · [Commands](#commands)
- [Tell it how big your window is](#tell-it-how-big-your-window-is)
- [Configuration](#configuration) · [zones](#your-own-zones) · [gates and the self-audit](#the-wrap-up-sequence)
- [What the assistant actually receives](#what-the-assistant-actually-receives)
- [The relay](#the-relay-optional-unix--tmux-only) · [Why 40% and 55%](#why-40-and-55)

## Install

**As a plugin** (recommended — no absolute paths anywhere):

```
/plugin marketplace add vy-developer/claude-lastcall
/plugin install lastcall@claude-lastcall
```

**Standalone**, if you would rather not use plugins, or you are on Windows
where `python3` is often not on `PATH`:

```
git clone https://github.com/vy-developer/claude-lastcall
cd claude-lastcall
python3 install.py            # this project only
python3 install.py --global   # every project
python3 install.py --uninstall
```

The installer detects a working interpreter (`py -3` on Windows), writes the
hooks into `.claude/settings.json`, and backs up whatever was there first.

Requires Python 3.9+. No third-party packages, ever. CI runs the suite on
Linux, macOS and Windows against 3.9, 3.11 and 3.13.

## Commands

```
lastcall.py setup      configure this project — five questions, writes
                       .claude/lastcall.json and docs/handoff/TEMPLATE.md
lastcall.py doctor     show what resolved: window, zones, handover readiness
lastcall.py doctor <transcript.jsonl>
                       measure a real session and report its zone
lastcall.py --version

relay/handoff.sh --dry-run    resolve everything, spawn nothing
relay/handoff.sh              hand over to a fresh session
relay/handoff.sh --help       every flag
```

`doctor` is the answer to "is this thing even working?". It never guesses: if
it cannot resolve the window or handover is half-configured, it says so and
tells you which piece is missing.

## Tell it how big your window is

This is the one thing it will not guess, and it is worth explaining why.

A model identifier does not reveal the window size. Measured on a real 5.1 MB
transcript: a session running the 1M-context Opus records its model as plain
`claude-opus-5` — byte-identical to the 200K variant — while holding 743,106
tokens. That is 371% of the window the identifier implies. Any tool that infers
the window from the model name is wrong on that session and cannot tell.

Guessing low is the dangerous direction: a 1M session mislabelled as 200K gets
told to wrap up at 15% full, which actively wrecks good sessions. So Last Call
stays **silent** when it does not know, and says so loudly when asked:

```
python3 .../lastcall.py doctor ~/.claude/projects/<project>/<session>.jsonl
```

Three ways to fix that, best first:

**1. The status line — exact, automatic.** Claude Code hands the status line the
real window size. Point yours at the bundled script and the guard never needs
telling again:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/plugins/lastcall/scripts/statusline.py"
  }
}
```

It prints a normal status line too: `Opus 5 | myrepo | ctx [####------] 43% YELLOW`.
If your Claude Code version names those fields differently, `statusline.py --dump`
prints the raw payload so you can check.

**2. Just tell it.** `.claude/lastcall.json`:

```json
{ "context_window_tokens": 200000 }
```

**3. Let it prove the window itself.** A session cannot hold more tokens than
its window, so once usage passes 200,000 the window is provably the 1M one and
the guard starts working on its own. This is a proof, not an inference — but it
only helps 1M users, and only after they are already deep into a session.

## Configuration

`.claude/lastcall.json` in your project. Every field is optional, and every one
can be overridden per-run with `LASTCALL_<FIELD>` in the environment —
`LASTCALL_RED_PERCENT=70` or `LASTCALL_red_percent=70`, both work. Copy
[`plugins/lastcall/lastcall.example.json`](plugins/lastcall/lastcall.example.json)
to start from a commented version.

| field | default | meaning |
|---|---|---|
| `yellow_percent` | `40` | warn once at this much of the window used |
| `red_percent` | `55` | escalate once here |
| `zones` | `null` | your own zones instead of the two above — see below |
| `gates` | `null` | commands that must pass before handing over; shown to the assistant as `{gates}` |
| `relay` | `null` | relay settings: `repo`, `handoff_dir`, `name_prefix`, `dirty_baseline`, `remote_control`, `skip_permissions` |
| `context_window_tokens` | `null` | window size; `null` means "work it out or stay quiet" |
| `mode` | `"block_once"` | `block_once` blocks the stop a single time at red so the handoff actually gets written; `advisory` never blocks |
| `template` | `null` | path to your own wrap-up instructions |
| `include_output_tokens` | `false` | count the last response's output too — budget for the next turn's input rather than the current window |
| `debug` | `false` | write a redacted copy of the last hook payload |
| `state_dir` | `~/.claude/lastcall` | where per-session state lives |
| `state_ttl_days` | `14` | prune state files older than this |
| `disabled` | `false` | turn the whole thing off without uninstalling |

### Your own wrap-up instructions

The built-in message is deliberately generic. What "wrap up" means is specific
to your project, so write it down and point at it:

```json
{ "template": ".claude/wrapup.md" }
```

Placeholders: `{percent}` `{tokens}` `{window}` `{remaining}` `{zone}`, and
`{relay}` for the bundled relay script, `{gates}` for your gate commands, and
`{transcript}` for this session's raw transcript path. See
[`example-wrapup.md`](plugins/lastcall/templates/example-wrapup.md), or
[`handoff-relay.md`](plugins/lastcall/templates/handoff-relay.md) if you want
the session to hand over to a fresh one automatically.

### Your own zones

Yellow at 40% and red at 55% is just the default arrangement, not a limit. Set
`zones` and you get as many as you like, with your names, your thresholds, your
instructions, and your choice of which ones hold the session open:

```json
{
  "zones": [
    { "name": "nudge",    "at": 50, "message": "Past halfway. Prefer finishing threads over opening them." },
    { "name": "winddown", "at": 70, "template": ".claude/winddown.md" },
    { "name": "closing",  "at": 88, "template": ".claude/closing.md", "block": true }
  ]
}
```

| key | meaning |
|---|---|
| `name` | what the zone is called, in the message and in state. Defaults to its threshold |
| `at` | percentage of the window that triggers it |
| `template` | file of instructions for this zone only |
| `message` | inline instructions for this zone only, if you don't want a file |
| `headline` | one line printed straight after the numbers, before the instructions |
| `block` | hold the stop once when this zone is first entered |

Instructions resolve most-specific-first: this zone's `template`, then its
`message`, then the project-wide `template`, then the built-in text. A shared
template plus one zone that overrides it works without repeating yourself.

`mode: "advisory"` overrides every `block` at once, which is the quickest way
to try a configuration out without it interrupting you.

Malformed zones are dropped rather than being fatal, and an empty `zones` list
falls back to the defaults — silently disabling the tool because of a typo
would be the worst possible failure for something whose job is to speak up.
Check what it resolved with `doctor`:

```
zones : nudge@50%[own text]  winddown@70%[own text]  closing@88%[block][own text]
```

## What the assistant actually receives

Nothing, while you are below every zone. That is the point, and it is why this
costs no context until it matters.

On the turn a zone is first entered, the hook returns JSON on stdout and Claude
Code injects its `additionalContext` into the conversation as hook feedback.
The assistant reads it as an instruction. It arrives **once per zone entry**,
not every turn.

The message is two parts. Last Call generates the first — the numbers, plus the
zone's `headline` — and you own the second entirely:

```
LAST CALL — WINDDOWN. 74% of the context window is in use
(740,000 of 1,000,000 tokens; 260,000 left).
Finish what is in flight; start nothing new.

<everything from here down is your template>
```

If the zone has `block`, the hook also returns `decision: "block"`, which stops
the assistant ending its turn and hands it that reason. It blocks **once**:
Claude Code sets `stop_hook_active` on the retry, and Last Call sees that flag
and stands down, so it can never trap a session in a loop of its own making.

To see a real message rather than trust this description, point `doctor` at any
session transcript.

## How it works

Three hooks, each doing one thing:

| hook | job |
|---|---|
| `Stop` | measure, and warn or block on a band change |
| `SessionStart` | clear stale state, prune old files |
| `PostCompact` | re-arm the bands after compaction freed up room |

Design rules it sticks to:

- **Silent while green.** Below the threshold it emits nothing, so it never
  spends context warning you about context.
- **Once per band.** It speaks on a band *change*, not every turn.
- **Fail passive, never fail green.** Unknown window, unreadable transcript,
  broken config, unhandled exception — it goes quiet rather than guessing, and
  never takes the session down with it.
- **Re-arms after compaction.** A compact drops usage back to green; the bands
  reset so the next climb warns again. It detects the drop directly, so this
  works even without the `PostCompact` hook registered.
- **Reads the transcript backwards.** The record it needs is the newest one.
  20 ms on a 5.1 MB transcript, and it does not get slower as the session
  grows.
- **Counts only the main agent.** Subagents and sidechains have their own
  windows; their usage blocks are skipped.
- **Never derives the transcript path.** It uses the one in the payload. The
  project slug replaces *every* non-alphanumeric character, and `cwd` may be a
  subdirectory of the project, so a derived path is wrong twice over.

Context usage is `input + cache_read + cache_creation`, matching how Claude Code
reports it. `cache_read` dominates — reading `input_tokens` alone reports about
`2` on a session actually holding 690,000.

## Why 40% and 55%

These are not round numbers picked for feel. They are the ladder from the hook
this was rewritten from, which has driven roughly fifty unattended session
handoffs over a fortnight, and the reasoning behind them is worth stating:
long-context quality degrades well before the window is full, and Anthropic's
own agent harness compacts its orchestrator at 100k while capping subagents at
200k. Firing late is the failure that actually costs you a session — by the
time you are at 85%, the assistant has been working from a lossy memory for a
while. The ~15-point gap gives in-flight work room to land before red.

If that is too eager for you, `{"yellow_percent": 70, "red_percent": 85}` is one
line. Re-check these on every model upgrade; long-context quality has moved a
lot between adjacent releases.

## Setting up handover

After installing, run this once per project:

```
python3 <plugin>/scripts/lastcall.py setup
```

Five questions, each with a recommendation based on what is actually present
on your machine — whether this is a git repository, whether tmux, git and the
`claude` CLI are on `PATH`:

```
1/5  How big is this project's context window?
  1) 200,000 tokens — standard  <- recommended
  2) 1,000,000 tokens — extended
     Getting this wrong is the one thing that makes Last Call useless,
     so it refuses to guess.

2/5  Hand over to a fresh session automatically when context runs low?
  y) yes — write a handoff, then spawn a successor in tmux  <- recommended
  n) no  — just warn me; the session ends there
     tmux, git and the claude CLI are all present.

3/5  What command proves this project's environment is actually up?
  The successor runs this FIRST and must not start work until it passes.
  > npm test && curl -sf localhost:3000/health

4/5  What must PASS before this project hands over?
  Tests, linters, a review gate — comma separated. The wrap-up shows
  these to the assistant so it cannot hand over unverified work.
  > pytest -q, ruff check

5/5  Should the successor run UNATTENDED?
  y) yes — remote control on, permission prompts skipped  <- recommended
  n) no  — successor waits for permission like a normal session
     Unattended means the successor runs tools without asking. It is
     what lets a chain of sessions continue while you are away.
```

It writes `.claude/lastcall.json`, creates `docs/handoff/TEMPLATE.md` seeded
with that command, and tells you exactly what is and is not wired up:

```
automatic handover: READY
  ok   template configured
  ok   template invokes the relay
  ok   relay script present
  ok   tmux on PATH
  ok   git on PATH
  ok   claude CLI on PATH
```

**Handover is off until you do this,** and the tool says so rather than letting
you find out at the worst moment. Until it is configured:

- `doctor` reports `automatic handover: NOT SET UP` with a MISS beside each
  missing piece
- the installer prints the same warning when it finishes
- the built-in wrap-up message tells the assistant plainly that nothing will
  carry the work forward, and asks it to say so once. That notice disappears
  the moment you configure a template of your own

A tool whose job is to prevent silent failure has no business failing silently.

### The wrap-up sequence

The bundled template walks the assistant through the whole handover, and two of
its steps are the ones that make the difference between a chain that works and
one that quietly degrades:

**Gates.** `{gates}` renders the commands you listed in config. Nothing hands
over on unverified work, and a failing gate must be fixed or written down — not
quietly omitted:

```
  4. RUN THE GATES. Nothing hands over on unverified work:

       pytest -q
       ruff check
```

**The self-audit.** `{transcript}` renders the path to the session's own raw
transcript, which turns "verify before handing over" from a pious instruction
into something the assistant can actually do — read what happened instead of
trusting the memory that is, by definition, running out:

```
  5. AUDIT the handoff against what ACTUALLY happened, not against your memory
     of it. Your raw transcript is at:

       /home/you/.claude/projects/-home-you-myrepo/<session>.jsonl
```

That is where stale rows and wrong numbers get caught. A second model reading
the transcript against the handoff catches more than one model alone.

The successor is started with `read <handoff> and follow it.` — so work resumes
**without you typing a first prompt.** Everything the next session needs has to
be in that document.

### The handoff document

The relay spawns the successor. The *document* is what makes that successor
useful, and no script can write it for you — so setup writes the shape instead,
at `docs/handoff/TEMPLATE.md`:

```
0. Step 0 — bring the environment up, and PROVE it
1. Where things stand
2. Your first work
3. What the last session did
4. How to work here
5. Decided — do not re-ask
6. Where everything is
```

Two of those sections do most of the work.

**§0 must be able to fail.** State the expected result of each command, not
just the command. "`GET /health` returns 200" is a proof; "check the server is
running" is not. A Step 0 that cannot fail proves nothing, and the successor
will start work on a broken environment believing it verified one.

**§5 stops the relitigating.** A fresh context has no memory of why you chose
Postgres over SQLite, so without this it will happily reopen it. Writing the
decisions down is what keeps a chain of sessions moving in one direction.

The rest is ordinary: what is done, what is next and where, how this repository
expects work to be done. Keep only what changes what the next session does.

## The relay (optional, Unix + tmux only)

The guard tells the assistant to wrap up. The relay is what makes a session
hand over to a fresh one and keep going without you.

Nothing invokes it automatically — it is a script your wrap-up template tells
the assistant to run as its last step. The bundled template does exactly that:

```json
{ "template": "<plugin>/templates/handoff-relay.md" }
```

The template's step 6 resolves the `{relay}` placeholder to the script's real
path, so nothing needs hand-editing when the plugin updates.

What the relay does, in order, refusing to continue at the first failure:

- finds your newest handoff in `docs/handoff/` (configurable)
- **refuses to spawn while that handoff is uncommitted.** This is the
  load-bearing rule: a rule you must remember at the moment your context is
  exhausted is a rule that gets skipped, so it is a precondition, not a habit
- refuses on a dirty tree unless you pass `--allow-dirty`
- **works without git.** Git is how "committed" is checked, not a requirement
  to hand over. A plain directory proceeds with a loud warning saying the
  durability check was skipped; `--require-git` restores the strict behaviour
- spawns the successor in tmux, seeded with the handoff
- **waits until the successor makes a real tool call** before reporting
  success. An assistant turn is not proof of work — a refusal is an assistant
  turn. A process sitting on a permission dialog cannot make a tool call
- never kills anything. The successor is *asked*, in its prompt, to retire the
  predecessor once it has proved its first step and committed. A failed spawn
  therefore leaves the old session alive to report the failure

```
bash plugins/lastcall/relay/handoff.sh --dry-run     # resolve everything, spawn nothing
bash plugins/lastcall/relay/handoff.sh               # hand over
bash plugins/lastcall/relay/handoff.sh --no-skip-permissions # force prompts on
```

Exit codes: `0` successor up and working, `1` precondition failure (nothing was
spawned), `2` spawned but never proved itself.

**Workspace trust.** Claude Code asks "is this a project you trust?" the first
time it opens a directory, and `--dangerously-skip-permissions` does **not**
bypass it. A successor spawned into an untrusted folder sits on that prompt
forever: no tool call, no transcript, and a timeout that tells you nothing.
The relay checks `~/.claude.json` before spawning and refuses with an
actionable message, or records the trust for you with `--trust`.

The relay reads its settings from `relay` in `.claude/lastcall.json`, so you
configure it once and never pass flags:

```json
{ "relay": { "repo": "/path/to/the/repo", "handoff_dir": "docs/handoff",
             "name_prefix": "myproject", "remote_control": true,
             "skip_permissions": true } }
```

The config is found by walking up from your working directory, so a session
run from a parent folder that owns several repos still finds it — **where the
config lives and which repo to hand over are different questions**, and `repo`
answers the second one.

`remote_control` passes `--remote-control <session-name>`, so you can reach the
successor from anywhere rather than only from the pane it was born in.
`skip_permissions` passes `--dangerously-skip-permissions`, which is what lets
the chain continue while you are away — it means the successor runs tools
without asking, so `setup` asks before enabling it and `--no-skip-permissions`
turns it off for one run.

Requires `bash`, `tmux`, `python3` and the `claude` CLI. Git is optional. The guard needs
none of these — if you are on Windows, or you just want the alarm, ignore this
whole section.

## Tests

```
python3 -m unittest discover -s tests -v
```

165 tests, standard library only, no network. They cover the failure modes that
motivated this: thresholds that can never fire, bands that never re-arm,
sidechain usage read as the main session's, and path-valued config silently
discarded.

## What this does not do

The relay is Unix + tmux only, and off unless your template calls it. The
guard itself has no such dependency and works anywhere Python does.

## Licence

MIT.
