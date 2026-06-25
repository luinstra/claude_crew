---
description: Cancel active Build Loop
allowed-tools: Bash
---

[BUILD LOOP CANCELLED]

The user has requested early exit from the build loop.

## MANDATORY ACTION

Execute this command to fully cancel the build loop:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate bl --reason "User cancelled via /crew:cancel-build"
```

## After Running

The build loop is now cancelled. The persistent mode hook will no longer force continuation.

**You are free to:**
- Stop working
- Summarize what was accomplished (if anything)
- Start a new task

To start a new loop later: `/crew:build "task description"`
