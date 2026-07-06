---
description: Start verified development loop with multi-model review before completion
argument-hint: "<task description>"
allowed-tools: Bash, Task, Read, Write, Glob
---

[BUILD LOOP STARTING]

$ARGUMENTS

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models verify; the rest is
the task. Default (no flag) = **OMIT the `--panel`/`--seats` flag entirely** and
let `review-prep` apply the configured default. The default `default_panel`
resolves across tiers (precedence CLI flag > per-repo `.crew/config.toml` >
global `~/.crew-config.toml` > built-in **full**; no env tier). Do NOT hardcode
`--panel full` — passing it would override a user's configured default.

- `--panel full|lite|solo|cursor|quick|<custom>` — a named preset. The five
  are built in (`quick` = `codex,sonnet` — the cheapest cross-model pair, good
  for routine diffs); `<custom>` is any roster defined under `[panels]` in
  per-repo or global config. `--seats <comma-list>` — an explicit subset of any
  registered seat (e.g. `--seats codex,opus`); `--seats`
  wins if both are given. `cursor-gemini`, `cursor-glm`, and `cursor-gpt` are opt-in (add via `--seats`).
  Opt-in `fable` is a premium-tier Claude voice (`model="fable"`) — add it
  explicitly (e.g. `--seats codex,opus,sonnet,fable`).
- **The engine resolves the preset — the orchestrator does NOT.** `review-prep`
  (Step 2a) takes the user's `--panel`/`--seats` and resolves it into
  `subprocess_seats` (the `codex`/`agy`/`cursor-*` entries),
  `task_seats` (the Claude voices), and `task_seat_models` (each Task seat's
  model pin). The orchestrator never defines presets, classifies seat names, or
  hardcodes a model pin — it passes the flag through and reads the JSON: Step 2a
  iterates `subprocess_seats`, Step 2b stages + spawns `task_seats`. **Pass
  `--panel`/`--seats` ONLY when the user named a panel or seats** (an explicit
  flag, or natural-language intent like "use the lite panel"); otherwise OMIT the
  flag and let `review-prep` apply the configured/built-in default.

Below, **"the task"** means `$ARGUMENTS` with these panel flags removed — use it
wherever a step references the task. Keep the raw `$ARGUMENTS` (flags included)
only in the `crew state … --prompt` so the panel survives across iterations. A
one-seat panel is fine — synthesis / never-choke handle any count down to one.

## MANDATORY: Activate the Loop

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state init bl --prompt "$ARGUMENTS"
```

## How This Works

1. Delegate the implementation to the **executor agent** (keeps main context clean)
2. When executor reports complete, get multi-model verification
3. If the panel approves (APPROVED), complete the loop
4. If the panel says REVISE with [BLOCKING] issues, re-delegate to executor with those issues, then re-verify
5. If the panel says REVISE with only [MINOR] issues, accept and complete
6. If you try to stop without completing, you'll be reminded to continue

You orchestrate. You don't implement. All file edits, builds, and tests happen inside the executor agent.

## Step 1: Delegate Work to Executor

Spawn the **executor agent** to implement the task:

```
Task(
  subagent_type="crew:executor",
  prompt="Execute: <the task>

Work through the task systematically. Create todos to track progress. Implement the changes, run builds/tests where applicable, and report back: what you changed, what you verified, and any blockers."
)
```

(`<the task>` = `$ARGUMENTS` with any panel flags stripped — see Panel options.)

Capture the executor's summary — you'll pass it to the review panel in the next step.

## Step 2: Get Multi-Model Verification

Before requesting verification:

- [ ] Executor reported the task complete
- [ ] No unresolved blockers in executor's report

When executor reports complete, verify the work with a multi-model panel instead
of a single advisor. The same code-review criteria fan out across several AI
seats in parallel over the **working-tree diff**, and you synthesize the results
into the existing verdict. Two seat kinds:

- **subprocess seats** — the `codex`/`agy`/`cursor-*` entries of
  the resolved panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries of the resolved panel, each a
  `crew:reviewer` spawned via the Task tool (in-session, on the subscription —
  no `claude -p`, no API key).

The panel is whatever the Panel-options flags resolved to (default **codex +
agy + cursor-auto + cursor-composer + opus + sonnet**). Only fan out the
seats in that list. A failed/skipped seat
NEVER aborts the verification (see "Synthesize" below) — the verdict is
synthesized from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.
> (`Write` stays in `allowed-tools` for the temp files that feed `persist-seat
> -f` and `repair-seat -f` — do not drop it.)

### Step 2a — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep` (2a.0–2a.1 below) — it is the SOLE source of the
`task_seats`/`task_seat_models` Step 2b needs.** Skip ONLY the per-seat subprocess
fan-out (2a.2) when `subprocess_seats` is empty (e.g. `--panel lite`/`solo`) — go
straight to Step 2b. (You STILL persist the Task seats in Step 2c and run the SAME
grouped `collect` in Step 2c.5 over the Task seats alone — Decision-I; there is no
"synthesize from raw Task returns" path anymore.) Otherwise fan the subprocess
seats out
over the working-tree diff **one `crew run` call per seat, in parallel** — each a
separate, visible, individually-killable shell (the per-seat shape
`/crew:debate`'s multi-round path uses), not one opaque `review` call hiding them
in an internal thread pool. Use the
bare-script path (its top-of-file `sys.path` guard makes package imports resolve).

**2a.0–2a.1 — prep the fan-out in ONE call.** `review-prep` does the deterministic
prep — resolves the `working-tree` target, resolves `--panel`/`--seats` into the
SUBPROCESS seat list (group tokens like `cursor` expanded via the registry) AND the
Task-seat split, and stages the ONE shared subprocess prompt (`--stage`,
byte-identical to a standalone `render working-tree --mode review --stage`) — and
PRINTS `{prompt_path, subprocess_seats, task_seats, task_seat_models}`
as one-line JSON. It runs NOTHING (the per-seat loop below stays yours). Substitute
your real id for `<session-id>` (the `[Session ID: …]` value; never the literal
placeholder or a `${…}` expansion):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep working-tree --session-id <session-id>
```

**OMIT `--panel`/`--seats` when the user named no panel** (as shown above) — so
`review-prep` applies the repo's configured `default_panel` or the built-in
**full** panel. Pass `--panel <preset>` / `--seats <subset>` ONLY when the user
chose one explicitly.

(No `--base` here: the `working-tree` target reviews the uncommitted diff vs `HEAD`
and ignores the base ref, so omitting it matches the historical `render
working-tree --stage` call and keeps `prompt_path` byte-identical.) Reference mode
(the default) makes each seat reproduce the compound working-tree diff itself,
untracked files included — so a seat can't miss files the executor just created;
add `--inline-diff` to embed it instead (forwarded to the staged prompt).

`review-prep` resolves BOTH seat kinds: Step 2b reads `task_seats` +
`task_seat_models` from this same JSON (it no longer classifies Claude seats
itself), and the engine still EXECUTES only the subprocess seats. **Parse the
one-line JSON** it prints (read it
directly from the command's stdout — it is NOT a shell `$(…)` capture): use
`subprocess_seats` to iterate the per-seat loop AND (JOINED comma-separated) as
part of the `collect --seats` list in Step 2c.5. The per-seat `run` DERIVES its
`-f` from `--session-id` (= `prompt_path`,
`.crew/reviews/<session-id>/prompt-seat.txt`), so you do NOT pass `prompt_path` as
`-f` yourself (see 2a.2).
**If `subprocess_seats` is empty** (a Claude-only `--panel lite`/`solo` resolves to
no subprocess seats, so `prompt_path` is `""` and nothing was staged), **SKIP only
the 2a.2 per-seat loop** — go straight to Step 2b. You STILL persist the Task seats
(Step 2c) and run the SAME grouped `collect` (Step 2c.5) with `--seats
<dot-stripped task_seats only>`; the grouped `panel.md` then covers the Claude-only
roster (Decision-I).

**2a.2 — run EACH seat in its own parallel shell.** First, **clear any pre-existing
`<seat>.json`** for the seats you are about to run — the session dir
`.crew/reviews/<session-id>/` is REUSED across reviews, so a leftover file from an
EARLIER panel (or from a seat whose shell you later kill) would otherwise be read
by `collect` as a FRESH result, folding stale data into this verdict. After
clearing, a seat that doesn't write a fresh file this run correctly renders as
SKIPPED, not stale-OK. Remove the expected target file for each seat first:

```bash
rm -f .crew/reviews/<session-id>/<seat>.json
```

Then, for every seat in `subprocess_seats` (from the 2a.0–2a.1 prep JSON), launch a
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

**2a.3 — do NOT collect yet.** The subprocess seats are NOT collected here on
their own anymore. The WHOLE panel — subprocess AND Task seats — flows through ONE
grouped `collect` in **Step 2c.5**, after the Task seats are persisted. Just launch
the 2a.2 shells (in parallel) and move on to Step 2b; Step 2c.5 owns the
wait-for-both barrier and the single collect.

### Step 2b — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each entry in `task_seats`** (from the
2a.0–2a.1 `review-prep` JSON — skip this step if `task_seats` is empty). Do
NOT hand-write each seat's prompt: **render it from the engine** so every seat —
subprocess AND Task — reviews ONE identical target. `render` is the single prompt
source, and it resolves `working-tree` the SAME way Step 2a's engine `review`
did — via the compound `Target.diff_cmd` that INCLUDES untracked files. That
matters most here: `/crew:build` verification routinely follows a step where the
executor just CREATED new files, and a hand-built `git diff HEAD` prompt would
make the Claude seats miss exactly those untracked files while the subprocess
seats see them. Render the Task seats' prompts over the SAME `working-tree`
target, staging ALL of them in ONE `--stage-all` call fed the **comma-joined
`task_seats`** from the prep JSON (NOT a hardcoded role list):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render working-tree --mode review --stage-all <comma-joined task_seats from the review-prep JSON> --session-id <session-id>
```

**Skip this `--stage-all` call entirely if `task_seats` is empty** (a
subprocess-only panel like `--panel cursor`). A Task seat name with a `.` (none in the current roster) would stage with the
dot stripped from the filename — the same derivation the spawn line below reads,
so the staged file and the read file can never diverge.

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

Then dispatch each seat with the rendered text as its prompt, appending the
executor's summary inline (it's small) so every seat shares the same intent. Each
seat is the SAME agent (`crew:reviewer`) with a per-spawn `model` override
selecting the voice. **Iterate `task_seats` from the prep JSON**; for each
`<seat>` read its model from `task_seat_models[<seat>]` and its prompt from the
staged `prompt-<seat, dot-stripped>.txt`. Do NOT hardcode seat names or model
pins — both come from the JSON:

```
# for each <seat> in task_seats:
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="<contents of .crew/reviews/<session-id>/prompt-<seat, dot-stripped>.txt> + executor summary")
```

The rendered prompt already states this is a **code review**, tells the seat to
inspect the changes itself in reference mode (the compound working-tree diff,
untracked files included), lists the criteria (completeness, code quality,
security, performance, error handling), and asks for findings tagged
`[BLOCKING]`/`[MINOR]` + a one-line verdict. `crew:reviewer` has `Read, Grep,
Glob, Bash` for exactly this (read-only git/inspection). Because every seat's
prompt comes from the one `render` source over the same `working-tree` target,
the panel reviews a single identical diff — no tracked-vs-untracked divergence.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors,
times out, returns no usable block, or is reported missing/failed by the harness
MUST NOT abort the verification. Catch it, normalize it to the six-field
`ok=False` shape (Step 2c), and continue. A failed Task seat is treated
identically to a failed subprocess seat.

### Step 2c — Normalize Task-seat results to the six-field shape

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS the
> seat's final message as its tool result — that returned text IS the seat's
> review *and* the proof it finished. Take `ok`/`output` from that returned
> result. **NEVER judge a seat by a proxy** (output-file byte size, transcript
> length, a missed notification, elapsed time): those race the transcript flush
> and have falsely declared a finished seat "dead", discarding a good review. A
> seat that has not returned is **still running, not failed** — wait for it.

For each Task seat **in `task_seats`** (from the `review-prep` JSON — NOT a
hardcoded `opus`/`sonnet` pair), normalize its return into the SAME six fields as
the subprocess results:

- `name` = the seat's dot-stripped name (the same `[A-Za-z0-9_-]`-only slug
  `_seat_role_slug` in `cli.py` derives — the ONE canonical dot-strip source, so
  a dotted role never drifts between staging, persist, and collect; for the
  current roster the slug is the name itself).
- `model` = the pinned model from `task_seat_models[<seat>]` (e.g. `opus`,
  `sonnet`, `fable`).
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

The engine derives the dot-stripped filename and the six-field shape itself —
the printed path is the `<seat>.json` the Step 2c.5 collect reads. The seat
entry you pass to `collect --seats` is the seat's dot-stripped slug, i.e. the
filename stem the engine printed.

### Step 2c.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every Step 2a.2
subprocess `crew run <seat>` shell to EXIT, AND (b) every Step 2b Task seat to
return AND be persisted in Step 2c. A seat whose `<seat>.json` is not written yet
is STILL RUNNING, not skipped — collecting early would silently drop it.

Define `<ran_seats>` = the comma-join of `subprocess_seats` (resolved order from
the prep JSON) **then** the dot-stripped `task_seats` (Decision-J order). In the
Claude-only branch (`subprocess_seats` empty) `<ran_seats>` is just the
dot-stripped `task_seats`.

**Repair non-compliant seats (per-seat haiku reformat).** A seat that ignored the
structured `## FINDINGS` schema can't be grouped — give it ONE cheap repair pass.
Ask the engine which seats did not parse:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --report-unparsed
```

This prints the seat names (one per line) whose output ran but did NOT parse into
structured findings. For EACH such `<seat>`:

1. **Read** its raw `output` field — `.crew/reviews/<session-id>/<seat>.json`.
2. Spawn the cheap **formatter** agent to reformat that text into the schema (a
   FAITHFUL transform — it must NOT invent, drop, or re-judge findings):

   ```
   Task(subagent_type="crew:formatter", model="haiku",
        prompt="Reformat this review into the FINDINGS schema verbatim — do not
   invent or drop findings, only reformat:\n\n<the seat's raw output field>")
   ```

3. Write the formatter's returned text into the seat file with the engine
   (it writes to a SEPARATE `repaired_output` field, NEVER overwriting the
   original `output`):

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/crew" repair-seat --seat .crew/reviews/<session-id>/<seat>.json -f <temp-file-with-reformatted-text>
   ```

`repair-seat` is **non-destructive**: it populates `repaired_output` ONLY IF the
reformatted text parses into the FINDINGS schema. If it STILL doesn't parse, the
write is a **no-op** — `repaired_output` is left unset and the command exits 0
with a stderr kept-original note (a no-op is success, not an error; exit 2 =
real usage/read error). Grouping uses `repaired_output` when present, while
`--full` and the RAW section ALWAYS render the original `output`, so a degraded
rewrite can never overwrite the seat's genuine review: `collect` renders the
ORIGINAL text raw (graceful degradation, never worse than the seat's real words). Just continue — the file is
correct either way. The formatter is an in-session `haiku` Task seat — no
`claude -p`, no API key.

**Collect ONCE — grouped + faithful:**

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --group -o .crew/reviews/<session-id>/panel.md --full .crew/reviews/<session-id>/panel-full.md
```

`--group` writes the deduped digest to `panel.md`; `--full` writes the faithful
per-seat concatenation to `panel-full.md` (the recovery artifact). **Read
`panel.md` once** and carry that ONE digest into Step 2d — do NOT also carry the
raw Task-seat blocks into context.

### Step 2d — Synthesize the verdict

Read the ONE grouped `panel.md` digest (Step 2c.5) — it already folds the WHOLE
panel (subprocess AND Task seats) — and emit the existing verdict format:

- APPROVED: Work is complete and code quality is acceptable.
- REVISE: Issues found. Classify each as [BLOCKING] or [MINOR].

If APPROVED or only [MINOR] issues, state: `VERDICT: APPROVED`.
If [BLOCKING] issues exist, state: `VERDICT: REVISE` with the list of issues.

`panel.md` has four sections, read them ALL: **VERDICTS** (roster of every seat
that ran + tally — reference seats by LABEL, never positionally), **CRITERIA
MATRIX** (per-criterion PASS/FAIL/`?`), **GROUPED FINDINGS** (each finding once,
with an `M/N` agreement count where N = findings-parsed seats; a **`⚠ SINGLETON`**
lone dissent is weighed ON ITS MERITS, never dismissed for being one seat), and
**RAW / UNPARSED SEATS** (any seat that didn't parse even after repair, verbatim —
**still read these**, a partial seat's prose findings live here).

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the verification and is NEVER silently dropped
(it shows in the VERDICTS roster and/or the RAW section): synthesize from the
seats that produced usable output. ONLY when **zero** seats produced usable output
do you skip the verdict and instead report:
`could not verify — all seats failed: <per-seat diagnostics>`.

## Step 3: Handle the Verdict

| Verdict                             | Action                                                                       |
| ----------------------------------- | ---------------------------------------------------------------------------- |
| **APPROVED**                        | Complete the loop (Step 4)                                                   |
| **REVISE with only [MINOR]**        | Accept and complete the loop (Step 4)                                        |
| **REVISE with any [BLOCKING]**      | Re-delegate to executor with the issues (below), then return to Step 2       |

### Re-delegating on REVISE [BLOCKING]

```
Task(
  subagent_type="crew:executor",
  prompt="REVISION needed for previous task.

Original task: <the task>

The panel flagged these [BLOCKING] issues:
[list each blocking issue verbatim]

Address each issue. Report what you changed and what you verified."
)
```

Then go back to Step 2 to verify the revised work.

## Step 4: Complete the Loop

When the panel's verdict is APPROVED (or REVISE with only [MINOR] issues):

1. **Deactivate the loop:**
```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate bl --reason "Panel approved"
```

2. **Summarize what was accomplished** and let the user know the task is complete.

## Early Exit

To exit before completion: `/crew:cancel-build`

---

First: run the activation command. Then proceed to Step 1 (delegate to executor).
