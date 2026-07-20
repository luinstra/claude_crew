---
description: Multi-model review of a plan OR code diff (runs the configured default panel; narrow with --panel/--seats)
argument-hint: "<the plan | the code | a .md path | a git scope>"
allowed-tools: Bash, Task, Read, Write, Glob
---

[MULTI-MODEL REVIEW]

$ARGUMENTS

## Conventions (stated once, apply to every step)

- **Session id is a literal.** Substitute your actual session id (the
  `[Session ID: …]` value) for `<session-id>` in every recipe; never the
  placeholder, never a `${CLAUDE_SESSION_ID}` expansion.
- **No shell expansions in recipes.** No `${…}`, `$(…)`, or backticks on any
  Bash line (they defeat permission allowlisting); the `${CLAUDE_PLUGIN_ROOT}`
  prefix is exempt (the harness substitutes it). The ENGINE anchors a relative
  `.crew/...` path arg to the project root (`crew_base()`), never the shell
  cwd, so recipes carry plain relative paths. Write-tool paths are absolute
  literals (`<project-root>/...`, your `CLAUDE_PROJECT_DIR` value
  substituted); Write takes no shell.
- **`<session_segment>`, never the raw id, in reconstructed run-dir paths**:
  it is the prep JSON's sanitized dir segment the engine actually built the
  run dir from; a raw id can diverge from it.
- **Reference, not payload.** Point a seat at a file to `Read`; never paste
  file contents into a prompt.
- The engine enforces and tests the mechanics these recipes rely on
  (run-scoping, preserve-valid, anchoring): see `scripts/CLAUDE.md`.

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models review; the rest is
the review request.

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
  (Step 3) splits the flags into `subprocess_seats`/`task_seats`/
  `task_seat_models`; pass the flag through and read the JSON.

## What this does

One review prompt fans out across a multi-model panel; you synthesize the
results into the `APPROVED / REVISE / [BLOCKING] / [MINOR]` verdict. Two seat
kinds: **subprocess seats** (external-CLI entries, via the engine) and **task
seats** (Claude voices, each the single `crew:reviewer` agent spawned via the
Task tool with a per-spawn `model` override; in-session on the subscription,
no `claude -p`, no API key; read-only by convention). `review-prep`'s JSON is
the roster of record; only fan out those seats. A failed/skipped seat NEVER
aborts the review.

> **`allowed-tools` scopes THIS orchestrator only.** Seat tool access is
> governed per-seat by the reviewer agent frontmatter and the engine's sandbox
> flags. (`Write` stays here for the temp files feeding `persist-seat -f` and
> `repair-seat -f`.)

## Step 1 — Dispatch plan vs code (natural language)

Read the review request (`$ARGUMENTS` minus panel flags) and pick the target:

- **Plan review** when it says "the plan", names a `.md` path, or is
  empty/"latest" (newest `.md` in `<project-root>/.crew/plans/`).
- **Code review** when it says "the code", "the diff", "my working tree", "vs
  main", a branch/commit, or other scope words.

Resolve the concrete target. Plan: the `.md` path. Code: a git scope for the
engine's `target` arg (`working-tree`, `branch` with `--base`, `commit:<sha>`,
or `auto`).

## Step 2 — Echo the resolved target; confirm on ambiguity

Before spawning ANY seat, print:

```
RESOLVED TARGET: kind=<plan|code> scope=<path or git scope> base=<base> state=<dirty|clean>
```

If plan-vs-code intent is **ambiguous** (e.g. a `.md` that is also
tracked-and-modified), ask **exactly ONE** confirming question and WAIT for
the answer. Do not silently guess.

## Step 3 — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep`: it is the SOLE source of the
`task_seats`/`task_seat_models` Step 4 needs.** When `subprocess_seats` is
empty (e.g. `--panel lite`/`solo`; `prompt_path` prints `""`), skip ONLY the
per-seat fan-out (3.2): you STILL persist the Task seats (Step 5) and run the
SAME grouped `collect` (Step 5.5) with `--seats <task_seats only>`; there is
no "synthesize from raw Task returns" path.

**3.0-3.1: prep the fan-out in ONE call.** `review-prep` resolves the target
and the panel split (group tokens expanded via the registry), mints the
RUN-SCOPED review dir (`run.json` + a frozen snapshot of the reviewed
content), stages every seat prompt against that snapshot, and PRINTS
`{prompt_path, subprocess_seats, task_seats, task_seat_models, run_dir,
run_id, target_sha256, session_segment, task_prompt_paths,
pending_subprocess_seats, pending_task_seats}` as one-line JSON. It runs
NOTHING. **Quote `"<TARGET>"`/`"<BASE>"`** (a plan path can contain spaces);
`<TARGET>` is the plan `.md` path or git scope from Step 1. (`--inline-diff`
embeds content instead; reference mode is the default.)

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep "<TARGET>" --base "<BASE>" --session-id <session-id>
```

OMIT `--panel`/`--seats` unless the user chose one. **Parse the one-line JSON
directly from stdout** (never a `$(…)` capture): `pending_subprocess_seats`
drives the per-seat loop, `run_id` rides every engine call below, the FULL
`subprocess_seats` joins `collect --seats` (Step 5.5). Use the printed
`run_dir` VERBATIM only in **Write-tool** and **Task `Read`** references; a
**Bash recipe** never pastes it and instead reconstructs the relative
`.crew/reviews/<session_segment>/<run_id from the prep JSON>/...`.

**3.2: run EACH pending seat in its own parallel shell.** Do NOT delete seat
files: results are RUN-SCOPED (changed content mints a NEW run dir; an
unchanged target RESUMES its dir, landed seats dropping out of the pending
lists; a landed valid result is never overwritten by a fresh failure). For
every seat in `pending_subprocess_seats` (NOT the full roster), launch a
SEPARATE `crew run <seat>` Bash call, all concurrently (e.g. background
calls) so they are distinct shells you can watch and kill individually; `run`
DERIVES `-f`/`-o` from the run dir, so pass no paths:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --json
```

`run --json` exits 0 for every result it WRITES, the six-field shape
(`name, model, ok, output, error, elapsed`); a failed/skipped seat lands as
`ok=False` with a diagnostic, so per-seat never-choke is automatic and the
Step 5.5 collect renders a missing named seat as a labeled SKIPPED block.
Run-scoped MISUSE (non-member seat, prompt/model/sandbox override) exits 2
with nothing written; the templated call above never hits that path.

**3.3: do NOT collect yet.** The WHOLE panel flows through ONE grouped
`collect` in **Step 5.5**, after the Task seats are persisted. Launch the 3.2
shells and move to Step 4.

## Step 4 — Fan out Task seats (parallel)

Spawn a reviewer **in parallel for each entry in `pending_task_seats`** (skip
if empty). Do NOT hand-write or re-render any prompt: `review-prep` ALREADY
STAGED one per Task seat against the run's FROZEN snapshot
(`task_prompt_paths`; `<run_dir>/target.md` or `target.diff`); a re-render
would re-resolve the LIVE target and reopen the drift window. Dispatch **by
reference** (the reviewer has `Read`), pinning `model` from
`task_seat_models[<seat>]`; never hardcode seat names or model pins, never
inline the staged prompt:

```
# for each <seat> in pending_task_seats (path = task_prompt_paths[<seat>]):
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read <run_dir>/prompt-<seat>.txt and follow it exactly.")
```

The staged prompt already states plan-vs-code, the frozen snapshot as review
authority, the criteria, and the per-criterion PASS/FAIL +
`[BLOCKING]`/`[MINOR]` findings + one-line verdict. `.crew/` is gitignored.

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS
> the seat's final message as its tool result; that returned text IS the
> seat's review *and* the proof it finished.
> **NEVER judge a seat by a proxy** (output-file byte size, transcript length,
> a missed notification, elapsed time): those race the transcript flush and
> have falsely declared a finished seat "dead", discarding a good review. A
> seat that has not returned is **still running, not failed**: wait for it.
> Mark `ok=False` ONLY when the Task returns an error, returns no usable
> block, or the harness reports it failed/missing.

**Never choke on a Task-seat failure:** a spawn that errors, times out, or
returns no usable block MUST NOT abort the review; normalize it to the
six-field `ok=False` shape (Step 5) and continue.

## Step 5 — Normalize Task-seat results to the six-field shape

Normalize each spawned seat's return into the SAME six fields as the
subprocess results: `name` = the seat name (its own filename stem), `model` =
`task_seat_models[<seat>]`, `ok` = True only if the returned result is a
usable review block, `error` = a populated diagnostic when `ok=False` (never a
fabricated `ok=True`), `elapsed` = best-effort, `output` = the review text. A
failed Task seat renders exactly like a failed subprocess seat; a
skipped/unspawnable seat renders a clearly-marked skipped block and is
excluded from the verdict math.

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
(never fabricate an ok result):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" persist-seat <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --model "<task_seat_models[<seat>]>" --failed --error "<one-line diagnostic>"
```

The engine derives the filename and shape; the printed path is the
`<seat>.json` the Step 5.5 collect reads.

## Step 5.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every 3.2
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

`collect` reads EXACTLY the named seats (never globs, so a stale/foreign file
is never folded in). `--group` writes the deduped digest (`panel.md`);
`--full` the faithful per-seat concatenation (`panel-full.md`, the recovery
artifact). **Read `panel.md` once** and carry that ONE digest into Step 6; do
NOT also carry raw Task-seat blocks into context.

## Step 6 — Synthesize the verdict

Read all four `panel.md` sections:

- **VERDICTS**: roster of every seat that RAN + tally. Reference seats by
  LABEL ("the codex verdict"), never positionally.
- **CRITERIA MATRIX**: per-criterion PASS/FAIL/`?` per seat.
- **GROUPED FINDINGS**: each finding ONCE with an `M/N` agreement count. A
  **`⚠ SINGLETON`** lone dissent is weighed ON ITS MERITS, never dismissed for
  being one seat (a real blocker often starts as one seat's finding).
- **RAW / UNPARSED SEATS**: any seat that didn't parse even after repair,
  verbatim. **Still read these**: a partial seat's prose findings live here.

Emit: **plan target** = `APPROVED` / `REVISE` (with `[BLOCKING]`/`[MINOR]`
items); **code target** = findings tagged `[BLOCKING]`/`[MINOR]` + a summary
verdict.

**The quorum header gates certification.** The run-scoped digest opens with a
`PANEL:` header; `APPROVED` (or completing on REVISE-with-only-[MINOR])
requires it to read MET, or to be absent (a flat/legacy digest carries no
quorum gate). On NOT MET, report `could not verify quorum: <usable>/<N> usable
(threshold from the header)`, name the pending/failed seats, and either
relaunch the pending seats (same `--run-id`; a landed valid seat is never
overwritten) or surface the shortfall to the user. This gates only
certification; the digest header is the authority on the count.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
NEVER aborts the review and is NEVER silently dropped (it shows in VERDICTS
and/or RAW): synthesize from the seats that produced usable output. ONLY when
**zero** seats produced usable output, skip the verdict and report: `could not
review — all seats failed: <per-seat diagnostics>`.

---

Subscription safety: the engine drives only external-CLI subprocess seats;
Claude voices are in-session Task seats. No `claude -p`, no Anthropic API — a
stray `ANTHROPIC_API_KEY` is irrelevant.
