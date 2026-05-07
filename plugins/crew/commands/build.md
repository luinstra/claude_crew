---
description: Start verified development loop with advisor review before completion
argument-hint: "<task description>"
allowed-tools: Bash, Task
---

[BUILD LOOP STARTING]

$ARGUMENTS

## MANDATORY: Activate the Loop

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py init bl --prompt "$ARGUMENTS"
```

## How This Works

1. Delegate the implementation to the **executor agent** (keeps main context clean)
2. When executor reports complete, get advisor verification
3. If advisor approves (APPROVED), complete the loop
4. If advisor says REVISE with [BLOCKING] issues, re-delegate to executor with those issues, then re-verify
5. If advisor says REVISE with only [MINOR] issues, accept and complete
6. If you try to stop without completing, you'll be reminded to continue

You orchestrate. You don't implement. All file edits, builds, and tests happen inside the executor agent.

## Step 1: Delegate Work to Executor

Spawn the **executor agent** to implement the task:

```
Task(
  subagent_type="crew:executor",
  prompt="Execute: $ARGUMENTS

Work through the task systematically. Create todos to track progress. Implement the changes, run builds/tests where applicable, and report back: what you changed, what you verified, and any blockers."
)
```

Capture the executor's summary — you'll pass it to the advisor in the next step.

## Step 2: Get Advisor Verification

Before requesting verification:

- [ ] Executor reported the task complete
- [ ] No unresolved blockers in executor's report

When executor reports complete, spawn the **advisor agent**:

```
Task(subagent_type="crew:advisor", prompt="VERIFY AND REVIEW:

Task: $ARGUMENTS

Summary of executor's work:
[paste executor's summary here]

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

## Step 3: Handle the Verdict

| Verdict                             | Action                                                                       |
| ----------------------------------- | ---------------------------------------------------------------------------- |
| **APPROVED**                        | Complete the loop (Step 4)                                                   |
| **REVISE with only [MINOR]**        | Accept and complete the loop (Step 4)                                        |
| **REVISE with any [BLOCKING]**      | Re-delegate to executor with the issues (below), then return to Step 2       |

### Re-delegating on REVISE [BLOCKING]

```
Task(
  subagent_type="crew:executor",
  prompt="REVISION needed for previous task.

Original task: $ARGUMENTS

Advisor flagged these [BLOCKING] issues:
[list each blocking issue verbatim]

Address each issue. Report what you changed and what you verified."
)
```

Then go back to Step 2 to verify the revised work.

## Step 4: Complete the Loop

When the advisor returns APPROVED (or REVISE with only [MINOR] issues):

1. **Deactivate the loop:**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate bl --reason "Advisor verified complete"
```

2. **Summarize what was accomplished** and let the user know the task is complete.

## Early Exit

To exit before completion: `/crew:cancel-build`

---

First: run the activation command. Then proceed to Step 1 (delegate to executor).
