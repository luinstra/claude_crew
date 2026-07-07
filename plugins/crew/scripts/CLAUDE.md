# Scripts - Claude Code Guidance

> **Context:** Python state machine for persistence loops + the multi-model review engine
> **Parent Guide:** [Project Root](../../../.claude/CLAUDE.md)

## Quick Reference

This directory contains the Python backend for crew's persistence features. Hooks receive JSON on stdin, process state, and output JSON results. The `crew-state.py` CLI manages loop state files. The `multiagent/` package is the review/council engine that fans a prompt across subprocess seats.

## Structure

> **Invocation:** the engine + state CLI are reached through the plugin-root bare
> dispatcher `plugins/crew/crew` (one level up — `../crew`), invoked as
> `"${CLAUDE_PLUGIN_ROOT}/crew" <sub> …` (engine) and `… crew state <sub> …`
> (state). See the "Plugin-root `crew` dispatcher" + "`crew state …`" bullets below.

```
scripts/
├── crew-state.py        # CLI for loop state management (reached via `crew state …`)
├── models.py            # Dataclasses for all JSON structures
├── persistent-mode.py   # Stop hook: enforces continuation
├── session-start.py     # SessionStart hook: restores state
├── tests/               # Unit tests (test-hooks.py, test-multiagent.py, fixtures/)
└── multiagent/          # Multi-model review/council engine (see below)
```

## Multi-Model Review Engine (`multiagent/`)

The engine that powers `/crew:review`, `/crew:debate`, and the review steps of
`/crew:build` and `/crew:measure-twice`. It drives **subprocess seats** only —
the external CLIs `codex` + `agy` + `cursor-gpt`/`cursor-gemini`/`cursor-glm`/`cursor-auto`/`cursor-composer`
(`cursor-gpt`/`cursor-gemini`/`cursor-glm` opt-in via `--seats`). The **Claude seats** (opus/sonnet) are NOT
in here: they're spawned by the orchestrating command as `crew:reviewer` Task
subagents (in-session, on the subscription), and the orchestrator normalizes
their results into the same six-field shape the engine returns.

```
multiagent/
├── cli.py               # argparse entry: `review` | `council` | `debate` | `run` | `dispatch` (write-mode single-seat WORK delegation) | `render` (incl. `--stage-all`) | `seats` (incl. `--debate`: config-aware full debate panel) | `collect` (incl. `--group`/`--full`/`--report-unparsed`) | `repair-seat` (write the haiku repair to a seat's separate `repaired_output` field, never overwriting `output`) | `review-prep` | `persist-seat` (write ONE normalized Task-seat result to the session dir — engine-owned slug + six-field shape) | `doctor` (/crew:init provider probe) | `probe` (opt-in BILLABLE live seat smoke test — doctor proves a seat's CLI is installed, probe proves it actually returns usable output) | `scaffold-config` (/crew:init config generator) subcommands (reached via the bare `../crew` dispatcher)
├── prompts.py           # THE single prompt builder: build_prompt(target,*,seat_role,mode,prior_round,inline) — review + discuss; council()
├── targets.py           # resolve a plan .md or git diff target (working-tree/branch/range A..B/commit/auto; untracked files as new-file diffs)
├── rounds.py            # debate run lifecycle: run-id (+traversal guard), run-dir, question.md, round-NN.md read/write, prior-rounds concat. NO model calls.
├── config.py            # TWO memoized loaders (per-repo `.crew/config.toml` + global `~/.crew-config.toml`) + per-key validating getters (default_panel + debate_panel ([debate].panel) + dispatch_seat ([dispatch].seat, validated vs known_seat_names()) + per-seat tuning + [panels] roster + seat `available`; per-repo>global>builtin; pure leaf, no cli/providers import; Python 3.11+ tomllib, else gracefully ignored)
├── render.py            # side-by-side panel + --json rendering (the faithful projection + raw-fallback)
├── findings.py          # PURE parser + complete-linkage grouping + grouped-digest renderer (no I/O, no model calls, never raises); powers `collect --group`
└── providers/
    ├── __init__.py      # ProviderResult (the six-field contract), Provider ABC (executor: run() + supports_workspace_write capability, fail-CLOSED/opt-in for /crew:dispatch), registry + known_seat_names()
    ├── codex.py         # CodexProvider — `codex exec - --sandbox read-only -o <tmp>`, prompt via stdin
    ├── cursor.py        # CursorProvider — the live `cursor-gpt`/`cursor-gemini`/`cursor-glm`/`cursor-auto`/`cursor-composer` seats; CURSOR_SEATS is the one-line-to-extend source of truth
    └── agy.py           # AgyProvider (default panel) — `agy -p <prompt> --model … --sandbox` (NOT --dangerously-skip-permissions). Re-validated 2026-07-01: 3-run `crew probe agy` = pass/pass/pass (6–11s) — the CLI connects and follows instructions; historical empty/off-target REVIEWS were prompt-level, not a dead seat. Re-probe before any future demotion decision.
```

Key contracts (do NOT regress):
- **Six-field `ProviderResult`** (`name, model, ok, output, error, elapsed`) — the
  one shape every seat (subprocess AND normalized Task seat) returns.
- **Provider = executor.** The `Provider` ABC's job is `run()` (invoke a CLI →
  `ProviderResult`). codex/agy/cursor-* are executors; opus/sonnet (Task seats) are NOT
  providers — they're owned by the orchestrator. (This is the debate-validated
  shape: we deliberately did NOT adopt the Enterprise fork's prompt-builder
  `BaseProvider`/`TaskProvider`, which modelled non-executable Claude seats as
  providers.)
- **One prompt builder; engine builds for all, executes only subprocess.**
  `prompts.build_prompt`/`council` is the SINGLE source of every seat's prompt.
  The engine EXECUTES only subprocess seats, but `render` BUILDS the prompt for
  ANY seat — including the Claude Task seats the orchestrator dispatches — so the
  subprocess and Task prompts can never drift. (This *evolves* the older "the
  engine only knows subprocess seats" contract.) The parity test in
  `test-multiagent.py` asserts `render` output == `prompts.build_prompt(...)`.
- **Modes** — `mode="review"` (rubric + APPROVED/REVISE) vs `mode="discuss"`
  (advisory council take, no verdict). `build_prompt` raises on an unknown mode.
  Defaults (`seat_role=None, mode="review", prior_round=None`) keep review output
  byte-identical — multi-round/discuss are opt-in.
- **Multi-round lives on disk** (`rounds.py`): a run dir holds `question.md` +
  `round-NN.md`; `render --run-id R --round n` folds rounds `1…n-1` in as
  injection-guarded DATA (`prior_round`). The orchestrator (debate.md) owns the
  round loop + convergence; the engine just renders/executes/reads-records.
- **Never choke** — a failed/skipped seat returns `ok=False`; the fan-out only
  exits nonzero when EVERY seat failed (and still emits the full array, no traceback).
- **Reference mode is the default** — seats fetch the diff/plan themselves (the
  engine passes `targets.Target.diff_cmd` / `ref_path`); `--inline-diff` embeds it.
  The Claude Task seats (`crew:reviewer` for review, `crew:panelist` for discuss)
  have `Read, Grep, Glob, Bash` and fetch the target the same way. Their `Bash` is
  read-only by CONVENTION (instructed to git diff/show/log + reads, never mutate)
  — NOT sandbox-enforced; see `reviewer.md` for the honest posture + the native
  enforcement paths if it's ever pointed at untrusted diffs.
- **`--out <file>`** writes results to a file so the call needs no shell redirect
  (stays permission-allowlistable).

### Presets & config precedence

- **Panel selection** — the commands accept `--panel full|lite|solo|cursor|quick` / `--seats <subset>`
  and pass them STRAIGHT to `crew review-prep`, which OWNS the resolution (`seats.PANEL_PRESETS` +
  `seats.MODEL_OVERRIDES`): it splits the preset/`--seats` into `subprocess_seats` (the
  `codex`/`agy`/`cursor-*` entries), `task_seats` (the Claude voices), and `task_seat_models` (each Task
  seat's model pin), and the orchestrator just reads that JSON — it no longer classifies seat names,
  defines presets, or hardcodes a model pin (the engine still EXECUTES only the subprocess subset,
  skipped when empty).
- The default panel is `codex + agy + cursor-auto/composer + opus + sonnet`; `cursor-gpt`,
  `cursor-gemini`, and `cursor-glm` are registered but opt-in (codex already covers the GPT lineage, so
  `cursor-gpt` isn't defaulted; `agy` covers the Gemini lineage flat-rate, so the metered
  `cursor-gemini` is left opt-in; and `cursor-glm`'s glm-max draws on Cursor's shared premium MAX
  allotment, so it's opt-in too — `cursor-auto` fills that slot from the cheap/dedicated bucket).
- When the user names NEITHER `--panel` nor `--seats`, the default panel NAME comes from `default_panel`
  (`config.py`, per-repo `.crew/config.toml` → global `~/.crew-config.toml`), falling back to the
  built-in `full`; precedence is CLI flag > per-repo config > global config > builtin (no env tier — the
  old env surface is retired).
- `/crew:debate` honors config via a separate resolver: it cannot call `review-prep` (it resolves its
  panel in the command markdown), so `crew seats --debate` prints the FULL debate panel (subprocess AND
  Claude Task seats), applying the debate precedence `--seats`/`--panel` > `config.debate_panel()`
  (`[debate].panel`) > `config.default_panel()` > built-in `full`. This is the lightest hook (extending
  the existing `seats` subcommand, not a heavyweight `debate-prep` mirror of `review-prep`); note debate
  adds the `debate_panel` tier that `review-prep` has no equivalent for.

### Availability filtering

- Panel-NAME resolution AND the `available` filter funnel through ONE shared point in `cli.py`
  (`_panel_seat_list` consults `config.panels()` before `seats.PANEL_PRESETS`).
- Two availability shapes share the same `available=false` drop + explicit-name skip-note: the
  single-list chokepoint `_resolve_seats` (ad-hoc `crew review`/`council`/`seats` + the debate resolver)
  uses `_filter_available`, which falls back to the unfiltered panel if the one list would empty;
  `review-prep` uses the lower-level `_drop_unavailable` per split (subprocess vs task) and applies a
  WHOLE-PANEL fallback ONCE — restoring the unfiltered panel only if the ENTIRE resolved panel
  (subprocess + task + any opaque `--task-seats` override) is empty, so disabling one seat-kind can't
  resurrect the other and an override keeps the panel non-empty. Wired into ALL panel paths so
  `[panels]` + availability behave identically everywhere.

### Group tokens & staging

- `--panel cursor` = `--seats cursor` (ALL Cursor seats incl.
  cursor-gpt/cursor-gemini/cursor-glm/cursor-auto, no codex/Claude).
- The engine itself only knows subprocess seats, and its subprocess-seat allowlist is registry-derived
  via `known_seat_names()` (no hardcoded codex/agy list). The `cursor` GROUP TOKEN (in `--seats` and
  `[panels]` rosters) expands to every registered `cursor-*` seat via `_expand_seat_groups`, so it grows
  with `CURSOR_SEATS`.
- `render --stage --session-id <id>` stages a seat prompt to
  `.crew/reviews/<session-id>/prompt-<seat-role>.txt` (session resolved in Python — arg →
  `CLAUDE_SESSION_ID` env — so the command carries no `${…}` expansion).
- **Plugin-root `crew` dispatcher (canonical invocation).** Commands invoke the
  engine via the bare `"${CLAUDE_PLUGIN_ROOT}/crew" <sub> …` dispatcher (a thin
  launcher at `plugins/crew/crew`, run directly via its shebang + exec bit — no
  `python` prefix, no `.py` tail), NOT the longer `…/scripts/multiagent/cli.py`
  path. It has its own `sys.path` guard (inserts the sibling `scripts/` dir) and
  delegates to `multiagent.cli.main()`, inheriting every subcommand unchanged —
  it is a pure path-prefix rename (identical args, output, exit codes). The old
  `cli.py` path still works (the dispatcher just imports it), but shipped commands
  emit only `crew`. Users add ONE allowlist rule (`Bash(*/crew *)`) covering the
  engine AND `crew state …` (see below).
- **`render --stage-all <comma-roles>` (N→1 collapse).** Stages one LABELED prompt
  PER comma-listed role in a SINGLE call — `.crew/reviews/<session-id>/prompt-<role>.txt`
  for each — and prints a JSON `{role: path}` map. The review-bearing commands
  (`review`/`build`/`measure-twice`) use it to collapse their N per-seat
  `render --stage --seat-role <role>` calls into ONE — and the role list they
  pass is the **comma-joined `task_seats`** from the `review-prep` JSON (NOT a
  hardcoded `opus,sonnet`), so staging, spawn, and model pins all derive from the
  one catalog and the staged filename always matches what the spawn reads (a
  `.`-bearing role — none in the current roster — would stage AND be read as its
  dot-stripped filename).
  Each role gets its own
  "acting as the **<role>** seat" label; the special role `seat` maps to
  `seat_role=None` (no label — matching the shared `prompt-seat.txt`). It reuses
  `_stage_path` + `_resolve_session_id` + the placeholder/traversal guards (so a
  `--stage-all opus` file is byte-identical to `--stage --seat-role opus`), is
  mutually exclusive with `--stage`/`--seat-role`/`-o`, and rejects
  duplicate/normalization-colliding/empty role lists (nonzero, no partial files).
  Roles are prompt LABELS, NOT registry seats — they are NOT filtered through
  `known_seat_names()`. `debate` keeps its per-seat `-o` renders (it stages into
  its own `.crew/debates/<dir>/` for round threading, which `--stage-all`'s
  `.crew/reviews/` layout doesn't model) — it gets the dispatcher prefix only.

### Prep

- The orchestrator preps the fan-out with ONE `crew review-prep <target> --panel <preset>` (or `--seats
  <spec>`) `--session-id <id>` call, which resolves the target, splits `--panel`/`--seats` into the
  subprocess seat list AND the Task-seat split, stages the shared subprocess prompt once (`render
  --stage` machinery, byte-identical), and PRINTS `{prompt_path, subprocess_seats, task_seats,
  task_seat_models}` as JSON — it runs NOTHING.

### Subprocess fan-out

- **Per-seat fan-out (visibility).** The review-bearing commands (`review`/`build`/`measure-twice`) fan
  subprocess seats out ONE `crew run <seat>` call PER seat — each a separate, visible, killable shell —
  rather than one opaque `crew review --seats <all>` call that hides them in the `_fan_out` thread pool.
- The orchestrator then iterates `subprocess_seats`, running each via `run <seat> --session-id <id>
  --json` — `run` DERIVES `-f` (`prompt-seat.txt`) and `-o` (`<seat>.json`) from the session dir when
  they're omitted, so the fan-out line no longer types either path; explicit `-f`/`-o` still override
  independently (debate's own `.crew/debates/` layout keeps passing explicit paths).
- `run --json` always exits 0 with the six-field result, so per-seat never-choke is automatic (no
  all-failed abort to handle).

### Task-seat dispatch & persist

- (`review-prep` resolves BOTH seat kinds but EXECUTES neither — the Claude Task seats stay
  orchestrator-DISPATCHED; the commands now CONSUME `task_seats` + `task_seat_models` to stage via
  `--stage-all` and spawn each Task seat with `model = task_seat_models[seat]`, so it is no longer the
  opaque echo the commands ignored.)
- After the fan-out, the orchestrator persists EACH normalized Task seat (opus/sonnet/fable) via `crew
  persist-seat <seat> --session-id <id> --model <pin> -f <text>` (or `--failed --error <diag>` for a
  seat that errored) — the engine owns the dot-stripped slug AND the six-field shape, so the command
  markdown no longer hand-assembles the `<seat>.json` with the Write tool.

### Repair & collect

- Each Task seat lands as a `<seat>.json` too, so the WHOLE panel (subprocess AND Task seats) flows
  through ONE `collect`. It then collapses the per-seat files into ONE GROUPED markdown digest with
  `crew collect --session-id <id> --seats <comma-joined ran seats> --group -o panel.md --full
  panel-full.md` and reads the deduped `panel.md` once.
- `collect` reads EXACTLY the named seats (never globs — a stale `<seat>.json` from an earlier panel in
  the reused session dir is never folded in), labels every block by its requested seat name (a missing,
  unreadable, non-JSON, or non-object file → a labeled SKIPPED block; a well-formed JSON object renders
  per its fields via `from_dict`'s render-safe coercion), and rejects a seat name with path chars
  (nonzero). `collect` is **no longer faithful-only** — it has THREE additive modes over the same
  named-seats read (`multiagent/findings.py` is the pure parser+grouping module; `collect`/`findings`
  spawn NO agents):
  - **no flag** — the original FAITHFUL `render_panel` projection (never summarizes/votes/reorders);
    byte-identical to before (the `test_collect` contract).
  - **`--group`** — the deduped digest (`findings.render_digest`): a VERDICTS roster (every ran
    seat) + CRITERIA MATRIX + GROUPED FINDINGS (`M/N` agreement, N = findings-parsed seats only, `⚠
    SINGLETON` for lone dissents) + a RAW / UNPARSED SEATS section that renders any non-parseable
    seat verbatim (Decision-C — a finding is NEVER dropped). Merge predicate (Decision-D): severity
    is the only hard partition; within it, COMPLETE-LINKAGE clustering on `path_compatible AND
    line_compatible AND jaccard>=0.5`. Falls back to exactly `render_panel` when NO seat is
    findings-parsed. `--full <path>` ALSO writes the faithful sibling (the recovery artifact /
    advisory size denominator).
  - **`--report-unparsed`** — prints ONLY the ran-but-non-findings-parsed seat names (the
    haiku-repair candidates); writes no digest.
- **Per-seat haiku repair (orchestrator-side).** Between the seat fan-out and the grouped collect, the
  command markdown runs `collect --report-unparsed`, and for each non-compliant seat Reads its raw
  `output`, spawns a `crew:formatter` (haiku, read-only) Task to reformat it into the FINDINGS schema (a
  FAITHFUL transform — never invents/drops/re-judges), then writes the result back with `crew
  repair-seat --seat <file> -f <text>` (writes the reformatted text to a SEPARATE `repaired_output`
  field, NEVER overwriting the seat's original `output`; round-trippable via `to_dict`/`from_dict`).
- `repair-seat` is **non-destructive**: it populates `repaired_output` ONLY IF the reformat parses
  (`findings.parse_seat` → `findings_parsed=True`); if it STILL doesn't parse the write is a NO-OP that
  leaves `repaired_output` unset and exits 0 with a stderr kept-original note (a no-op is successful
  graceful degradation, not an error — exit 2 stays reserved for real usage/read errors).
  Grouping/`--group` uses `repaired_output` when present, while `--full` and the RAW section ALWAYS
  render the original `output` — so a degraded haiku rewrite can never overwrite the seat's genuine
  review, and a seat still non-compliant after repair renders its ORIGINAL text raw (graceful
  degradation, never worse than the original).
- The orchestrator clears each expected `<seat>.json` BEFORE launching the per-seat run shells and WAITS
  for BOTH every subprocess run shell AND every Task-seat persist before collecting. The `review`
  subcommand still exists for ad-hoc one-shot fan-out; the commands just don't use it. (`/crew:debate`
  fans out per-seat in BOTH paths now — its multi-round path always did, and the single-round path
  scaffolds via `debate --seats none` then runs each subprocess seat with `run <seat>`, same as the
  commands above. Debate does NOT use `collect`.)
- **`dispatch` (write-mode single-seat WORK delegation).** The EXECUTION
  complement to read-only review/debate: `crew dispatch "<task>" [--seat <name>]`
  sends ONE subprocess seat (default `codex`; resolution `--seat` >
  `config.dispatch_seat()` (`[dispatch].seat`) > builtin) at the working tree in
  `sandbox="workspace-write"` — it OPTS INTO the existing `run -s workspace-write`
  plumbing, so review/debate (which never pass `-s`) stay `read-only` and
  byte-for-byte unchanged. `prompts.dispatch()` (mirrors `council`) frames the
  task between literal `BEGIN TASK`/`END TASK` markers and instructs the seat to
  leave changes UNCOMMITTED + UNSTAGED + on the same branch. `cmd_dispatch`
  captures a HEAD/staged/branch guard BEFORE and AFTER the run, PINNED to one
  `repo_dir = CLAUDE_WORKING_DIRECTORY or getcwd()` (the SAME tree the seat edits:
  codex/agy pin their subprocess `cwd` to `repo_dir` in workspace-write mode,
  cursor already pins its own). `_git_head` is TRI-STATE (sha / `"<unborn>"` /
  `None`) so an unborn-repo FIRST commit is caught; all three guards are NULL-SAFE
  typed booleans (`head_moved`/`staged_changed`/`branch_changed`, never `null`).
  Dispatch emits its OWN 15-field JSON envelope (NOT a polluted `ProviderResult`),
  DERIVES `dispatch-<seat>.json` from `--session-id` like `run`, AND prints the
  resolved path on stdout (`collect`-style) so the command markdown reads it back
  without constructing the filename. An unavailable-CLI seat writes a skipped
  `ok=false` envelope (exit 0) like `run`; unknown/Task/non-writable seats exit 2
  with no envelope. codex additionally OMITS `--ignore-user-config` in
  workspace-write mode so the WORK seat loads project context (`AGENTS.md` /
  skills), while read-only review stays hermetic (the `-c model_reasoning_effort`
  pin is unconditional).
- **`crew state …` routes crew-state through the dispatcher.** Loop state
  management is invoked as `"${CLAUDE_PLUGIN_ROOT}/crew" state <sub> …`, NOT the
  bare `scripts/crew-state.py` path, so ONE allowlist rule covers both engine and
  state. The dispatcher loads `crew-state.py` via `importlib` and swaps `sys.argv`
  around its no-arg `main()` (letting crew-state's own `SystemExit` propagate for
  correct exit codes). WHY route it rather than ship a plugin-root `crew-state.py`
  shim: `crew-state.py` does a bare `from models import …` with NO `sys.path`
  guard — a plugin-root shim would put the plugin root (not `scripts/`) on
  `sys.path[0]` and break that import. The `crew` dispatcher's guard already adds
  `scripts/`, so the import resolves with NO edit to `crew-state.py`.
- One engine-side affordance: `crew debate` is now **scaffold-only regardless of
  `--seats`** — it ALWAYS writes the dir + `question.md` + an empty
  `subprocess.json` (exit 0) and NEVER runs subprocess seats internally (the old
  internal `_fan_out` branch was a killability split-brain vs. `review-prep`).
  `--seats none`/`""` is the no-op default; a non-empty `--seats` (e.g. `codex`)
  prints a one-line stderr advisory and still scaffolds (it does NOT execute the
  seats). The single-round `/crew:debate` path uses this scaffold-only mode, then
  fans seats out per-seat into individual `<seat>.json` files — the scaffold's
  `subprocess.json` stays empty and unused. (It also still lets a Claude-only
  `lite`/`solo` council get its log dir.) `review`/`council` are unchanged — they
  still fan out via `_fan_out` and intentionally do NOT support empty seats
  (they're pure fan-out; the orchestrator skips them).
- Two-tier config (env retired): tuning lives in TWO TOML files — per-repo
  `.crew/config.toml` and global `~/.crew-config.toml` (`config.py`, two memoized
  loaders). Knobs: `default_panel`, `[debate].panel`, `[dispatch].seat`
  (the `/crew:dispatch` default seat, validated against `known_seat_names()` —
  a panel name / group token like `cursor` is rejected), `[seats.<name>]`
  (`model`/`reasoning_effort`/`print_timeout`/`available`), `[tuning].timeout`,
  and the global-tier `[panels]` roster. Resolution is PER-KEY (per-seat-per-key
  for `[seats.<name>]`): CLI flag > per-repo > global > builtin. The old tuning
  env surface is RETIRED — consulted at no call site. `[panels]` redefines a
  built-in preset or adds a custom one (validated vs known seats; unknown dropped
  + one-time warn); `available = false` opt-outs a seat (filtered from any
  resolved panel after roster resolution, before the run; explicit-named →
  skip-note; all-unavailable → warn + unfiltered fallback).
- **`/crew:init` support — `doctor` + `scaffold-config`.** Two registry-derived,
  NON-billable subcommands powering the `/crew:init` onboarding scaffolder (`cmd_doctor`
  / `cmd_scaffold_config` in `cli.py`; the target paths resolve through the public
  `config.repo_config_path()` / `config.global_config_path()` wrappers so init writes
  EXACTLY where the loader reads). They share ONE stdout/stderr discipline: **stdout
  carries ONLY the machine payload; EVERY note/error goes to stderr** (init.md parses
  stdout).
  - **`doctor`** iterates `known_seat_names()` → `get_provider(n).is_available()` into a
    `{subprocess, task}` JSON map (the `task` block in `sorted(TASK_SEAT_NAMES)` order →
    deterministic). NON-billable: codex/agy `is_available()` are pure `shutil.which`;
    Cursor's local `agent --version` identity probe runs EXACTLY ONCE and is fanned to
    every `cursor-*` seat (not 5×). Takes `--session-id` (writes
    `.crew/reviews/<id>/doctor.json`) and `-o` (override path; `-o` wins over the session
    path); with NEITHER, no file is written. The JSON is ALWAYS printed to stdout (no
    `--json` flag — output is always JSON).
  - **`scaffold-config`** renders a COMMENTED starter config emitting ONLY loader-read
    keys (`reasoning_effort` under `[seats.codex]`, `print_timeout` under `[seats.agy]`,
    cost-safe defaults, premium seats `available=false`). It consumes `doctor`'s JSON via
    `--detection` (ABSENT flag → omit per-seat `available` lines + a stderr "detection
    skipped" note; GIVEN-but-missing/empty/malformed → error+nonzero). ONE output
    contract: resolved-target default / `--out <path>` / `--out -` (pure-TOML stdout
    preview, no envelope). The target write OWNS no-clobber (absent → write +
    `diverted:false`; present → `<target>.new` + `diverted:true`; `--force` → overwrite)
    and is NOT routed through `_emit`. `--repo` seeds from the global VERBATIM (comments
    preserved) then applies CONSERVATIVE section-scoped line-edits (default_panel /
    `[dispatch].seat` / per-seat `available`): only safely-recognized simple shapes are
    edited; any inline-table / dotted-key / multiline / array-of-tables target is left
    verbatim + a stderr note (an unparseable global is rejected). Inputs validated before
    emit: `--default-panel` vs `PANEL_PRESETS ∪ panels()`; `--add-seat`/`--disable-seat`
    vs `known_seat_names() ∪ TASK_SEAT_NAMES` (task seats correctable); `--dispatch-seat`
    vs `known_seat_names()` only.

Run its tests with `python plugins/crew/scripts/tests/test-multiagent.py`.

## Key Patterns

### Hook I/O Pattern

All hooks follow the same pattern:

```python
#!/usr/bin/env python3
import json
import sys
from models import StopInput, HookResult

def main():
    # 1. Read JSON from stdin
    data = json.load(sys.stdin)

    # 2. Parse into typed model
    input = StopInput.from_dict(data)

    # 3. Do logic
    if should_block(input):
        result = HookResult.block("Reason to continue working")
    else:
        result = HookResult.allow()

    # 4. Output JSON
    print(result.to_json())

if __name__ == "__main__":
    main()
```

### State File Pattern

State is stored as JSON in `.crew/`:

```python
from pathlib import Path
from models import BuildState

# Get project directory (from env or cwd)
directory = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))

# Load state (returns default if file missing)
state_file = directory / ".crew" / "build-state.json"
state = BuildState.load(state_file)

# Modify and save
state.iteration += 1
state.save(state_file)  # Atomic write via atomic_write_json: creates parent
                        # dirs and writes the temp file 0600 FROM BIRTH
                        # (tempfile.mkstemp), then os.replace — no chmod window.
```

### SessionStart Output Pattern

SessionStart hooks use `additionalContext`, not `message`:

```python
from models import SessionStartResult

# Inject context into Claude's session
result = SessionStartResult.with_context("""
[Previous session context restored]
You were working on: fixing the auth bug
""")
print(result.to_json())
```

### build_plugin_guidance: CREW_VERBOSE Gating and Stack Behavior

`build_plugin_guidance` (invoked by `session-start.py` at every session start) controls what plugin and skill context gets injected. Three behaviors are key:

**CREW_VERBOSE gating** — output is bifurcated by `is_verbose()` (`models.py`), which checks the `CREW_VERBOSE` env var (`1`/`true`/`yes` → verbose):
- Default (unset): terse — a single `**Skills available**: sk:kotlin, sk:kotlin-testing` line + a one-line crew-agents hint (`Crew agents (via Task tool): advisor, executor, …`).
- `CREW_VERBOSE=1`: verbose — full bulleted skill descriptions + a full `<system-reminder>` crew-agents block.

This extends the same env var that already gates the Stop-hook persistence nudge in `persistent-mode.py`.

**`<system-reminder>` wrapping** — when a project stack is detected (Kotlin, Exposed, Gradle, Trino), the entire stack guidance block is wrapped in `<system-reminder>` tags (replaced the older `<IMPORTANT>` block and `## This project uses:` header). Terse vs. verbose only changes skill bullet verbosity; the wrapper and Kotlin LSP instruction are always present when the stack is detected.

**Kotlin LSP-load instruction** — when `"Kotlin"` is in `stack_hints`, the block prepends a REQUIRED first-action instruction before the skill list:
- Call `ToolSearch(query='select:LSP')` BEFORE reading or editing any code.
- Prefer LSP operations (`goToDefinition`, `findReferences`, `hover`, `documentSymbol`) over Read+grep for navigating Kotlin code.
- Print a `🔧 [crew] Kotlin LSP available · skills ready: …` status line in the first reply.
- Prefix the next user-facing message with `[skill: <name>]` when a skill is invoked.

LSP loading is Kotlin-only for now (`needs_lsp = "Kotlin" in stack_hints`); extending it requires adding the analogous check and `confirm_parts` entry in `build_plugin_guidance`.

## crew-state CLI

Loop state management ships in `crew-state.py` but the commands invoke it through
the plugin-root dispatcher as `crew state <sub> …` (one allowlist rule covering
both engine and state; see the `crew state …` bullet above for WHY). The bare
`crew-state.py` path still runs standalone during transition.

All subcommands accept `--session-id SESSION_ID` for session-scoped state files.
If omitted, falls back to `CLAUDE_SESSION_ID` env var, then legacy unsuffixed filenames.

```bash
# Show loop state
crew state show bl --session-id abc123
crew state show mt

# Check if active (exit code 0=active, 1=inactive)
crew state is-active bl --session-id abc123

# Initialize a loop — inline text …
crew state init bl --prompt "Fix the auth bug" --session-id abc123
crew state init mt --task "Add user profiles" --auto-plan --session-id abc123

# … OR spill the raw task to a UTF-8 file and pass -f, keeping $ARGUMENTS (which
# may contain $(…)/backticks/quotes) OFF the shell line. -f maps to the loop's
# text field and is mutually exclusive with --prompt/--task; a missing/unreadable
# (incl. non-UTF-8) file exits nonzero with NO state file created. The loop
# commands (build.md / measure-twice.md) Write the spill file then rm it after a
# successful init — the L5 cleanup patterns do NOT cover these task files.
crew state init bl -f .crew/task-bl-abc123.txt --session-id abc123

# Check if this session has conflicts (other sessions ignored)
crew state check-conflicts --session-id abc123

# Set specific fields
crew state set bl iteration 5 --session-id abc123
crew state set mt last_verdict REVISE --session-id abc123

# Deactivate — pass the SAME --session-id init used, so deactivate targets the
# exact session-scoped file (init and deactivate/cancel of one loop MUST resolve
# to the same file; cmd_deactivate additionally reuses the Stop hook's
# find_session_state_file so an adopted legacy file is also turned off).
crew state deactivate bl --reason "User cancelled" --session-id abc123
```

**Session ID resolution order:**
1. `--session-id` CLI argument (highest priority)
2. `CLAUDE_SESSION_ID` environment variable
3. Empty string -- legacy unsuffixed filenames (backward compatible)

**Session-scoped filename pattern:**
- `build-state-{session_id}.json`
- `measure-twice-state-{session_id}.json`

**Schema version + refuse-to-touch (L2 / Phase 5).** Every write stamps
`"schema": SCHEMA_VERSION` (currently 1); an ABSENT `schema` loads as 1 (legacy
files keep working). Reads classify a file via `read_state_json` /
`load_with_status` into `LOAD_OK` / `LOAD_MISSING` / `LOAD_CORRUPT` /
`LOAD_FUTURE_SCHEMA`. Every MUTATING path — the Stop hook AND the crew-state CLI
(`init` / `set` / `increment` / `deactivate`) — honors the SAME contract:
- **Newer schema** (`schema > SCHEMA_VERSION`): REFUSE to touch. The hook allows
  stop with a loud diagnostic; the CLI exits nonzero, bytes untouched — a
  downgrade must never clobber a live newer-format loop.
- **Corrupt** (unparseable / non-object JSON): the Stop hook and `init` set the
  file aside as `<name>.corrupt` (a one-time diagnostic; `init` then starts fresh
  state), while `set` / `increment` / `deactivate` refuse (nonzero) rather than
  silently overwrite. `*.corrupt` artifacts are swept by `session-start`
  cleanup.

**Loop aliases:**
- `bl` = `build`
- `mt` = `measure-twice`

## Data Models

### HookResult

```python
@dataclass
class HookResult:
    continue_: bool = True    # Allow or block
    message: Optional[str]    # Info message
    reason: Optional[str]     # Block reason
    system_message: Optional[str]  # User-visible diagnostic (systemMessage, allow-side)

# Usage (schema pinned by the 2026-07-06 C2 live smoke)
HookResult.allow()                    # {}
HookResult.allow("Info message")      # {"message": "..."}
HookResult.allow_with_diagnostic("…") # {"systemMessage": "..."} (loud allow)
HookResult.block("Must continue")    # {"decision": "block", "reason": "..."}
```

> **Do NOT emit `{"continue": false}` to block a Stop.** The 2026-07-06 C2
> live smoke proved it INERT for blocking — Claude Code honors it as a
> HARD-STOP directive (the child session terminated at the first stop
> attempt). `{"decision": "block", "reason": ...}` is the load-bearing,
> smoke-verified blocking shape.

### BuildState

```python
@dataclass
class BuildState:
    active: bool = False
    prompt: str = ""              # Original task
    iteration: int = 1
    max_iterations: int = 10
    completion_promise: str = "DONE"
    session_id: str = ""          # Session that owns this loop
```

### MeasureTwiceState

```python
@dataclass
class MeasureTwiceState:
    active: bool = False
    task_description: str = ""
    plan_file: str = ""           # e.g., ".crew/plans/auth-system.md"
    iteration: int = 1
    max_iterations: int = 10
    last_verdict: str = ""        # APPROVED, REVISE, REJECT
    session_id: str = ""          # Session that owns this loop
```

## Common Mistakes

❌ **Using `message` for SessionStart context**
```python
# WRONG - message is for PermissionRequest, not context injection
print(json.dumps({"message": "You were working on X"}))
```

✅ **Use `additionalContext` via SessionStartResult**
```python
result = SessionStartResult.with_context("You were working on X")
print(result.to_json())
```

---

❌ **Hardcoding project directory**
```python
state_file = Path("/Users/me/project/.crew/state.json")
```

✅ **Read from environment**
```python
directory = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
state_file = directory / ".crew" / "state.json"
```

---

❌ **Not handling missing state files**
```python
with open(state_file) as f:
    state = json.load(f)  # Crashes if file missing
```

✅ **Use model's load() method**
```python
state = BuildState.load(state_file)  # Returns default if missing
```

## Orphan Cleanup

The `session-start.py` hook cleans up stale state files on every session start:

- **Inactive** state files older than **1 day**: deleted
- **Active** state files older than **7 days**: force-deleted (safety limit for abandoned sessions)
- Applies to `build-state-*` and `measure-twice-state-*` files

## Testing

```bash
# Run all tests
python tests/test-hooks.py

# Run with verbose output
python tests/test-hooks.py -v
```

Tests cover:
- Model serialization/deserialization
- State file operations
- Hook result formatting
- CLI commands (legacy and session-scoped)
- Multi-session isolation (concurrent loops, conflict detection)
- Orphan cleanup (stale inactive and very old active files)
- `CLAUDE_SESSION_ID` env var fallback

## Working Here Checklist

- [ ] Import models from `models.py`, don't duplicate dataclasses
- [ ] Use `CLAUDE_PROJECT_DIR` env var for directory
- [ ] Handle missing state files gracefully (return defaults)
- [ ] Write state via `atomic_write_json` / `state.save` (0600-from-birth, atomic) — never hand-roll a write + chmod
- [ ] Run `tests/test-hooks.py` after changes
- [ ] Output valid JSON only (no print debugging to stdout)

## Related Guides

- [Project Root](../../../.claude/CLAUDE.md) - Adding hooks, overall architecture
