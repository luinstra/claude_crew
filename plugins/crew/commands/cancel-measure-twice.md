---
description: Cancel active measure-twice loop
allowed-tools: Bash
---

[MEASURE-TWICE CANCELLED]

The user has requested early exit from the measure-twice loop.

## MANDATORY ACTION

Execute this command to fully cancel the measure-twice loop. Pass `--session-id`
(the `[Session ID: …]` value injected this session, as a literal — NOT a
`${CLAUDE_SESSION_ID}` shell expansion) so cancel deactivates the exact
session-scoped state file the loop was init'd with; otherwise the scoped file
stays active and keeps blocking Stop:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate mt --reason "User cancelled via /crew:cancel-measure-twice" --session-id <session-id>
```

## After Running

The measure-twice loop is now cancelled.

**You may:**
- Summarize current plan state (if any plan was generated)
- Stop working
- Start a new task

To start a new loop later: `/crew:measure-twice "task description"`
