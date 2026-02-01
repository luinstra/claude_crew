---
description: Cancel active measure-twice loop
---

[MEASURE-TWICE CANCELLED]

The user has requested early exit from the measure-twice loop.

## MANDATORY ACTION

Execute this command to fully cancel the measure-twice loop:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate mt --reason "User cancelled via /cancel-measure-twice"
```

## After Running

The measure-twice loop is now cancelled.

**You may:**
- Summarize current plan state (if any plan was generated)
- Stop working
- Start a new task

To start a new loop later: `/crew:measure-twice "task description"`
