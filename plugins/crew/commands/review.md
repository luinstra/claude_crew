---
description: Multi-model review of a plan OR code diff (runs the configured default panel; narrow with --panel/--seats)
argument-hint: "<the plan | the code | a .md path | a git scope>"
allowed-tools: Bash, Task, Read, Write, Glob
---

[MULTI-MODEL REVIEW]

$ARGUMENTS

> **Note (Claude seats):** the Claude voices are a single agent, `crew:reviewer`
> (read-only by convention; not sandbox-enforced), spawned once per
> `opus`/`sonnet` seat with a per-spawn `model` override.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.
> (`Write` stays in `allowed-tools` for the temp files that feed `persist-seat
> -f` and `repair-seat -f` — do not drop it.)

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models review; the rest is
the review request. Default (no flag) = **OMIT the `--panel`/`--seats` flag
entirely** and let `review-prep` apply the configured default (precedence CLI
flag > per-repo `.crew/config.toml` > global `~/.crew-config.toml` > built-in
**full**; no env tier). Do NOT hardcode `--panel full` — it would override a
user's configured default.

- `--panel full|lite|solo|cursor|quick|<custom>` — a named preset (`quick` = the
  cheapest cross-model pair; `<custom>` = any `[panels]` roster in per-repo/global
  config). `--seats <comma-list>` — an explicit subset of any registered seat
  (e.g. `--seats codex,opus`); `--seats` wins if both are given.
  Some registered seats are opt-in and not in the built-in default panel
  (premium cursor model-seats; the `fable` Claude voice): pass their exact names
  via `--seats`. The full seat census is `crew doctor` / the README panel table.
- **The engine resolves the preset — the orchestrator does NOT.** `review-prep`
  (Step 3) resolves `--panel`/`--seats` into `subprocess_seats` (the external-CLI
  entries), `task_seats` (the Claude voices), and
  `task_seat_models` (each Task seat's model pin). The orchestrator never defines
  presets, classifies seat names, or hardcodes a model pin — pass the flag through
  and read the JSON. **Pass `--panel`/`--seats` ONLY when the user named a panel
  or seats** (an explicit flag, or intent like "use the lite panel"); otherwise
  OMIT it and let `review-prep` apply the configured/built-in default.

## What this does

The same review prompt fans out across a multi-model panel and you synthesize the
results into the existing `APPROVED / REVISE / [BLOCKING] / [MINOR]` verdict. Two
seat kinds:

- **subprocess seats** — the external-CLI entries of the resolved
  panel, via the Python engine.
- **task seats** — the Claude-voice entries (built-in `opus`/`sonnet`, plus any
  config-declared claude-code seat), each a `crew:reviewer` via the
  Task tool (in-session, on the subscription — no `claude -p`, no API key).

The panel is whatever the flags resolved to (the configured default panel:
`review-prep`'s JSON is the roster of record for this run); only fan out the
seats in that list. A failed/skipped seat NEVER aborts the review (Step 6) — the
verdict is synthesized from whichever seats succeed.

## Step 1 — Dispatch plan vs code (natural language)

Read the review request (`$ARGUMENTS` with panel flags removed) and pick the target:

- **Plan review** when the argument says "the plan", names a `.md` path, or is
  empty/"latest" (→ newest `.md` in `.crew/plans/`).
- **Code review** when it says "the code", "the diff", "my working tree", "vs
  main", a branch/commit, or other scope words.

Resolve the concrete target — Plan: the `.md` path (or newest in `.crew/plans/`);
Code: a git scope for the engine's `target` arg (`working-tree`, `branch` with
`--base`, `commit:<sha>`, or `auto`).

## Step 2 — Echo the resolved target; confirm on ambiguity

Before spawning ANY seat, print the resolved target descriptor:

```
RESOLVED TARGET: kind=<plan|code> scope=<path or git scope> base=<base> state=<dirty|clean>
```

If plan-vs-code intent is **ambiguous** (e.g. a `.md` that is also
tracked-and-modified, or wording that could mean either), ask **exactly ONE**
confirming question and WAIT for the answer before fanning out. Do not silently
guess.

## Step 3 — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep` (3.0–3.1) — it is the SOLE source of the
`task_seats`/`task_seat_models` Step 4 needs.** Skip ONLY the per-seat subprocess
fan-out (3.2) when `subprocess_seats` is empty (e.g. `--panel lite`/`solo`) — go
straight to Step 4; you STILL persist the Task seats (Step 5) and run the SAME
grouped `collect` (Step 5.5) over the Task seats alone (there is no "synthesize
from raw Task returns" path). Otherwise fan the subprocess seats out **one `crew
run` call per seat, in parallel** — each a separate, visible, individually-killable
shell (not one opaque `review` call hiding them in a thread pool). Use the
bare-script path (its `sys.path` guard resolves package imports).

**3.0–3.1 — prep the fan-out in ONE call.** `review-prep` resolves the target,
resolves `--panel`/`--seats` into the SUBPROCESS seat list (group tokens like
`cursor` expanded via the registry) AND the Task-seat split, stages the ONE shared
subprocess prompt (`--stage`, byte-identical to `render --mode review --stage`),
and PRINTS `{prompt_path, subprocess_seats, task_seats, task_seat_models}` as
one-line JSON. It runs NOTHING. **Quote `"<TARGET>"`/`"<BASE>"`** (a plan path can
contain spaces); `<TARGET>` is the plan `.md` path or git scope from Step 1
(`working-tree`/`auto` for a working-tree code review). Substitute your real id for
`<session-id>` (the `[Session ID: …]` value; never the literal placeholder or a
`${…}` expansion). Reference mode is the default — each seat fetches the diff/plan
itself; `--inline-diff` embeds it instead (rarely needed — forwarded to the staged
prompt):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep "<TARGET>" --base "<BASE>" --session-id <session-id>
```

**OMIT `--panel`/`--seats` when the user named no panel** (as shown) — so
`review-prep` applies the configured `default_panel` or built-in **full**. Pass
`--panel <preset>` / `--seats <subset>` ONLY when the user chose one explicitly.
`review-prep` resolves BOTH seat kinds: Step 4 reads `task_seats` +
`task_seat_models` from this same JSON; the engine EXECUTES only the subprocess
seats. **Parse the one-line JSON** it prints (read it directly from stdout — it is
NOT a shell `$(…)` capture): use `subprocess_seats` to iterate the per-seat loop
AND (JOINED comma-separated) as part of the `collect --seats` list in Step 5.5. The
per-seat `run` DERIVES its `-f` from `--session-id` (= `prompt_path`,
`.crew/reviews/<session-id>/prompt-seat.txt`), so you do NOT pass `prompt_path` as
`-f` yourself (see 3.2). **If `subprocess_seats` is empty** (a Claude-only
`--panel lite`/`solo` → `prompt_path` is `""`, nothing staged), **SKIP only the 3.2
per-seat loop** — go to Step 4; you STILL persist the Task seats (Step 5) and run
the SAME grouped `collect` (Step 5.5) with `--seats <task_seats only>`.

**3.2 — run EACH seat in its own parallel shell.** First, **clear any pre-existing
`<seat>.json`** for the seats you are about to run — the session dir
`.crew/reviews/<session-id>/` is REUSED across reviews, so a leftover file from an
EARLIER panel (or a seat whose shell you later kill) would be read by `collect` as
a FRESH result, folding stale data into this verdict. After clearing, a seat that
doesn't write a fresh file renders as SKIPPED, not stale-OK:

```bash
rm -f .crew/reviews/<session-id>/<seat>.json
```

Then, for every seat in `subprocess_seats`, launch a SEPARATE `crew run <seat>`
Bash call, all concurrently (e.g. background calls) so they are distinct shells you
can watch and kill individually. `--session-id` lets `run` DERIVE both paths from
`.crew/reviews/<session-id>/`: `-f` = `prompt-seat.txt`, `-o` = `<seat>.json`:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> --session-id <session-id> --json
```

- `run --json` ALWAYS exits 0 and writes the six-field result
  (`name, model, ok, output, error, elapsed`) — even a failed/skipped seat lands
  as `ok=False` with a diagnostic. So **per-seat never-choke is automatic**: one
  seat failing can't sink the others, there's no all-failed abort, and the Step
  5.5 collect reads EXACTLY the named seats, rendering a missing one as a labeled
  SKIPPED block.
- Same model / sandbox / auth-banner / ARG_MAX handling as the old single-call
  path — `run` dispatches through the identical provider machinery.

**3.3 — do NOT collect yet.** The WHOLE panel — subprocess AND Task seats — flows
through ONE grouped `collect` in **Step 5.5**, after the Task seats are persisted.
Launch the 3.2 shells (in parallel) and move to Step 4; Step 5.5 owns the
wait-for-both barrier and the single collect.

## Step 4 — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each entry in `task_seats`** (from the
`review-prep` JSON — skip if empty). Do NOT hand-write each seat's prompt:
**stage it from the engine** so every seat — subprocess AND Task — reviews ONE
identical target. `render` is the single prompt source and resolves the working-tree
target the SAME way the engine's `review` did (compound `Target.diff_cmd`, untracked
files included), so a Claude seat can't miss new files a subprocess seat sees. Stage
ALL Task seats in ONE `--stage-all` call over the SAME resolved `<TARGET>` (and
`--base "<BASE>"`) you passed the engine in Step 3, fed the **comma-joined
`task_seats`** from the prep JSON (NOT a hardcoded role list):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render "<TARGET>" --mode review --base "<BASE>" --stage-all <comma-joined task_seats from the review-prep JSON> --session-id <session-id>
```

**Skip this `--stage-all` call entirely if `task_seats` is empty** (a
subprocess-only panel like `--panel cursor`). A registered seat name is already
filename-safe (the `name == slug(name)` invariant means it can carry no `.`), so
each seat stages to `prompt-<seat>.txt` verbatim and the spawn below reads that
same file. (`<TARGET>` is the plan `.md` path or git scope from Step 1; `<BASE>` is
the Step 3 base — drop `--base` for a plan-file or working-tree target.)

`--stage-all` stages one LABELED prompt PER comma-listed role in a SINGLE call —
`.crew/reviews/<session-id>/prompt-<role>.txt` for each, each carrying its own
"acting as the **<role>** seat" label — and prints a JSON `{role: path}` map. It is
session-scoped so concurrent sessions never clobber each other. **Substitute your
actual session id for `<session-id>`** — the `[Session ID: …]` value (the same id
`crew state` uses). Pass it as a literal `--session-id` value — NOT the placeholder
text, and NOT a `${CLAUDE_SESSION_ID}` shell expansion (a `$…` expansion isn't
allowlistable and isn't reliably exported; the engine resolves the id itself, arg →
env). The engine **rejects an unsubstituted `<…>` placeholder** with a loud error,
so a missed substitution fails fast instead of silently sharing one dir. `.crew/`
is gitignored.

Then dispatch each seat **by reference** — the SAME agent (`crew:reviewer`) with a
per-spawn `model` override selecting the voice. **Iterate `task_seats` from the
prep JSON**; for each `<seat>` read its model from `task_seat_models[<seat>]`.
Do NOT hardcode seat names or model pins, and do NOT read + inline the staged
prompt — point the seat at its staged file and let it `Read` it (reference, not
payload; the reviewer has `Read`):

```
# for each <seat> in task_seats:
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read .crew/reviews/<session-id>/prompt-<seat>.txt and follow it exactly.")
```

The staged prompt already states plan-vs-code, tells the seat how to reach the
target itself (the plan file path, or the diff command — reference mode), lists the
criteria, and asks for per-criterion PASS/FAIL + `[BLOCKING]`/`[MINOR]` findings + a
one-line verdict. `crew:reviewer` has `Read, Grep, Glob, Bash` for exactly this
(read-only git/inspection). Because every seat's prompt comes from the one `render`
source, the panel reviews a single identical target.

> **The Task RESULT is the only completion signal.** Each `Task(...)` call RETURNS
> the seat's final message as its tool result — that returned text IS the seat's
> review *and* the proof it finished.
> **NEVER judge a seat by a proxy** — its output-file byte size, transcript length, a notification
> you think you missed, or elapsed time. Those race the transcript flush and have
> falsely declared a finished seat "dead" while it was still writing a complete
> review, throwing away good work. A seat that has not returned yet is **still
> running, not failed** — wait for its result. Mark a seat `ok=False` ONLY when its
> Task actually returns an error, returns no usable review block, or the harness
> itself reports it failed/missing.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors, times
out, returns no usable block, or is reported missing/failed MUST NOT abort the
review. Catch it, normalize it to the six-field `ok=False` shape (Step 5), and
continue. A failed Task seat is treated identically to a failed subprocess seat.

## Step 5 — Normalize Task-seat results to the six-field shape

For each Task seat **in `task_seats`** (from the `review-prep` JSON — NOT a
hardcoded `opus`/`sonnet` pair), normalize its return into the SAME six fields as
the subprocess results:

- `name` = the seat's name (a registered seat name is already `[A-Za-z0-9_-]`-only
  by the `name == slug(name)` invariant, so it doubles as its own filename stem).
- `model` = the pinned model from `task_seat_models[<seat>]` (e.g. `opus`,
  `sonnet`, `fable`).
- `ok` = True if the seat's **returned result** is a usable review block; **False**
  only if it is an error, has no usable block, or the harness reports the Task
  failed/missing. (Judge from the returned result — never a file size or proxy.)
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's review text, from its returned Task result.

A failed Task seat renders exactly like a failed subprocess seat and never
suppresses the others. A skipped/unspawnable seat renders a clearly-marked skipped
block and is excluded from the verdict math.

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

The engine derives the filename and the six-field shape itself — the
printed path is the `<seat>.json` the Step-5.5 collect reads. The seat entry you
pass to `collect --seats` is the seat name (the filename stem the
engine printed).

## Step 5.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every Step 3.2
subprocess `crew run <seat>` shell to EXIT, AND (b) every Step 4 Task seat to return
AND be persisted in Step 5. A seat whose `<seat>.json` is not written yet is STILL
RUNNING, not skipped — collecting early would silently drop it.

Define `<ran_seats>` = the comma-join of `subprocess_seats` (resolved prep order)
**then** `task_seats`. In the Claude-only branch
(`subprocess_seats` empty) `<ran_seats>` is just `task_seats`.

**Repair non-compliant seats (per-seat haiku reformat).** A seat that ignored the
structured `## FINDINGS` schema can't be grouped — give it ONE cheap repair pass.
Ask the engine which seats did not parse:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --report-unparsed
```

This prints the seat names (one per line) whose output ran but did NOT parse into
structured findings. For EACH such `<seat>`:

1. **Read** its raw output — `.crew/reviews/<session-id>/<seat>.json` — and take
   the `output` field.
2. Spawn the cheap **formatter** agent to reformat that text into the schema (a
   FAITHFUL transform — do NOT invent, drop, or re-judge findings, only reshape):

   ```
   Task(subagent_type="crew:formatter", model="haiku",
        prompt="Reformat this review into the FINDINGS schema verbatim — do not
   invent or drop findings, only reformat:\n\n<the seat's raw output field>")
   ```

3. Write the formatter's returned text into the seat file with the engine (it
   writes to a SEPARATE `repaired_output` field, NEVER overwriting the original
   `output`) — via a temp file with `-f`:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/crew" repair-seat --seat .crew/reviews/<session-id>/<seat>.json -f <temp-file-with-reformatted-text>
   ```

`repair-seat` is **non-destructive**: it populates `repaired_output` ONLY IF the
reformatted text parses into the FINDINGS schema. If it STILL doesn't parse, the
write is a **no-op** — `repaired_output` is left unset, a stderr note says the
original was kept, and the command exits 0 (a kept-original no-op is success; exit 2
= real usage/read error). Grouping uses `repaired_output` when present, while
`--full` and the RAW section ALWAYS render the original `output`, so a degraded
haiku rewrite can NEVER overwrite the seat's genuine review: `collect` renders the
ORIGINAL text raw (never worse than the seat's real words) and `panel-full.md` keeps
it verbatim. Either way, continue — the file is correct. The formatter is an
in-session `haiku` Task seat — no `claude -p`, no API key.

**Collect ONCE — grouped + faithful:**

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --group -o .crew/reviews/<session-id>/panel.md --full .crew/reviews/<session-id>/panel-full.md
```

`collect` reads EXACTLY the named seats (never globs — a stale/foreign file is never
folded in). `--group` writes the deduped digest to `panel.md`; `--full` writes the
faithful per-seat concatenation to `panel-full.md` (the recovery artifact — you do
NOT read it unless you need a seat's full block). **Read `panel.md` once** and carry
that ONE digest into Step 6 — do NOT also carry the raw Task-seat blocks into context.

## Step 6 — Synthesize the verdict

Read the ONE grouped `panel.md` digest — it already folds the WHOLE panel — and emit
the existing verdict format:

- **Plan target** → `APPROVED` / `REVISE` (with `[BLOCKING]`/`[MINOR]` items).
- **Code target** → findings tagged `[BLOCKING]`/`[MINOR]` + a summary verdict.

`panel.md` has four sections, read them all:

- **VERDICTS** — the roster of every seat that RAN, each with its verdict (or
  `(unparsed)`/`(skipped)`) + a tally. Reference seats by LABEL ("the codex
  verdict", "the cursor-auto block"), never positionally.
- **CRITERIA MATRIX** — per-criterion PASS/FAIL/`?` per seat.
- **GROUPED FINDINGS** — each finding ONCE, with an `M/N` agreement count (N =
  findings-parsed seats, distinct from the VERDICTS roster count). A finding marked
  **`⚠ SINGLETON`** is a lone dissent — weigh it ON ITS MERITS, never dismiss it
  because only one seat raised it (a real blocker often starts as one seat's finding).
- **RAW / UNPARSED SEATS** — any seat whose findings could not be parsed even after
  repair, rendered verbatim. **Still read these** — a partial seat's prose findings
  live here and must not be ignored.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the review and is NEVER silently dropped (it shows
in the VERDICTS roster and/or the RAW section):

- Synthesize the verdict from the seats that produced usable output.
- ONLY when **zero** seats produced usable output do you skip the verdict and
  report: `could not review — all seats failed: <per-seat diagnostics>`.

---

Subscription safety: the engine drives only external-CLI subprocess seats (all
external-CLI auth); Claude voices are in-session Task seats. No `claude -p`, no
Anthropic API — a stray `ANTHROPIC_API_KEY` is irrelevant.
