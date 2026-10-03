# Plan: Phase 5 — engine-owned build using existing executor routes

Status: implementation plan. Baseline: Phase 4 `50aef3b`, published `9d75917` (Crew 0.84.0;
marketplace 0.44.0). Authority: `plugins/crew/docs/multi-harness-engine-roadmap.md`, Phase 5;
the orchestrator refreshed its stale handoff. Installed Crew 0.83.1's advisor-assisted compatibility
recipe prepared this plan; Codex production planning is unsupported and the orchestrator owns the live loop.

## Outcome and limits

Python owns executor selection, implementation/review cycles, prompts, retries, drift, human decisions,
and terminal outcomes. `/crew:build`, SessionStart and Stop drive issued protocol actions. Reuse the
dispatcher, continuation store, LoopState, loop-owned reviews and deterministic return transport.
Preserve native Claude `crew:executor` and existing external workspace-write routes; reviewers stay
fresh, continuation remains executor-only, and unavailable routes never cause model/provider fallback.

In scope: build workflow, necessary shared-seam extensions, behavioral tests,
command/hook cutover, concise protocol documentation and disposable real Claude Code CLI evidence.
Out: Cursor-native executor/lifecycle gate, other Phase 6+ work, OpenHands, rebrand, provider/catalog
redesign, new configuration tiers, global model/provider/config changes, automatic commits or pushes.
No migration/adoption, legacy detection or upgrade guidance, paid-review seeding, special restart API,
downgrade fixtures or compatibility project. Preserve existing generic safeguards without extending them.
Keep Python 3.11 compatibility and existing project dependencies; no SDK, database or workflow framework.

Phase 4 passed five usable approvals/quorum four; Claude quota failures were not approvals. Checks:
131 measure-twice, 201 review, 674 hooks, 2026 isolated multiagent. Claude Code 2.1.287 evidence applies
only to its recorded epochs. References: `plugins/crew/docs/{phase-4-measure-twice-plan,measure-twice-protocol,
phase-4-measure-twice-evidence}.md` and `.crew/reviews/01a0f9e3-b687-7e63-b9e5-d1e8a07774b5/run-7e9b6ec6c48d/`.

## Existing seams and necessary changes

Paths are relative to `plugins/crew/`; verify these baseline anchors at milestone 1 before extraction.

| Current implementation | Consequence for Phase 5 |
|---|---|
| `commands/build.md:92-229`, `:446-527` | Parent currently selects, retries, interprets envelopes, synthesizes/revises and finishes. Move this policy once. |
| `scripts/multiagent/cli.py`, `cmd_dispatch`, `cmd_build_executor`, `_git_head`, `_git_staged`, `_git_branch`, `_dispatch_guard_warnings` | Extract these execution/resolution/guard implementations; retain CLI parsing/rendering and unrelated commands. |
| `scripts/multiagent/continuations.py:92-138`, `:779` | Reuse exact binding, ordered classifier, tombstone and chain lock; do not create another conversation store. |
| `scripts/loop_state.py:117-175`, `:236` | Verdict policy is shared; initialization still hardcodes mt. Generalize only the two actual loop kinds. |
| `scripts/multiagent/review_workflow.py:4897-5239` | Binding parser, owner/retry guards, locks and preparation assume mt and a plan. Add bl and working-tree code targets. |
| `scripts/multiagent/measure_twice.py:155-198` | Existing journal demonstrates receipts, checkpointing and decisions; its planning/promotion machine is not a build template to copy. |
| `scripts/multiagent/workflow_transport.py:57-120`, `:222`; `claude_native_transport.py:42-79` | Reuse exact-byte capture and runtime seam; remove only mt-specific assumptions needed for build executor returns. |
| `scripts/models.py:294`, `:667-728` | Schema 4 knows mt_workflow, executor and resume toggle, but no durable build attempts. Reuse existing schema safeguards. |
| `scripts/session-start.py:431`, `persistent-mode.py:485-531`, `loop_projection.py` | Replace build recipes with the existing lightweight projection pattern, retaining hook-owned counters/bounds. |

Create one deep `multiagent/build_workflow.py` module for build decisions and one focused
`multiagent/execution.py` extraction for dispatch/executor resolution and workspace observations.
Its adapters are existing providers and native Claude work; callers know requests, issued work, decisions
and steps. Keep review/state authority in their current modules. Do not copy measure_twice's interview,
promotion or full implementation; share helpers only with actual mt/build callers, without a generic framework.

## Public interface and ownership

Use strict dataclasses and exact versioned JSON at CLI ingress, matching existing dependency conventions.
`BuildRequest(raw_arguments, session_id)` parses leading `--panel`, `--seats`, `--executor` pairs in
Python; reject duplicate/missing/unknown leading options and preserve remaining task bytes verbatim.
Reuse the existing leading-option grammar without adding a planning interview or shell evaluation.
`crew build -f <request> --session-id <literal> --consume` consumes only a successful request spill.

`BuildRef(session_segment, loop_instance_id)` identifies the lifetime. `BuildStep` uses existing
`needs_input | work_batch | waiting | terminal` values. Issued items carry owner/action identity,
frozen route, model/role/access, prompt/artifact paths and literal argv; review items carry full ReviewRef.
External items also issue effective provider timeout and host execution allowance (at least that timeout
plus 60 seconds for settlement); long commands run as owned background work, kept alive to actual completion.
Do not invent a numeric host ceiling: if an actually observed host limit cannot honor the issued allowance,
refuse before launch with its diagnostic; record any unverified host limit as unknown.
Expose `start_build`, `next_build`, `claim_build_action`, `execute_build_action`, `submit_build_action`,
`recover_build_action`, `decide_build`, `cancel_build`; CLI verbs are `build`, `build-resume`, `build-next`,
`build-claim`, `build-execute`, `build-submit`, `build-capture`, `build-recover`, `build-decide`, `build-cancel`.
`build-resume --session-id <literal>` only discovers that session's state and returns its projection/issued
next command; it accepts no task and never starts a new lifetime. All other owner verbs require issued
`--session-segment` and `--loop-instance-id`, plus action ID where applicable.
External execute claims internally; native claim authorizes one spawn. `build-capture` maps to shared
workflow_transport capture and submit, with no separate build policy entry point.
Native issued items also include `build-native-bind` and `build-native-capture`, mapped to typed
`bind_build_native` / `capture_build_native` wrappers over the existing Claude transport. Both require
issued owner/action flags plus `--handle` and `--output-file`; capture also requires
`--completion-observed`, attesting an actual host completion event. Reuse the demonstrated
measure-twice native flag/receipt contract and test all emitted argv through the real parser.
`build-submit` calls submit_build_action directly; build-capture is the generic host-Write fallback.
BuildResult fields are `ref: BuildRef`, `action_id: str`, `status: completed|blocked|invalid_report|failed|timeout|cancelled`,
`returned_sha256: SHA256`, `execution_receipt_sha256: SHA256`, `diagnostic: str|null`. The engine receipt
binds route, actual transport outcome and engine-observed guards; host text cannot override those facts.
Both native AND external prompts require the final nonblank LF-delimited report line to be exactly:
`CREW_BUILD_STATUS: COMPLETED` or `CREW_BUILD_STATUS: BLOCKED`. COMPLETED asserts no unresolved task blocker.
For comparison only, remove one trailing CR per LF-delimited line; no other trimming of the final line.
That terminal line is the sole status marker; earlier examples/quoted markers are ordinary report text.
An absent, indented, quoted, fenced (last line would be a fence), suffixed or unknown terminal token is
invalid_report. Python derives completed/blocked without changing retained bytes; U+2028 is not a line break.
Blocked/invalid_report parks before any paid review or automatic retry, even when provider `ok=true`.
Failed/timeout/cancelled transport overrides any marker. Ordinary dispatch/provider `ok` semantics stay unchanged.

Add optional `bl_workflow` version 1 to the existing LoopState and bump its outer schema 4 → 5.
Keep `phase=drafting|reviewing|done`, budgets, counters, executor and resume fields where they are.
BuildJournal contains request identity/raw selection, frozen executor, round policy and action/attempt,
review checkpoint/ref/generation, accepted/applied receipts, human question/answer/retry authorization,
outstanding writer owner/action/handle. Internal record keys are implementation
details: milestone 2 defines typed version-1 records and strict round-trip/validation tests from these
invariants; this plan does not pretend to specify every nested key. No duplicate budget or mt shape change.
Stage/phase mapping is implementation→drafting, review→reviewing, done→done. Recovery retains its prior
drafting/reviewing phase. Only verdict application enters done.
Prompts, immutable executor results and transport receipts live in a guarded lifetime/action namespace
under `.crew/reviews/<session>/build-<lifetime>/`; no parallel workflow-state file or mutable shared envelope.

Admission uses existing state discovery and generic future/corrupt-state safeguards.
Request matching hashes exact task bytes after leading-option parsing plus the explicit panel/seats
values and their presence (no trimming, no resolution to today's default). Omitted panel differs from
explicit default; changed seats or panel refuses even if the roster coincides. Executor flag is excluded
from matching after activation because the frozen stamp wins; raw original arguments remain provenance.

| Existing state | start_build result (resume is defined separately below) |
|---|---|
| Active mt in same session | Refuse active_request_conflict; never adopt mt or launch build. |
| Active bl_workflow, matching task/panel/seats identity | Continue same lifetime; stamped executor wins over a new flag, reported explicitly. |
| Active bl, conflicting task/panel/seats identity | Refuse active_request_conflict without changing bytes or bounds. |
| Inactive bl | Explicit build follows existing new-start conventions, subject to writer fences; owner-bound next/execute calls still replay terminal. |
| Outstanding writer in same session, active or inactive | Refuse any replacement/build/mt activation until confirmed recovery; never infer quiescence from age. |

Resume has no request to compare: it returns the discovered workflow's current/terminal projection or
missing-state if absent. Normal start/resume use existing conventions; no special restart or old-format path.

One project `.crew/loop-admission.lock` serializes new workflow starts and existing
init for BOTH bl/mt. Use existing discovered state paths, including unowned files, only to check conflicts.
Ordinary start/resume of an existing lifetime takes candidate state locks in sorted absolute-path order
under admission, and never takes its continuation lock: return the same lifetime's waiting/work/input
projection or active_request_conflict without touching the chain. Only admission that will invalidate or
replace a chain takes that continuation lock. First classify under state locks, release those locks,
then acquire continuation before reacquiring sorted state locks; re-read every admission condition
before committing. A busy chain yields a retryable waiting status with owner/fence guidance and no
mutation, not a generic error or provider timeout; release admission promptly, never wait for the provider.
The existing bounded chain-lock acquisition is distinct from dispatch execution timeout. Test start,
resume and conflicting requests while a chained provider is held open, with unchanged state bytes.
Existing initialize's target-only lock is insufficient; use locked internals without reacquiring held locks.
No provider probe runs here. Lock order is admission → optional
continuation → sorted state/owner → review (review and continuation are never nested together).
Never acquire admission/continuation while holding state/review locks; normal progress
uses its owner lock only. Keep the admission lock inode stable during cleanup, using the existing lock helper.

## Executor resolution, retry and continuation contract

Extract `cmd_build_executor` resolution into a typed function shared by old CLI and new workflow.
Preserve precedence: valid matching active stamp > explicit flag > repo/global `[build].executor` >
`crew:executor`. Preserve the CLI's exact five keys, null channel off Claude and existing refusals.
The workflow additionally refuses a native sentinel on a host without its actual Claude executor role;
it never treats external read-only Claude as a replacement. Existing explicit external routes remain usable.

Freeze the chosen executor identity for the lifetime: seat/sentinel, host route/channel, provider,
requested model and reasoning settings, native role/inherit policy and resume toggle. Native inherit
remains inherit with honest requested-only attribution; do not invent a resolved model observation.
Before activation, an invalid/missing route refuses without state creation. For an active workflow lifetime,
catalog removal or incompatible route/model/capability change parks on route_unavailable with no claim/run;
after restoring the exact route, its bound retry_route decision revalidates it. Never substitute a route.
Resolve retry count, dispatch timeout/floor and validated provider write options at each new implementation
round, then freeze them across that round's attempts/resume; do not reread mid-retry configuration.

Keep `[build].executor_retries` in 0..2, default 0, for external execution only: one initial attempt plus
that many retries. Retain the same immutable prompt for those retries and stack on partial unstaged edits.
Pre-run misuse produces no result and no automatic retry. An actually terminated, guard-clean external
failed/timeout result, including the existing unavailable envelope, consumes that bounded retry policy.
Unknown termination requires recovery, not automatic retry; blocked/invalid_report follows the contract above.
Any fired workspace guard or incomplete observation takes priority over ok/failure and parks before review.
When exhausted, surface status/diff/untracked files and retained diagnostics without staging or cleanup.
Native executor failure also parks; do not invent automatic native retry configuration.

Reuse the existing `build-executor` continuation chain and classifier in continuations.py. Exact binding
still includes lifetime, seat/provider/model, canonical workspace, HEAD, branch and index tree. Resume is
default-on for supported codex/cursor executors; opt-out omits chain use, unsupported routes run fresh
and say so. Preserve unavailable-provider KEEP, pre-launch tombstone, timeout/guard invalidation,
resume failure and store-failure reporting. New lifetime invalidates old conversation only after admission.
Reviewers, formatters and synthesis never consume this chain, including when a reviewer shares the seat name.

## Durable write execution and recovery

The extraction retains ordinary dispatch's output/exit contracts and existing provider options. Build calls
the importable implementation with an issued action identity and unique result destination; no argparse
Namespace emulation, stdout parsing or copying the session's last dispatch envelope into a new attempt.
Pin build execution and review to `crew_base()`'s canonical workspace; refuse a conflicting working-directory
override before work. Generic dispatch keeps its documented cwd semantics. Share the existing HEAD/index/
branch observation helpers, including unborn/detached cases; absent pre/post facts cannot certify build safety.
Native guards are an intentional behavior change: native commit/stage/branch changes now park without
waiver. Document and exercise this in the real gate. Serialization is the durable ready→claimed compare-
and-set under the owner lock for EVERY route, including native and resume-off; no new action lock is needed.

| Step / verb | Lock lifetime and durable effects |
|---|---|
| next / prepare | Owner lock: validate lifetime/bounds/pause, issue immutable prompt and ready action; release. Probe capabilities/availability outside all locks. |
| execute (external) | Acquire continuation lock only if enabled; then owner lock, revalidate, observe pre-run facts, commit claimed + outstanding_writer. A prior claim returns waiting and never runs. Release owner; keep continuation lock if acquired. |
| claim (native) | Owner lock only: same validation/pre-run snapshot and durable claim/writer fence; release, return spawn authorization once. Duplicate calls return waiting. |
| launch | External provider executes with only continuation lock when enabled, otherwise no lock. Native host uses issued authorization and binds actual handle under a short owner transaction; no owner lock spans host launch/wait. |
| capture / settle | After actual completion, observe post-run facts. External retains continuation lock, acquires owner, revalidates, settles chain, retains immutable result/guards/return receipt and commits acceptance/status/next-stage together; release owner then continuation. Native capture uses owner only and the same receipt/acceptance rules. |
| receipt replay | Owner lock verifies identical retained digest and reconciles once; never respawns, increments retry twice or overwrites conflicting bytes. |

Duplicate external execute returns waiting on bounded continuation-lock contention with the same action
reference; it is not a provider timeout/failure and spends no retry. Native/resume-off duplicate claims also wait.
Cancellation winning before claim prevents launch. After claim, a host launch/cancel race is possible:
the writer fence remains until actual quiescence, owned-handle cancellation is attempted and late acceptance
cannot advance the loop. No timeout/file-size inference closes that gap. State/review locks never span
provider work, and review reconciliation starts only after continuation is released. Recheck workspace facts
before review preparation and completion. Continuation-store failure retains its existing invalidation/reporting;
a crash before a durable result is uncertain even if the chain file happened to be written.

Filesystem capture, continuation storage and LoopState are recoverable steps, not a cross-file transaction.
A crash after claim and before durable completion is an uncertain write even if no launch handle survived.
`next` returns waiting/recovery, never redispatches it. Recovery requires actual `not_running` confirmation;
then show current tracked/untracked edits and guards. A bound human decision may adopt completed edits for
fresh review, retry the same route with explicitly acknowledged stacking, or cancel. Record that decision
and workspace digest before the new action; stale decisions cannot authorize changed files or later attempts.
Adoption needs a known guard baseline and matching HEAD/index/branch; otherwise stop for workspace repair.
No automatic rollback, clean, stage, commit, branch switch or inference that missing output means no work.

Cancellation first makes the lifetime inactive, invalidates new claims/review authority and records reason;
request cancellation only for owned handles, with honest unavailable/failed-cancellation diagnostics.
Late returns may be retained for audit but cannot advance inactive/replaced state. Since a late executor
can still edit the workspace, retain an outstanding-writer marker in that same journal: new build/mt
admission for that session refuses replacement until actual termination/quiescence
is recorded, even after cancellation or safety exit. Recovery may clear this marker on an inactive owner
without reactivating it. Candidate SessionStart cleanup must retain this state AND its required receipts,
prompts, handles, review/continuation evidence and sibling lock regardless of active flag or age: neither the
inactive one-day nor active seven-day sweep may erase the fence. Recheck protection under the state lock
before deletion; artifact_prune/swab must honor the same protected references even with --yes. Retain and
diagnose malformed/unreadable writer journals rather than treating uncertainty as permission to delete.
After confirmed quiescence, normal cleanup policy applies. Other processes remain outside mechanical isolation.
This fence governs a session's issued work and lifetime replacement, not project-wide write isolation.
The observed Claude CLI --resume gate retained its ID; interactive/new-session resume may produce another
ID and is separate ownership. In that case show the old owner's recovery/cancel argv, never adopt it; add
one cross-session recovery test and clarify README evidence limits. Concurrent processes can still edit
the same workspace, so docs and evidence must not
claim their content is isolated by HEAD/index/branch guards. Independent builds need separate workspaces;
adding a project-wide writer scheduler is outside this phase's preservation of current workspace guards.
Every fence diagnostic (start, status, SessionStart and swab) prints the exact issued owner/action recovery argv:
`crew build-recover --session-segment <issued> --loop-instance-id <issued> --action-id <issued> --confirmation not_running`.
Frame that argv as requiring explicit operator confirmation of actual quiescence, never an automatic action.

## Review composition and decisions

Extend `LoopReviewBinding.loop` to exactly mt|bl, preserving existing mt JSON/identities. Route owner lookup,
retry authorization, pending checkpoints, prune protection and lock acquisition through the loop discriminator.
Extract the minimal shared owner/retry facts from current mt-only guards; validate each journal through its
own typed parser. No callback acquires a loop lock while holding a review lock. Order is owner → review;
no code path nests review and continuation locks. No provider/probe/native wait occurs inside either lock.

`prepare_loop_review` accepts promoted plan for mt and explicit `working-tree` code target for bl. Reuse
targets.resolve and snapshot_name for tracked, staged and untracked content, notes, descriptor and replay
spec/base. Freeze executor summary as supplementary, hash-bound context visible to every reviewer; code
snapshot remains the reviewed target. REJECT on bl requests structural reimplementation, not plan generation.
Retain current panel/seats precedence and absent-option semantics. Freeze roster, pins, route policy and
timeouts per review generation; reconnection never re-resolves them. Preserve current build review routes,
including external Cursor review seats: composing the seam must not silently opt build into Cursor-native
review or new force-external configuration semantics. Standalone review/debate and mt keep their policies.
Build reviews retain the uncapped existing `crew run` timeout: `[tuning].timeout` (repo > global > built-in
600 seconds), raised by each provider's effective floor. Freeze base/effective values per generation and
carry the effective timeout plus settlement allowance in background execute items. Derive this policy only
from verified loop_binding.loop=bl; mt and standalone retain their 540-second ceiling. Update shared prep,
foundation/action validation, retry/successor copying and execution/probe drift checks together: none may
cap/refuse a valid >540 build timeout or reread configuration on resume. Changed provider floor parks on
review_timeout_changed before launch for explicit fresh_review, never silently caps or extends the attempt.

Checkpoint complete prepared inputs before writing run files; reopen the same generation after a crash.
Bind only verified prepared artifacts, then permit paid work. Use shared review-execute/claim/capture/
submit/recover/retry; preserve accepted reviewers, formatter repair, full raw output and parallel fan-out.
After each operation call build-next. Do not read/write standalone or legacy current-run pointers to advance
a workflow-owned build, or certify from a summary/header. Use verified LoopReviewSource accepted-outcome evidence.

Apply outcome + receipt + counters in one LoopState transaction. Replays before/after application do not
repeat work. Preserve APPROVED and minor-only REVISE completion, blocking REVISE/REJECT revision counts,
strict majority, FAILED/usable contradiction, first all-failed fresh review and second terminal review_failed.
Synthesis failure parks for synthesis-only retry with successful reviewers intact; it is never FAILED.
Implementation/revision prompts live in Python, reference immutable task, prior summary and full findings,
require structural-cause diagnosis, preserve blockers and forbid commit/stage/branch changes. Native initial
work retains TodoWrite guidance; external prompts omit it. Never silently defer blockers to finish.

Completion advisories record nothing and park with exact owner/question/outcome/observed-target identity.
Explicit human `force --confirmation force` can waive only the disclosed shared verdict advisories and
records last_verdict_overrides. Revalidate all conditions/target bytes; changed observations require a new
question. Force cannot waive executor workspace guards, ownership/identity corruption, expired bounds or
uncertain running work. `retry_review` keeps accepted seats for unchanged targets, but drift requires a new
generation over current bytes; synthesis-only retry preserves the entire panel. Retry authorization is durable
and attempt-bound as in Phase 4. Human waits set awaiting_input before returning; cancellation remains allowed
after bounds. `phase=done` only finalizes, with honest forced/clean completion and no new paid action.

Terminated executor outcomes map once: blocked→executor_blocked; invalid_report→executor_report_recovery;
native failed/timeout/cancelled and exhausted external failed/timeout→execution_recovery. An externally
cancelled action with an active owner also issues execution_recovery. Known termination needs no additional
not_running attestation; unknown termination stays waiting until build-recover. Fired guards take priority
and issue workspace_guard. Completed plus valid guards alone advances to review.

`BuildDecision` has exactly `ref`, `question_id: str`, `kind: str` and nullable text `confirmation`,
`workspace_sha256`, `completed_action`, `answer`; omitted fields decode
to null. `build-decide` requires issued owner flags plus `--question-id` and `--kind`; extra/inapplicable
fields refuse. Questions retain action/review identity, proposed verdict/advisories and observed workspace
digest (canonical root + HEAD/branch/index facts + tracked/untracked target bytes) so decisions cannot drift.

| Issued question | Allowed kind and additional required flags |
|---|---|
| executor_blocked / executor_report_recovery / execution_recovery | `answer_executor --answer-file <spill> --confirmation stack_edits --workspace-sha256 <issued>`; `retry_executor --confirmation stack_edits --workspace-sha256 <issued>`; `adopt_edits --confirmation completed --completed-action <issued implementation or revision> --workspace-sha256 <issued>`; `cancel` |
| review_timeout_changed | `fresh_review --confirmation fresh_review --workspace-sha256 <issued>`; `cancel` |
| completion_advisory | `force --confirmation force`; `retry_review`; `cancel` |
| synthesis_retry | `retry_synthesis`; `cancel` |
| route_unavailable / workspace_guard / feedback_recovery | Respectively `retry_route` / `recheck_workspace` / `retry_feedback`; or `cancel` |

`build-recover` additionally requires `--action-id <issued> --confirmation not_running`: on an active
owner it records quiescence and issues execution_recovery; on an inactive owner it only clears the writer
fence and returns terminal. Adoption's completed-action must match the issued pending action; it cannot
repair missing guard history. Recheck_workspace only observes whether the operator restored the issued
HEAD/index/branch baseline; it never changes files, staging or branch. Unrestored facts remain workspace_guard.
Once restored, apply the retained original transport/domain outcome through its normal mapping exactly once:
completed advances to review; other outcomes use their retry/recovery policy. It never waives unknown guards.
Retry_executor creates a new explicitly authorized round with bounded attempts on the
same frozen route; it does not reset loop bounds. Retry_route preserves the prior round's remaining budget.
After accepting an outcome/receipt, missing, unreadable or invalid-UTF-8 retained revision/synthesis/panel
context issues feedback_recovery before another executor action. Retry_feedback rereads only that context;
it neither reapplies the verdict/counters nor purchases another panel. Each decision commits/replays once; changed observations issue
a new question. All listed retries require active, within-bound ownership; cancel and quiescence remain allowed.
Answer_executor reads nonblank UTF-8 from the spill into decision.answer, freezes bytes/digest with the
owner/question receipt, and creates a new round prompt referencing original task, prior report and this
supplementary answer. It leaves original request identity intact; answers survive process re-entry/replay. Invalid-report
retry also adds an engine-authored format correction. Retry_executor intentionally repeats work after an
operator has resolved the condition without new prose. Both create a fresh bounded round; only automatic
attempts inside one round reuse identical prompt bytes. Changed answers conflict with an accepted decision.

## Transport and lifecycle cutover

`models.SCHEMA_VERSION = 5`: candidate readers load schemas 1..5. Centralize the version stamp in
models' existing state-write policy (`update_state_json`); successful candidate mutations and whole-state
serialization use that constant. Reading never upgrades. Preserve existing future-schema no-touch guards;
do not add compatibility machinery. Candidate commands/hooks run together only in isolated fixtures until
cutover, never against the installed live planning loop. Existing build behavior stays in place until the
command/hook switch. All workflow-owned verdicts and decisions then pass through the new build protocol.

Extend existing native capture/receipts to issued `crew:executor`, retaining exact 2.1.287 validation and
host-Write fallback. Fix LF-only JSONL framing for legal Unicode separators at
`scripts/multiagent/claude_native_transport.py:145`. Executor guidance
accepts supplied canonical lifetime plans, excluding staging/transport files; unrelated Phase 4 minors defer.

Add `project_build` beside `project_measure` in `scripts/loop_projection.py`; hooks render argv/status only. Preserve
output shapes, counters, bounds, waits and ownership. Cut build/cancel commands and both hook branches
over together after protocol tests; update directly affected protocol/user/scripts/roadmap guidance only.
Document current bound build-decide force decisions; do not retain automatic force or a deprecated force shim.

## Green milestones and acceptance

1. **Characterize and extract execution.** Before cutover, run the existing recipe through the real Claude
   gate in disposable fixtures; retain native/external traces in `scripts/tests/fixtures/build_baseline_trace.json`
   with source epoch, pinned task/tree/scenario/panel/executor inputs, events/counts and nullable timing/usage.
   Use a harmless one-file implementation plus intentionally incomplete fixture requiring one real revision;
   retain exact fixture inputs for replay, and distinguish deterministic fault traces from live observations.
   Extract execution.py and route resolution without cutover. Public dispatch/build-executor tests retain keys,
   exits, options, cwd, guard warnings, continuation precedence and failure behavior; no model is called by resolve.
2. **Extend shared ownership.** Add schema 5/journal, bl binding/code snapshot and minimal owner/retry seams.
   Prove mt identities/behavior unchanged, code target fidelity, prepared-input replay, pointer independence,
   existing schema safeguards and protected pending artifacts.
   Concurrent bl/mt starts and duplicate same-session starts admit exactly one owner; workflow/initialization
   paths use the same admission lock. Pin build timeout >540 and a provider floor through prep/validation/
   retry/execution, while mt/standalone stay capped. Freeze internal typed records with their parser tests here.
3. **Implement build workflow.** Deliver typed interface, prompts, attempt receipts, native/external paths,
   decisions and recovery in `scripts/tests/test-build-workflow.py` (add to `scripts/CLAUDE.md` quick commands).
   Use public-interface tests with injected providers/runtime and temporary
   repos: two concurrent executes cause one launch; crash at every claim/capture/settle boundary never reruns;
   pre-run/cancel/pause/bound races admit no work; uncertain writes require confirmation; retries stack exactly
   0/1/2 times; route drift, guards and wrong receipts cannot reach review. Both native/external blocked, missing,
   quoted/indented/suffixed terminal markers park even with transport success; earlier marker examples are
   ordinary text. Verify exact terminal marker, Unicode/EOF and wrong-route refusal.
   Test the exact timeline for native, chained and resume-off execution, cancellation races and lock order.
   Blocked→bound answer→new prompt→completion must work without altering original request/bounds; replay
   adds no paid action and changed answers are refused. Exercise each terminated-status question mapping.
4. **Compose and cut over.** Exercise implementation → blocking review → revision → approval, all-failed twice,
   minor-only approval, synthesis retry, partial-panel retry, changed drift/force questions,
   cancelled writer fencing and done replay. Required cleanup/swab regressions retain active >7-day
   and cancelled >1-day outstanding writers, their locks/evidence and refusal to replace them; after confirmed
   recovery they become normally eligible. Cover malformed journals and a recovery/cleanup race. Test every
   decision-table row, stale/wrong owner/question/fields, and admission-table result through public CLI.
   Thin command/hook tests execute emitted argv through the real parser; reviewers stay fresh and successes
   survive retry. Replace obsolete recipe-string
   tests with these behavioral tests, retaining unrelated existing coverage. Publish build protocol documentation.
5. **Prove candidate and close.** Record candidate `scripts/tests/fixtures/build_candidate_trace.json` using
   milestone 1's retained inputs; compare paired deltas. Run new build tests, then required measure-twice,
   review-workflow, hooks and multiagent suites once source settles, using existing crew_config isolation.
   Check Python 3.11 grammar and whitespace; repeat affected checks only after relevant fixes. Run the real
   disposable Claude gate below and publish exact source/version manifests, traces, limitations and counts.

Milestones depend in order; shared regressions, uncertain writes, lock inversion and version skew must pass
their checks before cutover. Missing configured routes/credentials block the gate without route substitution.

## Real Claude gate and measurable orchestration cost

Use `scripts/tests/claude_cli_gate.py` in disposable repositories/sessions with candidate --plugin-dir and
real hooks: no host override, permission bypass, global config changes or live root-loop mutation. Record
CLI version/plugin path and before/after source hashes per process; unsupported direct capture uses fallback.
Exercise native crew:executor AND current configured `sol` via Codex, copying that resolved route/model into
the fixture's repo config or explicit --executor sol and recording its source. No global edits or substitution;
if unavailable, the gate blocks. Preserve both baseline/candidate pins and isolate native sentinel selection.
External execution must demonstrate actual exact-ID continuation across revision; unsupported resume is not
proof. Cover opt-out/failure deterministically. Retain returns, tracked/untracked diffs, identities, handles,
continuation records, accepted reviews and guards. Real reviews must complete a harmless fixture and reject
deliberately incomplete work before revision; label injected faults as tests.

Observe reviewer overlap/fresh conversations, Stop block → automatic next argv → same-lifetime progress,
human pause, actual session re-entry, interrupted-write recovery, cancellation, partial-panel recovery and
done-before-deactivate. Manual command repetition/fake hooks are not lifecycle evidence. Disposable HEAD/
index/branch violations must prevent review. Preserve evidence of completion classification on both routes.

Compare traces on identical task/tree/executor/panel/pins/rounds: parent calls/turns, paid role launches,
provider intervals/overlap, orchestration time and reported token/cache/cost fields. Separate ordinary work
from faults/audit operations; deduplicate tool IDs, never sum cumulative snapshots, label live versus simulated
evidence and unknown fields null. Require zero paid terminal replay, only authorized retry work, zero
bookkeeping scribes with direct capture, concurrent review and no new model calls for selection/hash/envelopes.
Ordinary-path timing/call-count deltas are informational; explain increases without invented savings or host
claims. The zero paid replay, zero bookkeeping and zero extra selection/hash/envelope calls above are hard gates.
