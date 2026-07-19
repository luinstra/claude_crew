# Scripts - Claude Code Guidance

> **Context:** Python state machine for persistence loops + the multi-model review engine
> **Parent Guide:** [Project Root](../../../.claude/CLAUDE.md)
> **Rationale / history / decision-gloss:** [docs/engine-notes.md](../docs/engine-notes.md) (NOT auto-loaded — the WHY behind these contracts)

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
├── artifact_prune.py    # ENUMERATE-only stale-artifact finder (single source shared by `crew swab` + the session-start reporter); never deletes
├── tests/               # Unit tests (test-hooks.py, test-multiagent.py, fixtures/)
└── multiagent/          # Multi-model review/council engine (see below)
```

## Multi-Model Review Engine (`multiagent/`)

The engine that powers `/crew:review`, `/crew:debate`, and the review steps of
`/crew:build` and `/crew:measure-twice`. It drives **subprocess seats** only —
the registered external-CLI seats (the codex, agy, and cursor rows of the shipped
catalog `multiagent/seats.toml`, merged with the user's config layers by
`seats.merged_catalog()`; the premium cursor model-seats are opt-in via
`--seats`). The **Claude seats** are NOT
in here: they're spawned by the orchestrating command as `crew:reviewer` Task
subagents (in-session, on the subscription), and the orchestrator normalizes
their results into the same six-field shape the engine returns.

```
multiagent/
├── cli.py               # argparse entry (reached via the bare `../crew` dispatcher); subcommands: review | council | debate | run | dispatch | build-executor | render (incl. --stage-all) | seats (incl. --debate) | collect (incl. --group/--full/--report-unparsed) | repair-seat | review-prep | persist-seat | wait (incl. --signal) | signal | doctor | probe | scaffold-config | swab
├── prompts.py           # THE single prompt builder: build_prompt(target,*,seat_role,mode,prior_round,inline) — review + discuss; council()
├── targets.py           # resolve a plan .md or git diff target (working-tree/branch/range A..B/commit/auto; untracked files as new-file diffs)
├── rounds.py            # debate run lifecycle: run-id (+traversal guard), run-dir, question.md, round-NN.md read/write, prior-rounds concat. NO model calls.
├── review_runs.py       # review-run lifecycle (pure leaf like rounds.py): run identity mint (content hash + review inputs), reviews-subdir session sanitizer (the ONE mechanism cli.py delegates to), write-once run.json + mint-conflict refusal, snapshot write/self-heal, the landed-and-valid seat predicate, preserve-valid seat writes (per-seat lock), pointer read/write. NO model calls.
├── seats.py             # The seat CATALOG loader: reads shipped `seats.toml`, merges the user's config layers per-seat-per-key, exposes merged_catalog()/merged_panels()/seat_spec()/task_seats()/group_tokens()/premium_off_seats() + PROVIDER_KINDS + the SeatSpec dataclass
├── seats.toml           # DATA — the shipped seat + panel catalog (`[seats.<name>]` rows, `[panels]` rosters); merged with per-repo/global config. The one place a built-in model seat is declared
├── config.py            # TWO memoized loaders (per-repo `.crew/config.toml` + global `~/.crew-config.toml`) + per-key validating getters (default_panel, [debate].panel, [dispatch].seat validated vs known_seat_names(), [panels] roster) + `raw_layers()` feeding seats.py's per-seat resolution (per-seat tuning + `available` live on `SeatSpec`, not here); per-repo>global>builtin; pure leaf, no cli/providers import; parses with stdlib tomllib (the 3.11 floor the `crew` dispatcher asserts)
├── render.py            # side-by-side panel + --json rendering (the faithful projection + raw-fallback)
├── findings.py          # PURE parser + complete-linkage grouping + grouped-digest renderer (no I/O, no model calls, never raises); powers `collect --group`
└── providers/
    ├── __init__.py      # ProviderResult (the six-field contract), Provider ABC (executor: run() + supports_workspace_write capability, fail-CLOSED/opt-in for /crew:dispatch), registry + known_seat_names()
    ├── codex.py         # CodexProvider — the live codex model-seats (`codex exec - --sandbox read-only -o <tmp>`, prompt via stdin); each seat's name/model/reasoning_effort arrives from its SeatSpec, so a `[seats.<name>]` codex row in config is a first-class seat
    ├── cursor.py        # CursorProvider — the live cursor model-seats; each seat is driven by its SeatSpec, so a `[seats.<name>]` cursor row in config is a first-class seat
    └── agy.py           # AgyProvider (default panel) — `agy -p <prompt> --model … --sandbox` (NOT --dangerously-skip-permissions)
```

Per-subcommand one-liners (do NOT regress the behavior each names):
- `review` / `council` — ad-hoc one-shot fan-out across subprocess seats (`_fan_out`).
- `debate` — scaffold-only (see below); the round loop lives in debate.md.
- `run <seat>` — run ONE subprocess seat; `--json` always exits 0 with the six-field core result
  for every result it WRITES (run-scoped results also carry the two optional identity stamps).
  Run-scoped MISUSE (non-member seat, prompt/model/sandbox override) exits 2 before any result is
  written, and a seat name matching a reserved control filename stem (run/current-run/seat,
  case-folded) is rejected on EVERY derived destination, flat and run-scoped alike (an explicit
  `-o` keeps ad-hoc freedom).
- `dispatch` — write-mode single-seat WORK delegation (see below).
- `build-executor` (RESOLVE-ONLY): prints `{executor, retries, source}` JSON for
  /crew:build's implement step (`--executor` flag > `[build].executor` config >
  builtin `crew:executor`). Validates the resolved value is the `crew:executor`
  sentinel OR a known write-capable subprocess seat; a Task seat, group token,
  unknown, or read-only seat exits 2 naming the reason. Runs NOTHING.
- `render` — build/stage a seat prompt; `--stage-all` collapses N stages into one call.
- `seats` — resolve/print a panel; `--debate` prints the config-aware full debate panel.
- `collect` — fold per-seat `<seat>.json` into a digest; `--group`/`--full`/`--report-unparsed`.
- `wait` — the review flows' barrier primitive: block until every named SUBPROCESS seat's
  `<seat>.json` exists (a failed seat counts as landed, it has FINISHED; certification is collect's
  job). Default seat list = the manifest's kind=subprocess names; a task-kind name is REFUSED (the
  orchestrator persists Task seats itself, so waiting on one would deadlock); flat mode requires
  --seats. Exit 0 all landed; exit 1 on timeout naming the missing seats (default 900s). Scope
  resolves through the same requested-scope fail-closed helpers `collect` uses. `--signal <tag>` is
  the OTHER target of the same primitive: it blocks on an agent's `signals/<tag>.json` marker instead
  of seat files (mutually exclusive with `--seats`/`--run-id`, which exits 2 naming the conflict:
  seats are result files in a run dir, signals are agent markers). Exit 0 on arrival printing the
  marker path + status (`--json`: the marker's fields + path); a `blocked` status RELEASES too (the
  agent spoke, and the status carries the bad news; exit 1 would conflate it with never speaking);
  exit 1 on timeout naming the tag, and saying plainly that the agent may still be working OR may
  have finished WITHOUT signalling, so check the tree rather than assume failure (`--json` prints
  `{tag, landed:false, elapsed, note}` on timeout too, matching seat-mode's emit-even-on-timeout
  payload). It VALIDATES before releasing: the marker's `tag` and `session_id` must match the wait,
  else exit 2 (without it any JSON object, `{}` included, releases and hands back a report about
  other work; and a mismatch can't be "still working", the file is already there). A successful wait
  LEAVES the marker in place, so the orchestrator can Read the report at the printed path: that known
  path is the whole point (it replaces racing out-of-order agent messages), and a wait that deleted it
  would hand back a path to nothing. Staleness is `signal --clear`'s job, not the waiter's.
- `signal <tag>` — write an agent's completion marker to `signals/<tag>.json` (`{tag, status, report,
  session_id, created_at}`; report from `-f`/stdin, empty allowed since the ARRIVAL is the signal;
  `--status done|blocked`, blocked for an agent that cannot finish, because a silently stuck agent is
  the worst case). Atomic (a concurrent `wait` never reads a torn marker) and session-scoped through
  the same sanitizing chokepoint; re-signalling a tag overwrites (a retried round re-signals, newest
  report wins). Prints the marker path. `--session-id` is REQUIRED here (exit 2 naming why, no
  `CLAUDE_SESSION_ID` fallback): the marker must land in the ORCHESTRATOR's session dir, and a
  long-lived teammate's env names the TEAMMATE's session, so a fallback would file the marker where
  nobody waits, which is the exact stranding this protocol exists to prevent. The delegating prompt
  supplies the tag AND the orchestrator's session id; `wait --signal` keeps the fallback (the
  orchestrator waits in its own session, where the env value IS correct): the asymmetry is
  deliberate. `--clear <tag>` removes a marker instead of writing one (exit 0 whether or not one
  existed; refuses `-f`, since a clear has no payload).
  **Reusing a tag:** markers PERSIST until cleared (`wait` reads and leaves them, so the report stays
  readable at the path it printed), and `wait` releases on ANY marker for the tag, so a surviving
  marker from a prior round releases the next round's wait IMMEDIATELY with the stale report.
  `signal <tag> --clear` before dispatch is the ONE mechanism against that, and the orchestrator MUST
  run it before dispatching a round that reuses a tag (at that point no writer exists to race the
  clear). Residual, stated honestly: an orchestrator that reuses a tag WITHOUT clearing, over a marker
  that survived, CAN read a stale report. The clear-before-dispatch rule is what prevents that, and it
  is convention like the rest of this protocol. Markers therefore accumulate under
  `.crew/reviews/<sid>/signals/` (gitignored and harmless); `session-start` reaps markers past
  7 days, anchored to the `<tag>.json` grammar this command mints.
  **Honest caveat:** this protocol is CONVENTION, not
  enforcement. A one-shot `Task` seat needs none of it (its RETURN is the completion signal and the
  harness cannot forget to deliver it); the signal exists for a LONG-LIVED teammate, which has no
  completion signal at all (it never completes, it only goes idle), and the agent must actually call
  it. A forgotten signal is indistinguishable from an agent still working: `wait`'s timeout is what
  makes that failure bounded and loud rather than a poll that never ends. The standing rule lives in
  `agents/executor.md` (signal LAST, always, blocked included, iff the prompt named a tag). The
  review-bearing flows (`review`/`build`/`measure-twice`) do NOT use this: they delegate via one-shot
  Tasks whose returns already signal, so a signal step there would be pure ceremony.
- `repair-seat` — write the haiku repair to a seat's SEPARATE `repaired_output` field, never overwriting `output`.
- `review-prep` — resolve target + split panel, mint the run-scoped review dir
  (`review_runs.py`: identity from content hash + review inputs, write-once
  `run.json`, frozen `target.md`/`target.diff` snapshot), stage EVERY seat prompt
  against the snapshot, and print the JSON contract with the pending seat lists;
  runs NOTHING.
- `persist-seat` — write ONE normalized Task-seat result (engine-owned slug +
  six-field core shape; an empty/whitespace-only output persists as ok=false with
  error "empty seat output", never a fabricated success). With `--run-id` it lands
  stamped in that run's dir under the preserve-valid rule; with NO `--run-id` while a session pointer exists it
  REFUSES (exit 2) rather than attribute a completed seat by mutable pointer;
  with neither, the flat session dir, which skips only the run-identity half
  (the reserved-stem guard and the empty-output refusal apply everywhere).
- `doctor` — `/crew:init` provider probe (NON-billable: installed-CLI detection).
- `probe` — opt-in BILLABLE live seat smoke test (doctor proves the CLI is installed; probe proves it returns usable output).
- `scaffold-config` — `/crew:init` commented-config generator.
- `swab` — attended cleanup of stale crew artifacts (orphaned review-run dirs +
  stale debate dirs). DRY-RUN BY DEFAULT (lists only, like `git clean -n`);
  `--yes` is the DESTRUCTIVE step (rmtree), scoped to enumerated candidates from
  `artifact_prune.collect_prunable` and exiting nonzero if any delete failed. A
  review run is protected while an active loop OR a current-run pointer names it
  (no age threshold); debates prune by the existing staleness rule.

**RESOLVED: all `.crew` paths anchor to CLAUDE_PROJECT_DIR via one resolver.**
Every `.crew` root in the codebase now derives from the single `crew_base()`
resolver (`state_discovery.py`: `CLAUDE_PROJECT_DIR or cwd`). The engine
(review-prep/collect/run/swab, via `_reviews_base()` and the anchored
`review_runs`/`rounds` defaults), the state layer (`crew state` verbs + `models`,
via `get_project_dir()`), `config.py`, and the session-start sweeps all resolve
`.crew` through that one function, so a cwd that differs from CLAUDE_PROJECT_DIR
no longer spawns a phantom nested `.crew`: the two layers land on the same tree.
The earlier cwd-relative split (the engine resolved `.crew` cwd-relative while the
state layer anchored to CLAUDE_PROJECT_DIR) is closed.

Key contracts (do NOT regress):
- **Six-field `ProviderResult` core** (`name, model, ok, output, error, elapsed`) — the
  one shape every seat (subprocess AND normalized Task seat) returns; the six core
  fields are unchanged and universally present. Additive-OPTIONAL fields follow the
  `repaired_output` precedent (serialized by `to_dict` only when set, coerced by
  `from_dict`, round-tripping through `repair-seat` untouched, ignored by `render`):
  `repaired_output` (the haiku repair) plus the run-identity stamps `run_id` /
  `target_sha256`, which `run` and `persist-seat` stamp from the run dir's own
  `run.json` whenever a result lands in a run-scoped review dir (never from the
  pointer, so a stamp always describes the dir the file physically sits in). The
  ad-hoc flat layout never sets the stamps, so its JSON stays byte-identical
  six-field.
- **Provider = executor.** The `Provider` ABC's job is `run()` (invoke a CLI →
  `ProviderResult`). the subprocess providers are executors; opus/sonnet (Task seats) are NOT
  providers — they're owned by the orchestrator. (WHY not model Claude seats as
  providers → engine-notes.)
- **One prompt builder; engine builds for all, executes only subprocess.**
  `prompts.build_prompt`/`council` is the SINGLE source of every seat's prompt.
  The engine EXECUTES only subprocess seats, but `render` BUILDS the prompt for
  ANY seat — including the Claude Task seats the orchestrator dispatches — so the
  subprocess and Task prompts can never drift. The parity test in
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
  — NOT sandbox-enforced; see `reviewer.md` for the honest posture, and
  [docs/engine-notes.md](../docs/engine-notes.md) for the native enforcement
  paths if it's ever pointed at untrusted diffs.
- **`--out <file>`** writes results to a file so the call needs no shell redirect
  (stays permission-allowlistable).

### Presets & config precedence

- **Panel selection** — the commands accept `--panel full|lite|solo|cursor|quick` / `--seats <subset>`
  and pass them STRAIGHT to `crew review-prep`, which OWNS the resolution (`seats.merged_panels()`
  for the roster + `seats.merged_catalog()` for each seat's provider/model): it splits the preset/`--seats` into `subprocess_seats` (the
  external-CLI entries), `task_seats` (the Claude voices), and `task_seat_models` (each Task
  seat's model pin), and the orchestrator just reads that JSON — it no longer classifies seat names,
  defines presets, or hardcodes a model pin (the engine still EXECUTES only the subprocess subset,
  skipped when empty).
<!-- seat-roster:default -->
- Built-in default panel: `codex`, `codex-luna`, `agy`, `cursor-auto`, `cursor-composer`, `opus`, `sonnet`
<!-- seat-roster:opt-in -->
- Registered but opt-in: `codex-terra`, `cursor-gpt`, `cursor-gemini`, `cursor-glm`, `cursor-grok`
<!-- seat-roster:task-opt-in -->
- Opt-in Task seats: `fable`
- These lines document the BUILT-IN roster; a configured `default_panel`/`[panels]`
  override changes what actually runs. `"${CLAUDE_PLUGIN_ROOT}/crew" seats` prints
  the resolved AVAILABLE external-CLI seats (the Claude Task voices complete the
  panel and are not listed there). WHY each opt-in seat is opt-in → engine-notes.
- When the user names NEITHER `--panel` nor `--seats`, the default panel NAME comes from `default_panel`
  (`config.py`, per-repo `.crew/config.toml` → global `~/.crew-config.toml`), falling back to the
  built-in `full`; precedence is CLI flag > per-repo config > global config > builtin (no env tier — the
  old env surface is retired).
- `/crew:debate` honors config via a separate resolver: it cannot call `review-prep` (it resolves its
  panel in the command markdown), so `crew seats --debate` prints the FULL debate panel (subprocess AND
  Claude Task seats), applying the debate precedence `--seats`/`--panel` > `config.debate_panel()`
  (`[debate].panel`) > `config.default_panel()` > built-in `full`. Debate adds a `debate_panel` tier
  that `review-prep` has no equivalent for (WHY `seats --debate` rather than a `debate-prep` mirror →
  engine-notes).

### Availability filtering

- Panel-NAME resolution AND the `available` filter funnel through ONE shared point in `cli.py`
  (`_panel_seat_list` resolves via `seats.merged_panels()`, which merges a configured `[panels]` entry over the shipped one).
- Two availability shapes share the same `available=false` drop + explicit-name skip-note: the
  single-list chokepoint `_resolve_seats` (ad-hoc `crew review`/`council`/`seats` + the debate resolver)
  uses `_filter_available`, which falls back to the unfiltered panel if the one list would empty;
  `review-prep` uses the lower-level `_drop_unavailable` per split (subprocess vs task) and applies a
  WHOLE-PANEL fallback ONCE — restoring the unfiltered panel only if the ENTIRE resolved panel
  (subprocess + task + any opaque `--task-seats` override) is empty, so disabling one seat-kind can't
  resurrect the other and an override keeps the panel non-empty. Wired into ALL panel paths so
  `[panels]` + availability behave identically everywhere.

### Group tokens & staging

- `--panel cursor` = `--seats cursor` (every registered cursor-* seat, grows with
  the cursor rows of the catalog; no codex, no Claude).
- The engine itself only knows subprocess seats, and its subprocess-seat allowlist is registry-derived
  via `known_seat_names()` (no hardcoded codex/agy list). The `cursor` GROUP TOKEN (in `--seats` and
  `[panels]` rosters) expands to every registered `cursor-*` seat via `_expand_seat_groups`, so it grows
  with the cursor rows of the catalog (`seats.group_tokens()` supplies the expansion members).
- `render --stage --session-id <id>` stages a seat prompt to the FLAT
  `.crew/reviews/<session-id>/prompt-<seat-role>.txt`, pointer or no pointer: only `review-prep` may
  write a run dir's frozen prompt files (session resolved in Python — arg →
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
- **`render --stage-all <comma-roles>` (N→1 collapse; ad-hoc only).** Stages one
  LABELED prompt PER comma-listed role in a SINGLE call — one
  `prompt-<role>.txt` each in the FLAT session dir (never a run dir; see the
  `--stage` bullet) — and prints a JSON `{role: path}` map. The
  review-bearing commands (`review`/`build`/`measure-twice`) NO LONGER call it:
  `review-prep` stages every Task-seat prompt itself against the run's frozen
  snapshot (`task_prompt_paths` in its JSON), because a separate render would
  re-resolve the LIVE target and reopen the drift window the snapshot closes.
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
  subprocess seat list AND the Task-seat split (rejecting, exit 2 with nothing written: `--mode
  discuss`, a duplicate name across the two kinds, a non-filename-safe task-seat name, and any seat
  named after a reserved run-dir filename stem (`run`/`current-run`/`seat`)), mints the run-scoped review dir
  `.crew/reviews/<id>/<run_id>/` (identity = content hash + replayable target spec + base + a per-seat
  EXECUTION SIGNATURE for every seat: kind + resolved provider + model for subprocess seats, kind +
  model for Task seats (no subprocess provider). A panel change, a kind flip, a
  `[seats.<name>].provider` flip, or a `[seats.<name>].model` change on EITHER seat kind mints a new
  run; write-once `run.json` carrying
  the full `identity_digest`; frozen snapshot `target.md`/`target.diff`; the
  `current-run.json` pointer as a launch-time handoff), stages the shared subprocess prompt AND one
  prompt per Task seat against the SNAPSHOT, and PRINTS `{prompt_path, subprocess_seats, task_seats,
  task_seat_models, run_dir, run_id, target_sha256, session_segment, task_prompt_paths,
  pending_subprocess_seats, pending_task_seats}` as JSON — it runs NOTHING. A re-prep of an identical spec RESUMES the same dir
  (`run.json` byte-untouched, landed-valid seats excluded from the pending lists); changed content or
  review inputs mint a DIFFERENT dir, so stale results are unreachable rather than merely inadvisable.
  Before printing, prep CLEARS every pending seat's stale non-valid `<seat>.json` (a prior launch's
  failure): this is the clear that keeps `wait`'s existence barrier honest, because at prep time no
  launch exists to race it.

### Subprocess fan-out

- **Per-seat fan-out (visibility).** The review-bearing commands (`review`/`build`/`measure-twice`) fan
  subprocess seats out ONE `crew run <seat>` call PER seat — each a separate, visible, killable shell —
  rather than one opaque `crew review --seats <all>` call that hides them in the `_fan_out` thread pool.
- The orchestrator then iterates `pending_subprocess_seats`, running each via `run <seat> --session-id
  <id> --run-id <run_id> --json` — `run` DERIVES `-f` (`prompt-seat.txt`) and `-o` (`<seat>.json`) from
  the run dir (explicit `--run-id` > the session pointer > the flat session dir; resolved ONCE at
  process start so a pointer flip cannot re-route a running seat), stamps the result with the run
  identity read from the run dir's own `run.json`, and applies the preserve-valid rule (an incoming
  failure never replaces a landed valid success; a fresh `ok=true` replaces anything). A run-scoped
  launch also CLEARS its one derived `<seat>.json` first when the existing file is not a landed valid
  success, but only as the BACKSTOP for a direct relaunch with no re-prep: the clear that carries
  `wait`'s barrier guarantee is prep's (race-free, since no launch exists at prep time, while a
  launch-time clear sits behind process start and every gate, a window a concurrent `wait` polls
  into); a landed valid file is never cleared. `--run-id` with
  an explicit `-o` is rejected (contradictory routing); explicit `-f`/`-o` alone still override
  independently (debate's own `.crew/debates/` layout keeps passing explicit paths, unstamped).
- `run --json` always exits 0 with the six-field core result for every result it writes (run-scoped
  results also carry the identity stamps), so per-seat never-choke is automatic (no
  all-failed abort to handle). Run-scoped misuse exits 2 with nothing written; the markdown-templated
  calls never hit those paths.

### Task-seat dispatch & persist

- (`review-prep` resolves BOTH seat kinds but EXECUTES neither — the Claude Task seats stay
  orchestrator-DISPATCHED; the commands spawn each PENDING Task seat over its prep-staged prompt
  (`task_prompt_paths[seat]`) with `model = task_seat_models[seat]`, so it is no longer the
  opaque echo the commands ignored.)
- After the fan-out, the orchestrator persists EACH normalized Task seat (opus/sonnet/fable) via `crew
  persist-seat <seat> --session-id <id> --run-id <run_id> --model <pin> -f <text>` (or `--failed
  --error <diag>` for a seat that errored) — the engine owns the `<seat>.json` filename (the seat name
  itself; catalog names are their own slug), the six-field core shape, the run-identity stamps, and the
  preserve-valid write, so the command markdown no longer hand-assembles the `<seat>.json` with the
  Write tool. Without `--run-id` while the session pointer exists, `persist-seat` exits 2 naming the
  fix (completion-time attribution by mutable pointer is the TOCTOU bug this closes).

### Repair & collect

- Each Task seat lands as a `<seat>.json` too, so the WHOLE panel (subprocess AND Task seats) flows
  through ONE `collect`. It then collapses the per-seat files into ONE GROUPED markdown digest with
  `crew collect --session-id <id> --run-id <run_id> --seats <comma-joined ran seats> --group -o
  <run_dir>/panel.md --full <run_dir>/panel-full.md` and reads the deduped `panel.md` once.
- `collect` reads EXACTLY the named seats (never globs — a stale `<seat>.json` from an earlier panel in
  the reused session dir is never folded in), labels every block by its requested seat name (a missing,
  unreadable, non-JSON, or non-object file → a labeled SKIPPED block; a well-formed JSON object renders
  per its fields via `from_dict`'s render-safe coercion), and rejects a seat name with path chars
  (nonzero). A RUN-SCOPED read (explicit `--run-id` or the session pointer) additionally validates
  every well-formed object against the run dir's own `run.json` (both identity stamps, the stored
  `name` vs the filename's seat, and panel membership), rendering anything else as a labeled SKIPPED
  block (`skipped: stale result (run X, current run Y)` style), so a stale, copied, or hand-placed file
  never folds into the digest; a stamped, named FAILURE still renders as its failed block. Flat reads
  validate nothing (legacy unstamped files render as before). `collect` is **no longer faithful-only** — it has THREE additive modes over the same
  named-seats read (`multiagent/findings.py` is the pure parser+grouping module; `collect`/`findings`
  spawn NO agents):
  - **no flag** — the original FAITHFUL `render_panel` projection (never summarizes/votes/reorders);
    byte-identical to before (the `test_collect` contract).
  - **`--group`** — the deduped digest (`findings.render_digest`): a VERDICTS roster (every ran
    seat) + CRITERIA MATRIX + GROUPED FINDINGS (`M/N` agreement, N = findings-parsed seats only, `⚠
    SINGLETON` for lone dissents) + a RAW / UNPARSED SEATS section that renders any non-parseable
    seat verbatim (a finding is NEVER dropped). A RUN-SCOPED grouped digest opens with one quorum
    header, `PANEL: <N> launched · <usable> usable · quorum <N//2+1>: MET|NOT MET` (N = distinct
    manifest names from `run.json`; usable = the success-only `result_valid` tier; NOT MET adds
    "an APPROVED verdict is not backed by quorum from this panel", a capability-NEUTRAL statement
    of fact, since this digest is SHARED with standalone `/crew:review`, which has no override verb;
    the `--force` guidance lives only in build.md / measure-twice.md). The command markdowns HONOR this
    header at synthesis time: a NOT MET digest must not certify an APPROVED (nor a
    REVISE-minor-only completion) without the user's explicit `--force`. The header is ADVISORY (a
    digest reader's summary); `crew state
    record-verdict` recounts usable seats itself against the identity frozen in loop state, and that
    count is what decides (a below-quorum completion is exit 3, clearable with `--force`, not a hard
    refusal). Flat-mode
    `--group` renders NO header plus a one-line stderr note (quorum facts need a run manifest).
    Merge predicate: severity is the only hard partition;
    within it, COMPLETE-LINKAGE clustering on `path_compatible AND line_compatible AND jaccard>=0.5`.
    Falls back to exactly `render_panel` when NO seat is findings-parsed (behind the quorum header
    when one applies). `--full <path>` ALSO writes
    the faithful sibling (the recovery artifact / advisory size denominator; never carries the
    quorum header).
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
- The wait-for-both barrier's subprocess half is `crew wait --session-id <id> --run-id <rid>` (the
  orchestrator still awaits its own Task returns and persists them before collecting).
- Seat results are RUN-SCOPED, so the orchestrator does no pre-launch `rm -f` clear anymore: a changed
  target mints a new run dir (an old panel's files are unreachable), and an unchanged target resumes
  with landed seats excluded from the pending lists (`review-prep` clears each pending seat's stale
  non-valid file before printing its JSON, and `run` repeats the clear at launch as the backstop for
  a re-prep-free relaunch; one derived file per seat, no globs). `collect` gains `--run-id` (fallback: the session pointer; with
  neither, the flat dir, byte-identical no-flag behavior). The orchestrator WAITS
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
  Dispatch emits its OWN 16-field JSON envelope (NOT a polluted `ProviderResult`),
  which adds a `guard_warnings` field (the formatted recovery strings for whichever
  guards fired, empty when none did, from the same helper the human path prints) so
  the seam relays precise recovery guidance instead of re-deriving prose from the
  booleans. It DERIVES `dispatch-<seat>.json` from `--session-id` like `run`, AND prints the
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
  correct exit codes). (WHY route it rather than ship a plugin-root shim →
  engine-notes.)
- `crew debate` is **scaffold-only regardless of `--seats`** — it ALWAYS writes
  the dir + `question.md` + an empty `subprocess.json` (exit 0) and NEVER runs
  subprocess seats internally. `--seats none`/`""` is the no-op default; a
  non-empty `--seats` (e.g. `codex`) prints a one-line stderr advisory and still
  scaffolds (it does NOT execute the seats). The single-round `/crew:debate` path
  uses this scaffold-only mode, then fans seats out per-seat into individual
  `<seat>.json` files — the scaffold's `subprocess.json` stays empty and unused.
  (It also still lets a Claude-only `lite`/`solo` council get its log dir.)
  `review`/`council` are unchanged — they still fan out via `_fan_out` and
  intentionally do NOT support empty seats (they're pure fan-out; the orchestrator
  skips them). (WHY scaffold-only → engine-notes.)
- Two-tier config (env retired): tuning lives in TWO TOML files — per-repo
  `.crew/config.toml` and global `~/.crew-config.toml` (`config.py`, two memoized
  loaders). Knobs: `default_panel`, `[debate].panel`, `[dispatch].seat`
  (the `/crew:dispatch` default seat, validated against `known_seat_names()` —
  a panel name / group token like `cursor` is rejected), `[seats.<name>]`
  (tunes an existing seat's `model`/`reasoning_effort`/`print_timeout`/`available`,
  OR declares a brand-new first-class seat by giving `provider` + `model`, plus
  optional `opt_in` to keep it out of the built-in panels), `[tuning].timeout`,
  `[tuning].deadline_minutes` (the persistence loops' wall clock, read by `crew
  state init` when `--deadline-minutes` is not passed; validated 1..1440, plus
  exactly 0 as the deliberate no-deadline opt-out for remote or
  intermittently-attended sessions, honored from the GLOBAL file ONLY: the
  per-repo file sits in the tree the bounded agent writes to, so a repo 0 is
  dropped with a warn and cannot mask a global bound. The stop-fires cap still
  bounds an unclocked loop; a non-int, a negative value, or one past the
  ceiling is dropped with a one-time warn, never clamped silently),
  `[build].executor` (the `/crew:build` implement-step executor: a RAW string,
  type-checked only; known-ness/write-capability is the `build-executor`
  command's to validate, since the `crew:executor` sentinel is a legal value that
  no registry knows), `[build].executor_retries` (int `0..2`; the seam's bounded
  retry count, resolved per-repo THEN global before the built-in default `0`, like
  `deadline_minutes`/`timeout`. An out-of-range/non-int value in ONE layer is
  dropped with a warn and that layer is SKIPPED, so an invalid per-repo value can
  still pick up a valid global value; only when every layer is unset/invalid does
  the command fall to `0`. It is never clamped to `2`), and the
  global-tier `[panels]` roster.
  Resolution is PER-KEY (per-seat-per-key
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
    `{subprocess, task}` JSON map (the `task` block in `sorted(seats.task_seats())` order →
    deterministic). NON-billable: the PATH-check seats' `is_available()` are pure `shutil.which`;
    Cursor's local `agent --version` identity probe runs EXACTLY ONCE and is fanned to
    every `cursor-*` seat (not 6×). Takes `--session-id` (writes
    `.crew/reviews/<id>/doctor.json`) and `-o` (override path; `-o` wins over the session
    path); with NEITHER, no file is written. The JSON is ALWAYS printed to stdout (no
    `--json` flag — output is always JSON).
  - **`scaffold-config`** renders a COMMENTED starter config emitting ONLY loader-read
    keys (`reasoning_effort` under a per-codex-seat table, e.g. `[seats.codex]`, `print_timeout` under `[seats.agy]`,
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
    emit: `--default-panel` vs `merged_panels() ∪ panels()`; `--add-seat`/`--disable-seat`
    vs `known_seat_names() ∪ task_seats()` (task seats correctable); `--dispatch-seat`
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
from models import LoopState
from state_discovery import crew_base

# Project root via the ONE resolver (CLAUDE_PROJECT_DIR, else cwd); every `.crew`
# path in the codebase derives from crew_base(), so the state layer and the review
# engine can never resolve `.crew` to different trees.
directory = crew_base()

# Load state (returns default if file missing; one dataclass serves both loops)
state_file = directory / ".crew" / "build-state.json"
state = LoopState.load(state_file)

# Modify. Only fields the WRITER owns: the Stop hook owns the bounds (stop_fires /
# parked_fires / started_at), the orchestrator owns the task fields. Mutations go
# through update_state_json: it holds the interprocess lock across the read AND the
# write (an atomic write alone does not make the read-modify-write atomic), and the
# mutate_fn edits only its own keys, so the other writer's fields and any unknown
# on-disk key survive. atomic_write_json underneath creates parent dirs and writes
# the temp file 0600 FROM BIRTH (tempfile.mkstemp), then os.replace: no chmod window.
update_state_json(
    state_file,
    lambda data: {**data, "plan_file": ".crew/plans/next.md", "schema": SCHEMA_VERSION},
)
# state.save(path) still exists (a WHOLE-state write) and is right only where a
# fresh state REPLACES the file, never for editing a live loop.
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
# successful init (the session-start orphan-cleanup patterns do NOT cover these task files).
# Anchor -f to the project root and double-quote it: `state init` resolves an
# explicit -f against the shell cwd (which need not BE the project root), and the
# path can contain spaces.
crew state init bl -f "${CLAUDE_PROJECT_DIR:-$PWD}/.crew/task-bl-abc123.txt" --session-id abc123

# The loop's wall clock: --deadline-minutes (1..1440; the flag REJECTS 0, the
# agent invokes init) > [tuning].deadline_minutes (per-repo, then global; 0 here
# is the operator's no-deadline opt-out) > the built-in 240. With the clock off
# the stop-fires cap still bounds the loop. `init` REFUSES (exit 1) when the
# state file it would replace records a force-exit (the Stop hook's bound tripped),
# because a fresh loop zeroes stop_fires and restarts the clock: that IS the way
# around a safety limit, so it takes an explicit --force.
crew state init bl --prompt "Fix the auth bug" --deadline-minutes 90 --session-id abc123

# Check if this session has conflicts (other sessions ignored)
crew state check-conflicts --session-id abc123

# Set a field. ONLY the loop's AGENT_SETTABLE allowlist (plan_file, for both
# loops). Any other field exits 2 and writes nothing: the hook owns the bounds,
# and the verdict/run-identity fields belong to the review verbs.
# FIELD-LEVEL: it edits the ONE key in the on-disk dict and writes the rest back
# verbatim (unknown keys included). A whole-state save would let an allowlisted
# write also restore this process's stale `stop_fires`/`parked_fires` snapshot,
# reverting a bump the Stop hook landed in between: the counter-reset hole the
# allowlist exists to close, reopened through a field the agent IS allowed to set.
crew state set mt plan_file "${CLAUDE_PROJECT_DIR:-$PWD}/.crew/plans/auth-system.md" --session-id abc123

# Begin a review: run it right after `crew review-prep`, BEFORE any seat runs.
# It reads the prepped run's OWN run.json (never the pointer: seat results are
# stamped from that record, so the gate must judge against the same authority)
# and freezes the run id, target sha/spec/base, and the seat roster (from
# `seat_signatures`, the roster INSIDE the hashed identity) into loop state, then
# stamps phase=reviewing and prints `phase=reviewing run=<id> seats=<N>`.
# Legal from `drafting` AND from `reviewing` (a re-prep after a compaction
# re-arms the freeze rather than hitting an error the orchestrator would route
# around); strictness lives at record-verdict, where a stale identity could
# actually certify the wrong bytes. Refuses (exit 2) with no pointer, an
# unverifiable/spec-less run record, or an empty roster.
crew state begin-review bl --session-id abc123

# Record the panel's verdict against the frozen identity. Legal ONLY from
# `reviewing`, and the results are resolved EXCLUSIVELY from state
# (`.crew/reviews/<sid>/<state.run_id>`): a pointer or flat-dir fallback would
# let a re-prep decide which results a verdict is made of.
# Every verdict but FAILED needs >= 1 usable seat (an all-failed panel recorded
# as REVISE would look like progress and reset the failure counter).
# COMPLETING verdicts (APPROVED, or REVISE --minor-only) additionally face
# quorum (len(expected)//2+1 usable), pointer divergence, and a target re-hash
# (drift). These plus the zero-usable check are ADVISORY, not hard refusals: a
# trip COLLECTS every tripped condition into one combined diagnostic and exits 3
# (distinct from the exit-2 hard errors), recording nothing. `--force` records
# anyway and stamps the tripped names in `last_verdict_overrides` for the audit
# trail; it exists for a HUMAN's explicit call, never an agent's convenience. The
# HARD checks (loop inactive, wrong phase, no/invalid run identity, FAILED over a
# usable panel, no frozen target spec) stay exit 2 and are never forced. A
# degraded panel may always demand more work; a completing verdict leaves
# phase=done, which is what deactivate requires.
# REVISE/REJECT advance revision_round (the one place it moves) and return the
# loop to `drafting`. FAILED is legal ONLY with zero usable seats; the second
# consecutive one deactivates the loop terminally IN the same transaction
# (exit_kind=review_failed), never via a follow-up call.
crew state record-verdict bl APPROVED --session-id abc123
crew state record-verdict bl REVISE --minor-only --session-id abc123
crew state record-verdict mt FAILED --session-id abc123

# Deactivate — pass the SAME --session-id init used, so deactivate targets the
# exact session-scoped file (init and deactivate/cancel of one loop MUST resolve
# to the same file; cmd_deactivate additionally reuses the Stop hook's
# find_session_state_file so an adopted legacy file is also turned off).
# GATED: a live loop is turned off either after a completing verdict (phase=done,
# stamped exit_kind=approved) or with an explicit --cancel (stamped cancelled);
# anything else exits 2 naming both, with the bytes untouched. --cancel is legal
# from ANY phase (an escape hatch a phase check could veto is not one), and
# deactivating an already-off loop is a no-op success.
crew state deactivate bl --session-id abc123            # after record-verdict APPROVED
crew state deactivate bl --cancel --reason "User cancelled" --session-id abc123

# On a loop that is already off (a FORCE-EXITED file, a terminally FAILED panel)
# the `--reason` lands in `deactivate_reason`, not `reason`: `reason` there is the
# exit that ended the loop, and `init` quotes a tripped bound back when it refuses
# to restart. The exit_kind of such a loop is likewise left as its first terminal
# writer stamped it.
```

**Session ID resolution order:**
1. `--session-id` CLI argument (highest priority)
2. `CLAUDE_SESSION_ID` environment variable
3. Empty string -- legacy unsuffixed filenames (backward compatible)

**Session-scoped filename pattern:**
- `build-state-{session_id}.json`
- `measure-twice-state-{session_id}.json`

**Schema version + refuse-to-touch.** Every write stamps
`"schema": SCHEMA_VERSION` (currently 3; 2 added the hook-owned termination
fields, 3 unified both loops into one LoopState with a single `task` field);
an ABSENT `schema` loads as 1, and schema-1/2 files keep working: the load
coalesces the legacy `prompt`/`task_description` into `task` (coalesce_task),
and the hook stamps a missing `started_at` on first fire so an adopted old
loop is still bounded). Reads classify a file via `read_state_json` /
`load_with_status` into `LOAD_OK` / `LOAD_MISSING` / `LOAD_CORRUPT` /
`LOAD_FUTURE_SCHEMA`. Every MUTATING path — the Stop hook AND the crew-state CLI
(`init` / `set` / `deactivate`) honors the SAME contract:
- **Newer schema** (`schema > SCHEMA_VERSION`): REFUSE to touch. The hook allows
  stop with a loud diagnostic; the CLI exits nonzero, bytes untouched — a
  downgrade must never clobber a live newer-format loop.
- **Corrupt** (unparseable / non-object JSON): the Stop hook and `init` set the
  file aside as `<name>.corrupt` (a one-time diagnostic; `init` then starts fresh
  state), while `set` / `deactivate` refuse (nonzero) rather than
  silently overwrite. These set-aside artifacts are swept by `session-start`
  cleanup, which is SCOPED to crew's own backup names via exact-plus-hyphen globs
  — `build-state.json.corrupt` / `build-state-*.json.corrupt` and
  `measure-twice-state.json.corrupt` / `measure-twice-state-*.json.corrupt` (the
  legacy exact name and the session-scoped `-<id>` form only) — so neither an
  unrelated `*.corrupt` file nor a prefix-collision like
  `build-stateEVIL.json.corrupt` is ever deleted.

**One serialized read-modify-write (`models.update_state_json`).** The Stop hook
and the crew-state CLI are two PROCESSES writing one file, so `atomic_write_json`
is not enough: it makes the replace indivisible, not the read-modify-write around
it. Two unlocked writers each rebuild the file from the dict they read BEFORE the
other landed, and whichever replaces last erases the other's field. That lost
update is how an allowlisted `set` could roll back a `stop_fires` bump (the
counter-reset escape route) and how a hook save could revert `last_verdict`. So
EVERY mutation (hook counters, force-exit, `init` / `set` / `deactivate`) goes
through `update_state_json(path, mutate_fn)`, which holds one `fcntl.flock` on a
sibling `<state-file>.lock` across the read AND the write, and each writer's
`mutate_fn` touches only the keys its owner owns (so unknown on-disk keys survive
and the two writers never overwrite each other's fields even when they interleave).

- The lock is on the SIBLING, never the state file: `atomic_write_json` replaces the
  state file's inode, so a lock on the old inode would guard nothing after the first
  write. The sweep reaps stale `*.json.lock` siblings on the same anchored globs as
  the other artifacts, and an acquire touches the lock's mtime so a live loop's lock
  is never reaped mid-use.
- The wait is BOUNDED (2s, under the hook's 5s budget in hooks.json), never an
  indefinite block. A lock that cannot be taken is a FAILED write: the Stop hook
  fails OPEN (allow + loud `systemMessage`, no write, because blocking without
  writing would nudge forever with the counters frozen, so the bound that ends the
  loop would never arrive), and the CLI exits 2 with the bytes untouched.
- Without `fcntl` (non-POSIX) the write still happens, unlocked, with a stderr note:
  degrading to last-writer-wins beats wedging every state write on a platform that
  cannot lock.

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

# Usage (the load-bearing HookResult JSON shapes)
HookResult.allow()                    # {}
HookResult.allow("Info message")      # {"message": "..."}
HookResult.allow_with_diagnostic("…") # {"systemMessage": "..."} (loud allow)
HookResult.block("Must continue")    # {"decision": "block", "reason": "..."}
```

> **Do NOT emit `{"continue": false}` to block a Stop.** A two-layer live
> smoke proved it INERT for blocking — Claude Code honors it as a
> HARD-STOP directive (the child session terminated at the first stop
> attempt). `{"decision": "block", "reason": ...}` is the load-bearing,
> smoke-verified blocking shape.

### LoopState (one dataclass, both loops)

```python
@dataclass
class LoopState:
    active: bool = False
    loop: str = ""                 # "bl" | "mt"
    task: str = ""                 # was prompt / task_description (load coalesces the legacy keys)
    plan_file: str = ""            # meaningful for mt; inert but accepted on bl
    phase: str = "drafting"        # drafting | reviewing | done
    revision_round: int = 0
    last_verdict: str = ""         # APPROVED | REVISE | REJECT | FAILED
    last_verdict_overrides: list = field(default_factory=list)  # advisories a completion was forced over (empty on a clean sign-off)
    run_id: str = ""
    target_sha256: str = ""        # full 64-hex
    target_spec: str = ""          # bare resolver spec, never "auto"
    target_base: str = ""
    expected_seats: list = field(default_factory=list)
    consecutive_review_failures: int = 0
    exit_kind: str = ""            # approved | cancelled | review_failed | force_exit
    session_id: str = ""           # Session that owns this loop
    stop_fires: int = 0            # hook-owned livelock circuit breaker
    max_stop_fires: int = 150
    started_at: str = ""           # hook-owned wall clock (ISO, UTC)
    deadline_minutes: int = 240    # 1..MAX_DEADLINE_MINUTES (1440) at init; 0 only with no_deadline
    no_deadline: bool = False      # 2nd half of the config-only opt-out marker; only init writes it
    parked_fires: int = 0          # hook-owned CONSECUTIVE parked-Stop counter
    max_parked_fires: int = 20

    AGENT_SETTABLE = frozenset({"plan_file"})
```

The two on-disk file prefixes (`build-state-*` / `measure-twice-state-*`) and
the `bl`/`mt` aliases stay: they are load-bearing across the hook, state
discovery, and the session-start cleanup globs. What unified is the SHAPE.
`revision_round` is written by the review verbs, never the hook: the Stop hook
still counts nothing but its own fires. A Stop hook cannot observe a revision
round, and a round field with no honest writer would be a frozen `Round 1/N`
lie on every surface that printed it. The budget the loop actually runs on is
`stop_fires`/`max_stop_fires` plus elapsed-vs-`deadline_minutes`, and that is
what `crew state show`, the nudge, and the SessionStart banner report.

## Stop hook: the loop contract (do NOT regress)

The Stop hook writes **only what a Stop hook can honestly observe.** These five
rules are load-bearing; each one is a bug that already happened.

1. **The hook counts FIRES, not rounds.** It writes only what it owns:
   `stop_fires`, `parked_fires`, the `started_at` stamp, and the deactivation
   when a bound trips. It once incremented a field named `iteration` on every
   fire, silently redefining "revision round" to mean "Stop fire". That field is
   gone rather than faked: nothing in the Stop path can see a revision round, so
   a round counter with no writer would report `Round 1/N` forever.

2. **Termination bounds are hook-owned, and the sanctioned paths to them are
   closed.** `crew state set` writes ONLY `AGENT_SETTABLE` (exit 2 for anything
   else), it writes that ONE key field-level so an allowlisted write can't carry
   back a stale snapshot of the counters, `--deadline-minutes` is validated to
   1..1440 at `init` (the no-deadline opt-out is config-only, the flag is
   agent-invoked), and `init`
   refuses to start a fresh loop over a force-exited
   one without `--force`. A safety limit the agent can rewrite is not a safety
   limit: a live loop, cornered by a cap it kept hitting merely for WAITING, reset
   its own counter 30 times and made the force-exit unreachable. `active` is
   refused for the same reason: setting it false is an unaudited back door around
   the panel-approval gate that `deactivate` exists to be.

   **This is reliability hardening, NOT a security boundary. Do not document it
   as one.** The agent has `Write`/`Edit` on `.crew/*-state-*.json` and can edit
   any field the CLI refuses. What the allowlist buys is that no *sanctioned,
   convenient, plausibly-innocent* path resets a bound: the counter-reset livelock
   happened through the CLI, in good faith, one `set` at a time. Every remaining
   route is now a conspicuous act (hand-editing state, or `init --force`), and the
   hook reads all four counters defensively (`effective_count`) so a garbage value
   falls back to a real bound instead of raising into the fail-open handler and
   silently disabling enforcement for that fire. `started_at` is read the same way
   (`effective_started_at`) and SELF-HEALS: a missing, unparseable, wrongly-typed,
   or FUTURE-dated stamp does not raise, it deletes the wall clock (elapsed reads
   as unknown or negative, so the deadline never trips), so the fire restamps it to
   now and persists that, bounding the loop from that fire forward.

   Every one of those writes goes through the ONE serialized read-modify-write
   (`update_state_json`), because an unlocked `set` could otherwise roll a counter
   bump straight back off the disk (see the state-lock section above).

3. **Waiting is FREE, but bounded.** When the Stop payload's `background_tasks`
   or `session_crons` is non-empty the session is parked on work it launched, so
   the hook **allows** the stop and does not touch `stop_fires`. The harness
   re-invokes the session when that work completes. Blocking a parked Stop is
   what manufactured the busy-wait: the agent was forced to take a turn with
   nothing to do, so it polled, which burned the counter and flooded the context
   until compaction wiped its memory of the panel mid-flight and it re-ran the
   panel over its own completed seats.

4. **`max_parked_fires` is the escape valve.** The park signal is broad: ANY
   harness-tracked background shell (a dev server, a `tail -f`) parks every Stop,
   which would otherwise disable the loop entirely (no nudge, no bound, no
   diagnostic). So `parked_fires` counts CONSECUTIVE parks and resets on any
   BLOCKING fire: a fire with nothing in flight, or a past-cap parked fire
   that got nudged (the nudge forces a working turn, which ends the unbroken
   run of parks the cap bounds). Past `max_parked_fires` (20) a parked Stop is
   nudged like an ordinary one and costs a `stop_fire`; because the nudge also
   resets the run, a shell that never exits costs one nudge per cap-length run
   of parks, never a blocked stop on EVERY later turn-end (in a session whose
   every turn-end is parked, the nothing-in-flight reset is unreachable, so a
   kept counter once let legitimate panel waits exhaust the cap cumulatively).
   20 is generous on
   purpose: an honest seat wait costs one or two parked fires per round.

5. **Termination beats parking.** The bounds are evaluated BEFORE the parked
   check, so a background task that never finishes cannot hide behind "I'm
   waiting" forever. The wall clock is the only cost ceiling in the system: every
   other bound is denominated in a unit the agent controls.

**Known limitation (do not overclaim the deadline).** Every bound is evaluated
ON a Stop fire, and nothing in a Stop hook fires on a timer. A session parked on
a background task that hangs forever is therefore never re-invoked, so no Stop
fires and no bound (deadline included) is ever evaluated. That case is bounded by
`max_parked_fires` once the session takes turns again, and by the session-start
orphan sweep (an active state file is force-deleted after 7 days), NOT by the
deadline. The deadline is an absolute ceiling on a loop that keeps *running*, not
on one that is asleep, and only while the clock is ON: the global-config
no-deadline opt-out turns it off entirely, leaving the stop-fires cap as the
sole running-loop bound.

Deliberately NOT added: a stall timer. A single panel seat legitimately takes
minutes, so a stall timer risks killing healthy work, and any late-landing seat
would reset it forever. The absolute deadline needs no such heuristic: it is
denominated in the one unit the agent cannot spend, and it does the job.

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

✅ **Resolve via the one `crew_base()` root**
```python
from state_discovery import crew_base
directory = crew_base()            # CLAUDE_PROJECT_DIR, else cwd (the ONE resolver)
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
state = LoopState.load(state_file)  # Returns default if missing
```

## Orphan Cleanup

The `session-start.py` hook cleans up stale state files on every session start:

- **Inactive** state files older than **1 day**: deleted
- **Active** state files older than **7 days**: force-deleted (safety limit for abandoned sessions)
- **Newer-schema** state files (`LOAD_FUTURE_SCHEMA`): NEVER deleted, at any age.
  The sweep classifies with `read_state_json` first, because deleting is the most
  destructive touch there is and the refuse-to-touch contract binds it too: a
  downgraded install must not destroy a live loop it cannot even read. It leaves a
  one-line stderr note when it declines a file it would otherwise have swept.
- Applies to `build-state-*` and `measure-twice-state-*` files
- **Non-state artifacts older than 7 days** are also swept, but EVERY glob is
  anchored to a crew-specific name (cleanup runs over the SHARED `~/.claude` root
  too, so a bare-suffix glob would delete foreign files):
  - context snapshots — the EXACT literals `context-snapshot.md` (save-context)
    and `context-snapshot.restored.md` (the restore rename), plus the legacy
    crew-prefixed `context-snapshot-*.json`. NOT `context-snapshot*.md` /
    `*.restored.md` (those would match a user's `context-snapshot-notes.md` or any
    tool's restored file).
  - `.corrupt` set-aside backups — the exact + `-<id>` forms only (see the
    crew-state schema section).
  - atomic-write temp orphans — `atomic_write_json` names each temp
    `.<state-filename>.<rand>.tmp` (mkstemp `prefix=f".{path.name}."`,
    `suffix=".tmp"`), so the sweep globs exactly those anchored names
    (`.build-state.json.*.tmp` / `.build-state-*.json.*.tmp` and the
    measure-twice pair), NEVER a bare `.*.tmp`.
  - agent signal markers (`<session>/signals/<tag>.json`, from `crew signal`) past
    7 days, anchored to the `[A-Za-z0-9_-]+.json` tag grammar, never a bare
    `*.json`. Nothing else prunes them (`wait` reads and LEAVES a marker); no
    liveness check is needed because a `wait` blocks for minutes, so no marker this
    old can be one a live wait is about to read. `session-start` sweeps them via
    `sweep_signal_markers_all`, which walks every `.crew/reviews/<session>/` dir
    (signal-sweeping ONLY, it deletes no run dir).
  - state-lock siblings (`<state-filename>.lock`, from `models.state_lock`), globbed
    on the same exact + `-<id>` anchoring (`build-state.json.lock` /
    `build-state-*.json.lock` and the measure-twice pair), NEVER a bare `*.lock`.
    An acquire touches the lock's mtime, so only locks whose loop is long gone reach
    the age threshold: deleting one mid-use would split that loop's writers across
    two inodes.

**Review-run + debate dirs: REPORTED, never deleted here.** `session-start` no
longer `rmtree`s review-run dirs (`.crew/reviews/<session>/run-*`) or stale debate
dirs (`.crew/debates/`): destructive removal of the user's disk from an unattended
hook was the wrong venue. Instead `report_stale_artifacts` enumerates the orphans
through the ONE shared `artifact_prune.collect_prunable` finder (the same list
`crew swab` acts on, with the same active-loop / current-run-pointer protections
and mint-grammar/symlink guards) and RETURNS a single terse line that `main()`
rides on the SessionStart `additionalContext` channel (a successful hook's only
reliably-delivered carrier, not stderr). It enumerates count-only
(`with_sizes=False`), so the notice names the orphan COUNT + the `crew swab` hint
with NO byte total: exact per-dir sizing is the attended swab's job, never a walk
on every session start. It deletes NOTHING. The attended `crew
swab` command OWNS the deletion (dry-run by default; `--yes` removes). Removing the
old delete path also retired the three data-loss bugs the review-run sweep carried
(over-broad `RUN_ID_RE`, symlink-follow, live-run-swept).

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
- Concurrent writers: a hook counter bump and a `crew state set` landing in the same
  window both survive (the widened-window case FAILS without the lock), plus the
  fail-open/exit-2 behavior when the lock cannot be taken

## Working Here Checklist

- [ ] Import models from `models.py`, don't duplicate dataclasses
- [ ] Resolve the project root via `crew_base()` (the ONE resolver: CLAUDE_PROJECT_DIR, else cwd), never a hand-rolled `os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())`
- [ ] Handle missing state files gracefully (return defaults)
- [ ] Mutate a live loop's state ONLY via `update_state_json` (locked read-modify-write, own keys only); `state.save` is a whole-state REPLACE, never an edit
- [ ] Never hand-roll a write + chmod (`atomic_write_json` is 0600-from-birth, atomic)
- [ ] Run `tests/test-hooks.py` after changes
- [ ] Output valid JSON only (no print debugging to stdout)

## Related Guides

- [Project Root](../../../.claude/CLAUDE.md) - Adding hooks, overall architecture
