---
description: Delegate a WORK task to ONE non-Claude seat in write mode (engine-resolved default seat): the seat edits the working tree and leaves changes UNCOMMITTED + UNSTAGED for you to review. Override with a leading --seat <name>. Use for delegated edits, not read-only review.
argument-hint: "[--seat <name>] <task description>"
allowed-tools: Bash, Read, Glob, Write
---

[DISPATCH — write-mode single-seat delegation]

$ARGUMENTS

> **What this is.** The EXECUTION complement to `/crew:review` /
> `/crew:debate`: ONE engine-resolved subprocess seat (override with `--seat`)
> at the live working tree with **write** access, the external-model analog of
> `crew:executor`. The seat edits files in place and is instructed to leave
> ALL changes **UNCOMMITTED and UNSTAGED and on the same branch**; you review
> the dirty tree and decide (keep / revert / pipe into `/crew:review`).

> **`allowed-tools` scopes THIS orchestrator only.** The seat's write access
> is governed by the engine's `workspace-write` sandbox flag, not this
> frontmatter. `Write` is listed because step 3 ALWAYS spills the task to a
> file for the engine's `-f`.

> **Seat resolution is ENGINE-OWNED.** This command NEVER reads config and
> NEVER hardcodes a seat: it passes `--seat` ONLY when you explicitly named
> one; otherwise the engine applies `[dispatch].seat` config, then a built-in
> default. The authoritative resolved-seat line is read from the returned
> envelope's `seat` field POST-run (step 4). Per-provider write-mode tuning
> lives under `[dispatch.<kind>]`; `crew dispatch --options` (non-billable)
> lists each provider's keys.

## Step 1 — Parse an optional LEADING `--seat`

`--seat <name>` is recognized **only as the LEADING flag** of `$ARGUMENTS`. A
`--seat` at the END of the task text is task DATA, not a flag. Everything
after the optional leading `--seat <name>` is the TASK.

## Step 2 — Print a generic pre-run banner

```
dispatching in write mode — changes will be left UNCOMMITTED and UNSTAGED for you to review
```

Name the seat in this line ONLY if the user explicitly provided `--seat`;
otherwise say "the default seat" (there is no honest way to know the resolved
seat pre-run).

## Step 3 — Invoke the engine (write-mode dispatch)

**ALWAYS spill the task to a file and pass `-f`, NEVER positional.** Write the
raw task text to the ABSOLUTE path
`<project-root>/.crew/dispatch/<session-id>-task.txt` with the **Write tool**
(exact bytes, no shell, creates the parent dir; substitute your
`CLAUDE_PROJECT_DIR` value for `<project-root>`). The spill is UNCONDITIONAL:
a positional task containing `$(…)`, `$VAR`, backticks, or `"` would be
expanded or mangled by the shell, and the `-f` file also dodges ARG_MAX.
Exactly ONE task source (`-f`).

The Bash recipe's `-f` is the plain RELATIVE `.crew/...` path, double-quoted:
the ENGINE anchors it to the project root (`crew_base()`), never the shell
cwd. No `${…}`/`$(…)`/backticks on the line (they defeat permission
allowlisting). Double quotes tame spaces and globs in a substituted value;
they do NOT neutralize `$(…)` or backticks, which is why the task text itself
never rides the command line.

Substitute your actual session id (the `[Session ID: …]` value) for
`<session-id>` as a literal, NEVER a `${CLAUDE_SESSION_ID}` expansion. Pass
**NO `-o`** (the engine derives the envelope path and prints it). Pass
`--seat <seat>` **ONLY when the user explicitly provided one**:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" dispatch -f ".crew/dispatch/<session-id>-task.txt" --session-id <session-id> --json
```

(When the user named a seat, add `--seat <seat>` to the command.)

**Immediately sweep the task spill**, right after the invocation and BEFORE
checking exit status, so EVERY exit path sweeps it (including the exit-2 STOP
below). A shell `rm` gets no engine anchoring, so it takes the double-quoted
ABSOLUTE literal (the `${…}` ban is on expansions, not literals):

```bash
rm -f "<project-root>/.crew/dispatch/<session-id>-task.txt"
```

## Step 4 — Check exit status, then read the envelope

**Check the dispatcher's exit status FIRST.** On a NONZERO exit (a pre-run
failure: unknown / Task / non-writable seat, or an unsubstituted session-id
placeholder; exit `2`) the engine wrote NO envelope and printed NO path:
surface stderr and STOP. Do NOT Read any JSON path.

On exit `0`, the engine printed the resolved envelope path as the **last
clean line of stdout**. Capture it by reading the Bash tool's stdout directly
(no `$(…)` wrapper), and do NOT construct the filename yourself (you may not
know the resolved seat). **Read** the envelope JSON from that path.

- **If `envelope.ok` is false** (ran-but-failed OR skipped-unavailable; exit
  was still `0`), surface `envelope.error` as a FAILURE banner: `dispatch
  FAILED: <envelope.error>`. Do NOT proceed as if it succeeded, but do NOT
  hide the diff either: a ran-but-failed seat may have left PARTIAL edits, so
  STILL run the read-only step 6 to see and recover whatever landed. (A
  skipped seat never ran, so its diff shows only pre-existing dirt; note
  that.)
- **If `envelope.ok` is true**, print the AUTHORITATIVE resolved-seat banner
  from the envelope: `dispatched seat: <envelope.seat> (<envelope.model>)`;
  omit the parens if `envelope.model` is null.

## Step 5 — Surface the safety-guard warnings (LOUD)

`envelope.guard_warnings` is the list of formatted warning strings the
engine's HEAD/staged/branch guard produced (empty when no guard fired). Relay
each one VERBATIM and LOUD; do NOT reconstruct them from the raw booleans
(the engine owns the exact recovery hints, including the `<unborn>` and
detached-HEAD cases). If empty, say plainly that the seat honored the
uncommitted-and-unstaged contract.

## Step 6 — Show the working-tree changes (READ-ONLY git only)

```bash
git status --short
git --no-pager diff --stat
git --no-pager diff
```

For untracked files (not shown by `git diff`), LIST names + porcelain codes
only; do NOT dump contents (open what you care about yourself). `-z` so odd
filenames don't break iteration:

```bash
git status --porcelain -z
```

**NEVER run `git add` / `git add -N`**: that mutates the index and breaks the
UNSTAGED contract the user relies on when triaging the seat's diff.
Surfacing is read-only only.

> **Dirty-tree attribution.** The shown diff is the UNSTAGED worktree-vs-index
> delta: content the seat STAGED, COMMITTED, or moved by switching branches
> is NOT in it (consult the step 5 guard warnings plus `git status` /
> `git log` for that), and pre-existing dirt is included and is NOT the
> seat's work.

## Step 7 — Report and hand back

Report the seat summary (`envelope.output`) + the diff; the user decides next
(keep, revert selectively, or pipe into `/crew:review`). Do NOT commit,
stage, push, or switch branches yourself. (If step 4 surfaced a FAILURE
banner, report that banner PLUS the recovery diff, never a success summary.)

> **No blanket revert.** Without a pre-run snapshot there is no way to
> isolate the seat's edits from pre-existing changes, so NEVER offer or run
> `git checkout -- .` / `git restore` wholesale. To undo, revert SELECTIVELY,
> naming the specific files or hunks that are the seat's.

---

Subscription safety: the engine drives only an external-CLI subprocess seat.
No `claude -p`, no Anthropic API — a stray `ANTHROPIC_API_KEY` is irrelevant
here.
