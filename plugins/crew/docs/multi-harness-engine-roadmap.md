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
build workflows inside Claude Code, Cursor, and Codex. Python owns the workflow;
each harness is a thin adapter that asks what to do next, performs the requested
host operation, and returns the result.

At completion:

- workflow decisions, prompts, routing, persistence, validation, retries, and
  terminal outcomes have one Python source of truth;
- Claude Code, Cursor, and Codex drive the same workflow interface rather than
  carrying separate copies of each workflow;
- existing seat and panel configuration works across harnesses through the
  existing `via` channel model;
- native host subagents are used where supported, while existing external CLI
  providers remain first-class execution routes;
- Claude behavior remains working throughout the migration;
- Cursor gains first-class in-app subagents as the first new host target;
- Codex receives a first-class adapter after the workflow design has been
  proven in Claude and Cursor;
- review seats remain fresh, executor continuation remains executor-only, and
  current frozen-target, quorum, failure-isolation, route, and loop-state
  invariants remain enforced; and
- the existing repository becomes Motley Crew rather than being forked into a
  second product.

The generic user-facing identifiers `crew`, `/crew`, and `.crew` do not need to
change merely for branding. Repository and product naming are separate from the
workflow migration and must not delay the Cursor milestone.

This roadmap covers the four core workflows and the support paths they call.
Other Task-oriented commands such as `analyze`, `code-search`, `execute`, and
`deepinit` remain documented-unsupported in Cursor unless a later plan brings
them into scope; their current silent substitution is not legitimized by this
migration.

## Grounded starting point

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
- `scripts/multiagent/rounds.py` owns debate round storage;
- `scripts/multiagent/continuations.py` and the dispatch implementation in
  `scripts/multiagent/cli.py` own guarded external executor continuation and
  workspace checks; and
- `models.py` plus `crew-state.py` own the current build and measure-twice loop
  state and guarded transitions.

The primary remaining problem is ownership. Command Markdown and hook recipes
still decide which operation happens next, render some role prompts, invoke
native Claude Tasks, perform workflow-specific retries and repair, synthesize
outcomes, and branch on verdicts. That logic is duplicated across review,
debate, measure-twice, and build and cannot be reused faithfully by Cursor or
Codex.

The migration therefore deepens the existing Python modules behind one small
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

### Routing and `via`

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
panel configuration remains meaningful in Claude and, later, Codex.

The current one-channel-per-seat rule is sufficient for the first-class Cursor
workflow. Ordered multi-channel fallback or a separate force-external setting
is deferred until a concrete use case requires it.

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
| Cursor agent surface | Populate with real thin Cursor-native roles |
| Codex skills and native roles | Add last against the proven interface |

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

Every numbered phase is separately planned, reviewed, implemented, verified,
and merged before the next phase begins.

For every phase:

1. Start from current `main` on a dedicated branch.
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
11. Merge before producing the implementation plan for the next phase.
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

- Begin with an installed Cursor capability probe covering custom agent
  discovery, explicit role invocation, foreground result return, cancellation,
  read-only enforcement, requested-model handling, and observable route/model
  provenance. Cursor documentation is design input; the current installed app
  is the admission authority.
- If the installed app cannot support a truthful native seat, stop the phase
  and reassess rather than substituting `cursor-agent`, silently changing the
  model, or claiming the priority plateau at a lower tier.
- Add thin Cursor-native reviewer, scribe, and formatter role adapters.
- Implement Cursor's native request, await/cancel, and result-return mechanics.
- Make reliable Cursor host detection part of the adapter slice; do not depend
  on an operator `CREW_HOST` value that Cursor may scrub from agent shells.
- Make the Cursor host resolve the Cursor channel natively while leaving
  non-native channels, including Claude, on their existing external providers.
- Replace Cursor's current silent substitution of Claude Task roles with
  explicit supported routing.
- Drive the same Python review workflow and deterministic fixture used by
  Claude.
- Validate the installed plugin using Cursor's actual marketplace/cache refresh
  path.

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

Claude and Cursor complete deterministic and bounded live council/debate
scenarios through the same adapters, including fresh-seat and partial-failure
cases. No debate or council phase tree remains in Markdown.

### Phase 4 — Measure-twice and lifecycle driving

#### Outcome

Measure-twice becomes a Python-owned workflow that composes review and can be
driven or resumed in both Claude and Cursor.

#### Scope

- Move advisor instructions, planning prompts, requirements decisions, budgets,
  deadlines, cancellation, verdict handling, and revision transitions into
  Python.
- Add the read-only Cursor-native advisor in this phase so Cursor can perform
  the planning action needed by the phase's own exit gate. It uses the proven
  Phase 2 native-work seam with a Python-rendered advisor prompt.
- Reuse the current `LoopState` and guarded `crew-state.py` transitions rather
  than creating a parallel persistence system.
- Compose the engine-owned review workflow.
- Reduce the measure-twice command to a protocol driver.
- Migrate only the measure-twice branch of SessionStart and Stop to lifecycle
  translation plus a request for the engine's next action. The existing build
  branch remains intact until Phase 5 migrates it; shared-hook tests cover both
  branches and their current precedence during the transition.
- Preserve Claude's existing automatic continuation behavior.
- Make explicit resume complete and supported in Cursor; improve automatic
  Cursor continuation only if live hook behavior proves it.

#### Exit gate

A harmless plan-only loop can start, pause, resume, revise, and complete in
Claude and Cursor with the same Python decisions. Claude automatic continuation
still works, and Cursor has an honest explicit-resume path even if its Stop hook
cannot reliably force another turn.

### Phase 5 — Engine-owned build using existing executor routes

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
- Allow Cursor to complete build using the existing external Codex CLI executor
  before Cursor-native write execution is added. `cursor-agent` is not an
  acceptable substitute, and the read-only Claude provider is not presented as
  a write route. If the configured write-capable executor is unavailable on the
  current machine, the phase gate blocks rather than silently changing routes.
- Reduce the build command and remaining build hook recipe to protocol driving.

#### Exit gate

A disposable build completes in Claude and Cursor, route identity and workspace
guards remain enforced, executor continuation works where configured, reviewers
remain fresh, and no host driver owns build policy. Explicit resume is the
complete Cursor baseline; automatic continuation is required only when the
current installed Cursor hooks have proven actual re-entry.

### Phase 6 — Cursor-native executor and proven lifecycle improvements

#### Outcome

Cursor gains a native executor without changing workflow semantics or weakening
access controls.

#### Scope

- Add the Cursor-native executor adapter using Python-rendered prompts. The
  read-only Cursor advisor already landed with the workflow that first needs it
  in Phase 4.
- Admit native writes only after a disposable-workspace probe proves the
  requested access, role scope, cancellation, and result behavior.
- Route the new Cursor-native build work through the same `via` and host
  resolver seam already used by review and the Phase 4 advisor.
- Improve Cursor automatic lifecycle continuation only when an observed hook
  event actually re-enters the engine; otherwise keep explicit resume as the
  truthful supported path.
- Preserve all existing Claude behavior.

#### Exit gate

Cursor completes a disposable native-write build through the shared engine, and
the Phase 4 native-advisor regression remains green. If automatic continuation
is advertised, a live probe demonstrates actual re-entry; otherwise explicit
resume remains the documented behavior.

### Phase 7 — First-class Codex adapter

#### Outcome

Codex becomes the third first-class workflow harness after the interface has
been proven by Claude and Cursor.

#### Scope

- Add thin Codex skill, native-role, result-return, cancellation, and lifecycle
  adapters.
- Reuse the same Python workflows, role prompts, `via` configuration, and
  deterministic fixtures.
- Support explicit resume everywhere and automatic resume only where the Codex
  app's real lifecycle mechanics prove it.
- Enable native read and write routes only at their verified access tiers;
  existing external providers remain available without nested CLI substitution.
- Default to independently planned and merged read-only, persistent-loop, and
  native-write Codex slices. Each slice has its own installed gate, preserves
  Claude and Cursor, and leaves one production workflow owner.

#### Exit gate

Codex completes review, council/debate, measure-twice, and build through the
shared engine at its honestly verified native/external and lifecycle tiers. The
Claude and Cursor regression gates remain green.

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
- Refresh and validate the current installed copy in Claude, Cursor, and Codex.
- Document the execution and lifecycle behavior actually demonstrated in each
  harness.
- Publish the release notes.

The phase is two independently reviewed and merged slices: **8A cleanup** proves
that every removed path is unused, then **8B rebrand/release** renames and
publishes the already-clean product. Naming cannot block or disguise the
single-implementation cleanup gate.

#### Exit gate

All four workflows use the same Python engine in all three harnesses; no
superseded workflow implementation remains; the installed artifacts match the
validated source; and the repository is presented as Motley Crew.

## Supported plateaus

The roadmap intentionally provides useful stopping points:

1. **After Phase 2:** Cursor can run mixed native Cursor and Claude CLI reviews.
2. **After Phase 4:** Claude and Cursor share review, debate, and a portable
   planning loop.
3. **After Phase 5:** Claude and Cursor share all four workflows, with Cursor
   using explicit resume and the external Codex CLI executor where native write
   support has not landed.
4. **After Phase 6:** Cursor has the complete native workflow experience,
   subject only to honestly observed lifecycle limitations.
5. **After Phase 7:** Claude, Cursor, and Codex share all core workflows.

These are completed increments, not partially installed future designs.

## Overall completion criteria

The roadmap is complete when:

1. review, council/debate, measure-twice, and build are Python-owned workflows;
2. Python renders every canonical workflow and migrated role prompt;
3. each harness requests the next action and returns typed results through the
   same narrow interface;
4. existing configuration, especially seats, panels, and `via`, drives all
   three harnesses;
5. Claude Code, Cursor, and Codex can each complete all four workflows;
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
