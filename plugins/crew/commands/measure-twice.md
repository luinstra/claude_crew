---
description: Start a self-refining plan loop until a multi-model panel approves
argument-hint: "<task description or .md path>"
allowed-tools: Bash, Task, Read, Glob
---

[MEASURE-TWICE MODE STARTING]

$ARGUMENTS

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models review the plan; the
rest is the task / design-doc path. Default (no flag) = the full panel — pass
`--panel full`.

- `--panel full|lite|solo|cursor` — a named preset. `--seats <comma-list>` — an
  explicit subset of any registered seat (e.g. `--seats codex,opus`); `--seats`
  wins if both are given. `cursor-gemini`, `cursor-glm`, and `cursor-gpt` are opt-in (add via `--seats`).
  Opt-in `opus-4.6` is a third Claude voice pinned to a version-locked model — add
  it explicitly (e.g. `--seats codex,opus,sonnet,opus-4.6`).
- **The engine resolves the preset — the orchestrator does NOT.** `review-prep`
  (Phase 3 Step 3a) takes the user's `--panel`/`--seats` and resolves it into
  `subprocess_seats` (the `codex`/`agy`/`cursor-*` entries),
  `task_seats` (the Claude voices), and `task_seat_models` (each Task seat's
  model pin). The orchestrator never defines presets, classifies seat names, or
  hardcodes a model pin — it passes the flag through and reads the JSON: Step 3a
  iterates `subprocess_seats`, Step 3b stages + spawns `task_seats`. Pass
  `--panel full` when the user gives no panel flag; otherwise pass the user's
  chosen `--panel <preset>` / `--seats <subset>`.

Below, **"the task"** means `$ARGUMENTS` with these panel flags removed — use it
as the requirements / design-doc path. Keep the raw `$ARGUMENTS` (flags included)
only in the `crew state … --task` so the panel survives across iterations. A
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
"${CLAUDE_PLUGIN_ROOT}/crew" state init mt --task "$ARGUMENTS" --auto-plan
```

Then get the state to find the plan file path:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state show mt
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

- **subprocess seats** — the `codex`/`agy`/`cursor-*` entries of
  the resolved panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries of the resolved panel, each a
  `crew:reviewer` spawned via the Task tool (in-session, on the subscription —
  no `claude -p`, no API key).

The panel is whatever the Panel-options flags resolved to (default **codex +
agy + cursor-auto + cursor-composer + opus + sonnet**). Only fan out the
seats in that list. A failed/skipped seat
NEVER aborts the review (see "Synthesize" below) — the verdict is synthesized
from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.

#### 3a — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep` (3a.0–3a.1 below) — it is the SOLE source of the
`task_seats`/`task_seat_models` 3b needs.** Skip ONLY the per-seat subprocess
fan-out (3a.2) + the `collect` (3a.3) when `subprocess_seats` is empty (e.g.
`--panel lite`/`solo`) — go straight to 3b and synthesize from the Task seats
alone. Otherwise fan the subprocess seats out over
the plan file (`[plan_file from state]`) **one `crew run` call per seat, in
parallel** — each a separate, visible, individually-killable shell (the per-seat
shape `/crew:debate`'s multi-round path uses), not one opaque `review` call hiding
them in an internal
thread pool. Use the bare-script path (its top-of-file `sys.path` guard makes
package imports resolve).

**3a.0–3a.1 — prep the fan-out in ONE call.** `review-prep` does the deterministic
prep — resolves the plan-file target, resolves `--panel`/`--seats` into the
SUBPROCESS seat list (group tokens like `cursor` expanded via the registry) AND the
Task-seat split, and stages the ONE shared subprocess prompt (`--stage`,
byte-identical to a standalone `render "[plan_file]" --mode review --stage`) — and
PRINTS `{prompt_path, subprocess_seats, task_seats, task_seat_models}` as one-line
JSON. It runs NOTHING (the per-seat loop below stays yours). **Quote
`"[plan_file from state]"`** (a plan path can contain spaces). Substitute your real
id for `<session-id>` (the `[Session ID: …]` value; never the literal placeholder or
a `${…}` expansion):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep "[plan_file from state]" --panel full --session-id <session-id>
```

Pass `--panel full` when the user gives no panel flag; otherwise pass the user's
chosen `--panel <preset>` / `--seats <subset>`.

(No `--base` here: a plan-file target reviews the plan body and ignores the base
ref, so omitting it matches the historical `render "[plan_file]" --stage` call and
keeps `prompt_path` byte-identical.) Reference mode (the default) makes each seat
read the plan file itself; add `--inline-diff` to embed it instead (forwarded to
the staged prompt).

`review-prep` resolves BOTH seat kinds: 3b reads `task_seats` +
`task_seat_models` from this same JSON (it no longer classifies Claude seats
itself), and the engine still EXECUTES only the subprocess seats. **Parse the
one-line JSON** it prints (read it directly
from the command's stdout — it is NOT a shell `$(…)` capture): use
`subprocess_seats` to iterate the per-seat loop AND (JOINED comma-separated) to
pass to `collect --seats` in 3a.3. The per-seat `run` DERIVES its `-f` from
`--session-id` (= `prompt_path`, `.crew/reviews/<session-id>/prompt-seat.txt`), so
you do NOT pass `prompt_path` as `-f` yourself (see 3a.2).
**If `subprocess_seats` is empty** (a Claude-only `--panel lite`/`solo` resolves to
no subprocess seats, so `prompt_path` is `""` and nothing was staged), **SKIP the
3a.2 per-seat loop AND the 3a.3 collect** entirely — go straight to 3b and
synthesize from the Task seats alone.

**3a.2 — run EACH seat in its own parallel shell.** First, **clear any pre-existing
`<seat>.json`** for the seats you are about to run — the session dir
`.crew/reviews/<session-id>/` is REUSED across reviews, so a leftover file from an
EARLIER panel (or from a seat whose shell you later kill) would otherwise be read
by `collect` as a FRESH result, folding stale data into this verdict. After
clearing, a seat that doesn't write a fresh file this run correctly renders as
SKIPPED, not stale-OK. Remove the expected target file for each seat first:

```bash
rm -f .crew/reviews/<session-id>/<seat>.json
```

Then, for every seat in `subprocess_seats` (from the 3a.0–3a.1 prep JSON), launch a
SEPARATE `crew run <seat>` Bash call, all concurrently (e.g. background calls).
Passing `--session-id <session-id>` lets `run` DERIVE both paths from
`.crew/reviews/<session-id>/`: `-f` = `prompt-seat.txt` (the `prompt_path` from
prep) and `-o` = `<seat>.json`:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> --session-id <session-id> --json
```

- `run --json` ALWAYS exits 0 and writes the six-field result
  (`name, model, ok, output, error, elapsed`) — a failed/skipped seat lands as
  `ok=False` with a diagnostic. **Per-seat never-choke is automatic**: one seat
  failing can't sink the others, and there's no all-failed abort to handle.
- Same model / sandbox / auth-banner / ARG_MAX handling as the old single-call
  path — `run` uses the identical provider machinery.

**3a.3 — collect.** WAIT for every 3a.2 `crew run <seat>` background shell to EXIT
before calling collect — a seat whose `<seat>.json` has not been written yet is
STILL RUNNING, not skipped (the same wait discipline as the Task seats in Step
3b). Because `collect` renders a missing file as a SKIPPED block, collecting early
would silently drop a slow-but-still-running seat from the verdict. Only after ALL
per-seat run shells have completed do you run collect.

Then collapse the per-seat result files into ONE markdown digest instead of
reading N `<seat>.json` files. You ALREADY have the resolved subprocess-seat list
as the `subprocess_seats` array from the 3a.0–3a.1 `review-prep` JSON (the SAME
list the per-seat loop iterated). **JOIN those seat names comma-separated** and
pass that SAME list as `collect --seats <comma-list>` so collect digests EXACTLY
the seats that ran:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <resolved-subprocess-seats> -o .crew/reviews/<session-id>/panel.md
```

where `<resolved-subprocess-seats>` is the comma-separated join of `subprocess_seats`
from the `review-prep` JSON. `collect` reads EXACTLY those named
`<seat>.json` files (never globs — a stale/foreign file is never folded in), and a
named-but-missing seat renders as a SKIPPED block labeled with its name. It writes
a faithful `render_panel` digest (it does NOT summarize, vote, or reorder) and
prints only the output path. Then **Read `panel.md` once** — it is the full
subprocess panel as labeled blocks — and carry it into 3d alongside the Task
seats. `panel.md` blocks follow the `--seats` ORDER; each is seat-labeled, so
reference seats by their LABEL ("the codex block", "the cursor-glm verdict"),
never positionally.

#### 3b — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each entry in `task_seats`** (from the
3a.0–3a.1 `review-prep` JSON — skip this step if `task_seats` is empty). Do
NOT hand-write each seat's prompt: **render it from the engine** so every seat —
subprocess AND Task — reviews ONE identical target. `render` is the single prompt
source, and it resolves the plan file the SAME way Step 3a's engine `review` did,
so the panel reviews a single identical plan. Render the Task seats' prompts over
the SAME `[plan_file from state]` target, staging ALL of them in ONE `--stage-all`
call fed the **comma-joined `task_seats`** from the prep JSON (NOT a hardcoded role
list):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render "[plan_file from state]" --mode review --stage-all <comma-joined task_seats from the review-prep JSON> --session-id <session-id>
```

**Skip this `--stage-all` call entirely if `task_seats` is empty** (a
subprocess-only panel like `--panel cursor`). A Task seat name with a `.` (e.g.
`opus-4.6`) stages with the dot stripped from the filename (`prompt-opus-46.txt`)
— the same derivation the spawn line below reads, so the staged file and the read
file can never diverge.

`--stage-all` stages one LABELED prompt PER comma-listed role in a SINGLE call —
`.crew/reviews/<session-id>/prompt-<role>.txt` for each, each carrying its own
"acting as the **<role>** seat" label — and prints a JSON `{role: path}` map to
stdout. It is session-scoped so concurrent sessions never clobber each other.
**Substitute your actual session id for `<session-id>`** — the `[Session ID: …]`
value from the SessionStart context (the same id `crew state` uses). Pass it as a
literal `--session-id` value — NOT the placeholder text, and NOT a
`${CLAUDE_SESSION_ID}` shell expansion (a `$…` expansion isn't allowlistable and
isn't reliably exported to the command shell; the engine resolves the id itself,
arg → env). The engine **rejects an unsubstituted `<…>` placeholder** with a loud
error, so a missed substitution fails fast instead of silently sharing one dir.
`.crew/` is gitignored. **Read** each path from the JSON map with the Read tool
and pass its contents to the matching seat.

Then dispatch each seat with the rendered text as its prompt — the SAME agent
(`crew:reviewer`) with a per-spawn `model` override selecting the voice. **Iterate
`task_seats` from the prep JSON**; for each `<seat>` read its model from
`task_seat_models[<seat>]` and its prompt from the staged
`prompt-<seat, dot-stripped>.txt` (e.g. `opus-4.6` → `prompt-opus-46.txt`). Do NOT
hardcode seat names or model pins — both come from the JSON:

```
# for each <seat> in task_seats:
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="<contents of .crew/reviews/<session-id>/prompt-<seat, dot-stripped>.txt>")
```

> A pinned `claude-opus-4-6` model (when `opus-4.6` is in `task_seat_models`) is
> checked against your org allowlist — it **silently falls back** to the inherited
> model if 4.6 isn't allowed.

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

Read the full panel — the subprocess seats come from the single `panel.md` collect
digest (Step 3a.3), joined with the normalized Task seats — and
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
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate mt --reason "Advisor approved"
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
