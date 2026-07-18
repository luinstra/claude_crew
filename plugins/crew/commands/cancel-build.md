---
description: Cancel active Build Loop
allowed-tools: Bash
---

[BUILD LOOP CANCELLED]

The user has requested early exit from the build loop.

## MANDATORY ACTION

Execute this command to fully cancel the build loop. Pass `--session-id` (the
`[Session ID: …]` value injected this session, as a literal — NOT a
`${CLAUDE_SESSION_ID}` shell expansion) so cancel deactivates the exact
session-scoped state file the loop was init'd with; otherwise the scoped file
stays active and keeps blocking Stop:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate bl --cancel --reason "User cancelled via /crew:cancel-build" --session-id <session-id>
```

`--cancel` is required and is the point of this command: it ends the loop WITHOUT
a panel verdict, which is exactly what the user asked for, and records the exit as
a cancellation rather than an approval. Without it, a loop that no panel signed off
on is refused. It is legal from any phase (an escape hatch a phase check could
veto would not be one).

## After Running

The build loop is now cancelled. The persistent mode hook will no longer force continuation.

**You are free to:**
- Stop working
- Summarize what was accomplished (if anything)
- Start a new task

To start a new loop later: `/crew:build "task description"`
