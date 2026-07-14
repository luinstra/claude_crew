---
description: Start a self-refining plan loop until a multi-model panel approves
argument-hint: "<task description or .md path>"
allowed-tools: Bash, Task, Read, Write, Glob
---

[MEASURE-TWICE MODE STARTING]

$ARGUMENTS

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models review the plan; the
rest is the task / design-doc path. Default (no flag) = **OMIT the
`--panel`/`--seats` flag entirely** and let `review-prep` apply the configured
default (precedence CLI flag > per-repo `.crew/config.toml` > global
`~/.crew-config.toml` > built-in **full**; no env tier). Do NOT hardcode `--panel
full` — it would override a user's configured default.

- `--panel full|lite|solo|cursor|quick|<custom>` — a named preset (`quick` = the
  cheapest cross-model pair; `<custom>` = any `[panels]` roster in per-repo/global
  config). `--seats <comma-list>` — an explicit subset of any registered seat
  (e.g. `--seats codex,opus`); `--seats` wins if both are given.
  Some registered seats are opt-in and not in the built-in default panel
  (premium cursor model-seats; the `fable` Claude voice): pass their exact names
  via `--seats`. The full seat census is `crew doctor` / the README panel table.
- **The engine resolves the preset — the orchestrator does NOT.** `review-prep`
  (Phase 3 Step 3a) resolves `--panel`/`--seats` into `subprocess_seats` (the
  external-CLI entries), `task_seats` (the Claude voices), and
  `task_seat_models` (each Task seat's model pin). The orchestrator never defines
  presets, classifies seat names, or hardcodes a model pin — pass the flag through
  and read the JSON. **Pass `--panel`/`--seats` ONLY when the user named a panel
  or seats** (an explicit flag, or intent like "use the lite panel"); otherwise
  OMIT it and let `review-prep` apply the configured/built-in default.

Below, **"the task"** means `$ARGUMENTS` with these panel flags removed — use it as
the requirements / design-doc path. The raw `$ARGUMENTS` (flags included) is
preserved byte-for-byte via the `-f` spill file below (Phase 2 Step 1) so the panel
survives across iterations. A one-seat panel is fine — synthesis / never-choke
handle any count down to one.

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

**Spill the task to a file and pass `-f` — NEVER inline.** Write the raw
`$ARGUMENTS` to `.crew/task-mt-<session-id>.txt` with the **Write tool** (which
writes the exact bytes — no shell is involved — and creates the `.crew/` parent
dir). A `--task "$ARGUMENTS"` value containing `$(…)`, `$VAR`, backticks, or `"`
would be expanded or mangled by the shell when the Bash tool runs the command, so
the task NEVER goes on the command line. Then init the loop, substituting your
actual session id (the `[Session ID: …]` value) for `<session-id>` as a literal
`--session-id` value, NEVER a `${CLAUDE_SESSION_ID}` shell expansion:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state init mt -f .crew/task-mt-<session-id>.txt --session-id <session-id> --auto-plan
```

Immediately after a successful init, remove the spill file (its own Bash call — no
chaining). The `session-start` L5 cleanup patterns do NOT cover these task files, so
this rm step is their sole owner:

```bash
rm -f .crew/task-mt-<session-id>.txt
```

Then get the state to find the plan file path:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state show mt --session-id <session-id>
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

Review the plan with a multi-model panel and synthesize the results into the
existing verdict. Two seat kinds:

- **subprocess seats** — the external-CLI entries of the resolved
  panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries, each a `crew:reviewer` spawned via
  the Task tool (in-session, on the subscription — no `claude -p`, no API key).

The panel is whatever the flags resolved to (the configured default panel:
`review-prep`'s JSON is the roster of record for this run); only fan out the
seats in that list. A failed/skipped seat NEVER aborts the review (see
"Synthesize" below) — the verdict
is synthesized from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the seats
> spawned below — seat tool access is governed per-seat by the reviewer agent
> frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.
> (`Write` stays in `allowed-tools` for the temp files that feed `persist-seat -f`
> and `repair-seat -f` — do not drop it.)

#### 3a — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep` (3a.0–3a.1) — it is the SOLE source of the
`task_seats`/`task_seat_models` 3b needs.** Skip ONLY the per-seat subprocess fan-out
(3a.2) when `subprocess_seats` is empty (e.g. `--panel lite`/`solo`) — go straight to
3b; you STILL persist the Task seats (3c) and run the SAME grouped `collect` (3c.5)
over the Task seats alone (there is no "synthesize from raw Task returns" path).
Otherwise fan the subprocess seats out over the plan file (`[plan_file from state]`)
**one `crew run` call per seat, in parallel** — each a separate, visible,
individually-killable shell (not one opaque `review` call hiding them in a thread
pool). Use the bare-script path (its `sys.path` guard resolves package imports).

**3a.0–3a.1 — prep the fan-out in ONE call.** `review-prep` resolves the plan-file
target, resolves `--panel`/`--seats` into the SUBPROCESS seat list (group tokens like
`cursor` expanded via the registry) AND the Task-seat split, stages the ONE shared
subprocess prompt (`--stage`, byte-identical to `render "[plan_file]" --mode review
--stage`), and PRINTS `{prompt_path, subprocess_seats, task_seats, task_seat_models}`
as one-line JSON. It runs NOTHING. **Quote `"[plan_file from state]"`** (a plan path
can contain spaces). Substitute your real id for `<session-id>` (the `[Session ID: …]`
value; never the literal placeholder or a `${…}` expansion):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep "[plan_file from state]" --session-id <session-id>
```

**OMIT `--panel`/`--seats` when the user named no panel** (as shown) — so
`review-prep` applies the configured `default_panel` or built-in **full**. Pass
`--panel <preset>` / `--seats <subset>` ONLY when the user chose one explicitly.
(No `--base` here: a plan-file target reviews the plan body and ignores the base ref,
so omitting it keeps `prompt_path` byte-identical to the historical `render
"[plan_file]" --stage` call.) Reference mode (the default) makes each seat read the
plan file itself; `--inline-diff` embeds it instead (forwarded to the staged prompt).

`review-prep` resolves BOTH seat kinds: 3b reads `task_seats` + `task_seat_models`
from this same JSON; the engine EXECUTES only the subprocess seats. **Parse the
one-line JSON** it prints (read it directly from stdout — it is NOT a shell `$(…)`
capture): use `subprocess_seats` to iterate the per-seat loop AND (JOINED
comma-separated) as part of the `collect --seats` list in 3c.5. The per-seat `run`
DERIVES its `-f` from `--session-id` (= `prompt_path`,
`.crew/reviews/<session-id>/prompt-seat.txt`), so you do NOT pass `prompt_path` as
`-f` yourself (see 3a.2). **If `subprocess_seats` is empty** (a Claude-only
`--panel lite`/`solo` → `prompt_path` is `""`, nothing staged), **SKIP only the 3a.2
per-seat loop** — go to 3b; you STILL persist the Task seats (3c) and run the SAME
grouped `collect` (3c.5) with `--seats <dot-stripped task_seats only>`.

**3a.2 — run EACH seat in its own parallel shell.** First, **clear any pre-existing
`<seat>.json`** for the seats you are about to run — the session dir
`.crew/reviews/<session-id>/` is REUSED across reviews, so a leftover file from an
EARLIER panel (or a seat whose shell you later kill) would be read by `collect` as a
FRESH result, folding stale data into this verdict. After clearing, a seat that
doesn't write a fresh file renders as SKIPPED, not stale-OK:

```bash
rm -f .crew/reviews/<session-id>/<seat>.json
```

Then, for every seat in `subprocess_seats`, launch a SEPARATE `crew run <seat>` Bash
call, all concurrently (e.g. background calls). `--session-id` lets `run` DERIVE both
paths from `.crew/reviews/<session-id>/`: `-f` = `prompt-seat.txt`, `-o` = `<seat>.json`:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> --session-id <session-id> --json
```

- `run --json` ALWAYS exits 0 and writes the six-field result
  (`name, model, ok, output, error, elapsed`) — a failed/skipped seat lands as
  `ok=False` with a diagnostic. **Per-seat never-choke is automatic**: one seat
  failing can't sink the others, and there's no all-failed abort to handle.
- Same model / sandbox / auth-banner / ARG_MAX handling as the old single-call path
  — `run` uses the identical provider machinery.

**3a.3 — do NOT collect yet.** The WHOLE panel — subprocess AND Task seats — flows
through ONE grouped `collect` in **Step 3c.5**, after the Task seats are persisted.
Launch the 3a.2 shells (in parallel) and move to 3b; Step 3c.5 owns the wait-for-both
barrier and the single collect.

#### 3b — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each entry in `task_seats`** (from the
`review-prep` JSON — skip if empty). Do NOT hand-write each seat's prompt: **stage it
from the engine** so every seat — subprocess AND Task — reviews ONE identical target.
`render` is the single prompt source and resolves the plan file the SAME way Step 3a's
engine `review` did, so the panel reviews a single identical plan. Stage ALL Task
seats in ONE `--stage-all` call over the SAME `[plan_file from state]` target, fed the
**comma-joined `task_seats`** from the prep JSON (NOT a hardcoded role list):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render "[plan_file from state]" --mode review --stage-all <comma-joined task_seats from the review-prep JSON> --session-id <session-id>
```

**Skip this `--stage-all` call entirely if `task_seats` is empty** (a
subprocess-only panel like `--panel cursor`). A Task seat name with a `.` (none in
the current roster) stages with the dot stripped from the filename — the same
derivation the spawn below reads, so the staged file and the read file never diverge.

`--stage-all` stages one LABELED prompt PER comma-listed role in a SINGLE call —
`.crew/reviews/<session-id>/prompt-<role>.txt` for each, each carrying its own
"acting as the **<role>** seat" label — and prints a JSON `{role: path}` map. It is
session-scoped so concurrent sessions never clobber each other. **Substitute your
actual session id for `<session-id>`** — the `[Session ID: …]` value (the same id
`crew state` uses). Pass it as a literal `--session-id` value — NOT the placeholder
text, and NOT a `${CLAUDE_SESSION_ID}` shell expansion (a `$…` expansion isn't
allowlistable and isn't reliably exported; the engine resolves the id itself, arg →
env). The engine **rejects an unsubstituted `<…>` placeholder** with a loud error, so
a missed substitution fails fast instead of silently sharing one dir. `.crew/` is
gitignored.

Then dispatch each seat **by reference** — the SAME agent (`crew:reviewer`) with a
per-spawn `model` override selecting the voice. **Iterate `task_seats` from the prep
JSON**; for each `<seat>` read its model from `task_seat_models[<seat>]`. Do NOT
hardcode seat names or model pins, and do NOT read + inline the staged prompt — point
the seat at its staged file and let it `Read` it (reference, not payload; the reviewer
has `Read`):

```
# for each <seat> in task_seats:
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read .crew/reviews/<session-id>/prompt-<seat, dot-stripped>.txt and follow it exactly.")
```

The staged prompt already states this is a **plan review**, tells the seat to read
the plan at `[plan_file from state]` itself (reference mode), lists the criteria
(clarity, testability, completeness, context), and asks for per-criterion PASS/FAIL +
`[BLOCKING]`/`[MINOR]` findings + a one-line verdict. `crew:reviewer` has `Read, Grep,
Glob, Bash` for exactly this. Because every seat's prompt comes from the one `render`
source over the same plan target, the panel reviews a single identical plan.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors, times
out, returns no usable block, or is reported missing/failed MUST NOT abort the review.
Catch it, normalize it to the six-field `ok=False` shape (3c), and continue. A failed
Task seat is treated identically to a failed subprocess seat.

#### 3c — Normalize Task-seat results to the six-field shape

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS the
> seat's final message as its tool result — that returned text IS the seat's review
> *and* the proof it finished. Take `ok`/`output`
> from that returned result. **NEVER judge a seat by a proxy** (output-file byte size,
> transcript length, a missed notification, elapsed time): those race the transcript
> flush and have falsely declared a finished seat "dead", discarding a good review. A
> seat that has not returned is **still running, not failed** — wait for it.

For each Task seat **in `task_seats`** (from the `review-prep` JSON — NOT a hardcoded
`opus`/`sonnet` pair), normalize its return into the SAME six fields as the subprocess
results:

- `name` = the seat's dot-stripped name (the same `[A-Za-z0-9_-]`-only slug
  `_seat_role_slug` in `cli.py` derives — the ONE canonical dot-strip source; for
  the current roster the slug is the name itself).
- `model` = the pinned model from `task_seat_models[<seat>]` (e.g. `opus`,
  `sonnet`, `fable`).
- `ok` = True if the seat's **returned result** is a usable review block; **False**
  only if it is an error, has no usable block, or the harness reports the Task
  failed/missing. (Judge from the returned result — never a proxy.)
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's review text, from its returned Task result.

A failed Task seat renders exactly like a failed subprocess seat. A skipped/unspawnable
seat renders a clearly-marked skipped block and is excluded from the verdict math.

**Persist each Task seat through the engine** (Decision-H). For EACH entry in
`task_seats` (from the prep JSON — never a hardcoded list), write the seat's
returned Task result to a temp file with the Write tool, then:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" persist-seat <seat> --session-id <session-id> --model "<task_seat_models[<seat>]>" -f <temp-file>
```

For a seat whose Task errored / returned no usable block, persist the failure
instead (never fabricate an ok result):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" persist-seat <seat> --session-id <session-id> --model "<task_seat_models[<seat>]>" --failed --error "<one-line diagnostic>"
```

The engine derives the dot-stripped filename and the six-field shape itself — the
printed path is the `<seat>.json` the 3c.5 collect reads. The seat entry you pass to
`collect --seats` is the seat's dot-stripped slug (the filename stem the engine
printed).

#### 3c.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every Step 3a.2
subprocess `crew run <seat>` shell to EXIT, AND (b) every Step 3b Task seat to return
AND be persisted in 3c. A seat whose `<seat>.json` is not written yet is STILL
RUNNING, not skipped — collecting early would silently drop it.

Define `<ran_seats>` = the comma-join of `subprocess_seats` (resolved prep order)
**then** the dot-stripped `task_seats`. In the Claude-only branch (`subprocess_seats`
empty) `<ran_seats>` is just the dot-stripped `task_seats`.

**Repair non-compliant seats (per-seat haiku reformat).** A seat that ignored the
structured `## FINDINGS` schema can't be grouped — give it ONE cheap repair pass. Ask
the engine which seats did not parse:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --report-unparsed
```

This prints the seat names (one per line) whose output ran but did NOT parse into
structured findings. For EACH such `<seat>`:

1. **Read** its raw `output` field — `.crew/reviews/<session-id>/<seat>.json`.
2. Spawn the cheap **formatter** agent to reformat that text into the schema (a
   FAITHFUL transform — do NOT invent, drop, or re-judge findings):

   ```
   Task(subagent_type="crew:formatter", model="haiku",
        prompt="Reformat this review into the FINDINGS schema verbatim — do not
   invent or drop findings, only reformat:\n\n<the seat's raw output field>")
   ```

3. Write the formatter's returned text into the seat file with the engine (it writes
   to a SEPARATE `repaired_output` field, NEVER overwriting the original `output`):

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/crew" repair-seat --seat .crew/reviews/<session-id>/<seat>.json -f <temp-file-with-reformatted-text>
   ```

`repair-seat` is **non-destructive**: it populates `repaired_output` ONLY IF the
reformatted text parses into the FINDINGS schema. If it STILL doesn't parse, the write
is a **no-op** — `repaired_output` is left unset and the command exits 0 with a stderr
kept-original note (a no-op is success; exit 2 = real usage/read error). Grouping uses
`repaired_output` when present, while `--full` and the RAW section ALWAYS render the
original `output`, so a degraded rewrite can never overwrite the seat's genuine review:
`collect` renders the ORIGINAL text raw (never worse than the seat's real words).
Continue — the file is correct either way. The formatter is an in-session `haiku` Task
seat — no `claude -p`, no API key.

**Collect ONCE — grouped + faithful:**

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --group -o .crew/reviews/<session-id>/panel.md --full .crew/reviews/<session-id>/panel-full.md
```

`--group` writes the deduped digest to `panel.md`; `--full` writes the faithful
per-seat concatenation to `panel-full.md` (the recovery artifact). **Read `panel.md`
once** and carry that ONE digest into 3d — do NOT also carry the raw Task-seat blocks
into context.

#### 3d — Synthesize the verdict

Read the ONE grouped `panel.md` digest — it already folds the WHOLE panel — and emit
the existing verdict format. Read all four sections: **VERDICTS** (roster of every
seat that ran + tally — reference seats by LABEL, never positionally), **CRITERIA
MATRIX** (per-criterion PASS/FAIL/`?`), **GROUPED FINDINGS** (each finding once with an
`M/N` agreement count where N = findings-parsed seats; a **`⚠ SINGLETON`** lone dissent
is weighed ON ITS MERITS, never dismissed for being one seat), and **RAW / UNPARSED
SEATS** (any seat that didn't parse even after repair, verbatim — **still read
these**). You MUST use one of these verdicts:

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
(subprocess OR Task) NEVER aborts the review and is NEVER silently dropped: synthesize
the verdict from the seats that produced usable output, and render each failed/skipped
seat's block with its diagnostic alongside the successful ones. ONLY when **zero**
seats produced usable output do you skip the verdict and instead report: `could not
review — all seats failed: <per-seat diagnostics>`.

### Step 4: Handle Verdict

| Verdict | Action |
|---------|--------|
| **APPROVED** | Complete the loop (see below) |
| **REVISE with any [BLOCKING]** | Update the plan to address blocking issues, then re-review |
| **REVISE with only [MINOR]** | Complete the loop — minor issues are acceptable |
| **REJECT** | Fundamentally rethink approach, create new plan, re-review |

## Completing the Loop

When the panel's verdict is **APPROVED** (or REVISE with only [MINOR] issues):

1. **Deactivate the loop** — pass the SAME `--session-id` the init used (the
   `[Session ID: …]` value, as a literal, NOT a `${CLAUDE_SESSION_ID}` shell
   expansion), so deactivate targets the exact session-scoped state file that init
   wrote (otherwise the scoped file stays active and keeps blocking Stop):
```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate mt --reason "Panel approved" --session-id <session-id>
```

2. **Present the final plan to the user:**
   - Read the plan file
   - Summarize what was planned
   - Note how many iterations it took
   - Ask if they want to proceed with `/crew:execute`

## Exit Conditions

- Panel verdict is **APPROVED**
- Panel verdict is **REVISE** but ALL issues are marked [MINOR]
- A hook-owned safety limit trips: the stop-fire cap or the wall-clock deadline
  (both force-exit the loop; see `scripts/CLAUDE.md`)
- User runs `/crew:cancel-measure-twice`

---

**If you provided a design doc:** I'll read it and proceed directly to plan generation and the review loop — no interview needed.

**Otherwise:** Start with Phase 1 — I'll ask 2-3 clarifying questions about your task. When you're ready, proceed to Phase 2 (plan generation) and Phase 3 (review loop).
