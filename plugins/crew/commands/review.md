---
description: Multi-model review of a plan OR code diff (default panel codex + agy + cursor-auto + cursor-composer + opus + sonnet; narrow with --panel/--seats)
argument-hint: "<the plan | the code | a .md path | a git scope>"
allowed-tools: Bash, Task, Read, Write, Glob
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
the review request. Default (no flag) = **OMIT the `--panel`/`--seats` flag
entirely** and let `review-prep` apply the configured default. The default
`default_panel` resolves across tiers (precedence CLI flag > per-repo
`.crew/config.toml` > global `~/.crew-config.toml` > built-in **full**; no env
tier). Do NOT hardcode `--panel full` — passing it would override a user's
configured default.

- `--panel full|lite|solo|cursor|<custom>` — a named preset. The four are
  built in; `<custom>` is any roster defined under `[panels]` in per-repo or
  global config (e.g. `[panels].quick`). `--seats <comma-list>` — an explicit
  subset of any registered seat (e.g. `--seats codex,opus`); `--seats`
  wins if both are given. `cursor-gemini`, `cursor-glm`, and `cursor-gpt` are opt-in (add via `--seats`).
  Opt-in `opus-4.6` is a third Claude voice pinned to a version-locked model — add
  it explicitly (e.g. `--seats codex,opus,sonnet,opus-4.6`).
- **The engine resolves the preset — the orchestrator does NOT.** `review-prep`
  (Step 3) takes the user's `--panel`/`--seats` and resolves it into
  `subprocess_seats` (the `codex`/`agy`/`cursor-*` entries),
  `task_seats` (the Claude voices), and `task_seat_models` (each Task seat's
  model pin). The orchestrator never defines presets, classifies seat names, or
  hardcodes a model pin — it passes the flag through and reads the JSON. **Pass
  `--panel`/`--seats` ONLY when the user named a panel or seats** (an explicit
  flag, or natural-language intent like "use the lite panel"); otherwise OMIT the
  flag and let `review-prep` apply the configured/built-in default.

## What this does

The same review prompt fans out across a multi-model panel and you synthesize
the results into the existing `APPROVED / REVISE / [BLOCKING] / [MINOR]`
verdict. Two seat kinds:

- **subprocess seats** — the `codex`/`agy`/`cursor-*` entries of
  the resolved panel, via the Python engine.
- **task seats** — the `opus`/`sonnet` entries of the resolved panel, each a
  `crew:reviewer` via the Task tool (in-session, on the subscription — no
  `claude -p`, no API key).

The panel is whatever the Panel-options flags resolved to (default **codex +
agy + cursor-auto + cursor-composer + opus + sonnet**); only fan out the
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

## Step 3 — Fan out subprocess seats (one visible shell PER SEAT)

**ALWAYS run `review-prep` (3.0–3.1 below) — it is the SOLE source of the
`task_seats`/`task_seat_models` Step 4 needs.** Skip ONLY the per-seat subprocess
fan-out (3.2) when `subprocess_seats` is empty (e.g. `--panel lite`/`solo`) — go
straight to Step 4. (You STILL persist the Task seats in Step 5 and run the SAME
grouped `collect` in Step 5.5 over the Task seats alone — Decision-I; there is no
"synthesize from raw Task returns" path anymore.) Otherwise fan the subprocess
seats out
**one `crew run` call per seat, in parallel** — each is a separate, visible,
individually-killable shell (the same per-seat shape `/crew:debate`'s multi-round
path uses, instead
of one opaque `review` call hiding them in an internal thread pool). Use the
bare-script path (its top-of-file `sys.path` guard makes package imports resolve).

**3.0–3.1 — prep the fan-out in ONE call.** `review-prep` does the deterministic
prep — resolves the target, resolves `--panel`/`--seats` into the SUBPROCESS seat
list (group tokens like `cursor` expanded via the registry) AND the Task-seat
split, and stages the ONE shared subprocess prompt (`--stage`, byte-identical to a
standalone `render --mode review --stage`) — and PRINTS
`{prompt_path, subprocess_seats, task_seats, task_seat_models}` as one-line JSON.
It runs NOTHING (the per-seat loop below stays yours). Substitute your real id for
`<session-id>` (the `[Session ID: …]` value; never the literal placeholder or a
`${…}` expansion). **Quote `"<TARGET>"`/`"<BASE>"`** (a plan path can contain
spaces); `<TARGET>` is the plan `.md` path or git scope from Step 1
(`working-tree`/`auto` for a working-tree code review). Reference mode is the
default — each seat fetches the diff/plan itself; add `--inline-diff` to embed it
instead (rarely needed — it is forwarded to the staged prompt):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-prep "<TARGET>" --base "<BASE>" --session-id <session-id>
```

**OMIT `--panel`/`--seats` when the user named no panel** (as shown above) — so
`review-prep` applies the repo's configured `default_panel` or the built-in
**full** panel. Pass `--panel <preset>` / `--seats <subset>` ONLY when the user
chose one explicitly. `review-prep` resolves BOTH seat
kinds: Step 4 reads `task_seats` + `task_seat_models` from this same JSON (it no
longer classifies Claude seats itself), and the engine still EXECUTES only the
subprocess seats. **Parse the one-line JSON** it prints (read it
directly from the command's stdout — it is NOT a shell `$(…)` capture): use
`subprocess_seats` to iterate the per-seat loop AND (JOINED comma-separated) as
part of the `collect --seats` list in Step 5.5. The per-seat `run` DERIVES its
`-f` from `--session-id` (= `prompt_path`,
`.crew/reviews/<session-id>/prompt-seat.txt`), so you do NOT pass `prompt_path` as
`-f` yourself (see 3.2).
**If `subprocess_seats` is empty** (a Claude-only `--panel lite`/`solo` resolves to
no subprocess seats, so `prompt_path` is `""` and nothing was staged), **SKIP only
the 3.2 per-seat loop** — go straight to Step 4. You STILL persist the Task seats
(Step 5) and run the SAME grouped `collect` (Step 5.5) with `--seats
<dot-stripped task_seats only>`; the grouped `panel.md` then covers the Claude-only
roster (Decision-I).

**3.2 — run EACH seat in its own parallel shell.** First, **clear any pre-existing
`<seat>.json`** for the seats you are about to run — the session dir
`.crew/reviews/<session-id>/` is REUSED across reviews, so a leftover file from an
EARLIER panel (or from a seat whose shell you later kill) would otherwise be read
by `collect` as a FRESH result, folding stale data into this verdict. After
clearing, a seat that doesn't write a fresh file this run correctly renders as
SKIPPED, not stale-OK. Remove the expected target file for each seat first:

```bash
rm -f .crew/reviews/<session-id>/<seat>.json
```

Then, for every seat in `subprocess_seats` (from the 3.0–3.1 prep JSON), launch a
SEPARATE `crew run <seat>` Bash call, all concurrently (e.g. background calls) so
they are distinct shells you can watch and kill individually. Passing
`--session-id <session-id>` lets `run` DERIVE both paths from
`.crew/reviews/<session-id>/`: `-f` = `prompt-seat.txt` (the `prompt_path` from
prep) and `-o` = `<seat>.json`:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" run <seat> --session-id <session-id> --json
```

- `run --json` ALWAYS exits 0 and writes the six-field result
  (`name, model, ok, output, error, elapsed`) — even a failed/skipped seat lands
  as `ok=False` with a diagnostic. So **per-seat never-choke is automatic**: one
  seat failing can't sink the others, and there is no all-failed abort to handle —
  the Step 5.5 collect reads EXACTLY the named seats and renders a missing seat as
  a labeled SKIPPED block.
- Same model / sandbox / auth-banner / ARG_MAX handling as the old single-call
  path — `run` dispatches through the identical provider machinery.

**3.3 — do NOT collect yet.** The subprocess seats are NOT collected here on
their own anymore. The WHOLE panel — subprocess AND Task seats — flows through ONE
grouped `collect` in **Step 5.5**, after the Task seats are persisted. Just launch
the 3.2 shells (in parallel) and move on to Step 4; Step 5.5 owns the
wait-for-both barrier and the single collect.

## Step 4 — Fan out Task seats (parallel)

Spawn a reviewer seat **in parallel for each entry in `task_seats`** (from the
3.0–3.1 `review-prep` JSON — skip this step if `task_seats` is empty). Do
NOT hand-write each seat's prompt: **render it from the engine** so every seat —
subprocess AND Task — reviews ONE identical target. `render` is the single prompt
source, and it resolves the working-tree target the SAME way the engine's
`review` step did (compound `Target.diff_cmd` including untracked files), so a
Claude seat can't miss new files a subprocess seat sees. Render each Task seat's
prompt over the SAME resolved `<TARGET>` (and `--base "<BASE>"`) you passed the
engine in Step 3, staging ALL of them in ONE `--stage-all` call fed the
**comma-joined `task_seats`** from the prep JSON (NOT a hardcoded role list):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" render "<TARGET>" --mode review --base "<BASE>" --stage-all <comma-joined task_seats from the review-prep JSON> --session-id <session-id>
```

**Skip this `--stage-all` call entirely if `task_seats` is empty** (a
subprocess-only panel like `--panel cursor`). A Task seat name with a `.` (e.g.
`opus-4.6`) stages with the dot stripped from the filename
(`prompt-opus-46.txt`) — the same derivation the spawn line below reads, so the
staged file and the read file can never diverge.

(`<TARGET>` is the plan `.md` path or git scope from Step 1; `<BASE>` is the same
base passed in Step 3 — drop `--base` for a plan-file or working-tree target.)
`--stage-all` stages one LABELED prompt PER comma-listed role in a SINGLE call —
`.crew/reviews/<session-id>/prompt-<role>.txt` for each, each carrying its own
"acting as the **<role>** seat" label — and prints a JSON `{role: path}` map to
stdout. It is session-scoped so concurrent sessions never clobber each other.
**Substitute your actual session id for `<session-id>`** — the `[Session ID: …]`
value from the SessionStart context (the same id `crew state` uses). Pass it as a
literal `--session-id` value — NOT the placeholder text, and NOT a
`${CLAUDE_SESSION_ID}` shell expansion (a `$…` expansion isn't allowlistable and
isn't reliably exported to the command shell; the engine resolves the id itself,
arg → env). The engine **rejects an unsubstituted `<…>` placeholder** with a loud
error, so a missed substitution fails fast instead of silently sharing one dir.
`.crew/` is gitignored. **Read** each path from the JSON map with the Read tool
and pass its contents to the matching seat.
Then dispatch each seat with the rendered text as its prompt — the SAME agent
(`crew:reviewer`) with a per-spawn `model` override selecting the voice. **Iterate
`task_seats` from the prep JSON**; for each `<seat>` read its model from
`task_seat_models[<seat>]` and its prompt from the staged
`prompt-<seat, dot-stripped>.txt` (e.g. `opus-4.6` → `prompt-opus-46.txt`). Do NOT
hardcode seat names or model pins — both come from the JSON:

```
# for each <seat> in task_seats:
Task(subagent_type="crew:reviewer", model="<task_seat_models[<seat>]>", prompt="<contents of .crew/reviews/<session-id>/prompt-<seat, dot-stripped>.txt>")
```

> **`opus-4.6` caveat:** the `claude-opus-4-6` pin (from `task_seat_models`) is
> checked against your org's
> model allowlist — if 4.6 isn't allowed it **silently falls back** to the
> inherited model (you'd get another 4.8, with no error). Only trust the version
> spread if 4.6 is actually available to you.

The rendered prompt already states plan-vs-code, tells the seat how to reach the
target itself (the plan file path, or the diff command — reference mode), lists
the criteria, and asks for per-criterion PASS/FAIL + `[BLOCKING]`/`[MINOR]`
findings + a one-line verdict. `crew:reviewer` has `Read, Grep, Glob, Bash` for
exactly this (read-only git/inspection). Because every seat's prompt comes from
the one `render` source, the panel reviews a single identical target — no
embedded-vs-referenced or tracked-vs-untracked divergence.

> **The Task RESULT is the only completion signal.** Each `Task(...)` call
> RETURNS the seat's final message as its tool result — that returned text IS the
> seat's review *and* the proof it finished. Take `ok`/`output` (Step 5) straight
> from that returned result. **NEVER judge a seat by a proxy** — its output-file
> byte size, transcript length, a notification you think you missed, or elapsed
> time. Those race the transcript flush and have falsely declared a finished seat
> "dead" while it was still writing a complete review, throwing away good work. A
> seat that has not returned yet is **still running, not failed** — wait for its
> result. Mark a seat `ok=False` ONLY when its Task actually returns an error,
> returns no usable review block, or the harness itself reports it failed/missing.

**Never choke on a Task-seat failure:** a `crew:reviewer` spawn that errors,
times out, returns no usable block, or is reported missing/failed by the
harness MUST NOT abort the review. Catch it, normalize it to the six-field
`ok=False` shape (Step 5), and continue with the other seats. A failed Task seat
is treated identically to a failed subprocess seat.

## Step 5 — Normalize Task-seat results to the six-field shape

For each Task seat **in `task_seats`** (from the Step 3.0–3.1 `review-prep` JSON —
NOT a hardcoded `opus`/`sonnet` pair), normalize its return into the SAME six
fields as the subprocess results (the Step 1.0 contract):

- `name` = the seat's dot-stripped name (the same `[A-Za-z0-9_-]`-only slug
  `_seat_role_slug` in `cli.py` derives — the ONE canonical dot-strip source, so
  `opus-4.6` → `opus-46` and a dotted role never drifts between staging, persist,
  and collect).
- `model` = the pinned model from `task_seat_models[<seat>]` (e.g. `opus`,
  `sonnet`, or the version-locked `claude-opus-4-6` for `opus-4.6`).
- `ok` = True if the seat's **returned result** is a usable review block;
  **False** only if that returned result is an error, has no usable block, or the
  harness reports the Task failed/missing. (Judge from the returned result — never
  from a file size or any other proxy.)
- `error` = a populated diagnostic when `ok=False` (never a fabricated empty
  `ok=True` block).
- `elapsed` = best-effort wall time (0.0 if unavailable — diagnostic only).
- `output` = the seat's review text, taken from its returned Task result.

A failed Task seat renders exactly like a failed subprocess seat — its error
block shows and never suppresses the others. A skipped/unspawnable seat renders
a clearly-marked skipped block and is excluded from the verdict math.

**Persist each normalized Task seat to disk so it flows through the SAME grouped
`collect` as the subprocess seats** (Decision-H). For EACH entry in `task_seats`
(from the prep JSON — NOT a hardcoded `opus,sonnet` pair), compute its
dot-stripped filename the SAME way `render --stage-all` does (strip any char
outside `[A-Za-z0-9_-]`, so `opus-4.6` → `opus-46`), clear any stale file, then
Write the six-field dict:

```bash
rm -f .crew/reviews/<session-id>/<dot-stripped seat>.json
```

Then use the **Write tool** (no shell heredoc) to write the normalized
`ProviderResult` — the exact six fields `{name, model, ok, output, error,
elapsed}` — to `.crew/reviews/<session-id>/<dot-stripped seat>.json` as JSON. Use
the dot-stripped name as BOTH the filename and the `name` field's seat label
(e.g. `opus-46.json` with `"name": "opus-46"`), so the persisted filename, the
`collect --seats` entry, and the digest column label are identical end-to-end and
a dotted name never trips collect's charset guard (Decision-J).

## Step 5.5 — Wait-for-both, repair non-compliant seats, then collect ONCE

**Wait-for-BOTH barrier.** Before collecting, wait for BOTH (a) every Step 3.2
subprocess `crew run <seat>` shell to EXIT, AND (b) every Step 4 Task seat to
return AND be persisted in Step 5. A seat whose `<seat>.json` is not written yet
is STILL RUNNING, not skipped — collecting early would silently drop it.

Define `<ran_seats>` = the comma-join of `subprocess_seats` (resolved order from
the prep JSON) **then** the dot-stripped `task_seats` (Decision-J order). In the
Claude-only branch (`subprocess_seats` empty) `<ran_seats>` is just the
dot-stripped `task_seats`.

**Repair non-compliant seats (per-seat haiku reformat).** A seat that ignored the
structured `## FINDINGS` schema can't be grouped — give it ONE cheap repair pass
so its findings still fold into the digest. First ask the engine which seats did
not parse:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --report-unparsed
```

This prints the seat names (one per line) whose output ran but did NOT parse into
structured findings. For EACH such `<seat>`:

1. **Read** its raw output — `.crew/reviews/<session-id>/<seat>.json` — and take
   the `output` field.
2. Spawn the cheap **formatter** agent to reformat that text into the schema (a
   FAITHFUL transform — it must NOT invent, drop, or re-judge findings, only
   reshape them):

   ```
   Task(subagent_type="crew:formatter", model="haiku",
        prompt="Reformat this review into the FINDINGS schema verbatim — do not
   invent or drop findings, only reformat:\n\n<the seat's raw output field>")
   ```

3. Write the formatter's returned text into the seat file with the engine
   (it writes to a SEPARATE `repaired_output` field, NEVER overwriting the
   original `output`) — pass the text on stdin or via a temp file with `-f`:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/crew" repair-seat --seat .crew/reviews/<session-id>/<seat>.json -f <temp-file-with-reformatted-text>
   ```

`repair-seat` is **non-destructive**: it populates `repaired_output` ONLY IF the
reformatted text actually parses into the FINDINGS schema. If the reformat STILL
doesn't parse, it is a **no-op** — `repaired_output` is left unset and the
command exits with a non-zero sentinel (3). Grouping uses `repaired_output` when
present, while `--full` and the RAW section ALWAYS render the original `output`,
so a degraded haiku rewrite can NEVER overwrite the seat's genuine review:
`collect` then renders the ORIGINAL text raw (graceful degradation that is never
worse than the seat's real words), and `panel-full.md` keeps the original verbatim. You do not need to inspect the
exit code — just continue; the file is correct either way. The formatter is
in-session on the subscription (a `haiku` Task seat) — no `claude -p`, no API key.

**Collect ONCE — grouped + faithful.** Now collapse the WHOLE panel into ONE
grouped digest, with the byte-faithful sibling preserved on disk:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" collect --session-id <session-id> --seats <ran_seats> --group -o .crew/reviews/<session-id>/panel.md --full .crew/reviews/<session-id>/panel-full.md
```

`collect` reads EXACTLY the named seats (never globs — a stale/foreign file is
never folded in). `--group` writes the deduped digest to `panel.md`; `--full`
writes the faithful per-seat concatenation to `panel-full.md` (the recovery
artifact — you do NOT read it unless you need a seat's full block). **Read
`panel.md` once** and carry that ONE digest into Step 6 — do NOT also carry the
raw Task-seat blocks into context (that is the whole point).

## Step 6 — Synthesize the verdict

Read the ONE grouped `panel.md` digest (Step 5.5) — it already folds the WHOLE
panel (subprocess AND Task seats) — and emit the existing verdict format:

- **Plan target** → `APPROVED` / `REVISE` (with `[BLOCKING]`/`[MINOR]` items).
- **Code target** → findings tagged `[BLOCKING]`/`[MINOR]` + a summary verdict.

`panel.md` has four sections, read them all:

- **VERDICTS** — the roster of every seat that RAN, each with its verdict (or
  `(unparsed)`/`(skipped)`) + a tally. Reference seats by LABEL ("the codex
  verdict", "the cursor-auto block"), never positionally.
- **CRITERIA MATRIX** — per-criterion PASS/FAIL/`?` per seat.
- **GROUPED FINDINGS** — each finding ONCE, with an `M/N` agreement count
  (N = findings-parsed seats, distinct from the VERDICTS roster count). A finding
  marked **`⚠ SINGLETON`** is a lone dissent — weigh it ON ITS MERITS, never
  dismiss it because only one seat raised it (a real blocker often starts as one
  seat's finding).
- **RAW / UNPARSED SEATS** — any seat whose findings could not be parsed even
  after repair, rendered verbatim. **Still read these** — a partial seat's prose
  findings live here and must not be ignored.

**Never choke — synthesize from whatever succeeded.** A failed/skipped seat
(subprocess OR Task) NEVER aborts the review and is NEVER silently dropped (it
shows in the VERDICTS roster and/or the RAW section):

- Synthesize the verdict from the seats that produced usable output.
- ONLY when **zero** seats in the panel produced usable output do you skip the
  verdict and instead report:
  `could not review — all seats failed: <per-seat diagnostics>`.

---

Subscription safety: the engine drives only the subprocess seats — `codex` + the
`agy`/`cursor-*` seats, all external-CLI auth; Claude voices are
in-session Task seats. No `claude -p`, no Anthropic API — a stray
`ANTHROPIC_API_KEY` is irrelevant here.
