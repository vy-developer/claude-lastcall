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

         bash {relay}

     It spawns the successor in tmux seeded with the newest handoff and waits
     until that session has actually MADE A TOOL CALL before reporting success
     — not merely that a process exists. It never kills anything: the successor
     is ASKED, in its prompt, to retire this session once it has proved Step 0
     and committed a checkpoint. That is an instruction, not a guarantee, so do
     not assume this session will end.

     Non-zero exit means THIS session is still alive and must report the
     failure; the launcher prints where its log ended up. Add --skip-permissions
     if the successor should run unattended without permission prompts.
