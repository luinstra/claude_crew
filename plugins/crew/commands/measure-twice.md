---
description: Start a self-refining plan loop until advisor approves
argument-hint: "<task description or .md path>"
allowed-tools: Bash, Task
---

[MEASURE-TWICE MODE STARTING]

$ARGUMENTS

## How This Works

1. **Gather Requirements** - Interview user to understand the task (or skip if design doc provided)
2. **Generate Plan** - Advisor creates the plan
3. **Review** - Another advisor evaluates (with BLOCKING/MINOR classification)
4. **Iterate** - If BLOCKING issues: revise and re-review. If REJECT: rethink approach.
5. **Complete** - When advisor returns APPROVED (or only MINOR issues)

---

## Check: Is a Design Doc Referenced?

**If `$ARGUMENTS` contains a file path** (e.g., `docs/plans/foo-design.md`, `.crew/plans/bar.md`, or any `.md` path):

→ **Skip Phase 1** (requirements already captured in the document)
→ **Go directly to Phase 2** using that document as the requirements source

Read the referenced document and use it as the basis for plan generation. Do NOT re-interview the user — the design doc contains the requirements.

---

## Phase 1: Requirements Gathering (You)

**Skip this phase if a design doc was referenced above.**

Interview the user to understand:
1. **Goal** — What are they trying to accomplish?
2. **Constraints** — Timeline, tech stack, existing patterns to follow?
3. **Scope** — What's in/out of scope?
4. **Concerns** — Any risks or edge cases they're worried about?

Ask 2-3 clarifying questions. Don't over-interview — get enough context to brief the advisor.

---

## Phase 2: Plan Generation

When the user says "create the plan", "generate the plan", or "I'm ready" — **or if a design doc was referenced**:

### Step 1: Activate the Loop

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py init mt --task "$ARGUMENTS" --auto-plan
```

Then get the state to find the plan file path:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py show mt
```

### Step 2: Generate Initial Plan

Summarize what you learned from the interview, then spawn the **advisor agent** to create the plan:

```
Task(subagent_type="crew:advisor", prompt="Create an implementation plan for: [summary of task]

Requirements:
- [requirement 1 from interview]
- [requirement 2 from interview]

Constraints:
- [constraint 1 from interview]

Save the plan to: [plan_file from state]

The plan must include:
- Summary of the task
- Step-by-step implementation approach
- Key decisions and rationale
- Risks and mitigations
- Acceptance criteria

Focus on clarity and executability — this plan will be reviewed by another advisor.")
```

---

## Phase 3: Review Loop

### Step 3: Get Advisor Review

Spawn the **advisor agent** using the Task tool:

```
Task(subagent_type="crew:advisor", prompt="REVIEW THIS PLAN for clarity, completeness, and executability.

Plan file: [plan_file from state]

Evaluation criteria:
- **Clarity**: Can someone execute this without guessing?
- **Testability**: Are acceptance criteria concrete and measurable?
- **Completeness**: Are edge cases and dependencies addressed?
- **Context**: Does the plan explain WHY, not just WHAT?

## Verdict Format (REQUIRED)

You MUST use one of these verdicts:

**APPROVED** - Plan is ready for execution. No blocking issues.

**REVISE** - Issues need addressing. For EACH issue, classify as:
- [BLOCKING] - Must fix before execution (unclear steps, missing info, wrong approach)
- [MINOR] - Nice to have but not required (style, small clarifications, suggestions)

Example:
> VERDICT: REVISE
> - [BLOCKING] Step 3 doesn't specify which API endpoint to call
> - [BLOCKING] Missing error handling for network failures
> - [MINOR] Could add a diagram for clarity
> - [MINOR] Consider mentioning rollback strategy

**REJECT** - Fundamental problems requiring complete replanning.

Provide your verdict with detailed reasoning.")
```

### Step 4: Handle Verdict

| Verdict | Action |
|---------|--------|
| **APPROVED** | Complete the loop (see below) |
| **REVISE with any [BLOCKING]** | Update the plan to address blocking issues, then re-review |
| **REVISE with only [MINOR]** | Complete the loop — minor issues are acceptable |
| **REJECT** | Fundamentally rethink approach, create new plan, re-review |

## Completing the Loop

When advisor returns **APPROVED** (or REVISE with only [MINOR] issues):

1. **Deactivate the loop:**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate mt --reason "Advisor approved"
```

2. **Present the final plan to the user:**
   - Read the plan file
   - Summarize what was planned
   - Note how many iterations it took
   - Ask if they want to proceed with `/crew:execute`

## Exit Conditions

- Advisor returns **APPROVED**
- Advisor returns **REVISE** but ALL issues are marked [MINOR]
- Max iterations (10) reached (safety limit)
- User runs `/crew:cancel-measure-twice`

---

**If you provided a design doc:** I'll read it and proceed directly to plan generation and the review loop — no interview needed.

**Otherwise:** Start with Phase 1 — I'll ask 2-3 clarifying questions about your task. When you're ready, proceed to Phase 2 (plan generation) and Phase 3 (review loop).
