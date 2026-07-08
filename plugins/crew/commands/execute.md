---
description: Execute a plan or task via the executor agent (saves main context)
argument-hint: "<task or plan path>"
allowed-tools: Task, Read, Glob
---

## Execute via Executor Agent

**Task:** $ARGUMENTS

### Step 0: Consider Workspace Isolation

For multi-file features or significant changes, consider working in an isolated git worktree — especially if the main branch needs to stay clean during development.

### Step 1: Check for Plan File

If `$ARGUMENTS` references a plan (e.g., "the plan", a plan name, or a path like `.crew/plans/something.md`):
- Look in `.crew/plans/` for matching files
- Read the plan content to include in the executor prompt

### Step 2: Check for Model Override (optional)

Scan `$ARGUMENTS` for an explicit model keyword (case-insensitive):
- "use opus", "with opus", "opus model", "using opus" → model = `opus`
- "use sonnet", "with sonnet", "sonnet model", "using sonnet" → model = `sonnet`
- "use haiku", "with haiku", "haiku model", "using haiku" → model = `haiku`

If no keyword is found, skip — the executor will use its frontmatter default.

### Step 3: Delegate to Executor

Spawn the **executor agent**. Include `model="<chosen>"` only if Step 2 matched a keyword; otherwise omit the parameter:

```
Task(
  subagent_type="crew:executor",
  model="<chosen-model>",   # only include if a keyword was found
  prompt="Execute: $ARGUMENTS

[Include plan content here if a plan file was found]

Work through tasks systematically. Create todos to track progress. Report completion status and any blockers."
)
```

### Step 4: Report Results

When executor completes, summarize what was done and surface any issues that need user input.

---

**Note:** This command delegates to keep the main context clean. All the verbose file reads and edits stay in the executor's context.
