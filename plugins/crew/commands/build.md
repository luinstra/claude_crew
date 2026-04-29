---
description: Start verified development loop with advisor review before completion
argument-hint: "<task description>"
---

[BUILD LOOP STARTING]

$ARGUMENTS

## Session ID

Use the session ID from your SessionStart context (shown as `[Session ID: ...]`).
Pass it as `--session-id SESSION_ID` to all `crew-state.py` commands below.
The `CLAUDE_SESSION_ID` environment variable serves as an automatic fallback.

## MANDATORY: Activate the Loop

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py init bl --prompt "$ARGUMENTS" --session-id SESSION_ID
```

## How This Works

1. Work on the task using todos to track progress
2. When you think you're done, get advisor verification (see below)
3. If advisor approves (APPROVED), complete the loop (see below)
4. If advisor says REVISE with [BLOCKING] issues, fix them and re-verify
5. If advisor says REVISE with only [MINOR] issues, accept and complete
6. If you try to stop without completing, you'll be reminded to continue

## Getting Verification

When you believe the task is complete, spawn an advisor:

```
Task(subagent_type="crew:advisor", prompt="VERIFY AND REVIEW:

Task: $ARGUMENTS

Summary of work done:
[describe what you did]

Review context:
- Run `git diff` to see all changes
- Read modified files for full context

Verify:
1. All requirements are met (completeness)
2. Code quality (style consistency, security, performance, error handling)
3. No obvious gaps or issues

Verdict format:
- APPROVED: Work is complete and code quality is acceptable
- REVISE: Issues found. Classify each as [BLOCKING] or [MINOR]

If APPROVED or only [MINOR] issues, state: 'VERDICT: APPROVED'
If [BLOCKING] issues exist, state: 'VERDICT: REVISE' with list of issues")
```

**If advisor APPROVED** -> complete the loop (see below)
**If advisor REVISE with only [MINOR]** -> accept and complete the loop
**If advisor REVISE with [BLOCKING]** -> fix them, then verify again

## Completing the Loop

When the advisor approves your work (or only [MINOR] issues remain):

1. **Deactivate the loop:**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate bl --reason "Advisor verified complete" --session-id SESSION_ID
```

2. **Summarize what was accomplished** and let the user know the task is complete.

## Before Requesting Verification

- [ ] Todo list is 100% complete
- [ ] Code compiles/runs without errors
- [ ] Tests pass (if applicable)
- [ ] Original request is fully addressed

## Early Exit

To exit before completion: `/crew:cancel-build`

---

First: run the activation command. Then begin working.
