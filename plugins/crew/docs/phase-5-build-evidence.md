# Phase 5 build evidence

Recorded 2026-10-02. Phase 5 is implemented in the uncommitted working tree on
`codex/multi-harness-engine`, based on `9d75917`. Commands, hooks and the build
engine are cut over together. The installed root orchestrator remains Crew
0.83.1; candidate commands were never run against its state or continuation.
Plugin/marketplace version fields, Git hooks, branch, HEAD and index are unchanged.
The reviewed plan and root state/lock content hashes match their initial capture.

The current interface is documented in [build-protocol.md](build-protocol.md).
The operator's exclusions apply: no migration, old-format adoption/detection,
upgrade guidance, paid-result seeding or special restart subsystem was added.
Cursor native build/lifecycle, OpenHands and later roadmap phases remain deferred.

## Implementation and deterministic checks

The five implementation milestones are delivered: shared execution extraction;
schema-5 build ownership and code reviews; typed build actions/decisions/recovery;
command/hook cutover; and isolated regression plus real Claude CLI verification.
Python owns policy. The host follows issued owner/action argv and actual returns.

The prior finishing pass moved passive build records, writer-claim validation and
recovery text into `scripts/build_state.py`. Build policy and full journal
validation remain in the engine. SessionStart, artifact enumeration, projections,
state admission and swab now reuse the leaf without importing the build/review/
execution engine. Missing, unknown, future-version or inconsistent writer
records remain protected; cleanup never infers quiescence from age.

Checks use stdlib tests, the existing dependencies and isolated configuration.
The table below records the prior finishing epoch. Post-panel revision checks
are listed separately below; these earlier checks do not cover the later edits:

| Check | Result | Retained log |
|---|---:|---|
| Build workflow, final finishing source | 43 tests passed | `/private/tmp/crew-finishing-build-final.log` |
| Final focused writer/hook/cleanup/admission regressions | 6 tests passed | `/private/tmp/crew-finishing-focused.log` |
| Measure-twice, after the import correction | 131 tests passed | `/private/tmp/crew-finishing-measure.log` |
| Review workflow, retained | 201 tests passed | `/private/tmp/crew-phase5/source-settled-review.log` |
| Hooks, after the import correction | 674 checks passed | `/private/tmp/crew-finishing-hooks.log` |
| Multiagent, retained with `crew_config()` isolation | 1,990 checks passed | `/private/tmp/crew-phase5/closure-multiagent.log` |
| Python 3.11 grammar | All 47 Python files passed | `/private/tmp/crew-finishing-source-check.log` |
| Ruff | New modules/test pass; edited shared files pass fatal-name/syntax checks | Isolated Ruff tool cache; no project dependency added |
| Whitespace | `git diff --check` passed | Working tree |

The suites ran with Python 3.14.5; the separate grammar check verifies the 3.11
syntax floor. Tests cover concurrent admission and execute claims, held
continuation locks, resume-off, exact-ID resume, retry counts 0/1/2, frozen timeout
floors above 540 seconds, target snapshots, every decision-table row, strict
receipt identity, report markers, crash reconciliation, all-failed twice,
blocking/minor verdicts, synthesis-only/partial retry, force disclosure drift,
feedback recovery, and cleanup races/malformed writer journals.

The new finalization test interrupts deactivation after the accepted outcome is
durable: `phase=done`, active ownership and one applied receipt survive. Re-entry
deactivates without executing providers, reapplying counters or checking expired
work bounds. This is a deterministic injected filesystem failure, not a claim
that an unmodified live CLI sampled that intermediate transaction.

The prior final inspection reproduced an orphan-lock deletion racing new
admission. Project cleanup holds the same admission lock; the barrier regression
fails before that fix and passes afterward (`admission-cleanup-before.log`,
`admission-cleanup-after.log`, under `/private/tmp/crew-phase5/`). The retained
`closure-source.json` includes `_cleanup_stale_files_locked`, its admission guard
and the barrier test: its SessionStart SHA256 is
`99624db50ed1e23b989ae4d2e6bd8dbdbab6ea74e41e40a85ab5ba1566e472ff`,
and its build-test SHA256 is
`c81fe9f69196f1c5bd71ad871ebd9ad0d3a0ae34ef1ffcb134f1d96b48740750`.
Both matched the working tree at the start of this finishing pass. The closure
logs cover those late bytes (41 build, 674 hook and 1,990 multiagent checks);
the earlier `source-settled-*` counts/manifests alone do not establish that.

The new import-boundary regression uses a fresh interpreter and an actual
SessionStart invocation with an aged cancelled writer, then exercises cleanup
and artifact enumeration. It rejects engine imports and fail-open early exits,
requires all assertions to complete, and preserves state bytes and the lock
inode. It fails against the retained pre-correction closure hook
(`/private/tmp/crew-finishing-import-boundary-before.log`) and passes after the
correction. A second test checks malformed writer records against both the
engine validator and cleanup/artifact protection. Final focused checks also
rerun the admission-lock race and claim-under-cleanup-lock tests.

The finishing runtime/test Python snapshot is retained at
`/private/tmp/crew-finishing-source.json`, with canonical manifest SHA256
`e75796f994ee2427da3061538be495fe91ac9b0ae87bc4350417763b065c4147`.
The final 43-test build run and six focused regressions cover these bytes.
Broad hook and measure-twice checks followed the substantive import correction;
the final focused regressions also cover the subsequent formatting and stricter
test completion assertion. No unaffected full review/multiagent suite or paid
live experiment was repeated.

Obsolete
build-recipe prose assertions were replaced with parser/transport and behavioral
coverage; unrelated provider, review and measure-twice checks remain. This explains
the multiagent count changing from 2,026 to 1,990; it does not imply 36 engine
regressions were discarded. The 43 build tests are separate.

## Post-panel revision

This revision addresses the five accepted structural causes and the related
current-workflow corrections in `run-5b547ea2b07b`. The parent owns the next panel
and live root loop; this pass records no panel approval or root completion.

- The write execution request passes its canonical workspace per call through
  every provider API. Codex, Cursor and Agy launch there instead of falling back
  to shell cwd. Omitted provider workspace arguments retain ordinary defaults;
  no process-global cwd or environment mutation was added.
- Shared writer admission rejects unreadable build evidence. Stop retains its
  original bytes and emits a diagnostic, including native and resume-off writers.
  Cleanup rechecks current bytes, load status, active age threshold and mtime
  under the owner lock. Passive imports remain confined to the shared leaf.
- Swab takes one eligibility scan under admission and sorted owner locks per
  removal batch. `StateLockError` enters the failed-list/exit-code contract;
  successful partial removals still receive dangling-pointer cleanup.
- Prompt paths and ordinals checkpoint only after publication succeeds. An
  injected write failure recovers through `retry_feedback` without repeating
  accepted review outcomes, counters or paid work.
- Feedback and shared transport containment resolve workspace aliases, accept
  aliases above the workspace and reject traversal or symlinks in the protected
  namespace. A full revision/recovery regression uses an ancestor alias.
- Guarded native and external claims return the actual `needs_input` projection.
  Restored guards resume on one owner/question-bound recheck; stale questions,
  unknown original observations and unrelated force-fact drift remain refused.
  Terminal/replay output retains `last_verdict_overrides`.
- Successfully matched waiting starts consume the request. A lock-busy,
  unadmitted start retains its spill, returns `ref: null` and explains retry.
  Uncapped build reviews avoid the standalone cap warning. Dispatch preserves
  the exact continuation-store diagnostic once and the stamped-session hint.
- Current guidance uses schema 5 and engine-owned commands. The unreachable
  native binding branch and dead lock-sweep branch were removed; lightweight
  imports were hoisted and passive fence values cached where reused.

All `/private/tmp/...` paths in this document refer to **ephemeral local audit
files**, not a portable archive or repository dependency. The repository
trace fixtures preserve the earlier live acceptance facts, pins, source epochs
and exact counts. No raw Phase 5 audit evidence or trace fixture changed in this
revision, and no paid live experiment was rerun. Workspace launch was checked
with real local child processes through the actual provider implementations'
mocked subprocess boundary; aliases, publication failures and cleanup races are
deterministic fixture evidence, not new live provider observations.

| Revision check | Result | Ephemeral local log |
|---|---:|---|
| Build workflow, settled revision | 52 tests passed | `/private/tmp/crew-revision-audit/build-settled-final.log` |
| Hooks | 674 checks passed | `/private/tmp/crew-revision-audit/hooks-final.log` |
| Measure-twice | 131 tests passed | `/private/tmp/crew-revision-audit/measure-final.log` |
| Review workflow | 201 tests passed | `/private/tmp/crew-revision-audit/review-final.log` |
| Multiagent, isolated `crew_config()` | 1,989 passed; one obsolete provider-kwargs golden failed | `/private/tmp/crew-revision-audit/multiagent-final.log` |
| Corrected dispatch golden, failure invariants and placeholder checks | 16 checks passed, zero failed | `/private/tmp/crew-revision-audit/multiagent-focused-final.log` |
| Python 3.11 grammar | All 47 Python files passed | `/private/tmp/crew-revision-audit/source-check.log` |
| Ruff | Build leaf/engine/test pass; all scripts pass fatal syntax/name checks | `/private/tmp/crew-revision-audit/ruff-new-final.log`, `ruff-fatal.log` |
| Whitespace | `git diff --check` passed | Working tree |

The multiagent failure compared the entire old provider keyword dictionary.
The required `workspace` keyword was the only difference; the complete 16-field
unchained envelope still matched its original bytes. The corrected expectation
and affected dispatch group passed. The unaffected broad suite was not repeated.

The revision Python manifest is `/private/tmp/crew-revision-audit/python-source-final.json`.
Its SHA256 is `4a69497663253e5fb37cb68bdba9319788326f5231f32545b50341d46889e976`
using the relative path/hash map with sorted keys and compact JSON separators.
The settled build run covers the final formatted runtime and final test bytes.
Shared suites ran after the substantive corrections; the subsequent runtime
change was formatting of `build_workflow.py`, covered by the settled build run.
The final targeted dispatch group covers its corrected test bytes. These checks
ran with Python 3.14.5 and separately parsed the 3.11 grammar floor.

The initial and final revision audit hashes match for all 350 root-session files.
Branch, HEAD, index, plugin versions and global config remain unchanged. The
previous whole-plugin live manifests do not cover this revision's runtime,
regression or guidance edits. Existing live observations below apply only to
their recorded source epochs. No unresolved implementation blocker remains;
no broader host or interactive-resume claim is inferred from these checks.

Revision-touched files (relative to the repository):

- Runtime: `plugins/crew/scripts/artifact_prune.py`, `crew-state.py`,
  `loop_state.py`, `models.py`, `persistent-mode.py`, `session-start.py`;
  `plugins/crew/scripts/multiagent/build_workflow.py`, `claude_native_transport.py`,
  `cli.py`, `execution.py`, `measure_twice.py`, `review_workflow.py`;
  `plugins/crew/scripts/multiagent/providers/__init__.py`, `agy.py`, `claude.py`,
  `codex.py`, `cursor.py`.
- Tests: `plugins/crew/scripts/tests/test-build-workflow.py`, `test-hooks.py`,
  `test-multiagent.py`.
- Guidance: `.claude/CLAUDE.md`, `README.md`, `plugins/crew/scripts/CLAUDE.md`,
  `plugins/crew/commands/cancel-build.md`, `review.md`;
  `plugins/crew/docs/CLAUDE.md`, `build-protocol.md`, `codex-host.md`,
  `cursor-host.md`, `engine-notes.md`, `measure-twice-protocol.md`,
  `phase-5-build-evidence.md`.

## Second panel revision

The accepted findings in `run-49549b6d56be` are addressed at a separate
deterministic source epoch. Replacing a review generation now clears obsolete
retry authorization and its pause. External build capture retains exact adapter
report bytes before display trimming or newline conversion, while dispatch JSON
and envelopes retain their existing shape. Build and review capture guidance use
their distinct accepted flags. Uncertain writer protection covers both the state
filename owner and valid recorded ownership.

Related corrections retain structured timeout/cancellation/unavailable outcomes
without authorizing automatic cancellation/unavailable retries, expose inactive
writer fences and exact recovery argv through verbose status stderr, retain
accepted force decisions on digest revalidation, and use build-specific ownership
diagnostics. Status JSON stdout remains parseable. Guidance changes are confined
to the affected current workflow.

| Revision check | Result | Ephemeral local log under `/private/tmp/crew-revision2-audit/` |
|---|---:|---|
| Build workflow | 59 tests passed | `build-settled.log` |
| Final affected regressions | 8 tests passed | `build-focused-settled.log` |
| Adapter regression after import ordering | 1 test passed | `provider-import-settled.log` |
| Hooks | 674 checks passed | `hooks-final.log` |
| Review workflow | 201 tests passed | `review-final.log` |
| Measure-twice | 131 tests passed | `measure-final.log` |
| Multiagent, isolated `crew_config()` | 1,989 passed; one stale serialization expectation failed | `multiagent-final.log` |
| Corrected serialization contract | 10 checks passed, zero failed | `multiagent-contract-settled.log` |
| Python 3.11 grammar | All 47 Python files passed, runtime 3.12.13 | `grammar.log` |
| Ruff and whitespace | Engine/leaf/build tests pass full lint and format; all scripts pass fatal checks; `git diff --check` passes | `ruff-targets.log`, `ruff-fatal.log` |

The shared-suite failure expected every dataclass field to serialize. Its corrected
fixture populates the two internal fields and proves both stay out of JSON; the
affected contract group passed. The unchanged remainder of the broad suite was
not repeated. The adapter regression invokes deterministic local fixture binaries
through Codex, Agy and Cursor, checking exact retained returns, whitespace marker
rejection, CRLF acceptance and ordinary dispatch compatibility. Retry-floor drift,
actual swab, verbose status and force revalidation use disposable owners and no
paid work. The final focused run covers the later diagnostic assertions; the
subsequent runtime edit only ordered Codex's Path import and its adapter regression
passed again.

The final Python path/hash map is `python-source-final.json`, with canonical SHA256
`fc8952e574586ef802254f35b8f4285168f8f035941c50a30748ae88b1dda318`.
It covers runtime/test Python, not a whole-plugin live gate. Earlier live manifests
do not cover this revision; all reused live observations remain limited to their
recorded epochs. No live experiment, raw audit evidence or trace fixture changed.
All 371 retained root-session files match their initial bytes, mtimes and modes;
branch and HEAD are unchanged, and the staged diff is empty. No implementation
blocker remains.

Files touched in this revision:

- Runtime: `plugins/crew/scripts/artifact_prune.py`, `crew-state.py`,
  `loop_projection.py`, `loop_state.py`, `session-start.py`;
  `plugins/crew/scripts/multiagent/build_workflow.py`, `execution.py`;
  `plugins/crew/scripts/multiagent/providers/__init__.py`, `_proc.py`, `agy.py`,
  `codex.py`, `cursor.py`.
- Tests: `plugins/crew/scripts/tests/test-build-workflow.py`, `test-hooks.py`,
  `test-multiagent.py`.
- Guidance: `.claude/CLAUDE.md`, `plugins/crew/scripts/CLAUDE.md`,
  `plugins/crew/commands/build.md`, `status.md`;
  `plugins/crew/docs/build-protocol.md`, `codex-host.md`, `phase-5-build-evidence.md`.

## Third panel revision

The three accepted causes in `run-90c156c3a80b` are corrected at another
deterministic epoch. Timeout teardown now probes the stable owned group through
bounded TERM/KILL escalation; waiting for the leader is not group proof. All four
provider adapters and their probes handle confirmed and unconfirmed timeouts.
An unconfirmed build timeout retains its claimed writer and diagnostic, accepts
no completion receipt, and requires explicit quiescence recovery before retry,
review or replacement. Ordinary provider/dispatch JSON remains compatible.

Cursor parses stream JSONL with LF-only framing, retaining a final result containing
literal Unicode separators instead of falling back to earlier assistant text.
Build, measure-twice initialization and generic init release admission before a
chain wait, then take chain before admission/state locks and recheck current
ownership. The Git baseline runs outside admission. Build CLI state-lock timeouts
return schema-1 `build_error`, exit 2 and same-command retry guidance; unadmitted
spills survive. Build capture guidance now runs its already-complete issued argv.

| Check | Result | Ephemeral log under `/private/tmp/crew-revision3-audit/` |
|---|---:|---|
| Build, settled source | 64 tests passed | `build-final.log` |
| Hooks | 674 checks passed | `hooks-final.log` |
| Review | 201 tests passed | `review-final.log` |
| Measure-twice | 131 tests passed | `measure-final.log` |
| Shared suite, isolated `crew_config()` | 1,990 checks passed | `multiagent-final.log` |
| Python 3.11 grammar | All 47 files passed; runtime 3.12.13 | `grammar.log` |
| Lint/format/whitespace | Engine, runner and build tests pass full Ruff; all scripts pass fatal checks; format and `git diff --check` pass | `ruff-targets.log`, `ruff-fatal.log` |

The local Codex fixture spawns a TERM-ignoring writer. KILL still reaches it after
the leader exits; suppressing KILL proves that the surviving writer remains fenced
and cannot retry or be replaced. Fixture processes are explicitly cleaned up.
Chained Cursor fixtures exercise an actual resume and final BLOCKED/invalid reports
containing U+2028/U+0085/U+2029. Barrier tests cover chain waits and the baseline;
overlapping real CLI processes prove busy-chain waiting and request retention, and
held admission/owner locks prove start/resume JSON errors without tracebacks.

Final Python manifest SHA256 is
`257ee7686f3c541d376eeca43aac96765b98303cf0422620b02d5ce093419c2e`
(`python-source-final.json`, sorted compact relative path/hash map). The full suites
cover these final runtime/test bytes. This is local process/fixture evidence, not
a new live provider gate. Earlier paid observations remain limited to their recorded
source epochs; no paid experiment, trace fixture or raw audit evidence was changed.
All 392 root-session files, the index hash, branch and HEAD remain unchanged.
No implementation blocker remains; the parent still owns panel and root settlement.

Revision files: `plugins/crew/scripts/crew-state.py`, `loop_state.py`;
`plugins/crew/scripts/multiagent/build_workflow.py`, `cli.py`;
`plugins/crew/scripts/multiagent/providers/_proc.py`, `agy.py`, `claude.py`,
`codex.py`, `cursor.py`; `plugins/crew/scripts/tests/test-build-workflow.py`;
`plugins/crew/commands/build.md`; `plugins/crew/docs/build-protocol.md` and this file.

## Actual CLI harness and source epochs

All live runs used the existing `scripts/tests/claude_cli_gate.py`, Claude Code
**2.1.287**, Codex **0.159.2**, and disposable repositories under
`/private/tmp/crew-phase5/`. No desktop app, host override, permission bypass,
global configuration edit or provider substitution was used. Sol's current
resolved route/model was copied into fixture-local configuration:
`model="gpt-6.1-sol"`, `via=["codex"]`. Native executor selection was explicitly
`crew:executor`; its requested model remained inherit.

Each process retains its exact `*.invocation.json`, `*.stream.jsonl`, debug/stderr
logs, `*.source-before.json`, `*.source-after.json` and `*.process.json`. The runner
uses `claude --print --output-format stream-json --verbose --include-hook-events
--plugin-dir <copied-plugin> --strict-mcp-config --mcp-config <empty-fixture-config>
--session-id <literal>`; actual re-entry replaces session-id with `--resume`.
The invocation records the exact allowed-tool list and prompt path. Runner calls
used `rtk proxy env UV_CACHE_DIR=/private/tmp/crew-phase5-uv-cache
/Users/luinstra/.local/bin/uv run --no-project --python /opt/homebrew/bin/python3
python <copied-plugin>/scripts/tests/claude_cli_gate.py --fixture <fixture>
--prompt <retained-prompt> --session-id <literal> --label <label>
[--resume] --claude /Users/luinstra/.local/bin/claude`.

Portable summaries, full source manifests, exact task/tree pins, deduplicated tool
IDs, report bytes, review evidence, operator observations and artifact digests:

- [build_baseline_trace.json](../scripts/tests/fixtures/build_baseline_trace.json)
- [build_candidate_trace.json](../scripts/tests/fixtures/build_candidate_trace.json)

These are observed traces. Candidate acceptance rules were not imposed on the
baseline. Actual baseline runs used parent Write and launched no scribes; their
role counts are retained as observed, without inventing the old recipe's possible
scribe launches. Pre-cutover authentication/premature-exit experiments are retained
and distinguished from successful runs.

Repeated JSON objects/lists of at least 1,024 canonical bytes are stored once
per file in `shared_by_sha256`; `{"$ref_sha256": "<digest>"}` references that
table, recursively. Each key is SHA256 of the **expanded** value serialized as
UTF-8 JSON with sorted keys and compact separators. Expanding references and
removing the table reproduces the original JSON exactly, including every run,
acceptance fact, source manifest, input pin, report and count. No raw audit file
under `/private/tmp/crew-phase5/` was modified.

The baseline shrank from 359,343 to 241,153 bytes (14 shared objects, 35
references); candidate from 1,133,496 to 652,694 bytes (63 objects, 143 references).
Independent expansion/hash checks passed. The expanded baseline/candidate
canonical SHA256 digests are respectively
`db826c817669081538afb1ed38838dcd0fca41c82f1d8d312df4e6b1cc287cae` and
`c8ac122d0d791ec5924650e658f2e5cb26f5ee39dc505f08f142b961a7b71571`;
details are in `/private/tmp/crew-finishing-trace-dedup.json` and
`/private/tmp/crew-finishing-source-check.log`.

The manifest digest is SHA256 of UTF-8 JSON with sorted keys and compact
separators, over the runner's full relative-path/hash dictionary:

| Epoch | Copied plugin | Manifest digest |
|---|---|---|
| Baseline recipe | `baseline-plugin` | `c5613e9cf9972afee6f4c29c4fdc5d566508631ddef83482d8bf2d88dfaa121f` |
| Initial candidate lifecycle/faults | `final-plugin` | `5d29cc7a375608731ad02b986261ad8fffe9ce0548eaf2bd7ba71d74d00d655f` |
| Settled runtime comparisons/guards/replay | `clean-plugin` | `042fac34c3f87f3827c592d1b7bc666b1beb1dcd50627d901178a2c6e609e620` |
| Final cleanup/hook runtime replay | `closure-plugin` | `731932a55bc984ee7524fd97435cc938e9c37592184bd1ecfe8e7fad61cd2bd3` |

Every recorded process retained matching before/after source hashes, including
the deliberately signalled process. Real native and Sol terminal replay passed
against the closure copy. That copy's runtime/test Python matched the working
tree **before the prior finishing correction**, including the late cleanup locking;
it is not byte identical to either the finishing tree or the post-panel revision. The passive fence extraction,
caller import changes, final regression tests, documentation and trace
deduplication are not covered by any earlier whole-plugin live manifest.
The extraction is covered by the prior deterministic finishing checks and
Python snapshot above. The post-panel changes are covered by their separate
checks and snapshot below. The completed live experiments are reused only for their
recorded source epochs; no new live behavior or measurement is claimed.

## Live observations

| Scenario | Actual evidence/outcome |
|---|---|
| Native negative → revision → approval | `paired-final-candidate-native`, session `12ef4a2f-8558-469a-aeaf-786c174fc1f7`: two fresh executor actions, real Sol blocking review, revision round 1, approved/done/inactive. Direct capture; no scribes. |
| Sol negative → revision → approval | `final-sol`, session `0c05c244-5642-4a5f-b251-1f2d6b7d1bd6`: two implementation actions, two fresh Sol reviews, real rejection and approval, unchanged known workspace guards. |
| Exact executor continuation | That Sol run retained conversation `01a0feb2-d2c5-7ce2-98e6-645c668b18f0`. Its actual Codex session metadata records both issued implementation/revision prompts and gpt-6.1-sol turns in the same conversation. Revision prompt SHA matches the issued file. Only metadata/digests were copied to `continuation-observation.json`. |
| Fresh external reviewers | The same Sol gate's exact review-prompt digests match distinct actual Codex conversations `01a0feb4-9a7c-7e43-a02a-330fa52e8d3a` and `01a0feb6-7c77-75e0-85da-6b037081fb00`, separate from its executor conversation. Metadata/digests are retained in `fresh-review-observation.json`. |
| Stop → automatic next | `final-native` and `final-sol` raw hook responses contain an actual Stop block with the same owner's literal build-next. Subsequent parent calls progressed that lifetime. No hook was manually simulated. |
| Human pause and real re-entry | `final-faults`, session `e9837a18-4951-4014-8b01-6c7a76444c81`: actual native BLOCKED parks before review; actual CLI resume submits an operator answer, with original task/bounds unchanged. A stale observation refused and issued a new question. |
| Concurrent fresh reviewers | `panel3` launches fresh foreground Opus Task and foreground Sol Bash in the same assistant message. Both actual task-start events precede either completion; the host remains alive. Exact overlap milliseconds are unavailable. |
| Partial-panel recovery | Opus's actual completed return was deliberately settled failed, explicitly labelled TEST. Real accepted Sol yields a quorum advisory. `partial-retry` launches only a fresh Opus; Sol's accepted action and SHA remain identical. Actual quorum approval finishes the lifetime. No failure was treated as an approval. |
| Cancellation/interrupted writer | `live-interruption`, session `4ec88cb6-63ae-4e8e-95cc-c991f6398c49`: real claimed native handle, cancellation ends authority while retaining its writer fence and cancellation-unavailable diagnostic. The exact owned process tree was signalled (exit 143), all four PIDs were then observed absent. Actual CLI resume applies explicit not_running recovery; owner stays cancelled, fence clears, no paid workflow work. |
| HEAD/index/branch violations | `live-guard-head`, `live-guard-index`, `live-guard-branch`: labelled operator injections after actual native claim/bind each produce workspace_guard, no review generation and zero review launches. Injected Git facts were restored only after evidence capture, then paused owners cancelled. Root Git was untouched. |
| Terminal replay | Native paired owner and ordinary Sol owner each actually resume and run same-owner next: two Bash calls, zero Agent/provider/review/synthesis launches, identical retained state. The parent CLI still incurs its own audit-turn cost. |

Cancelled/fault tests are labelled operator injections. Continuation opt-out,
provider timeout/failure variants, synthesis failure, crash boundaries and aged
cleanup race cases were verified deterministically rather than represented as
live provider incidents. Unmodified live gates observed final done/inactive;
the intermediate done checkpoint is verified by the explicit deterministic test.

## Comparable orchestration observations

The native pair uses identical raw task arguments, `.gitignore` and acceptance
file bytes, executor sentinel, Sol panel/model and one revision round. The ordinary
Sol pair uses identical task/tree/panel/route/model and zero revision rounds.
Actual Skill args match the pins in both pairs. Source paths/session IDs differ
as required for isolation. Fault/audit processes are excluded from these counts.

| Pair | Parent message IDs, baseline → candidate | Parent tool IDs | Paid native roles | External command IDs | Scribes |
|---|---:|---:|---|---:|---:|
| Native, real negative/revision | 24 → 29 | 27 → 34 | 2 executor → 2 executor | 2 → 2 reviews | 0 → 0 |
| Sol, ordinary approval | 20 → 15 | 20 → 19 | 0 → 0 | 2 → 2 (implementation + review) | 0 → 0 |

The native increase includes explicit owner claims/binding/capture and engine
projection calls. The Sol reduction is one observed run, not a general saving.
Latest CLI-reported costs were native $0.8231578 → $1.0847686 and Sol $0.5821066
→ $0.511904. These are host-reported snapshots, not complete cross-provider bills.
Full token/cache/model fields are retained; cumulative snapshots/resumes are never
summed. CLI duration/turn fields have their own scope and can change across Stop
continuations; they are not inferred process wall time or orchestration overhead.
Complete provider intervals, overlap duration, orchestration-time deltas and a
numeric host ceiling are **null/unknown**. No timing saving is asserted.

A separate identical negative Sol experiment returned BLOCKED in the candidate
while the old recipe advanced. That candidate correctly purchased no review;
it is not a comparable completed ordinary run. Earlier audit-file contamination,
wrong-executable instructions and background-parent exits likewise remain failed
experiments, excluded from ordinary deltas. The live two-round Sol continuation
gate above is independent of the ordinary cost pair.

Hard gates passed: zero paid workflow actions on retained terminal replay,
zero candidate bookkeeping scribes with direct capture, concurrent reviewers,
only engine-authorized automatic/human retry work, and no extra model roles for
selection, hashing or envelope construction. Claims beyond the recorded CLI,
source epochs and supplied host transport are unsupported. In particular, actual
CLI re-entry does not establish every interactive app resume behavior.

The actual 2.1.287 tool inventory lacked TaskOutput. An exploratory background
Sol reviewer was stopped when its parent exited; its real notification and bound
recovery are retained. Successful overlap used concurrent foreground calls.
The host accepted 7,260,000 ms foreground allowance; its maximum remains unknown.
No runtime fallback, host ceiling or automatic cancellation capability was invented.

## Fourth panel revision

The three accepted structural groups in `run-e4b3fa843cf6` are corrected. This
pass records deterministic and harmless local-process verification, not a new
panel verdict or paid/live host experiment.

- Every subprocess completion checks the owned process group. Ordinary success
  and failure terminate surviving descendants to the same bounded deadline as
  timeout; communication exceptions also attempt group teardown. An unconfirmed
  shutdown reaches the adapters as an internal transport fact and retains the
  build writer fence, with no retry or review. Confirmed ordinary completions keep
  their tuple/output contract; provider serialization still omits internal facts.
- Incomplete chained workspace observations reach build admission before
  continuation setup rejects them. Native and chained claims park with a bound
  `workspace_guard`, without launch. Recovery digesting represents target
  resolution failure explicitly alongside the available Git facts, rather than
  throwing or inventing a complete snapshot. Unknown guards remain non-waivable.
- Active current-owner `next` reconciles a retained submission before projecting
  waiting, under the owner lock. Receipt schema, owner/action, frozen route,
  before/after facts, transport/report status, diagnostic and both content hashes
  are validated through the same acceptance path. Missing, malformed or foreign
  evidence preserves uncertainty and produces structured errors. Replay neither
  launches work nor repeats acceptance/retry counters or overwrites artifacts;
  cancelled and expired owners do not advance through automatic replay.

The local-process regression covers normal leader success, normal leader failure,
timeout and an injected communication exception, each with confirmed termination
and with deliberately suppressed KILL. Descendants ignore TERM and write harmless
heartbeat files; fixture cleanup verifies their absence. Adapter regressions also
exercise uncertain transport through Codex, Cursor and Agy. Real disposable Git
fixtures cover missing metadata, corrupt index and unmerged index on native and
continuation-enabled external routes, including CLI JSON and restoration through
the current question. Native bound-artifact capture and the Codex adapter both
exercise failure of the actual owner-state save after durable result publication,
then exactly-once CLI replay. Sixteen invalid retained-evidence variants verify
structured rejection, unchanged artifacts/counters and retained writer authority.

All final suites ran after the final runtime and test edits with Python 3.12.13.
Commands used `rtk proxy env UV_CACHE_DIR=/private/tmp/crew-uv-cache uv run
--no-project python plugins/crew/scripts/tests/<suite>.py`. Multiagent used the
same interpreter/environment with an importlib loader and `with m.crew_config():
m.main()` to isolate the user's global model overrides.

| Revision check | Result | Ephemeral local log under `/private/tmp/crew-revision4-audit/` |
|---|---:|---|
| Build workflow | 67 tests passed | `build-final.log` |
| Hooks | 674 checks passed | `hooks-final.log` |
| Review workflow | 201 tests passed | `review-final.log` |
| Measure-twice | 131 tests passed | `measure-final.log` |
| Multiagent, isolated configuration | 1,990 checks passed | `multiagent-final.log` |
| Python 3.11 grammar | All 47 Python files passed; checker Python 3.14.3 | `grammar-source.log` |
| Ruff, build engine/execution/runner/build tests | Passed | `ruff-focused.log` |
| Ruff, fatal syntax/name checks across scripts | Passed | `ruff-fatal.log` |
| Whitespace | `git diff --check` and changed-file trailing whitespace checks passed | `whitespace.log` |

The final 47-file Python path/hash manifest is `python-source-manifest.json` in
that ephemeral audit directory. Its canonical JSON SHA256 is
`5b7c08090e15f29e2a83a407e7e69a4f5050a66b2cb30b5bd1032945ad5118c5`.
All final suites cover those bytes. The grammar check establishes syntax
compatibility, not a test execution on Python 3.11. Earlier complete plugin
manifests and live traces do not cover this revision; their observations remain
limited to the source epochs recorded above. No trace or raw prior audit was
changed. No unresolved implementation blocker remains in these accepted groups.

This revision touched only `multiagent/build_workflow.py`, `execution.py`,
`providers/_proc.py`, `codex.py`, `cursor.py`, `agy.py`, `claude.py`,
`tests/test-build-workflow.py` (all below `plugins/crew/scripts/`) and this evidence
document. The audit confirms all 413 root-session files unchanged in bytes,
mtime, mode and inventory; branch, HEAD and index remain unchanged, with no
staged changes. The root's installed workflow was never operated by candidate
commands, hooks or tests.

## Fifth panel revision

The four accepted structural classes from `run-8105b75ee24b` are corrected:

- An unanswered bound question restores `awaiting_input` on `next`, including
  after the Stop parked-turn cap clears it. Outstanding writers project waiting
  with exact recovery argv ahead of questions. Unconfirmed transport no longer
  issues an unanswerable execution decision; explicit human `not_running`
  recovery clears the fence and then parks the execution recovery question.
- External review drift and ordinary execution now share the post-probe owner
  and review locks, exact ReviewRef/generation admission and action snapshot
  revalidation. Stale generation probes cannot park or claim a newer generation;
  a competing claim/settlement also prevents obsolete drift parking.
- Revision prompts instruct executors to read validated retained feedback files
  in full. Reports remain complete in their immutable files. Preparation,
  cached-prompt retries and claim admission verify readability/UTF-8; missing
  files park feedback recovery before another executor launch. Fixture reports
  exceed 330 KiB while the revision prompts remain below 16 KiB and pass through
  the actual Cursor/Agy adapters with deterministic subprocess returns.
- Build admission identifies its replacement file in both locked checks, so an
  inactive safety-exited planning owner does not veto a different new build.
  Planning replacement refusal, active owner checks, load-status safeguards and
  current writer fences remain enforced.

Tests exercise capped Stop -> CLI next -> allowed Stop, generation replacement
and concurrent claims during unlocked probes, reference-preserving retries and
missing feedback at preparation and claim, uncertain adapter/real local child
outcomes followed by recovery, and cross-workflow admission. No paid/live
experiment or panel seat was launched or rerun, including Luna.

Final verification used the same isolated commands as the fourth revision, with
Python 3.12.13 and outer `crew_config()` for the shared suite. Logs under
`/private/tmp/crew-revision5-audit/` are ephemeral local audit evidence:

| Check | Result | Local log |
|---|---:|---|
| Build workflow, settled source | 72 tests passed | `build-settled-final.log` |
| Hooks | 674 checks passed | `hooks-final.log` |
| Review workflow | 201 tests passed | `review-final.log` |
| Measure-twice | 131 tests passed | `measure-final.log` |
| Multiagent, isolated configuration | 1,990 checks passed | `multiagent-final.log` |
| Python 3.11 grammar | All 47 Python files passed; checker 3.14.3 | `grammar-source-final.log` |
| Ruff, build engine/test and fatal checks across scripts | Passed | `ruff-focused-final.log`, `ruff-fatal.log` |
| Whitespace | Diff and changed-file checks passed | `whitespace.log` |

The initial full build run found one obsolete assertion requiring inline feedback.
It now verifies the referenced file and unchanged report bytes; the entire build
suite was rerun successfully. Other suites ran after the final runtime edits and
were not repeated for that test-only correction. The final 47-file Python
manifest, `python-source-manifest.json`, has canonical SHA256
`4378b54b6cb758184fdc5e7ef5796463a30b9680dd6dbbc5f722c66a4caeae05`.
The grammar check is not a test execution on Python 3.11. Earlier live manifests
and observations apply only to their recorded source epochs and do not cover
these changes; no new live behavior is claimed.

This pass changed `scripts/multiagent/build_workflow.py`, `review_workflow.py`,
`scripts/loop_state.py`, `scripts/tests/test-build-workflow.py`,
`commands/build.md` and this document, all under `plugins/crew/`. All 434 root
session files remained unchanged in bytes, mtime, mode and inventory. Branch,
HEAD and index remained unchanged, with nothing staged or committed. No
unresolved implementation blocker remains in the accepted revision scope.


## Sixth panel revision

The accepted blockers from `run-c5caa7ce05c2` are resolved at the shared boundaries:

- Executor reports remain complete in owner-scoped return artifacts. Review
  identities freeze the path and SHA256; prompt construction/replay verifies the
  artifact and gives explicit read instructions. Revision feedback also verifies
  accepted executor report hashes. A 420,043-byte report passes actual Cursor/Agy
  prompt construction on initial execution and panel retry without being inlined.
- Executor probes revalidate the current action, status, route, policy, workspace
  baseline and review identity under the owner lock before parking or claiming.
  Review probes retain exact ref/action/policy admission. Both compare effective
  floors with the frozen base timeout, including decreases below a floored action.
- Native build review launch uses the same complete projection as issuance,
  reconstructed from the authoritative claim and checked against the owner.
  Fake-runtime tests reject altered outer fields, commands and foreign references
  before any launch, including an otherwise valid claim from another build.
- BLOCKED and invalid-report questions expose the verified retained report path,
  hash and bound answer/retry/cancel choices. CLI projections stay bounded; reports
  are not truncated or copied into the question.
- Process waits observe exit without reaping while owned-group signals are sent.
  macOS Python 3.12.13 exercises kqueue NOTE_EXIT; Python 3.14.3 exercises
  waitid/WNOWAIT. Only read-only group probes follow the sole real reap. Unsupported
  or failed observation, custom SIGCHLD dispositions and unconfirmed group absence
  retain writer uncertainty. No automatic quiescence attestation is introduced.

Deterministic fixtures use disposable repositories/sessions and subprocess returns;
real harmless local children cover ordinary success/failure, timeout and injected
communication failure, with confirmed and suppressed-KILL outcomes. Instrumented
signals precede leader reaping; a simulated reused group after reap receives no
signal. Fixture cleanup uses a child exit file, not post-reap group signals.

Verification logs under `/private/tmp/crew-revision6-audit/` are ephemeral:

| Check | Result | Local log |
|---|---:|---|
| Final functional build source, Python 3.12.13 | 78 passed | `build-final4.log` |
| Hooks | 674 passed | `hooks-final2.log` |
| Review workflow | 201 passed | `review-final2.log` |
| Measure-twice | 131 passed | `measure-final2.log` |
| Multiagent with outer isolated `crew_config()` | 1,990 passed | `multiagent-final.log` |
| Final shared provider descendant checks | 6 passed | `shared-process-final.log` |
| Final functional waitid/identity regressions, Python 3.14.3 | 2 passed | `waitid-process-final.log` |
| Final byte observer regression, Python 3.12.13 | 1 passed | `observer-final-byte-check.log` |
| Python 3.11 grammar; Ruff focused/fatal; whitespace | Passed | Final audit records |

Initial runs failed while signal handling and stale test fixtures/assertions were
being corrected; the reported final checks pass. Shared suites started at the
47-file source snapshot `shared-suite-source.json` (canonical SHA256
`5291bbaee14041a7539ee73b705678b224d6ef39b4c15caa18c35e4648845b61`).
Late changes tightened build feedback hashes and conservative observer error
handling; the multiagent run spans the finishing edits. The final full build and
focused provider/identity checks verify those affected paths. A subsequent change
only added a Ruff suppression comment; the final byte observer check followed it.
The final 47-file Python manifest `final-python-source.json` has canonical SHA256
`1072efd2f0fd495b2276692c87e3dedfbf4cec936198c5d2229e34434ca5f5cb`.
All 47 files parse with Python 3.11 grammar; no Python 3.11 runtime execution is
claimed. Earlier live/plugin manifests remain limited to their recorded epochs
and do not cover this revision. No paid/live experiment or review seat was rerun.

This revision changed `scripts/multiagent/build_workflow.py`,
`review_workflow.py`, `workflow_transport.py`, `providers/_proc.py`,
`scripts/tests/test-build-workflow.py` and this document, under `plugins/crew/`.
All 455 protected root-session files retain their bytes, mtime, mode and inventory.
Branch, HEAD and index are unchanged; nothing is staged or committed. No unresolved
implementation blocker remains in the accepted scope.

## Seventh panel revision

The two accepted structural causes from `run-045917b7552b` are resolved:

- Recovery question identities include the existing durable accepted-decision
  sequence. A genuine recurrence after an accepted answer gets a fresh identity;
  an unanswered question and replay of an accepted answer remain stable. Repeated
  route faults and unsuccessful feedback repairs recover without duplicate work,
  budget changes or a new history subsystem.
- Invalid UTF-8 executor returns retain their original bytes and hashes. Retries
  reference a complete, readable companion artifact bound to the owner, action
  and original hash. It explicitly labels escaped undecodable bytes, prefixes
  report lines and makes no completion assertion. Prompts reference this artifact
  rather than inline it. Publication failure leaves the decision unconsumed;
  missing companions can be reproduced from verified original bytes. Missing,
  altered or unsafe evidence retains the existing recovery/error safeguards.

Deterministic native and actual external-provider adapter fixtures cover reports
over 340 KB, both answer/retry choices, failed publication, repeated decisions,
old-answer replay, missing/corrupt evidence and unsafe paths. Original return,
receipt and submission bytes and mtimes remain unchanged through successful
recovery. No provider/model call, live experiment or review seat was rerun.

Verification logs under `/private/tmp/crew-revision7-audit/` are ephemeral:

| Check | Result | Local log |
|---|---:|---|
| Build workflow, Python 3.12.13 | 82 passed | `build-final2.log` |
| Hooks | 674 passed | `hooks-final.log` |
| Review workflow | 201 passed | `review-final.log` |
| Measure-twice | 131 passed | `measure-final.log` |
| Multiagent with outer isolated `crew_config()` | 1,990 passed | `multiagent-final.log` |
| Python 3.11 grammar, all 47 Python files | Passed | `grammar-source-final.log` |
| Ruff focused/fatal; whitespace | Passed | Final audit records |

Initial build runs exposed test assertions using nonexistent budget fields; those
assertions were corrected and the full build suite rerun successfully. All final
suites ran after the final Python source settled. Its 47-file manifest,
`python-source-manifest.json`, has canonical SHA256
`6db5776410656d3add8966201d1527023b1e0c70c520b9817e8569ecbc45b796`.
The grammar checker was Python 3.14.3, not a Python 3.11 runtime execution. Earlier
live/plugin manifests remain limited to their recorded source epochs and do not
cover this revision. No live evidence or trace was rewritten.

This revision changed `scripts/multiagent/build_workflow.py`,
`scripts/tests/test-build-workflow.py` and this document, under `plugins/crew/`.
All 476 protected root-session files retain their bytes, mtime, mode and inventory.
Branch, HEAD and index are unchanged; nothing is staged or committed. No unresolved
implementation blocker remains in the accepted scope.

## Eighth panel revision

The two accepted causes from `run-c42924bf8510` are resolved:

- Cursor constructs authoritative stream output from strict UTF-8 decoding of
  the original bytes, separately from ordinary display normalization. Malformed
  stream bytes are retained unchanged, including when corruption is outside the
  terminal JSON result. Fresh and resumed execution park as `invalid_report`
  before review; bound retries use the existing readable companion feedback.
  Valid stream result extraction and ordinary display/serialization remain intact.
- CLI `build-capture` delegates file reading to an owner-locked engine seam.
  It validates the current claimed/settled native action and exact issued regular
  host-return artifact using existing namespace safeguards before reading.
  Unclaimed, stale, foreign and external actions, alternate paths, traversal,
  missing/non-files and namespace symlinks are rejected without reading ingress
  bytes or changing owner state/evidence. Exact-path capture and receipt replay
  pass; the existing in-memory transport remains available.

Verification logs under `/private/tmp/crew-revision8-audit/` are ephemeral:

| Check | Result | Local log |
|---|---:|---|
| Focused adapter/build/CLI and adjacent regressions | 4 passed | `focused-second.log` |
| Full build workflow | 84 passed | `build-final.log` |
| Affected shared Cursor/CLI, isolated `crew_config()` | 234 passed | `shared-cursor-cli-final2.log` |
| Hooks | 674 passed | `hooks-final.log` |
| Python 3.11 grammar, all 47 Python files | Passed | `grammar-source-final.log` |
| Ruff focused engine/tests and fatal checks across scripts | Passed | `ruff-focused.log`, `ruff-fatal.log` |
| Cursor/CLI broad lint delta; whitespace | No new findings; passed | Final audit records |

The shared subset runs `test_result_contract`, `test_cursor_continuation`,
`test_cursor`, `test_provider_readonly_cwd`, `test_dispatch_chain_e2e_fake_clis`,
`test_dispatch`, `test_dispatch_options` and `test_path_arg_anchoring` from
`test-multiagent.py`. All fixtures are disposable and provider returns/binaries
are deterministic. Initial focused tests omitted the required decision digest;
the assertion was corrected. The first shared wrapper used an incorrect counter
name after its checks; the corrected wrapper and affected subset were rerun.
The final checks pass. Broad lint still reports 19 existing CLI and 6 existing
Cursor findings, verified against hash-matching pre-revision bytes; none were
added or expanded into this pass.

Final suites ran on Python 3.12.13 after Python source settled. The 47-file
`python-source-manifest.json` has canonical SHA256
`c6e2c64dfed44cd171f6a170012aac3129aee809a9f0664d3d3fdddd4e7a8175`.
Grammar validation used Python 3.14.3, not a Python 3.11 runtime. Unaffected
review/measure-twice/full multiagent suites were not repeated; earlier live/plugin
evidence remains limited to its recorded source epochs. No paid/live experiment,
review seat, process teardown change or trace rewrite was performed.

This revision changed `scripts/multiagent/providers/cursor.py`,
`scripts/multiagent/build_workflow.py`, `scripts/multiagent/cli.py`,
`scripts/tests/test-build-workflow.py` and this document, under `plugins/crew/`.
All 497 protected root-session files retain their bytes, mtime, mode and inventory.
Branch, HEAD and index are unchanged; everything remains unstaged and uncommitted.
No unresolved implementation blocker remains in the accepted revision scope.

## Ninth panel revision

The two accepted causes from `run-615f7ba021fe` are resolved:

- Cursor report authority uses complete LF-framed JSON records, separately from
  permissive display parsing. A structured report requires one successful final
  result with unambiguous string content and consistent session identity, including
  the requested ID on resume. Malformed/truncated data, missing/failed terminal
  responses, duplicate keys/results and trailing data retain their original bytes.
  An in-process authority fact is excluded from ordinary provider JSON and bound
  into the existing engine receipt. Exit-zero ambiguous write reports park as
  `invalid_report` without automatic retry/review, even if the raw evidence itself
  ends with a valid-looking COMPLETED marker. Receipt replay preserves that result.
  Valid plain-stdout execution and ordinary display/envelope behavior remain intact.
- The journal freezes the original round prompt path and SHA256 after publication.
  Automatic attempts verify and copy the same bytes; claim and launch-payload
  checks bind those bytes to the frozen digest. Changed, missing or unsafe cached
  prompts authorize no additional writer or attempt/budget advancement. Existing
  feedback recovery/errors apply. Explicit executor answers/retries and review
  revisions clear both identity fields when intentionally beginning a new round.

Adapter/build tests cover thirteen malformed/ambiguous response classes, including
  resumed identity loss and a crash between receipt publication and state save.
Prompt tests cover changed/missing/symlink/non-file cache artifacts, repair to the
original bytes, drift after retry issuance, a payload repaired before admission,
identical retries and intentional round changes. Fixtures are disposable and
provider returns/binaries deterministic; no paid/live experiment or panel was run.

Verification logs under `/private/tmp/crew-revision9-audit/` are ephemeral:

| Check | Result | Local log |
|---|---:|---|
| Full build workflow | 87 passed | `build-final.log` |
| Hooks | 674 passed | `hooks-final.log` |
| Affected shared Cursor/CLI subset, isolated configuration | 255 passed; one stale assertion corrected below | `shared-cursor-cli-final.log` |
| Corrected provider result contract | 10 passed | `shared-contract-final.log` |
| Python 3.11 grammar, all 47 Python files | Passed | `grammar-source-final.log` |
| Ruff focused engine/leaf/build tests; fatal checks across scripts | Passed | `ruff-focused.log`, `ruff-fatal-final.log` |
| Provider broad lint delta; whitespace | No new findings; passed | Final audit records |

The shared subset is the eighth revision's eight functions plus
`test_from_dict_and_escaping`. Its sole failure was an assertion comparing
`asdict` with the serialized result without excluding the new in-process authority
fact. The fixture now sets that fact false and excludes it from the expected
six-field JSON. Its entire ten-check contract was rerun successfully; the other
246 checks were not repeated for that test-only change. The 256 distinct affected
shared checks therefore pass. Existing broad lint findings remain unchanged:
six in Cursor and four in the provider contract module.

Final runtime suites ran on Python 3.12.13 at the 47-file
`python-source-manifest.json` epoch (canonical SHA256
`7ec08b02a425e396f9b6546bcb7df34315e98c800e5468e923df11a398990f66`).
Only the shared assertion fixture changed afterward; no runtime code changed.
The final `final-python-source-manifest.json` has canonical SHA256
`c7bf79bec22b7fd20116ba7c13981095254db825fbc3770bf3a1d1c0c6985bf8`.
Grammar validation used Python 3.14.3, not a Python 3.11 runtime. Earlier full
review/measure-twice/shared suites and live/plugin evidence remain limited to
their recorded source epochs. No process teardown, bound/reconciliation ordering,
provider failure-retention policy or trace was revised.

This pass changed `scripts/build_state.py`, `scripts/multiagent/build_workflow.py`,
`scripts/multiagent/providers/{cursor.py,__init__.py}`,
`scripts/tests/{test-build-workflow.py,test-multiagent.py}` and this document,
under `plugins/crew/`. All 518 protected root-session files retain their bytes,
mtime, mode and inventory. Branch, HEAD and index are unchanged; everything remains
unstaged and uncommitted. No accepted implementation blocker remains unresolved.

## Tenth panel revision

The two accepted causes from `run-5811fec7b9ee` are resolved. Build opts into an
exact-report flag through the shared write/provider boundary. Agy and Cursor
recognize valid build reports before scanning their prose for authentication
phrases; Cursor also requires authoritative stream evidence. Original report
bytes remain unchanged. Authentication banners, stderr authentication diagnostics,
failed terminal records and actual nonzero/timeout/cancelled/unconfirmed transport
outcomes retain precedence. Ordinary dispatch and provider JSON/display contracts
keep their existing behavior. The strict marker parser now lives in the passive
`build_state.py` leaf and is shared with the workflow; providers import no engine.

Next/resume replays verified current-owner receipts/submissions before projecting
terminal state. Cancellation, done phase and safety bounds make the owner inactive
before acceptance, so replay only finalizes its writer fence and accepts once;
it cannot issue retries/reviews or change terminal reasons and budgets. Invalid
retained evidence preserves the fence and inactive authority, with a diagnostic
in the terminal step. Active-owner validation errors retain their existing contract.
Force-exit admission and restart behavior were not changed.

Deterministic regressions exercise real Agy/plain Cursor/chained Cursor adapters
through build execution, valid reports quoting authentication/error phrases,
genuine banners and failed transports with misleading markers. BLOCKED produces
its bound question after one launch, with no automatic retry or review. Actual
native capture and external Codex settlement cover durable publication followed
by failed owner-state save for completed/blocked/failed returns: active, cancelled,
newly bounded, already force-exited and done owners, repeated next/resume and
immutable artifacts. Missing/corrupt/foreign/unconfirmed evidence retains uncertainty.
The first full run exposed cancellation projection errors for corrupt evidence
and an obsolete explicit-recovery test after verified finalization; both were
corrected before the final suites below.

Final checks used disposable repositories/sessions and isolated configuration.
Local logs under `/private/tmp/crew-revision10-audit/` are ephemeral:

| Check | Result | Local log |
|---|---:|---|
| Full build workflow | 89 passed | `build-settled.log` |
| Hooks | 674 passed | `hooks-settled.log` |
| Affected shared Agy/Cursor/provider/dispatch/CLI subset | 287 passed | `shared-settled.log` |
| Python 3.11 grammar, all 47 Python files | Passed | `grammar-final.log` |
| Ruff focused leaf/execution/workflow/build tests; fatal script checks | Passed | `ruff-focused-final.log`, `ruff-fatal-final.log` |
| Provider broad lint delta; whitespace and Git/root guards | No new findings; passed | Audit records |

All final runtime suites ran on Python 3.12.13 at the 47-file
`python-source-final-manifest.json` epoch (canonical SHA256
`cd8de975c00456ecdfca1ca43410b66eedbab302564afba14f677d1a7e2c5503`).
No Python source changed afterward. Grammar validation used Python 3.14.3, not a
Python 3.11 runtime. Earlier full shared/review/measure-twice and live/plugin
evidence remains limited to its recorded source epochs; no new paid/live work,
panel, process-teardown change or trace rewrite was performed.

This pass changed `scripts/build_state.py`, `scripts/multiagent/{execution.py,
build_workflow.py}`, `scripts/multiagent/providers/{__init__.py,agy.py,cursor.py,
codex.py,claude.py}`, `scripts/tests/test-build-workflow.py` and this document,
under `plugins/crew/`. All 539 protected root-session files retain their bytes,
mtime, mode and inventory. Branch, HEAD and index are unchanged; all changes remain
unstaged and uncommitted. No accepted implementation blocker remains unresolved.
