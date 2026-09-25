# Last Call

**Last call for your coding agent's session.** Last Call watches how full the
context window is and tells the assistant to wrap up while it still has the
room to do it properly. It works with **Claude Code and OpenAI Codex**, in the
CLIs and in the desktop apps.

Long sessions fail quietly. Once the window fills, older material is
summarized away and the assistant keeps editing against stale state with
complete confidence. Most context tools show *you* a number. This one talks to
the assistant, once per threshold, and can hold the session open until the
handoff is written:

```
LAST CALL — YELLOW. 43% of the context window is in use
(430,000 of 1,000,000 tokens; 570,000 left).
Finish what is in flight; start nothing new. This is an alarm, not a
decision — you judge what still fits.
```

Optionally, **the relay** hands the work to a fresh Claude Code or Codex
session, proves that the new session started, and retires the old one.
`lastcall status` shows every live session of both agents with its context
usage. Python 3.9+, standard library only. CI covers Linux, macOS and Windows
on Python 3.9, 3.11 and 3.13.

## Install

Install once per machine. This covers both agents, CLI and desktop app:

```
git clone https://github.com/vy-developer/claude-lastcall
claude-lastcall/plugins/lastcall/bin/lastcall install      # --dry-run to preview
```

This registers the checkout as a local marketplace. It then installs the
plugin through each agent's own `plugin` command, for every agent on `PATH`
(`--claude` or `--codex` picks one). It also links `lastcall` into
`~/.local/bin` if that directory is on `PATH`, and creates a commented
`~/.lastcall/config.json` in which nothing is active yet.

- **Codex needs one trust step.** New hooks do not run until you open Codex,
  run `/hooks`, and trust the Last Call entries. The trust is stored in
  `~/.codex/config.toml`, so the desktop app shares it.
- **The desktop apps use the same configuration as the CLIs.** Claude desktop
  runs a bundled Claude Code that reads `~/.claude`, and Codex desktop uses
  `CODEX_HOME`. Restart open sessions to load Last Call.
- **After `git pull`**, run `lastcall install --refresh`. Claude Code loads the
  plugin in place from the checkout, so restart its sessions (or run
  `/reload-plugins`). Codex runs a cached copy, and `--refresh` re-copies it.
- **`--method hooks`** writes hook entries straight into
  `~/.claude/settings.json` and `~/.codex/hooks.json`, with absolute paths.
  On macOS and Linux each entry runs the same `scripts/lastcall-hook` launcher
  as the plugin; `--python PATH` pins the interpreter it uses.
  Before writing, it backs each file up to `<file>.lastcall.bak`, and it only
  ever touches Last Call's own entries. `--project DIR` targets one project
  instead. A plugin install removes any leftover hooks-method entries, so
  nothing fires twice.
- `lastcall uninstall` reverses all of it and leaves `~/.lastcall` in place.
  The old `python3 install.py` still works: it wraps `lastcall install
  --method hooks` for one project, or for all of them with `--global`.

Desktop apps run hooks with a minimal `PATH`, so the `sh` launcher
`scripts/lastcall-hook` finds Python itself. It tries `$LASTCALL_PYTHON`, then
`python3` on `PATH`, Homebrew, `/usr/local`, pyenv and conda. It uses macOS's
`/usr/bin/python3` only when the Command Line Tools exist, so no install
dialog appears, and it exits quietly if it finds no Python.

**On Windows**, Codex runs `py -3` directly. Claude Code has no per-OS hook
command, so without Git Bash, use `lastcall install --method hooks`, which
writes the path of a detected interpreter. The relay is POSIX-only (macOS,
Linux, WSL).

Warnings work as soon as Last Call is installed. Handing over to a fresh
session stays off until you [set it up](#onboarding).

## How it works

`scripts/lastcall.py <Event>` handles every hook for both agents. It detects
which agent is calling (`LASTCALL_AGENT` forces it).

- **Measure** on `PostToolUse`, `UserPromptSubmit` and `Stop`. For Claude Code,
  this is `input + cache_read + cache_creation` of the newest main-session
  response; subagent and sidechain usage is skipped. For Codex, it is the
  rollout's newest `last_token_usage.total_tokens`.
  A `PostToolUse` reading can lag one model call behind, because the agent
  writes that call's usage just after the hook starts; `Stop` readings are
  current.
- **Warn once per zone.** Nothing is printed below every zone, so it costs no
  context. When a zone is first entered, whichever hook notices delivers the
  warning, once.
- **Hold once.** At a zone with `"block": true` (red, by default), `Stop`
  returns `decision: "block"` one time, so the wrap-up actually gets written.
  While `stop_hook_active` is set it says nothing, so it never loops, and that
  matters on Codex, which has no loop cap.
- **Re-arm on compaction.** Compaction is detected from the compaction record,
  from `PostCompact`, or from a steep drop in usage. The zones then warn again
  on the next climb, and the model is told to re-read its plan and handoff
  files.
- **Fail passive.** An unreadable transcript, a broken config or an exception
  makes it go quiet. It never breaks the session.

Codex rejects a whole payload over one unexpected key, so each agent's
adapter builds its own output:

| event | Claude Code | Codex |
|---|---|---|
| `PostToolUse`, `UserPromptSubmit` | `additionalContext`, mid-turn | `additionalContext`, mid-turn |
| `Stop`, warning | `additionalContext`; the model continues once to read it | `decision: "block"` with the warning as the reason, the only Stop output Codex shows the model |
| `Stop`, blocking zone | `decision: "block"` with the warning, once | the same |
| `SessionStart` | after compaction: the re-read note. Unconfigured project: the onboarding offer, once per project. | the same |

**Why 40% and 55%.** Long-context quality degrades well before the window is
full, and firing late is the failure that costs a session. The defaults come
from a hook that drove about fifty unattended handoffs. For a later ladder,
`{"yellow_percent": 70, "red_percent": 85}` is one line.

## Context windows

**Codex** writes the usable window into every rollout, and Last Call uses
that figure over everything, config included. Until the rollout states it,
Codex's own `models_cache.json` fills in.

**Claude Code** does not record the window. The 200K and 1M variants of a
model share one id. So Last Call takes the first of these that applies:

1. `context_window_tokens`
2. the status line (exact; see below)
3. the `windows` map, matched against the session's model
4. a window learned for that model
5. `[1m]` in the session's model name
6. a `[1m]` model in `$ANTHROPIC_MODEL` or `~/.claude/settings.json` that names
   this model
7. proof: more than 200,000 tokens in use means the window is 1M
8. `fallback_window_tokens` (200,000), used only while the tokens in use fit in
   it

When the tokens in use disprove any of 1 to 6, the window is corrected, and
the source says so. A window from step 8 is **assumed**: its warnings say so,
tell a 1M session to carry on, and never block. Set the fallback to `null` to
stay silent until the window is known.

**The `windows` map** goes in `~/.lastcall/config.json` or a project config.
Entries from both files merge:

```json
{ "windows": { "claude-opus-5-5": 1000000, "claude-sonnet-*": 200000, "claude:opus": 1000000 } }
```

A key is an exact model id, a prefix, or a `*`/`?` glob. An exact key beats
the longest prefix or glob. `claude:` or `codex:` limits a key to one agent.

**Learned windows.** When a session *proves* its window, Last Call records it
in `~/.lastcall/state/windows.json`. Proof means more than 200K tokens in use,
the status line's figure, or a Codex rollout. The next session on that model
may be running the 200K variant, so it treats a learned 1M as assumed (it
warns, never blocks) until its own tokens pass 200K. If a session on the model
auto-compacts below 60% of the learned window, the model evidently runs
smaller too. The entry is then marked **conflicted** and ignored, and `doctor`
and `status` tell you to **pin the model** in `windows`. The map always wins
over anything learned.

**The status line** receives the real window from Claude Code, caches it for
the hooks, and prints `Opus 5 | myrepo | ctx [####------] 43% YELLOW`:

```json
{ "statusLine": { "type": "command",
                  "command": "python3 /path/to/plugins/lastcall/scripts/statusline.py" } }
```

Alternatively, write zones in `at_tokens` (below). Those zones need no window
at all.

## Settings

**Files, later wins:** the defaults, then `~/.lastcall/config.json`
(`$LASTCALL_HOME`), then the nearest project config found by walking up from
the project directory. A project config is `.lastcall.json`,
`.lastcall/config.json`, the legacy `.claude/lastcall.json`, or
`.codex/lastcall.json`, and the walk never stops at your home directory. Last
come `LASTCALL_<FIELD>` environment variables, such as
`LASTCALL_RED_PERCENT=70` (any case). A project value replaces the global one;
`windows` and the `relay` block merge key by key instead. `lastcall doctor`
reports unknown keys (suggesting the key you probably meant), wrong types and
missing templates. See [Reference](#reference) for every key, and
[`lastcall.example.json`](plugins/lastcall/lastcall.example.json) for a
commented starting point.

### Zones

Yellow and red are only the default. `zones` sets as many zones as you like,
with your names, thresholds and instructions:

```json
{ "zones": [
    { "name": "nudge",    "at": 50, "message": "Past halfway. Finish threads, don't open them." },
    { "name": "winddown", "at": 70, "template": ".lastcall/winddown.md" },
    { "name": "closing",  "at_tokens": 550000, "template": ".lastcall/closing.md", "block": true } ],
  "min_window_tokens": 500000 }
```

A zone takes `name`, then either `at` (a percentage) or `at_tokens` (a count
that needs no window). It can also take `template` or `message` for its own
instructions, a `headline` line, and `block` to hold the stop once.
`min_window_tokens` silences a ladder on smaller windows. Malformed zones are
dropped rather than being fatal. `"mode": "advisory"` disables every `block`.

### Templates, gates and the verifier

Last Call writes the first part of each warning: the numbers and the zone's
headline. You write the rest. It comes from the zone's `template`, else its
`message`, else the project `template`, else a generic built-in that also
says handover is not set up. The placeholders are `{percent}` `{tokens}`
`{window}` `{remaining}` `{zone}`, `{gates}`, `{verifier}`, `{transcript}` and
`{relay}`. `{transcript}` is the session's own raw transcript, so the
assistant can audit its handoff against what really happened. `{relay}` is the
full relay command. Start from
[`example-wrapup.md`](plugins/lastcall/templates/example-wrapup.md), or from
[`handoff-relay.md`](plugins/lastcall/templates/handoff-relay.md) to end with
a handover.

`gates` lists shell commands that must pass before handing over
(`"pytest -q"`, not "run the tests"). `verifier` names a second model that checks the
work, such as `codex exec "Review this branch against the plan…"`. The hook
never runs either one, because a test suite run at Stop time would hang the
session. It puts them in front of the assistant, where skipping them is
visible.

### Onboarding

Onboarding is one flow with three front ends, and each asks the same
questions, in the same order, with the same recommendations:

- **In session.** The first session in a project with no configuration offers
  to tailor Last Call, once per project. The installer's commented example
  does not count as configuration. The assistant reads AGENTS.md, CLAUDE.md,
  the README and the CI config first, so it proposes your real commands.
- **`/lastcall:onboard`** runs the same interview on demand. In Codex it
  appears as a skill.
- **`lastcall setup`** asks the nine questions in a terminal. Questions 6 to 9
  are asked only if you want handover:

```
1/9  When should Last Call warn that the context is filling?
2/9  What does wrap-up mean in this project?
3/9  What must PASS before this project hands over?
4/9  Have a SECOND model check the work before handing over?
5/9  Hand over to a fresh session automatically when context runs low?
6/9  Which model should drive the successor?
7/9  Which permission mode should the successor start in?
8/9  Start a Claude successor with Remote Control?
9/9  Retire the OLD session once the successor has checked in?
```

The permission question recommends `inherit` (see **Permissions** under the
relay), and bypass for every successor needs an explicit, typed choice. Each flow writes to **this project
only**, never your home directory: `.lastcall.json` (an existing project
config is updated in place, after a `.bak` copy), `.lastcall/wrapup.md`, and,
with handover, `docs/handoff/TEMPLATE.md`. It then runs the `doctor` readiness
check. To keep Last Call out of a project, write `{"disabled": true}`.

## Handover: the relay

The relay hands a session over to a fresh one. It needs no tmux and no TTY,
so desktop-app sessions can hand over too. It only runs when the wrap-up
template tells the assistant to run it, which the shipped `handoff-relay.md`
does via `{relay}` (`python3 <plugin>/bin/lastcall relay`).

```
lastcall relay --dry-run         # resolve and print everything, spawn nothing
lastcall relay                   # hand over to the same agent
lastcall relay --agent codex     # hand over across agents (or --agent claude)
```

It stops at the first failure:

1. It picks the newest handoff in `docs/handoff/`, skipping `TEMPLATE.md`.
2. It refuses while that handoff is uncommitted, and names the newest
   committed one for `--handoff`. It also refuses on a dirty tree unless you
   pass `--allow-dirty` or set `dirty_baseline`. Without git, it warns that
   the check was skipped; `--require-git` refuses instead.
3. It starts the successor detached, named `<prefix> · handoff N · <topic>`,
   with the prompt `read <handoff> and follow it.` The successor gets a clean
   environment: the predecessor's session-identity variables are dropped, and
   provider and auth settings are kept.
4. It waits for the successor to **check in** on
   `~/.lastcall/relay/<chain>.jsonl`. Only a check-in that carries this
   spawn's nonce counts, so a stale successor from an earlier attempt cannot
   pass for this one.
5. It runs the agent-specific checks below, then retires the predecessor, if
   that is configured.

Exit codes: `0` the successor checked in; `1` a precondition failed and
nothing was spawned; `2` something was spawned but never checked in. A failed
handover always leaves the old session alive to report the failure.

**Agent.** The successor's agent is `relay.agent`, else the agent running the
predecessor, else Claude. Set `agent` (or pass `--agent`) to hand over across
agents; the handoff is plain Markdown either way.

**Claude successors** start with `claude --bg -n NAME --remote-control NAME
--permission-mode auto --settings JSON PROMPT`. The inline settings add a `SessionStart` hook that
checks in. **Remote Control is verified**: the relay looks for
`bridgeSessionId` in `~/.claude/jobs/<short>/state.json`, then in a
`bridge-session` transcript entry, then in `~/.claude/sessions`. If it finds
none, it prints `remote control did NOT connect`; with
`--require-remote-control`, it also exits 2 and retires nothing. `claude --bg`
refuses folders you have not trusted, and skipping permissions does not change
that. Run `claude` once in the repo and accept the prompt; the relay never
edits `~/.claude.json`.

**Codex successors** depend on `codex_mode`:

- `app` (the default): a detached runner drives `codex app-server` over
  JSON-RPC (`thread/start`, `thread/name/set`, `turn/start`), so the thread
  appears in the Codex app's sidebar and in `codex resume`. The runner checks
  in once the turn is accepted, and keeps the server up until the turn ends or
  `codex_app_max_seconds` runs out. If app mode fails before the turn starts,
  the relay loudly falls back to `exec`.

  Starting a `workspace-write` thread makes Codex itself mark the repo as a
  trusted project in `~/.codex/config.toml` (`[projects."<repo>"]
  trust_level = "trusted"`). An unattended successor needs that trust anyway,
  so the relay does not prevent it. It reads the file (never writes it) and,
  when the repo is not trusted yet, says so before spawning.
- `exec` runs `codex exec --json`. That thread is hidden from the sidebar and
  from the default `codex resume` list.
- `tmux` runs the TUI in a tmux session. It is the only mode that needs tmux.

**Retiring the predecessor** is off by default. Turn it on with
`kill_predecessor` (alias `retire_predecessor`, or `--retire-predecessor`).
The relay runs inside the session it retires, so the retirement happens only
after the check-in, `kill_delay` seconds later, from a detached process. It is
logged as `retired` or `retire-failed`.

| predecessor | retired by |
|---|---|
| a `claude --bg` session | `claude stop <short id>` |
| a Claude desktop, IDE or SDK session | **never** (close it in the app) |
| a Codex thread with no `codex` CLI above the relay (desktop app, app-server) | **never** |
| a tmux session that an earlier relay created | `tmux kill-session` |
| a tmux pane whose process is an ancestor of the predecessor | `tmux kill-pane` |
| a plain Claude or Codex CLI process | `SIGTERM` |

`TMUX_PANE` alone is never trusted: an app started from a tmux shell inherits
it.

**Time budget.** Every wait comes out of one budget: spawn, check-in, Remote
Control and naming the thread. The budget is `max_wait_seconds`, 105 s by
default, which fits inside Claude Code's 2-minute Bash tool timeout. The relay
prints its worst case up front.

**Permissions.** A Claude successor starts in Claude Code's **auto mode**
(`--permission-mode auto`), unless the predecessor runs with **bypass
permissions**; then the successor does too (`--dangerously-skip-permissions`).
Every hook payload carries the session's `permission_mode`, the hooks record
the latest one in the session's state, and the relay reads it for the session
it runs in. A mode it cannot find counts as not bypass. Codex reports only
`default` or `bypassPermissions`, the latter when approvals and the sandbox are
both off (`--dangerously-bypass-approvals-and-sandbox`).

A Codex successor keeps `codex_sandbox` (`workspace-write`) and
`codex_approval` (`never`, so app-mode approval requests are declined) for
every mode but bypass. Bypass gives it full access: sandbox
`danger-full-access` with requests accepted in app mode, and
`--dangerously-bypass-approvals-and-sandbox` in `exec` and `tmux` mode. The
other modes are Claude's and do not change a Codex successor.

The first match wins:

1. `--skip-permissions`, else `--permission-mode MODE`
2. `relay.skip_permissions: true`, else `relay.permission_mode`
3. the predecessor is in bypass mode: bypass
4. `auto`

`--no-skip-permissions`, or `skip_permissions: false` in the config, rules out
bypass from every level below it, the predecessor's included. `inherit` at a
level goes straight to step 3. `--dry-run` prints the choice and its reason:
`auto (default)`, `bypass (predecessor is in bypass mode)`,
`plan (from config: permission_mode)`. If auto mode is not available to the
account or the model, Claude Code decides what the successor gets. If the
relay itself runs inside a Codex sandbox, the successor inherits that sandbox,
and the relay warns about it.

| relay key | default | meaning |
|---|---|---|
| `agent` | the predecessor's | `claude` or `codex` |
| `repo` | project, git toplevel, or cwd | the directory to hand over |
| `handoff_dir` | `docs/handoff` | where handoffs live, relative to the repo |
| `name_prefix` | repo name | first part of the successor's name |
| `model`, `fallback_model` | CLI default | Claude model, and comma-separated fallbacks |
| `codex_model` | CLI default | Codex model |
| `remote_control` | `true` | start a Claude successor with Remote Control |
| `permission_mode` | `inherit` | `auto`, `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`, or `inherit` (auto; bypass when the predecessor is) |
| `skip_permissions` | unset | `true`: bypass mode; `false`: never inherit bypass |
| `kill_predecessor` | `false` | retire the old session after the check-in |
| `kill_delay` | `5` | seconds from the check-in to the retirement |
| `max_wait_seconds` | `105` | total budget for every wait |
| `require_git` | `false` | refuse outside a git worktree |
| `dirty_baseline` | none | paths that may be dirty |
| `codex_mode` | `app` | `app`, `exec` or `tmux` |
| `codex_sandbox` | `workspace-write` | Codex sandbox |
| `codex_approval` | `never` | app-mode approval policy |
| `codex_app_max_seconds` | `21600` | app-mode runner lifetime |

`LASTCALL_RELAY` (JSON) goes on top of the config, and flags beat everything;
`lastcall relay --help` lists them all. With the plugin installed, every
successor's `SessionStart` hook also checks in and tells the successor which
handoff to read. `relay/handoff.sh` is a deprecated shim: it maps the old
flags and variables (`--kill-predecessor`, `LASTCALL_MODEL`, …) onto the
relay, so old configs keep working.

**The handoff document** is what makes the successor useful. `setup` writes
its shape to `docs/handoff/TEMPLATE.md`. Step 0 brings the environment up and
**proves** it, so it must be able to fail ("`GET /health` returns 200", not
"check the server"). The other sections are: where things stand, the first
work, what was done, how to work here, **decided, do not re-ask**, and where
everything is.

Verified against Claude Code 2.1.281 and Codex 0.153.4: Codex app mode end to
end, the Claude `--bg` check-in, and `bridgeSessionId` in a `--bg` job's
state. The exec fallback, tmux mode and the retirement paths have been tested
only against fake binaries.

## Seeing your sessions

`lastcall status` lists the live sessions of both agents. Filter with
`--agent` or `--surface`, and use `--json` for machine-readable output:

```
AGENT   SURFACE  PROJECT  TITLE                        STATUS  RC  AGE  CONTEXT
claude  cli      myrepo   myrepo · handoff 3 · parser  busy    ✓   2m   312k/1000k 31%
codex   desktop  site     fix the build                idle    –   9m   88k/258k 34%
```

`RC` is Remote Control: ✓ connected, ✗ not connected, – not applicable.
`CONTEXT` is judged against the same window the hooks use, and a note under
the table flags models with conflicted learned windows. `--json` carries the
same figures as `tokens`, `window`, `percent` and `window_source` (`null`
when unknown). Claude sessions come
from `~/.claude/sessions`. Codex sessions come from the rollouts a running
`codex` holds open (checked with `lsof`); without `lsof`, recently written
rollouts count.

`lastcall tidy` proposes `<project> · <title>` names for old chats. Vague or
missing titles are derived from the first prompt, and duplicates get a date:

```
lastcall tidy                        # read-only table (--project, --older-than DAYS)
lastcall tidy --plan plan.json       # write the proposal; review and edit it
lastcall tidy --apply plan.json      # apply it (--dry-run to preview)
```

Applying writes the same records the agents' own rename commands write: a
`custom-title` line in a Claude transcript, or an entry in Codex's
`session_index.jsonl`. Each file is first copied to
`<agent home>/lastcall-backups/<stamp>/`. **Live sessions are skipped**, and
checked again at apply time. **Desktop-app sessions are skipped** unless you
pass `--include-desktop`, because the apps keep their own titles. Plan files
hold a hash, never prompt text.

## Doctor and troubleshooting

`lastcall doctor` shows what resolved: the config files, any `PROBLEM`, the
zones, the `windows` map and learned windows, and whether **automatic
handover** is `READY` or `NOT SET UP`, with an `ok`/`MISS` line per piece.
For each agent on `PATH` it also reports the install: whether the plugin is
installed and enabled, any hooks-method entries (both at once fire every hook
twice), and for Codex how many Last Call hooks `/hooks` has trusted. It reads
the agents' own files and runs neither CLI, so it stays quick.
Give it a transcript (`~/.claude/projects/…/<session>.jsonl` or
`~/.codex/sessions/…/rollout-….jsonl`) to measure a real session: agent,
model, tokens, window and its source, percentage and zone.

| symptom | fix |
|---|---|
| Codex never warns | trust the hooks: `/hooks` in Codex |
| a desktop app never warns | restart it, and check that Python 3.9+ is installed where the launcher looks |
| warns too early on a 1M Claude session | pin the model in `windows`, or install the status line |
| window `UNKNOWN` in `doctor` | `fallback_window_tokens` is `null`; set a window source |
| hooks fire twice | re-run `lastcall install` (removes old hooks-method entries) |
| old behaviour after `git pull` | `lastcall install --refresh`, then restart sessions |
| relay: `untrusted workspace` | run `claude` once in the repo |
| relay: handoff not committed / tree dirty | commit, or use `--handoff` / `--allow-dirty` |
| relay cut off by the Bash tool timeout | lower `max_wait_seconds`, or run it in the background |
| the hook seems silent | run it by hand with `LASTCALL_TRACE=1` to see the exception |

## Reference

### Configuration keys

| field | default | meaning |
|---|---|---|
| `yellow_percent` | `40` | warn once at this share of the window |
| `red_percent` | `55` | escalate, and hold the stop once |
| `zones` | `null` | your own zones instead of the two above |
| `min_window_tokens` | `null` | stay silent on a smaller window |
| `gates` | `null` | commands that must pass, shown as `{gates}` |
| `verifier` | `null` | second-opinion command, shown as `{verifier}` |
| `relay` | `null` | relay settings (see the relay key table) |
| `context_window_tokens` | `null` | one window for every Claude session; beats even the status line |
| `windows` | `null` | model → window map |
| `fallback_window_tokens` | `200000` | assumed Claude window: warns, never blocks; `null` stays silent |
| `mode` | `"block_once"` | `advisory` never blocks |
| `template` | `null` | your wrap-up instructions |
| `compaction_note` | `null` | post-compaction note: built-in, `false` for none, or your text |
| `include_output_tokens` | `false` | also count the last response's output |
| `debug` | `false` | save a redacted copy of the last hook payload |
| `state_dir` | `~/.lastcall/state` | per-session state |
| `state_ttl_days` | `14` | prune session state older than this |
| `disabled` | `false` | turn Last Call off here |

### Environment variables

`LASTCALL_<FIELD>` overrides one key. `LASTCALL_HOME` replaces `~/.lastcall`.
`LASTCALL_AGENT` forces `claude` or `codex`. `LASTCALL_PYTHON` sets the hook
interpreter. `LASTCALL_TRACE` raises exceptions instead of staying silent.
`LASTCALL_CLAUDE_HOME` and `LASTCALL_CODEX_HOME` set the homes `status` and
`tidy` read, which otherwise follow `CLAUDE_CONFIG_DIR` and `CODEX_HOME`.
`LASTCALL_RELAY_DIR` moves the relay ledgers. `CLAUDE_BIN`, `CODEX_BIN` and
`TMUX_BIN` set the binaries the relay runs. The relay sets `LASTCALL_RELAY_*`
on successors; do not set those yourself.

### Hook events

| event | timeout | job |
|---|---|---|
| `PostToolUse` (`*`) | 5 s | measure and warn mid-turn (skipped for subagents and unchanged transcripts) |
| `UserPromptSubmit` | 5 s | measure and warn before the turn |
| `Stop` | 15 s | measure, warn, or hold once |
| `SessionStart` | 10 s | reset (new), keep (resume), re-arm and remind (compact), offer onboarding, relay check-in |
| `PostCompact` | 10 s | re-arm the zones |

### Files it writes

| path | contents |
|---|---|
| `~/.lastcall/config.json` | global config (a commented example at first) |
| `~/.lastcall/state/<agent>-<session>.json` | session state, including the latest `permission_mode` the relay reads; pruned after `state_ttl_days`, and only files with Last Call's marker are deleted |
| `~/.lastcall/state/windows.json`, `onboarded.json` | learned windows, and projects already offered onboarding |
| `~/.lastcall/state/last-payload.json` | only with `debug` |
| `~/.lastcall/relay/<chain>.jsonl` | relay ledger, plus Codex successor logs |
| `<file>.lastcall.bak`, `<agent home>/lastcall-backups/` | backups from `install` and from `tidy --apply` |

## Tests

```
python3 -m unittest discover -s tests -v
```

723 tests, standard library only, no network. They cover the failure modes
that shaped the design: thresholds that can never fire, zones that never
re-arm, sidechain usage counted as the main session's, Stop payloads Codex
would reject, and a README that drifts from the code.

## Licence

MIT.
