---
description: Delegate a WORK task to ONE non-Claude seat in write mode (engine-resolved default seat): the seat edits the working tree and leaves changes UNCOMMITTED + UNSTAGED for you to review. Override with a leading --seat <name>. Use for delegated edits, not read-only review.
argument-hint: "[--seat <name>] <task description>"
allowed-tools: Bash, Read, Glob, Write
---

[DISPATCH — write-mode single-seat delegation]

$ARGUMENTS

> **What this is.** The EXECUTION complement to `/crew:review` / `/crew:debate`.
> Where review/debate fan a PANEL of seats at a target **read-only**, dispatch
> sends **ONE** chosen subprocess seat (an engine-resolved default seat, override
> with `--seat`) at the live working tree with **write** access, the external-model analog of
> `crew:executor`. The seat edits files in place and is instructed to leave ALL
> changes **UNCOMMITTED and UNSTAGED and on the same branch**. You then review the
> dirty tree and decide what to do (keep / revert / pipe into `/crew:review`).

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> dispatched seat — the seat's write access is governed by the engine's
> `workspace-write` sandbox flag, not by this frontmatter. `Write` is listed here
> because step 3 ALWAYS spills the task to a file for the engine's `-f` (shell-safe;
> the task text never reaches the shell).

> **Seat resolution is ENGINE-OWNED.** This command NEVER reads config and NEVER
> hardcodes a seat. It passes `--seat` to the engine ONLY when you explicitly
> named one; otherwise the engine applies its own resolution (`[dispatch].seat`
> config → a built-in default). So the command cannot honestly know the resolved
> seat before the run — the authoritative resolved-seat line is read from the
> returned envelope's `seat` field POST-run (step 4).

## Step 1 — Parse an optional LEADING `--seat`

`--seat <name>` is recognized **only as the LEADING flag** of `$ARGUMENTS` (the
same place the other crew commands look for their flags). A `--seat` placed at the
END of the task text is treated as task DATA, not a flag — put `--seat` FIRST if
you mean it as a flag. Everything after the optional leading `--seat <name>` is the
TASK.

## Step 2 — Print a generic pre-run banner

Before invoking, print a one-line banner:

```
dispatching in write mode — changes will be left UNCOMMITTED and UNSTAGED for you to review
```

Name the seat in this line ONLY if the user explicitly provided `--seat` (e.g.
"dispatching to `agy` in write mode …"); otherwise say "the default seat" — do NOT
name or guess it (this command has no honest way to know the resolved seat
pre-run).

## Step 3 — Invoke the engine (write-mode dispatch)

**Pre-run hygiene.** Remove any stale dispatch envelope from an earlier run in the
reused session dir so it can't be mistaken for this run's output (the engine
re-announces the exact path it writes, so this is hygiene, not load-bearing). The
`rm` recipe uses the SHELL EXPANSION `"${CLAUDE_PROJECT_DIR:-$PWD}/.crew/..."` (the
shell resolves it at runtime, so a project root containing `$(…)`, backticks,
`$VAR`, or a quote cannot execute or mangle the command): the engine writes
`dispatch-<seat>.json` under its anchored `crew_base()/.crew/reviews/<session-id>/`,
so a bare `.crew/...` `rm` run from a drifted cwd would miss it:

```bash
rm -f "${CLAUDE_PROJECT_DIR:-$PWD}/.crew/reviews/<session-id>/dispatch-"*.json
```

**ALWAYS spill the task to a file and pass `-f`, NEVER positional.** Write the raw
task text to the ABSOLUTE anchored path
`<project-root>/.crew/dispatch/<session-id>-task.txt` with the **Write tool** (which
writes the exact bytes, no shell involved, and creates the `.crew/dispatch/` parent
dir automatically; substitute your `CLAUDE_PROJECT_DIR` value for `<project-root>`
in the WRITE-tool path, safe as a literal there), then pass `-f <file>` to the
engine. The Bash recipe's `-f` uses the SHELL EXPANSION
`"${CLAUDE_PROJECT_DIR:-$PWD}/.crew/..."` (resolving to the SAME `.crew` at
runtime): `dispatch` resolves an explicit `-f` against the shell cwd, which need
not be the project root, so a bare `.crew/...` could split the tree. This is
UNCONDITIONAL (not gated on a newline/byte-size trigger): a positional task
containing `$(…)`, `$VAR`, backticks, or `"` would be expanded or mangled by the
shell when the Bash tool runs the command, so the task NEVER goes on the command
line. The `-f` file fully dodges shell quoting/expansion AND ARG_MAX. Exactly ONE
task source (`-f`).

Then invoke the bare `crew` dispatcher. Substitute your actual session id (the
`[Session ID: …]` value) for `<session-id>` — pass it as a literal `--session-id`
value, NEVER a `${CLAUDE_SESSION_ID}` shell expansion. Pass **NO `-o`** (the engine
derives `dispatch-<seat>.json` and prints the path). Pass `--seat <seat>` **ONLY
when the user explicitly provided one** (omit it otherwise so the engine resolves
the seat itself):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" dispatch -f "${CLAUDE_PROJECT_DIR:-$PWD}/.crew/dispatch/<session-id>-task.txt" --session-id <session-id> --json
```

(When the user named a seat, add `--seat <seat>` to the command.)

**Immediately sweep the task spill.** The returned process no longer needs the `-f`
file by the time the call returns (it has either read the task or exited before
doing so), so delete it NOW, right after the invocation and BEFORE checking exit
status. Doing it here (not at the end) means EVERY exit path
sweeps it, including the exit-2 STOP in step 4 that returns before the report:

```bash
rm -f .crew/dispatch/<session-id>-task.txt
```

## Step 4 — Check exit status, then read the envelope

**Check the dispatcher's exit status FIRST.** On a NONZERO exit (a pre-run failure
— unknown / Task / non-writable seat, or an unsubstituted session-id placeholder —
exit `2`) the engine wrote NO envelope and printed NO path: surface the
dispatcher's stderr and STOP. Do NOT Read any JSON path.

On exit `0`, the engine printed the resolved envelope path as the **last clean line
of stdout**. **Capture that printed path by reading the last clean line of the Bash
tool's stdout directly** — do NOT wrap the invocation in a shell `$(…)` command
substitution, and do NOT construct `dispatch-<seat>.json` yourself (you may not know
the resolved seat). **Read** the envelope JSON from that path with the Read tool.

- **If `envelope.ok` is false** (a ran-but-failed seat — timeout / CLI error — OR a
  skipped-unavailable seat; the exit was still `0`), surface `envelope.error` as a
  clear FAILURE banner: `dispatch FAILED: <envelope.error>`. Do NOT proceed as if it
  succeeded (no success banner, no keep / pipe-to-review suggestion). But do **NOT**
  hide the diff: a ran-but-failed seat may have left PARTIAL edits in the working
  tree (left by design), so STILL run the read-only `git status` / `git diff` of
  step 6 so you can SEE and recover whatever landed. (A skipped-unavailable seat
  never ran, so its diff shows only pre-existing dirt, if any — note that.)
- **If `envelope.ok` is true**, print the AUTHORITATIVE resolved-seat banner sourced
  from the envelope: `dispatched seat: <envelope.seat> (<envelope.model>)`. If
  `envelope.model` is null, omit the parens: `dispatched seat: <envelope.seat>`.

## Step 5 — Surface the safety-guard warnings (LOUD)

The engine's HEAD/staged/branch guard ran in the SAME tree the seat edited, and the
result is ALREADY formatted in the envelope: `envelope.guard_warnings` is the list of
warning strings (empty when no guard fired), the SAME strings the engine's human-mode
output prints. Relay each one VERBATIM and LOUD. Do NOT reconstruct them from the raw
`head_moved` / `staged_changed` / `branch_changed` booleans: the engine owns the exact
recovery hints, including the first-commit (`<unborn>`) and detached-HEAD special
cases, so re-deriving them here would duplicate that logic and risk missing an edge.
If `guard_warnings` is empty, the seat honored the uncommitted-and-unstaged contract;
say so plainly.

## Step 6 — Show the working-tree changes (READ-ONLY git only)

Show what changed with read-only git that NEVER touches the index. These run in the
SAME tree the guard inspected (the Bash cwd is `CLAUDE_WORKING_DIRECTORY`):

```bash
git status --short
git --no-pager diff --stat
git --no-pager diff
```

For untracked files (not shown by `git diff`), LIST them read-only — names + their
porcelain status codes — with the single command below. The `-z` null-termination is
mandatory so filenames with spaces / quotes / newlines don't break iteration. Just
LIST the entries; do **NOT** dump their contents (a huge or binary untracked file
must not be spilled into the transcript — open what you care about yourself):

```bash
git status --porcelain -z
```

**NEVER run `git add` / `git add -N`** (that mutates the index and breaks the
UNSTAGED contract the user relies on when triaging the seat's diff: dispatch's
whole point is to leave the seat's edits unstaged for review). This is NOT about
the engine's staged-changes guard: that guard's after-capture already ran by the
time you surface the diff, so staging here would trip nothing. Surfacing is
read-only only.

> **Dirty-tree attribution.** The shown `git diff` is the UNSTAGED working-tree
> delta (worktree vs the INDEX): the unstaged changes in the tree, which is NOT a
> complete picture of what the seat did (and, on a pre-dirty tree, not solely the
> seat's work either). Content the seat STAGED (`git add`),
> COMMITTED, or moved by switching branches is NOT in this diff: a staged hunk lives
> in the index, a commit would be measured against the new HEAD, and a branch switch
> describes a different branch. So if a guard WARNING fired in step 5 (HEAD moved,
> staged changed, or branch changed), that content is NOT surfaced here: consult the
> guard warnings plus `git status` / `git log` to see it, not the diff alone. And if
> the tree was ALREADY dirty before dispatch, those pre-existing edits are included
> in the diff and are NOT solely the seat's work.

## Step 7 — Report and hand back

Report the seat summary (`envelope.output`) + the diff, and hand back to the user to
decide next: keep the changes, revert selectively, or pipe them into `/crew:review`.
Do NOT commit, stage, push, or switch branches yourself. (If step 4 surfaced a
FAILURE banner, report that banner PLUS the read-only recovery diff of any partial
edits, never a success summary.)

> **No blanket revert.** Without a pre-run snapshot of the tree there is no way to
> isolate the seat's edits from changes that were ALREADY present, so NEVER offer or
> run `git checkout -- .` / `git restore` wholesale: that can destroy pre-existing
> work the seat never touched. To undo, review the diff and revert SELECTIVELY,
> naming the specific files or hunks that are the seat's.

---

Subscription safety: the engine drives only an external-CLI subprocess seat (all
external-CLI auth). No `claude -p`, no Anthropic API — a stray
`ANTHROPIC_API_KEY` is irrelevant here.
