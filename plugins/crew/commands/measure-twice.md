---
description: Start a self-refining plan loop that runs until a completing verdict (APPROVED or REVISE --minor-only, or a human --force over an advisory)
argument-hint: "<task description or .md path>"
allowed-tools: Bash, Task, Read, Write, Glob
---

[MEASURE-TWICE MODE STARTING]

$ARGUMENTS

## Conventions (stated once, apply to every step)

- **Session id is a literal.** Substitute your actual session id (the
  `[Session ID: …]` value) for `<session-id>` in every recipe; never the
  placeholder, never a `${CLAUDE_SESSION_ID}` expansion.
- **No shell expansions in recipes.** No `${…}`, `$(…)`, or backticks on any
  Bash line (they defeat permission allowlisting); the `${CLAUDE_PLUGIN_ROOT}`
  prefix is exempt (the harness substitutes it). The ENGINE anchors a relative
  `.crew/...` path arg to the project root (`crew_base()`), never the shell
  cwd, so recipes carry plain relative paths. Double quotes tame spaces and
  globs in a substituted value; they do NOT neutralize `$(…)`/backticks, hence
  the next rule.
- **Spill task text, never inline it.** A `--task "$ARGUMENTS"` value with
  `$(…)`, `$VAR`, backticks, or `"` would be expanded or mangled by the shell:
  Write the exact bytes to a file and pass `-f`. Write-tool and Read-tool
  paths are absolute literals (`<project-root>/...`, your `CLAUDE_PROJECT_DIR`
  value substituted); those tools take no shell.
- **`<session_segment>`, never the raw id, in reconstructed run-dir paths**:
  it is the prep JSON's sanitized dir segment the engine actually built the
  run dir from; a raw id can diverge from it.
- **Reference, not payload.** Point a seat at a file to `Read`; never paste
  file contents into a prompt.
- The engine enforces and tests the mechanics these recipes rely on
  (run-scoping, preserve-valid, anchoring, quorum recount): `scripts/CLAUDE.md`.

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models review the plan;
the rest is the task / design-doc path.

- `--panel full|lite|solo|cursor|quick|<custom>` = a named preset (`<custom>`
  = any `[panels]` roster in config). `--seats <comma-list>` = an explicit
  subset of any registered seat (e.g. `--seats codex,opus`); it wins if both
  are given.
  Some registered seats are opt-in and not in the built-in default panel
  (premium cursor model-seats; the `fable` Claude voice): pass their exact
  names via `--seats`. Census: `crew doctor` / the README panel table.
- **Pass `--panel`/`--seats` ONLY when the user named a panel or seats**;
  otherwise OMIT the flag so `review-prep` applies the configured default (CLI
  flag > per-repo `.crew/config.toml` > global `~/.crew-config.toml` >
  built-in `full`). Never hardcode `--panel full`.
- **The engine resolves the preset, the orchestrator does not**: `review-prep`
  (Step 3a) splits the flags into `subprocess_seats`/`task_seats`/
  `task_seat_models`; pass the flag through and read the JSON.

**"The task"** below = `$ARGUMENTS` minus these panel flags. The raw
`$ARGUMENTS` is preserved byte-for-byte via the `-f` spill (Phase 2 Step 1) so
the panel choice survives across iterations. A one-seat panel is fine.

## How This Works

1. **Gather Requirements** - interview (skipped when a design doc is given)
2. **Generate Plan** - advisor creates the plan
3. **Review** - the resolved multi-model panel evaluates
4. **Iterate** - BLOCKING issues: revise and re-review; REJECT: rethink
5. **Complete** - on APPROVED / only-MINOR with quorum met, or a
   human-authorized `--force` over an advisory

---

## Check: Is a Design Doc Referenced?

**If `$ARGUMENTS` contains a file path** (e.g. `docs/plans/foo-design.md` or
any `.md` path): skip Phase 1 and go directly to Phase 2 with that document as
the requirements source. Read it first: resolve a RELATIVE path against the
project root (`<project-root>/.crew/plans/bar.md`, per Conventions), never a
bare cwd-relative path a divergent cwd would resolve against the wrong tree;
an ABSOLUTE path is read as-is. Do NOT re-interview the user.

---

## Phase 1: Requirements Gathering (You)

**Skip this phase if a design doc was referenced above.**

Interview the user: 1. **Goal**, 2. **Constraints** (timeline, tech stack,
patterns to follow), 3. **Scope** (in/out), 4. **Concerns** (risks, edge
cases). Ask 2-3 clarifying questions; get just enough context to brief the
advisor.

---

## Phase 2: Plan Generation

When the user says "create the plan" or equivalent, **or if a design doc was
referenced**:

### Step 1: Activate the Loop

Write the raw `$ARGUMENTS` to `<project-root>/.crew/task-mt-<session-id>.txt`
with the **Write tool** (exact bytes; creates `.crew/`), then init the loop.
`--consume` deletes the spill after a successful init (a failed init leaves it
for retry):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state init mt -f ".crew/task-mt-<session-id>.txt" --session-id <session-id> --auto-plan --consume
```

Then get the state to find the plan file path:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state show mt --session-id <session-id>
```

### Step 2: Generate Initial Plan

Summarize what you learned, then spawn the **advisor agent**:

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

Review the plan with a multi-model panel. Two seat kinds: **subprocess seats**
(external-CLI entries, via the engine) and **task seats** (Claude voices, each
a `crew:reviewer` spawned via the Task tool, in-session on the subscription;
no `claude -p`, no API key). `review-prep`'s JSON is the roster of record;
only fan out those seats.

> **`allowed-tools` scopes THIS orchestrator only.** Seat tool access is
> governed per-seat by the reviewer agent frontmatter and the engine's sandbox
> flags. (`Write` stays here for the temp files feeding `persist-seat -f` and
> `repair-seat -f`.)

#### 3a — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep`: it is the SOLE source of the
`task_seats`/`task_seat_models` 3b needs.** When `subprocess_seats` is empty
(e.g. `--panel lite`/`solo`; `prompt_path` prints `""`), skip ONLY the
per-seat fan-out (3a.2): you STILL persist the Task seats (3c) and run the
SAME grouped `collect` (3c.5) with `--seats <task_seats only>`; there is no
"synthesize from raw Task returns" path.

**3a.0-3a.1: prep the fan-out in ONE call.** `review-prep` resolves the
plan-file target and the panel split (group tokens expanded via the registry),
mints the RUN-SCOPED review dir (`run.json` + a frozen snapshot of the plan
body), stages every seat prompt against that snapshot, and PRINTS
`{prompt_path, subprocess_seats, task_seats, task_seat_models, run_dir,
run_id, target_sha256, session_segment, task_prompt_paths,
pending_subprocess_seats, pending_task_seats}` as one-line JSON. It runs
NOTHING. **Quote `"[plan_file from state]"`** (a plan path can contain
spaces). (`--inline-diff` embeds content; reference mode is the default. No
`--base`: a plan-file target ignores it.)

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep "[plan_file from state]" --session-id <session-id>
```

OMIT `--panel`/`--seats` unless the user chose one. **Parse the one-line JSON
directly from stdout** (never a `$(…)` capture): `pending_subprocess_seats`
drives the per-seat loop, `run_id` rides every engine call below, the FULL
`subprocess_seats` joins `collect --seats` (3c.5). Use the printed `run_dir`
VERBATIM only in **Write-tool** and **Task `Read`** references; a **Bash
recipe** never pastes it and instead reconstructs the relative
`.crew/reviews/<session_segment>/<run_id from the prep JSON>/...`.

**3a.1b: freeze the run identity into loop state**, immediately after prep and
BEFORE any seat runs (skip it and the verdict is unrecordable):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state begin-review mt --session-id <session-id>
```

It freezes the run id, the plan's hash + spec, and the roster (what
`record-verdict` judges against) and prints `phase=reviewing run=<run_id>
seats=<N>`. Re-running after a re-prep is fine. **Leave the plan file
untouched until the verdict is recorded**: `record-verdict` re-hashes it, and
drifted bytes raise a completion advisory (exit 3, Step 4) that only the
user's `--force` overrides.

**3a.2: run EACH pending seat in its own parallel shell.** Do NOT delete seat
files: results are RUN-SCOPED (a revised plan mints a NEW run dir, so review
rounds never fold together; an unchanged plan RESUMES its dir, landed seats
dropping out of the pending lists; a landed valid result is never overwritten
by a fresh failure). For every seat in `pending_subprocess_seats` (NOT the
full roster), launch a SEPARATE `crew run <seat>` Bash call, all concurrently
(e.g. background calls); `run` DERIVES `-f`/`-o` from the run dir, so pass no
paths:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --json
```

`run --json` exits 0 for every result it WRITES, the six-field shape
(`name, model, ok, output, error, elapsed`); a failed/skipped seat lands as
`ok=False` with a diagnostic, so per-seat never-choke is automatic. Run-scoped
MISUSE (non-member seat, prompt/model/sandbox override) exits 2 with nothing
written; the templated call above never hits that path.

**3a.3: do NOT collect yet.** The WHOLE panel flows through ONE grouped
`collect` in **3c.5**, after the Task seats are persisted. Launch the 3a.2
shells and move to 3b.

#### 3b — Fan out Task seats (parallel)

Spawn a reviewer **in parallel for each entry in `pending_task_seats`** (skip
if empty). Do NOT hand-write or re-render any prompt: `review-prep` ALREADY
STAGED one per Task seat against the FROZEN snapshot of the plan
(`task_prompt_paths`); a re-render would re-resolve the LIVE plan file and
reopen the drift window. Dispatch **by reference** (the reviewer has `Read`),
pinning `model` from `task_seat_models[<seat>]`; never hardcode seat names or
model pins, never inline the staged prompt:

```
# for each <seat> in pending_task_seats (path = task_prompt_paths[<seat>]):
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read <run_dir>/prompt-<seat>.txt and follow it exactly.")
```

The staged prompt already frames the plan review (frozen snapshot as review
authority, criteria, per-criterion PASS/FAIL + `[BLOCKING]`/`[MINOR]` findings
+ one-line verdict). `.crew/` is gitignored. **Never choke on a Task-seat
failure:** a spawn that errors, times out, or returns no usable block MUST NOT
abort the review; normalize it to `ok=False` (3c) and continue.

#### 3c — Normalize Task-seat results to the six-field shape

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS
> the seat's final message as its tool result; that returned text IS the
> seat's review *and* the proof it finished. Take `ok`/`output` from it.
> **NEVER judge a seat by a proxy** (output-file byte size, transcript length,
> a missed notification, elapsed time): those race the transcript flush and
> have falsely declared a finished seat "dead", discarding a good review. A
> seat that has not returned is **still running, not failed**: wait for it.

Normalize each spawned seat's return into the SAME six fields as the
subprocess results: `name` = the seat name (its own filename stem), `model` =
`task_seat_models[<seat>]`, `ok` = True only if the returned result is a
usable review block, `error` = a populated diagnostic when `ok=False` (never a
fabricated `ok=True`), `elapsed` = best-effort, `output` = the review text. A
failed Task seat renders exactly like a failed subprocess seat.

**Persist each Task seat through the engine.** Write each returned result to
`<project-root>/.crew/reviews/<session_segment>/tmp-seat-<seat>.md` (Write
tool; NEVER a system temp or scratchpad dir, which sits outside the approved
working dirs and fires a permission prompt), then persist with the prep's
`--run-id` (required: without it a late seat exits 2 rather than risk landing
in a newer run's dir):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" persist-seat <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --model "<task_seat_models[<seat>]>" -f ".crew/reviews/<session_segment>/tmp-seat-<seat>.md"
```

For a seat whose Task errored / returned no usable block, persist the failure
(never fabricate an ok result); a skipped/unspawnable seat renders as a labeled
SKIPPED block and is excluded from the verdict math:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" persist-seat <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --model "<task_seat_models[<seat>]>" --failed --error "<one-line diagnostic>"
```

The engine derives the filename and shape; the printed path is the
`<seat>.json` the 3c.5 collect reads.

#### 3c.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every 3a.2
shell to EXIT, AND (b) every Task seat to return AND be persisted. A seat
whose `<seat>.json` is not written yet is STILL RUNNING, not skipped. For the
subprocess half:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" wait --session-id <session-id> --run-id <run_id from the prep JSON> --timeout 550
```

`wait` exits 0 once every subprocess seat file has landed (ok and failed
alike: failed = finished); on timeout it exits 1 naming the missing seats.
`--timeout 550` stays under the Bash tool's own budget (fits a call raised to
600000ms; `wait`'s 900s default would be harness-killed first). `wait` refuses
a Task seat by name: you persist those yourself, so waiting on one would
deadlock. Wait for your own Task returns, persist, THEN collect.

`<ran_seats>` = comma-join of the FULL `subprocess_seats` (prep order) then
the FULL `task_seats`: the whole roster, not only pending (a landed seat from
a resumed run is part of this verdict). Claude-only branch: just `task_seats`.

**Repair non-compliant seats (per-seat haiku reformat).** A seat that ignored
the `## FINDINGS` schema can't be grouped; give it ONE cheap repair pass:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --run-id <run_id from the prep JSON> --seats <ran_seats> --report-unparsed
```

For EACH seat name it prints:

1. **Read** the raw `output` field of `<run_dir>/<seat>.json`.
2. Spawn the formatter (a FAITHFUL transform; never invent, drop, or re-judge):

   ```
   Task(subagent_type="crew:formatter", model="haiku",
        prompt="Reformat this review into the FINDINGS schema verbatim — do not
   invent or drop findings, only reformat:\n\n<the seat's raw output field>")
   ```

3. Write the formatter's return to
   `<project-root>/.crew/reviews/<session_segment>/tmp-repair-<seat>.md`
   (Write tool; never a system temp or scratchpad dir), then (seat NAME
   positional; the engine derives the seat file from the run scope):

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/crew" repair-seat <seat> --session-id <session-id> --run-id <run_id from the prep JSON> -f ".crew/reviews/<session_segment>/tmp-repair-<seat>.md"
   ```

`repair-seat` is **non-destructive**: it fills a SEPARATE `repaired_output`
field ONLY IF the reformat parses; otherwise a kept-original no-op (exit 0,
stderr note). `--full` and the RAW section always render the original
`output`, so a degraded rewrite never replaces the seat's real words. Continue
either way.

**Collect ONCE — grouped + faithful:**

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --run-id <run_id from the prep JSON> --seats <ran_seats> --group -o ".crew/reviews/<session_segment>/<run_id from the prep JSON>/panel.md" --full ".crew/reviews/<session_segment>/<run_id from the prep JSON>/panel-full.md"
```

`--group` writes the deduped digest (`panel.md`); `--full` the faithful
per-seat concatenation (`panel-full.md`, the recovery artifact). **Read
`panel.md` once** and carry that ONE digest into 3d; do NOT also carry raw
Task-seat blocks into context.

#### 3d — Synthesize the verdict

Read all four `panel.md` sections: **VERDICTS** (roster + tally; reference
seats by LABEL, never positionally), **CRITERIA MATRIX**, **GROUPED FINDINGS**
(each finding once, `M/N` agreement count; a **`⚠ SINGLETON`** lone dissent is
weighed ON ITS MERITS, never dismissed for being one seat), and **RAW /
UNPARSED SEATS** (**still read these**). You MUST use one of these verdicts:

**APPROVED** - Plan is ready for execution. No blocking issues.

**REVISE** - Issues need addressing. For EACH issue, classify as:
- [BLOCKING] - Must fix before execution (unclear steps, missing info, wrong approach)
- [MINOR] - Nice to have but not required (style, small clarifications, suggestions)

**REJECT** - Fundamental problems requiring complete replanning.

**The quorum header is advisory.** The digest opens with a `PANEL:` header; a
completing verdict expects MET or absent (a flat/legacy digest carries no
quorum gate). On NOT MET a completing `record-verdict` exits 3 rather than
certify: report `could not verify quorum without the user's explicit --force:
<usable>/<N> usable (threshold from the header)`, name the pending/failed
seats, and relaunch pending seats (same `--run-id`), surface the shortfall, or
complete with `--force` ONLY on the user's explicit say-so. Never-choke is
unchanged; zero usable seats is the all-failed branch below.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
NEVER aborts the review and is NEVER silently dropped: render each failure's
diagnostic alongside the successful seats. ONLY when **zero** seats produced
usable output, skip the verdict and report: `could not review — all seats
failed: <per-seat diagnostics>`.

### Step 4: Handle Verdict

| Verdict | Action |
|---------|--------|
| **APPROVED** (quorum MET) | `record-verdict mt APPROVED`, then complete the loop (see below) |
| **REVISE with any [BLOCKING]** | `record-verdict mt REVISE`, then update the plan to address blocking issues, then re-review |
| **REVISE with only [MINOR]** (quorum MET) | `record-verdict mt REVISE --minor-only`, then complete the loop |
| **REJECT** | `record-verdict mt REJECT`, then fundamentally rethink approach, create new plan, re-review |
| **Quorum NOT MET**, or any completion advisory (plan drift, pointer divergence) | A completing `record-verdict` exits 3 and records NOTHING: the loop CANNOT certify WITHOUT the user's explicit `--force`. Surface the advisory; relaunch pending seats (same `--run-id`), or complete with `--force` ONLY on the user's say-so |
| **Zero usable seats** (all-failed branch of 3d) | `record-verdict mt FAILED`, then report the per-seat diagnostics |

**Record the verdict through the engine before acting on it** (an unrecorded
verdict leaves the loop unfinishable):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state record-verdict mt <APPROVED|REVISE|REJECT|FAILED> [--minor-only] --session-id <session-id>
```

`--minor-only` completes the loop, so it faces the same checks APPROVED does.
The engine re-checks this table's conditions itself and exits:

- **Exit 0**: recorded (a completing verdict printed `phase=done`): complete
  the loop.
- **Exit 3**: a completion advisory tripped; NOTHING recorded, every tripped
  condition printed. **Do NOT re-run with `--force` on your own.** Surface the
  advisory, state that `--force` would record over it, WAIT for the user. Only
  on their say-so re-run the SAME call with `--force` (records and stamps
  `last_verdict_overrides`). **`--force` is the HUMAN's authorization, carried
  out by you; you never originate it.**
- **Exit 2**: a hard error (wrong phase, no run identity, FAILED over a usable
  panel): a real contradiction to FIX, never forced.

REVISE and REJECT advance `revision_round`, the loop's only honest iteration
count. FAILED is only for a panel that returned nothing usable; a SECOND
consecutive FAILED ends the loop itself (stamps it inactive; no deactivate
follows).

**On REVISE, diagnose the CLASS before revising.** When several findings share
one structural cause, the revision must name it and change the STRUCTURE once,
re-deriving the findings from it; patching each finding where it was reported
mints new findings every round. A round whose finding count goes UP on a
more-specified plan usually means the review is polishing prose, not design:
SURFACE that trajectory to the user and ask whether to cap the loop (never a
self-granted exit; a cap still requires every open BLOCKING fixed or moved to
a user-approved deferred list). A lone dissenting BLOCKING is high-signal but
must be VERIFIED before it forces a revision.

## Completing the Loop

Step 4 already recorded the verdict (the ONLY place it is recorded;
`record-verdict` refuses from `phase=done`), and `phase=done` is what the
deactivate below requires: a loop with no completing verdict on record is
refused there.

1. **Deactivate the loop**, passing the SAME `--session-id` the init used
   (otherwise the scoped state file stays active and keeps blocking Stop):
```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate mt --reason "Panel approved" --session-id <session-id>
```
   When the completion required `--force`, the `--reason` must name that:
   `--reason "Completion forced over advisories"` (matching the Stop-hook
   nudge); a clean completion keeps `"Panel approved"`.

2. **Present the final plan to the user:** read the plan file, summarize it,
   note the loop's cost against its budget (stop fires and elapsed vs the
   deadline, as `crew state show mt` reports; the only honest iteration count
   is `revision_round`), and ask if they want to proceed with `/crew:execute`.

## Exit Conditions

- **APPROVED**, quorum MET or absent (or recorded on the user's explicit
  `--force` over a NOT MET header)
- **REVISE** with ALL issues [MINOR], same quorum rule
- A **SECOND consecutive `FAILED`** verdict: terminal in-place deactivation
  (`exit_kind=review_failed`); a single FAILED does not end it
- A hook-owned safety limit trips (stop-fire cap or wall-clock deadline; see
  `scripts/CLAUDE.md`)
- User runs `/crew:cancel-measure-twice`

---

**If you provided a design doc:** I'll read it and proceed directly to plan
generation and the review loop — no interview needed.

**Otherwise:** Start with Phase 1 — I'll ask 2-3 clarifying questions about
your task. When you're ready, proceed to Phase 2 (plan generation) and Phase 3
(review loop).
