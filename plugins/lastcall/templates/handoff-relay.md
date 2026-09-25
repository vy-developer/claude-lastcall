Wrap up this session and hand over to a fresh one. The successor starts from
your handoff with NO user prompt, so anything you leave out is lost.

  1. FINISH what is in flight. Your judgment on what is small enough to land —
     a two-line fix here is fine, a new phase is not.

  2. UPDATE the durable record: whatever status, backlog and gotchas files this
     project keeps. Close what you actually finished. Write down what you found
     and did not fix — an unrecorded problem gets rediscovered from scratch.

  3. WRITE the successor's prompt — a dated file in docs/handoff/, following
     docs/handoff/TEMPLATE.md. It is NOT a summary of this session. It is the
     next session's instructions, written for someone with ZERO context, and it
     must carry: how to bring the environment up and PROVE it; what was done;
     what was left out and why; what to do next and WHERE; the decisions
     already taken so they are not re-argued; and how to work in this
     repository — including when to use a subagent, a teammate, or a workflow,
     so the successor parallelises instead of decaying in one context.

  4. RUN THE GATES. Nothing hands over on unverified work:

{gates}

     If a gate fails, fix it or write down explicitly that it is failing and
     why. Do not hand over a green report you did not earn.

  5. AUDIT the handoff against what ACTUALLY happened, not against your memory
     of it — your memory is the thing that is running out. Your raw transcript
     is at:

       {transcript}

     Read it and cross-check: every claim in the handoff, every "done", every
     number. Look for what the docs do not mention — abandoned attempts, a
     decision made in passing, a test that was skipped. This step is where
     stale rows and wrong numbers get caught. A second model reviewing the
     transcript against the handoff catches more than you will alone.

  6. COMMIT everything. The handoff must be durable BEFORE any spawn, and the
     relay refuses to run while it is not.

  7. HAND OVER: run

         {relay}

     It refuses unless the handoff is committed, then starts the successor
     detached — Claude Code as a `claude --bg` session with Remote Control,
     Codex through `codex app-server` so the thread shows in the Codex app —
     seeded with the newest handoff, and reports success only once that
     session has CHECKED IN from its own SessionStart hook, not merely because
     a process exists. The successor is the same agent as you unless you add
     `--agent codex` or `--agent claude`, which hands over ACROSS agents
     (the handoff is plain Markdown either way). Nothing is killed before the
     check-in. After it, THIS session is retired only if "kill_predecessor"
     is set (or you pass --retire-predecessor): the relay then retires it a
     few seconds after reporting success — but never a desktop-app session.
     Otherwise nobody retires it and it stays open; the successor is NOT
     ASKED to stop it. Either way, start nothing after the relay succeeds,
     and do not assume this session will end.

     Exit 1 means nothing was spawned; exit 2 means a successor was started
     but never checked in. Either way THIS session is still alive and must
     report the failure. Add --dry-run to see every command first. The
     successor starts in auto mode, or in bypass mode if THIS session runs
     with bypass permissions; --skip-permissions forces bypass and
     --permission-mode MODE picks another mode.
