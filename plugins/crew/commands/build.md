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

- `--panel full|lite|solo|cursor|quick|<custom>` — a named preset (`quick` = the
  cheapest cross-model pair; `<custom>` = any `[panels]` roster in per-repo/global
  config). `--seats <comma-list>` — an explicit subset of any registered seat
  (e.g. `--seats codex,opus`); `--seats` wins if both are given.
  Some registered seats are opt-in and not in the built-in default panel
  (premium cursor model-seats; the `fable` Claude voice): pass their exact names
  via `--seats`. The full seat census is `crew doctor` / the README panel table.
- **The engine resolves the preset — the orchestrator does NOT.** `review-prep`
  (Step 2a) resolves `--panel`/`--seats` into `subprocess_seats` (the external-CLI
  entries), `task_seats` (the Claude voices), and
  `task_seat_models` (each Task seat's model pin). The orchestrator never defines
  presets, classifies seat names, or hardcodes a model pin — pass the flag through
  and read the JSON. **Pass `--panel`/`--seats` ONLY when the user named a panel
  or seats** (an explicit flag, or intent like "use the lite panel"); otherwise
  OMIT it and let `review-prep` apply the configured/built-in default.

Below, **"the task"** means `$ARGUMENTS` with these panel flags AND the
`--executor` flag (below) removed: use it wherever a step references the task.
The raw `$ARGUMENTS` (flags included) is preserved byte-for-byte via the `-f`
spill file below so the panel survives across iterations. A one-seat panel is fine
(synthesis / never-choke handle any count down to one).

## Executor option (optional)

The IMPLEMENT step normally runs `Task(crew:executor)` (Claude). You can instead
route it through an external write-capable seat (e.g. `codex-luna`), with the
panel still gating completion exactly as today. Unset = byte-identical to today.

- `--executor <seat>`: a start-of-`$ARGUMENTS` flag (alongside the panel flags):
  the subprocess seat that implements each round (e.g. `--executor codex-luna`),
  or the literal `crew:executor` to force the default Task path. Precedence: this
  flag > `[build].executor` (per-repo `.crew/config.toml` > global
  `~/.crew-config.toml`) > builtin `crew:executor`.
- `[build].executor_retries`: optional config int `0..2` (default `0`), how many
  times to retry a FAILED external-executor round before surfacing and stopping.
  Retries STACK on the failed attempt's partial edits (there is no clean-tree
  revert), so the cap is low.

The engine (`crew build-executor`, resolved right after activation below) OWNS
this resolution + validation (the orchestrator never hardcodes a seat or reads
config). Pass `--executor <seat>` ONLY when the user named one; otherwise OMIT it.

## MANDATORY: Activate the Loop

**Spill the task to a file and pass `-f`, NEVER inline.** Write the raw
`$ARGUMENTS` to the ABSOLUTE anchored path
`<project-root>/.crew/task-bl-<session-id>.txt` with the **Write tool** (which
writes the exact bytes, no shell involved, and creates the `.crew/` parent dir).
Substitute your `CLAUDE_PROJECT_DIR` value for `<project-root>` in the WRITE-tool
path (Write takes no shell, so a literal absolute path is safe there). A
positional/`--prompt "$ARGUMENTS"` value containing `$(…)`, `$VAR`,
backticks, or `"` would be expanded or mangled by the shell when the Bash tool runs
the command, so the task NEVER goes on the command line. Then init the loop,
substituting your actual session id (the `[Session ID: …]` value) for
`<session-id>` as a literal `--session-id` value, NEVER a `${CLAUDE_SESSION_ID}`
shell expansion. The Bash recipe's `-f` is the plain RELATIVE path `.crew/...`,
double-quoted: the ENGINE anchors a relative path arg to the project root
(`crew_base()`, the same root the Write-tool literal named), never the shell cwd,
and the quotes keep any odd character in a substituted value inert. `${…}`
expansions are BANNED in these recipes: a command line carrying one defeats
permission allowlisting (it fires a manual approval prompt even when the prefix
rule matches), and a pasted literal root could carry `$(…)`/backticks into the
shell. `--consume` makes the engine delete the spill file after a successful init
(a failed init leaves it for retry), so no separate cleanup step exists to miss:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state init bl -f ".crew/task-bl-<session-id>.txt" --session-id <session-id> --consume
```

## MANDATORY: Resolve the Executor

Immediately after activation and **before Step 1**, resolve which executor
implements each round. The engine owns the resolution + validation. Add
`--executor <seat>` ONLY when the user passed one (extract it from `$ARGUMENTS`);
otherwise OMIT it so the `[build].executor` config / builtin default applies:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" build-executor
```

Read the one-line JSON it prints: `{"executor": "<crew:executor|seat>", "retries":
<int>, "source": "<flag|config|builtin>"}`. Keep `executor` and `retries` for the
executor step below.

**If it exits 2, SURFACE the reason and STOP.** A misconfigured executor (an
unknown seat, a Task seat like `opus`, a group token like `cursor`, or a
read-only seat) exits 2 naming the reason: it must NOT silently run as Claude, so
do not proceed to Step 1 until the executor resolves cleanly.

## How This Works

1. Delegate the implementation via **the executor step**: `Task(crew:executor)`
   by default, or a `crew dispatch --seat <seat>` external seat (keeps main
   context clean)
2. When the executor reports complete, get multi-model verification
3. If the panel approves (APPROVED, with the digest's quorum met), complete the loop
4. If the panel says REVISE with [BLOCKING] issues, re-delegate via the executor step with those issues, then re-verify
5. If the panel says REVISE with only [MINOR] issues (quorum met), accept and complete
6. If you try to stop without completing, you'll be reminded to continue

You orchestrate. You don't implement. All file edits, builds, and tests happen
inside the executor (the `crew:executor` agent, or the dispatched seat).

## The executor step (shared)

Both the initial implementation (Step 1) and the revision re-delegation (Step 3)
run the IMPLEMENT step through this ONE block, given a task PROMPT the caller
supplies. Do NOT duplicate the fork at the two sites; each just says "run the
executor step with this PROMPT". The step yields the round's **summary**; nothing
downstream (panel, verdict, quorum) changes.

**If `executor == "crew:executor"`** (the default, unchanged, byte-identical to
today): spawn the executor agent with the caller's PROMPT verbatim (INCLUDING its
`Create todos to track progress.` sentence: `crew:executor` has `TodoWrite`), and
the round's summary is the Task's returned text:

```
Task(
  subagent_type="crew:executor",
  prompt=<the caller's PROMPT, verbatim>
)
```

**Else (an external subprocess seat `<executor>`):** a subprocess seat has no
`TodoWrite`, so it runs via `crew dispatch`, not a Task.

1. **Write the spill ONCE, before the attempt loop.** Write the PROMPT with the
   sentence `Create todos to track progress.` omitted (leaving the rest of the
   prompt intact) to the ABSOLUTE anchored path
   `<project-root>/.crew/reviews/<session-id>/executor-task.txt` with the **Write
   tool** (exact bytes, no shell; substitute your `CLAUDE_PROJECT_DIR` value for
   `<project-root>` in the WRITE-tool path, safe as a literal there, and it creates
   the parent dir). (A subprocess seat has no todo list to
   create.) This ONE file feeds EVERY attempt below; do NOT rewrite or delete it
   between attempts (each retry re-runs the SAME `-f` dispatch and needs it).
2. **Attempt loop (1 initial run + up to `retries` retries, `retries` is `0..2`).**
   Each attempt dispatches the seat at the working tree, reusing that same `-f`
   spill: it edits files in place and leaves them uncommitted + unstaged, the dirty
   tree the panel then reviews.

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/crew" dispatch --seat <executor> --session-id <session-id> -f ".crew/reviews/<session-id>/executor-task.txt" --json
   ```

   (The relative `-f` is anchored by the ENGINE to the project root, the same
   file the Write-tool literal created; no `${…}` expansion belongs on this line,
   and the double quotes keep the substituted id's characters inert.)

   **Check the PROCESS EXIT STATUS first, before reading any envelope.** The
   envelope path is derived from the session id alone and PERSISTS across rounds,
   so a prior round's `dispatch-<executor>.json` is sitting there before this
   round runs. `dispatch` exits nonzero (exit 2) on a pre-run/misuse failure
   WITHOUT writing or printing an envelope, so a nonzero exit means NO fresh
   envelope was written THIS round:

   - **Nonzero exit:** the configured executor did not run this round. Surface the
     command's stderr, clean the spill (step 3), and **STOP this round**. Do NOT
     read the envelope (it would consume a PRIOR round's result and send the panel
     to review work the selected executor never produced this round), do NOT
     retry, do NOT proceed to the panel. The loop stays ACTIVE; the human resolves
     the misuse.
   - **Exit 0:** the envelope was written fresh this round at the derived path.
     Proceed to read it and branch on guards / ok as below.

   `dispatch` DERIVES and PRINTS its envelope path
   (`.crew/reviews/<session-id>/dispatch-<executor>.json`) as its last stdout line:
   read it back from there, do not construct the name. **Read the envelope JSON**
   at that path and branch on the envelope's guard fields FIRST, not `ok` alone (a
   seat can return `ok=true` while having committed or switched branches, AND a
   seat that FAILED can ALSO have committed/staged/switched, so a guard violation
   must surface and stop the round no matter what `ok` says). Evaluate in THIS
   order:

   - **Any of `head_moved` / `staged_changed` / `branch_changed` is `true`**
     (the envelope's guard booleans), REGARDLESS of `ok`: a GUARD VIOLATION,
     checked FIRST and NOT retryable (the seat already committed, staged, or
     switched branches; a retry would only stack on top of it, and a seat that
     ALSO failed leaves hidden VCS mutations under any retry). Relay the
     envelope's `guard_warnings` strings VERBATIM (the formatted recovery
     guidance the engine put in the envelope), point the user at `git status` /
     `git log`, and **STOP this round before any review** (do step 3 first). Do
     NOT proceed to the panel: the panel reviews the WORKING TREE (`git diff`),
     which cannot see committed or branch-switched work, so approving it would
     sign off on an incomplete picture the panel never saw. Do NOT retry. The
     loop stays ACTIVE; the human resolves the committed work.
   - **Else `envelope.ok` is `false`** (timeout, CLI error, or a skipped-unavailable
     seat) and NO guard fired: a clean failure, RETRYABLE. If attempts remain, emit
     a LOUD visible note (`executor <executor> failed, retry N/<retries>`) and retry
     the SAME dispatch. Honestly: a retry starts ON TOP of the failed attempt's
     partial edits (there is no clean-tree snapshot to revert to), so retries STACK,
     which is why the cap is `0..2`. After the attempts are exhausted, **SURFACE the
     failure AND the partial-edits diff** (`git status` + `git diff`, read-only) and
     **STOP this round** (do step 3 first), handing back to the user. `git diff`
     shows tracked changes and untracked NAMES but not the CONTENTS of newly-created
     files, so a seat that failed after creating new files leaves that work
     invisible: also list the untracked files read-only (`git status --porcelain -z`
     or `git ls-files --others --exclude-standard -z`, `-z` so odd filenames don't
     break iteration) and Read the ones that matter to show their content (skip huge
     or binary files). Read-only only: NEVER `git add` / `git add -N` (the seat's
     edits stay unstaged for the human's triage). The loop stays
     ACTIVE (the Stop hook holds), so nothing is lost. **NEVER fall back to
     `crew:executor`:** substituting Claude's work into a round the user asked a
     specific model to do is a lie about what produced the diff.
   - **Else `envelope.ok` is `true` AND no guard fired:** SUCCESS. The round's summary
     is `envelope.output`. Do step 3, then continue to review as normal.
3. **`rm -f` the `executor-task.txt` spill AFTER the loop ends** (its own Bash
   call), on EVERY exit path (success, guard-violation stop, or retries-exhausted
   stop), so no transient task file lingers. Do this only after all attempts, never
   between them. (No consume-on-first-read here: dispatch retries re-read the SAME
   `-f`, so the file must outlive every attempt.) This `rm` is a plain shell
   command with no engine anchoring, so it takes the ABSOLUTE path: substitute
   your `CLAUDE_PROJECT_DIR` value for `<project-root>` (the same root the
   Write-tool literal named), keeping it a double-quoted LITERAL. The `${…}` ban
   is on expansions, not literals: a substituted literal stays allowlistable and
   deletes the right tree's file even when the shell cwd has drifted.

   ```bash
   rm -f "<project-root>/.crew/reviews/<session-id>/executor-task.txt"
   ```

Whichever path ran, the round's summary (the Task's returned text, or the
envelope's `output`) is what Step 2b writes to `<run_dir>/executor-summary.md`
(that single write serves both paths, and the panel reads the summary from there).
The run dir is minted later, by `review-prep` in Step 2a, so the summary is
captured HERE and written THERE: do not write it in this step.

> **Envelope overwrite across rounds (known, harmless).** `dispatch` derives its
> envelope name `dispatch-<executor>.json` from the session id alone, so every
> round of a multi-round build overwrites the previous round's envelope. Harmless,
> because the executor step reads the envelope immediately after each round, before the
> next round runs, but it means there is NO cross-round envelope audit trail.
> Documented, not fixed.

## Step 1: Delegate Work to Executor

Run **the executor step** (the shared block above) with the task PROMPT below; do
NOT inline a `Task(...)` here (the executor step owns the Task-vs-subprocess fork):

PROMPT:

```
Execute: <the task>

Work through the task systematically. Create todos to track progress. Implement the changes, run builds/tests where applicable, and report back: what you changed, what you verified, and any blockers.
```

(`<the task>` = `$ARGUMENTS` with any panel/executor flags stripped: see the
options above. The `Create todos to track progress.` sentence STAYS in this PROMPT
text; the executor step omits that sentence for the subprocess path.)

Capture the round's summary the executor step yields; you'll pass it to the
review panel in Step 2b.

## Step 2: Get Multi-Model Verification

Before requesting verification:

- [ ] Executor reported the task complete
- [ ] No unresolved blockers in executor's report

When executor reports complete, verify the work with a multi-model panel over the
**working-tree diff**, and synthesize the results into the existing verdict. Two
seat kinds:

- **subprocess seats** — the external-CLI entries of the resolved
  panel, via the Python engine.
- **task seats** — the Claude-voice entries (built-in `opus`/`sonnet`, plus any
  config-declared claude-code seat), each a `crew:reviewer` spawned via
  the Task tool (in-session, on the subscription — no `claude -p`, no API key).

The panel is whatever the flags resolved to (the configured default panel:
`review-prep`'s JSON is the roster of record for this run); only fan out the
seats in that list. A failed/skipped seat NEVER aborts the verification (see
"Synthesize" below) — the
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
mints the RUN-SCOPED review dir (`run.json` + a frozen snapshot of the reviewed
diff), stages the shared subprocess prompt AND one prompt per Task seat against
that snapshot, and PRINTS `{prompt_path, subprocess_seats, task_seats,
task_seat_models, run_dir, run_id, target_sha256, task_prompt_paths,
pending_subprocess_seats, pending_task_seats}` as one-line JSON. It runs NOTHING.
Substitute your real id for `<session-id>` (the `[Session ID: …]` value; never the
literal placeholder or a `${…}` expansion):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep working-tree --session-id <session-id>
```

**OMIT `--panel`/`--seats` when the user named no panel** (as shown) — so
`review-prep` applies the configured `default_panel` or built-in **full**. Pass
`--panel <preset>` / `--seats <subset>` ONLY when the user chose one explicitly.
(No `--base` here: the `working-tree` target reviews the uncommitted diff vs `HEAD`
and ignores the base ref.) Reference mode (the default) freezes the compound
working-tree diff, untracked files included, into the run dir's snapshot, so a
seat can't miss files the executor just created and mid-review edits can't change
what the panel certifies; `--inline-diff` embeds the content instead (forwarded to
the staged prompt).

`review-prep` resolves BOTH seat kinds: Step 2b reads `task_seats` +
`task_seat_models` + `task_prompt_paths` from this same JSON; the engine EXECUTES
only the subprocess seats. **Parse the one-line JSON** it prints (read it directly
from stdout — it is NOT a shell `$(…)` capture): use `pending_subprocess_seats` to
iterate the per-seat loop, `run_id`/`run_dir` on every engine call below, and the
FULL `subprocess_seats` (JOINED comma-separated) as part of the `collect --seats`
list in Step 2c.5. Use the printed `run_dir` VERBATIM only for **Write-tool** and
**Task `Read`** references (a literal path, no shell). In a **Bash recipe** never
paste `run_dir` verbatim: reconstruct it as the RELATIVE
`.crew/reviews/<session_segment>/<run_id from the prep JSON>/...`, with no root
prefix and no `${…}` expansion (which would defeat permission allowlisting); the
ENGINE anchors a relative path arg to the project root (`crew_base()`), never the
shell cwd. `<session_segment>` is the `session_segment` field from the prep JSON,
the SANITIZED session dir segment the engine actually built the run dir from. Use
THAT, never the raw `<session-id>`: the raw id can diverge from the engine's dir,
or a crafted one traverse, so a reconstruction with the raw id could miss the real
dir (`<session_segment>`/`run_id` are safe engine-controlled strings). The per-seat `run` DERIVES its `-f`/`-o` from `--session-id` +
`--run-id` (the run dir's `prompt-seat.txt` / `<seat>.json`), so you do NOT pass
`prompt_path` as `-f` yourself (see 2a.2). **If `subprocess_seats` is empty** (a
Claude-only `--panel lite`/`solo` → `prompt_path` is `""`, no subprocess prompt
staged), **SKIP only the 2a.2 per-seat loop** — go to Step 2b; you STILL persist
the Task seats (Step 2c) and run the SAME grouped `collect` (Step 2c.5) with
`--seats <task_seats only>`.

**2a.1b: freeze the run identity into loop state.** Immediately after prep
prints its JSON and BEFORE any seat runs:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state begin-review bl --session-id <session-id>
```

This reads the prepped run's own `run.json` and freezes the run id, the target
hash + spec, and the seat roster into the loop's state (the identity Step 3's
`record-verdict` judges the panel's results against). It prints `phase=reviewing
run=<run_id> seats=<N>`. Re-running it after a re-prep is fine (it re-arms the
freeze against the newest run). Skip it and the verdict is unrecordable, and a
loop with no recorded verdict cannot be completed at all.

**Keep the working tree quiet from here until the verdict is recorded.**
`record-verdict` re-hashes the reviewed target, and if the bytes on disk are no
longer the bytes the panel read it raises drift as a completion advisory (exit 3,
below): nothing is recorded until the user authorizes `--force`. The seats are
read-only, so nothing moves unless YOU edit mid-review: don't. If the tree does
drift, the advisory names it rather than certifying work no panel saw, and the
clean fix is a fresh `review-prep` + `begin-review` + panel over the current
content (the user may instead `--force` the completion over the drift).

**2a.2 — run EACH seat in its own parallel shell.** Do NOT delete seat files:
results are RUN-SCOPED. Each `review-prep` mints a run identity from the reviewed
content + panel, so a re-verification of CHANGED work lands in a NEW `<run_dir>`
(an earlier round's stale results are unreachable), while an UNCHANGED target
RESUMES its dir: seats already landed there stay landed, are excluded from
`pending_subprocess_seats`/`pending_task_seats`, and a valid landed result is
never overwritten by a fresh failure (preserve-valid).

For every seat in `pending_subprocess_seats` (NOT the full roster, landed seats
resume), launch a SEPARATE `crew run <seat>` Bash call, all concurrently (e.g.
background calls). `--session-id` + `--run-id` let `run` DERIVE both paths from
the run dir: `-f` = `prompt-seat.txt`, `-o` = `<seat>.json`, stamped with the run
identity:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --json
```

- `run --json` ALWAYS exits 0 and writes the six-field result
  (`name, model, ok, output, error, elapsed`) — a failed/skipped seat lands as
  `ok=False` with a diagnostic. **Per-seat never-choke is automatic**: one seat
  failing can't sink the others, and there's no all-failed abort to handle.
  (Run-scoped results also carry the `run_id`/`target_sha256` stamps; a run-scoped
  MISUSE, like a prompt/model/sandbox override or a non-member seat, exits 2
  before any result is written. The templated call above never hits those paths.)
- Same model / sandbox / auth-banner / ARG_MAX handling as the old single-call path
  — `run` uses the identical provider machinery.

**2a.3 — do NOT collect yet.** The WHOLE panel — subprocess AND Task seats — flows
through ONE grouped `collect` in **Step 2c.5**, after the Task seats are persisted.
Launch the 2a.2 shells (in parallel) and move to Step 2b; Step 2c.5 owns the
wait-for-both barrier and the single collect.

### Step 2b — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each entry in `pending_task_seats`** (from
the `review-prep` JSON — skip if empty; a landed seat from an unchanged-target
resume is already in the run dir). Do NOT hand-write each seat's prompt, and do
NOT run a separate `render` call: `review-prep` ALREADY STAGED one prompt per Task
seat into the run dir (`task_prompt_paths` in the prep JSON,
`<run_dir>/prompt-<seat>.txt`), each pointing the seat at the run's FROZEN
snapshot (`<run_dir>/target.diff`) as the content under review. That matters most
here: `/crew:build` verification routinely follows a step where the executor just
CREATED new files, and the snapshot already includes untracked files as new-file
diffs, while a re-render would re-resolve the LIVE working tree and reintroduce
the drift the snapshot exists to close. Every seat, subprocess AND Task, reviews
the same frozen bytes.

Then **write the executor's summary to `<run_dir>/executor-summary.md`**
with the Write tool (a PATH, not inline — so it isn't re-transcribed into every seat's
prompt), and dispatch each seat **by reference** — the SAME agent (`crew:reviewer`)
with a per-spawn `model` override selecting the voice. **Iterate
`pending_task_seats` from the prep JSON**; for each `<seat>` read its model from
`task_seat_models[<seat>]` and its prompt path from `task_prompt_paths[<seat>]`. Do
NOT hardcode seat names or model pins, and do NOT read + inline the staged prompt —
point the seat at its staged file and the summary file and let it `Read` them
(reference, not payload; the reviewer has `Read`):

```
# for each <seat> in pending_task_seats (path = task_prompt_paths[<seat>]):
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="You are the <seat> seat. Read <run_dir>/prompt-<seat>.txt and the executor summary at <run_dir>/executor-summary.md, then follow the prompt exactly.")
```

The staged prompt already states this is a **code review**, points the seat at the
frozen snapshot as the review authority (with the live diff command as
supplementary context), lists the criteria (completeness, code quality, security,
performance, error handling), and asks for findings tagged `[BLOCKING]`/`[MINOR]`
+ a one-line verdict. `crew:reviewer` has `Read, Grep, Glob, Bash` for exactly
this (read-only git/inspection). Because every seat's prompt comes from the one
prep-staged source over one snapshot, the panel reviews a single identical diff.
`.crew/` is gitignored.

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

For each Task seat **you spawned in Step 2b** (`pending_task_seats` from the
`review-prep` JSON — NOT a hardcoded `opus`/`sonnet` pair), normalize its return
into the SAME six fields as the subprocess results:

- `name` = the seat's name (a registered seat name is already `[A-Za-z0-9_-]`-only
  by the `name == slug(name)` invariant, so it doubles as its own filename stem).
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

**Persist each Task seat through the engine.** For EACH seat you spawned
(`pending_task_seats` from the prep JSON — never a hardcoded list), write the
seat's returned Task result to a temp file with the Write tool (a file
under the ABSOLUTE anchored `<project-root>/.crew/reviews/<session_segment>/`,
e.g. `tmp-seat-<seat>.md`; substitute your `CLAUDE_PROJECT_DIR` value for
`<project-root>` and the prep JSON's `session_segment` for `<session_segment>` so
the Write literal and the engine's `-f` read resolve to ONE file; NEVER a system
temp or scratchpad dir, which sits outside the approved working
dirs and fires a permission prompt on every write), then persist it
with the SAME `--run-id` the prep printed (required: a late seat persisted
without it exits 2 rather than risk landing in a newer run's dir). The Write-tool
path above is literal (no shell); the Bash `-f` below reads the SAME file as the
plain RELATIVE `.crew/...` path + the sanitized `<session_segment>`: the ENGINE
anchors it to the project root, so no `${…}` expansion belongs on the line:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" persist-seat <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --model "<task_seat_models[<seat>]>" -f ".crew/reviews/<session_segment>/tmp-seat-<seat>.md"
```

For a seat whose Task errored / returned no usable block, persist the failure
instead (never fabricate an ok result):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" persist-seat <seat> --session-id <session-id> --run-id <run_id from the prep JSON> --model "<task_seat_models[<seat>]>" --failed --error "<one-line diagnostic>"
```

The engine derives the filename and the six-field shape itself — the
printed path is the `<seat>.json` the Step 2c.5 collect reads. The seat entry you
pass to `collect --seats` is the seat name (the filename stem the engine printed).

### Step 2c.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every Step 2a.2
subprocess `crew run <seat>` shell to EXIT, AND (b) every Step 2b Task seat to return
AND be persisted in Step 2c. A seat whose `<seat>.json` is not written yet is STILL
RUNNING, not skipped — collecting early would silently drop it.

For the subprocess half, use the engine's barrier primitive instead of
improvised polling (it exits 0 once every subprocess seat file has landed, ok
and failed alike, since a failed seat has FINISHED; on timeout it exits 1
naming exactly the seats still missing, so you can report or relaunch them):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" wait --session-id <session-id> --run-id <run_id from the prep JSON> --timeout 550
```

The snippet passes `--timeout 550` because `wait`'s 900s default exceeds the
Bash tool's own timeout (120s default, 600s max): a foreground call left at
the default gets killed by the harness before `wait` can name the missing
seats. Keep the explicit `--timeout` under your Bash call's budget (550 fits
a call with its timeout raised to 600000ms), or run the call in the
background and check its result.

`wait` covers ONLY the subprocess shells, and refuses a Task seat by name: the
Task seats are persisted by YOU after their Task returns, so waiting on one
would deadlock the only process that can write it. Wait for your own Task
returns, persist them, THEN collect.

Define `<ran_seats>` = the comma-join of the FULL `subprocess_seats` (resolved
prep order) **then** the FULL `task_seats`: the whole roster, not only the
pending seats: a landed seat from a resumed run is part of this panel's verdict.
In the Claude-only branch (`subprocess_seats` empty) `<ran_seats>` is just
`task_seats`.

**Repair non-compliant seats (per-seat haiku reformat).** A seat that ignored the
structured `## FINDINGS` schema can't be grouped — give it ONE cheap repair pass. Ask
the engine which seats did not parse:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --run-id <run_id from the prep JSON> --seats <ran_seats> --report-unparsed
```

This prints the seat names (one per line) whose output ran but did NOT parse into
structured findings. For EACH such `<seat>`:

1. **Read** its raw `output` field — `<run_dir>/<seat>.json`.
2. Spawn the cheap **formatter** agent to reformat that text into the schema (a
   FAITHFUL transform — do NOT invent, drop, or re-judge findings):

   ```
   Task(subagent_type="crew:formatter", model="haiku",
        prompt="Reformat this review into the FINDINGS schema verbatim — do not
   invent or drop findings, only reformat:\n\n<the seat's raw output field>")
   ```

3. Write the formatter's returned text into the seat file with the engine (it writes
   to a SEPARATE `repaired_output` field, NEVER overwriting the original
   `output`), via a temp file with `-f` (Write it literal, also under the ABSOLUTE
   anchored `<project-root>/.crew/reviews/<session_segment>/`, e.g.
   `tmp-repair-<seat>.md`; never a system temp or scratchpad dir). Pass the seat
   NAME positionally: the engine derives `<run_dir>/<seat>.json` from
   `--session-id`/`--run-id` itself, so no seat-file path is reconstructed at all;
   the relative `-f` temp path is engine-anchored to the project root (the SAME
   file the Write tool created; no `${…}` expansion belongs on the line):

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/crew" repair-seat <seat> --session-id <session-id> --run-id <run_id from the prep JSON> -f ".crew/reviews/<session_segment>/tmp-repair-<seat>.md"
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
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --run-id <run_id from the prep JSON> --seats <ran_seats> --group -o ".crew/reviews/<session_segment>/<run_id from the prep JSON>/panel.md" --full ".crew/reviews/<session_segment>/<run_id from the prep JSON>/panel-full.md"
```

(The relative `-o`/`--full` paths are anchored by the ENGINE to the project root;
`collect` echoes the resolved absolute path it wrote.)

`--group` writes the deduped digest to `panel.md`; `--full` writes the faithful
per-seat concatenation to `panel-full.md` (the recovery artifact). **Read `panel.md`
once** and carry that ONE digest into Step 2d — do NOT also carry the raw Task-seat
blocks into context.

### Step 2d — Synthesize the verdict

Read the ONE grouped `panel.md` digest — it already folds the WHOLE panel — and emit
the existing verdict format:

- APPROVED: Work is complete and code quality is acceptable.
- REVISE: Issues found. Classify each as [BLOCKING] or [MINOR].

If the panel found nothing at all, state: `VERDICT: APPROVED`. If it found only
[MINOR] issues, state `VERDICT: REVISE` with those issues listed: the loop
still completes (Step 3's `--minor-only` row), but the verdict says what the
panel actually said instead of laundering minor findings into a sign-off. If any
[BLOCKING] issues exist, state `VERDICT: REVISE` with the list of issues.

Either completing verdict expects the digest's quorum header to read MET, or to
be absent (a flat/legacy digest with no `PANEL:` header carries no quorum gate);
a NOT MET digest can still complete, but only on the user's explicit `--force`
(see Step 3), and otherwise renders the could-not-verify-quorum outcome below.

`panel.md` has four sections, read them ALL: **VERDICTS** (roster of every seat that
ran + tally — reference seats by LABEL, never positionally), **CRITERIA MATRIX**
(per-criterion PASS/FAIL/`?`), **GROUPED FINDINGS** (each finding once, with an `M/N`
agreement count where N = findings-parsed seats; a **`⚠ SINGLETON`** lone dissent is
weighed ON ITS MERITS, never dismissed for being one seat), and **RAW / UNPARSED
SEATS** (any seat that didn't parse even after repair, verbatim — **still read
these**, a partial seat's prose findings live here).

**The quorum header is advisory.** A run-scoped grouped digest opens with a
`PANEL:` header. When it reads `NOT MET`, too few usable seats reviewed this
content for a clean sign-off, and a completing `record-verdict` will exit 3
rather than certify it (see Step 3). That is NOT a hard stop: it means the loop
cannot certify WITHOUT the user's explicit `--force`. Report the outcome as
`could not verify quorum without the user's explicit --force: <usable>/<N>
usable (threshold from the header)`, name the seats that are pending or failed,
and either relaunch the pending seats (same `--run-id`; a landed valid seat is
never overwritten), surface the shortfall to the user, or complete with
`--force` ONLY on the user's explicit say-so. This gates only how much SUCCESS
certification requires: a failed seat still never aborts the review (never-choke
below is unchanged), and the digest header is the authority on the count. Zero
usable seats stays the all-failed branch below.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the verification and is NEVER silently dropped (it
shows in the VERDICTS roster and/or the RAW section): synthesize from the seats that
produced usable output. ONLY when **zero** seats produced usable output do you skip
the verdict and instead report: `could not verify — all seats failed: <per-seat
diagnostics>`.

## Step 3: Handle the Verdict

| Verdict                             | Action                                                                       |
| ----------------------------------- | ---------------------------------------------------------------------------- |
| **APPROVED** (quorum MET)           | `record-verdict bl APPROVED`, then complete the loop (Step 4)                |
| **REVISE with only [MINOR]** (quorum MET) | `record-verdict bl REVISE --minor-only`, then complete the loop (Step 4) |
| **REVISE with any [BLOCKING]**      | `record-verdict bl REVISE`, then re-delegate to executor with the issues (below), then return to Step 2 |
| **Quorum NOT MET** (the digest's `PANEL:` header), or any completion advisory (target drift, pointer divergence) | A completing `record-verdict` exits 3 and records NOTHING: the loop CANNOT certify WITHOUT the user's explicit `--force`. Surface the advisory to the user (below); relaunch the pending seats (same `--run-id`), or complete with `--force` ONLY on the user's say-so |
| **Zero usable seats** (the all-failed branch of Step 2d) | `record-verdict bl FAILED`, then report the per-seat diagnostics |

The completing rows expect the digest's quorum header to read MET (or to be
absent: a flat/legacy digest with no `PANEL:` header carries no quorum gate); a
NOT MET header does not block completion outright, it routes through the exit-3
advisory flow below (the user's `--force` decides).

**Record the verdict through the engine before acting on it.** The loop's state
is what completion checks, so an unrecorded verdict leaves the loop unfinishable:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state record-verdict bl <APPROVED|REVISE|REJECT|FAILED> [--minor-only] --session-id <session-id>
```

`--minor-only` is the REVISE-with-nothing-blocking row: it completes the loop, so
it faces the same checks APPROVED does. The engine re-checks what this table
already told you (at least one usable seat for any non-FAILED verdict, plus
quorum, run-pointer agreement, and target drift for a completing one). It exits
one of three ways:

- **Exit 0**: recorded (a completing verdict printed `phase=done`): proceed to Step 4.
- **Exit 3**: a completion advisory tripped (quorum short, zero usable seats on
  a plain REVISE/REJECT, the tree drifted
  since the panel, the run pointer moved, the target no longer resolves): NOTHING
  was recorded and the engine printed every tripped condition. **Do NOT re-run
  with `--force` on your own.** Surface the advisory text to the user, state
  plainly what tripped and that `--force` would record the completion over it,
  and WAIT for the user's explicit decision. Only if the user says to proceed do
  you re-run the SAME call with `--force` appended (it records and stamps
  `last_verdict_overrides`). If the user says no, do not complete: re-run the
  pending seats (same `--run-id`), re-prep, or stop, per what the user wants.
  **`--force` is the HUMAN's authorization, carried out by you on their say-so;
  you never originate it.** That is the whole reason the check is
  advisory-with-a-human rather than a silent auto-pass.
- **Exit 2**: a hard error (wrong phase, no run identity, FAILED over a usable
  panel): a real contradiction to FIX, never forced.

FAILED is only for a panel that returned nothing usable; the SECOND consecutive
FAILED ends the loop itself (it stamps the state inactive, so no deactivate
follows) rather than re-prepping a panel that is not returning.

### Re-delegating on REVISE [BLOCKING]

**Diagnose the CLASS before dispatching the fixes.** Before re-delegating, ask
of the finding list: do several findings share one structural cause (the same
property violated at different sites, the same value judged in the wrong place,
the same two-mechanisms-one-invariant split)? If so, instruct the executor to
fix the STRUCTURE once and re-derive the sites from it, not to patch each
finding where it was reported. Site-by-site patches of a structural problem
mint new findings every round (each patch adds reviewable surface with the same
underlying flaw), and the loop converges only when the structure changes. One
sentence in the prompt naming the shared cause is enough; when the findings are
genuinely independent, say that instead and fix them as listed.

Run **the executor step** (the shared block) with this revision PROMPT; do NOT
inline a `Task(...)` (the executor step owns the same Task-vs-subprocess fork Step
1 uses), so the revision runs on whatever `executor` resolved to:

PROMPT:

```
REVISION needed for previous task.

Original task: <the task>

The panel flagged these [BLOCKING] issues:
[list each blocking issue verbatim]

[If several findings share one structural cause, name it here and instruct:
fix that structure once, then re-check every listed finding against the fix.]

Address each issue. Report what you changed and what you verified.
```

Then go back to Step 2 to verify the revised work.

## Step 4: Complete the Loop

When the panel's verdict is APPROVED (or REVISE with only [MINOR] issues) AND
`record-verdict` recorded it: exit 0 on a met/absent quorum header, or a
`--force` the user explicitly authorized over a NOT MET header or another
completion advisory (see Step 3):

Step 3 already recorded the verdict, and that is the ONLY place it is recorded:
`record-verdict` refuses from `phase=done`, so a second call here would fail. Its
success printed `phase=done`, which is the phase the deactivate below requires: a
loop with no completing verdict on record is refused there, because a plain
deactivate is otherwise indistinguishable from an unreviewed finish.

1. **Deactivate the loop** — pass the SAME `--session-id` the init used (the
   `[Session ID: …]` value, as a literal, NOT a `${CLAUDE_SESSION_ID}` shell
   expansion), so deactivate targets the exact session-scoped state file that init
   wrote (otherwise the scoped file stays active and keeps blocking Stop):
```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate bl --reason "Panel approved" --session-id <session-id>
```
   When the completion required `--force` (a NOT MET panel the user authorized),
   the `--reason` must name that instead: `--reason "Completion forced over
   advisories"`, matching the reason the Stop-hook nudge already prints for a
   forced completion; a clean completion keeps `"Panel approved"`.

2. **Summarize what was accomplished** and let the user know the task is complete.

## Early Exit

The loop can end without the panel approving in these ways:

- A SECOND consecutive `FAILED` verdict: the loop deactivates terminally in place
  (`exit_kind=review_failed`; see Step 3). A single FAILED does not end it.
- User runs `/crew:cancel-build`
- A hook-owned safety limit trips: the stop-fire cap or the wall-clock deadline
  (both force-exit the loop; see `scripts/CLAUDE.md`). Report where the work
  stands; do NOT re-init the loop to keep going.
- The turn is PARKED: background work the session launched is still in flight
  (panel seats, but also any unrelated background shell), so the Stop is ALLOWED
  and the turn ends. This is not an exit: the loop stays active and picks up when
  that work completes. Consecutive parked turns are capped, so a shell that never
  exits cannot switch the loop off.

---

First: run the activation command, then resolve the executor (`crew
build-executor`). Then proceed to Step 1 (run the executor step).
