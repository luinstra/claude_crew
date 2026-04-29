---
description: Review an existing plan
argument-hint: "<plan path (optional)>"
allowed-tools: Task, Read, Glob
---

[PLAN REVIEW MODE]

$ARGUMENTS

## Plan Review

You will delegate to the **advisor agent** to critically evaluate a plan.

### Usage

```
/crew:review .crew/plans/my-feature.md    # Review specific plan
/crew:review                               # Review most recent plan
```

### Your Role

1. **Locate the plan** — If no path given, find the most recent `.md` file in `.crew/plans/`
2. **Spawn advisor** — Delegate the review to the advisor agent
3. **Report results** — Present the advisor's verdict to the user

### Advisor Prompt

Spawn the **advisor agent** with:

```
Review this plan for clarity, completeness, and executability:

[path to plan]

Evaluation criteria:
- **Clarity**: Can someone execute this without guessing?
- **Testability**: Are acceptance criteria concrete and measurable?
- **Completeness**: Are edge cases and dependencies addressed?
- **Context**: Does the plan explain WHY, not just WHAT?

Verdict options:
- **APPROVED** - Ready for execution
- **REVISE** - Issues to address (list specific feedback)
- **REJECT** - Fundamental problems requiring replanning

Provide your verdict with detailed reasoning.
```

---

Provide a plan file path, or I'll find the most recent plan in `.crew/plans/` and have the advisor review it.
