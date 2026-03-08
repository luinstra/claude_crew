---
description: Cancel active Build Loop
---

[BUILD LOOP CANCELLED]

The user has requested early exit from the build loop.

## MANDATORY ACTION

Use the session ID from your SessionStart context (shown as `[Session ID: ...]`).
Execute this command to fully cancel the build loop:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate bl --reason "User cancelled via /crew:cancel-build" --session-id SESSION_ID
```

## After Running

The build loop is now cancelled. The persistent mode hook will no longer force continuation.

**You are free to:**
- Stop working
- Summarize what was accomplished (if anything)
- Start a new task

To start a new loop later: `/crew:build "task description"`
