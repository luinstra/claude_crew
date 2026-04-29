---
description: Execute a plan or task via the executor agent (saves main context)
argument-hint: "<task or plan path>"
---

## Execute via Executor Agent

**Task:** $ARGUMENTS

### Step 0: Consider Workspace Isolation

For multi-file features or significant changes, consider suggesting `/superpowers:using-git-worktrees` to work in an isolated workspace — especially if the main branch needs to stay clean during development.

### Step 1: Check for Plan File

If `$ARGUMENTS` references a plan (e.g., "the plan", a plan name, or a path like `.crew/plans/something.md`):
- Look in `.crew/plans/` for matching files
- Read the plan content to include in the executor prompt

### Step 2: Delegate to Executor

Spawn the **executor agent**:

```
Task(
  subagent_type="crew:executor",
  prompt="Execute: $ARGUMENTS

[Include plan content here if a plan file was found]

Work through tasks systematically. Create todos to track progress. Report completion status and any blockers."
)
```

### Step 3: Report Results

When executor completes, summarize what was done and surface any issues that need user input.

---

**Note:** This command delegates to keep the main context clean. All the verbose file reads and edits stay in the executor's context.
