---
description: Crew-native multi-model debate on a question — single round (council) or multi-round with rebuttals across a config-aware panel; override with --panel/--seats. Use for open-ended discussion, not plan/code review.
argument-hint: "[--rounds N] [--panel ...] <question>"
allowed-tools: Bash, Task, Read, Glob, Write
---

[CREW-NATIVE DEBATE]

$ARGUMENTS

> **`allowed-tools` scopes THIS orchestrator only.** Seat tool access is
> governed per-seat by the panelist / reviewer agent frontmatter and the
> engine's sandbox flags.

## Conventions (stated once, apply throughout)

- **No shell expansions in recipes.** No `${…}`, `$(…)`, or backticks on any
  Bash line; the `${CLAUDE_PLUGIN_ROOT}` prefix is exempt. The ENGINE anchors
  relative `.crew/...` args to the project root, and Write-tool / Task `Read`
  references use the printed ABSOLUTE paths verbatim.
- **Reference code, don't paste it.** Every seat runs in the repo: point seats
  at a diff, branch, or file instead of inlining a large diff.
- **The one prompt source.** Every seat's prompt, subprocess AND task, every
  round, is built by the engine's `render` subcommand. NEVER hand-write a
  seat's prompt: render it so all seats see byte-identical material.

## Options (all optional; flags go at the START of `$ARGUMENTS`)

- **Rounds**: `--rounds N` (1-5, default **1**). 1 = single-round council;
  N>1 = multi-round debate where each round sees the prior round's positions
  and may rebut/revise.
- **Panel**: `--panel <name>` = a named preset (`full`, `lite`, `solo`,
  `quick`, `cursor` = every registered cursor-* seat, engine-expanded) or any
  custom `[panels]` roster in config. Print a preset's roster with
  `"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --panel <name>`.
  `--seats <list>` = an explicit comma-list of any registered seat (e.g.
  `--seats codex,opus`); it wins over `--panel`. Some seats are opt-in and
  never in a built-in default panel (the premium `fable` Claude voice among
  them): pass their exact names via `--seats`.
- **Config-aware default**: when you name NEITHER flag, the panel is NOT
  hard-`full`; the engine resolves `CLI flag > [debate].panel > default_panel
  > built-in full`, per-repo config over global at each tier. So a repo can
  default its debates fuller than its reviews.
- **Mode**: inferred, not flagged. A free-form **question** = **discuss** mode
  (seats give a take; dispatch `crew:panelist`). A **diff / branch / plan
  `.md`** = **review** mode (seats score it; dispatch `crew:reviewer`;
  single-round only; use `/crew:review` for the full review verdict). When in
  doubt it's discuss.

Resolve the flags to the **roster split**, strip them from `$ARGUMENTS` (the
remainder is the question/target). **When the user named NEITHER `--panel` NOR
`--seats`, do NOT assume `full`**: ask the engine for the config-aware split.
`--json` returns the panel ALREADY split, the same split fields the review
flows consume, so this command never classifies a seat name or hardcodes a
model pin:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --json
```

When the user DID name a panel, pass it through so the explicit choice wins:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --panel <preset> --json   # explicit preset
"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --seats <list> --json     # explicit seat list
```

The split: **`subprocess_seats`** (external CLI; one `crew run <seat>` each),
**`task_seats`** (native via Task, `crew:panelist`/`crew:reviewer`), and
**`task_seat_models`** (catalog pin; spawn with `model =
task_seat_models[<seat>]`). Native Claude seats use subscription; other hosts
resolve Claude seats to external `subprocess_seats`.
PRINTS `{subprocess_seats, task_seats, task_seat_models, host, seat_channels}` as one-line JSON.

Seat only the models in this split. For `--panel cursor`, `task_seats` is
empty and the Task-seats step is SKIPPED. A failed/skipped seat NEVER sinks
the debate (see "Never choke"); only an all-empty panel aborts.

> **Question-file naming:** both paths write the question to **`question.md`**.
> The `debate` subcommand is **ALWAYS scaffold-only**: it writes `question.md`;
> it writes empty `subprocess.json`; it never runs subprocess seats; A2 visibly
> fans them out. Multi-round writes `question.md` in `run-<id>/` for `rounds.py`.

---

# A) Single round (`--rounds 1`, the default)

## A1 — Scaffold the debate dir (one allowlistable call)

Write the question with the **Write tool** (robust for quotes/newlines) to the
ABSOLUTE path `<project-root>/.crew/debates/staged-question-<slug>.txt`
(substitute your `CLAUDE_PROJECT_DIR` value). Then scaffold with **`--seats
none`** (one allowlistable call, no `mkdir`/heredoc/redirect):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" debate -f ".crew/debates/staged-question-<slug>.txt" --slug <short-kebab-slug> --seats none --consume
```

**Always `--seats none` here**: `debate` is scaffold-only regardless of
`--seats`; a non-empty value prints a stderr advisory, so `none` is the
advisory-free form. A Claude-only panel uses the identical call and simply skips A2.
`--consume` deletes the staging file; the unlink is best-effort and silent,
so a leftover staging file is cosmetic.

The subcommand creates `.crew/debates/<timestamp>-<slug>/`, writes `question.md`
plus empty `subprocess.json`, and prints JSON with **`dir`** (ABSOLUTE,
engine-anchored). Use the printed ABSOLUTE `dir` verbatim as `<dir>` for
Write/Task `Read`; its basename is `<dir-name>`, which Bash uses as the RELATIVE
`.crew/debates/<dir-name>/...`.

## A2 — Fan out subprocess seats (one visible shell PER SEAT)

**Skip this step if the resolved panel has no subprocess seat** (e.g. `--panel
lite`/`solo`). `subprocess_seats` from the split is ALREADY group-expanded:
just loop over it.

**A2.1 — render each seat's prompt.** Discuss-mode council prompts carry a
per-seat label ("acting as the **<seat>** seat"), so render ONE prompt per
seat from the single builder (for a review-mode debate, render
`--mode review <target>` instead):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role <seat> -f ".crew/debates/<dir-name>/question.md" -o ".crew/debates/<dir-name>/.prompt-<seat>.txt"
```

**A2.2 — run EACH seat in its own parallel shell.** One SEPARATE `crew run
<seat>` Bash call per seat, all concurrently:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> -f ".crew/debates/<dir-name>/.prompt-<seat>.txt" --json -o ".crew/debates/<dir-name>/<seat>.json"
```

`run --json` ALWAYS exits 0 and writes the six-field core plus optional
`channel` provenance; a failed/skipped seat lands as `ok=False` with a
diagnostic. WAIT for every shell (a seat whose `-o` file hasn't appeared is
still running, not failed), then read each `<seat>.json` into A4.

## A3 — Fan out task seats (parallel)

> **Claude Code host ONLY.** Native Task seats apply on Claude Code. On another
> host, `seats --debate` resolves Claude seats into external `subprocess_seats`;
> do NOT emulate these seats with host-native subagents. A missing external CLI
> becomes a named skipped result and the other seats continue.

**Skip this step if `task_seats` is empty** (e.g. `--panel cursor`). For EACH
`<seat>` in `task_seats`, render its prompt (one render line per Task seat),
then dispatch in parallel:

```bash
# for each <seat> in task_seats:
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role <seat> -f ".crew/debates/<dir-name>/question.md" -o ".crew/debates/<dir-name>/.prompt-<seat>.txt"
```

Dispatch each seat **by reference** (the panelist has `Read`), pinning the
model from `task_seat_models[<seat>]`:

```
# for each <seat> in task_seats:
Task(subagent_type="crew:panelist", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read <dir>/.prompt-<seat>.txt and follow it exactly.")
```

(Review mode: render `--mode review <target>` and dispatch `crew:reviewer`
instead. Most debates are discuss.)

> Why `-o`: debate's `.crew/debates/...` dir holds question, per-seat results,
> and synthesis; its OWN run-id threads prior rounds through `render --run-id`.
> The two staging schemes are intentional, not an inconsistency to "fix".

## A4 — Normalize + synthesize + log

> Spawn EXACTLY as written: a bare one-shot `Task(...)`, NO `name` argument.
> (A named Task delivers only via `SendMessage` and strands the debate.)
> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS the
> seat's final message; that returned text IS the seat's take. **NEVER judge a
> seat by a proxy** (output-file byte size, transcript length, notification, or
> elapsed time).
> A seat that has not returned is still running, not failed. A Task error, timeout,
> or unusable block is a failed seat, never a reason to abort the debate.

Gather every seat into the six-field core shape
(`name, model, ok, output, error, elapsed`): subprocess seats from their
`<dir>/<seat>.json` (**IGNORE the empty `subprocess.json` scaffold**), Task
seats normalized from returned results. A failed/skipped seat renders with its
diagnostic and never suppresses the others. Render the panel side-by-side,
then synthesize: **Areas of agreement / Key disagreements / Recommendation**
(discuss), or the `APPROVED`/`REVISE` + `[BLOCKING]`/`[MINOR]` verdict (review).
With the Write tool, log each seat's output (at least one logged file per
Task-seat take) plus `synthesis.md` into the debate `dir`. Tell the user the path.

---

# B) Multi-round (`--rounds N`, N > 1) — discuss mode

Multi-round composes render/run, run-dir, and Write; no new engine call.

## B1 — Open the run

Pick a run-id `run-<short-slug>` matching `^run-[A-Za-z0-9_-]+$` (any other
character makes the first `render --run-id` fail with a `RoundError`). Define
ONE absolute run dir for every read, write, and Task reference:

```
<run-dir> = <project-root>/.crew/debates/<run-id>
```

Bash uses relative `.crew/debates/<run-id>/...`; engine resolves prior
rounds from that root. Write question with **Write** to `<run-dir>/question.md`.

## B2 — For each round n = 1 … N

**Render each seat's prompt** (the engine reads rounds `1…n-1`; round 1 has no
prior). Render EVERY seat in the panel, subprocess AND task; omit `--base-dir`:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role <seat> \
  -f ".crew/debates/<run-id>/question.md" \
  --run-id <run-id> --round <n> \
  -o ".crew/debates/<run-id>/.prompt-<seat>-r<n>.txt"
```

Then run the round's seats (in parallel where possible):

- **Subprocess seats**:
  ```bash
  "${CLAUDE_PLUGIN_ROOT}/crew" run <seat> -f ".crew/debates/<run-id>/.prompt-<seat>-r<n>.txt" --json -o ".crew/debates/<run-id>/<seat>-r<n>.json"
  ```
- **Task seats**: one `crew:panelist` per seat, **pinned from
  `task_seat_models[<seat>]`**, by reference, exactly as A3. A Task seat appears
  only when the current host resolves it native; on another host, the
  `seats --debate` split places it in the external subprocess list instead.
  Never emulate an external seat with a host-native subagent:
  ```
  # for each <seat> in task_seats:
  Task(subagent_type="crew:panelist", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read <run-dir>/.prompt-<seat>-r<n>.txt and follow it exactly.")
  ```
  Use each Task's RETURNED result as its take and completion signal (see A4).

**Record the round.** Wait for every seat, normalize to the six-field core plus
optional channel provenance; use **Write** to write all seats' positions,
labeled by seat name, into:

```
<run-dir>/round-<NN>.md      (zero-padded: round-01.md, round-02.md, …)
```

That file is what the NEXT round's `render` folds in (`rounds.py` expects the
zero-padded `round-NN.md` name).

**Convergence check.** After writing `round-NN.md`, stop if positions have
converged (stable, no new substantive disagreement) or n == N; otherwise
continue to round n+1. Note early convergence in the synthesis.

## B3 — Synthesize the debate

Read the final round (and the trajectory) and write `synthesis.md` into the
run dir: **How positions evolved / Areas of agreement / Remaining
disagreements / Recommendation**. Tell the user the run-dir path.

---

## Never choke (both modes)

A failed/skipped seat, subprocess OR task, in any round, NEVER aborts the debate
and is NEVER silently dropped:

- Subprocess seats run one-per-seat via `run --json` (always exit 0, six-field
  core plus optional channel provenance); a failed seat lands as `ok=False`,
  the others are unaffected. Read whichever `<seat>.json` files appear.
- A `crew:panelist`/`crew:reviewer` Task that errors, times out, or returns no
  usable block is normalized to `ok=False` and rendered with its diagnostic;
  the other seats proceed.
- In multi-round, a seat that failed in round n can still participate in
  round n+1 (re-rendered + re-dispatched fresh).
- ONLY when **zero** seats produced usable output, skip the synthesis and
  report: `could not convene — all seats failed: <per-seat diagnostics>`.

---

Subscription safety: on a Claude Code host, native Claude voices are in-session
Task seats, using no `claude -p` or Anthropic API. On other hosts, the engine
may drive external `claude` CLI; a missing CLI is a named skipped result, never
a host-native emulation. A stray `ANTHROPIC_API_KEY` is irrelevant to the NATIVE path.
This council is fully self-contained in crew: NEVER call `agy -p` / `codex exec`
/ `cursor-agent` directly, and NEVER hand off to any external debate
plugin/shell.
