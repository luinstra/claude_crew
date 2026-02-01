---
description: Start verified development loop with advisor feedback before completion
---

[FEEDBACK LOOP STARTING]

$ARGUMENTS

## MANDATORY: Activate the Loop

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py init fl --prompt "$ARGUMENTS"
```

## How This Works

1. Work on the task using todos to track progress
2. When you think you're done, get advisor verification (see below)
3. If advisor approves, complete the loop (see below)
4. If you try to stop without completing, you'll be reminded to continue

## Getting Verification

When you believe the task is complete, spawn an advisor:

```
Task(subagent_type="advisor", prompt="VERIFY TASK COMPLETION:

Task: $ARGUMENTS

Summary of work done:
[describe what you did]

Please verify:
1. All requirements are met
2. No obvious gaps
3. Tests pass (if applicable)

If complete, state: 'VERIFIED: Task is complete'
If issues remain, list what needs fixing.")
```

**If advisor approves** → complete the loop (see below)
**If advisor finds issues** → fix them, then verify again

## Completing the Loop

When the advisor verifies your work is complete:

1. **Deactivate the loop:**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate fl --reason "Advisor verified complete"
```

2. **Summarize what was accomplished** and let the user know the task is complete.

## Before Requesting Verification

- [ ] Todo list is 100% complete
- [ ] Code compiles/runs without errors
- [ ] Tests pass (if applicable)
- [ ] Original request is fully addressed

## Early Exit

To exit before completion: `/crew:cancel-feedback-loop`

---

First: run the activation command. Then begin working.
