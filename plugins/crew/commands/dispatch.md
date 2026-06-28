---
description: Delegate a WORK task to ONE non-Claude seat in write mode (default seat codex; the seat edits the working tree and leaves changes UNCOMMITTED + UNSTAGED for you to review). Override the seat with a leading --seat <name>.
argument-hint: "[--seat <name>] <task description>"
allowed-tools: Bash, Read, Glob, Write
---

[DISPATCH — write-mode single-seat delegation]

$ARGUMENTS

> **What this is.** The EXECUTION complement to `/crew:review` / `/crew:debate`.
> Where review/debate fan a PANEL of seats at a target **read-only**, dispatch
> sends **ONE** chosen subprocess seat (default `codex`, override with `--seat`)
> at the live working tree with **write** access — the external-model analog of
> `crew:executor`. The seat edits files in place and is instructed to leave ALL
> changes **UNCOMMITTED and UNSTAGED and on the same branch**. You then review the
> dirty tree and decide what to do (keep / revert / pipe into `/crew:review`).

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> dispatched seat — the seat's write access is governed by the engine's
> `workspace-write` sandbox flag, not by this frontmatter. `Write` is listed here
> because step 3 may spill a large/multiline task to a file for the engine's `-f`.

> **Seat resolution is ENGINE-OWNED.** This command NEVER reads config and NEVER
> hardcodes a seat. It passes `--seat` to the engine ONLY when you explicitly
> named one; otherwise the engine applies its own resolution (`[dispatch].seat`
> config → built-in `codex`). So the command cannot honestly know the resolved
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
re-announces the exact path it writes, so this is hygiene, not load-bearing):

```bash
rm -f .crew/reviews/<session-id>/dispatch-*.json
```

**Spill the task to a file when (DETERMINISTIC rule):** the task contains a newline
OR exceeds **4096 bytes**. In that case write it to `.crew/dispatch/<session-id>-task.txt`
with the **Write tool** (which creates the `.crew/dispatch/` parent dir
automatically) and pass `-f <file>` to the engine instead of the positional task —
this dodges ARG_MAX and shell-quoting foot-guns. Otherwise pass the task
positionally. Exactly ONE task source.

Then invoke the bare `crew` dispatcher. Substitute your actual session id (the
`[Session ID: …]` value) for `<session-id>` — pass it as a literal `--session-id`
value, NEVER a `${CLAUDE_SESSION_ID}` shell expansion. Pass **NO `-o`** (the engine
derives `dispatch-<seat>.json` and prints the path). Pass `--seat <seat>` **ONLY
when the user explicitly provided one** (omit it otherwise so the engine resolves
the seat itself):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" dispatch "<TASK>" --session-id <session-id> --json
```

(When the user named a seat, add `--seat <seat>` after the task; when the task was
spilled, replace `"<TASK>"` with `-f .crew/dispatch/<session-id>-task.txt`.)

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

The engine's HEAD/staged/branch guard ran in the SAME tree the seat edited. For each
that fired, surface a LOUD warning:

- `envelope.head_moved` is true → "the dispatched seat committed against
  instruction — inspect with `git log -1`; undo with `git reset --soft HEAD@{1}`"
  (or, when `envelope.head_before` is `<unborn>`, the seat made the repo's FIRST
  commit — "undo with `git update-ref -d HEAD`").
- `envelope.staged_changed` is true → "the dispatched seat STAGED changes against
  instruction — unstage with `git reset`".
- `envelope.branch_changed` is true → "the dispatched seat changed branch against
  instruction — return with `git checkout <envelope.branch_before>`".

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

**NEVER run `git add` / `git add -N`** (that mutates the index, breaks the unstaged
contract, and would trip the engine's own staged-changes guard). Surfacing is
read-only only.

> **Dirty-tree attribution.** The shown `git diff` is the FULL working-tree delta —
> if the tree was ALREADY dirty before dispatch, those pre-existing edits are
> included and are NOT solely the dispatched seat's work.

## Step 7 — Report and hand back

Report the seat summary (`envelope.output`) + the diff, and hand back to the user to
decide next: keep the changes, revert them (`git checkout -- .` / `git restore`), or
pipe them into `/crew:review`. Do NOT commit, stage, push, or switch branches
yourself. (If step 4 surfaced a FAILURE banner, report that banner PLUS the
read-only recovery diff of any partial edits — never a success summary.)

---

Subscription safety: the engine drives only the subprocess seat (`codex` / `agy` /
`cursor-*`, all external-CLI auth). No `claude -p`, no Anthropic API — a stray
`ANTHROPIC_API_KEY` is irrelevant here.
