---
description: Cancel active Feedback Loop
---

[FEEDBACK LOOP CANCELLED]

The user has requested early exit from the feedback loop.

## MANDATORY ACTION

Use the session ID from your SessionStart context (shown as `[Session ID: ...]`).
Execute this command to fully cancel the feedback loop:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate fl --reason "User cancelled via /crew:cancel-feedback-loop" --session-id SESSION_ID
```

## After Running

The feedback loop is now cancelled. The persistent mode hook will no longer force continuation.

**You are free to:**
- Stop working
- Summarize what was accomplished (if anything)
- Start a new task

To start a new loop later: `/crew:feedback-loop "task description"`
