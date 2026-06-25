---
description: Crew-native multi-model debate on a question — single round (council) or multi-round with rebuttals; default panel codex + cursor-gemini + cursor-glm + cursor-composer + opus + sonnet; narrow with --panel/--seats
argument-hint: "[--rounds N] [--panel ...] <question>"
allowed-tools: Bash, Task, Read, Glob, Write
---

[CREW-NATIVE DEBATE]

$ARGUMENTS

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the panelist /
> reviewer agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox
> flags.

## Options (all optional; flags go at the START of `$ARGUMENTS`)

- **Rounds** — `--rounds N` (1–5, default **1**). `--rounds 1` is a single-round
  council; `N>1` runs a multi-round debate where each round sees the prior
  round's positions and may rebut/revise.
- **Panel** — `--panel full` =
  `codex,cursor-gemini,cursor-glm,cursor-composer,opus,sonnet` (default) ·
  `--panel lite` = `opus,sonnet` · `--panel solo` = `opus` · `--seats <list>` =
  an explicit comma-list of any registered seat
  (`codex, agy, cursor-gemini, cursor-glm, cursor-composer, opus, sonnet`;
  `agy` is opt-in — works via `--seats agy` but is not in the default panel).
  `--seats` wins over `--panel`.
- **Mode** — inferred, not flagged: a free-form **question** → **discuss** mode
  (seats give a take; dispatch `crew:panelist`). An argument naming a **diff /
  branch / plan `.md`** → **review** mode (seats score it; dispatch
  `crew:reviewer`). When in doubt it's discuss. Review mode is single-round
  (use `/crew:review` for the full review verdict); multi-round is discuss only.

> **Question-file naming (one convention):** both paths write the question to
> **`question.md`** inside the debate dir. Single-round (section A) uses the
> engine `debate` subcommand, which writes `question.md` into a
> `<timestamp>-<slug>/` dir. Multi-round (section B) writes `question.md` into a
> `run-<id>/` dir — the filename + dir layout the `rounds.py` reader
> (`render --run-id`) expects.

Resolve the flags to a **seat list**, strip them from `$ARGUMENTS` (the remainder
is the question/target), then split the seat list: the `codex`/`cursor-*` entries
(and opt-in `agy`) are **subprocess seats** (the Python engine); `opus`/`sonnet`
are **task seats** (`crew:panelist`
for discuss / `crew:reviewer` for review, via the Task tool — in-session, on the
subscription, no `claude -p`, no API key). Seat the models in the resolved list
only. A failed/skipped seat NEVER sinks the debate (see "Never choke" below) — the
synthesis is built from whichever seats succeed; only an all-empty panel aborts.

> **Reference code, don't paste it.** Every seat runs in the repo. If the
> question is about a diff/branch/files, point seats at it (*"run `git diff
> main...HEAD` and read the changed files"*) rather than inlining a big diff —
> smaller prompts, no agy ARG_MAX cap, no synthesis-context bloat.

> **The one prompt source.** Every seat's prompt — subprocess AND task, every
> round — is built by the engine's `render` subcommand (which calls
> `prompts.build_prompt`/`council`). NEVER hand-write a seat's prompt: render it,
> so all seats see byte-identical material and prior-round context.

---

# A) Single round (`--rounds 1`, the default)

## A1 — Scaffold + fan out subprocess seats (one call)

Write the question to a staging file with the **Write tool** (robust for quotes /
newlines), then run the engine `debate` subcommand over it (scaffolds the dir AND
fans out the subprocess seats in one allowlistable call — no
`mkdir`/heredoc/redirect):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" debate -f .crew/debates/staged-question-<slug>.txt --slug <short-kebab-slug> --seats <resolved codex/cursor-* seats, or none> --consume
```

`--seats` carries the resolved subprocess seats (the `codex`/`cursor-*` entries,
plus opt-in `agy`); pass **`none`** for a
Claude-only panel (`--panel lite`/`solo`) so the dir is still scaffolded with no
subprocess seats run. `--consume` deletes the staging file after the question is
copied in. The subcommand creates `.crew/debates/<timestamp>-<slug>/`, writes
`question.md` + `subprocess.json` (six-field results), and prints a JSON summary
with **`dir`** — read it; that's where the Claude-seat outputs + synthesis go.

## A2 — Fan out task seats (parallel)

For each `opus`/`sonnet` entry in the panel, render its prompt from the ONE
builder, then dispatch the discuss seat (`crew:panelist`) in parallel:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render --mode discuss --seat-role opus -f .crew/debates/<dir>/question.md -o .crew/debates/<dir>/.prompt-opus.txt
```

```
Task(subagent_type="crew:panelist", model="opus",   prompt="<contents of .prompt-opus.txt>")
Task(subagent_type="crew:panelist", model="sonnet", prompt="<contents of .prompt-sonnet.txt>")
```

(Review mode: `--mode review <target>` instead of `-f question`, and dispatch
`crew:reviewer`. The subprocess seats then use `cli.py review` rather than
`debate` — but most debates are discuss.)

## A3 — Normalize + synthesize + log

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS the
> seat's final message — that returned text IS the seat's take *and* the proof it
> finished. Take `ok`/`output` from the returned result. **NEVER judge a seat by
> a proxy** (output-file byte size, transcript length, a missed notification,
> elapsed time): those race the transcript flush and have falsely declared a
> finished seat "dead", discarding good work. A seat that has not returned is
> still running, not failed — wait for it. `ok=False` ONLY when the Task itself
> returns an error / no usable block, or the harness reports it failed.

Normalize each task seat to the six-field shape (`name, model, ok, output, error,
elapsed`); a failed/skipped seat renders like a failed subprocess seat and never
suppresses the others. Render the full panel side-by-side, then synthesize:
**Areas of agreement / Key disagreements / Recommendation** (discuss), or the
`APPROVED`/`REVISE` + `[BLOCKING]`/`[MINOR]` verdict (review). With the **Write
tool**, log the seat outputs (`opus.md`, `sonnet.md`) and `synthesis.md` into the
debate `dir`. Tell the user the path.

---

# B) Multi-round (`--rounds N`, N > 1) — discuss mode

Multi-round needs **no new engine call** — it composes `render` (which folds the
prior round in as DATA) + `run` + the run-dir convention + the Write tool.

## B1 — Open the run

Pick a run-id `run-<short-slug>` (must match `^run-[A-Za-z0-9_-]+$`). The slug
must use ONLY `[A-Za-z0-9_-]` characters — no spaces, dots, slashes, or other
punctuation — else the first `render --run-id` call fails with a `RoundError`
(that's `rounds.py`'s `^run-[A-Za-z0-9_-]+$` traversal guard rejecting it). Write
the question once with the **Write tool** to:

```
.crew/debates/<run-id>/question.md
```

(Writing the file creates the run dir. This is the dir `render --run-id`
reads prior rounds from.)

## B2 — For each round n = 1 … N

**Render each seat's prompt** (the engine reads rounds `1…n-1` from the run dir
and folds them in as injection-guarded DATA — on round 1 there is no prior):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render --mode discuss --seat-role <seat> \
  -f .crew/debates/<run-id>/question.md \
  --run-id <run-id> --round <n> --base-dir .crew/debates \
  -o .crew/debates/<run-id>/.prompt-<seat>-r<n>.txt
```

Then run the round's seats (in parallel where possible):

- **Subprocess seats** (the `codex`/`cursor-*` entries, plus opt-in `agy`): execute the rendered prompt via the engine —
  ```bash
  python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" run <seat> -f .crew/debates/<run-id>/.prompt-<seat>-r<n>.txt --json -o .crew/debates/<run-id>/<seat>-r<n>.json
  ```
- **Task seats** (`opus`/`sonnet`): `Task(subagent_type="crew:panelist", model="<seat>", prompt="<contents of .prompt-<seat>-r<n>.txt>")`. Use each Task's RETURNED result as its take and completion signal — never an output-file size or other proxy (see A3).

**Record the round.** Normalize every seat to the six-field shape, then with the
**Write tool** write all seats' positions for this round into — wait for every
seat's Task result first; a seat still running is not a failed seat:

```
.crew/debates/<run-id>/round-<NN>.md      (zero-padded: round-01.md, round-02.md, …)
```

This file is what the NEXT round's `render` folds in — so it must contain each
seat's take, labeled by seat name. Use the same `round-NN.md` name the engine's
`rounds.py` expects (zero-padded to two digits).

**Convergence check.** After writing `round-NN.md`, judge whether the seats have
converged (positions stable, no new substantive disagreement). If converged — or
n == N — stop. Otherwise continue to round n+1. Note in the synthesis if you
stopped early on convergence.

## B3 — Synthesize the debate

Read the final round (and the trajectory across rounds) and write `synthesis.md`
into the run dir with: **How positions evolved / Areas of agreement / Remaining
disagreements / Recommendation**. Tell the user the run-dir path.

---

## Never choke (both modes)

A failed/skipped seat — subprocess OR task, any round — NEVER aborts the debate
and is NEVER silently dropped:

- The engine never raises out of a fan-out: a failed/skipped subprocess seat is an
  `ok=False` entry with a diagnostic; the others still return. A nonzero engine
  exit (every subprocess seat failed) still emits the full JSON — fold the task
  seats in and continue.
- A `crew:panelist`/`crew:reviewer` Task that errors, times out, or returns no
  usable block is normalized to an `ok=False` six-field entry and rendered with
  its diagnostic; the other seats proceed.
- In multi-round, a seat that failed in round n can still participate in round
  n+1 (it's re-rendered + re-dispatched fresh).
- ONLY when **zero** seats in the whole panel produced usable output do you skip
  the synthesis and report:
  `could not convene — all seats failed: <per-seat diagnostics>`.

---

Subscription safety: the engine drives only the subprocess seats — `codex` + the
`cursor-*` seats (plus opt-in `agy`), all external-CLI auth; Claude voices are
in-session Task seats. No `claude -p`, no Anthropic API — a stray
`ANTHROPIC_API_KEY` is irrelevant here. This council is fully self-contained in
crew — NEVER call `agy -p` / `codex exec` / `cursor-agent` directly, and NEVER
hand off to any external debate plugin/shell.
