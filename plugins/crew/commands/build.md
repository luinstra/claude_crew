---
description: Start verified development loop with multi-model review before completion
argument-hint: "<task description>"
allowed-tools: Bash, Task, Read, Glob
---

[BUILD LOOP STARTING]

$ARGUMENTS

## Panel options (optional)

Flags at the **start** of `$ARGUMENTS` choose which models verify; the rest is
the task. Default (no flag) = the full panel.

- `--panel full` = `codex,cursor-gpt,cursor-gemini,cursor-glm,cursor-composer,opus,sonnet`
  (default) · `--panel lite` = `opus,sonnet` · `--panel solo` = `opus`
- `--seats <list>` = an explicit comma-list of any registered seat
  (`codex, agy, cursor-gpt, cursor-gemini, cursor-glm, cursor-composer, opus, sonnet`;
  e.g. `--seats codex,opus`). `agy` is opt-in — works via `--seats agy` but is
  not in the default panel. `--seats` wins if both are given.

Resolve the flags to a **seat list** once, then split it:
- **subprocess seats** = the `codex`/`cursor-*` entries (and opt-in `agy`) → pass
  as the engine's `--seats` (Step 2a). **If the list has NO subprocess seat, SKIP
  Step 2a entirely.**
- **task seats** = the `opus`/`sonnet` entries → in Step 2b spawn `crew:reviewer`
  ONLY for those (zero, one, or both).

Below, **"the task"** means `$ARGUMENTS` with these panel flags removed — use it
wherever a step references the task. Keep the raw `$ARGUMENTS` (flags included)
only in the `crew-state.py --prompt` so the panel survives across iterations. A
one-seat panel is fine — synthesis / never-choke handle any count down to one.

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

- **subprocess seats** — the `codex`/`cursor-*` entries (and opt-in `agy`) of
  the resolved panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries of the resolved panel, each a
  `crew:reviewer` spawned via the Task tool (in-session, on the subscription —
  no `claude -p`, no API key).

The panel is whatever the Panel-options flags resolved to (default **codex +
cursor-gpt + cursor-gemini + cursor-glm + cursor-composer + opus + sonnet**). Only fan out the
seats in that list. A failed/skipped seat
NEVER aborts the verification (see "Synthesize" below) — the verdict is
synthesized from whichever seats succeed.

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob, Bash`) and the engine's sandbox flags.

### Step 2a — Fan out subprocess seats (one Bash call)

**Skip this step if the resolved panel has no subprocess seat** (e.g.
`--panel lite`) — go straight to Step 2b. Otherwise run the engine once over the
working-tree diff, passing ONLY the resolved subprocess seats. Use the bare-script
path (its top-of-file `sys.path` guard makes package imports resolve):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" review working-tree --seats <resolved codex/cursor-* seats> --json
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

Spawn a reviewer seat **in parallel for each `opus`/`sonnet` entry in the
resolved panel** (zero, one, or both — skip this step if neither is present). Do
NOT hand-write each seat's prompt: **render it from the engine** so every seat —
subprocess AND Task — reviews ONE identical target. `render` is the single prompt
source, and it resolves `working-tree` the SAME way Step 2a's engine `review`
did — via the compound `Target.diff_cmd` that INCLUDES untracked files. That
matters most here: `/crew:build` verification routinely follows a step where the
executor just CREATED new files, and a hand-built `git diff HEAD` prompt would
make the Claude seats miss exactly those untracked files while the subprocess
seats see them. Render each `opus`/`sonnet` seat's prompt over the SAME `working-tree`
target:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render working-tree --mode review --seat-role opus   -o .crew/.prompt-opus.txt
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" render working-tree --mode review --seat-role sonnet -o .crew/.prompt-sonnet.txt
```

Then dispatch each seat with the rendered text as its prompt, appending the
executor's summary inline (it's small) so every seat shares the same intent. Each
seat is the SAME agent (`crew:reviewer`) with a per-spawn `model` override
selecting the voice (spawn only the ones in the panel):

```
# Spawn ONLY the opus/sonnet seats in the resolved panel — e.g. --panel solo
# spawns just the opus line; --panel lite spawns both; full spawns both too.
Task(subagent_type="crew:reviewer", model="opus",   prompt="<contents of .crew/.prompt-opus.txt> + executor summary")
Task(subagent_type="crew:reviewer", model="sonnet", prompt="<contents of .crew/.prompt-sonnet.txt> + executor summary")
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

For each Task seat, normalize its return into the SAME six fields as the
subprocess results:

- `name` = `opus` / `sonnet` (the seat name).
- `model` = the pinned model (`opus` / `sonnet`).
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
ones. ONLY when **zero** seats in the panel produced usable output do you skip
the verdict and instead report:
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
${CLAUDE_PLUGIN_ROOT}/scripts/crew-state.py deactivate bl --reason "Advisor verified complete"
```

2. **Summarize what was accomplished** and let the user know the task is complete.

## Early Exit

To exit before completion: `/crew:cancel-build`

---

First: run the activation command. Then proceed to Step 1 (delegate to executor).
