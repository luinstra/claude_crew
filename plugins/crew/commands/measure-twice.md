---
description: Start a self-refining plan loop until a multi-model panel approves
argument-hint: "<task description or .md path>"
allowed-tools: Bash, Task, Read, Glob
---

[MEASURE-TWICE MODE STARTING]

$ARGUMENTS

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models review the plan; the
rest is the task / design-doc path. Default (no flag) = the full panel.

- `--panel full` = `codex,agy,opus,sonnet` (default) · `--panel lite` =
  `opus,sonnet` · `--panel solo` = `opus`
- `--seats <list>` = an explicit comma-list from `codex,agy,opus,sonnet`
  (e.g. `--seats codex,opus`). `--seats` wins if both are given.

Resolve the flags to a **seat list** once, then split it:
- **subprocess seats** = the `codex`/`agy` entries → pass as the engine's
  `--seats` (Phase 3 Step 3a). **If the list has NO codex/agy, SKIP Step 3a.**
- **task seats** = the `opus`/`sonnet` entries → in Step 3b spawn `crew:reviewer`
  ONLY for those (zero, one, or both).

Below, **"the task"** means `$ARGUMENTS` with these panel flags removed — use it
as the requirements / design-doc path. Keep the raw `$ARGUMENTS` (flags included)
only in the `crew-state.py --task` so the panel survives across iterations. A
one-seat panel is fine — synthesis / never-choke handle any count down to one.

## How This Works

1. **Gather Requirements** - Interview user to understand the task (or skip if design doc provided)
2. **Generate Plan** - Advisor creates the plan
3. **Review** - The resolved multi-model panel evaluates (with BLOCKING/MINOR classification)
4. **Iterate** - If BLOCKING issues: revise and re-review. If REJECT: rethink approach.
5. **Complete** - When the panel's verdict is APPROVED (or only MINOR issues)

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

Focus on clarity and executability — this plan will be reviewed by a multi-model panel.")
```

---

## Phase 3: Review Loop

### Step 3: Get a Multi-Model Plan Review

Review the plan with a multi-model panel instead of a single advisor. The same
plan-review criteria fan out across several AI seats in parallel, and you
synthesize the results into the existing verdict. Two seat kinds:

- **subprocess seats** — the `codex`/`agy` entries of the resolved panel, via
  the Python engine.
- **task seats** — the `opus`/`sonnet` entries of the resolved panel, each a
  `crew:reviewer` spawned via the Task tool (in-session, on the subscription —
  no `claude -p`, no API key).

The panel is whatever the Panel-options flags resolved to (default **codex + agy
+ opus + sonnet**). Only fan out the seats in that list. A failed/skipped seat
NEVER aborts the review (see "Synthesize" below) — the verdict is synthesized
from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.

#### 3a — Fan out subprocess seats (one Bash call)

**Skip this step if the resolved panel has no `codex`/`agy` seat** (e.g.
`--panel lite`) — go straight to 3b. Otherwise run the engine once over the plan
file (`[plan_file from state]`), passing ONLY the resolved subprocess seats. Use
the bare-script path (its top-of-file `sys.path` guard makes package imports
resolve):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" review "[plan_file from state]" --seats <resolved codex/agy seats> --json
```

- The engine returns a JSON array of result objects, each with the six fields:
  `name, model, ok, output, error, elapsed`.
- **Never choke on a seat failure:** the engine NEVER raises out of the fan-out.
  Any failed/skipped subprocess seat comes back as an `ok=False` entry with a
  diagnostic in `error`; the other seats still return. The engine exits nonzero
  with an `all N seats failed: <diagnostics>` message on stderr ONLY when every
  subprocess seat failed — and even then emits the full JSON array (no
  traceback). A nonzero engine exit does NOT abort the review: fold in the Task
  seats and synthesize from whatever succeeded.

#### 3b — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each `opus`/`sonnet` entry in the
resolved panel** (zero, one, or both — skip this step if neither is present),
passing each a **criteria-equivalent** plan-review prompt to the engine's (same
clarity / testability / completeness / context criteria — NOT byte-identical).
Like the subprocess seats, the Task seats **read the plan file themselves** — do
NOT paste the plan body into the prompt. Tell each seat to read the plan at
`[plan_file from state]` in the repo it is already in. This keeps all seats
reviewing the SAME source by the SAME means — no embedded-vs-referenced
divergence. Each seat is the SAME agent (`crew:reviewer`) with a per-spawn
`model` override selecting the voice (spawn only the ones in the panel):

```
# Spawn ONLY the opus/sonnet seats in the resolved panel — e.g. --panel solo
# spawns just the opus line; --panel lite spawns both; full spawns both too.
Task(subagent_type="crew:reviewer", model="opus",   prompt="<the assembled plan-review prompt>")
Task(subagent_type="crew:reviewer", model="sonnet", prompt="<the assembled plan-review prompt>")
```

The assembled review prompt must state this is a **plan review**, tell the seat
to read the plan at `[plan_file from state]` itself, list the criteria (clarity,
testability, completeness, context), and ask for per-criterion PASS/FAIL +
`[BLOCKING]`/`[MINOR]` findings + a one-line verdict.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors,
times out, returns no usable block, or is reported missing/failed by the harness
MUST NOT abort the review. Catch it, normalize it to the six-field `ok=False`
shape (3c), and continue. A failed Task seat is treated identically to a failed
subprocess seat.

#### 3c — Normalize Task-seat results to the six-field shape

For each Task seat, normalize its return into the SAME six fields as the
subprocess results:

- `name` = `opus` / `sonnet` (the seat name).
- `model` = the pinned model (`opus` / `sonnet`).
- `ok` = True if the seat returned a usable review block; **False** if it
  returned no usable block, errored, or the harness reports it missing/failed.
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's review text.

A failed Task seat renders exactly like a failed subprocess seat. A
skipped/unspawnable seat renders a clearly-marked skipped block and is excluded
from the verdict math.

#### 3d — Synthesize the verdict

Read the full panel (subprocess + normalized Task seats, in stable order) and
emit the existing verdict format. You MUST use one of these verdicts:

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

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the review and is NEVER silently dropped:
synthesize the verdict from the seats that produced usable output, and render
each failed/skipped seat's block with its diagnostic alongside the successful
ones. ONLY when **zero** seats in the panel produced usable output do you skip
the verdict and instead report:
`could not review — all seats failed: <per-seat diagnostics>`.

### Step 4: Handle Verdict

| Verdict | Action |
|---------|--------|
| **APPROVED** | Complete the loop (see below) |
| **REVISE with any [BLOCKING]** | Update the plan to address blocking issues, then re-review |
| **REVISE with only [MINOR]** | Complete the loop — minor issues are acceptable |
| **REJECT** | Fundamentally rethink approach, create new plan, re-review |

## Completing the Loop

When the panel's verdict is **APPROVED** (or REVISE with only [MINOR] issues):

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

- Panel verdict is **APPROVED**
- Panel verdict is **REVISE** but ALL issues are marked [MINOR]
- Max iterations (10) reached (safety limit)
- User runs `/crew:cancel-measure-twice`

---

**If you provided a design doc:** I'll read it and proceed directly to plan generation and the review loop — no interview needed.

**Otherwise:** Start with Phase 1 — I'll ask 2-3 clarifying questions about your task. When you're ready, proceed to Phase 2 (plan generation) and Phase 3 (review loop).
