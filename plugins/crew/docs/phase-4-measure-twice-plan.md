# Plan: Phase 4 — engine-owned measure-twice

Status: proposed implementation plan; no Phase 4 source changes are delivered by this document. Requirements: the
user-approved scope and constraints below become the fresh-clone authority when the durable plan and linked tracked
documentation are published together. The durable destination is
`plugins/crew/docs/phase-4-measure-twice-plan.md`. The local planning brief is
`.crew/plans/finish-loop-migration-requirements.md` and is supporting evidence. Strategic authority:
`plugins/crew/docs/multi-harness-engine-roadmap.md`. Source baseline inspected: branch
`codex/multi-harness-engine`, `b39f647`.

## Outcome and scope

Move measure-twice's decisions and prompts into Python, composing the existing review workflow rather than
maintaining another panel implementation. Markdown must become a transport for issued actions. Keep one loop state,
guarded verdict policy, current budgets, existing seats/configuration precedence, and concurrent reviews. Prove the
interface can accept native results directly from code without adding OpenHands, a new provider catalog, or a
generic capability registry.

Deliver requirements handling, initial advisor planning, structural revision, replanning, review composition,
interruption/resume, human waits, termination, cancellation, and the measure-twice lifecycle guidance. Leave
build's production recipe, executor routes, continuation policy, and current-run pointer contract intact until
Phase 5. Phase 5's implementation specification follows this gate; the old chunk-c shim plans are historical
rationale, not execution instructions.

Claude is the installed-host exit gate. Cursor advisor/app lifecycle proof, Codex-native planning, and OpenHands
integration remain unimplemented here. Do not substitute a reviewer, parent model, or external provider for a
missing advisor route silently; report `unsupported_planning_host` before activation.
Use existing `channels.current_host()` (its rule is mirrored by the lightweight hook `host_detect.detect_host`);
Phase 4's production advisor route is explicitly Claude → native `crew:advisor` at the
resolved inherit model. Cursor and Codex planning routes are unsupported here even if an agent
file exists. Validate that issued role metadata is available before activation; no new host detector.

Ownership invariant: submission ownership alone cannot protect against a cancelled advisor's late filesystem
writes. Separate action staging from promoted plan authority, make start admission explicit, and require actual
Stop-triggered continuation evidence before declaring Phase 4 complete.

## Current code and why a simple wrapper is insufficient

Use these semantic anchors when implementing; do not rely on historical lines:
Source module paths below are relative to `plugins/crew/`; `.crew/` artifacts are repository-relative.

- `commands/measure-twice.md`, Phase 1/2/3 and Completing the Loop: parent-owned
  interview, advisor prompt, review-prep fan-out, scribe/formatter transport,
  verdict selection, state verbs, and exit decisions.
- `models.py`, `LoopState`, `AGENT_SETTABLE`, `update_state_json`, `state_lock`:
  shared state shape; only `plan_file` is agent-settable; all mutations serialize.
- `crew-state.py`, `cmd_begin_review`: reads legacy `current-run.json`, verifies
  run.json identity, freezes roster from hashed `seat_signatures`, and permits
  drafting/reviewing, clearing awaiting_input.
- `crew-state.py`, `cmd_record_verdict`: reads ONLY the frozen run's flat seat
  files, not a standalone ReviewStep; validates strict-majority usable quorum,
  legacy pointer agreement, target re-hash, phase, and FAILED/usable symmetry.
  Exit 3 records nothing; exit 2 is a hard contradiction. REVISE/REJECT advance
  revision_round; FAILED increments a separate consecutive-failure counter.
- `multiagent/review_workflow.py`, `ReviewRef`, `_start_run`, `_advance_locked`,
  `_derive_step`, `submit_review`, `retry_review`: workflow.json owns claimed and
  accepted actions, attempts, canonical panels and synthesis; review/debate have
  dedicated standalone pointers. Accepted native results are action artifacts,
  not necessarily the flat seat JSON that the old verdict gate counts.
- `_validate_submission_locked` and `_validate_workflow` (called by
  `_advance_locked`) both whitelist APPROVED/REVISE synthesis judgments. Use one
  kind-gated predicate in admission and advance validation: REJECT is permitted
  only for loop_review, requires minor_only=false, and appears in its authoritative
  synthesis prompt. Standalone review retains its existing vocabulary.
- `review_runs.py`, `mint_identity`, `verify_run_record`, `preserve_valid_write`:
  content/input identity, write-once manifests, and success-preserving results.
- `persistent-mode.py`, `_handle_loop`, `_force_exit`, measure-twice arm of main;
  `session-start.py`, `loop_next_step`, `build_session_status`: bound enforcement
  and re-entry guidance currently coexist with long mt review recipes.
- `tests/test-review-workflow.py`, `tests/review_workflow_fakes.py`,
  `tests/test-hooks.py`: typed protocol, recovery, pointer, locking, completion,
  budget and legacy-state regression evidence to preserve.

Calling `start_review` followed by `begin-review` would bind the wrong pointer and result representation. Moving
the old commands into a Python subprocess without an explicit composition seam would preserve two policy owners.

## Minimal ownership and interfaces

| Term | Meaning |
|---|---|
| Loop lifetime | One initialized budget and immutable loop_instance_id |
| Review generation | A new evaluation within that lifetime, independent of plan revision count |
| Attempt | Shared review recovery epoch; retries retain settled successes |
| Action ordinal | Monotonic mt action identity; a revision gets a new output path |
| Staging / sealed / canonical | Advisor-writable output / captured engine bytes / promoted review target |
| Receipt | Durable proof of an accepted action or applied outcome, used to replay without duplicate work |

### Importable loop state service

Extract the existing guarded operations from `crew-state.py` into `scripts/loop_state.py`; retain `crew state` as a
compatibility CLI delegating to that service. Do not import a hyphenated CLI dynamically from workflow code. Expose
initialize/resolve, await, bind-review, record-verdict and deactivate operations as typed functions over the
existing state path resolver and `update_state_json`. Return typed hard failures, combined advisories and recorded
results; translate them to existing CLI exit codes at the CLI boundary.

Extract the verdict predicate once. Accept verified `ReviewEvidence`: `run_id`, target hash/spec/base,
expected/usable seat labels as tuples[str, ...], `binding_status: BindingStatus` (MATCH|DIVERGED|MISSING), and
`source: LegacyReviewSource(run_id, manifest_identity_digest, observed_pointer_run_id: str|None)` or
`LoopReviewSource(binding: LoopReviewBinding, ref: ReviewRef, accepted_outcome_sha256)`. All digests are validated
full SHA-256 strings; the source is a tagged union, not a caller-supplied boolean or arbitrary dictionary. Two
producers supply it: the existing legacy manifest/flat-seat reader for build, and the shared review workflow's
verified accepted-action reader for migrated mt. An arbitrary caller's panel count, digest header or HostResult is
never a ReviewEvidence authority.

The shared predicate preserves hard phase/identity/FAILED contradictions and advisories `zero-usable`,
`quorum-not-met`, `pointer-divergence`, `target-drift`, `target-no-longer-resolves`. The old build producer checks
current-run.json; the migrated mt producer checks the explicit loop binding instead of that unrelated pointer. Same
policy, different verified evidence source.

### One LoopState, one workflow journal

Raise shared LoopState SCHEMA_VERSION from 3 to 4; add optional `mt_workflow` with nested version=1, round-tripped
by load/save and validated at mt entrypoints. Schema 1..3 still loads through current compatibility plus one-shot
mt migration; schema 4 prevents an older install from silently stripping the journal on save. Older
loaders/hooks/cleanup must refuse-to-touch it as future schema, including schema-4 build state saved by the newer
shared writer. Build's route, counter and review semantics do not change; test both sides of this downgrade
contract with a pinned schema-3 loader fixture and the new loader, including their write/cleanup paths.
The journal holds only mt progress: immutable input/requirements references and hashes, selection
options, detailed stage, monotonic action ordinal, review_generation, current bound ReviewRef, action
statuses/submission receipts, last applied outcome receipt and human question. Keep coarse
`phase=drafting|reviewing|done` for existing lifecycle semantics. Use existing `loop_instance_id` as the immutable
lifetime identifier; mt init already stamps it. No separate loop-workflow.json, no duplicate deadline or budget
counters in a review workflow, no widened AGENT_SETTABLE.

Drafting stages are `planning`, `revision`, `replanning`; requirements questions precede new activation.
Legacy requirements readiness follows the cutover predicate below, never the absence of a
requirements field that legacy LoopState did not persist. An unready adopted loop stays parked
in planning until a bound human answer, preserving its running budget. Reviewing holds a bound review; done has
only finalization left. Advisor work is fresh for each action, writes only the issued plan in its lifetime namespace,
and returns a completion artifact. Here "issued plan" means an action-scoped staging file, never the authoritative
promoted plan. It has no executor continuation. Reviewers and formatters are always fresh; build remains the sole
continuation consumer.

Add `multiagent/measure_twice.py` with typed entrypoints:

| Entry | Purpose |
|---|---|
| `start_measure_twice(request)` | Parse supplied input/options and resume an identical active request, or initialize after requirements are ready |
| `next_measure_twice(ref)` | Reconcile receipts and derive a work batch, wait, human question or terminal outcome |
| `claim_measure_action(ref, action_id)` | Once-only authorization for advisor/revision/replanning work |
| `submit_measure_action(request)` | Validate issued artifact/result, record receipt, advance under the loop lock |
| `recover_measure_action(request)` | Resolve a confirmed-lost claim; never assume elapsed time proves loss |
| `decide_measure_twice(ref, decision)` | Bound human answer, explicit retry/cancel/force authorization |
| `cancel_measure_twice(ref, reason)` | Existing guarded cancellation and late-result invalidation |

Start admission is explicit: an active migrated loop with identical immutable request identity resumes; a different
identity returns `active_request_conflict` without changing any bytes, budget, clock, binding or claims. Identity
includes raw task bytes, explicit panel/seats provenance and selected requirements source hash. An active loop
missing mt_workflow enters one-shot migration, never init. Initialize only after readiness and no active loop,
respecting existing terminal admission rules: force-exited state remains terminal without explicit human restart
authorization; start is not an init --force escape hatch. Identical terminal requests return their terminal status,
not a replacement lifetime.

Keep the existing deliberate operator restart surface: after explicit human authorization,
`crew state init mt --force -f <raw-task-spill> --auto-plan --consume --session-id <literal>`
may replace an inactive safety-exited state with a fresh lifetime. The spill contains the exact
new raw arguments; `--force` remains human intent, never an engine-generated recommendation to
execute automatically. `start_measure_twice` then adopts that fresh journal-less state through the
one-shot migration path, preserving its newly initialized bounds. `next`/start never issue or execute
this restart themselves. Existing init conflict checks still refuse any active loop. Test refusal
without authorization, the explicitly authorized CLI restart, fresh lifetime/bounds, and replay of
start without a second initialization. This retains the existing intentional reset contract rather
than inventing another restart service or treating resume as authorization.

Define strict `MeasureRef(session_segment, loop_instance_id)` plus action IDs scoped to that lifetime and monotonic
ordinal. Review actions additionally carry the full existing ReviewRef; attempts and hashes must never be
reconstructed. Use `review_runs.session_segment(session_id)` and existing ReviewRef validation for session
segments; never implement another sanitizer. Mirror ReviewStep's four response types rather than inventing an interactive subsystem. A batch may
contain shared review WorkItems as issued, together with the immutable mt owner binding; there is no parent
synthesis of next steps.

Define `MeasureStep(type, ref?, display?, question?, work_items=(), in_flight=(), outcome?)`.
Use the existing StepType values: needs_input (requirements or bound human question), work_batch
(advisor or review actions), waiting (claimed handles still in flight), terminal (guarded inactive
completion/cancellation). Ref is absent only before activation. Work items carry their MeasureRef
owner and full ReviewRef where applicable; JSON serialization omits absent fields like ReviewStep.

### Loop-bound review composition

Extend review_workflow with a small typed `LoopReviewBinding` and public `start_loop_review`, `next_loop_review`,
`read_loop_review_evidence` operations. The binding contains session, loop alias `mt`, loop_instance_id and
review_generation. The hashed workflow identity has kind `loop_review` and this binding. The generation advances
for a new evaluation, including a second all-failed panel over unchanged plan bytes; it is not revision_round.
Retries within one evaluation advance attempt_id through existing retry receipts.

Factor `_start_run` so standalone entrypoints retain their pointer behavior and loop starts do not read, adopt,
write or delete ANY standalone/legacy pointer. Extend foundation/action validation to the one additional kind; do
not weaken schema checks by allowing arbitrary kinds. Every loop API validates the owner before claims, execution
admission and applying results. Standalone `start_review`/`start_debate` mint only their own kinds;
`next_review`/`review-next` refuse a loop_review ref rather than bypass its fence.

Reuse these shared action verbs for loop_review, adding the loop-owner fence before their existing
review lock/validation; they are not standalone workflow starts:

| Issued review work | API / CLI under migrated mt |
|---|---|
| External reviewer | `execute_external_review` / `review-execute`, which claims internally; no separate `review-claim` |
| Native reviewer, formatter or synthesis, or other issued non-external action | `claim_review_action` / `review-claim`, perform the issued work only when authorized, then deterministic capture and `submit_review` / `review-submit` |
| Confirmed lost claim | `recover_review_action` / `review-recover`, preserving the existing not_running gate |
| Explicit recovery retry | `retry_review` / `review-retry`, preserving successes and reconciling attempt_id in the loop binding |

Every shared action entry derives and validates the owner from the hashed binding; never trust a
parent-supplied owner alone. Claims and admission use loop → review lock order and release before
execution. Acceptance remains durable in the review journal first, followed by loop reconciliation.
An unlocked validated manifest peek identifies the kind/binding needed for lock selection;
re-read and validate it under loop → review locks before any admission or mutation. An identity
change between peek and locked validation refuses; the peek alone grants no authority.
Their ReviewStep return is review-local bookkeeping only: the mt driver must call
`next_measure_twice` / `measure-twice-next` after execute, submit, recover or retry and use only that
MeasureStep for subsequent work, questions and completion. It never acts on a review-local complete
or verdict. `claim_measure_action` handles advisor actions, not the review actions in this table.

Define `accepted_outcome_sha256` once as the hash of canonical UTF-8 JSON (sorted keys, compact
separators) containing full ReviewRef, LoopReviewBinding, target identity, frozen expected/usable
labels, verified accepted action IDs/digests sorted by ID, and typed synthesis judgment (null only
for verified zero-usable outcomes, which take the FAILED policy branch). Raw output
is retained in its artifact and referenced by accepted digest; synthesis text alone is not the
outcome authority. Both evidence production and application receipts use the same canonicalizer.

Binding becomes durable only after run.json/workflow.json and prompts exist and verify; then one guarded LoopState
update stores the ReviewRef, target identity, hashed roster and phase=reviewing before any reviewer can be claimed.
Unbound prepared artifacts after a crash are harmless: same generation deterministically reopens them, validates
identity and finishes the bind. Config changes after a bind cannot change its roster/model/routes/timeouts. New
evaluations resolve normal CLI > repo > global > built-in settings; preserve explicit seats precedence and the
distinction between absent options and explicit values. At initialization preserve parsed explicit panel/seats and
their provenance in the immutable mt request identity; never recover them from a rewritten task summary. Legacy
adoption parses those leading flags from the preserved raw task once. Freeze resolved roster/pins/routes at each
review binding and retain them for revision replay/resume of that evaluation; apply documented precedence only when
genuinely starting a new evaluation, not when reconnecting a claimed one.

Before filesystem preparation, checkpoint `pending_review_inputs` in the existing mt journal:
binding, target bytes/hash/spec/base, resolved roster/signatures/routes/timeout/role policy and
prompt-version identity. Feed that frozen tuple plus kind=loop_review to mint_identity; it yields
the exact run/ref to reopen. A crash or config change before bind uses this checkpoint, never
re-resolves current settings. Different existing bytes under that identity conflict. Step 2 owns
this checkpoint, bound ReviewRef, retry/application receipts and their tests; step 3 owns advisor
action ordinals/stages. No paid review can start before the bind.

No shared callback mutates LoopState while holding the review lock. Composition lock order is always loop state,
then review workflow. Existing standalone code must not acquire a loop lock. Do not hold either lock over
model/provider work, native Task waiting or user interaction. Build state mutations continue using the same
serialized state helper and their existing evidence producer.

### Atomic state changes and replay

Do not claim that two filesystem replacements are a cross-file transaction. Review action/artifact acceptance
commits first in workflow.json. A loop wrapper then acquires the loop lock, reads the bound verified workflow
evidence under the review lock, validates current binding and target, and applies the outcome AND an application
receipt in ONE LoopState read-modify-write. The receipt key includes loop lifetime, generation, full ReviewRef and
canonical outcome digest.

Crash before loop application: `next` reapplies the durable outcome. Crash after application but before returning:
the same receipt returns the resulting stage without incrementing revision/failure counters or reissuing an advisor
action. An older generation, replaced loop, changed attempt or different outcome digest is a typed conflict.
Receipt consumption is not permission to overwrite a later stage. Review retry's attempt transition is reconciled
into the loop binding with the same compare-and-swap discipline before returning any newly issued work.

For settled mt action submission, atomically store accepted digest, action status and next stage in LoopState.
Advisor submissions first use the pending-promotion protocol below; they settle only after canonical promotion.
Exact duplicate submissions return the prior acceptance; different bytes for a settled action conflict. Provide
retry-safe helper receipts so replay after ingress consumption does not require a missing file. Preserve existing
review-submit semantics for existing callers; add helper-level replay using its durable accepted receipt rather
than weakening path/hash validation.

### Advisor output ownership and promotion

Never reuse init's task-slug output path for an advisor. Issue staging paths
`.crew/plans/<slug>-<loop_instance_id>/staging/<action-id>/plan.md` and promote to
`.crew/plans/<slug>-<loop_instance_id>/plan-<action-ordinal>.md`. Both lifetime and action are immutable path
components. Each revision produces a new canonical file; no shared slug alias is written. Advisor prompts reference
the preceding canonical plan read-only and name only the new staging output as writable. Retain original input
paths as provenance; migrated/adopted plans are copied into the lifetime namespace through the same owned promotion
helper.

The sealed candidate has deterministic path
`.crew/plans/<slug>-<loop_instance_id>/sealed/<action-id>/<submitted-sha256>.md`,
outside advisor-writable staging. It is engine-owned transport data, not another state authority.

On a real advisor return, hold the loop lock, verify exact active lifetime/claimed action/issued path and submitted
digest, capture staged bytes to a sealed engine-owned candidate, and atomically record `promotion_pending` plus its
digest/candidate path in LoopState. A cancelled/replaced claim cannot reach this state. A late advisor can change
only its staging file, which is no longer read. The seal is validated by digest and never overwritten by a
different submission. Then under the same loop-owner/action checks, verify sealed bytes, atomically write the
action's canonical file and commit promotion receipt + plan_file + settled action + next stage in one LoopState
mutation. Review sees only promoted bytes; compatibility `state set plan_file` refuses migrated mt to prevent
bypass.

These are recoverable filesystem steps, not one cross-file transaction. Crash before promotion_pending leaves an
unbound sealed candidate; replay verifies the same submission/digest. Crash after it resumes promotion without a
new advisor. Crash after canonical write/before state commit verifies the existing canonical digest and commits
once; a different existing digest conflicts, never overwrites. Crash after state commit returns its receipt.
Cancellation between steps prevents further promotion; any orphan canonical file is isolated to the old lifetime
and cannot overwrite a replacement's plan. Path-scoped role instructions remain advisory with current tools; do not
claim sandbox enforcement against deliberate out-of-scope writes.

## Decisions, actions and lifecycle

Engine prompts live in `multiagent/prompts.py` or a focused mt prompt module; Markdown contains no planning,
revision, repair, quorum or termination prose. Port the existing singleton verification, raw/unparsed reading,
structural-cause diagnosis and rising-findings warning into the issued synthesis/revision prompts. A warning
produces an engine-owned human question when applicable; it never self-grants completion or silently moves BLOCKING
findings to a deferred list.

| State/evidence | Engine action |
|---|---|
| Explicit requirements document | Resolve against crew_base, freeze referenced bytes, skip re-interview |
| Task without sufficient requirements | Return bounded questions; capture answers before advisor launch |
| Requirements ready | Issue fresh `crew:advisor` with requirements snapshot and exact plan path |
| Advisor succeeds with readable issued plan | Snapshot plan; bind shared review; issue review batch |
| Advisor fails/times out or plan absent | Typed diagnostic and human wait/recovery; never review stale bytes |
| Shared reviewers/formatters claimed | Return waiting; wait for actual host completion, never poll file size |
| Complete APPROVED or REVISE minor_only | Only after the guard records successfully, deactivate and return terminal summary; advisories remain waiting |
| Complete REVISE with BLOCKING | Apply verdict once; increment revision_round; issue structural revision |
| Complete REJECT | Apply verdict once; increment revision_round; issue fresh replanning |
| Zero usable reviewer results | Record FAILED once; preserve existing first-failure retry and second consecutive failure exit |
| Synthesis failed with usable reviewers | Human recovery/synthesis retry; NEVER record FAILED or rerun successful reviewers |
| Completing verdict with any advisory | Record nothing; store question/awaiting_input; explicit human force or corrective retry |
| Degraded non-completing REVISE/REJECT | Preserve existing progress policy; quorum shortfall cannot certify completion |
| phase=done interrupted before deactivate | Finalize only; no panel, advisor or new verdict |

Requirements source is a tagged choice: an explicit `.md` document in the task selects document mode, resolves via
crew_base and skips interview; captured-answer mode selects task text plus answers when no document is given.
Supplying both modes, multiple explicit documents, or answers with a mismatched bound request/question identity returns
`conflicting_requirements_sources` before activation; do not silently override either source. A human must correct
the request explicitly. Freeze the selected source bytes and digest for action prompts and request identity.

Before activation, derive question identity from the canonical raw request hash, selected source
mode and ordered question payload. Return that identity with the questions and require it with
captured answers; changed request/question bytes require a new answer. This is stateless request
binding, not a second loop-state file. Active legacy questions additionally bind MeasureRef.

Do not initialize an active loop merely to interview an unready user. Before activation, a needs_input response has
no loop ref and does not spend a fresh budget; user answers become the request for the actual activation. Once
active, any human wait is durably awaiting_input before yielding, with the existing park cap. A response or
advancing action clears it; no manual happy-path clearing.

The human decision object references the exact advisory/question and proposed verdict. Force is explicit operator
intent, never a boolean generated by an agent from an error; preserve last_verdict_overrides and honest
forced-completion text. Hard contradictions remain unforceable. This is reliability hardening, not an enforced
security boundary against an agent that can edit files directly.

Keep Stop's authority over fires, parks, started_at normalization, deadline and safety deactivation. Share existing
bound predicate/helpers so mt advance also declines new paid work when a bound already elapsed; it cannot reset any
bound. Do not add timers: dormant sessions are still not guaranteed a deadline wake-up. `phase=done` retains the
existing bypass of work limits for safe finalization.

Add a pure mt lifecycle projection in leaf `scripts/loop_projection.py` (stage, next command,
waiting/advisory, budget, cancellation hint), importing only light state/identity helpers.
Hooks import this leaf in-process, never the review engine; its issued next argv invokes
`crew measure-twice-next` in the following agent turn. It reads durable state without claiming or
reconciling actions inside the bounded hook invocation. SessionStart
and Stop use that projection instead of the old mt recipe; hook output schemas, bounded waits, orphan scoping,
corrupt and future-schema handling remain unchanged. A projection never calls a model or claims work. Leave the
build branch's recipe and routing intact.
Stop transports the rendered next argv in its existing block-reason text; SessionStart uses its
existing additionalContext shape. The installed gate must observe those actual transports.

Cancellation uses the same LoopState transaction as existing deactivate --cancel; stamp cancelled and invalidate mt
claims/current binding. Ask the runtime to stop only owned handles when it can; lack of a native cancellation API
is reported. Late artifacts may remain for audit, but cannot advance inactive/replaced state. Safety force-exit
likewise invalidates admission; do not re-init to evade bounds.

## Deterministic transport and the exercised native seam

Expose narrow `plugins/crew/scripts/multiagent/workflow_transport.py` helpers, reusable by mt/review adapters:
render issued argv using quote_argv; capture returned bytes at an issued path; hash bytes; construct/validate
HostResult or mt result; write exact submission envelope; submit and return receipt. All filesystem paths are
engine-issued and guarded against escape/symlinks. Input text is spilled as data, never interpolated into shell. A
file is never accepted merely because it is nonempty.

CLI: `crew measure-twice -f <request-file> --session-id <literal> --consume`, plus `measure-twice-next`,
`measure-twice-claim`, `measure-twice-submit`, `measure-twice-recover`, `measure-twice-decide`,
`measure-twice-cancel`. Request-file schema carries raw arguments and optional captured requirements; Python alone
parses leading panel/seats options, rejects duplicate/missing values, and preserves positional task bytes. Add a
deterministic submission-file helper accepting the issued action/ref and `-f <returned-text-file>` or typed
failure; it computes the hash/envelope. No hand-built JSON, separate hashing shell call, or model-selected paths.
Suppress artifact bodies in helper stdout.

The initial raw-argument spill is the parent host's existing Write operation, once, with exact
bytes; it needs no scribe or hashing model. Python reads/parses/consumes it. Code adapters may
write the same request bytes directly. Scribe hygiene applies to native Task return transport,
not to this initial user-input spill.

Code adapter seam: an injected, per-invocation runtime has `launch(work_item)`, notification-driven completion
records, and optional `cancel(handle)`; the runner starts every independent item before awaiting any completion. A
completion supplies returned bytes/status; the deterministic helper performs direct capture. This seam consumes
existing frozen WorkItems; it does NOT re-resolve models or decide which provider is native. Native/external
describes route, not whether Python can run it. A code-native runtime can execute a native WorkItem directly; the
Claude Markdown transport still launches the host's native Task.

Exercise the actual helper/runner with a deterministic fake native runtime that accepts different provider/model
labels as already-issued native items, returns bytes and timestamps, and fails if any scribe is requested. This
proves direct capture and concurrent scheduling, not real multi-provider native admission in Claude/OpenHands.
Production seat routing stays unchanged; no new host name, arbitrary force-native option, runtime registration
config or SDK dependency.

For installed Claude retain bare fresh Task spawn, issued roles/models and prompt references. Its returned text
currently needs model-mediated file transport: keep existing issued scribe hygiene and verified fallback until
direct capture is demonstrated on that host. Replace mechanical post-write hash/envelope steps with the helper; add
no agent solely for new bookkeeping. Formatters remain only for parser repair, never automatic extra reviewers.
Launch external background work before native waits, preserve overlap, and reconcile once per completed
batch/notification. Never add a polling loop or serialize per-seat reviews.

## Implementation sequence and file ownership

Each step is an independently green milestone; it may contain smaller green commits when execution includes commits. Retain old
production mt until the tested cutover in step 5. New internal APIs/tests may land before the command switches. Do
not leave an intermediate commit with half-migrated hooks or failing existing callers.

1. **Freeze baselines and extract state service.** Record existing mt call/role
   traces in `scripts/tests/fixtures/measure_twice_baseline_trace.json`, including
   source/plugin versions, scenario, target/panel/pins and normalized event/count
   records; pair the migrated trace with the same inputs in step 6. Keep live
   observations distinguished from deterministic fixtures. Add pure evidence/typed outcome tests before moving guard code. Move
   state mutation logic to loop_state.py and leave existing CLI entry behavior.
   Acceptance: both current build/mt hook and state suites pass unchanged.
2. **Add loop review composition.** Implement binding/kind, no-pointer start,
   accepted-action evidence, owner fencing and receipt reconciliation in
   review_workflow.py/review_runs.py and the state service. This step owns the
   schema-4 bump and optional journal field plus downgrade fixtures; old mt still
   runs without a journal until cutover. Acceptance: concurrent
   standalone review/debate/build prep cannot change mt evidence, and stale loop
   results never increment or complete a replacement lifetime. Gate with the
   quorum/advisory, review-receipt crash, pointer isolation and owner-fencing
   verification scenarios below plus the existing review-workflow suite.
3. **Implement mt actions/prompts.** Add measure_twice.py, model serialization,
   input parsing, advisor/requirements/revision/human decisions and lifecycle
   projection. Acceptance: all decision-table paths run through the one guard;
   interrupted verdict application and finalization replay exactly once. Gate
   with requirements-source/start-admission, advisor-promotion crash/late-write,
   decision-table and done-finalization scenarios below plus hook regressions.
4. **Implement transport and code-runner proof.** Add helpers, CLI commands,
   strict envelopes, concurrent runner and test fake. Acceptance: no hash/JSON
   reasoning needed by the orchestrator; direct fake-native completion uses no
   scribe and starts the full review batch before any wait.
5. **Cut over mt command/hooks and migrate legacy state.** Replace production
   choreography; preserve build arms. Update cancellation command, status,
   artifact_prune.py protection and documentation for bound run directories.
   Acceptance: no permanent fallback to old mt panel recipes and no pruning of
   active/nonterminal loop-bound reviews. Extend live_run_keys and identity
   validation for explicit bound/pending ReviewRefs; never rely on standalone
   pointers for protection. Report terminal lifetime plan artifacts for operator
   cleanup; do not add automatic deletion of plans or evidence.
6. **Complete regression and installed-Claude gates.** Document measured costs,
   supported host facts and residual scribe/deadline limits. Only then write the
   Phase 5 implementation specification against the proved interface.

## Legacy cutover and deletion inventory

Use a single one-shot migration when an active mt lacks mt_workflow; preserve task/plan, owner, loop_instance_id
(stamp once if genuinely absent), all budgets, revision/failure counts, override history, awaiting_input and exit
fields. Refuse corrupt/future-schema files as today. Never migrate build to the new mt workflow.

For BOTH active drafting and reviewing, first park with a migration question. An existing file,
absent file, finished-looking panel, elapsed time or empty requirements field cannot prove that
old advisor/reviewer work has returned. `measure-twice-decide` requires
`kind=legacy_work_not_running`, MeasureRef, question ID, retained phase, retained run ID when
present, and `confirmation=not_running`. The operator confirms only after old work has finished
or been stopped. Revalidate all bindings under the loop lock; stale confirmations refuse. No
adoption, new advisor, revision or review is admitted before that confirmation. A fresh legacy
state deliberately created by the compatibility init path uses this same conservative gate.

After quiescence, readiness is deterministic: after parsing preserved leading selection flags,
an exact single design-document path uses its frozen readable bytes; otherwise nonblank preserved
task text is the minimum legacy requirements source, including when interview answers were never
stored. Only an empty task or unreadable exact document returns a bound requirements question.
Never infer unready from a missing journal-era requirements field. Do not invent lost answers.

- Legacy drafting: inspect the plan FILE, not merely its populated plan_file string. Copy a
  readable usable plan through owned promotion. With last_verdict empty, review that adopted
  plan; if the file is absent/unreadable, issue initial planning from ready requirements.
  With last_verdict=REVISE, issue structural revision using the plan and verified old panel;
  with REJECT, issue fresh replanning. If that old verdict's panel/context is missing, park for
  a bound human recovery decision rather than re-paneling unchanged bytes or inventing feedback.
  A confirmation may additionally attest `completed_action=initial_plan|revision|replan` and
  the exact readable plan digest; if that completion matches the retained verdict's expected
  action, adopt the already-produced output and review it without duplicating paid planning.
  A not_running confirmation alone never asserts successful completion.
- Legacy reviewing: retain old identity and raw artifacts as migration evidence; after the
  common quiescence gate, promote the selected readable plan and start a fresh shared evaluation.
  Do not count old flat outputs as shared accepted actions. Missing/unreadable plan requires
  bound human recovery before another action. No automatic finished-files alternative exists.
- Legacy done: guarded finalization only, preserving clean/forced completion.
- Inactive/cancelled/force-exited: remain terminal; no automatic activation or
  budget reset. Repeated migration invocation changes nothing after its receipt.

Delete mt's review-prep/run/persist-seat/wait/repair/collect/synthesis recipes from commands/measure-twice.md and
its Stop/SessionStart guidance. Retain those engine commands for build/standalone legacy consumers; deleting them
now breaks Phase 5. Remove duplicated mt prompt/verdict prose, not shared reviewer/formatter roles. Reconcile
`plugins/crew/agents/advisor.md`: workflow-issued requirements, staging path and revision instructions take
precedence over its generic interview/save recipe. Preserve its ordinary standalone planning behavior and advisory
access tier; it must not re-interview or select another path inside migrated mt. Keep compatibility state verbs for
build and legacy cancellation; direct begin-review/record-verdict on migrated mt must refuse or delegate to its
typed binding protocol, never become a second way to certify a workflow.

Update scripts/CLAUDE.md, engine-notes.md, user documentation and command specs. Replace mt recipe-golden tests
with protocol/behavior tests; retain build golden tests. Search production files for obsolete mt review-prep and
state-verdict recipes and demonstrate only explicitly historical docs reference them.

## Verification and evidence gates

Add tests/test-measure-twice.py and reusable mt fake fixtures. Test public typed APIs and CLI, not snapshots of
private dictionaries alone. Required scenarios:

- Requirements doc skips interview; task questions and answers bind correctly;
  hostile shell characters, spaces, leading options and divergent cwd preserve
  exact bytes and crew_base anchoring. Absent panel is never hardcoded full.
- Conflicting document/answer sources refuse before activation; identical active
  requests resume; differing identity leaves the whole state/budgets/binding
  unchanged; missing journal migrates once; force-exited state cannot re-init automatically.
  Unsupported advisor host emits unsupported_planning_host with no active state.
- The authorized compatibility `state init mt --force` restart creates one fresh
  lifetime and budget; typed start then adopts once. Without human authorization
  the driver never calls it, and start returns the old terminal status. Active
  loops still refuse init, even with force; resume cannot reset a bound.
- Advisor late WRITE after cancel and same-task replacement changes only old
  staging bytes: replacement canonical plan and bound target stay unchanged.
  Promotion replay after each sealed-candidate/pending/canonical/receipt crash
  commits the same digest once without spawning again; changed digest refuses.
- Initial draft → approve; blocking revise → fresh review; reject → replan;
  minor-only completion; singleton blocking verified; raw/unparsed repair kept.
- Strict quorum is `usable >= N // 2 + 1` from frozen distinct labels. Missing,
  failed and late seats stay named. Formatter/synthesis failure is not FAILED.
  First all-failed evaluation retries once; second ends review_failed, even if
  the plan hash is unchanged. A successful reviewer breaks consecutive failures.
- Every completion advisory records nothing until an exact human decision;
  forced overrides are stamped; malformed/wrong-phase identities remain hard
  errors. Drifted plan, re-bound generation and missing target cannot certify.
- Claim replay never spawns twice. Submission replay after consumption succeeds
  only for its accepted digest. Lost claim requires explicit not_running; timeout
  alone is insufficient. Retry preserves successes and freshness for new seats.
- Inject crashes after run creation/before bind, after accepted review/before
  outcome apply, after apply/before return, and between done/deactivate. Counters,
  action issuance and revision generation remain exactly once.
  Change config between preparation and bind; replay reopens the checkpointed
  identity/roster and does not mint a second paid evaluation.
- Concurrent hook counter increment and mt action submission both survive. Cancel
  or safety force-exit racing completion/claim prevents later advancement.
  Lock timeout emits a typed error/no partial state; document existing non-POSIX
  state-lock degradation rather than claim unsupported transactional guarantees.
- Standalone review/debate pointers and legacy build current-run.json can change
  during an mt review without altering it; malformed bound refs still refuse.
  Same session/new loop and other sessions cannot adopt stale mt submissions.
- Shared review action verbs accept bound loop_review work with owner fencing;
  next_review/review-next refuses that kind. Review-local complete/needs_input
  returns never drive mt: the next authoritative response comes from
  measure-twice-next. Test cancellation between action admission and acceptance,
  and failed loop reconciliation followed by a successful receipt replay.
- Parked and human waits retain free bounded parks; past-cap nudge resets parks;
  termination precedes parking; done routes only to finalization. Future schema
  is untouched; legacy migration and pruning protections cover every phase.
- Legacy drafting and reviewing emit bounded needs_input and preserve run/budgets;
  only bound legacy_work_not_running confirmation permits adoption/new work.
  Test a legacy advisor running before its first write and after a partial write:
  neither file absence nor existence admits another advisor or reviews unfinished
  output. After confirmation, nonblank task/no requirements field is ready; an
  empty task or unreadable exact document parks. Test empty/REVISE/REJECT prior
  verdicts, missing plan despite populated plan_file, missing prior panel, and
  attested completed revision/replan adoption with no duplicate planning.
  Wrong/stale confirmation refuses. Schema-3 adoption preserves fields; schema-4
  downgrade load/save/cleanup refuses on both mt and build without stripping data.
- Fake all-native runtime launches at least two provider/model-labelled WorkItems
  before first completion; direct captures byte-identical results with zero
  scribes, preserved attribution and notification-driven waits.

Run Python 3.11+ with `rtk` prefix: test-measure-twice.py, test-review-workflow.py, test-hooks.py and
test-multiagent.py. Use an explicit verified interpreter and process-local PATH that resolves subprocess python3 to
the same installation; these suites replace HOME and asdf shims can otherwise fail before exercising code.
Non-normative inspected-machine example: `rtk proxy env PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
/opt/homebrew/bin/python3 plugins/crew/scripts/tests/<suite>.py`. Do not hardcode that path in tests. Isolate
catalog-default checks with the existing crew_config helper so personal seat/panel overrides cannot leak into
fixtures. Do not change global settings or set host overrides for live workflow validation. Run relevant command
conformance checks and version metadata checks if packaging files change.

Installed-Claude gate: use a harmless plan-only fixture and explicitly selected small panel in an isolated
project/session. Capture loaded plugin version and paths. Start, pause on human input, resume after
compaction/session re-entry, revise one blocker, review and complete; separately exercise cancel, interrupted claim
recovery, partial-panel advisory and done-before-deactivate recovery. Verify Stop-triggered AUTOMATIC continuation:
deliberately end an active working turn with no human question or work in flight, observe the installed Stop hook
block and re-enter the session, then observe the engine resume the same lifetime/action and make progress without
manual command re-invocation or duplicate paid work. Also verify actual Task roles/pins, external/native overlap, truthful
result transport, no duplicated panel launches, and intact budgets. Automated fakes cannot replace this mandatory
app gate. If a required host surface is unavailable, report implementation/regressions ready and installed-Claude
validation pending; Phase 4 is NOT complete and the Phase 5 plan remains gated. Keep runnable evidence;
terminal-only probes cannot satisfy it. Only explicit operator rescheduling can change that gate, and this planning
authorization grants no such exception. Cursor's deferred host gate remains visibly owed.

Measure baseline and migrated identical target bytes/seats/model pins. Record orchestrator tool calls/tokens where
observable, advisor/reviewer/formatter/scribe counts, wall time, provider intervals, batch overlap and
interrupted-resume costs. Deterministic gates: no polling or extra reviewer/advisor/bookkeeping invocations; full
reviewer launch overlap; no model-generated hash/envelope; no duplicate paid work on replay; direct code-native
capture has zero scribes. Report Claude's retained scribes separately. Live token/latency numbers are observed
evidence, not a promised speedup or pass threshold fabricated from provider variance.

Definition of done: one mt decision owner, one loop state authority, a tested shared review binding, deleted old mt
choreography, preserved build behavior, passing relevant regression suites, installed-Claude lifecycle evidence
including Stop-triggered automatic continuation, and documented efficiency/unsupported-host limits. This gate
authorizes preparing the Phase 5 plan; it does not imply build or OpenHands has been migrated.

## Planning review inputs

This planning run reviews this implementation plan and the current roadmap amendments together. Reviewers should
inspect the linked roadmap and operator follow-ups for sequencing, support claims and phase gates; compare code
claims to baseline b39f647. The brief is summarized in this plan and the roadmap, so an ignored local file is not
required to execute the durable plan on another clone.

Before recording plan approval, the orchestrator must verify the tracked linked inputs still have the hashes below.
A change requires a new plan snapshot and panel review; a verdict on this document alone cannot certify changed
inputs. Publish the durable plan and linked tracked docs together to make the reviewed instructions self-contained
on a fresh clone. Until then this is a saved plan, not a published branch change. The ignored shim documents carry
superseded headers and are supporting, session-only history; they are not must-verify inputs for future clone
execution and need not be published.

| Review input (repository relative) | SHA-256 |
|---|---|
| `plugins/crew/docs/multi-harness-engine-roadmap.md` | `cf1d9c165a7888ac6739482d98b8b19482b7fd21bd2a8b82833125772025645c` |
| `plugins/crew/docs/operator-followups.md` | `403c8b3f21b49d0fd667b58279be633281ed005b017c1ee82f079bdc6e9d5f25` |
| `.claude/CLAUDE.md` | `d1432505c650706234482c1b7d3b811e72631d837d98f925bb0ab387893dd156` |

Supporting session-only historical input hashes (not execution prerequisites):

| Ignored input | SHA-256 |
|---|---|
| `.crew/plans/chunk-c-measure-twicemd-shim-refactor-create-an-ex.md` | `e3dcd0e8158c0e1882418ea041992e83b8111fcf9e5cabb1101fc6d618fd5e56` |
| `.crew/plans/chunk-c-buildmd-shim-refactor-create-an-executor-r.md` | `3ad3ec3c5f818774c81c7d1e8256bba1a1a01d21d8de30192b5398e008fe546a` |
