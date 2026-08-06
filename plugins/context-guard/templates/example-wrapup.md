Wrap up this session rather than starting anything new.

Available placeholders: {percent} {tokens} {window} {remaining} {band}
Currently at {percent:.0f}% — {remaining:,} tokens of headroom left.

  1. FINISH what is already in flight. Your judgment on what is small enough
     to land — a two-line fix here is fine, a new phase is not.
  2. UPDATE the record: docs/STATUS.md, docs/BACKLOG.md, gotchas.md.
     Close rows you actually fixed; file anything found and not fixed.
  3. WRITE the successor's prompt — a dated file in docs/handoff/. It is NOT
     a summary, it is the next session's instructions, written for someone
     with zero context. It must carry: how to bring the env up and PROVE it;
     what was done; what was left out and why; what to do next and WHERE; the
     decisions already taken so they are not re-asked; and how to work here.
  4. AUDIT it against the raw session transcript, not against your memory of
     the session. Ask a second model what the transcript shows that the docs
     do not.
  5. COMMIT everything. The handoff must be durable before the session ends.

Copy this file, edit it for your project, and point at it:

    {"template": ".claude/wrapup.md"}
