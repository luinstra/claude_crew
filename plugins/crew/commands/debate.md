---
description: Crew-native multi-model debate on a question — single round (council) or multi-round with rebuttals across a config-aware panel; override with --panel/--seats. Use for open-ended discussion, not plan/code review.
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
  `codex,agy,cursor-auto,cursor-composer,opus,sonnet` (the built-in fallback) ·
  `--panel lite` = `opus,sonnet` · `--panel solo` = `opus` · `--panel quick` =
  `codex,sonnet` (the cheapest cross-model pair, good for routine questions) ·
  `--panel cursor` =
  all Cursor model-seats (`--seats cursor`, which the engine expands to every
  registered cursor-* seat — cursor-gpt, cursor-gemini, cursor-glm, cursor-auto,
  cursor-composer, and any future ones); a pure cross-model Cursor panel — NO
  codex, NO opus/sonnet Task seats · `--seats <list>` =
  an explicit comma-list of any registered seat
  (`codex, agy, cursor-gpt, cursor-gemini, cursor-glm, cursor-auto, cursor-composer, opus, sonnet`;
  `cursor-gpt`, `cursor-gemini`, and `cursor-glm` are opt-in — work via `--seats …` / `--panel cursor`
  but are not in the default panel; `codex` covers the GPT lineage and `agy` covers the Gemini lineage flat-rate).
  `--panel <name>` also accepts any **custom roster** defined under `[panels]`
  in per-repo or global config (e.g. `--panel nightly`). `--seats` wins over `--panel`.
- **Config-aware default** — when you name NEITHER `--panel` NOR `--seats`, the
  panel is NOT hard-`full`: it is resolved by the engine from config across two
  tiers — per-repo `.crew/config.toml` then global `~/.crew-config.toml` (see the
  flag-resolution step below). Debate precedence is `CLI --panel/--seats`
  (explicit, wins) > `[debate].panel` (debate-only override) > `default_panel`
  (the shared review/build/measure-twice default) > the built-in `full`, with
  per-repo config taking precedence over global at each tier (no env tier). So a
  repo can default its debates fuller than its reviews — debate's value is
  cross-model diversity. An explicit `--panel`/`--seats` always wins, exactly as
  before.
- **Opt-in `fable` Claude seat** — a premium Mythos-class Claude voice
  (`model="fable"`, a first-class alias like opus/sonnet). NOT in any default so
  a routine debate never silently spends the premium tier; add it explicitly
  (e.g. `--seats opus,sonnet,fable`) for the hardest questions.
- **Mode** — inferred, not flagged: a free-form **question** → **discuss** mode
  (seats give a take; dispatch `crew:panelist`). An argument naming a **diff /
  branch / plan `.md`** → **review** mode (seats score it; dispatch
  `crew:reviewer`). When in doubt it's discuss. Review mode is single-round
  (use `/crew:review` for the full review verdict); multi-round is discuss only.

> **Question-file naming (one convention):** both paths write the question to
> **`question.md`** inside the debate dir. Single-round (section A) uses the
> engine `debate` subcommand, which is **ALWAYS scaffold-only** — it writes
> `question.md` + an empty `subprocess.json` into a `<timestamp>-<slug>/` dir and
> NEVER runs subprocess seats internally, regardless of `--seats` (the shipped
> call passes `--seats none`; A2 fans the subprocess seats out per-seat in
> visible, killable shells). Multi-round (section B) writes `question.md` into a
> `run-<id>/` dir — the filename + dir layout the `rounds.py` reader
> (`render --run-id`) expects.

Resolve the flags to a **seat list**, strip them from `$ARGUMENTS` (the remainder
is the question/target). **When the user named NEITHER `--panel` NOR `--seats`,
do NOT assume `full`** — ask the engine for the config-aware default seat list
(it reads `.crew/config.toml`, which the markdown cannot, and applies the debate
precedence `[debate].panel` → `default_panel` → built-in `full`):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate
```

When the user DID name `--panel`/`--seats`, pass it through so the explicit
choice wins (`--seats` over `--panel`):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --panel <preset>     # explicit preset
"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --seats <list>       # explicit seat list
```

`seats --debate` prints the FULL resolved panel — one seat per line, group
tokens (`cursor`) already expanded, including the Claude voices — so it is the
single seat list to split. Then split it: the `codex`/`agy`/`cursor-*`
entries are **subprocess seats** (the Python engine); `opus`/`sonnet`/`fable`
are **task seats** (`crew:panelist`
for discuss / `crew:reviewer` for review, via the Task tool — in-session, on the
subscription, no `claude -p`, no API key; every current Task seat pins its own
name as the model, e.g. `fable` → `model="fable"`).
Seat the models in the resolved list only. For `--panel cursor` the subprocess
seats are all cursor-* (pass `--seats cursor` to the engine, which expands it to
every cursor-* seat) and the Task-seats step is SKIPPED (no Claude Task seats). A failed/skipped seat NEVER sinks the debate (see "Never choke" below) — the
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

## A1 — Scaffold the debate dir (one allowlistable call)

Write the question to a staging file with the **Write tool** (robust for quotes /
newlines), then run the engine `debate` subcommand with **`--seats none`** to
scaffold the dir and copy the question into `question.md` — WITHOUT running any
subprocess seats (the per-seat fan-out happens visibly in A2). One allowlistable
call, no `mkdir`/heredoc/redirect:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" debate -f .crew/debates/staged-question-<slug>.txt --slug <short-kebab-slug> --seats none --consume
```

**Always `--seats none` here** — though `debate` is ALWAYS scaffold-only and
never runs subprocess seats internally regardless of `--seats` (a non-empty
`--seats` just prints a stderr advisory and still scaffolds), so passing `none`
is the explicit, advisory-free form. Every subprocess seat is instead launched in
its own visible shell in A2 (so a `--panel lite`/`solo` Claude-only panel uses the
identical A1 call and simply skips A2). `--consume` deletes the staging file after
the question is copied in. The subcommand creates `.crew/debates/<timestamp>-<slug>/`,
writes `question.md` (and an always-empty `subprocess.json`), and prints a JSON
summary with **`dir`** and `seats: []` — read it; that's the debate dir where
every per-seat result + the synthesis go.

## A2 — Fan out subprocess seats (one visible shell PER SEAT)

**Skip this step if the resolved panel has no subprocess seat** (e.g.
`--panel lite`/`solo`) — go straight to A3. Otherwise fan the subprocess seats out
**one `crew run` call per seat, in parallel** — each a separate, visible,
individually-killable shell (the same per-seat shape Section B's multi-round path
and `/crew:review` Step 3 use, instead of one opaque `debate` call hiding them in
an internal thread pool). Use the bare-script path (its top-of-file `sys.path`
guard makes package imports resolve).

**A2.0 — isolate the subprocess seats.** The `seats --debate` output you split
above is ALREADY group-expanded (the engine expands `cursor` → every registered
`cursor-*` seat before printing), so the `codex`/`agy`/`cursor-*` entries are
already concrete seat names — just take those entries from the resolved panel and
loop. No second expansion call is needed. (If you ever need to re-expand a bare
group token in isolation — e.g. you only have `--panel cursor` and not the
`--debate` output — `crew seats --seats cursor` expands it to every registered
`cursor-*` seat, keeping the registry authoritative with no hardcoded cursor list
in this command.)

**A2.1 — render each seat's prompt.** Discuss-mode council prompts carry a
per-seat label, so render ONE prompt per seat from the single builder (the same
shape Section B uses, minus the `--run-id`/`--round` round-threading — a single
round has no prior):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role <seat> -f .crew/debates/<dir>/question.md -o .crew/debates/<dir>/.prompt-<seat>.txt
```

This labels each subprocess seat ("acting as the **<seat>** seat") — a deliberate
parity improvement over the old bundled `debate` call, which ran one shared
*unlabeled* council prompt for all seats. (For a review-mode debate, render
`--mode review <target>` here instead of `--mode discuss` — same as A3's note for
the Task seats.)

**A2.2 — run EACH seat in its own parallel shell.** For every seat from A2.0,
launch a SEPARATE `crew run <seat>` Bash call, all concurrently (e.g. background
calls) so they are distinct shells you can watch and kill individually:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> -f .crew/debates/<dir>/.prompt-<seat>.txt --json -o .crew/debates/<dir>/<seat>.json
```

- `run --json` ALWAYS exits 0 and writes the six-field result
  (`name, model, ok, output, error, elapsed`) — even a failed/skipped seat lands
  as `ok=False` with a diagnostic. So **per-seat never-choke is automatic**: one
  seat failing can't sink the others, and there is no all-failed abort to handle —
  you read whichever `<seat>.json` files appear.
- Same model / sandbox / auth-banner / ARG_MAX handling as the old single-call
  `debate` fan-out — `run` dispatches through the identical provider machinery.

**A2.3 — collect.** WAIT for every A2.2 `run` shell to exit first — a seat whose
`-o` file hasn't been written yet is still running, not failed (the same wait
discipline as the Task seats in A3/A4). Then read each
`.crew/debates/<dir>/<seat>.json` (one six-field result apiece) and carry them
into A4 alongside the Task seats.

## A3 — Fan out task seats (parallel)

**Skip this step if the resolved panel has no Task seat** (e.g. `--panel cursor`)
— go straight to A4. Otherwise, for each Claude-seat entry (`opus`, `sonnet`, or
opt-in `fable`) in the panel, render its prompt from the ONE builder — **one
render line per Claude seat in the panel** — then dispatch the discuss seat
(`crew:panelist`) in parallel:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role opus -f .crew/debates/<dir>/question.md -o .crew/debates/<dir>/.prompt-opus.txt
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role sonnet -f .crew/debates/<dir>/question.md -o .crew/debates/<dir>/.prompt-sonnet.txt
# opt-in — only if fable is in the panel:
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role fable -f .crew/debates/<dir>/question.md -o .crew/debates/<dir>/.prompt-fable.txt
```

Dispatch each seat **by reference** — point the panelist at its rendered file and
let it `Read` it (reference, not payload; `crew:panelist` has `Read`), NOT the
inlined contents:

```
Task(subagent_type="crew:panelist", model="opus",   prompt="You are the opus seat. Read .crew/debates/<dir>/.prompt-opus.txt and follow it exactly.")
Task(subagent_type="crew:panelist", model="sonnet", prompt="You are the sonnet seat. Read .crew/debates/<dir>/.prompt-sonnet.txt and follow it exactly.")
# opt-in fable — premium tier; only if it's in the panel:
Task(subagent_type="crew:panelist", model="fable", prompt="You are the fable seat. Read .crew/debates/<dir>/.prompt-fable.txt and follow it exactly.")
```

(Review mode: render with `--mode review <target>` instead of `-f question`, and
dispatch `crew:reviewer` for the Task seats — the subprocess fan-out in A2 is
already the same per-seat `crew run` shape as `/crew:review` Step 3. But most
debates are discuss.)

> **Why `-o` here (both A2 and A3), not `--stage`:** `/crew:review`,
> `/crew:build`, and `/crew:measure-twice` stage prompts via `render --stage`
> (session-scoped `.crew/reviews/<session-id>/`). Debate deliberately keeps `-o`
> into its own `.crew/debates/<dir>/` (single-round) or `<run-id>/` (multi-round)
> dir instead — that dir is also where `question.md`/`round-NN.md` and the
> per-seat `<seat>.json` results live, and the run-id is what threads prior rounds
> through `render --run-id` (which `--stage` doesn't model). The two staging
> schemes are intentional, not an inconsistency to "fix".

## A4 — Normalize + synthesize + log

> **The Task RESULT is the only completion signal.** Each `Task(...)` RETURNS the
> seat's final message — that returned text IS the seat's take *and* the proof it
> finished. Take `ok`/`output` from the returned result. **NEVER judge a seat by
> a proxy** (output-file byte size, transcript length, a missed notification,
> elapsed time): those race the transcript flush and have falsely declared a
> finished seat "dead", discarding good work. A seat that has not returned is
> still running, not failed — wait for it. `ok=False` ONLY when the Task itself
> returns an error / no usable block, or the harness reports it failed.

Gather every seat into the six-field shape (`name, model, ok, output, error,
elapsed`): the subprocess seats are already six-field — read each
`.crew/debates/<dir>/<seat>.json` from A2.3 (IGNORE the empty `subprocess.json`
the A1 scaffold wrote — the real per-seat results live in the individual
`<seat>.json` files, not that bundled file); normalize each task seat (A3) into the
same shape from its returned Task result. A failed/skipped seat (subprocess OR
task) renders with its diagnostic and never suppresses the others. Render the full
panel side-by-side, then synthesize: **Areas of agreement / Key disagreements /
Recommendation** (discuss), or the `APPROVED`/`REVISE` + `[BLOCKING]`/`[MINOR]`
verdict (review). With the **Write tool**, log the seat outputs (`opus.md`,
`sonnet.md`, and optionally the subprocess seats) and `synthesis.md` into the
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
"${CLAUDE_PLUGIN_ROOT}/crew" render --mode discuss --seat-role <seat> \
  -f .crew/debates/<run-id>/question.md \
  --run-id <run-id> --round <n> --base-dir .crew/debates \
  -o .crew/debates/<run-id>/.prompt-<seat>-r<n>.txt
```

Render EVERY seat in the panel — subprocess AND task, including the opt-in
`fable` Task seat when present — exactly as A3 renders one prompt per Claude
seat; keep the render `-o` and the matching Task `Read .../.prompt-<seat>-r<n>.txt`
reference pointed at the same file.

Then run the round's seats (in parallel where possible):

- **Subprocess seats** (the `codex`/`agy`/`cursor-*` entries): execute the rendered prompt via the engine —
  ```bash
  "${CLAUDE_PLUGIN_ROOT}/crew" run <seat> -f .crew/debates/<run-id>/.prompt-<seat>-r<n>.txt --json -o .crew/debates/<run-id>/<seat>-r<n>.json
  ```
- **Task seats** (`opus`/`sonnet`/opt-in `fable`): dispatch one `crew:panelist`
  per Claude seat in the panel, **pinning each seat by its model id** — `opus` →
  `model="opus"`, `sonnet` → `model="sonnet"`, `fable` → `model="fable"` (every
  current Task seat pins its own name; the Task tool validates the model value,
  so a full version-locked id would be rejected at spawn). This is the SAME
  task-seat set and SAME pinning A3 uses, so single-round and multi-round stay
  in lockstep.
  Dispatch each **by reference** (`crew:panelist` has `Read` — point it at the
  rendered file, do NOT inline the contents):
  ```
  Task(subagent_type="crew:panelist", model="opus",            prompt="You are the opus seat. Read .crew/debates/<run-id>/.prompt-opus-r<n>.txt and follow it exactly.")
  Task(subagent_type="crew:panelist", model="sonnet",          prompt="You are the sonnet seat. Read .crew/debates/<run-id>/.prompt-sonnet-r<n>.txt and follow it exactly.")
  # opt-in fable — only if it's in the panel:
  Task(subagent_type="crew:panelist", model="fable",           prompt="You are the fable seat. Read .crew/debates/<run-id>/.prompt-fable-r<n>.txt and follow it exactly.")
  ```
  Use each Task's RETURNED result as its take and completion signal — never an output-file size or other proxy (see A3).

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

- Subprocess seats run one-per-seat via `run --json`, which ALWAYS exits 0 and
  writes a six-field result — a failed/skipped seat lands as `ok=False` with a
  diagnostic, the others are unaffected, and there is no all-failed engine abort to
  handle. (The A1 scaffold's `debate --seats none` runs no seats, so it can't fail
  the panel either.) Read whichever `<seat>.json` files appear.
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
`agy`/`cursor-*` seats, all external-CLI auth; Claude voices are
in-session Task seats. No `claude -p`, no Anthropic API — a stray
`ANTHROPIC_API_KEY` is irrelevant here. This council is fully self-contained in
crew — NEVER call `agy -p` / `codex exec` / `cursor-agent` directly, and NEVER
hand off to any external debate plugin/shell.
