---
description: Start verified development loop with multi-model review before completion
argument-hint: "<task description>"
allowed-tools: Bash, Task, Read, Glob
---

[BUILD LOOP STARTING]

$ARGUMENTS

## MANDATORY: Activate the Loop

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py init bl --prompt "$ARGUMENTS"
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
  prompt="Execute: $ARGUMENTS

Work through the task systematically. Create todos to track progress. Implement the changes, run builds/tests where applicable, and report back: what you changed, what you verified, and any blockers."
)
```

Capture the executor's summary — you'll pass it to the review panel in the next step.

## Step 2: Get Multi-Model Verification

Before requesting verification:

- [ ] Executor reported the task complete
- [ ] No unresolved blockers in executor's report

When executor reports complete, verify the work with a multi-model panel instead
of a single advisor. The same code-review criteria fan out across several AI
seats in parallel over the **working-tree diff**, and you synthesize the results
into the existing verdict. Two seat kinds:

- **subprocess seats** — `codex` and `agy` via the Python engine.
- **task seats** — `crew:reviewer` spawned twice (at `model: opus` and
  `model: sonnet`) via the Task tool (in-session, on the subscription — no
  `claude -p`, no API key).

Default panel: **codex + agy + opus + sonnet**. A failed/skipped seat NEVER
aborts the verification (see "Synthesize" below) — the verdict is synthesized
from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob`) and the engine's sandbox flags.

### Step 2a — Fan out subprocess seats (one Bash call)

Run the engine once over the working-tree diff. Use the bare-script path (its
top-of-file `sys.path` guard makes package imports resolve):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" review working-tree --seats codex,agy --json
```

- The engine returns a JSON array of result objects, each with the six fields:
  `name, model, ok, output, error, elapsed`.
- **Never choke on a seat failure:** the engine NEVER raises out of the fan-out.
  Any failed/skipped subprocess seat comes back as an `ok=False` entry with a
  diagnostic in `error`; the other seats still return. The engine exits nonzero
  with an `all N seats failed: <diagnostics>` message on stderr ONLY when every
  subprocess seat failed — and even then emits the full JSON array (no
  traceback). A nonzero engine exit does NOT abort the verification: fold in the
  Task seats and synthesize from whatever succeeded.

### Step 2b — Fan out Task seats (parallel)

Spawn the reviewer seats **in parallel**, passing each a **criteria-equivalent**
code-review prompt to the engine's (same completeness / code-quality / security /
error-handling criteria — NOT byte-identical). Include the working-tree diff and
the executor's summary so every seat reviews the same thing. Both seats are the
SAME agent (`crew:reviewer`) with a per-spawn `model` override selecting the
voice:

```
Task(subagent_type="crew:reviewer", model="opus",   prompt="<the assembled code-review prompt>")
Task(subagent_type="crew:reviewer", model="sonnet", prompt="<the assembled code-review prompt>")
```

The assembled review prompt must state this is a **code review** of the task
(`$ARGUMENTS`), include the executor's summary and the working-tree diff, list
the criteria (completeness, code quality, security, performance, error
handling), and ask for findings tagged `[BLOCKING]`/`[MINOR]` + a one-line
verdict.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors,
times out, returns no usable block, or is reported missing/failed by the harness
MUST NOT abort the verification. Catch it, normalize it to the six-field
`ok=False` shape (Step 2c), and continue. A failed Task seat is treated
identically to a failed subprocess seat.

### Step 2c — Normalize Task-seat results to the six-field shape

For each Task seat, normalize its return into the SAME six fields as the
subprocess results:

- `name` = `opus` / `sonnet` (the seat name).
- `model` = the pinned model (`opus` / `sonnet`).
- `ok` = True if the seat returned a usable review block; **False** if it
  returned no usable block, errored, or the harness reports it missing/failed.
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's review text.

A failed Task seat renders exactly like a failed subprocess seat. A
skipped/unspawnable seat renders a clearly-marked skipped block and is excluded
from the verdict math.

### Step 2d — Synthesize the verdict

Read the full panel (subprocess + normalized Task seats, in stable order) and
emit the existing verdict format:

- APPROVED: Work is complete and code quality is acceptable.
- REVISE: Issues found. Classify each as [BLOCKING] or [MINOR].

If APPROVED or only [MINOR] issues, state: `VERDICT: APPROVED`.
If [BLOCKING] issues exist, state: `VERDICT: REVISE` with the list of issues.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the verification and is NEVER silently dropped:
synthesize the verdict from the seats that produced usable output, and render
each failed/skipped seat's block with its diagnostic alongside the successful
ones. ONLY when **zero** seats produced usable output (every subprocess seat
failed AND both Task seats failed) do you skip the verdict and instead report:
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

Original task: $ARGUMENTS

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
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate bl --reason "Advisor verified complete"
```

2. **Summarize what was accomplished** and let the user know the task is complete.

## Early Exit

To exit before completion: `/crew:cancel-build`

---

First: run the activation command. Then proceed to Step 1 (delegate to executor).
