Wrap up this session and hand over to a fresh one.

  1. FINISH what is in flight. Your judgment on what is small enough to land —
     a two-line fix here is fine, a new phase is not.
  2. UPDATE the durable record of the work: whatever status, backlog or notes
     files this project keeps. Close what you actually finished; write down
     anything you found and did not fix.
  3. WRITE the successor's prompt — a dated file in docs/handoff/. It is NOT a
     summary, it is the next session's instructions, written for someone with
     zero context. It must carry: how to bring the environment up and PROVE it;
     what was done; what was left out and why; what to do next and WHERE; the
     decisions already taken so they are not re-argued; and how to work in this
     repository.
  4. AUDIT it against the actual state of the repository, not against your
     memory of the session — your memory is the thing that is running out.
     Read the diff. Check the claims.
  5. COMMIT everything. The handoff must be durable BEFORE any spawn, and the
     relay refuses to run while it is not.
  6. HAND OVER: run

         bash {relay}

     It spawns the successor in tmux seeded with the newest handoff and waits
     until that session has actually MADE A TOOL CALL before reporting success
     — not merely that a process exists. It never kills anything: the successor
     is ASKED, in its prompt, to retire this session once it has proved its
     first step and committed a checkpoint. That is an instruction, not a
     guarantee, so do not assume this session will end.

     Non-zero exit means THIS session is still alive and must report the
     failure; the launcher prints where its log ended up. Add --skip-permissions
     only if you want the successor to run unattended without permission
     prompts.
