# Context Guard

A Claude Code hook that tells the assistant when it is running out of context —
before it starts working from a compacted memory of files it only thinks it
read.

Long sessions fail quietly. Once the context window fills, older material is
summarized away, and the assistant carries on editing against stale state with
complete confidence. Nothing in the transcript announces this. Context Guard
measures the real number every time the assistant stops, and speaks up once,
at the moment it matters.

```
CONTEXT GUARD — YELLOW. 74% of the context window is in use
(743,106 of 1,000,000 tokens; 256,894 left).
Wrap up; do not stop dead. This is an alarm, not a decision — you judge
what still fits — but start no new phase or feature.
```

## Install

**As a plugin** (recommended — no absolute paths anywhere):

```
/plugin marketplace add YOURNAME/claude-context-guard
/plugin install context-guard@claude-context-guard
```

**Standalone**, if you would rather not use plugins, or you are on Windows
where `python3` is often not on `PATH`:

```
git clone https://github.com/YOURNAME/claude-context-guard
cd claude-context-guard
python3 install.py            # this project only
python3 install.py --global   # every project
python3 install.py --uninstall
```

The installer detects a working interpreter (`py -3` on Windows), writes the
hooks into `.claude/settings.json`, and backs up whatever was there first.

Requires Python 3.9+. No third-party packages, ever. CI runs the suite on
Linux, macOS and Windows against 3.9, 3.11 and 3.13.

## Tell it how big your window is

This is the one thing it will not guess, and it is worth explaining why.

A model identifier does not reveal the window size. Measured on a real 5.1 MB
transcript: a session running the 1M-context Opus records its model as plain
`claude-opus-5` — byte-identical to the 200K variant — while holding 743,106
tokens. That is 371% of the window the identifier implies. Any tool that infers
the window from the model name is wrong on that session and cannot tell.

Guessing low is the dangerous direction: a 1M session mislabelled as 200K gets
told to wrap up at 15% full, which actively wrecks good sessions. So Context
Guard stays **silent** when it does not know, and says so loudly when asked:

```
python3 .../context_guard.py doctor ~/.claude/projects/<project>/<session>.jsonl
```

Three ways to fix that, best first:

**1. The status line — exact, automatic.** Claude Code hands the status line the
real window size. Point yours at the bundled script and the guard never needs
telling again:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/plugins/context-guard/scripts/statusline.py"
  }
}
```

It prints a normal status line too: `Opus 5 | myrepo | ctx [#######---] 74% YELLOW`.
If your Claude Code version names those fields differently, `statusline.py --dump`
prints the raw payload so you can check.

**2. Just tell it.** `.claude/context-guard.json`:

```json
{ "context_window_tokens": 200000 }
```

**3. Let it prove the window itself.** A session cannot hold more tokens than
its window, so once usage passes 200,000 the window is provably the 1M one and
the guard starts working on its own. This is a proof, not an inference — but it
only helps 1M users, and only after they are already deep into a session.

## Configuration

`.claude/context-guard.json` in your project. Every field is optional, and every
one can be overridden per-run with `CONTEXT_GUARD_<FIELD>` in the environment.

| field | default | meaning |
|---|---|---|
| `yellow_percent` | `70` | warn once at this much of the window used |
| `red_percent` | `85` | escalate once here |
| `context_window_tokens` | `null` | window size; `null` means "work it out or stay quiet" |
| `mode` | `"block_once"` | `block_once` blocks the stop a single time at red so the handoff actually gets written; `advisory` never blocks |
| `template` | `null` | path to your own wrap-up instructions |
| `include_output_tokens` | `false` | count the last response's output too — budget for the next turn's input rather than the current window |
| `debug` | `false` | write a redacted copy of the last hook payload |
| `state_dir` | `~/.claude/context-guard` | where per-session state lives |
| `state_ttl_days` | `14` | prune state files older than this |
| `disabled` | `false` | turn the whole thing off without uninstalling |

### Your own wrap-up instructions

The built-in message is deliberately generic. What "wrap up" means is specific
to your project, so write it down and point at it:

```json
{ "template": ".claude/wrapup.md" }
```

Placeholders: `{percent}` `{tokens}` `{window}` `{remaining}` `{band}`. See
`plugins/context-guard/templates/example-wrapup.md` for a fuller one that
covers status docs, a dated handoff file, and committing before the session
ends.

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

## Tests

```
python3 -m unittest discover -s tests -v
```

52 tests, standard library only, no network. They cover the failure modes that
motivated this: thresholds that can never fire, bands that never re-arm,
sidechain usage read as the main session's, and path-valued config silently
discarded.

## What this does not do

It does not spawn a successor session. Automating the handoff itself needs a
terminal multiplexer, which pins you to Unix and to a specific idea of how your
project hands over. That belongs in a separate opt-in module, not here.

## Licence

MIT.
