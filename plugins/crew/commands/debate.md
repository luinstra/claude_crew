---
description: Crew-native council — single-round multi-model take on a question (codex + agy + opus + sonnet)
argument-hint: "<question>"
allowed-tools: Bash, Task, Read, Glob
---

[CREW-NATIVE COUNCIL]

$ARGUMENTS

> **`allowed-tools` scopes THIS orchestrator only.** It grants nothing to the
> seats spawned below — seat tool access is governed per-seat by the reviewer
> agent frontmatter (`Read, Grep, Glob`) and the engine's sandbox flags.

## What this does

This is a **lightweight, single-round council** — NOT a multi-round debate.
The same question fans out across a multi-model panel; each seat gives an
INDEPENDENT, CRITICAL take (its direct answer/recommendation + the strongest
objection to that take + the key risks/tradeoffs others might miss). Then YOU
synthesize: **areas of agreement / key disagreements / recommendation**.

Single round only — no rebuttals, no round-threading, no judge-as-separate-
model (you are the synthesizer). Two seat kinds:

- **subprocess seats** — `codex` and `agy` via the Python engine.
- **task seats** — `crew:reviewer` spawned twice (at `model: opus` and
  `model: sonnet`) via the Task tool (in-session, on the subscription — no
  `claude -p`, no API key).

Default panel: **codex + agy + opus + sonnet** (agy is best-effort). This is
safe because a failed/skipped seat NEVER sinks the council (see Step 5) — the
synthesis is built from whichever seats succeed.

## Step 1 — Fan out subprocess seats (one Bash call)

Run the engine once for the subprocess seats. Use the bare-script path (its
top-of-file `sys.path` guard makes package imports resolve). **Write the question
to a file and pass it with `-f`** — a positional question is brittle for quotes,
newlines, and shell metacharacters, and `-f` sidesteps all of it. Write the file
with a **quoted** heredoc (`<<'EOF'` — no shell expansion, so the question text
stays literal):

```bash
mkdir -p .crew/debates
cat > .crew/debates/.council-question.txt <<'CREW_QUESTION_EOF'
<the question text, verbatim — exactly what the user asked>
CREW_QUESTION_EOF
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" council -f .crew/debates/.council-question.txt --seats codex,agy --json
```

- Use this stable `python …cli.py council …` form (it hits the allowlist).
  NEVER call `agy -p` / `codex exec` directly, and NEVER hand off to any
  external debate plugin/shell — this council is fully self-contained in crew.
- Pick a heredoc delimiter the question can't contain (`CREW_QUESTION_EOF` is
  safe in practice).
- **Reference code, don't paste it.** The seats run in the repo. If the question
  is about a diff, branch, or files, point them at it — *"review the changes on
  branch X vs main: run `git diff main...HEAD` and read the files"* — rather than
  inlining a large diff. Keeps the prompt small, sidesteps agy's ARG_MAX cap, and
  avoids synthesis-context bloat.
- The engine returns a JSON array of result objects, each with the six fields:
  `name, model, ok, output, error, elapsed`.
- **Never choke on a seat failure:** the engine NEVER raises out of the
  fan-out. Any failed/skipped subprocess seat comes back as an `ok=False` entry
  with a diagnostic in `error`; the other seats still return. The engine exits
  nonzero with an `all N seats failed: <diagnostics>` message on stderr ONLY
  when every subprocess seat failed — and even then it emits the full JSON
  array (no traceback). A nonzero engine exit does NOT abort the council: fold
  the Task seats in and synthesize from whatever succeeded.

## Step 2 — Fan out Task seats (parallel)

Spawn the reviewer seats **in parallel**, passing each the SAME council prompt
(criteria-equivalent to the engine's `council` template): the question + ask
for a direct take + the strongest objection + key risks/tradeoffs;
evidence-based, no rubber-stamp, no manufactured contrarianism. Both seats are
the SAME agent (`crew:reviewer`) with a per-spawn `model` override selecting the
voice:

```
Task(subagent_type="crew:reviewer", model="opus",   prompt="<the council prompt with the question inlined>")
Task(subagent_type="crew:reviewer", model="sonnet", prompt="<the council prompt with the question inlined>")
```

The assembled council prompt must state that this is a free-form council
question (not a plan/code review), include the question text, and ask for the
three things above.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors,
times out, returns no usable block, or is reported missing/failed by the
harness MUST NOT abort the council. Catch it, normalize it to the six-field
`ok=False` shape (Step 3), and continue with the other seats.

## Step 3 — Normalize Task-seat results to the six-field shape

For each Task seat, normalize its return into the SAME six fields as the
subprocess results:

- `name` = `opus` / `sonnet` (the seat name).
- `model` = the pinned model (`opus` / `sonnet`).
- `ok` = True if the seat returned a usable take; **False** if it returned no
  usable block, errored, or the harness reports it missing/failed.
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's council take.

A failed Task seat renders exactly like a failed subprocess seat — its error
block shows and never suppresses the others. A skipped/unspawnable seat renders
a clearly-marked skipped block and is excluded from the synthesis.

## Step 4 — Synthesize

Read the full panel (subprocess + normalized Task seats, in stable order) and
render them side-by-side. Then write a synthesis with exactly these sections:

- **Areas of agreement** — where the seats converge.
- **Key disagreements** — where they diverge, and why.
- **Recommendation** — your single synthesized recommendation, accounting for
  the strongest objections and tradeoffs surfaced.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the council and is NEVER silently dropped.
Synthesize from the seats that produced usable output and render the
failed/skipped ones with their diagnostics. ONLY when **zero** seats produced
usable output (every subprocess seat failed AND both Task seats failed) do you
skip the synthesis and report: `could not convene — all seats failed:
<per-seat diagnostics>`.

## Step 5 — Log the transcript

Write the per-seat outputs and your synthesis to a timestamped directory under
`.crew/debates/`. Build the directory with a Bash `date` call (slugify the
question into a short kebab-case slug — first few words, lowercased,
non-alphanumerics to `-`):

```bash
DIR=".crew/debates/$(date +%Y%m%d-%H%M%S)-<slug>"
mkdir -p "$DIR"
```

Then write the panel + synthesis into that directory (e.g. `transcript.md` with
each seat's block and the agreement/disagreement/recommendation synthesis).
Tell the user the path you logged to.

---

Subscription safety: the engine drives only `codex`/`agy` (external-CLI auth);
Claude voices are in-session Task seats. No `claude -p`, no Anthropic API — a
stray `ANTHROPIC_API_KEY` is irrelevant here.
