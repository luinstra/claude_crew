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
let `review-prep` apply the configured default (precedence CLI flag > per-repo
`.crew/config.toml` > global `~/.crew-config.toml` > built-in **full**; no env
tier). Do NOT hardcode `--panel full` — it would override a user's configured
default.

- `--panel full|lite|solo|cursor|quick|<custom>` — a named preset (`quick` =
  `codex,sonnet`, the cheapest cross-model pair; `<custom>` = any `[panels]`
  roster in per-repo/global config). `--seats <comma-list>` — an explicit subset
  of any registered seat (e.g. `--seats codex,opus`); `--seats` wins if both are
  given. `cursor-gemini`, `cursor-glm`, `cursor-gpt`, `cursor-grok`, and `fable`
  (a premium-tier Claude voice, `model="fable"`) are opt-in — add via `--seats`.
- **The engine resolves the preset — the orchestrator does NOT.** `review-prep`
  (Step 2a) resolves `--panel`/`--seats` into `subprocess_seats` (the
  `codex`/`agy`/`cursor-*` entries), `task_seats` (the Claude voices), and
  `task_seat_models` (each Task seat's model pin). The orchestrator never defines
  presets, classifies seat names, or hardcodes a model pin — pass the flag through
  and read the JSON. **Pass `--panel`/`--seats` ONLY when the user named a panel
  or seats** (an explicit flag, or intent like "use the lite panel"); otherwise
  OMIT it and let `review-prep` apply the configured/built-in default.

Below, **"the task"** means `$ARGUMENTS` with these panel flags removed — use it
wherever a step references the task. The raw `$ARGUMENTS` (flags included) is
preserved byte-for-byte via the `-f` spill file below so the panel survives across
iterations. A one-seat panel is fine — synthesis / never-choke handle any count
down to one.

## MANDATORY: Activate the Loop

**Spill the task to a file and pass `-f` — NEVER inline.** Write the raw
`$ARGUMENTS` to `.crew/task-bl-<session-id>.txt` with the **Write tool** (which
writes the exact bytes — no shell is involved — and creates the `.crew/` parent
dir). A positional/`--prompt "$ARGUMENTS"` value containing `$(…)`, `$VAR`,
backticks, or `"` would be expanded or mangled by the shell when the Bash tool runs
the command, so the task NEVER goes on the command line. Then init the loop,
substituting your actual session id (the `[Session ID: …]` value) for
`<session-id>` as a literal `--session-id` value, NEVER a `${CLAUDE_SESSION_ID}`
shell expansion:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state init bl -f .crew/task-bl-<session-id>.txt --session-id <session-id>
```

Immediately after a successful init, remove the spill file (its own Bash call — no
chaining). The `session-start` L5 cleanup patterns do NOT cover these task files, so
this rm step is their sole owner:

```bash
rm -f .crew/task-bl-<session-id>.txt
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

Capture the executor's summary — you'll pass it to the review panel in Step 2b.

## Step 2: Get Multi-Model Verification

Before requesting verification:

- [ ] Executor reported the task complete
- [ ] No unresolved blockers in executor's report

When executor reports complete, verify the work with a multi-model panel over the
**working-tree diff**, and synthesize the results into the existing verdict. Two
seat kinds:

- **subprocess seats** — the `codex`/`agy`/`cursor-*` entries of the resolved
  panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries, each a `crew:reviewer` spawned via
  the Task tool (in-session, on the subscription — no `claude -p`, no API key).

The panel is whatever the flags resolved to (default **codex + agy + cursor-auto +
cursor-composer + opus + sonnet**); only fan out the seats in that list. A
failed/skipped seat NEVER aborts the verification (see "Synthesize" below) — the
verdict is synthesized from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the seats
> spawned below — seat tool access is governed per-seat by the reviewer agent
> frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.
> (`Write` stays in `allowed-tools` for the temp files that feed `persist-seat -f`
> and `repair-seat -f` — do not drop it.)

### Step 2a — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep` (2a.0–2a.1) — it is the SOLE source of the
`task_seats`/`task_seat_models` Step 2b needs.** Skip ONLY the per-seat subprocess
fan-out (2a.2) when `subprocess_seats` is empty (e.g. `--panel lite`/`solo`) — go
straight to Step 2b; you STILL persist the Task seats (Step 2c) and run the SAME
grouped `collect` (Step 2c.5) over the Task seats alone (there is no "synthesize
from raw Task returns" path). Otherwise fan the subprocess seats out over the
working-tree diff **one `crew run` call per seat, in parallel** — each a separate,
visible, individually-killable shell (not one opaque `review` call hiding them in a
thread pool). Use the bare-script path (its `sys.path` guard resolves package imports).

**2a.0–2a.1 — prep the fan-out in ONE call.** `review-prep` resolves the
`working-tree` target, resolves `--panel`/`--seats` into the SUBPROCESS seat list
(group tokens like `cursor` expanded via the registry) AND the Task-seat split,
stages the ONE shared subprocess prompt (`--stage`, byte-identical to `render
working-tree --mode review --stage`), and PRINTS `{prompt_path, subprocess_seats,
task_seats, task_seat_models}` as one-line JSON. It runs NOTHING. Substitute your
real id for `<session-id>` (the `[Session ID: …]` value; never the literal
placeholder or a `${…}` expansion):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep working-tree --session-id <session-id>
```

**OMIT `--panel`/`--seats` when the user named no panel** (as shown) — so
`review-prep` applies the configured `default_panel` or built-in **full**. Pass
`--panel <preset>` / `--seats <subset>` ONLY when the user chose one explicitly.
(No `--base` here: the `working-tree` target reviews the uncommitted diff vs `HEAD`
and ignores the base ref, so omitting it keeps `prompt_path` byte-identical to the
historical `render working-tree --stage` call.) Reference mode (the default) makes
each seat reproduce the compound working-tree diff itself, untracked files included
— so a seat can't miss files the executor just created; `--inline-diff` embeds it
instead (forwarded to the staged prompt).

`review-prep` resolves BOTH seat kinds: Step 2b reads `task_seats` +
`task_seat_models` from this same JSON; the engine EXECUTES only the subprocess
seats. **Parse the one-line JSON** it prints (read it directly from stdout — it is
NOT a shell `$(…)` capture): use `subprocess_seats` to iterate the per-seat loop AND
(JOINED comma-separated) as part of the `collect --seats` list in Step 2c.5. The
per-seat `run` DERIVES its `-f` from `--session-id` (= `prompt_path`,
`.crew/reviews/<session-id>/prompt-seat.txt`), so you do NOT pass `prompt_path` as
`-f` yourself (see 2a.2). **If `subprocess_seats` is empty** (a Claude-only
`--panel lite`/`solo` → `prompt_path` is `""`, nothing staged), **SKIP only the 2a.2
per-seat loop** — go to Step 2b; you STILL persist the Task seats (Step 2c) and run
the SAME grouped `collect` (Step 2c.5) with `--seats <dot-stripped task_seats only>`.

**2a.2 — run EACH seat in its own parallel shell.** First, **clear any pre-existing
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

**2a.3 — do NOT collect yet.** The WHOLE panel — subprocess AND Task seats — flows
through ONE grouped `collect` in **Step 2c.5**, after the Task seats are persisted.
Launch the 2a.2 shells (in parallel) and move to Step 2b; Step 2c.5 owns the
wait-for-both barrier and the single collect.

### Step 2b — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each entry in `task_seats`** (from the
`review-prep` JSON — skip if empty). Do NOT hand-write each seat's prompt: **stage
it from the engine** so every seat — subprocess AND Task — reviews ONE identical
target. `render` is the single prompt source and resolves `working-tree` the SAME
way Step 2a's engine `review` did — via the compound `Target.diff_cmd` that INCLUDES
untracked files. That matters most here: `/crew:build` verification routinely
follows a step where the executor just CREATED new files, and a hand-built `git diff
HEAD` prompt would make the Claude seats miss exactly those untracked files while the
subprocess seats see them. Stage ALL Task seats in ONE `--stage-all` call over the
SAME `working-tree` target, fed the **comma-joined `task_seats`** from the prep JSON
(NOT a hardcoded role list):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render working-tree --mode review --stage-all <comma-joined task_seats from the review-prep JSON> --session-id <session-id>
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
env). The engine **rejects an unsubstituted `<…>` placeholder** with a loud error,
so a missed substitution fails fast instead of silently sharing one dir. `.crew/` is
gitignored.

Then **write the executor's summary to `.crew/reviews/<session-id>/executor-summary.md`**
with the Write tool (a PATH, not inline — so it isn't re-transcribed into every seat's
prompt), and dispatch each seat **by reference** — the SAME agent (`crew:reviewer`)
with a per-spawn `model` override selecting the voice. **Iterate `task_seats` from
the prep JSON**; for each `<seat>` read its model from `task_seat_models[<seat>]`. Do
NOT hardcode seat names or model pins, and do NOT read + inline the staged prompt —
point the seat at its staged file and the summary file and let it `Read` them
(reference, not payload; the reviewer has `Read`):

```
# for each <seat> in task_seats:
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read .crew/reviews/<session-id>/prompt-<seat, dot-stripped>.txt and the executor summary at .crew/reviews/<session-id>/executor-summary.md, then follow the prompt exactly.")
```

The staged prompt already states this is a **code review**, tells the seat to inspect
the changes itself in reference mode (the compound working-tree diff, untracked files
included), lists the criteria (completeness, code quality, security, performance,
error handling), and asks for findings tagged `[BLOCKING]`/`[MINOR]` + a one-line
verdict. `crew:reviewer` has `Read, Grep, Glob, Bash` for exactly this (read-only
git/inspection). Because every seat's prompt comes from the one `render` source over
the same `working-tree` target, the panel reviews a single identical diff.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors, times
out, returns no usable block, or is reported missing/failed MUST NOT abort the
verification. Catch it, normalize it to the six-field `ok=False` shape (Step 2c), and
continue. A failed Task seat is treated identically to a failed subprocess seat.

### Step 2c — Normalize Task-seat results to the six-field shape

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS the
> seat's final message as its tool result — that returned text IS the seat's review
> *and* the proof it finished. Take `ok`/`output`
> from that returned result. **NEVER judge a seat by a proxy** (output-file byte size,
> transcript length, a missed notification, elapsed time): those race the transcript
> flush and have falsely declared a finished seat "dead", discarding a good review. A
> seat that has not returned is **still running, not failed** — wait for it.

For each Task seat **in `task_seats`** (from the `review-prep` JSON — NOT a hardcoded
`opus`/`sonnet` pair), normalize its return into the SAME six fields as the
subprocess results:

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
printed path is the `<seat>.json` the Step 2c.5 collect reads. The seat entry you
pass to `collect --seats` is the seat's dot-stripped slug (the filename stem the
engine printed).

### Step 2c.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every Step 2a.2
subprocess `crew run <seat>` shell to EXIT, AND (b) every Step 2b Task seat to return
AND be persisted in Step 2c. A seat whose `<seat>.json` is not written yet is STILL
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
reformatted text parses into the FINDINGS schema. If it STILL doesn't parse, the
write is a **no-op** — `repaired_output` is left unset and the command exits 0 with a
stderr kept-original note (a no-op is success; exit 2 = real usage/read error).
Grouping uses `repaired_output` when present, while `--full` and the RAW section
ALWAYS render the original `output`, so a degraded rewrite can never overwrite the
seat's genuine review: `collect` renders the ORIGINAL text raw (never worse than the
seat's real words). Continue — the file is correct either way. The formatter is an
in-session `haiku` Task seat — no `claude -p`, no API key.

**Collect ONCE — grouped + faithful:**

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --group -o .crew/reviews/<session-id>/panel.md --full .crew/reviews/<session-id>/panel-full.md
```

`--group` writes the deduped digest to `panel.md`; `--full` writes the faithful
per-seat concatenation to `panel-full.md` (the recovery artifact). **Read `panel.md`
once** and carry that ONE digest into Step 2d — do NOT also carry the raw Task-seat
blocks into context.

### Step 2d — Synthesize the verdict

Read the ONE grouped `panel.md` digest — it already folds the WHOLE panel — and emit
the existing verdict format:

- APPROVED: Work is complete and code quality is acceptable.
- REVISE: Issues found. Classify each as [BLOCKING] or [MINOR].

If APPROVED or only [MINOR] issues, state: `VERDICT: APPROVED`.
If [BLOCKING] issues exist, state: `VERDICT: REVISE` with the list of issues.

`panel.md` has four sections, read them ALL: **VERDICTS** (roster of every seat that
ran + tally — reference seats by LABEL, never positionally), **CRITERIA MATRIX**
(per-criterion PASS/FAIL/`?`), **GROUPED FINDINGS** (each finding once, with an `M/N`
agreement count where N = findings-parsed seats; a **`⚠ SINGLETON`** lone dissent is
weighed ON ITS MERITS, never dismissed for being one seat), and **RAW / UNPARSED
SEATS** (any seat that didn't parse even after repair, verbatim — **still read
these**, a partial seat's prose findings live here).

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the verification and is NEVER silently dropped (it
shows in the VERDICTS roster and/or the RAW section): synthesize from the seats that
produced usable output. ONLY when **zero** seats produced usable output do you skip
the verdict and instead report: `could not verify — all seats failed: <per-seat
diagnostics>`.

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

1. **Deactivate the loop** — pass the SAME `--session-id` the init used (the
   `[Session ID: …]` value, as a literal, NOT a `${CLAUDE_SESSION_ID}` shell
   expansion), so deactivate targets the exact session-scoped state file that init
   wrote (otherwise the scoped file stays active and keeps blocking Stop):
```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate bl --reason "Panel approved" --session-id <session-id>
```

2. **Summarize what was accomplished** and let the user know the task is complete.

## Early Exit

To exit before completion: `/crew:cancel-build`

---

First: run the activation command. Then proceed to Step 1 (delegate to executor).
