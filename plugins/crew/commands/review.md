---
description: Multi-model review of a plan OR code diff (codex + agy + opus + sonnet)
argument-hint: "<the plan | the code | a .md path | a git scope>"
allowed-tools: Bash, Task, Read, Glob
---

[MULTI-MODEL REVIEW]

$ARGUMENTS

> **Note (Claude seats):** the two Claude voices are a single read-only agent,
> `crew:reviewer`, spawned twice in parallel with a per-spawn `model` override —
> once at `model: opus` and once at `model: sonnet`.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob`) and the engine's sandbox flags.

## What this does

The same review prompt fans out across a multi-model panel and you synthesize
the results into the existing `APPROVED / REVISE / [BLOCKING] / [MINOR]`
verdict. Two seat kinds:

- **subprocess seats** — `codex` and `agy` via the Python engine.
- **task seats** — `crew:reviewer` spawned twice (at `model: opus` and
  `model: sonnet`) via the Task tool (in-session, on the subscription — no
  `claude -p`, no API key).

Default panel: **codex + agy + opus + sonnet**. `agy` is a default subprocess
seat alongside `codex`; opus and sonnet are the Task seats. This is safe because
a failed/skipped seat NEVER aborts the review (see Step 6) — the verdict is
synthesized from whichever seats succeed.

## Step 1 — Dispatch plan vs code (natural language)

Read `$ARGUMENTS` and pick the target:

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

Run the engine once for the subprocess seats. Use the bare-script path (its
top-of-file `sys.path` guard makes package imports resolve):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" review "<TARGET>" --seats codex,agy --json --base "<BASE>"
```

- **Quote `"<TARGET>"` and `"<BASE>"`** — a plan path can contain spaces, and
  quoting is harmless for `working-tree`/`auto`/`main`.
- `<TARGET>` is the resolved plan `.md` path or git scope from Step 1.
- For a code review of the working tree, `<TARGET>` is typically `working-tree`
  or `auto`.
- `codex,agy` is the default subprocess panel; omitting `--seats` selects the
  same default. To narrow the panel, pass an explicit `--seats` (e.g. just
  `--seats codex`).
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

Spawn the reviewer seats **in parallel**, passing each a
**criteria-equivalent** prompt to the engine's (same plan-review or code-review
criteria — NOT byte-identical). Like the subprocess seats, the Task seats
**fetch the target themselves** — do NOT paste the plan body or diff into the
prompt. Tell each seat where to look: `Read` the plan file at its path, or run
the diff command (`git diff HEAD` for a working tree, `git diff <base>...HEAD`
for a branch) and read the changed files. `crew:reviewer` has
`Read, Grep, Glob, Bash` for exactly this (read-only git/inspection). This keeps
all four seats reviewing the SAME source by the SAME means — no
embedded-vs-referenced divergence. Both seats are the SAME agent
(`crew:reviewer`) with a per-spawn `model` override selecting the voice:

```
Task(subagent_type="crew:reviewer", model="opus",   prompt="<the assembled review prompt>")
Task(subagent_type="crew:reviewer", model="sonnet", prompt="<the assembled review prompt>")
```

The assembled review prompt must state plan-vs-code, tell the seat how to reach
the target itself (the plan file path, or the diff command), list the criteria,
and ask for per-criterion PASS/FAIL + `[BLOCKING]`/`[MINOR]` findings + a
one-line verdict.

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
- ONLY when **zero** seats produced usable output (every subprocess seat failed
  AND both Task seats failed) do you skip the verdict and instead report:
  `could not review — all seats failed: <per-seat diagnostics>`.

---

Subscription safety: the engine drives only `codex`/`agy` (external-CLI auth);
Claude voices are in-session Task seats. No `claude -p`, no Anthropic API — a
stray `ANTHROPIC_API_KEY` is irrelevant here.
