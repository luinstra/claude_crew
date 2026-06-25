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

- `--panel full` = `codex,cursor-gemini,cursor-glm,cursor-composer,opus,sonnet`
  (default) · `--panel lite` = `opus,sonnet` · `--panel solo` = `opus`
- `--panel cursor` = all Cursor model-seats (`--seats cursor`, which the engine
  expands to every registered cursor-* seat — cursor-gpt, cursor-gemini,
  cursor-glm, cursor-composer, and any future ones); a pure cross-model Cursor
  panel — NO codex, NO opus/sonnet Task seats.
- `--seats <list>` = an explicit comma-list of any registered seat
  (`codex, agy, cursor-gpt, cursor-gemini, cursor-glm, cursor-composer, opus, sonnet`;
  e.g. `--seats codex,opus`). `agy` is opt-in — works via `--seats agy` but is
  not in the default panel. `--seats` wins if both are given.
- **Opt-in `opus-4.6` Claude seat** — a third Claude voice pinned to
  `claude-opus-4-6` (some prefer 4.6 over 4.7/4.8). NOT in any default; add it
  explicitly (e.g. `--seats codex,opus,sonnet,opus-4.6`). A Task seat like
  `opus`/`sonnet`, just version-pinned.

Resolve the flags to a **seat list** once, then split it:
- **subprocess seats** = the `codex`/`cursor-*` entries (and opt-in `agy`) → pass
  as the engine's `--seats` (Phase 3 Step 3a). **If the list has NO subprocess
  seat, SKIP Step 3a.** For `--panel cursor` the subprocess seats are all cursor-*
  (pass `--seats cursor` to the engine, which expands it to every cursor-* seat).
- **task seats** = the `opus`/`sonnet`/`opus-4.6` entries → in Step 3b spawn
  `crew:reviewer` ONLY for those. For `--panel cursor` there are NO Claude Task
  seats, so SKIP Step 3b entirely.

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

- **subprocess seats** — the `codex`/`cursor-*` entries (and opt-in `agy`) of
  the resolved panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries of the resolved panel, each a
  `crew:reviewer` spawned via the Task tool (in-session, on the subscription —
  no `claude -p`, no API key).

The panel is whatever the Panel-options flags resolved to (default **codex +
cursor-gemini + cursor-glm + cursor-composer + opus + sonnet**). Only fan out the
seats in that list. A failed/skipped seat
NEVER aborts the review (see "Synthesize" below) — the verdict is synthesized
from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.

#### 3a — Fan out subprocess seats (one visible shell PER SEAT)

**Skip this step if the resolved panel has no subprocess seat** (e.g.
`--panel lite`) — go straight to 3b. Otherwise fan the subprocess seats out over
the plan file (`[plan_file from state]`) **one `cli.py run` call per seat, in
parallel** — each a separate, visible, individually-killable shell (the per-seat
shape `/crew:debate`'s multi-round path uses), not one opaque `review` call hiding
them in an internal
thread pool. Use the bare-script path (its top-of-file `sys.path` guard makes
package imports resolve).

**3a.0 — get the concrete seat list** (expands group tokens like `cursor`; keeps
the registry authoritative — no hardcoded cursor list here):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" seats --seats <resolved codex/cursor-* seats>
```

It prints one seat per line. (For `--panel cursor`, pass `--seats cursor`.)

**3a.1 — render the shared subprocess prompt ONCE** over the plan via `--stage`
(session-scoped — substitute your real id for `<session-id>`, the `[Session ID: …]`
value; never the literal placeholder or a `${…}` expansion):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render "[plan_file from state]" --mode review --stage --session-id <session-id>
```

It prints the staged path — `.crew/reviews/<session-id>/prompt-seat.txt`. Reference
mode (the default) makes each seat read the plan file itself.

**3a.2 — run EACH seat in its own parallel shell.** For every seat from 3a.0, launch
a SEPARATE `cli.py run <seat>` Bash call, all concurrently (e.g. background calls):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" run <seat> -f .crew/reviews/<session-id>/prompt-seat.txt --json -o .crew/reviews/<session-id>/<seat>.json
```

- `run --json` ALWAYS exits 0 and writes the six-field result
  (`name, model, ok, output, error, elapsed`) — a failed/skipped seat lands as
  `ok=False` with a diagnostic. **Per-seat never-choke is automatic**: one seat
  failing can't sink the others, and there's no all-failed abort to handle.
- Same model / sandbox / auth-banner / ARG_MAX handling as the old single-call
  path — `run` uses the identical provider machinery.

**3a.3 — collect.** Read each `.crew/reviews/<session-id>/<seat>.json` (one
six-field result apiece) and carry them into 3d alongside the Task seats.

#### 3b — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each Claude-seat entry (`opus`, `sonnet`,
or opt-in `opus-4.6`) in the resolved panel** (skip this step if none present). Do
NOT hand-write each seat's prompt: **render it from the engine** so every seat —
subprocess AND Task — reviews ONE identical target. `render` is the single prompt
source, and it resolves the plan file the SAME way Step 3a's engine `review` did,
so the panel reviews a single identical plan. Render each Claude seat's
prompt over the SAME `[plan_file from state]` target:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render "[plan_file from state]" --mode review --seat-role opus   --stage --session-id <session-id>
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render "[plan_file from state]" --mode review --seat-role sonnet --stage --session-id <session-id>
# opt-in — only if opus-4.6 is in the panel (staged as prompt-opus-46.txt):
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render "[plan_file from state]" --mode review --seat-role opus-4.6 --stage --session-id <session-id>
```

`--stage` derives `.crew/reviews/<session-id>/prompt-<seat-role>.txt` from the
`--seat-role` + session id, writes it (creating the dir), and prints the path —
session-scoped so concurrent sessions never clobber each other. **Substitute your
actual session id for `<session-id>`** — the `[Session ID: …]` value from the
SessionStart context (the same id `crew-state.py` uses). Pass it as a literal
`--session-id` value — NOT the placeholder text, and NOT a `${CLAUDE_SESSION_ID}`
shell expansion (a `$…` expansion isn't allowlistable and isn't reliably exported
to the command shell; the engine resolves the id itself, arg → env). The engine
**rejects an unsubstituted `<…>` placeholder** with a loud error, so a missed
substitution fails fast instead of silently sharing one dir. `.crew/` is
gitignored. **Read** each staged file with the Read tool and pass its contents to
the matching seat.

Then dispatch each seat with the rendered text as its prompt — the SAME agent
(`crew:reviewer`) with a per-spawn `model` override selecting the voice (spawn
only the ones in the panel):

```
# Spawn ONLY the Claude seats in the resolved panel — e.g. --panel solo spawns
# just the opus line; --panel lite spawns opus+sonnet; opt-in adds opus-4.6.
# opt-in opus-4.6: Task(subagent_type="crew:reviewer", model="claude-opus-4-6", prompt="<contents of .crew/reviews/<session-id>/prompt-opus-46.txt>")
# (model="claude-opus-4-6" silently falls back to the inherited model if 4.6 isn't on your org allowlist.)
Task(subagent_type="crew:reviewer", model="opus",   prompt="<contents of .crew/reviews/<session-id>/prompt-opus.txt>")
Task(subagent_type="crew:reviewer", model="sonnet", prompt="<contents of .crew/reviews/<session-id>/prompt-sonnet.txt>")
```

The rendered prompt already states this is a **plan review**, tells the seat to
read the plan at `[plan_file from state]` itself (reference mode), lists the
criteria (clarity, testability, completeness, context), and asks for
per-criterion PASS/FAIL + `[BLOCKING]`/`[MINOR]` findings + a one-line verdict.
Because every seat's prompt comes from the one `render` source over the same plan
target, the panel reviews a single identical plan — no embedded-vs-referenced
divergence.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors,
times out, returns no usable block, or is reported missing/failed by the harness
MUST NOT abort the review. Catch it, normalize it to the six-field `ok=False`
shape (3c), and continue. A failed Task seat is treated identically to a failed
subprocess seat.

#### 3c — Normalize Task-seat results to the six-field shape

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS the
> seat's final message as its tool result — that returned text IS the seat's
> review *and* the proof it finished. Take `ok`/`output` from that returned
> result. **NEVER judge a seat by a proxy** (output-file byte size, transcript
> length, a missed notification, elapsed time): those race the transcript flush
> and have falsely declared a finished seat "dead", discarding a good review. A
> seat that has not returned is **still running, not failed** — wait for it.

For each Task seat, normalize its return into the SAME six fields as the
subprocess results:

- `name` = `opus` / `sonnet` (the seat name).
- `model` = the pinned model (`opus` / `sonnet`).
- `ok` = True if the seat's **returned result** is a usable review block;
  **False** only if that result is an error, has no usable block, or the harness
  reports the Task failed/missing. (Judge from the returned result — never a proxy.)
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's review text, taken from its returned Task result.

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
