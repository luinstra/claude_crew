---
description: Multi-model review of a plan OR code diff (default panel codex + cursor-gemini + cursor-glm + cursor-composer + opus + sonnet; narrow with --panel/--seats)
argument-hint: "<the plan | the code | a .md path | a git scope>"
allowed-tools: Bash, Task, Read, Glob
---

[MULTI-MODEL REVIEW]

$ARGUMENTS

> **Note (Claude seats):** the Claude voices are a single agent, `crew:reviewer`
> (read-only by convention — it has `Bash` for git inspection but is instructed
> not to mutate; not sandbox-enforced), spawned once per `opus`/`sonnet` seat in
> the panel with a per-spawn `model` override.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models review; the rest is
the review request. Default (no flag) = the full panel.

- `--panel full` = `codex,cursor-gemini,cursor-glm,cursor-composer,opus,sonnet`
  (default) · `--panel lite` = `opus,sonnet` · `--panel solo` = `opus`
- `--seats <list>` = an explicit comma-list of any registered seat
  (`codex, agy, cursor-gemini, cursor-glm, cursor-composer, opus, sonnet`;
  e.g. `--seats codex,opus`). `agy` is opt-in — works via `--seats agy` but is
  not in the default panel. `--seats` wins if both are given.

Resolve to a **seat list**, then split it: the `codex`/`cursor-*` entries (and
opt-in `agy`) are the engine's `--seats` (Step 3 — **skip Step 3 if there are
none**); the `opus`/`sonnet` entries are the Task seats (Step 4 — spawn only
those). A one-seat panel is fine.

## What this does

The same review prompt fans out across a multi-model panel and you synthesize
the results into the existing `APPROVED / REVISE / [BLOCKING] / [MINOR]`
verdict. Two seat kinds:

- **subprocess seats** — the `codex`/`cursor-*` entries (and opt-in `agy`) of
  the resolved panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries of the resolved panel, each a
  `crew:reviewer` via the Task tool (in-session, on the subscription — no
  `claude -p`, no API key).

The panel is whatever the Panel-options flags resolved to (default **codex +
cursor-gemini + cursor-glm + cursor-composer + opus + sonnet**); only fan out the
seats in that list. This is safe because a
failed/skipped seat NEVER aborts the review (see Step 6) — the verdict is
synthesized from whichever seats succeed.

## Step 1 — Dispatch plan vs code (natural language)

Read the review request (`$ARGUMENTS` with any panel flags removed) and pick the
target:

- **Plan review** when the argument says "the plan", names a `.md` path, or is
  empty/"latest" (→ use the most recent `.md` in `.crew/plans/`).
- **Code review** when the argument says "the code", "the diff", "my working
  tree", "vs main", a branch/commit, or other scope words.

Resolve the concrete target:

- Plan: the `.md` path (or newest in `.crew/plans/`).
- Code: a git scope for the engine's `target` arg — `working-tree`, `branch`
  (with `--base`), `commit:<sha>`, or `auto`.

## Step 2 — Echo the resolved target; confirm on ambiguity

Before spawning ANY seat, print the resolved target descriptor so the user
sees what's about to be reviewed:

```
RESOLVED TARGET: kind=<plan|code> scope=<path or git scope> base=<base> state=<dirty|clean>
```

If plan-vs-code intent is **ambiguous** (e.g. the argument names a `.md` that is
also tracked-and-modified, or the wording could mean either), ask **exactly
ONE** confirming question and WAIT for the answer before fanning out. Do not
silently guess.

## Step 3 — Fan out subprocess seats (one Bash call)

**Skip this step if the resolved panel has no subprocess seat** (e.g.
`--panel lite`) — go straight to Step 4. Otherwise run the engine once for the
resolved subprocess seats. Use the bare-script path (its top-of-file `sys.path`
guard makes package imports resolve):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" review "<TARGET>" --seats <resolved codex/cursor-* seats> --json --base "<BASE>"
```

- **Quote `"<TARGET>"` and `"<BASE>"`** — a plan path can contain spaces, and
  quoting is harmless for `working-tree`/`auto`/`main`.
- `<TARGET>` is the resolved plan `.md` path or git scope from Step 1.
- For a code review of the working tree, `<TARGET>` is typically `working-tree`
  or `auto`.
- `--seats` carries ONLY the resolved subprocess seats — the `codex`/`cursor-*`
  entries, plus opt-in `agy` (the Panel-options flags already chose them).
- The engine returns a JSON array of result objects, each with the six fields:
  `name, model, ok, output, error, elapsed`.
- By default the subprocess seats **fetch the target themselves** (reference
  mode — they run the git command / read the plan file). Pass `--inline-diff` to
  embed the diff in the prompt instead (rarely needed). `--out <file>` writes the
  JSON to a file (no shell redirect — keeps the call allowlistable).
- **Never choke on a seat failure:** the engine NEVER raises out of the fan-out.
  Any failed/skipped subprocess seat comes back as an `ok=False` entry with a
  diagnostic in `error`; the other seats still return. The engine exits nonzero
  with an `all N seats failed: <diagnostics>` message on stderr ONLY when every
  subprocess seat failed — and even then it emits the full JSON array (no
  traceback). A nonzero engine exit does NOT abort the review: fold the Task
  seats in and synthesize from whatever succeeded.

## Step 4 — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each `opus`/`sonnet` entry in the
resolved panel** (zero, one, or both — skip this step if neither is present). Do
NOT hand-write each seat's prompt: **render it from the engine** so every seat —
subprocess AND Task — reviews ONE identical target. `render` is the single prompt
source, and it resolves the working-tree target the SAME way the engine's
`review` step did (compound `Target.diff_cmd` including untracked files), so a
Claude seat can't miss new files a subprocess seat sees. For each `opus`/`sonnet`
seat, render its prompt over the SAME resolved `<TARGET>` (and `--base "<BASE>"`)
you passed the engine in Step 3:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render "<TARGET>" --mode review --seat-role opus --base "<BASE>" -o .crew/.prompt-opus.txt
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render "<TARGET>" --mode review --seat-role sonnet --base "<BASE>" -o .crew/.prompt-sonnet.txt
```

(`<TARGET>` is the plan `.md` path or git scope from Step 1; `<BASE>` is the same
base passed in Step 3 — drop `--base` for a plan-file or working-tree target.)
Then dispatch each seat with the rendered text as its prompt — the SAME agent
(`crew:reviewer`) with a per-spawn `model` override selecting the voice (spawn
only the ones in the panel):

```
Task(subagent_type="crew:reviewer", model="opus",   prompt="<contents of .crew/.prompt-opus.txt>")
Task(subagent_type="crew:reviewer", model="sonnet", prompt="<contents of .crew/.prompt-sonnet.txt>")
```

The rendered prompt already states plan-vs-code, tells the seat how to reach the
target itself (the plan file path, or the diff command — reference mode), lists
the criteria, and asks for per-criterion PASS/FAIL + `[BLOCKING]`/`[MINOR]`
findings + a one-line verdict. `crew:reviewer` has `Read, Grep, Glob, Bash` for
exactly this (read-only git/inspection). Because every seat's prompt comes from
the one `render` source, the panel reviews a single identical target — no
embedded-vs-referenced or tracked-vs-untracked divergence.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors,
times out, returns no usable block, or is reported missing/failed by the
harness MUST NOT abort the review. Catch it, normalize it to the six-field
`ok=False` shape (Step 5), and continue with the other seats. A failed Task seat
is treated identically to a failed subprocess seat.

## Step 5 — Normalize Task-seat results to the six-field shape

For each Task seat, normalize its return into the SAME six fields as the
subprocess results (the Step 1.0 contract):

- `name` = `opus` / `sonnet` (the seat name).
- `model` = the pinned model (`opus` / `sonnet`).
- `ok` = True if the seat returned a usable review block; **False** if it
  returned no usable block, errored, or the harness reports it missing/failed.
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's review text.

A failed Task seat renders exactly like a failed subprocess seat — its error
block shows and never suppresses the others. A skipped/unspawnable seat renders
a clearly-marked skipped block and is excluded from the verdict math.

## Step 6 — Synthesize the verdict

Read the full panel (subprocess + normalized Task seats, in stable order) and
emit the existing verdict format:

- **Plan target** → `APPROVED` / `REVISE` (with `[BLOCKING]`/`[MINOR]` items).
- **Code target** → findings tagged `[BLOCKING]`/`[MINOR]` + a summary verdict.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the review and is NEVER silently dropped:

- Synthesize the verdict from the seats that produced usable output.
- Render each failed/skipped seat's block with its diagnostic alongside the
  successful ones.
- ONLY when **zero** seats in the panel produced usable output do you skip the
  verdict and instead report:
  `could not review — all seats failed: <per-seat diagnostics>`.

---

Subscription safety: the engine drives only the subprocess seats — `codex` + the
`cursor-*` seats (plus opt-in `agy`), all external-CLI auth; Claude voices are
in-session Task seats. No `claude -p`, no Anthropic API — a stray
`ANTHROPIC_API_KEY` is irrelevant here.
