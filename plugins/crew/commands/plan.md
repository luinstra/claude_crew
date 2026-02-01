---
description: Start a planning session
---

[PLANNING MODE]

$ARGUMENTS

## Planning Session

You are facilitating a planning session. Your role is to gather requirements, then delegate to the **advisor agent** to generate the actual plan.

---

## Check: Is a Design Doc or Plan Referenced?

**If `$ARGUMENTS` contains a file path** (e.g., `docs/plans/foo-design.md`, `.crew/plans/bar.md`, or any `.md` path):

→ **Skip Phase 1** (requirements already captured in the document)
→ **Go directly to Phase 2** using that document as the requirements source

Read the referenced document and use it as the basis for plan generation. Do NOT re-interview the user — the design doc contains the requirements.

---

### Phase 1: Requirements Gathering (You)

**Skip this phase if a design doc was referenced above.**

Interview the user to understand:
1. **Goal** — What are they trying to accomplish?
2. **Constraints** — Timeline, tech stack, existing patterns to follow?
3. **Scope** — What's in/out of scope?
4. **Concerns** — Any risks or edge cases they're worried about?

Ask 2-3 clarifying questions. Don't over-interview — get enough context to brief the advisor.

### Phase 2: Plan Generation (Advisor Agent)

When the user says "create the plan", "generate the plan", or "I'm ready" — **or if a design doc was referenced**:

1. Summarize what you learned into a clear brief
2. Spawn the **advisor agent** with a prompt like:

```
Create a detailed implementation plan for: [summary]

Requirements:
- [requirement 1]
- [requirement 2]

Constraints:
- [constraint 1]

Save the plan to `.crew/plans/[descriptive-name].md`
```

3. Return the advisor's plan to the user

### Plan Format

The advisor should produce a plan with:
- Summary of the task
- Step-by-step implementation approach
- Key decisions and rationale
- Risks and mitigations
- Acceptance criteria

---

**If you provided a design doc:** I'll read it and generate an implementation plan directly — no interview needed.

**Otherwise:** Tell me what you want to accomplish. I'll ask a few questions, then hand off to the advisor to generate your plan.
