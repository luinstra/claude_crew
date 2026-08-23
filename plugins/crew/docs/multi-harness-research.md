# Research: First-class multi-harness Crew workflows

> **Status:** Archived research and design evidence. This document is
> non-normative and is not an implementation plan. The phased direction and
> merge gates are owned by `multi-harness-engine-roadmap.md`; every claim reused
> from this research must be rechecked against current code, current host
> documentation, or live behavior.
>
> **Objective:** Run the same Crew review, debate, measure-twice, and build
> workflows from Claude Code, Cursor, and Codex while preserving Crew's existing
> selection, frozen-run, quorum, review freshness, and executor-continuation
> semantics. ChatGPT Work remains an optional one-shot surface; its lack of
> plugin hooks does not constrain persistent loops in Codex.

## Task summary

Create one shared workflow module and three thin host adapters. The shared module
must own workflow phases and invariants; the Python engine must continue to own
deterministic configuration, seat selection, execution resolution, run identity,
result normalization, quorum, and loop state. A host adapter may translate only
the small set of operations that genuinely varies by harness: locate the plugin
dispatcher, spawn and await a native subagent, and serialize lifecycle-hook
responses.

The orchestration host and the selected execution route are independent. A run
started in Cursor may execute a seat through native Cursor, the external Claude
client, or the external Codex client according to that seat's ordered `via`
list. Native execution must never jump ahead of an earlier viable `via` entry,
and a runtime failure must never trigger an unrecorded route, model, or seat
substitution.

This plan supersedes `.crew/plans/portable-multi-harness-seat-execution.md` for
execution. Retain that file as design history. It correctly introduced `via`,
host-aware resolution, and channel provenance, but its Phases 1-2 are partly
implemented already, it still assumes a Claude `Task` split, and its Pi,
`--via`, route-registry, and weighted-panel ideas are not part of this work.

## Verified current-state map

### Shared engine and configuration

- `plugins/crew/scripts/multiagent/seats.py` already exposes `SeatSpec.via`,
  mechanically translates legacy `provider` to a channel, rejects a row that
  declares both spellings, and preserves the existing catalog/panel merge rules.
  It currently rejects more than one `via` entry with the temporary diagnostic
  "one channel per seat until a second host exists."
- `plugins/crew/scripts/multiagent/channels.py` already detects Claude and Codex,
  accepts `CREW_HOST=claude|cursor|codex`, returns `ResolvedExecution`, and
  records the selected channel. It now answers the native question twice:
  `native_channel` maps Claude and Cursor to an in-session channel, while
  `task_native_channel`, the narrower answer every caller declares today, maps
  only Claude. Its multi-entry algorithm also prefers a later native candidate
  over an earlier viable external candidate, which would violate ordered route
  ownership once multi-entry `via` is enabled.
- `plugins/crew/scripts/multiagent/cli.py` already routes `review-prep`, debate
  seat resolution, dispatch, and `build-executor` through `channels.resolve_seat`.
  It preserves panel availability, opt-in filtering, run-scoped frozen targets,
  per-seat signatures, pending-seat resumption, failed-seat isolation, and
  channel provenance.
- The engine-to-orchestrator interface is still Claude-shaped. `review-prep`
  emits `subprocess_seats`, `task_seats`, `task_seat_models`, and their pending
  counterparts; run records encode signatures as `kind = subprocess|task`.
  Debate emits the same split. This is the seam to replace, not duplicate three
  times.
- `build-executor` preserves the required fresh precedence:
  `active-loop stamp > --executor > [build].executor > crew:executor`. It rejects
  every native resolved seat except the special Claude-owned `crew:executor`
  sentinel, so a configured native executor cannot yet run in Cursor or Codex.
- `plugins/crew/scripts/models.py`, `crew-state.py`, and continuation records
  already preserve executor identity, `[build].resume_executor`, exact external
  continuation IDs, stop-fire/deadline bounds, and terminal verdict state.

### Workflow orchestration

- `plugins/crew/commands/review.md`, `debate.md`, `measure-twice.md`, and
  `build.md` repeat the same fan-out, native-result persistence, repair, collect,
  and quorum prose. Every native branch is explicitly "Claude Code host ONLY"
  and invokes `Task(...)` with Claude agent names.
- `measure-twice.md` and `build.md` additionally hard-code native advisor and
  executor Task calls. Cursor currently performs a silent generic substitution
  for those missing agents; that is not a valid adapter.
- Fresh reviewer seats and executor-only continuation are currently distinct and
  must stay distinct: panel reviewers are new one-shot agents each round; only
  the selected external build executor may reuse its exact continuation chain.
- `status.md` and cancellation commands contain Claude-specific root/session
  wording even though their state-machine operations are host-neutral.

### Host packaging and live evidence

- Claude packaging is first-class through
  `plugins/crew/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
  `commands/`, `agents/`, and `hooks/hooks.json`.
- Cursor packaging exists through `plugins/crew/.cursor-plugin/plugin.json`,
  `.cursor-plugin/marketplace.json`, and `hooks/cursor-hooks.json`. The Cursor
  manifest points at `agents-cursor/`, which now ships the three thin role
  adapters standalone review drives in-session (reviewer, formatter, scribe)
  alongside the `.gitkeep` that retains the directory.
  `plugins/crew/docs/cursor-host.md` verifies commands,
  hooks, all three external clients, and an end-to-end review, but records native
  agent substitution and unverified stop-hook follow-up. The Cursor host marker
  table now ships filled with `CURSOR_AGENT` and `CURSOR_CONVERSATION_ID`; an
  independent second capture is still owed. Its verified install
  currently uses the Claude marketplace manifest/copy semantics and Cursor's
  pinned marketplace clone/cache path. Loops are operator-enabled and explicitly
  best-effort; the previously shipped Cursor refuse-to-arm guard was removed and
  must not be reintroduced. This is verified Cursor evidence, not evidence of a
  live Claude install.
- Codex has verified SessionStart/Stop delivery and stop-loop coercion in
  `plugins/crew/docs/codex-host.md`, but there is no source
  `plugins/crew/.codex-plugin/plugin.json`, no first-class Codex marketplace
  entry, and no source Crew skills. The current migrated command skills cover
  only a small subset and do not provide review/debate/build parity.
- Current OpenAI plugin guidance supports Codex subagents and hooks, recommends
  expressing reusable commands/agents as skills, and states that ChatGPT does
  not run plugin hooks. Current Cursor supports plugin skills/commands,
  subagents, and lifecycle hooks. These capabilities justify a real three-adapter
  seam; their exact invocation, model pinning, and permission behavior still
  require the live probes below.
- `scripts/post-commit-version-bump.sh` and
  `plugins/crew/scripts/tests/test-version-bump.py` update and roll back only the
  Claude/Cursor manifest and marketplace twins.
- `README.md` still describes Crew primarily as a Claude Code plugin and marks
  Cursor subagent commands unsupported.

## Invariants that must not change

1. **Review selection:** `--seats > --panel > default_panel > built-in full`.
2. **Debate selection:** `--seats > --panel > [debate].panel > default_panel > built-in full`.
3. **Build executor selection:** active-loop executor stamp > `--executor` >
   `[build].executor` > `crew:executor`.
4. Shipped and configured panels, group-token expansion, `available`, `opt_in`,
   explicit-unavailable diagnostics, and the whole-panel empty fallback retain
   their current behavior.
5. Quorum remains a strict majority of distinct launched seat names. Do not
   collapse seats by vendor or model family.
6. Every panel seat reviews the same frozen target bytes and is identified by
   its exact run identity and execution signature.
7. A failed or unavailable seat is labeled and isolated; it never silently
   becomes another seat, model, or channel.
8. Reviewers are fresh each round. Only the selected build executor may continue,
   and only when the existing `resume_executor` policy and provider continuation
   contract allow it.
9. A loop cannot change executor or resolved route after activation. Legacy
   active loops that lack route stamps retain the old executor-only behavior for
   their remaining lifetime; new loops always stamp the route.
10. Preparation remains zero-spawn. `review-prep` may stage and describe work,
    but it must not hide fan-out inside an opaque Python thread pool.

## Target module, interface, seam, and adapters

### 1. Deterministic execution-resolution module

Keep the real Python seam in `multiagent/channels.py`; deepen it instead of
adding a second route registry.

Its interface is one operation with an optional pinned route:

```text
resolve_seat(
    seat, host, observed_harness_version, requested_access,
    capabilities, pinned_route=None
)
    -> ResolvedExecution | None
```

`observed_harness_version` is an explicit nullable argument, not a field hidden
inside `capabilities`. Work-plan entry points discover it exactly once from their
typed lifecycle context and pass the same value to every resolution in that
preparation; `resolve_seat` never re-discovers or reads ambient lifecycle state.

`pinned_route` is a `RouteStamp(host, observed_harness_version,
external_client_version, channel, driver, access, requested_model,
request_kind)` used
only for active-loop resume. When present, resolution verifies that exact route
or errors; it never performs first-viable search.

The external-client version is required when `driver=external` and must be null
when `driver=native`. Resume re-observes the harness version, requires exact
equality with `observed_harness_version` **and** satisfaction of the current
version constraint, and for an external driver requires the exact current client
version to equal its stamp. Any nullability/version mismatch exits before launch
with loop state and continuation untouched.

`ResolvedExecution` contains only facts consumed by orchestration, run identity,
or validation:

```text
seat
requested model
request kind = exact | family_alias | router
selected channel
driver = native | external
resolved access = read-only | workspace-write
supports continuation
enforces requested role/tool scope
supports bounded await through exact host interface
supports cancellation by handle
supports bounded post-cancel settlement
observed harness version
observed external client version (external driver only)
```

Resolution walks `SeatSpec.via` from first to last. For each channel, a matching
host tries that channel's native driver first only when current evidence admits
the requested model/access/role; if native is inadmissible, it tries the same
channel's independently eligible external client before moving to the next
`via` entry. Failing native admission closes only that native candidate; it does
not poison or lend evidence to the external candidate. A non-matching
host has only the external candidate. This preserves channel order while giving
an `external-only` host a legal path. Once a driver is selected and launch
begins, runtime failure is final; it never retries that channel externally or
advances to another channel.

Capability facts are keyed by **host + channel + driver**, never by channel
alone. Add `plugins/crew/scripts/multiagent/host_capabilities.py` as the static,
probe-backed source for native facts such as
`("codex", "codex", "native")`; keep external facts owned by the existing
provider modules and normalize them under keys such as
`("*", "codex", "external")`. `channels.py` merges those two sources for one
lookup. A native Codex driver and an external Codex CLI may therefore report
different model, write, continuation, and role-scope capabilities without
either borrowing the other's guarantees.

Add one host-version seam, proven independently for every host in Phase 0A:

```text
LifecycleContext = ClaudeLifecycleContext | CursorLifecycleContext | CodexLifecycleContext
discover_harness_version(host, lifecycle_context) -> normalized string | null
```

Each typed lifecycle context has the common envelope `host`, `event`,
`session_id`, absolute `project_dir`, `harness_version`, and `payload_sha256`, plus a
host-specific typed fields object. Phase-0A host docs pin the exact required/
nullable Claude, Cursor, and Codex field names, types, and normalization from
captured payloads; constructors reject a host/discriminant mismatch and do not
read arbitrary ambient environment keys.

The lifecycle writer/refresh policy is versioned evidence. SessionStart is the
initial writer: it filters the raw payload into allowlisted host fields and
atomically writes mode-0600
`.crew/reviews/.sessions/<validated-session-id>/lifecycle.json` (already ignored
by the fixture's review-root rule). The common stored schema is `schema`,
`host`, `event` (initially `session_start`, thereafter the exact proven refresh
event), `session_id`, canonical `project_dir`,
`harness_version` (`string|null`), typed `host_fields`, SHA-256 of the raw payload
bytes, `captured_at`, and fixed 24-hour `expires_at`; raw payload bytes are never
stored at runtime. Only a Phase-0A-proven synchronous, session-correlated
lifecycle event may atomically refresh the same host/session/project record; its
evidence pins required stable identity/version fields and readiness semantics.
Any accepted refresh replaces the record and performs contained, no-symlink cleanup of expired
`lifecycle.json` files only under that project's `.sessions` root; it never
deletes sibling loop artifacts. CLI commands look it up only through their existing
validated session ID, require host/session/canonical-project equality, reject
expired/malformed/mismatched records, and never scan another session directory.
A non-hook invocation uses `event="cli"` over a successfully loaded current
lifecycle record; without one it constructs a typed context with
`harness_version=null`, which makes native resolution unavailable. Raw captures
exist only as redacted Phase-0A evidence artifacts.

`harness_version` remains `string|null` through lifecycle lookup, resolution,
prepared/work-item/run records, external `ProviderResult`, diagnostics, and
evidence. Native `SpawnRequest`, `HostHandle`, and `HostResult` require a
non-null admitted value. Tests cover atomic replacement, expiry, payload-hash
stability, missing/corrupt records, wrong host/session/project, unsafe session
IDs, and hook versus CLI construction.

Cursor needs an explicit branch: its `sessionStart` is new-conversation and
fire-and-forget, so native prep performs a fixed ten-second monotonic readiness
wait for the initial record, matching the current hook timeout; it is not
configurable. Expiry or readiness failure occurs before reservation/spawn,
diagnoses native unavailability, and leaves the normal ordered resolver free to
select an independently eligible external route. Phase 0A must then prove Stop or another common synchronous
event carries the same stable `conversation_id` plus `cursor_version` and can
refresh that same session record. If no such same-session event passes, expired
Cursor loops pause unchanged and Cursor cannot advertise recoverable persistence
beyond 24 hours; starting a new conversation is not same-session recovery.
Cursor may claim `full-persistence` only when both this same-session refresh and
the independent Stop `followup_message` continuation probe pass; either missing
proof limits it to `core-read-only`.

It reads only the lifecycle payload/environment or host API that the applicable
probe proves identifies the running orchestration harness. It must never use an
external `claude`, `cursor`, or `codex` client version as a substitute unless a
host-specific probe proves that value is the harness version. The observed
normalized version flows through `resolve_seat`, prep payloads, `run.json`,
`HostHandle`, `HostResult`, normalized persisted results, `status`, and
`doctor`. Native entries are added only after the relevant live probe is
recorded. Each entry includes `version_constraint` (initially exact normalized
equality), `verified_on`, and `evidence_doc`; `verified_on` is provenance, never
a TTL. **Stale** means the normalized observed version does not satisfy
`version_constraint`. Missing, unparseable, or stale observation
makes native unavailable, retains external-only resolution, and emits a loud
runtime/doctor diagnostic containing observed version, version constraint,
evidence path, and exact re-probe -> capability/evidence update -> reinstall ->
`doctor` instructions. There is no config or force override for re-admission,
and an unknown newer version is not admitted. A future evidence-backed version range or feature probe may
replace an exact version, but only as a separately tested capability fact.

Centralize model evidence in
`model_matches(requested_model, reported_model, capability)`, where every
capability declares `request_kind=exact|family_alias|router`:

- `exact` normalizes the requested and concrete reported values with the
  host-specific normalization proven in Phase 0A and requires equality. A
  concrete Cursor request always requires that concrete report; null or a
  fallback model is a mismatch;
- Cursor `auto` is a `router`, not an alias target. It requires and persists a
  nonblank concrete `reported_model`; null, blank, or a report of only `auto`
  is a mismatch;
- Claude model aliases are provider- and harness-version-bound
  `family_alias` requests. Capture the concrete reported model when available
  and verify it belongs to the evidenced family. A concrete incompatible family
  is a mismatch; null is accepted only under the version-bound guarantee
  recorded in that capability; and
- all other missing reports fail unless their current version-bound capability
  explicitly guarantees the requested pin.

An upstream model retirement leaves the affected native gate failed and the
host external-only. Recovery is an intentional same-commit update to the shipped
catalog, live fixture `via`/model row, semantic expectation, capability evidence,
and tests; the resolver never substitutes a replacement model on its own.

External model evidence is driver-specific and version-bound. Track
`external_client_version` separately from orchestration `harness_version` and
propagate it through resolution, work items, run execution records, provider
results, persistence, and doctor. Cursor read-only runs always request its
documented stream JSON and parse `system/init.model`; `auto` is usable only when
a live probe for that client version returns a concrete nonblank, non-`auto`
model. Claude external runs parse the documented stream init model. Codex and
Agy may persist `reported_model=null` only when evidence bound to that exact
external-client version proves the exact `--model` request cannot silently fall
back and includes an invalid-model negative control; otherwise that external
route is unavailable. Provider CLI construction must never overwrite an
observed report with the requested model. Parser fixtures are version-keyed and
include missing/renamed/init-order drift; unknown parser/version output fails
the external candidate closed. The external-only validation matrix runs these
probes: it may retain a labeled unusable seat result, but cannot claim
`cursor-auto` roster parity without its concrete routed-model evidence.

Caller access is fixed, not left to implementer judgment:

- `review-prep`, debate/council, panel `run`, reviewer, panelist, advisor, and
  formatter seat/model requests use `read-only`; a scribe, when a host still
  needs one, receives a separately proven single-artifact-write role rather than
  general workspace write and does not participate in seat routing;
- `dispatch` and build executor resolution use `workspace-write`;
- advisor planning uses `read-only` and the built-in implementation executor
  uses `workspace-write`;
- continuation is not an admission requirement. Resolution returns whether it
  is supported; the existing `resume_executor=true` policy reuses it when
  supported and otherwise starts fresh exactly as today. Review roles always
  discard continuation capability. No review/debate caller rejects a seat
  because continuation is absent.

Native workspace-write admission additionally requires the host capability to
prove enforceable executor role/tool scope. A generic write-capable child or
silent role substitution is not an executor adapter. Hosts without that proof
must reject `crew:executor`/native configured executors and may still use an
explicit external write-capable seat.

Every native admission, read or write, also requires live evidence for
`bounded_await`, `cancel_by_handle`, and bounded settlement through that host's
exact handle interface. The Markdown adapter owns await/cancel/settlement;
Python owns the absolute deadlines, validates the returned timing, and rejects a
late terminal result rather than relabeling it success. Missing any operation
makes the host external-only. Do not synthesize support with a generic Python/
shell watchdog that cannot terminate the real host child by handle.
This applies to Claude too: if its live bounded-await/cancel/settlement proof
fails, the existing Task-shaped native mechanics are intentionally demoted to
external-only. Prior behavior is not grandfathered into native admission.

Keep the channel set explicit (`claude`, `codex`, `cursor`, `agy`). Do not add a
user-configurable route registry. Existing provider-specific tuning remains
where it is until two real adapters need the same new option.

### 2. Host-neutral work-plan module

Add `plugins/crew/scripts/multiagent/workplans.py`. This module earns its seam
because review and debate already need the same roster resolution and three
hosts need the same output. Its interface is:

```text
prepare_panel(request, host, lifecycle_context) -> PreparedPanel
prepare_debate_round(request, host, lifecycle_context, round_number) -> PreparedDebateRound
resolve_build_executor(request, host, lifecycle_context) -> PreparedExecutor
prepare_run_role(context: RunRoleContext, role) -> WorkItem
prepare_loop_role(context: LoopRoleContext, role) -> WorkItem
```

`RunRoleContext` is an immutable typed record whose schema/validation is owned
by `review_runs.py` and whose instance is assembled by `workplans.py` from the
normalized frozen run. Its exact fields are `host`, typed `lifecycle_context`,
the run's frozen current `observed_harness_version`, `session_id`, `run_id`,
absolute `run_dir`, `round`, `resolved_execution` (the complete frozen route,
model/request-kind/access/external-client-version facts), `role_catalog_version`,
`source_name`, absolute `source_result_path`, `source_result_sha256`, absolute
`prompt_path`, `prompt_sha256`, and absolute `result_path`. Derived formatter/
scribe preparation reuses that frozen run observation and never discovers again.
`LoopRoleContext` is an immutable typed record whose schema/validation is owned
by `crew-state.py` and whose instance is assembled by the loop workplan. Its
exact fields are `host`, typed `lifecycle_context`, current
`observed_harness_version`, `session_id`, `loop`, `loop_instance_id`, `round`,
absolute contained `loop_artifact_root`, `resolved_execution`, frozen
`route_stamp`, exact canonical `workspace`, `head`, `index_tree`, and `branch`
(the same names and normalization as `ContinuationBinding`),
`role_catalog_version`, `resume_executor`, nullable selected `continuation_id`,
absolute `prompt_path`, `prompt_sha256`, and absolute `result_path`. The owning
advisor/executor prep reobserves the harness exactly once, before resolution,
then freezes and propagates that value.
Neither context constructor nor either role preparer reads ambient environment
or performs hidden/double discovery.

Top-level panel/debate prep discovers once and freezes the observation for the
run; derived run roles reuse it. Owning loop-role prep reobserves once. These
observations enter the capability context passed explicitly to `resolve_seat`
and propagate unchanged through the returned contract.
`channels.active_capabilities(host, lifecycle_context,
observed_harness_version)` is the sole workplan capability-assembly owner; it
does not discover a version or resolve a seat.

`PreparedPanel` exposes one ordered `work_items` list rather than parallel
Task/subprocess arrays. Each item contains exactly the fields used by a host
adapter:

```json
{
  "kind": "seat",
  "name": "opus",
  "source_name": null,
  "scope": "run",
  "run_id": "run-123",
  "run_dir": "/fixture/.crew/reviews/run-123",
  "loop": null,
  "loop_instance_id": null,
  "round": 1,
  "host": "claude",
  "requested_model": "opus",
  "request_kind": "family_alias",
  "channel": "claude",
  "driver": "native",
  "access": "read-only",
  "prompt_path": "/absolute/frozen/prompt-opus.txt",
  "prompt_sha256": "<64 lowercase hex>",
  "role_id": "crew:reviewer",
  "role_sha256": "<64 lowercase hex>",
  "continuation_id": null,
  "result_path": "/fixture/.crew/reviews/run-123/opus.json",
  "operation_key": "<deterministic run/round/seat-or-role key>",
  "harness_version": "<observed normalized host version>",
  "external_client_version": null,
  "timeout_seconds": 600,
  "deadline_epoch": 1787012345.0,
  "cancel_grace_seconds": 10,
  "pending": true
}
```

`WorkItem` is role-neutral: `kind` plus `name` replace any seat-only identity.
It always carries nullable `continuation_id`; the value is null for every
reviewer, panelist, formatter, scribe, and advisor, and may be non-null only for
an admitted build executor under the frozen continuation policy.
Every result-bearing item has an absolute normalized `result_path` produced by
the engine and contained by `run_dir` or the loop's ignored session artifact
root. Review seats land at `<run_dir>/<validated-seat>.json`; debate seats land
at `<debate_dir>/round-<n>-<validated-seat>.json`. Run formatter/scribe items
use `<run_dir>/repair-<validated-source-seat>.md` and
`<run_dir>/tmp-host-result-<validated-source-seat>.json`; run-role preparation
therefore requires `source_name`. Run-less advisor/executor exchanges use
`.crew/reviews/.sessions/<session-id>/loops/<loop-instance-id>/round-<n>/
<role>.exchange.json`. Resolve each path, reject symlink/escape/collision, and
store the exact destination in the immutable work item. `collect`, quorum, and
debate settlement read the normalized provider-result JSON at seat destinations
and take review prose from its `text` field; formatter Markdown and synthesized
panel/debate reports remain separate artifacts and are never seat-result paths.

Legal preparation pairs are exhaustive:

| Scope | Kind/name | Preparation owner |
|---|---|---|
| review/debate run | `seat/<configured-seat>` | `prepare_panel` / `prepare_debate_round` |
| review run | `role/formatter`, `role/scribe` | `prepare_run_role` with source seat |
| debate run | `role/scribe` (and formatter only if Phase-0 inventory proves a debate repair consumer) | `prepare_run_role` with source seat |
| measure-twice loop | `role/advisor` | `prepare_loop_role` |
| build loop | `role/executor` | `prepare_loop_role` |

Table tests cover every legal pair and reject every other scope/kind/name/source
combination. `PreparedExecutor` contains a complete executor `WorkItem`, not a
partial route record. The shared workflow remains the sole post-reservation
assembler and copies this complete item mechanically into `SpawnRequest`.

The shared workflow is the sole owner of assembling `SpawnRequest`: it takes
prepared scope/identity plus one complete `WorkItem`, verifies the catalog-pinned
installed role, obtains the just-in-time reservation JSON only after that check
passes, and mechanically copies those fields plus the returned operation ID. It
performs no model, route, access, timeout, prompt, host, or version inference;
adapters accept only that assembled immutable request.

The top-level prep payload keeps the current run facts (`run_dir`, `run_id`,
`target_sha256`, `session_segment`, `host`) and adds `work_items`. During Phases
2-5 it also emits the current `subprocess_seats`, `task_seats`,
`task_seat_models`, prompt-path, and pending-list keys, mechanically derived from
`driver=external|native`, so existing review/build/measure commands remain
functional until their wrappers migrate. Phase 6 deletes the bridge only after
all consumers move. The adapter partitions only by declared `driver`; it never
classifies a seat name or channel.

`PreparedDebateRound` is separate because debate does not use a frozen review
run. It contains `debate_dir`, validated `run_id`, `round_number`,
`question_path`, nullable `prior_round_path`, and ordered `work_items` with each
round-specific prompt/result path. `prepare_debate_round` uses `rounds.py` to
validate/create the debate directory, read rounds `1..n-1`, and thread that text
through prompt rendering; round completion writes the synthesized record through
`rounds.write_round`, which is the sole input to the next round. It never reuses
review-run pointers or review snapshots.

`debate-prep` is the orchestration CLI and emits exactly these authoritative
fields: `debate_dir`, `run_id`, `round_number`, `question_path`, nullable
`prior_round_path`, `host`, `harness_version`, and ordered `work_items` using the
same `operation_key`, route, model/request-kind, access, prompt, and timing shape
as panel work. Through Phase 5 it additionally emits mechanically derived
`subprocess_seats`, `task_seats`, `task_seat_models`, prompt-path, and pending
keys solely for compatibility. Existing `seats --debate --json` remains a
roster/config compatibility query through Phase 5 and does not prepare a round.
Phase 6 deletes its Task/subprocess split keys after consumer scans/tests pass,
while retaining the host-neutral roster query.

Debate reservations always use the run form:
`crew reserve-operation --session-id <id> --run-id <debate_run_id> --run-dir
<absolute_debate_dir> --round <n> --operation-key <debate_key>`. Before touching
the ledger, `rounds.py` validates that run ID/directory/round identity; the
ordinary mutable `operations.json` lives inside `debate_dir` and is removed with
that debate run. Debate never uses the loop-state reservation form.

`debate-prep` also atomically creates immutable
`<debate_dir>/round-<n>-manifest.json` before launch. It records session/run/
round identity, question and prior-round hashes, ordered roster, and for every
seat the full execution signature, prompt hash, operation key, absolute
contained `result_path`, and pending state. Re-prep may reuse only byte-identical
facts; conflicting manifest bytes fail closed. Debate seat exchanges land only
through:

```text
crew persist-debate-seat <seat> --session-id <id> --run-id <id>
  --run-dir <absolute_debate_dir> --round <n>
  --host-result-file <absolute HostExchange path>
  --expected-sha256 <64-lowercase-hex>
```

Before parsing, the command uses Python `hashlib` to verify the contained file
against the parent's required `--expected-sha256`; that digest is stored with
the operation and participates in replay/conflict identity. The command then
validates the immutable manifest identity, reserved operation,
host/version/route/model/access/role bytes, and exact contained result path.
Same-operation replay with the same accepted digest is idempotent; a changed
digest/replay, second operation, path
escape/collision, or cross-run/round/seat input exits 2. Usable terminal output
lands with exit 0, unusable terminal output with exit 4, malformed/untrusted
input with exit 2; a terminal seat has zero retry. All seat exchanges must be
landed/settled against the manifest before `rounds.write_round` may synthesize
and seal the round used by the next prep.

New `run.json` records write an ordered `roster` plus a per-seat `executions`
map (`kind`, `name`, `requested_model`, `request_kind`, `channel`, `driver`,
`access`, prompt/role hashes, contained `result_path`, nullable
`external_client_version`), the orchestration `host`, and the
observed `harness_version` plus frozen target/identity facts. The execution map
and observed harness version are run-signature inputs.
The immutable prep record cannot know an actual model reported after launch.
`ProviderResult.model` remains the requested model pin so current callers and
serialized contracts keep their meaning; add optional `reported_model` and
route-provenance fields instead of repurposing it. Optional fields are omitted
from serialized provider results when null, and `from_dict` accepts both their
absence and the additive current shape. Model mismatch is a labeled failure.
Readers normalize legacy records with this
exact mapping: `kind=task -> driver=native`, channel from `seat_channels` or
legacy fallback `claude`; `kind=subprocess -> driver=external`, channel from
`seat_channels` or `provider` through `LEGACY_PROVIDER_TO_CHANNEL`; legacy
`model -> requested_model`. New `run.json` writers emit only the new record
shape; only the temporary CLI payload bridge emits both interfaces.

`PreparedExecutor` keeps the current `executor`, `source`, `retries`, and
`resume_executor` fields and contains the complete loop-role executor `WorkItem`
carrying those resolved execution facts, including its nullable
`continuation_id`; it has no second continuation field or lookup. The
`crew:executor` sentinel remains the built-in selection value for compatibility,
but is resolved only through:

```text
resolve_builtin_executor(host, lifecycle_context) -> ResolvedExecution
```

This is separate from seat `via` resolution. It discovers the current harness
version, selects that host's native channel/driver, requires workspace-write and
enforced executor role/tool scope, and supplies a lifecycle-proven concrete
requested model plus `request_kind`, or a version-bound guaranteed model/family.
Missing/unsupported model evidence, generic-role substitution, or insufficient
scope rejects `crew:executor`; it never guesses an alias or falls back to an
external seat. The returned host/version/channel/driver/access/requested-model/
request-kind fields are stamped exactly into the new loop route. A configured
seat may still resolve native or external through ordinary ordered `via` when
its execution supports workspace write.

### 3. Shared workflow module

Create source skills under `plugins/crew/skills/`; all three plugin manifests
install the same files. The public interface is the skill set:

```text
crew:review
crew:debate
crew:measure-twice
crew:build
crew:status
crew:cancel-build
crew:cancel-measure-twice
```

Use this layout:

```text
plugins/crew/skills/
  review/SKILL.md
  debate/SKILL.md
  measure-twice/SKILL.md
  build/SKILL.md
  status/SKILL.md
  cancel-build/SKILL.md
  cancel-measure-twice/SKILL.md
  _shared/
    workflow-contract.md
    workflows/panel-review.md
    workflows/debate.md
    workflows/measure-twice.md
    workflows/build.md
    roles/advisor.md
    roles/executor.md
    roles/reviewer.md
    roles/panelist.md
    roles/scribe.md
    roles/formatter.md
    hosts/claude.md
    hosts/cursor.md
    hosts/codex.md
```

The shared workflow files own phase order and prompt handling, and sequence
Python normalization, repair, collection, quorum, state, and cleanup commands.
Python commands are exclusively authoritative for persistence, validation,
quorum arithmetic, state transitions, and exit status; Markdown never mutates or
recomputes those facts and only branches on command output. Role files own role
behavior. Each public `SKILL.md` is a short entry
point that loads its workflow and the adapter matching the prep payload's
`host`.

`skills/_shared/roles/*.md` becomes the canonical role prose. Existing
`plugins/crew/agents/*.md` and any proven `agents-cursor/*.md` keep only
host-specific frontmatter/tool declarations plus a thin directive to load the
matching canonical role; they do not retain copied role bodies. Phase 0A's
installed shared-file probe must prove a public `SKILL.md` can read
`../_shared/...` and a thin agent can read `../skills/_shared/...` from cached
plugin bytes before this conversion lands.

Before any native spawn, run engine-owned
`crew verify-role --installed-plugin-root <absolute> --role-id <id>
--role-sha256 <digest>`. The committed/versioned
`plugins/crew/skills/_shared/role-catalog.json` is the authority and has exact
top-level keys `schema`, `plugin_version`, and `roles`; each `roles[role_id]`
contains only `path` (a contained path relative to installed plugin root under
`skills/_shared/roles/`) and `sha256` (64 lowercase hex). Prep
copies that versioned catalog-pinned digest; it never hashes installed bytes to
create or update the expected value. Python `hashlib.sha256` verifies installed
bytes and path containment against the pin. The prepared `WorkItem` supplies the expected ID/digest,
and both fields copy unchanged through `SpawnRequest`, `HostHandle`, and
`HostResult`. The shared workflow runs `verify-role` before reserving an
operation. Success exits 0; an unknown ID, escaped catalog path, missing file,
or digest mismatch exits 2 with no reservation/attempt consumed and no
`HostHandle` or `HostResult`. Persistence revalidates the carried catalog pair,
but role verification failure is not synthesized as a host result. LLM echo or
self-report is never role-loading evidence.
Any canonical role edit must update the catalog digest in the same intentional
commit and pass source/package/installed-byte/version-bump consistency tests.
Phase 2 lands the catalog parser/verifier with contract fixtures; Phases 3, 4,
and 5 add each role entry in the same commit that adds that canonical role, and
prep cannot advertise an entry before it exists in the installed catalog.

The host-adapter interface is concrete and asynchronous so parallel fan-out,
correlation, timeout, and cancellation have one meaning:

```text
spawn_role(request: SpawnRequest) -> SpawnOutcome
spawn_native_seat(request: SpawnRequest) -> SpawnOutcome
await_host(handle, deadline_monotonic) -> HostResult
cancel_host(handle) -> HostResult
```

`SpawnOutcome` is exactly `handle: HostHandle` plus nullable
`terminal_result: HostResult`. Before asking the host to create a child, the
adapter deterministically constructs the handle from the immutable request with
`started_at` set and `child_id=null`. A successful host create returns the same
stable handle facts with its reported child ID and `terminal_result=null`; that
handle alone may be awaited. A host-declared child-create failure returns the
preconstructed null-child handle plus one matching terminal
`status=failed/failure_code=spawn_failed` result, which the workflow immediately
wraps as `HostExchange` and persists without await. An adapter/tool/transport
crash before it returns a valid `SpawnOutcome` is workflow exit 2 with no
invented handle/result, no claimed seat, and no persistence; orchestration never
converts its own exception into `spawn_failed`.

`SpawnRequest` is an immutable complete launch record. It contains every field
that must be copied unchanged into the pre-spawn portion of `HostHandle`:
`operation_key`, reserved `operation_id`, `scope=run|loop`, nullable `run_id`
and absolute `run_dir`, nullable `loop`, `loop_instance_id`, `round`, `host`,
observed `harness_version`, `kind=role|seat`, `name`, `requested_model`,
`request_kind`, `access`, `channel`, `driver`, `timeout_seconds`,
`deadline_epoch`, `cancel_grace_seconds`, and nullable
`external_client_version`. It additionally contains an
nullable `source_name`, absolute contained `result_path`, absolute `prompt_path`
plus `prompt_sha256`, installed canonical `role_id` plus
`role_sha256`, and optional exact continuation
identity; continuation must be null except for an admitted implementation
executor. The shared workflow mechanically copies `continuation_id` from the
complete `WorkItem`; the adapter may not look it up or recreate it. The adapter
adds only `started_at` and nullable `child_id` to make the handle. It never fills
a missing request fact from ambient context.
`cancel_grace_seconds` is the fixed constant `10` in this change; it is not a
config, CLI, capability, or adapter-tuning field, and any other value is invalid.

Operation keys never contain an attempt suffix. Their grammar is
`review/<run_id>/round/<n>/seat/<seat>`,
`review/<run_id>/round/<n>/role/<formatter|scribe>/source/<source-seat>`,
`debate/<run_id>/round/<n>/seat/<seat>`, or
`debate/<run_id>/round/<n>/role/scribe/source/<source-seat>` (plus
`/role/formatter/source/<source-seat>` only when the positive inventory enables
debate repair), or
`loop/<loop_instance_id>/round/<n>/role/<advisor|executor>`; reject any other
role/key pairing, missing source segment, or source that is not an exact member
of the owning immutable roster/round manifest. Reviewer/panelist work uses the seat form. Review/debate seats,
formatter, scribe, and advisor use the effective panel timeout (current default
600 seconds/config override); executor uses the effective dispatch timeout
(current default 1800 seconds plus provider floor). Python prep owns those
sources and the adapter cannot replace them.

Spawn returns `SpawnOutcome`; only an outcome without `terminal_result` proceeds
to await, while an outcome with the create-failure terminal is persisted
immediately. Await/cancel return the inner `HostResult`. The
engine-owned persistence object is named `HostExchange` and has the exact
top-level object below; no bare result or extra top-level key is accepted. Field
names/types are normative; the displayed IDs, paths, timestamps, versions, and
model values are non-normative examples:

```json
{
  "handle": {
    "operation_id": "review/run-123/round/1/seat/codex/attempt/1",
    "operation_key": "review/run-123/round/1/seat/codex",
    "scope": "run",
    "run_id": "run-123",
    "run_dir": "/fixture/.crew/reviews/run-123",
    "loop": null,
    "loop_instance_id": null,
    "round": 1,
    "host": "codex",
    "harness_version": "1.2.3",
    "external_client_version": null,
    "kind": "seat",
    "name": "codex",
    "source_name": null,
    "result_path": "/fixture/.crew/reviews/run-123/codex.json",
    "requested_model": "gpt-5.6-sol",
    "request_kind": "exact",
    "access": "read-only",
    "role_id": "crew:reviewer",
    "role_sha256": "<64 lowercase hex>",
    "continuation_id": null,
    "child_id": "child-123",
    "started_at": "2026-08-18T20:00:00Z",
    "timeout_seconds": 600,
    "deadline_epoch": 1787012345.0,
    "cancel_grace_seconds": 10,
    "channel": "codex",
    "driver": "native"
  },
  "result": {
    "operation_id": "review/run-123/round/1/seat/codex/attempt/1",
    "harness_version": "1.2.3",
    "external_client_version": null,
    "status": "succeeded",
    "text": "review output\n",
    "child_id": "child-123",
    "failure_code": null,
    "error": "",
    "requested_model": "gpt-5.6-sol",
    "reported_model": "gpt-5.6-sol",
    "role_id": "crew:reviewer",
    "role_sha256": "<64 lowercase hex>",
    "elapsed_seconds": 1.25
  }
}
```

`HostHandle` preserves the request's operation/scope/run-or-loop/round, host,
version, kind/name, requested-model/request-kind, access, timeout/deadline/
cancel-grace, route, role ID/digest, nullable external-client-version, and nullable
source/continuation identities plus contained result path, and adds nullable `child_id` and RFC3339
`started_at`. `operation_id` is unique and stable for
`run-or-loop/round/seat-or-role/attempt`; the attempt counter lives in a mutable
operation ledger, increments immediately before an actual spawn/retry call, and
is never written back into immutable `run.json`. Panel/debate runs use an
atomically updated, locked `operations.json` beside their immutable run/round
record. It is ordinary run-local bookkeeping: status does not treat it as a
result, and existing run cleanup/swabbing deletes it with its owning run rather
than retaining or migrating it separately. Persistent loops use an additive `operation_attempts` map in their
already mutable `LoopState`. A parse/lock/write failure stops before spawn, so an
attempt cannot be launched without its identity being reserved. It correlates parallel results even
when a host never exposes a child ID. `child_id` may remain null only when the
current host capability evidence says that harness exposes none.
Review/debate freshness uses distinct child IDs when the host exposes them. When
it does not, admission requires a version-bound live proof that each exact spawn
creates fresh context, plus distinct operation IDs, null continuation, and an
observed fresh spawn per round. Without either proof branch, that host's native
review/panelist route is unavailable and the work remains external-only.
The prepared work item carries only `operation_key`. Immediately before actual
native review/debate/advisor work, the shared workflow calls the deterministic
engine's applicable `reserve-operation` command, which atomically increments
the ledger and returns the full `operation_id`. Native executor work instead
calls `state begin-role-spawn`, which returns the same reservation facts while
also performing its continuation transaction. The returned ID is passed to the
sole Markdown host adapter and returned in its handle. Prep therefore remains
zero-spawn and cannot consume an attempt for work that is never launched.

Reservation has two mutually exclusive CLI contracts:

```text
crew reserve-operation --session-id <id> --run-id <run_id>
  --run-dir <absolute_run_dir> --round <n> --operation-key <run_key>

crew state reserve-operation mt --session-id <id>
  --loop-instance-id <id> --round <n> --operation-key <loop_key>
```

The run form requires the run identity/directory and rejects every loop flag;
the state form requires the active measure-twice loop identity and rejects every
run flag or non-advisor key; native `bl/executor` uses the transaction below.
Both validate the attempt-free key grammar above and emit exactly
`{"operation_key":"...","attempt":1,"operation_id":".../attempt/1"}` plus
newline for the first reservation; later reservations use monotonically
increasing positive integer `n` in both `attempt=n` and `/attempt/n`. Malformed identity/key, stale run/loop, lock/read/write
failure, or mixed scope exits 2 without spawn. The run ledger owns all
reviewer/panelist/formatter/scribe attempts, including review rounds inside an
active loop. `LoopState.operation_attempts` owns only run-less advisor/executor
attempts. Generic `state reserve-operation` updates only that map via locked
`models.update_state_json`; its key is not in `AGENT_SETTABLE`, and that generic
RMW may change no other field, especially `stop_fires`/`parked_fires`. External `crew run` performs the same run-scoped
reservation internally immediately before provider launch and stamps the
returned operation ID into its result. Tests race reservations to prove unique
monotonic attempts and preservation of all unrelated state fields.

Native executor launch instead uses one exact Python-owned caller-visible
transaction after `verify-role` succeeds and immediately before the Markdown
adapter spawn:

```text
crew state begin-role-spawn bl --session-id <id>
  --loop-instance-id <id> --round <n> --role executor
  --operation-key <loop/.../role/executor>
  (--expected-continuation-id <id> | --expect-no-continuation)
```

The two expectation flags are required and mutually exclusive; every run flag,
`mt`, advisor, or another role exits 2. The command acquires the loop-state lock
then the `build-executor` continuation lock in that fixed order, and no caller
may acquire them in reverse. While holding both it validates the active exact
loop/round, operation-key grammar, current lifecycle observation and frozen
RouteStamp, role/access/model capability, HEAD/index/branch/workspace guards,
and that the selected continuation record is exactly the expected ID (or absent
for `--expect-no-continuation`). The expectation must exactly equal the prepared
executor WorkItem's nullable `continuation_id`; the command may verify and
tombstone that value but never discovers, substitutes, or recreates it. A
mismatch exits 2 without incrementing an attempt or changing the continuation.

On success it increments `operation_attempts`, reserves the operation ID, and
tombstones only that expected continuation as one recoverable transaction. A
durable `executor_spawn_transaction` in `LoopState` records
`operation_key`, `attempt`, `operation_id`, expected continuation identity, and
`state=prepared|committed`: write `prepared`, invalidate under the held
continuation lock, then atomically mark `committed` before emitting. If the
process stops after `prepared`, a same-input retry under both locks
finishes that transaction and returns the same operation ID without increment;
a conflicting retry exits 2, and no adapter may spawn from `prepared`. Its
canonical compact JSON output is this one line followed by one LF (with `false`
for the explicit absent case):

```json
{"operation_key":"...","attempt":1,"operation_id":".../attempt/1","continuation_tombstoned":true}
```

This is the sole operation allowed to update both `operation_attempts` and
`executor_spawn_transaction`; it does not weaken the generic reservation
command's operation-attempts-only invariant or modify stop/park counters.
`HostResult.operation_id` and `harness_version` must equal its handle.
`spawn_role`/`spawn_native_seat` return only `SpawnOutcome`; `await_host` and
`cancel_host` return `HostResult`. `await_host` is terminal and idempotent at the workflow level: exactly one
`HostResult` is normalized/persisted per handle. `cancel_host` requests host
cancellation and then yields `cancelled`, `timed_out`, or the already terminal
result; a cancellation request is not itself success. Deadline expiry always
settles through this cancel path rather than first manufacturing a timeout
result. The adapter owns one terminal result per operation: the first bounded
cancel settlement freezes its canonical bytes, `await_host`/later
`cancel_host` calls return those byte-identical bytes, and the workflow persists
them once. A late host response cannot replace the frozen terminal.
`succeeded` requires
`text.strip()` to be non-empty and `model_matches(...)` to pass; persistence retains
the original text bytes, including surrounding whitespace. Stable failure codes are
`spawn_failed`, `host_failed`, `timed_out`, `cancelled`, `empty_result`, and
`model_mismatch`. Null host facts remain explicit in this exchange envelope. In
the normalized provider-result record, additive `reported_model` and provenance
keys are omitted when null so legacy consumers retain their current shape.
`reported_model=null` is admissible only when the current
host+channel+driver capability entry guarantees the requested pin for the
running harness version; otherwise a nominal success becomes
`model_mismatch`. This is the sole null rule used by adapters and persistence.

`HostResult.status` is exactly `succeeded|failed|timed_out|cancelled` with a
total invariant: `succeeded` requires usable text, `failure_code=null`, and
`error=""`; `failed` requires one of
`spawn_failed|host_failed|empty_result|model_mismatch` and a
nonblank error; `timed_out` requires `failure_code=timed_out` and a nonblank
error; `cancelled` requires `failure_code=cancelled` and a nonblank error.
Failure/timeout/cancel text preserves any returned bytes but is never usable.
Every other status/code/text/error combination is malformed and persistence
exits 2.

Native review/debate work has zero retry after its reservation/handle exists.
Persist its one terminal result exactly once, including `spawn_failed`; a second
operation for a seat that already has a persisted usable or failed result exits
2 rather than replacing it. Native advisor/executor work also has no implicit
retry in this change. Existing `executor_retries` remains scoped to its current
external configured-provider failure policy and does not authorize a native
retry. A native seat operation never retries, including child-create failure.
A writer/transport retry does not retry the seat: it creates a fresh
source-qualified scribe role attempt for that same seat, reserves the next
attempt number on the scribe operation key, and can only retranscribe the
already frozen canonical `HostExchange` bytes.

Native persistence uses one new engine-owned interface whose `HostExchange`
file schema is exactly `{ "handle": <HostHandle>, "result": <HostResult> }`:

```text
crew persist-seat <seat> --session-id <id> --run-id <run_id>
  --host-result-file <run_dir>/tmp-host-result-<seat>.json
  --expected-sha256 <64-lowercase-hex>
```

The writer copies deterministic JSON bytes and never interprets review text.
Claude retains its write-only scribe unless Phase 0A proves parent-context
ordinary file writes are safe; Cursor/Codex use ordinary file writes only when
their probe proves it, otherwise a verified write-only role. The parent shared
workflow computes the lowercase SHA-256 over its canonical `HostExchange`
bytes and supplies that digest explicitly to persistence. Before parsing, the
engine opens the contained landed file and uses Python `hashlib` to require its
digest to equal `--expected-sha256`; the canonical source bytes are not a
second CLI input, so the plan does not claim a separate byte-for-byte
comparison. The accepted digest is stored with the operation and participates
in same-operation idempotency/conflict checks. The scribe never declares its own digest. If the exact writer plus digest
path is unavailable or unproven, the scribe path is not admitted. On writer/
transport failure or digest mismatch, retry transport exactly once through a separately
proven ordinary writer or fresh scribe; a second mismatch exits 2 and leaves the
seat unclaimed. `persist-seat`
strictly validates handle/result operation-ID equality, host/kind/name, CLI seat
and run equality, run membership, requested model, channel/driver route provenance, current
observed harness version/current capability evidence, and run identity, then maps it to
the normalized provider-result record including `child_id`, `failure_code`, and
`reported_model`, with additive `operation_id`, `harness_version`, and nullable
`external_client_version`. Replaying the same accepted digest
for the same operation is idempotent and returns the original 0/4 outcome;
same-operation changed digest/content, stale run, cross-seat/name, or a different
operation against an existing seat record is rejected with exit 2. Exit 0 means a usable `ok=true` result landed; exit 4 means a
terminal unusable result (`failed`, timeout, cancel, empty, or mismatch) landed
as `ok=false` and panel execution continues; exit 2 means malformed/untrusted
input or identity/path failure and the workflow stops without claiming a result.
The parser has two mutually exclusive modes. Legacy mode accepts current
`-f`/stdin, `--model`, `--elapsed`, `--failed`, `--error`, and retains
`--verify`. Host mode requires `--host-result-file`, `--expected-sha256`, and
seat/session/run,
does not read stdin, intrinsically applies its 0/4/2 verification, and rejects
every legacy content/status/model/elapsed flag including `--verify`.

Loop roles use a separate Python-authoritative ingestion path:

```text
crew state persist-role <bl|mt> --session-id <id>
  --loop-instance-id <id> --round <n> --role <advisor|executor>
  --host-result-file <absolute HostExchange path>
  --expected-sha256 <64-lowercase-hex>
```

It first verifies the contained file against the parent's required SHA-256,
stores that digest as part of operation replay identity, then requires the
active exact loop/instance/round and validates `scope=loop`,
the only legal pair `mt/advisor` or `bl/executor`, contained prepared
`result_path`, reserved operation key/ID,
`role_id`/`role_sha256`, current and stamped route/version/model/request-kind/
access, plus the expected `SpawnRequest`/handle/result identity. Replay of the
same operation and accepted digest is idempotent; changed digest/replay, a second operation,
or any stale/cross-loop/round/path fact exits 2 without mutation. Exit 0 records
a usable normalized role result/artifact; exit 4 records a terminal unusable
role result; malformed/untrusted input exits 2. Advisor continuation is always
null and is never stored. For a native executor, a no-spawn/unavailable outcome
may retain the selected continuation. After role verification, the shared
workflow calls `state begin-role-spawn` instead of generic reservation; the
successful transaction reserves the operation and tombstones the selected
continuation immediately before the adapter's actual spawn. An
attempted spawn that fails, times out, is cancelled, reports a model/role
mismatch, or fails an exact guard leaves it cleared. Only exit 0 may atomically
store the returned continuation ID, and only when `resume_executor=true`, the
resolved capability supports continuation, and the exact loop/RouteStamp/HEAD/
index/branch/workspace guards still match. Every terminal `persist-role`
validates the committed transaction's operation ID and clears only its
`executor_spawn_transaction`; failure leaves the continuation tombstoned,
whereas guarded exit 0 stores the returned ID as it clears the marker. Locked
state updates preserve every other field; tests cover no-spawn retention,
attempted-failure clearing, successful guarded replacement, crash recovery, and
the full 0/4/2/idempotency matrix.

Role operations are fixed:

| Work | Operation | Access/continuation |
|---|---|---|
| selected reviewer or panelist seat | `spawn_native_seat` | read-only, always fresh |
| advisor | `spawn_role` | read-only, fresh |
| implementation executor | `spawn_role` | proven workspace-write, executor continuation policy only |
| formatter | `spawn_role` | read-only, fresh |
| scribe | transport-only `spawn_role` when required by host | transcribes canonical `{handle,result}` bytes only; `persist-seat` remains validation/persistence authority |

The review workflow preserves the existing two-phase barrier:

1. launch every external process and invoke every native spawn before awaiting,
   recording one valid `SpawnOutcome` per returned operation;
2. immediately persist each outcome's create-failure terminal, await only the
   outcomes whose `terminal_result` is null, and persist each resulting terminal
   `HostResult` exactly once;
3. call `crew wait` with **only `driver=external` seat names** (or skip it when
   there are none); and
4. collect only after native results are persisted and every external result
   file exists.

Add deterministic, mutually exclusive trace contracts:

```text
crew trace-phase --scope <review|debate> --session-id <id>
  --run-id <id> --run-dir <absolute> --round <n> --phase <name>
  --native-count <n> --external-count <n> --failed-count <n>
  --repaired-count <n> --continuation-count <n>

crew trace-phase --scope <measure|build> --session-id <id>
  --loop <mt|bl> --loop-instance-id <id> --loop-root <absolute>
  --round <n> --phase <name> --native-count <n> --external-count <n>
  --failed-count <n> --repaired-count <n> --continuation-count <n>
  [--review-run-id <id> --review-run-dir <absolute>]
```

The run form requires every run flag and rejects every loop flag. The loop form
requires every loop flag and rejects every run flag; its child-review pair is
required together only for `review_started` and `verdict_recorded` and is
forbidden on all other phases. Any mixed/incomplete form exits 2 without append.
Python validates owning identity and the next transition, then appends one
canonical JSON object to run-local or loop-root `trace.jsonl`; it is ordinary
bookkeeping removed with that run/loop. The
exact review sequence is seven lines, once each: `prep`, `launch_all`,
`native_persisted`, `external_wait_complete`, `collect_unparsed`,
`repair_complete`, `collect_final`. Zero/waived work still emits its phase with
count zero. Before transition validation, the emitter canonicalizes the proposed
JSON line including its LF and compares it to the current final complete line:
byte-identical replay exits 0 without append. A conflicting duplicate (same
phase/identity with any changed byte), replay of a non-final prior event, or any
other out-of-order event exits 2 without append. Installed Phase-3 tests
assert exact order, line count, run identity, and native/external counts rather
than inferring phase order from prose or adapter logs.

The emitter also requires `--scope` and `--round` and validates these exact
per-round FSMs (each event exactly once):

- debate: `prep`, `launch_all`, `native_persisted`,
  `external_wait_complete`, `results_settled`, `round_written`, `round_complete`;
- measure-twice: `loop_loaded`, `advisor_reserved`, `advisor_persisted`,
  `plan_written`, `review_started`, `verdict_recorded`, `loop_transition`;
- build: `loop_loaded`, `executor_reserved`, `executor_persisted`,
  `review_started`, `verdict_recorded`, `continuation_committed`,
  `loop_transition`.

The review FSM remains the seven events above and is scoped to its immutable
review run. Debate keys by debate run+round; loop FSMs key by loop instance+
round and reference the child review run where applicable. Counts for native,
external, failed, repaired, and continuation are present on every line and use
zero when inapplicable. Wrong scope/round, conflicting duplicate, skipped, or
out-of-order events exit 2 without append. Parameterized tests simulate a crash
after every appended event in every FSM and require its byte-identical retry to
return 0 with unchanged file bytes; changed-count/identity retries return 2.
Phase-4/5 installed tests assert exact JSONL order, count, scope, round, and
zero-count behavior.

After the barrier, preserve the repair interstitial exactly: first
`collect --report-unparsed`. For each named unparsed seat, spawn a fresh native
formatter only when the host/version/role capability admits it. Otherwise the
parent host loads the canonical formatter role inline and formats the RAW text
without creating spawned work or a `HostHandle`. Pass either output to
`repair-seat`, which remains the validation authority. If formatting is
unavailable, fails, or `repair-seat` rejects it, skip repair for that seat and
preserve the RAW/unparsed artifact and label. Then perform the one final grouped/
full collect. Repair is non-destructive and never overwrites raw output. The
external-only fixture must observe zero native handles, including formatter and
scribe handles.

`wait` must not become driver-blind: waiting on a native seat before its
spawn-return is persisted recreates the current Task-seat deadlock.

Python prep owns portable `timeout_seconds`, absolute `deadline_epoch`, and a
separate `cancel_grace_seconds`; no retry resets the absolute deadline. On
await entry, the host adapter validates the handle fields, samples wall and
monotonic clocks together, and derives exactly
`deadline_monotonic = monotonic_now + max(0, min(handle.timeout_seconds,
handle.deadline_epoch - wall_now))`. That exact value is the sole second
argument to `await_host(handle, deadline_monotonic)`; await must not extend it,
apply a second timeout override, or derive another deadline.
Supported Python monotonic clocks can be compared across Python processes on the
same machine; the reason not to serialize them here is the broader host/runtime
and restart boundary, not a same-process restriction.
Expired or unparseable values fail closed before spawn/await. Panel work
preserves the current 600-second default/config override, dispatch preserves
1800 seconds and provider floors, and loop `deadline_minutes` remains the
independent hook-owned wall-clock bound. After cancellation, `cancel_host`
derives a new local settlement bound capped by `cancel_grace_seconds`, persists
the one frozen terminal cancellation/timeout described above, and never waits
indefinitely or extends the operation deadline for more work.

It also defines the harness-specific dispatcher-root recipe and hook serializer.
External work items continue through visible, individually killable `crew run`
or `crew dispatch` processes launched by the shared workflow outside the native
host adapter. All persistence still crosses the existing CLI
interface (`persist-seat`, `collect`, `state ...`); host adapters do not write
run JSON directly.

The Markdown host adapters are the sole implementation of native
spawn/await/cancel. Do not add a parallel Python native-agent layer. Python is
limited to the enumerated deterministic engine responsibilities: version and
capability resolution, work-plan generation, provider subprocesses, persistence,
normalization, quorum, state transitions, and diagnostics.

### 4. Thin host adapters

- **Claude:** translate `spawn_*` to fresh one-shot Task agents and use the
  existing Claude hook JSON shape. Reuse native model aliases exactly as today.
- **Cursor:** translate `spawn_*` to Cursor subagents and use
  `cursor-hooks.json` response shapes. Add only the minimal Cursor agent
  definitions needed to enforce reviewer/panelist versus executor tool access;
  their task prompt comes from the shared role/workflow files, not copied Claude
  prose.
- **Codex:** translate `spawn_*` to Codex native subagents from the shared Crew
  skills and use the already verified `hooks/hooks.json` behavior. Do not invoke
  nested `codex exec` for a native Codex work item.

ChatGPT one-shot review/debate exposure remains conditional on the Phase-6
universal-plugin/`@` probe. Build and measure-twice must report that persistent
execution requires Codex when plugin hooks are not available. Do not silently
degrade a requested persistent loop into one shot.

The source skill names are plugin-qualified `crew:*`; the existing slash
commands remain `/crew:*`. They intentionally coexist during migration: command
wrappers are compatibility entry points, while their mandatory directive loads
the same-named plugin skill. Tests distinguish the slash-command file from the
qualified skill and reject recursive command invocation.

## Phased file-level implementation

### Phase 0 — Baseline and two independent live-evidence fences

Use the fixed live fixture at
`plugins/crew/scripts/tests/fixtures/multi-harness-live/`. Commit these exact
inputs as UTF-8/LF files with no Markdown display indentation and exactly one
trailing newline:

- `plan.md` bytes: `# Fixture plan\n\nReview only; change no files.\n`;
- `sample.txt` bytes: `before\n`;
- `.gitignore` exact bytes, narrowly matching only Crew's current runtime grammar:

  ```gitignore
  /.crew/reviews/
  /.crew/debates/
  /.crew/hook-payload-keys.txt
  /.crew/build-state.json
  /.crew/build-state-*.json
  /.crew/measure-twice-state.json
  /.crew/measure-twice-state-*.json
  /.crew/build-state.json.lock
  /.crew/build-state-*.json.lock
  /.crew/measure-twice-state.json.lock
  /.crew/measure-twice-state-*.json.lock
  /.crew/build-state.json.corrupt
  /.crew/build-state-*.json.corrupt
  /.crew/measure-twice-state.json.corrupt
  /.crew/measure-twice-state-*.json.corrupt
  /.crew/.build-state.json.*.tmp
  /.crew/.build-state-*.json.*.tmp
  /.crew/.measure-twice-state.json.*.tmp
  /.crew/.measure-twice-state-*.json.*.tmp
  ```

- `.crew/config.toml` exact bytes:

  ```toml
  default_panel = "multi-harness-live"

  [panels]
  multi-harness-live = ["codex", "cursor-auto", "opus"]

  [seats.codex]
  via = ["codex"]
  model = "gpt-5.6-sol"
  available = true

  [seats.cursor-auto]
  via = ["cursor"]
  model = "auto"
  available = true

  [seats.opus]
  via = ["claude"]
  model = "opus"
  available = true
  ```

- `expected.patch` exact mutation oracle bytes:

  ```diff
  diff --git a/sample.txt b/sample.txt
  --- a/sample.txt
  +++ b/sample.txt
  @@ -1 +1 @@
  -before
  +after
  ```

- `expected.json` exact semantic oracle (serialize with two-space indent and one
  trailing newline):

  ```json
  {
    "roster": ["codex", "cursor-auto", "opus"],
    "requested_models": {"codex": "gpt-5.6-sol", "cursor-auto": "auto", "opus": "opus"},
    "request_kinds": {"codex": "exact", "cursor-auto": "router", "opus": "family_alias"},
    "reported_model_rules": {"codex": "normalized_exact", "cursor-auto": "required_concrete_nonblank", "opus": "concrete_family_or_version_guarantee"},
    "native_when_admitted_by_host": {"claude": "opus", "cursor": "cursor-auto", "codex": "codex"},
    "no_native_admission": {"native_seat": null, "native_handles": 0},
    "read_only_tree_unchanged": true,
    "write_before": "before\n",
    "write_after": "after\n",
    "launched": 3,
    "quorum_required": 2
  }
  ```

- `debate-question.md` exact bytes: `# Fixture question\n\nShould sample.txt change from before to after?\n`;
- `debate-expected.json` exact semantic oracle (two-space indent, one trailing
  newline):

  ```json
  {
    "question": "# Fixture question\n\nShould sample.txt change from before to after?\n",
    "roster": ["codex", "cursor-auto", "opus"],
    "rounds": [
      {"round_number": 1, "prior_round_path": null, "fresh_children": true},
      {"round_number": 2, "prior_round_path": "round-1.md", "fresh_children": true}
    ],
    "round_1_written_before_round_2_prep": true
  }
  ```

  It asserts question bytes, round/path linkage, roster, and freshness only;
  model prose is deliberately not an oracle.

  Every roster member other than the named admitted native seat is external.
  `expected.patch` owns the only permitted workspace mutation bytes;
  `expected.json` owns roster, requested-model, route, native-admission, quorum,
  and before/after semantics. Fixture `via`/`model` rows change only in the same
  commit as an intentional shipped `seats.toml` catalog change, with the mapping
  change called out in the validation evidence. A catalog-drift test fails on
  any unpaired difference; it never auto-copies current catalog values into the
  fixture or silently blesses a model retirement.

Each operator copies the fixture to a fresh `mktemp -d`, creates a separate empty
temporary `HOME`, sets `CLAUDE_PROJECT_DIR` to the fixture root, initializes and
commits Git, and installs the plugin into that host's profile under the isolated
home. Do not inherit a normal host profile. Repository config wins over global
config for the same key, but a key absent from repository config can still leak
from global config; the empty home/profile is therefore part of the oracle, not
just hygiene.

Add the committed fixture helper
`plugins/crew/scripts/tests/fixtures/multi-harness-live/fixture-check.py` as an
exact fixture input; copying the
fixture therefore copies the helper bytes under the same Git guard. Its documented snapshot
recipe runs these exact commands from the fixture root, writing separate
`before/` and `after/` artifacts in a sibling temporary evidence directory
outside `CLAUDE_PROJECT_DIR`: `git rev-parse HEAD`, `git status --porcelain=v1 -z
--untracked-files=all`, `git diff --cached --binary --no-ext-diff`, `git diff
--binary --no-ext-diff`, and `python3 "$CLAUDE_PROJECT_DIR/fixture-check.py" manifest
--root "$CLAUDE_PROJECT_DIR"`. `manifest` emits a path-byte-sorted SHA-256 list
and treats the committed `.gitignore` as the sole runtime-pattern authority: for
each candidate it calls `git check-ignore --no-index -q -- <path>` and excludes
only a match (plus `.git` itself); `.gitignore` is always included and hashed.
The helper contains no duplicate pattern table. Thus status and manifest use the
same exact legacy/session-scoped state, lock, corrupt, atomic-temp, review, and
debate grammar, with no broad `.crew/` or nonexistent `.crew/state` exclusion.
The recipe uses `cmp -s before/<field>
after/<field>` for HEAD, status, cached diff, unstaged diff, and manifest.
Read-only passes only when HEAD, status bytes, staged/unstaged diff bytes,
non-Crew untracked paths, and the file manifest are identical. For the write
probe, run `python3 "$CLAUDE_PROJECT_DIR/fixture-check.py" normalize-diff --input
after/unstaged.diff --output after/normalized.patch`
(which removes only a line beginning `index `), then run `cmp -s expected.patch
after/normalized.patch`; staged diff stays empty, HEAD is
unchanged, and the file manifest differs only at `sample.txt`.

The read-only prompt remains the committed `plan.md`. The write task bytes are
exactly `Replace sample.txt content "before" with "after" and modify no other
path.\n`. A mutation during review fails native read-only admission and moves
that host to `external-only`.

First run the existing suites and record their pass/fail totals in
`plugins/crew/docs/multi-harness-validation.md`:

- `python3 plugins/crew/scripts/tests/test-multiagent.py`
- `python3 plugins/crew/scripts/tests/test-hooks.py`
- `python3 plugins/crew/scripts/tests/test-version-bump.py`

Land the fixture files, `fixture-check.py`, and automated byte/oracle tests as a
Phase-0 baseline subcommit before any attended operator probe. Those tests pin
every exact input, `.gitignore` grammar, manifest exclusions, normalized patch,
and raw-status comparison; an operator gate cannot start from an untested helper.
The byte literals in this plan are bootstrap instructions only. Once that
subcommit lands, the committed fixture files are the sole byte authority; later
catalog/oracle changes update the committed files/tests/evidence together and do
not reinterpret bytes from this plan.
Also record a privacy-safe Phase-0 inventory of repository fixtures plus
operator-confirmed live config/run/state shapes (counts and schema/spelling only,
no prompt/result contents). That inventory decides the persistent compatibility
lifetimes below; tests alone do not manufacture a legacy consumer.
The authority is committed
`plugins/crew/docs/evidence/legacy-inventory.json` with exact top-level keys
`schema=1`, `observed_at` (RFC3339), `operator` (non-secret identifier), and
ordered `entries`. Each entry has exactly `kind` (`fixture|config|run|state`),
`location_class` (`repository|user-config|session-state|installed-cache`),
nonnegative integer `count`, nullable integer `artifact_schema_version`, sorted
`legacy_spellings` (field/provider names only), `active_status`
(`active|inactive|unknown|not-applicable`), `source` (`repo|operator`), and
`decision` (`retain|delete|bridge-only`). It records no absolute/home paths,
tokens, prompts, results, or config values. Schema/golden tests reject unknown
keys, secret-looking path/content fields, inconsistent counts, or an operator
entry without the top-level operator/timestamp provenance.

These live fences are operator-run and evidence-heavy. When evidence is missing,
stale, or contradictory, the executor stops at the gate and reports the exact
missing row; it must not fabricate fixtures, capability entries, or host support.

After the automated baseline, host-neutral dataclasses, compatibility readers, fake
capabilities, and their tests in Phases 1-2 may be developed. No real native
capability entry, native host adapter, or support claim may land until its
applicable fence passes. Phase-1/2 tests use the typed fake lifecycle/spawn
contracts; production host constructors remain unreachable until that host's
Phase 0A gate passes.

#### Phase 0A — Read-only, packaging, and forwarding fence

Add reproducible procedures—not ad hoc notes—at:

- `plugins/crew/docs/probes/native-readonly.md`;
- `plugins/crew/docs/probes/plugin-packaging.md`;
- `plugins/crew/docs/probes/command-skill-forwarding.md`; and
- `plugins/crew/docs/multi-harness-validation.md` for the result table.

Add `plugins/crew/docs/evidence/multi-harness.schema.json` as the
machine-checkable evidence contract and versioned per-host JSON records under
`plugins/crew/docs/evidence/{claude,cursor,codex}/`. Tests validate every record,
unique `probe_id`, referenced artifact/transcript existence, harness/client
version fields, tier, status, and fixture hash. The Markdown validation table is
rendered from these JSON records and is never the evidence authority.
The schema requires `probe_kind` and uses `oneOf` branches to make fields
conditionally required, nullable, or forbidden. Operation/model probes require
operation identity, requested/reported model, route, and access; lifecycle
probes require session/project/version facts; packaging and malformed-input
probes require their artifact/error facts and forbid operation/model fields.
Schema tests include one valid and each missing/forbidden-field negative per
branch, so a non-operation row never invents an operation ID to validate.

Record host-specific evidence in `claude-host.md` (add it), `cursor-host.md`, and
`codex-host.md` for:

1. a fresh read-only child and exact returned text;
2. two parallel children, unique handle/child IDs, out-of-order completion, and
   correct correlation;
3. requested versus reported model;
4. spawn failure, empty result, bounded await, timeout, cancel-by-handle, bounded
   ten-second settlement, and late-result rejection through the exact host
   interface, mapped to `HostExchange`/`HostResult`;
5. plugin-root/dispatcher discovery from an installed source skill;
6. a throwaway command wrapper mandatorily loading a source skill and forwarding
   tagged arguments containing spaces, quotes, `--panel`, and `--seats` without
   loss or reinterpretation;
7. installed-byte shared resolution: a skill reads `../_shared/marker.txt` and a
   thin agent reads `../skills/_shared/marker.txt`, and deterministic role ID/
   digest verification succeeds, while missing/changed bytes exit 2 before
   reservation with no attempt, handle, result, or spawn;
8. native `HostResult` ingestion through `persist-seat --host-result-file`
   plus the parent-computed `--expected-sha256` for success and every terminal
   failure status, including digest rejection, exact exit mapping, and landed
   fields;
9. lifecycle payload delivery plus the disposable-repository read-only guard
   above (Stop continuation behavior is Phase 0B);
10. the exact literal `CREW_HOST=cursor` dispatcher assignment through Cursor's
    real command approval/permission path. Keep the assignment only if the
    structural wrapper assertion and installed live invocation both pass; do
    not add a new `--host` flag;
11. `discover_harness_version` from two real lifecycle contexts per host plus
    missing/malformed negative controls. Record the raw source and normalized
    value and prove an installed external client version is not used unless that
    host's evidence explicitly establishes equivalence. Also prove which exact
    new/resume/compaction lifecycle events refresh the filtered record; and
12. a fenced, committed canonical host-contract transcript for the observed
    harness version containing exact `SpawnRequest`, spawn call, returned
    `HostHandle`, await success, cancel, timeout, and terminal `HostResult`
    snippets plus raw host transcript references. These snippets are the only
    mechanics Phase 3 may copy into that host's Markdown adapter.

Probe 8 is a forward dependency: Phase 2 may implement host-result ingestion
while Phase 0A is open, but Phase 0A cannot close and no native adapter may be
admitted until that Phase-2 path has landed and the live ingestion rows pass.

Required sequence: committed Phase-0 baseline -> Phase-2 host-result persistence
-> live Phase-0A probe 8 -> close that host's Phase 0A -> Phase-3 adapter slice.

Current OpenAI documentation establishes the Codex baseline:
`plugins/crew/.codex-plugin/plugin.json`, `skills: "./skills/"`, repository-root
`.agents/plugins/marketplace.json`, and default discovery of `hooks/hooks.json`.
Create a throwaway plugin outside the repository and verify those documented
paths with the current CLI, shared-file access, install command, and cache bytes;
record the result in `codex-host.md`. A live failure stops Codex packaging work
and triggers documentation re-check rather than inventing another layout.

Capture a second independent Cursor agent-shell environment. If a distinctive
non-empty exact marker is stable across both captures and absent from the
Claude/Codex fixtures, add it to both `multiagent/channels.py` and
`scripts/host_detect.py`. Otherwise keep detection conservative and make the
Cursor skill adapter bind `CREW_HOST=cursor` for each dispatcher invocation;
do not depend on a launch-shell export that Cursor scrubs. Claude's documented
permission wildcards spanning command prefixes do not establish Cursor approval
behavior; the Cursor literal-assignment row remains live evidence.

Update `test-hooks.py` fixtures only with verified marker, payload, packaging,
and forwarding facts. Do not promote a requested model or permission into
`host_capabilities.py` unless the host reported or guaranteed it.

**Read-only gate:** a host may receive native read-only capability entries and a
Phase 3 review adapter only after it produces correlated `HostResult` envelopes,
live-proven bounded await/cancel-by-handle/settlement, portable dispatcher
discovery, and successful installed command-to-skill
argument forwarding, HostResult persistence, shared-file resolution, and a clean
Git/tree guard. A host that fails native spawn/model/scope evidence may still be
`external-only`: its commands/skills and engine run the same roster entirely
through installed clients, with zero native work items; native-specific Phase 3
rows are waived but wrapper, frozen target, persistence for external results,
repair, collect, and quorum rows still pass. Phase 1's generic resolver and Phase
2's normalized record/work-item code may land earlier using fake capabilities;
enabling native mapping may not.

Evidence and landing are host-independent:

| Host row | If Phase 0A version/read-only probes pass | If any required row fails |
|---|---|---|
| Claude Code | its proven native read-only capability and Phase-3 slice may land | Claude remains external-only |
| Cursor | its proven native read-only capability and Phase-3 slice may land | Cursor remains external-only |
| Codex | its proven native read-only capability and Phase-3 slice may land | Codex remains external-only |

One passing host never re-admits another. The shared engine/wrappers must still
pass the all-external rows for every host whose native row remains open.
A solo operator may complete and land these host rows serially; parallel access
to all three harnesses is not a gate requirement.
Minimum native success is one Phase-0A-admitted host: that host's shared/native
Phase-3 slice may proceed while failed/unprobed hosts remain external-only under
their waiver rows. If no host passes Phase 0A, stop before implementing any
native Phase-3 adapter and re-scope explicitly; external wrappers/engine behavior
may still be documented, but fake native adapters cannot stand in for evidence.

#### Phase 0B — Workspace-write and persistence fence

Add procedures at:

- `plugins/crew/docs/probes/native-write.md`;
- `plugins/crew/docs/probes/loop-persistence.md`; and
- the shared result table in `multi-harness-validation.md`.

For each host, use the disposable fixture copy to verify:

1. an executor role with host-enforced tool/role scope edits only `sample.txt`;
2. cancellation, timeout, child failure, and before/after Git guards;
3. whether a native executor can be identified and correlated without silent
   generic-role substitution; and
4. Stop-hook continuation through at least three genuine stop attempts, exact
   session state mutation, cancel, and deadline/stop-fire termination.
5. Keep a `host_recipes` emitted-continuation row explicitly `open` during
   Phase 0B. Phase 4 closes it only after the real `host_recipes.py` output runs
   successfully through the installed host Stop path; Phase 5 cannot start for
   that host while the row is open.

**Write gate:** a host receives native workspace-write capability only when the
role/tool-scope probe passes. A host joins the `full-persistence` tier only when
the Stop continuation probe passes. Phase 5 may use external write executors on
a host that lacks native write scope, but it must reject the native
`crew:executor` sentinel there. Phase 5 production enablement and persistence
claims wait on this fence; Phases 1-4 read-only work do not.

### Phase 1 — Deepen ordered route resolution without changing defaults

1. In `multiagent/seats.py`:
   - allow one or more unique known entries in `via`, preserving order;
   - keep legacy `provider -> one-element via` translation only when the Phase-0
     inventory found a live config; otherwise schedule its Phase-6 deletion;
   - continue rejecting `provider` plus `via`, empty lists, unknown channels,
     unsafe seat names, and invalid channel/model combinations;
   - leave shipped `seats.toml` entries one-element so default cost/routing is
     behavior-identical.
2. In `multiagent/channels.py`:
   - replace native-preference selection with first-viable-in-`via` order;
   - make explicit `driver` and requested access/continuation capability facts
     authoritative. Retain `native` and `engine_runnable` temporarily as derived
     compatibility properties, never independent routing inputs, until the
     Phase-2 source scan and consumer tests prove no caller remains; remove them
     only at that no-consumer gate;
   - map Claude, Cursor, and Codex hosts to their matching native channel;
   - keep `agy` external-only;
   - fail closed for a failed `model_matches`, missing/stale harness version,
     unsupported access mode, or absent client;
     continuation is returned as a fact and is never review admission;
   - never retry resolution after launch failure.
3. Add `multiagent/host_capabilities.py`, including
   `discover_harness_version` and `model_matches`, and feed its Phase-0A/0B-proven native
   facts as `capabilities` while passing the one observed lifecycle version in
   the explicit resolver argument. In `multiagent/providers/__init__.py` and
   the four existing provider modules, adapt external availability/write/
   continuation facts to the same host+channel+driver-keyed lookup. Do not move
   provider-specific CLI construction out of those modules and do not create a
   dynamic route registry. A host-native entry must never reuse an external
   provider's capability object.
4. Extend `test-multiagent.py` with table-driven cases for every host/channel
   pair, multi-entry ordering, unavailable-first fallback, same-channel
   native-inadmissible-to-external fallback, later-native ordering, and the full
   model matrix: exact normalized match/mismatch, Cursor router concrete/blank/
   alias-only reports, Claude family concrete/mismatch/null-guarantee cases, and
   upstream-retirement failure. Add version discovery/normalization for each
   host, external-CLI non-substitution, missing/malformed/stale fail-closed
   diagnostics, read/write gates, continuation reporting (including a review that
   succeeds without it), and the
   no-runtime-reroute rule. Include a non-vacuous divergence case where native
   Codex is read-only while external Codex is write-capable, plus the inverse,
   to prove the key includes host and driver. Add an explicit all-host `agy`
   row proving it can resolve only `driver=external` and has no native
   capability key. Retain the existing `council` resolver tests as regression
   coverage and negative controls proving
   resolver-consuming commands do not reintroduce provider-name or Task-seat
   oracles.
   Add version-keyed external stream-parser fixtures for Cursor/Claude init
   models, Codex/Agy exact-model null guarantees and invalid-model negatives,
   parser drift, and requested-model overwrite prevention. Include a test-only
   cross-named seat `alpha` with `via=["codex"]` to prove routing/parser behavior
   comes from the channel, never a seat-name convention.
5. Add a commented, non-default multi-entry example to the doctor/init-generated
   config template, such as `via = ["claude", "cursor"]`, explaining channel
   order and same-channel native/external behavior. Shipped roster defaults stay
   one-element; the template and live fixture are the real configuration
   consumers.

**Gate:** all existing one-entry configurations resolve exactly as before; a
multi-entry test proves the first viable configured channel wins even when a
later channel is native. Generic/fake-capability code may pass before Phase 0A;
no real host-native entry is enabled until that host's applicable evidence gate.

### Phase 2 — Replace the Task/subprocess work-plan contract

1. Add `multiagent/workplans.py` with `PreparedPanel`, `PreparedDebateRound`,
   `PreparedExecutor`, role-neutral `WorkItem`, `prepare_debate_round`, and the
   run/loop role preparation implementations described above. Phase 2 owns the
   complete `debate-prep` CLI JSON/manifest contract. Move roster resolution,
   availability filtering, de-duplication, channel resolution, and driver
   partition facts out of `cli.py`; keep CLI parsing/output in `cli.py`.
   `multiagent/review_runs.py` owns the locked mutable panel/debate
   `operations.json`; immutable `run.json` remains untouched by attempts.
2. Update `multiagent/cli.py`:
   - make `review-prep` and the new orchestration `debate-prep` emit the uniform work-item
     contract plus the temporary mechanically derived legacy split/pending/model
     keys through Phase 5;
   - keep `seats --debate --json` as a roster/config compatibility query through
     Phase 5; it never allocates round paths or operations;
   - stage a prompt path for every item and retain atomic frozen-snapshot writes;
   - add `reserve-operation` for run work and run-less advisor work: validate the
     run/loop/round/key, atomically increment immediately before spawn, and emit
     the full operation ID without launching anything. Native executor work is
     deliberately excluded and uses `crew-state.py`'s transaction below;
   - add `trace-phase` with the exact run-local JSONL transition contract above;
   - make `run`, `persist-seat`, `persist-debate-seat`, `repair-seat`, and `collect` consume seat
     names/run IDs without classifying drivers; keep `wait` explicitly scoped to
     the external work-item subset (or already-persisted files), never the full
     roster;
   - make `build-executor` and `dispatch` validate requested access against the
     same resolved-execution facts;
   - preserve zero-spawn prep and one visible shell per external seat.
   - add strict `persist-seat --host-result-file <path>
     --expected-sha256 <64hex>` ingestion with the
     envelope validation, normalized-field mapping, preserve-valid behavior, and
     exit 0/4/2 contract defined above. Implement the two mutually exclusive
     parser modes exactly: legacy retains `-f`/stdin, status/model/elapsed flags
     and `--verify`; host mode accepts the file plus identity flags only and
     rejects every legacy content/status flag and `--verify`.
   - extend `ProviderResult` and its `to_dict`/`from_dict` contract in
     `multiagent/providers/__init__.py`: `model` remains the requested pin;
     additive `reported_model`, `operation_id`, `host`, `driver`, `child_id`,
     `failure_code`, `harness_version`, and `external_client_version` round-trip when present and are
     omitted when null.
   - add engine-owned installed-role verification and landed-file `hashlib`
     verification; `persist-seat`, `persist-debate-seat`, and
     `state persist-role` all require the parent-computed
     `--expected-sha256`, reject malformed/mismatched digests before parsing,
     and store the digest in replay identity;
   - in `session-start.py` and the CLI lifecycle loader, implement the atomic
     filtered lifecycle record, existing-session-ID lookup, cleanup, expiry,
     replacement, and negative-test contract defined above. This code is
     conditional on the exact host fields proven in Phase 0A and remains
     unreachable for an unproven host, but it must land before that host's
     Phase-3 native slice.
   - implement each Phase-0A-proven same-session refresh writer in the owning
     hook/event script, with the same atomic schema/identity checks. Add Cursor's
     fixed ten-second monotonic initial-record readiness wait before its first native prep and the
     stable-conversation refresh negative cases. A host slice cannot enter
     Phase 3 until its initial writer/readiness and, for recoverable persistence,
     same-session refresh contract pass installed tests.
   - record per-host `refresh_events` evidence naming the exact lifecycle event
     that rewrites the record on new/resumed/compacted sessions. If an active
     loop reaches the fixed 24-hour expiry before such an event, every native
     launch pauses before reservation/spawn, leaves state/route/continuation
     untouched, and prints explicit recovery: trigger the proven refresh event,
     rerun `doctor`, then resume. Expiry is an operator gate, never silent
     rerouting or grandfathered native admission.
3. Update `multiagent/review_runs.py` with one record-normalization reader:
   - new records use `roster` and `executions`;
   - legacy `subprocess_seats`/`task_seats` records normalize to the same internal
     representation;
   - existing immutable-run, pointer, snapshot, pending, stale-result, and target
     drift gates operate on the normalized representation.
4. Update every `crew-state.py` review-record consumer—not only quorum/verdict.
   In particular, route `cmd_begin_review` through the normalized roster and
   execution-signature reader before it freezes the round; route
   `record-verdict` and status/advisory readers through the same function.
   Quorum arithmetic stays strict-majority-by-distinct-seat. Implement
   `state persist-role` with the exact loop-role HostExchange validation,
   continuation guards, committed-transaction consumption, and 0/4/2 behavior
   above. Also implement `state begin-role-spawn` as the sole native-executor
   reservation/tombstone transaction, including fixed state-then-continuation
   lock ordering, prepared-transaction recovery, exact output, and mismatch-
   leaves-both-untouched behavior. Add `executor_spawn_transaction` to
   `LoopState` alongside additive/default-empty route-stamp and
   `operation_attempts` fields; they are engine-owned and not `AGENT_SETTABLE`.
   Phase 2 tests them with exact fixture state, but no active-loop initializer or
   wrapper writes them until Phase 5, so this contract code cannot enable native
   execution early.
5. Replace the current contract-freeze fixtures/assertions in
   `test-multiagent.py` with the new exact payload and run-record shapes while
   pinning the temporary legacy payload keys. When the Phase-0 inventory found a
   live legacy artifact, keep a bounded persistent compatibility matrix: one
   mixed native/external legacy record, one active-loop legacy-to-current resume
   fixture, and the current shape. That positive-inventory resume fixture starts
   an active loop from a legacy `seat_signatures` review, survives the upgrade,
   and starts its next round with a new `roster`/`executions` run; malformed
   legacy facts fail closed without modifying active state. With negative
   inventory, do not create or retain any active legacy-resume fixture: keep
   only temporary wrapper-bridge tests, then delete the unused legacy reader
   and fixtures in Phase 6. Keep golden tests
   for target hash, signature sensitivity,
   pending-seat resumption, duplicate-seat rejection, reserved stems, and failed
   seat isolation. Add legacy/current provider-result fixtures, every
   host-envelope status and exit mapping, same-operation idempotent replay, and
   rejection of changed same-operation, stale-run, cross-seat, and
   different-operation writes, including a persisted failed native seat followed
   by a second operation (exit 2, original preserved). Assert review/debate never
   reserve a retry after any handle exists. Table-drive every mixed parser case—host-result
   file plus each legacy flag, `--verify`, piped/nonempty stdin, and combined
   legacy content sources—and require exit 2 with no landed record. Pin
   `timeout_seconds`/`deadline_epoch`/`cancel_grace_seconds` serialization,
   expired/malformed refusal, local monotonic derivation, harness-version
   propagation, and `model_matches` persistence. Test both generic reservation
   CLIs plus exact `begin-role-spawn`: mixed-scope/role/expectation argv exit 2,
   attempt-free key validation, concurrent unique increments, lock-order safety,
   stale route/workspace/continuation refusal with no mutation, prepared-crash
   same-ID recovery, concurrent same-input same-ID completion versus conflicting-
   input exit 2, external `crew run` just-in-time reservation, generic-
   reservation operation-attempts-only behavior, and byte preservation of every
   unrelated `LoopState` field including stop/park counters. The
   positive-inventory resume fixture must normalize both shapes and preserve the
   same state-machine roster.
   Table-test all legal/illegal WorkItem scope-role pairs and result-path
   containment; debate manifest creation/immutability/seat ingestion; role byte
   verification before reservation, including exit-2/no-attempt negatives;
   loop-role persist 0/4/2 and continuation
   guards; lifecycle writer/lookup/refresh/expiry negatives; and every four-scope
   trace FSM, including mixed/incomplete run-versus-loop argv exit 2.
6. Update `cmd_doctor`, `_render_config_template`, `cmd_scaffold_config`, and
   their seat-roster fixtures in `multiagent/cli.py` so doctor/init report
   host+driver execution possibilities and ordered `via`, not a permanent
   Task/subprocess split. Preserve non-billable probes, no-clobber behavior,
   repository-over-global precedence for each present key (while documenting
   that absent repository keys may read global values), and existing
   panel/executor precedence.
7. Add command-level smoke tests for the still-legacy `review.md`, `build.md`,
   and `measure-twice.md` consumers so every gate-complete Phase 2 commit proves
   the dual-emission bridge keeps them functional. Phase 6 removes the bridge
   only after wrapper migration. Commit
   `plugins/crew/scripts/tests/fixtures/legacy-bridge-consumers.json`, mapping
   each emitted legacy field (`subprocess_seats`, `task_seats`, model, prompt,
   pending variants) to every exact command/skill consumer found by source scan.
   Each later wrapper phase removes itself from the applicable lists and may
   delete a field immediately after its last list becomes empty and its
   absence/consumer tests pass; retain no field merely because another bridge
   field is still needed.

**Gate:** review prep for an identical target/panel produces the same roster,
model pins, channels, frozen bytes, and pending behavior on all hosts. No command
body classifies a seat as Claude Task, subprocess, Codex, or Cursor by name.
`begin-review`, collect, quorum, verdict, doctor, and scaffold tests all consume
the normalized representation; the two-phase native-return/persist plus
external-only-wait barrier has a deadlock regression test. HostResult ingestion
passes every status/exit mapping, and all legacy command consumers still parse
the bridge payload. Phase 0A probe 8 remains open until this ingestion code is
landed and exercised live.

### Phase 3 — Ship the shared read-only review vertical slice

1. Add the shared skill layout, initially implementing only `review/SKILL.md`,
   `_shared/workflow-contract.md`, `_shared/workflows/panel-review.md`, the
   reviewer/scribe/formatter roles, and the three host adapters. Each adapter
   file must spell out the verified harness invocation syntax, handle creation,
   await/cancel operations, `HostResult` field mapping, dispatcher-root recipe,
   and hook serializer; it may not defer those mechanics back to free-form model
   judgment or invent a second implementation. Copy the exact version-fenced
   mechanics from that host's committed Phase-0A contract transcript.
2. Reduce `plugins/crew/commands/review.md` to a thin compatibility entry point
   using the argument-forwarding convention proven in Phase 0A. Pin this source
   contract: after frontmatter, the first instruction is
   `MANDATORY: invoke Skill("crew:review") exactly once before any other action.`
   It is followed by exactly one `<crew_arguments>` block containing the host's
   raw `$ARGUMENTS` placeholder exactly once and a closing `</crew_arguments>`.
   The loaded skill treats the bytes inside that block as `crew_arguments` and
   performs parsing; the wrapper performs no flag parsing or workflow phase.
   If a host requires a different literal skill-call spelling, Phase 0A records
   it and the wrapper substitutes only that verified directive while preserving
   the tag/placeholder contract. Keep the user-facing
   `/crew:review` name in Claude and Cursor and prohibit recursive invocation of
   `/crew:review` from `crew:review`.
3. Add minimal Cursor reviewer/scribe/formatter definitions under
   `plugins/crew/agents-cursor/` only if the probe proves named plugin
   subagents. SUPERSEDED on the tool-restriction half: the three adapters
   SHIPPED, and this host offers no per-role tool field, so their read-only and
   write-only constraints are held by role prose plus a `readonly: true` key on
   the reviewer and formatter whose app-surface enforcement is UNVERIFIED (the
   only evidence for that key is from the external CLI surface).
   `docs/cursor-host.md` records that weaker tier as a host fact.
   Remove the test that requires the directory
   to be empty and replace it with manifest/role/access assertions. If Cursor's
   supported interface is generic prompt-only, leave the directory empty and
   encode the verified generic operation in `hosts/cursor.md` instead.
   In this phase convert only existing
   `plugins/crew/agents/{reviewer,scribe,formatter}.md` bodies to thin host
   frontmatter adapters over canonical `_shared/roles`; retain advisor,
   executor, panelist, and unrelated reader/document-writer agents until their
   owning phases.
4. Update `plugins/crew/.claude-plugin/plugin.json` and
   `plugins/crew/.cursor-plugin/plugin.json` with their documented
   `skills: "./skills"` path fields and retain their existing command/agent/hook
   routing. Add documented `plugins/crew/.codex-plugin/plugin.json` with
   `skills: "./skills/"` and repository-root
   `.agents/plugins/marketplace.json`; rely on documented default
   `hooks/hooks.json` discovery, then verify installed/cache bytes as Phase 0A
   requires. Do not check generated migrated-command skills into source.
5. Extend `scripts/post-commit-version-bump.sh` and `test-version-bump.py` so the
   Codex manifest/marketplace participate in version reads, substantive-change
   detection, snapshots, rollback, staging, and twin-equality tests alongside
   Claude/Cursor. Preserve unrelated working-tree bytes on failure.
6. Add source-shape tests in `test-hooks.py` for all three manifests, declared
   skills paths, shared skill discovery, and host adapter references. Make the
   wrapper test deterministic: require exactly one qualified skill directive,
   exactly one raw-argument placeholder inside exactly one tagged block, no
   executable prose outside that directive/block, and zero occurrences of the phase sentinels
   `review-prep`, `persist-seat`, `collect`, `record-verdict`, or `Task(`.
7. Install the development plugin separately in Claude Code, Cursor, and Codex;
   run the same harmless fixed-file review with an identical panel and compare
   `run.json`, channel provenance, seat results, grouped findings, and quorum.
   Exact selected-panel invocations are Claude/Cursor
   `/crew:review --seats codex,cursor-auto,opus "plan.md"` and Codex
   `crew:review` with `crew_arguments` exactly
   `--seats codex,cursor-auto,opus "plan.md"`. Repeat the distinct default-panel
   row with Claude/Cursor `/crew:review "plan.md"` and Codex `crew_arguments`
   exactly `"plan.md"`; it must resolve the same committed default roster.
   Cursor's user invocation remains bare; the internal adapter trace, not user
   syntax, proves the literal `CREW_HOST=cursor` binding on every dispatcher call.
   Invoke through the actual compatibility command/skill with the Phase 0A
   quoting/flag cases and assert the exact seven-event `trace.jsonl` contract.
   This live trace, not source shape alone, is the adapter-drift gate.
   Execute host rows serially if needed. A host lacking native admission runs the
   explicit external-only waiver row; it does not block a separately admitted
   host's Phase-3 slice or receive a placeholder native adapter.
   Every row must use Phase 2's lifecycle lookup/refresh path. This phase does
   not add a Cursor loop refuse-to-arm guard or remove its operator-enabled
   best-effort commands.

**Gate:** `/crew:review` in Claude/Cursor and the Crew review skill in Codex run
the same roster against the same frozen bytes. A native work item is actually
native; an external item uses its configured client; failures remain labeled;
no nested Codex CLI is used for native Codex work. The installed wrapper loads
the intended qualified skill, preserves the user's exact flags/quoted payload,
and executes the required two-phase barrier in every native-capable host. An
`external-only` host waives native spawn/persist rows but must pass the same
wrapper, frozen-roster, external wait, repair, collect, and quorum checks with
zero native work items or handles. Its unparsed fixture exercises parent-inline
canonical formatting (or preserved RAW when unavailable), never a spawned
formatter/scribe.

### Phase 4 — Port debate and host-neutral loop controls

1. Add `debate/SKILL.md`, `_shared/workflows/debate.md`, and the shared panelist
   role, then convert `plugins/crew/agents/panelist.md` to its thin canonical-role
   adapter. Migrate the debate wrapper/consumer to Phase 2's existing
   `debate-prep`/`workplans.prepare_debate_round` contract: `cmd_debate` retains initial scaffold/
   question ownership; each round prep returns debate dir, round, prior-round
   path, and round-specific work items; final synthesis calls
   `rounds.write_round` before another prep may read it. Replace
   `commands/debate.md` with a thin entry point. Reuse work-item execution/result
   normalization but not review-run directories or pointers; preserve debate
   panel precedence and round context. Apply the same mandatory qualified-skill,
   tagged-argument, structural wrapper, banned-phase, and live forwarding gates
   as review. Pin its exact authoritative/temporary JSON fields and the committed
   two-round semantic oracle described above; keep `seats --debate --json` only
   as the temporary roster/compatibility query through Phase 5, then apply its
   Phase-6 split-key cleanup fate.
2. Add shared `status`, `cancel-build`, and `cancel-measure-twice` skills. Reduce
   the corresponding command files to wrappers. The shared workflow owns state
   display/ordering; Python state commands remain the only transition writers.
   Host adapters supply only session ID and dispatcher-root mechanics.
   Apply the same deterministic wrapper contract, substituting each qualified
   skill name and its raw arguments.
3. Add `plugins/crew/scripts/host_recipes.py` and route every nudge in
   `persistent-mode.py` through it. It derives the absolute installed dispatcher
   from the hook script location and emits the verified host continuation entry:
   Claude/Cursor qualified slash-command wrapper, Codex `crew:build` or
   `crew:measure-twice` skill, plus host-neutral state command steps. No Cursor/
   Codex recipe contains `${CLAUDE_PLUGIN_ROOT}` or assumes a Claude Task call.
   Unit tests pin build, measure, done, cancel, and configured-executor recipes
   for claude/cursor/codex/unknown, and Phase 0B exercises the emitted recipe in
   a genuine Stop continuation. That installed run is what closes the Phase-0B
   `host_recipes` row; a unit test alone cannot close it or admit Phase 5.
4. Parameterize project/session wording in `session-start.py` so it does not
   describe every host as Claude or advertise unavailable native agents. Keep
   the existing host-specific hook serializers in `session-start.py` and
   `persistent-mode.py`.
5. Add CLI/skill tests for debate precedence, `PreparedDebateRound` paths,
   `rounds.read_prior_rounds`/`write_round` handoff, multi-round fresh panelists,
   using distinct child IDs when exposed or the version-bound fresh-context/
   distinct-operation/null-continuation proof branch otherwise; failure of both
   branches makes that host external-only. Cover cancellation by exact session,
   status budgets, and hook output shapes in all
   three hosts.

**Gate:** review and debate share one fan-out workflow; status/cancel operate on
the same state files from all three hosts; no host adapter contains panel
selection, quorum, or verdict logic.

The existing engine-only `council` subcommand remains an intentional ad-hoc
external fan-out and is not advertised as a host-native public workflow. Giving
it public parity is separate scope.

### Phase 5 — Port measure-twice and build after read-only parity

1. Add `measure-twice/SKILL.md`, `build/SKILL.md`, the corresponding shared
   workflow files, and advisor/executor roles. In this phase convert
   `plugins/crew/agents/{advisor,executor}.md` to thin adapters over those
   canonical roles. Reduce `commands/measure-twice.md`
   and `commands/build.md` to wrappers satisfying the same qualified-skill,
   tagged-argument, deterministic source, and installed live-forwarding gates.
2. Add `resolve_builtin_executor` beside `resolve_seat` in
   `multiagent/channels.py`; in `multiagent/cli.py`, allow `build-executor` to return either native or
   external resolved execution. Resolve `crew:executor` only through
   `resolve_builtin_executor(host, lifecycle_context)` and only when
   `host_capabilities.py` contains Phase-0B proof of enforced
   executor role/tool scope and workspace-write. Reject it on a hookless,
   unknown, generic-substitution, or unscoped host rather than silently using a
   different mechanism. A user-selected external write-capable seat remains
   available there.
3. Activate Phase 2's additive route-stamp/operation transaction fields in the
   `state init bl` path in `crew-state.py`: host, `observed_harness_version`, nullable
   `external_client_version`, selected channel, driver, access, requested model,
   and request kind. The
   build wrapper passes the values returned by `build-executor` into `state init`
   in the same activation transaction as executor/resume settings. Keep schema-3
   compatibility because the Phase 2 fields are additive. New loops stamp them; active
   legacy loops without them retain the existing executor-only resolution until
   completion and may not opt into multi-`via`/native execution mid-loop.

   On every resumed/compacted round, `build-executor` first reads the active
   stamp. A configured seat calls `channels.resolve_seat(...,
   pinned_route=RouteStamp(host, observed_harness_version,
   external_client_version, channel, driver, access, requested_model,
   request_kind))`; `crew:executor` calls
   `resolve_builtin_executor` and compares every returned stamp field exactly.
   Neither calls unrestricted first-viable resolution. Pinned resolution checks current host
   equality, exact current observation equality to `observed_harness_version`
   plus current `version_constraint` satisfaction, exact external-client version
   for external routes/null for native, model/request kind, access, driver, and
   that the stamped channel remains in the seat's configured `via`. A missing
   client, changed `via`, host change, model change, or capability loss exits 2
   before launch, reports observed version, constraint, evidence and the
   re-probe/update/reinstall/doctor path alongside expected versus actual route
   facts, leaves state and
   continuation untouched, and keeps the loop active for operator action.
4. Update the shared build workflow so only the executor may reuse the existing
   continuation chain. Every review round calls `spawn_native_seat` afresh for
   every pending native reviewer. Never pass an executor continuation to a
   reviewer or a previous reviewer ID to a later round.
5. Preserve current external dispatch guards: nonzero process exit before
   envelope read; no review after HEAD/staged/branch drift; no retry after guard
   violation; retry only clean provider failure; no fallback to the builtin
   executor; partial edits remain uncommitted and unstaged.
6. Verify the existing stop-fire/deadline/FAILED-verdict/cancel transitions under
   each `full-persistence` host's current hook behavior. Cursor enters this tier
   only when Phase 0B proves a live `followup_message` causes continuation and
   Phase 0A proves the same-session lifecycle refresh contract. If either does
   not pass, Cursor remains `core-read-only`: review/debate/status/cancel are
   supported, existing loop commands are explicitly experimental/best-effort,
   and Cursor is excluded from the Phase 5 completion matrix rather than both
   passing and failing it. Preserve the proven refresh/recovery path and the
   operator decision to allow best-effort loop arming; do not restore the removed
   Cursor refuse-to-arm guard.
7. Add tests for exact executor precedence, route-stamp immutability, configured
   native executor acceptance, native/external write rejection, external
   continuation reuse, every `resolve_builtin_executor` model/scope/version
   rejection and exact returned stamp, native executor no-implicit-retry behavior,
   existing external-only executor retry scope, fresh reviewers, retry fences,
   partial edits, cancellation, and compaction/resume re-resolution.
8. Extend the committed live fixture with these UTF-8/LF, one-trailing-newline
   files and keep their bytes under golden tests:

   - `phase5-measure-task.txt` exact escaped bytes: `Write an implementation plan for replacing the exact sample.txt content "before" with "after"; do not modify files.\n`
   - `phase5-build-task.txt` exact escaped bytes: `Replace sample.txt content "before" with "after" and modify no other path.\n`
   - `phase5-plan.md`:

     ```markdown
     # Implementation plan

     1. Replace the exact contents of `sample.txt` from `before` to `after`.
     2. Verify `expected.patch` is the only working-tree diff.
     ```

   - `phase5-expected.json`:

     ```json
     {
       "measure_states": ["drafting", "reviewing", "done"],
       "build_states": ["drafting", "reviewing", "done"],
       "approved_exit_kind": "approved",
       "command_exit_codes": {"init": 0, "begin_review": 0, "record_verdict": 0, "deactivate": 0},
       "fake_adapter_route_hosts": ["claude", "cursor", "codex"],
       "reviewers_fresh": true,
       "executor_continuation_only": true,
       "task_spills_consumed_and_absent": true,
       "only_allowed_generated_plan": "phase5-plan.md",
       "final_patch": "expected.patch"
     }
     ```

   Automated Phase-5 tests use deterministic fake advisor/reviewer/executor
   adapters that implement the exact canonical `SpawnRequest -> HostHandle ->
   HostResult` contracts using committed Phase-0A transcript fixtures (with only
   documented deterministic ID/time substitutions): the advisor returns the committed `phase5-plan.md` bytes and the
   executor applies only `expected.patch`. They byte-compare task/plan/diff and
   exact serialized state/exit oracles. Live model prose is not compared across
   hosts, but the committed task bytes, Python-owned state sequence, exit codes,
   route stamp, frozen roster, and final normalized patch are exact.
   The fixture guard requires every temporary task spill to be consumed and
   absent at completion and permits exactly one generated plan path whose bytes
   equal committed `phase5-plan.md`; do not ignore `.crew/plans/`, `*plan*`, or
   any broad plan root. Fake lifecycle constructors cover Claude, Cursor, Codex,
   hook/CLI events, and missing/malformed/expired/host-session-project mismatch
   cases using the canonical lifecycle transcript fixtures.

**Gate:** one harmless plan-only measure-twice loop and one disposable-file build
loop complete through the same phase/verdict state machine in every host whose
Phase 0B row is `full-persistence` (Claude Code and Codex are expected but must
still pass; Cursor participates only if its follow-up probe passed). For each
participating host, native write execution is tested only when role/tool scope
was proven; otherwise the build uses an explicitly configured external executor.
The resulting state proves exact stamped-route verification, fresh review seats,
and executor-only continuation. A `core-read-only` Cursor result does not block
the project from completing the read-only multi-harness scope and is not counted
as persistent-loop parity.

### Phase 6 — Documentation, install, and cleanup

1. Update `README.md`, `plugins/crew/README.md`, and the host contracts with a
   three-harness install/feature matrix. Assign each installed host one evidence-
   backed tier: `full-persistence` (review/debate plus enforced loops),
   `core-read-only` (review/debate/status/cancel; loop continuation unproven), or
   `external-only` (commands/skills plus external review/debate/dispatch, zero
   native work items), or `one-shot` (ChatGPT review/debate only). State current verified limitations,
   especially ChatGPT hooks and any remaining Cursor persistence caveat. The
   tier, not aspirational host identity, determines which Phase 5 and acceptance
   rows apply.
2. Document the same workflow vocabulary and config examples for every host.
   Keep invocation spelling idiomatic (`/crew:...` where commands exist,
   Crew skills in Codex) while promising identical configuration and outcomes.
3. Update `docs/CLAUDE.md`, `plugins/crew/scripts/CLAUDE.md`, and engine notes for
   the user-facing host behavior, work-item/`HostResult` contracts, resolver,
   doctor/init output, run-record compatibility, route-stamp verification, and
   adapter ownership rules.
4. Delete obsolete full orchestration prose from the command files and obsolete
   `task_seats()`/`has_executor` execution oracles after source gates prove no
   live consumer. Remove the temporary prep-payload legacy split/model/pending
   keys per the field-to-consumer map as each last wrapper migrates; remove any
   remaining wrapper-only legacy parser only after consumer smoke tests have
   been retargeted **and** `crew status` plus state/run
   scans prove there is no active legacy loop that still depends on them. If one
   exists, defer deletion until it completes/cancels; do not rewrite it in place.
   Delete replaced tests rather than retaining duplicate coverage.
5. Run all automated suites, reinstall all three development plugins with the
   documented cache-refresh procedure, and repeat the three-host validation
   matrix below on installed bytes rather than the checkout alone.
6. Probe ChatGPT Work's current universal-plugin/`@` skill surface with the
   installed one-shot review/debate skills and record exact prerequisites and
   invocation if it works. If the surface cannot load them, document one-shot
   ChatGPT as unsupported with the observed missing prerequisite instead of
   claiming parity. In either outcome, persistent measure/build skills must
   refuse in ChatGPT's hookless context and direct the user to Codex.

## Compatibility and migration approach

- **Configuration:** existing one-element `via` behavior is unchanged and
  multiple channels become legal/ordered. Retain legacy `provider -> via`
  translation persistently only if the Phase-0 inventory finds a live config;
  otherwise delete the spelling and its compatibility-only tests in Phase 6
  instead of preserving a phantom consumer. When retained, both spellings
  together remain an error. Do not add `--via` or per-seat CLI overrides.
- **Panels and workflows:** command names and flags remain. Claude/Cursor command
  wrappers use the verified mandatory qualified-skill/tagged-payload convention,
  so existing user muscle memory survives and argument bytes have one owner.
  Codex exposes the equivalent skills directly. Slash `/crew:*` and qualified
  `crew:*` skill names coexist without recursive lookup.
- **Run artifacts:** new writers emit only `roster`/`executions`. Retain the old
  Task/subprocess reader persistently only if Phase 0 finds a live artifact;
  otherwise delete it and its compatibility-only fixtures in Phase 6. A found
  immutable legacy run is normalized on read and never rewritten merely to
  migrate shape. Independently, prep JSON dual-emission is migration-only for
  the known Markdown consumers through Phase 5; the field map removes each key
  after its last consumer migrates/tests, with any final parser cleanup subject
  to the no-active-legacy-loop fence.
- **Loop state:** route fields are additive. Existing inactive history remains
  readable. Active legacy loops finish with their current executor-only stamp;
  they normalize legacy/current review-run shapes across rounds but cannot adopt
  native or multi-route execution mid-loop. New loops freeze executor plus route
  and verify the exact stamp before every launch.
- **Provider options:** `reasoning_effort`, `print_timeout`, dispatch options,
  and continuation storage keep their present owners and precedence. Do not
  invent a generic adapter-options table.
- **Installation:** keep Claude and Cursor marketplaces functional while adding
  the first-class Codex layout proven by Phase 0A. All manifests explicitly
  expose the shared skills using their documented schema. Generated cache/
  migration output is never a source of truth.

## Automated and live validation matrix

Every operator-run row uses one canonical evidence schema:
`host`, `tier`, `probe_kind`, `harness_version`, `plugin_version`,
`fixture_commit`, `fixture_sha256`, isolated `HOME`/profile path, exact
`invocation`, `probe_id`, expected result, observed result, plus the
kind-conditional operation/model/lifecycle/packaging fields required by the JSON Schema,
canonical `evidence_path`, artifact paths, `status=pass|fail|missing|waived`, operator,
and UTC timestamp. A missing
required field is a missing row, not a pass; it halts the owning enablement gate.
Use `status=waived` only for a native-specific row that is inapplicable to an
explicitly advertised `external-only` tier, with waiver reason/evidence. Tier
summaries publish pass/fail/missing/waived counts; a waiver never increments
passes, and a tier is advertised only when every applicable row passes and every
inapplicable row has the allowed explicit waiver. `core-read-only` cannot waive
its admitted native read rows, and `full-persistence` cannot waive Phase-0B/5
rows.

| Concern | Claude Code | Cursor | Codex |
|---|---|---|---|
| Advertised tier | 0A failure=`external-only`; 0B pass=`full-persistence` | 0A failure=`external-only`; else `core-read-only` until 0B, then `full-persistence` | 0A failure=`external-only`; 0B pass=`full-persistence` |
| Host detection | Claude markers | verified stable marker or adapter-bound override | exact Codex markers |
| Harness version | Phase-0A-proven lifecycle source; missing/stale -> external-only | same, never external CLI substitution | same, never external CLI substitution |
| Skill/command install | thin slash wrapper + shared skill | thin slash wrapper + shared skill | first-class shared skill |
| Argument forwarding | tagged raw payload live trace | same | direct skill payload plus compatibility probe |
| SessionStart/Stop | Claude JSON shape | Cursor JSON shape | verified Codex-compatible hook shape |
| External seat | visible `crew run` child | same | same |
| External model evidence | Claude stream init parse/version fixture | Cursor stream `system/init.model`; `auto` concrete required | Codex exact-null guarantee + invalid-model negative (Agy row is shared external-only) |
| Native seat | fresh Task child when 0A-admitted, else zero | fresh Cursor subagent when 0A-admitted, else zero | fresh Codex subagent when 0A-admitted, else zero |
| Model evidence | family alias: concrete family match or version-bound null guarantee | `auto` router requires concrete report; exact requests require normalized equality | exact requests require normalized equality or version-bound null guarantee |
| Failed native seat | `HostResult` envelope -> normalized labeled failure | same | same |
| Frozen target | identical target hash/snapshot | identical | identical |
| Panel drivers | mixed native+external when admitted; all external otherwise | same | same |
| Quorum | distinct-seat strict majority | same | same |
| Debate round context | fresh panelists | fresh panelists | fresh panelists |
| Native write executor | only with proven role/tool scope | same; otherwise external only | only with proven role/tool scope |
| External executor continuation | exact stored provider ID | same | same |
| Reviewer freshness | new child IDs every round | same | same |
| Stop persistence | Phase-0B live proof required | tier upgrade only after follow-up proof | Phase-0B live proof required |
| Cancel/status | exact session state | same | same |
| Portable timeout | epoch/seconds converted to local monotonic; separate cancel grace | same | same |
| Installed-byte check | plugin cache | refreshed marketplace clone/cache | Codex plugin cache |

Use `plugins/crew/scripts/tests/fixtures/multi-harness-live/` and the exact tasks
defined in Phase 0 for live comparison. Record host,
plugin version, requested/reported models, work items, run ID, target hash, result
paths, child/session IDs, verdict, quorum, and loop exit kind. Do not compare prose
byte-for-byte across models; compare the deterministic contract and artifacts.

## Failure modes and mitigations

| Failure mode | Mitigation |
|---|---|
| Native host is silently preferred over the user's first `via` route | First-viable ordered resolver tests, including later-native negative control |
| Native capability borrows external client guarantees | Key capability lookup by host+channel+driver and test divergent native/external facts |
| External client version is mistaken for harness version | Phase-0A-proven `discover_harness_version`; missing/unparseable/stale fails native closed with evidence/re-probe diagnostic |
| Host adapter substitutes its own model for an exact seat | Verify reported actual model where available; otherwise fail the native route closed unless the host contract guarantees the pin |
| Cursor `auto` is treated as a concrete model | `request_kind=router` requires a nonblank concrete reported model and persists it |
| Monotonic timestamps cross host/runtime boundaries | Serialize epoch/relative seconds only; adapters derive local monotonic bounds and fail closed on expired/malformed input |
| Runtime failure silently falls through to the next channel | Freeze selected execution before launch; normalize failure; never re-enter resolution |
| Adapter loses child correlation or treats timeout/cancel as success | Required `HostHandle`/`HostResult` envelope, lifecycle contract tests, and installed phase trace |
| Cursor shell loses `CREW_HOST` | Stable exact marker after second capture, else bind override in each adapter dispatcher call |
| Cursor Stop hook does not continue | Do not claim full loop persistence; keep review/debate full and loops explicitly best-effort until live proof |
| Nested Codex CLI fails inside Codex sandbox | Native Codex work items use Codex subagents; nested `codex exec` remains only an external route off-host |
| Three workflow copies or wrappers drift | Canonical shared source; structural one-directive/one-tagged-block/sentinel test plus installed argument/phase trace |
| Parallel arrays disagree or double-count quorum | One ordered work-item list and one normalized run roster; duplicate seat rejection before writes |
| Existing run artifacts become unreadable | Compatibility reader fixtures; immutable old run files are never rewritten |
| Active loop changes route after config/client/host changes | Verify only the exact stamped channel/driver/requested model before every launch; never invoke unrestricted resolution |
| Reviewer context continues across rounds | Host adapter always spawns a fresh reviewer; tests compare child IDs; continuation handle exists only in executor path |
| Native reviewer can edit the workspace | Require host-enforced read-only scope where available and before/after tree guards; otherwise record the host limitation before claiming parity |
| Native executor is an unscoped generic child | Admit native workspace-write only after Phase-0B role/tool-scope proof; otherwise reject the sentinel and require an external seat |
| Codex source layout is guessed from another host | Throwaway current-CLI install records exact accepted manifest/marketplace paths and fields before source files are added |
| Version bump updates some host manifests but not others | Include all three in snapshot/rollback/equality tests and preserve dirty-tree bytes |
| ChatGPT ignores hooks | One-shot review/debate only; persistent skills direct the user to Codex instead of pretending to loop |

## Explicit non-goals

- Pi or a fourth orchestration host.
- Weighted voting, model-family metadata, family-collapsed quorum, or automatic
  panel composition.
- Personal/work profiles, billing taxonomy, credential storage, or automatic
  cheapest-route selection.
- A dynamic user-defined provider/route protocol.
- `--via`, panel-wide route overrides, or per-seat invocation overrides.
- Runtime rerouting after a seat starts.
- Changing built-in panel membership, opt-in policy, debate override, executor
  precedence, retry counts, timeouts, or completion verdict semantics.
- Giving every legacy specialized command (`analyze`, `code-search`, `deepinit`,
  and similar commands outside the core workflow) first-class multi-harness
  support in this change.
- Deploying or submitting a ChatGPT app. The source skills may support one-shot
  use, but app deployment is separate work.
- Refactoring provider subprocess implementations merely for stylistic symmetry.

## Acceptance criteria

1. The same Crew configuration and selected panel run from Claude Code, Cursor,
   and Codex for the read-only review/debate capability in that host's tier,
   without editing the roster. Executor behavior is tier-gated separately.
2. All selection precedence and availability/opt-in behavior listed under
   **Invariants** are covered by passing regression tests and remain unchanged.
3. A multi-entry `via` list resolves deterministically in listed order; the run
   record stores host, observed harness/external-client versions, requested model/
   request kind, resolved access, selected channel, and native/external driver,
   while each result stores requested and nullable reported model.
4. Host and route are independent: every native-capable host tests one native and
   one external item; an `external-only` host tests the identical roster with all
   items external. No premium/native route is forced merely by host identity.
5. `review-prep` and debate emit one ordered work-item contract. No workflow or
   host adapter classifies seat names or contains a `task_seats`/subprocess
   execution oracle.
6. Review, debate, measure-twice, and build consume shared workflow/role sources;
   Claude/Cursor command files satisfy the qualified-skill/tagged-argument
   contract and Codex uses first-class source skills. Installed live traces prove
   flags arrive intact and required phases execute in order.
7. The same frozen target hash and roster reach every mixed-panel seat; strict
   majority quorum, failed-seat isolation, repair, and verdict behavior match
   current behavior.
8. Review/panel agents are fresh every round. Only the selected build executor
   may continue, and its exact executor, model, channel, driver, and continuation
   identity plus observed harness/external-client versions cannot drift after
   loop activation. Resume verifies the exact stamp
   without unrestricted first-viable resolution and fails before launch on any
   mismatch.
9. Native and external workspace-write executors are admitted only when the
   resolved execution supports write; native admission additionally requires
   proven enforced executor role/tool scope. Every existing dispatch guard and
   no-fallback rule still passes.
10. SessionStart, Stop, status, cancellation, stop-fire/deadline bounds, and
    terminal verdict behavior pass automated tests and live installed-plugin
    probes in every host advertised as `full-persistence`. `core-read-only` and
    `external-only`/`one-shot` hosts are judged only against their documented
    tier; external-only requires zero native work items plus external review/
    debate/dispatch, frozen target, repair, collect, and quorum.
11. Codex installs from the exact source manifest/marketplace paths and fields
    proven by the Phase-0A throwaway install; its native work items do not launch
    nested `codex exec`. Claude and Cursor manifests explicitly expose the shared
    skills and their installs continue to work.
12. Old loop state remains readable. Phase-0-inventoried live `provider` config
    and old run records retain their documented readers; compatibility with no
    live consumer is removed after wrapper migration/no-active-loop gates.
13. ChatGPT one-shot support is labeled from the Phase-6 universal-plugin/`@`
    probe (or explicitly unsupported with prerequisites), and persistent
    workflows refuse there and point to Codex when hooks are unavailable.
14. All three automated suites pass after implementation, followed by the full
    installed-byte validation matrix. A passing checkout-only test is not enough.
15. Every host adapter produces the required terminal `HostResult` envelope;
    native handles are correlated, native results are persisted before collect,
    and `crew wait` receives external seats only.
16. Doctor/init expose ordered host+driver routes without billable probes or
    clobbering config, and `begin-review` freezes the normalized roster for both
    legacy and current run-record shapes.
17. Each native path uses its Phase-0A-proven harness-version source and
    propagates the observation through resolve, prep, run, handle/result,
    persistence, status, and doctor. Missing/malformed/stale versions fail native
    closed with the recorded evidence and re-probe path; no force override or
    external-client-version substitution exists.
18. `model_matches` passes the exact/family/router matrix (including concrete
    Cursor `auto` reporting), and portable epoch/relative timeout plus separate
    cancellation-grace tests pass across adapter boundaries. Expired/malformed
    timing and model fallback both fail closed.
19. Every native launch consumes one complete immutable `SpawnRequest`; adapters
    add only start/child facts, and the shared workflow mechanically assembles it
    after reservation without inference. Both generic reservation CLIs pass
    identity, concurrency, scope-mixing, just-in-time external launch, and
    unrelated-state preservation tests. Native executor launch instead passes
    the exact `begin-role-spawn` atomic reservation/tombstone, mismatch-no-
    mutation, and prepared-crash recovery contract.
20. `crew:executor` resolves only through `resolve_builtin_executor`; its proven
    host/version/model-kind/access/channel/driver stamp is immutable and an
    unsupported model or role/tool scope fails before launch without deactivating
    an active loop.
21. An external-only review, including an unparsed result, produces zero native
    handles. Repair either validates parent-inline canonical formatting through
    `repair-seat` or preserves the RAW/unparsed artifact without changing quorum.
22. External client versions and model reports pass their versioned stream/null-
    guarantee probes without requested-model overwrite. Cursor `auto` parity is
    claimed only with a concrete routed report; parser/version drift makes only
    that external candidate unavailable and labeled.
23. Role-neutral WorkItems cover only the legal run/loop role table, carry
    contained deterministic result paths and installed role digests, and produce
    complete SpawnRequests without inference. Missing/mismatched role bytes exit
    2 before reservation/spawn and produce no attempt, handle, or host result.
24. `state persist-role` and `persist-debate-seat` pass their complete 0/4/2,
    parent-computed digest, replay/conflict, manifest/route/path, zero-retry, and
    continuation-mutation matrices before native loop/debate enablement. Native
    executor tests prove no-spawn-before-transaction retention, exact expected-
    ID verification, atomic pre-spawn reservation/tombstoning, attempted-failure
    clearing, committed-transaction consumption, and guarded success replacement.
25. Lifecycle records are session/project bound, filtered, expiring, refreshed
    only by proven host events, and mandatory for native work. Expired active
    loops pause unchanged until the documented refresh/doctor recovery succeeds.
26. Installed review, debate, measure-twice, and build runs emit exactly their
    scope/round trace FSM with correct zero counts; duplicate/out-of-order traces
    fail closed.

## Sequencing fences

1. **Split evidence gates:** baseline permits host-neutral Phase 1-2 types,
   compatibility readers, and fake-capability tests. Phase 0A precedes real
   native read-only capability entries, adapters, packaging paths, wrappers, and
   read-only support claims. Phase 0B precedes native write admission,
   persistence enablement, and full-loop claims.
2. **Resolution before orchestration:** ordered multi-`via` tests land before any
   host may consume native work items.
3. **Contract before wrappers:** the uniform work-plan and legacy reader land
   before replacing command prose.
4. **Read-only before writes:** review passes all three hosts before debate;
   review/debate pass before measure-twice/build native executors. The native-
   persist/external-wait barrier is preserved throughout.
5. **Tiered persistence:** Cursor is not called `full-persistence` and is not in
   the Phase 5 completion gate until live stop-follow-up is demonstrated.
6. **Route freeze before continuation:** route stamps and mismatch tests land
   before native or multi-route build execution is enabled.
7. **Compatibility before deletion:** legacy run/config/state fixtures pass
   before deleting Task/subprocess writers or execution oracles.
8. **Installed bytes before completion:** reinstall/cache-refresh and live probes
   happen after version/marketplace changes and before declaring the project
   complete.

## Commit and version-bump sequencing

Record this reload/evidence excerpt for each gate-complete phase commit:

| Phase | Required reload boundary | Evidence excerpt recorded before next phase |
|---|---|---|
| 0 | fresh fixture process; isolated host profile for attended probes | fixture hash plus schema-valid probe IDs/status counts |
| 1 | fresh Python process/client parser fixture | resolver row showing route, request kind/access, harness/client versions |
| 2 | fresh dispatcher process | exact prep/HostExchange/reservation/trace contract test names and totals |
| 3 | reinstall/cache-refresh each tested host | installed seven-line trace plus tier pass/waiver counts |
| 4 | reinstall hooks/skills before Stop test | debate round linkage and closed `host_recipes` probe ID |
| 5 | reinstall each full-persistence host | route-stamp comparison and terminal state/exit oracle |
| 6 | final reinstall/cache refresh | final feature matrix counts and ChatGPT probe/unsupported record |

Gate-complete subcommits within a phase are allowed and preferred when they keep
tests reviewable; do not commit a real capability entry or advertise an adapter
before its operator gate, and do not batch an unverified adapter with later
workflow work. Use these conventional scopes across the phase:

1. Phase 0 evidence/fixtures: `test(crew): capture multi-harness host contracts`
   (use `docs(crew): ...` only for a documentation-only follow-up). These
   test/docs commits intentionally trigger the repository's patch bump; record
   and verify it instead of treating it as accidental production drift.
2. Phase 1 resolver: `refactor(crew): resolve ordered host execution routes`.
3. Phase 2 work plans/migration: `refactor(crew): normalize portable panel work`.
4. Phase 3 review/packaging: `feat(crew): add portable review hosts`.
5. Phase 4 debate/controls: `feat(crew): port debate and loop controls`.
6. Phase 5 persistent workflows: `feat(crew): port verified persistent loops`.
7. Phase 6 docs/cleanup: `docs(crew): document multi-harness capability tiers`
   followed by a separate `refactor(crew): remove legacy execution oracles` if
   cleanup changes production code.

Before each commit, run that phase's targeted tests plus all suites whose files
changed. Let the repository's post-commit version-bump hook run; do not hand-edit
versions. Inspect its generated bump/staging result immediately and verify that
the Claude, Cursor, and Phase-0A-verified Codex manifest/marketplace files moved
together or remained unchanged together. Finish/record that bump before the next
subcommit so `feat(crew)` versus `refactor(crew)` history cannot be folded
into one ambiguous version decision. After the final installed-byte matrix, make
only evidence/docs corrections; any production fix reopens its owning phase and
requires a new conventional commit and revalidation.

## Readiness statement

The design is ready for implementation because the varying host operations, the
shared invariants, the exact current blockers, the migration rules, and the
phase gates are identified. The implementation is not yet verified: native
subagent model/access behavior, portable dispatcher-root discovery, and Cursor
stop-follow-up remain live evidence gates. An executor should stop at the
relevant gate rather than weakening the shared contract or inventing a
host-specific fallback.
