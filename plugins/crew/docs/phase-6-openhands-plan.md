# Phase 6: OpenHands native agent adapter

Status: draft plan, 2026-10-03. Scheduled after the Codex adapter and its parent-owned
gates. No OpenHands implementation or live gate has run.
Baseline: Phase 5 `179b815`, published as `6b16ba3` on
`codex/multi-harness-engine` (Crew 0.85.0; marketplace 0.45.0). This branch has
not been merged into `main` or installed. Build on that engine, not older main.

## Outcome and boundary

Run review, council/debate, measure-twice, and build through Crew's existing
Python engine using native OpenHands agents for every requested role. Each agent
can have its configured model provider; host identity does not select the model.
OpenHands owns agent execution. Crew owns prompts, action ordering, panels,
quorum, budgets, retries, human decisions, and workflow completion.

The initial surface is a local, code-driven Software Agent SDK adapter. A thin
OpenHands tool or command may invoke it and display results. A parent model does
not dispatch individual seats or copy their outputs. Remote Agent Server and
product UI integration can follow only when needed; neither is an initial gate.
Wrapping provider CLIs through ACP does not satisfy the native-agent outcome.

Preserve current seats, panels, explicit routes, external providers, and shipped
Claude/Cursor behavior. Add only the explicit runtime selection needed to pair
an OpenHands agent with a model. Do not reinterpret existing `via` values, infer
native routing from a provider name, silently substitute models, or change global
defaults. A CLI-only seat cannot become an SDK seat until a supported model and
authentication route are explicitly configured. Prove the mapping before edits.

Migration, legacy formats, upgrade guidance, special restart machinery, a general
runtime registry, remaining Cursor expansion, and rebranding are out of scope.

## Evidence informing the design

Primary sources checked 2026-10-03; pin and recheck the chosen release at implementation:

- The [SDK](https://github.com/OpenHands/software-agent-sdk) exposes agents and
  conversations for programmatic execution. Its current
  [package metadata](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/pyproject.toml)
  requires Python 3.12+. Keep SDK dependencies in an optional adapter environment;
  Crew's Python 3.11+ stdlib core must still import and run without them.
- [Task Tool Set](https://docs.openhands.dev/sdk/guides/task-tool-set) documents
  sequential blocking delegation. It is not evidence of concurrent panels. Run
  engine-issued independent conversations concurrently in adapter code, using
  the SDK's [async execution surface](https://docs.openhands.dev/sdk/guides/convo-async)
  as appropriate to the pinned version, and verify actual overlap.
- [Persistence](https://docs.openhands.dev/sdk/guides/convo-persistence) restores
  conversations by ID and persistence directory. Bind those identifiers to
  existing Crew action receipts; SDK persistence is not a transaction with
  Crew's workflow state.
- [Pause/resume](https://docs.openhands.dev/sdk/guides/convo-pause-and-resume)
  provides execution control, while
  [confirmation policies](https://docs.openhands.dev/sdk/guides/security) control
  approvals. Neither alone proves read-only filesystem access or that cancelled
  tools and their descendants have stopped writing. Those require adapter gates.

## Implementation sequence

1. **Prove the adapter seam.** Pin the SDK version and inspect the current native
   action/result and channel interfaces. Exercise two explicit model routes,
   direct final-result capture, concurrent conversations, access restrictions,
   and cancellation in a disposable workspace. Select the smallest routing
   extension and record its resolved host/runtime/provider/model identities.
   If SDK model access is unavailable, record the missing prerequisite; do not
   substitute an installed CLI and report a native pass.
2. **Ship review and council/debate.** Consume existing engine-issued actions,
   bind each conversation to its action and frozen target, and return exact raw
   reports plus typed status/identity through existing validation. Use a fresh
   conversation for each new reviewer action. The adapter persists results
   directly, with no scribe. Prove read-only tools or workspace enforcement,
   independent failure handling, concurrent seats, and unchanged quorum policy.
3. **Ship measure-twice.** Add the native advisor using engine-rendered prompts.
   Preserve existing action replay, budgets, human-question binding, and explicit
   resume. Reconcile a completed conversation with its pending Crew receipt
   without launching duplicate work. Use existing uncertainty handling when
   settlement cannot be established; do not invent a restart service.
4. **Ship build.** Add the native executor with workspace-limited write access.
   Continue only the engine-authorized executor conversation on revision;
   reviewers remain fresh. Gate branch/index/target drift, exact report capture,
   failure, interruption, and cancellation before accepting a result. Cancellation
   must settle owned tools and writers before review or replacement execution.

Keep these as small implementation slices using the same adapter. Resolve the
runtime/routing seam once; do not repeat whole-roadmap reviews for each role.
The engine owns the outer build loop; OpenHands' agent loop performs one issued
role action. No second goal-completion loop decides whether Crew should finish.

## Verification and completion

Use deterministic adapter tests for wrong or duplicate action IDs, late results,
malformed and missing reports, denied access, provider errors, human waits,
interrupted result settlement, executor-only continuation, and cancel/write
races. Reuse existing engine policy tests rather than copy their implementation.
Run affected engine, host, and provider regression suites with isolated config.

Run one bounded disposable end-to-end gate covering a concurrent native
multi-model panel, debate, a plan revision, and an implementation/review/revision
build. Record source and SDK versions, resolved routes, conversation/action IDs,
enforced access, raw-result evidence, cancellation, and explicit resume. Keep
secret-bearing SDK persistence private; export only necessary sanitized evidence.
Each claimed capability must be exercised; unrun UI, remote, and automatic-resume
surfaces remain unclaimed. Repeat a live gate only when a relevant change or
failure invalidates its evidence.

Measure orchestration calls, bookkeeping model calls (expected zero), reviewer
overlap, wall time, model time, and token/cost metrics where available. Compare
only runs with equivalent targets, panels, and models; a different runtime or
model mix is descriptive evidence, not a speedup claim. Completion means all four
workflows work through one engine with native OpenHands roles and no regression
to shipped routes. Codex is the preceding roadmap increment, per the operator's
updated sequencing; phase numbers are retained.
