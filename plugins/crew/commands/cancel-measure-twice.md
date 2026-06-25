---
description: Cancel active measure-twice loop
allowed-tools: Bash
---

[MEASURE-TWICE CANCELLED]

The user has requested early exit from the measure-twice loop.

## MANDATORY ACTION

Execute this command to fully cancel the measure-twice loop:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate mt --reason "User cancelled via /crew:cancel-measure-twice"
```

## After Running

The measure-twice loop is now cancelled.

**You may:**
- Summarize current plan state (if any plan was generated)
- Stop working
- Start a new task

To start a new loop later: `/crew:measure-twice "task description"`
