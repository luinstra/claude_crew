# Motley Crew multi-harness engine roadmap

> Status: high-level development roadmap
>
> This document defines the destination, ownership model, phase sequence, and
> merge gates. It is intentionally not an implementation specification. Each
> phase receives its own narrowly scoped plan and review immediately before that
> phase is implemented.
>
> Review this document for architectural direction, sequencing, useful phase
> exits, and missing outcomes. Detailed schemas, command arguments, prompt text,
> adapter mechanics, and recovery algorithms belong in the phase that first
> needs them.

## Desired outcome

Motley Crew provides the same core review, council/debate, measure-twice, and
build workflows inside Claude Code, OpenHands, and Codex. Python owns the workflow;
each harness is a thin adapter that asks what to do next, performs the requested
host operation, and returns the result.

At completion:

- workflow decisions, prompts, routing, persistence, validation, retries, and
  terminal outcomes have one Python source of truth;
- Claude Code, OpenHands, and Codex drive the same workflow interface rather than
  carrying separate copies of each workflow;
- existing seat and panel configuration works across harnesses through the
  existing `via` channel model;
- native host subagents are used where supported, while existing external CLI
  providers remain first-class execution routes;
- Claude behavior remains working throughout the migration;
- Cursor gained in-app review subagents as the first new host target, verified
  live in the app; its debate seats ship on the same seam but are not yet
  exercised there. Shipped Cursor behavior keeps regression coverage; remaining
  Cursor expansion is outside this roadmap (see "Deferred: Cursor work");
- Codex receives the next adapter over the existing protocols, followed by
  OpenHands native SDK agents and deterministic orchestration;
- review seats remain fresh, executor continuation remains executor-only, and
  current frozen-target, quorum, failure-isolation, route, and loop-state
  invariants remain enforced; and
- the existing repository becomes Motley Crew rather than being forked into a
  second product.

The generic user-facing identifiers `crew`, `/crew`, and `.crew` do not need to
change merely for branding. Repository and product naming are separate from the
workflow migration and must not delay the Codex or OpenHands milestone.

This roadmap covers the four core workflows and the support paths they call.
Other Task-oriented commands such as `analyze`, `code-search`, `execute`, and
`deepinit` remain documented-unsupported in Cursor unless a later plan brings
them into scope; their current silent substitution is not legitimized by this
migration.

## Queue order

Phases 1–5 are implemented on the integration branch. The remaining queue is
**7 (Codex), 6 (OpenHands), 8A (cleanup)**. The operator's 2026-10-03 pivot
replaces the planned Cursor expansion with OpenHands. Phase 6 keeps its number
but now names the OpenHands adapter; its old Cursor scope is retained only as
historical backlog below. The operator subsequently scheduled Codex first;
phase numbers remain stable while execution order is 7 then 6. 8B (rebrand) is the
operator's call and is not scheduled here.

## Grounded starting point

### Current handoff (2026-10-03)

The source integration branch is `codex/multi-harness-engine`; this Codex task
works in a separate worktree on `codex/native-integration`, based on `6b16ba3`. Phase 5 is
committed and pushed in `179b815`, with Crew 0.85.0 and marketplace 0.45.0 in
`6b16ba3`. It has not been merged into `main` or installed. Standalone review,
council/debate, measure-twice, and build now use Python-owned workflows. Phase 4
remains recorded in `50aef3b`, published with its version bump in `9d75917`.

Phase 4 completed with five approving reviewers, quorum met, and no blocking
findings. The Claude Code CLI lifecycle gate, exact source epochs, regression
results, and remaining evidence limits are recorded in
[phase-4-measure-twice-evidence.md](phase-4-measure-twice-evidence.md). Its
[implementation plan](phase-4-measure-twice-plan.md) remains the historical
design record. This completion does not close unrelated app gates in
`operator-followups.md` or the deferred Cursor work.

The [Phase 5 plan](phase-5-build-plan.md) completed measure-twice against that
published baseline with quorum met and minor implementation clarifications.
Implementation ran through the configured Sol executor via Codex.
Migration, legacy handling, upgrade guidance and special restart machinery are
excluded by the operator. Phase 5 is implemented, verified, and pushed; the
installed plugin has not been refreshed. Current interface, regression
results, real Claude CLI gates, exact source epochs and evidence limits are in
[build-protocol.md](build-protocol.md) and
[phase-5-build-evidence.md](phase-5-build-evidence.md).
The final planning synthesis is retained under
`.crew/reviews/01a0f9e3-b687-7e63-b9e5-d1e8a07774b5/run-1887652ecf97/`.

Next is the [Phase 7 Codex plan](phase-7-codex-plan.md). Its source adapter
is implemented in the isolated dirty worktree; the [live native gates](phase-7-codex-evidence.md)
passed. Implementation review closed minor-only without overrides. The follow-up fixes,
installed skill discovery and automatic hook re-entry gate are now complete. The [Phase 6 OpenHands plan](phase-6-openhands-plan.md)
follows Codex and remains a planning pivot, not an implemented adapter or a passed gate.

The older machine-local `chunk-c-measure-twicemd-shim-refactor-*` and
`chunk-c-buildmd-shim-refactor-*` plans describe an earlier prose-reduction
approach. Their rules preserving Markdown loop decisions and prohibiting engine
changes are superseded by Phases 4 and 5. Retain their invariant and transport
history as reference, not as current execution instructions.

### Existing implementation

This is a restructuring of an existing Python engine, not a greenfield rewrite.
Paths in this section are relative to `plugins/crew/`. The current code already
provides most of the difficult lower-level behavior:

- `scripts/multiagent/seats.py` owns seats, panels, models, access, and `via`;
- `scripts/multiagent/channels.py` resolves a configured channel against the
  current host and decides whether a route is native or external;
- `scripts/multiagent/providers/` owns normalized external Claude, Codex,
  Cursor, and Agy execution;
- `scripts/multiagent/targets.py`, `prompts.py`, `review_runs.py`, and
  `findings.py` own target freezing, much of prompt rendering, run persistence,
  result normalization, repair, and quorum inputs;
- `scripts/multiagent/review_workflow.py` owns debate round persistence and
  transitions;
- `scripts/multiagent/continuations.py` and the shared execution implementation in
  `scripts/multiagent/execution.py` own guarded external executor continuation and
  workspace checks; and
- `models.py` and `loop_state.py` own shared loop state and guarded transitions;
  `multiagent/measure_twice.py` owns planning actions and
  `multiagent/build_workflow.py` owns implementation/review cycling. Existing
  `crew-state.py` mutation entrypoints respect workflow ownership.

Build now composes the shared review workflow and loop policy established by
measure-twice. Its command and lifecycle-hook branches transport issued actions
and lightweight projections. Python owns prompts, retries, bound human decisions
and terminal outcomes.

The extraction deepens the existing Python modules behind one small
workflow interface. It does not replace working providers, routing, state,
review persistence, or continuation machinery with parallel abstractions.

## Architectural direction

### The workflow module

The workflow engine is a deep Python module. Conceptually, its interface allows
a caller to:

1. begin or resume a workflow;
2. ask for the next action;
3. submit the result of a requested host action; and
4. repeat until the workflow reports a terminal or intentionally paused result.

Python owns everything needed to decide that next action:

- workflow phases and transitions;
- canonical workflow and role prompts;
- seat, panel, channel, model, access, and executor selection;
- external provider execution;
- run and loop identity;
- target freezing and workspace guards;
- persistence and result normalization;
- timeout, retry, cancellation, repair, and continuation policy;
- quorum and verdict interpretation; and
- the distinction between waiting, paused, failed, and complete.

The initial interface is only as broad as review requires. Later phases deepen
the same module when debate, persistent planning, and build demonstrate real new
needs. The roadmap does not design every future action type in advance.

### Harness adapters

A harness adapter owns only behavior that genuinely differs by host:

- locating and invoking the installed Python dispatcher;
- executing a Python-requested native host operation;
- awaiting or cancelling that native operation using real host mechanics;
- returning its result to Python; and
- translating lifecycle-hook input and output shapes.

An adapter does not select a panel, render a prompt, interpret a verdict, choose
a retry, inspect workflow state to decide what comes next, or silently
substitute a different role or route.

Commands, skills, agent definitions, and hooks become thin protocol drivers at
the time their workflow migrates. Static host metadata and tool permissions may
remain host-specific; canonical role instructions and flow decisions do not.

### Deterministic transport and execution runtimes

A model provider identifies the requested model service. An execution runtime
identifies how an agent conversation is run. A native agent may use a model from
any provider its runtime supports. Native versus external therefore does not
decide whether Python can execute the work: some hosts expose native operations
only to the parent agent, while an SDK adapter can run native conversations from
code. Preserve the shipped seats, panels, and `via` selection semantics; expose
only the minimal adapter seam the current phase exercises.

The existing provider-specific native-channel mapping is the shipped Claude
and Cursor implementation, not a requirement for every future harness. A future
runtime that admits several model providers must not need a separate workflow
or silently reroute existing seats. Phase 6 exercises this distinction with an
OpenHands adapter and direct result capture. A general capability registry
remains out of scope; earlier deterministic adapter fixtures do not establish
OpenHands support.

Move argument parsing and quoting, artifact hashing, result-envelope
construction, and mechanical persistence into code wherever the host can
transport the required input. Keep host-specific spawning, awaiting,
cancellation, and result return in the adapter. Do not add an agent for
bookkeeping. Retain the existing scribe transport only where the host needs it
for return/transcript handling, and document that limitation. A runtime with
direct result capture persists the return in code rather than invoking a
scribe. Deterministic helpers never reinterpret review text as instructions or
discard its raw form.

Each loop cutover must preserve overlapping independent reviews and avoid
polling. Record orchestration tool calls, bookkeeping model invocations,
reviewer freshness, elapsed wall time, provider time, and interruption/resume
behavior on an equivalent baseline and migrated run with the same target,
models, and panel. Deterministic execution traces gate the cutover; live token
and latency observations are reported separately and are not a promised
speedup. Explain any added orchestration calls before the phase closes.

### Shipped routing and `via`

The existing configuration model remains the routing foundation:

- a seat's `via` value names its logical execution channel;
- the host resolver determines whether that channel is native in the current
  harness;
- a native channel is executed by the harness adapter;
- a non-native channel is executed by its existing external provider adapter;
  and
- host identity and execution channel remain independent concepts.

This gives the desired Cursor behavior without a second configuration system:
inside Cursor, Cursor-channel seats can use Cursor-native subagents while
Claude-channel seats continue to use the existing Claude CLI provider. The same
panel configuration remains meaningful in Claude and, later, OpenHands and Codex.
OpenHands needs an explicit native runtime selection independent of the seat's
model provider. Phase 6 will define the smallest such extension without changing
the meaning of existing `via` values or automatically rerouting existing seats.

The current one-channel-per-seat rule is sufficient for the first-class Cursor
workflow. Ordered multi-channel fallback stays deferred until a concrete use
case requires it. The force-external setting is no longer deferred: replacing a
live-verified external route with a native one that rests on unverified
app-surface assumptions, on the operator's only permitted Cursor surface, was
that use case. It ships as `[review].force_external_channels` (CLI
`--force-external` > per-repo > global > built-in, default force nothing), and a
listed channel is resolved external for seats and drives no in-session support
role either. `docs/engine-notes.md` carries the reasoning.

## What changes and what does not

| Area | Direction |
| --- | --- |
| Seats, panels, `via`, and channel resolution | Keep and deepen |
| External provider adapters | Keep |
| Target, prompt, run, finding, and quorum behavior | Keep and deepen |
| Loop state, guarded transitions, dispatch, and continuations | Keep and compose |
| `multiagent/cli.py` | Reduce gradually to dispatch and support entry points; no wholesale rewrite |
| Workflow Markdown | Replace phase trees with thin protocol drivers in the owning phase |
| Hook serializers and host detection | Keep |
| Workflow recipes embedded in hooks | Move into Python in the owning loop phase |
| Claude agent definitions | Retain only as thin native-role adapters where needed |
| Cursor agent surface | DONE for review and debate: `agents-cursor/` ships four thin roles (reviewer, panelist, scribe, formatter). Review is app-verified, debate is not yet. Advisor and executor roles are deferred |
| OpenHands SDK agents | Add after Codex through a deterministic adapter; native roles may use different model providers |
| Codex skills and native roles | Source adapter and live native gates complete; installed discovery remains separate |

The deletion test governs the result: if the workflow module disappeared,
workflow complexity should reappear across every harness. Deleting a harness
adapter should remove only that host's mechanics, not workflow behavior.

## Role of the research document

`plugins/crew/docs/multi-harness-research.md` remains a deferred research
document. It contains valuable current-state evidence, invariants, host risks,
and failure cases, but its detailed schemas, compatibility machinery, adapter
contracts, and phase sequence are non-normative.

Each phase rechecks only the research claims relevant to that phase against the
current code, current host documentation, or a live probe. A proposal does not
enter the production design merely because the research document considered
it.

This project serves its current user. Historical plugin versions, old state
schemas, upgrade paths, inactive installed copies, and compatibility for other
users or machines are out of scope. We may make breaking internal changes and
delete superseded paths once the current user's active workflow no longer uses
them.

## Development discipline

Every numbered phase is separately planned, reviewed, implemented, and verified.
Integrate its reviewed prerequisite work before starting the next implementation.
The current branch handoff above is an explicit exception permitting the next
plan to be reviewed before the outstanding Phase 1-3 merge; it does not authorize
a merge or treat the phase's unrun host gates as passed.

Two exceptions let a phase advance with a gate arm outstanding. Both keep the
arm named in the phase's own gate, so nothing is quietly dropped, and no other
exception is sanctioned:

- An OPERATOR-ONLY arm, one needing a GUI, an account action, or a push, never
  blocks engine work. It moves to `docs/operator-followups.md` and stays owed
  there. This is the existing policy in that file, stated here so the two agree.
- A SCOPE arm explicitly excluded by the operator does not gate later phases.
  The former Cursor arms are retained in "Deferred: Cursor work" as historical
  backlog following the OpenHands pivot; they no longer gate roadmap completion.

For every phase:

1. Establish the reviewed prerequisite baseline. Prefer current `main` after
   integration; if preceding phases are still on an integration branch, name
   that dependency explicitly and do not start from an older `main` missing
   their engine. Do not rewrite pushed prerequisite history to fit a plan.
2. Write an implementation plan limited to that phase's outcome and exit gate.
3. Tell reviewers explicitly that downstream schemas and mechanics are out of
   scope unless the current phase cannot work without them.
4. Revalidate every reviewer claim against the current code and appropriate
   host documentation or live behavior before changing the plan or code.
5. Test through the deepest new module interface. Replace superseded tests of
   Markdown shape with behavioral tests at that interface; do not retain two
   permanent sources of test truth. Keep host-driver safety tests that still
   protect executable shell fences, argument forwarding, or permission
   allowlisting after the driver becomes thin.
6. Preserve existing invariant coverage for configuration precedence, target
   freezing, strict-majority quorum, failure isolation, fresh reviewers,
   executor-only continuation, workspace guards, and loop state.
7. Run the relevant existing suites as well as the focused phase tests. Test
   commands must neutralize ambient `CREW_HOST` and `CLAUDE_PROJECT_DIR` unless
   a case intentionally supplies them.
8. Use small installed live probes only when the phase changes real host
   mechanics. Deterministic engine behavior is proven locally; model prose is
   never the oracle. The phase plan names the observable route, action sequence,
   terminal class, invariant checks, and forbidden fallback that make its exit
   repeatable.
9. Reduce a migrated command or hook to a thin driver before that phase merges.
   Do not carry the old executable phase tree to a final cleanup phase.
10. Stop at the phase gate. Do not implement later-host or later-workflow
    machinery to make the current phase appear more comprehensive.
11. Verify and integrate the phase before producing the next implementation
    plan, except for the explicitly recorded current handoff above. A downstream
    plan cannot claim a prerequisite gate passed merely because code is present.
12. Keep exactly one final generated version-bump commit for each merged phase;
    intermediate bump commits are consolidated before merge.

Shared registries, generated drivers, capability databases, operation ledgers,
and generalized recovery protocols are not prerequisites. A phase may introduce
one only when its production slice demonstrates that the simpler existing
mechanism cannot satisfy the required invariant.

## Phase sequence

### Phase 1 — Engine-owned review without breaking Claude

#### Outcome

Review becomes the first complete Python-owned workflow. Claude Code continues
to run its current native and external review routes through a thin production
adapter.

#### Scope

- Introduce the smallest next-action/result interface required by review.
- Build the review workflow by composing the existing seat, channel, target,
  prompt, run, provider, result, repair, and quorum modules.
- Move the reviewer, scribe, and formatter instructions and all review flow
  decisions into Python.
- Preserve current panel resolution, `via` routing, frozen targets, prompt-file
  behavior, pending-seat resume, failed-seat isolation, and strict-majority
  quorum.
- Add an in-memory adapter for deterministic workflow tests and a thin Claude
  Task adapter for production.
- Reduce the Claude review command to a protocol driver.
- Keep the workflow interface internal and provisional until Cursor proves the
  second production adapter.
- The phase plan may split this into an internal engine slice followed by the
  Claude production cutover. Both slices must merge before Phase 2 begins, and
  neither may leave two production owners of review behavior.

#### Exit gate

Claude review produces equivalent normalized results and terminal behavior on
the existing deterministic scenarios and a bounded installed smoke. No review
prompt or workflow decision remains in the Claude command driver.

#### Deferred

Cursor-native execution, debate, persistent loops, build, generalized driver
generation, and Codex are out of scope.

### Phase 2 — Cursor-native review MVP

#### Outcome

The full review workflow runs inside the Cursor app with real Cursor-native
subagents and can mix them with Claude CLI seats through existing configuration.
This is the first priority plateau.

#### Scope

Status: engine complete, app gates pending. Every engine item below has
shipped, including the seat-pin decision (`native_model` on the catalog row,
admission gated on it, attribution stamped and rendered; recorded in
`docs/cursor-host.md` and `docs/engine-notes.md`). Pending, all app-only,
tracked in `docs/operator-followups.md`: F2.1 read-only enforcement on the
app, F2.2 the P12 app-side environment capture, F2.3 the P13 hook-environment
reading, F2.4 the installed-plugin validation through the marketplace refresh
path, and F2.5 native cancellation. F2.6, the Claude-host installed
regression, passed on 2026-09-02 as `run-d067f1b5e3fa` and is no longer owed.

- SHIPPED, PARTLY: the capability probe covering custom agent
  discovery, explicit role invocation, foreground result return, cancellation,
  read-only enforcement, requested-model handling, and observable route/model
  provenance. `docs/cursor-host.md` carries a full EXTERNAL-CLI batch plus two
  live app-surface review runs (2026-08-25) that settle discovery, invocation,
  foreground return, and route and model provenance; still unmeasured on the
  app are read-only enforcement and the P12 and P13 environment captures.
  Cursor documentation is design input; the current installed app is the
  admission authority.
- If the installed app cannot support a truthful native seat, stop the phase
  and reassess rather than substituting `cursor-agent`, silently changing the
  model, or claiming the priority plateau at a lower tier. The unverified
  assumptions this rests on have an operator escape hatch,
  `[review].force_external_channels`, which sends the seats back out through
  their CLI without a host override.
- SHIPPED: thin Cursor-native reviewer, scribe, and formatter role adapters, in
  `plugins/crew/agents-cursor/`.
- SHIPPED: Cursor's native request, await/cancel, and result-return mechanics
  (the scribe transport, its host-write fallback, and the `native_task_lost` /
  `formatter_task_lost` recovery codes).
- SHIPPED: reliable Cursor host detection, from the marker tables rather than an
  operator `CREW_HOST` value that Cursor may scrub from agent shells.
- SHIPPED for standalone review: the Cursor host resolves the Cursor channel
  natively while leaving non-native channels, including Claude, on their
  existing external providers. The `review-prep` paths (build, measure-twice)
  keep every cursor seat external until their own phases.
- SHIPPED for standalone review: explicit supported routing in place of the
  silent substitution of Claude Task roles. The other Task-spawning commands
  still substitute silently.
- SHIPPED: the same Python review workflow and deterministic fixture Claude
  drives.

#### Exit gate

A live review launched from the Cursor app completes with at least one
Cursor-native seat and one Claude CLI seat, with truthful route provenance and
no `cursor-agent` dependency for the native seat. The Claude review regression
gate remains green. Passing this gate proves the second production adapter and
promotes the shared workflow interface from provisional to the stable seam that
later workflow phases deepen only for demonstrated new needs.

#### Deferred

Native Cursor advisors/executors, automatic loop continuation, ordered `via`
fallback, and Codex are out of scope.

### Phase 3 — Council and debate

#### Outcome

The remaining read-only panel workflows reuse the proven review seam instead of
creating another orchestration path.

#### Scope

Status: engine complete for council and debate; app gates pending (F3.0, F3.1, F3.2 in docs/operator-followups.md). No debate or council phase tree remains in Markdown.

- Move council and debate prompts, panelist instructions, round transitions,
  prior-round context, freshness, partial-failure behavior, and synthesis flow
  into Python. Python renders the synthesis request and decides when it runs;
  the requested model still produces the synthesis text.
- Add the thin Cursor-native panelist role in this phase so the Cursor gate uses
  a real native debate seat rather than external-only execution or silent role
  substitution.
- Reuse routing, native work requests, external providers, normalization, and
  persistence where the behavior is genuinely shared. Phase 3 may deepen the
  Phase 2 adapter interface only for a new host operation that debate actually
  requires.
- Treat council as the bounded single-round slice and debate as the multi-round
  extension. They may be planned and merged as 3A and 3B if the implementation
  plan finds that safer.
- Reduce each migrated command to a thin driver in its owning slice.

#### Exit gate

Claude completes deterministic and bounded live council/debate scenarios
through the shared adapters, including fresh-seat and partial-failure cases. No
debate or council phase tree remains in Markdown. The Cursor arm of this gate
(F3.1 and F3.2) is deferred: see "Deferred: Cursor work".

#### Deferred

Early-stop convergence, a `.crew/debates/` migration, and native Cursor advisor
or executor roles.

### Phase 4 — Measure-twice and lifecycle driving

#### Outcome

Measure-twice becomes a Python-owned workflow that composes review behind a
host-neutral interface. Adding a host must not require new workflow policy.
Claude is the only production harness this phase proves, and the phase claims
nothing more.

#### Scope

- Move advisor instructions, planning prompts, requirements decisions, budgets,
  deadlines, cancellation, verdict handling, and revision transitions into
  Python.
- Reuse the current `LoopState` and guarded `crew-state.py` transitions rather
  than creating a parallel persistence system.
- Compose the engine-owned review workflow.
- Bind each child review to its owning loop and revision without overwriting
  standalone-review pointers. Preserve the state completion gate's frozen run,
  target hash, roster, drift, and human-only override checks; name the guarded
  handoff and replay behavior in the phase plan.
- Move deterministic transport behind code helpers, exercise direct result
  capture with a fake runtime, and compare orchestration overhead with the
  existing loop on equivalent work.
- Reduce the measure-twice command to a protocol driver.
- Migrate only the measure-twice branch of SessionStart and Stop to lifecycle
  translation plus a request for the engine's next action. The existing build
  branch remains intact until Phase 5 migrates it; shared-hook tests cover both
  branches and their current precedence during the transition.
- Preserve Claude's existing automatic continuation behavior.

#### Exit gate

A harmless plan-only loop can start, pause, resume, revise, and complete in
Claude with the same Python decisions, and Claude automatic continuation still
works. The workflow stays host-neutral (no Claude-only assumption enters the
Python), but the Cursor arm of this gate is deferred: see "Deferred: Cursor
work".

The gate also requires one production owner of planning/revision/review policy,
safe interruption and replay without duplicate agent work or renewed budgets,
an all-native direct-capture test with no scribe, unchanged build behavior, and
recorded baseline/migrated orchestration counts. This phase does not claim an
OpenHands implementation or mechanically enforced access without host evidence.

### Phase 5 — Engine-owned build using existing executor routes

Status: implemented, verified, committed in `179b815`, and pushed with the
`6b16ba3` version bump (Crew 0.85.0; marketplace 0.45.0). Not installed. The Claude
Code CLI gate covers native executor and configured Sol via Codex, real reviews
and revision, exact-ID continuation, human waits, concurrent reviewers, recovery,
guards and terminal replay. Evidence limits and measured comparisons are recorded
in [phase-5-build-evidence.md](phase-5-build-evidence.md). Cursor work remains deferred.

#### Outcome

Build becomes a Python-owned workflow by composing the existing dispatcher,
continuation store, workspace guards, and review workflow.

#### Scope

- Move executor selection, implementation/review cycling, implementation and
  revision prompts, retries, drift handling, and terminal decisions into Python.
- Preserve Claude's current native executor through a thin Claude adapter.
- Preserve existing external write-capable executor routes and guarded
  continuation behavior.
- Keep reviewers fresh and continuation limited to the executor.
- Reuse the Phase 4 loop/review and transport seams. Preserve target and workspace
  guards, and record comparable orchestration overhead for the disposable build.
- Never present a read-only provider as a write route, and never substitute one
  executor for another to get a build to run. If the configured write-capable
  executor is unavailable on the current machine, the phase gate blocks rather
  than silently changing routes. This is a route-honesty rule and holds on every
  host.
- Reduce the build command and remaining build hook recipe to protocol driving.

#### Exit gate

A disposable build completes in Claude, route identity and workspace guards
remain enforced, executor continuation works where configured, reviewers remain
fresh, and no host driver owns build policy. The Cursor arm of this gate is
deferred: see "Deferred: Cursor work".

### Phase 7 — First-class Codex adapter

Status: source adapter implemented in the isolated worktree based on `6b16ba3`;
unstaged and uncommitted. Synthetic suites and [real native gates](phase-7-codex-evidence.md)
passed. Initial review findings are fixed; final panel approval is pending. Installed discovery and new automatic
hook re-entry remain separate, unverified follow-ups; active plugins were not changed.

#### Outcome

Codex runs the existing four Python-owned protocols before OpenHands. Claude's
proven interface and Cursor's live review evidence inform the seam; unrun Cursor
debate behavior is not offered as evidence.

#### Scope

- Follow [phase-7-codex-plan.md](phase-7-codex-plan.md), the discoverable source
  skills and [Codex transport](codex-transport.md).
- Use canonical role prompts and frozen native reviewer model/effort, with fresh
  contexts and advisory inherited access. Preserve force-external and named
  external build selection; configured Sol stays external.
- Capture the actual final reply through explicit host-written return and the
  existing capture CLI, without a scribe or speculative transcript parser.
- Bind actual collaboration handles. An interrupt reports previous status and
  cannot release an uncertain writer fence.
- Support explicit journal resume. Native executor rounds are fresh; external
  exact-conversation continuation remains unchanged. Automatic re-entry and
  installed discovery claims require observed host evidence.

#### Exit gate

The parent runs bounded disposable review/debate, plan promotion, build/review/
revision, explicit resume and cancellation gates with exact actions, models,
handles and captures retained. No paid or recursive gates run inside the
implementation executor. Record the installed discovery gate separately; source
skills alone do not prove app exposure. Preserve Claude, Cursor and external
regression coverage. OpenHands follows this gate, rather than being a prerequisite.

### Phase 6 — OpenHands native agents through the shared engine

#### Outcome

OpenHands follows the Codex adapter as a first-class workflow harness. Reviewers,
panelists, advisors, and executors are native OpenHands SDK agents with explicitly selected
models. Crew's Python engine drives their work and retains workflow authority.

#### Scope

- Follow [phase-6-openhands-plan.md](phase-6-openhands-plan.md): prove the runtime
  seam, then read-only workflows, persistent planning, and native-write build.
- Use a deterministic driver to launch concurrent independent reviewers and
  capture their returns directly; no model is needed for bookkeeping.
- Preserve seat/model/route identity, fresh reviewers, executor-only
  continuation, existing engine guards, and bound human decisions.
- Isolate optional SDK dependencies from Crew's stdlib-only core. Verify access,
  cancellation, result capture, and resume against a pinned SDK version.
- Preserve shipped Claude, Cursor, and external-provider behavior. Existing CLI
  providers remain available, but do not count as native OpenHands evidence.

#### Exit gate

All four workflows complete through the shared engine with native OpenHands
roles, including a concurrent multi-model panel and a disposable write/review/
revision build. Route identity, enforced access, cancellation quiescence,
explicit resume, and direct result capture have recorded evidence. Automatic
lifecycle claims require a separate observed gate. No second workflow owner or
bookkeeping agent is introduced.

The former Phase 6 Cursor-native executor scope is historical backlog under
"Deferred: Cursor work" and is not a prerequisite.

### Phase 8 — Cleanup, rebrand, and release

#### Outcome

The repository ships as Motley Crew with one production implementation of each
migrated workflow and an evidence-backed support statement.

#### Scope

- Remove only orchestration, agent bodies, support facades, and tests made
  unused by earlier phase-local cutovers.
- Confirm command Markdown, skills, hooks, and host adapters contain no workflow
  prompts or phase decisions.
- Rename the existing repository and user-visible product to Motley Crew. Do
  not create a stale compatibility repository or a parallel maintained fork.
- Retain generic `crew` identifiers unless changing one provides a concrete
  product benefit.
- Refresh and validate the current installed copy in every harness whose
  validation is runnable at the time: Claude, OpenHands, and Codex in 8A, plus
  shipped Cursor regression checks that need no GUI. Cursor marketplace-refresh
  validation (F2.4) is historical backlog outside the active roadmap.
- Document the execution and lifecycle behavior actually demonstrated in each
  harness.
- Publish the release notes.

The phase is two independently reviewed and merged slices: **8A cleanup** proves
that every removed path is unused, then **8B rebrand/release** renames and
publishes the already-clean product. Naming cannot block or disguise the
single-implementation cleanup gate.

#### Exit gate for 8A (cleanup)

Every path removed is proven unused; no superseded workflow implementation
remains for any workflow already migrated; command Markdown, skills, hooks, and
host adapters carry no workflow prompts or flow decisions; and the installed
artifacts match the validated source on the harnesses whose installed validation
is runnable under Phase 8A's scope. The unrun Cursor refresh is recorded at F2.4
as historical backlog. 8A does not wait on Cursor expansion or any naming change,
and it is the last scheduled phase before the stop boundary.

#### Exit gate for 8B (rebrand and release)

The repository and user-visible product are presented as Motley Crew and the
release notes are published. 8B is the operator's call and is not scheduled by
this roadmap.

#### Completion gate for the roadmap

All four workflows use the same Python engine in every harness the roadmap
claims to support, at the tiers each harness has actually demonstrated; F3.0 has
run; and 8B has shipped. The 2026-10-03 pivot formally removes unfinished Cursor
expansion from this gate: "Desired outcome" and "Overall completion criteria"
now name OpenHands in its place. Shipped Cursor behavior retains regression
coverage and its existing, limited support statement.

## Deferred: Cursor work

**Historical backlog, outside the active roadmap as of 2026-10-03.** OpenHands
replaces this future expansion. These items are not scheduled or owed for this
roadmap's completion. **Do not start them without a new operator request.**

The recovered scheduling decision is dated 2026-09-03. It deferred remaining
Cursor-specific work while shared-engine work continued. The historical draft
motivated that choice with operator-reported model-access uncertainty; this
roadmap does not assert that report as a current provider fact. The scheduling
choice was superseded by the operator's 2026-10-03 OpenHands pivot. The details
below preserve the earlier scope and evidence gaps rather than promise delivery.

Existing Cursor review and debate seats, the four `agents-cursor/` role adapters,
host detection, and hook output shapes remain shipped. Review is app-verified;
debate is not, which is what F3.1 and F3.2 would settle. Deferral does not remove
those paths or waive their regression coverage.

### Deferred item 1: the read-only Cursor-native advisor

Was Phase 4 scope. Add the read-only Cursor-native advisor so Cursor can perform
the planning action, using the proven native-work seam with a Python-rendered
advisor prompt. Phase 4 ships without it and its exit gate is Claude-only;
nothing in the measure-twice workflow may assume a Claude host, so this stays a
pure adapter addition.

### Deferred item 2: Cursor lifecycle resume for the loops

Was Phase 4 scope. Make explicit resume complete and supported in Cursor, and
improve automatic Cursor continuation only if live hook behavior proves it.

### Deferred item 3: the Cursor arms of the Phase 4 and Phase 5 exit gates

The Phase 4 arm: a harmless plan-only loop starts, pauses, resumes, revises, and
completes in Cursor with the same Python decisions Claude gets, and Cursor has an
honest explicit-resume path even if its Stop hook cannot reliably force another
turn. Automatic Cursor continuation is claimed only if live hook behavior proves
re-entry.

The Phase 5 arm: a disposable build completes in Cursor with route identity and
workspace guards enforced, executor continuation working where configured,
reviewers staying fresh, and no host driver owning build policy. Any verified
write-capable route may carry it, external or native. The route-honesty rule in
Phase 5 still holds: no substitution, and no read-only provider presented as a
write route. Explicit resume is the complete baseline; automatic continuation is
required only where the installed Cursor hooks have proven actual re-entry.

Order depends on the machine. Where an external write-capable executor CLI is
installed and verified, this item can pass on its own. If that route is absent
when the gate runs, item 4 lands first and supplies the route this arm verifies.

### Deferred item 4: former Phase 6, the Cursor-native executor

#### Outcome

Cursor gains a native executor without changing workflow semantics or weakening
access controls.

#### Scope

- Add the Cursor-native executor adapter using Python-rendered prompts. It
  depends on deferred item 1, the read-only Cursor advisor, which Phase 4 no
  longer ships; take them in that order.
- Admit native writes only after a disposable-workspace probe proves the
  requested access, role scope, cancellation, and result behavior.
- Route the new Cursor-native build work through the same `via` and host
  resolver seam already used by review and by the advisor in deferred item 1.
- Improve Cursor automatic lifecycle continuation only when an observed hook
  event actually re-enters the engine; otherwise keep explicit resume as the
  truthful supported path.
- Preserve all existing Claude behavior.

#### Exit gate

Cursor completes a disposable native-write build through the shared engine, and
the native-advisor regression from deferred item 1 remains green. If automatic
continuation is advertised, a live probe demonstrates actual re-entry; otherwise explicit
resume remains the documented behavior.

### Deferred item 5: the unrun app gates

`docs/operator-followups.md` holds them: F2.1 through F2.5 from the review MVP,
and F3.1 and F3.2 for council and debate. The app surface is not unevidenced:
the Phase 2 exit gate ran live in the app, and the shipped `native_model` pin
was badge-verified there (`docs/cursor-host.md` carries both). These gates extend
that evidence to read-only enforcement, cancellation, env capture, and the
council and debate paths, none of which has been exercised on the app. F3.0 is
NOT in this section: it is the Claude gate on the installed plugin and stays
owed.

## Supported plateaus

The roadmap intentionally provides useful stopping points:

1. **After Phase 2:** Cursor can run mixed native Cursor and Claude CLI reviews.
2. **After Phase 4:** review, debate, and a portable planning loop are all
   Python-owned, proven on Claude.
3. **After Phase 5:** all four workflows are Python-owned, proven on Claude,
   with every host-neutral decision inside the engine.
4. **After Phase 7:** Claude and Codex share the core protocols at their observed
   host tiers; installed/native gates must be recorded before claiming this increment.
5. **After Phase 6:** Claude, Codex and OpenHands share all core workflows, with
   native OpenHands roles and deterministic orchestration.
6. **After Phase 8A:** one production implementation of each workflow. F3.0, the
   Claude gate on the installed plugin, remains owed if not already completed.
   Cursor expansion is historical backlog outside this roadmap.

These are completed increments, not partially installed future designs.

## Overall completion criteria

The roadmap is complete when:

1. review, council/debate, measure-twice, and build are Python-owned workflows;
2. Python renders every canonical workflow and migrated role prompt;
3. each harness requests the next action and returns typed results through the
   same narrow interface;
4. existing configuration, especially seats, panels, and `via`, drives all
   three harnesses;
5. Claude Code, OpenHands, and Codex can each complete all four workflows, while
   shipped Cursor behavior retains its regression coverage;
6. native versus external execution and automatic versus explicit resume are
   honest capability differences, not separate workflow implementations;
7. routing precedence, frozen targets, strict-majority quorum, failure
   isolation, fresh reviewers, executor-only continuation, workspace guards,
   and loop state remain covered by tests;
8. migrated Markdown commands, skills, hooks, and adapters are decision-free;
   and
9. the repository and product are Motley Crew.

At that point, another harness should require a thin adapter and host evidence,
not another copy of the workflows.
