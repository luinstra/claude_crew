# Phase 4 implementation and validation evidence

Initial record: 2026-10-01; correctness revision: 2026-10-02. Source baseline: `b39f647` on
`codex/multi-harness-engine`. Changes are intentionally uncommitted and unstaged.
The reviewed plan, roadmap handoff, operator follow-ups and root guidance remain
byte-for-byte unchanged. This record adds implementation evidence without
rewriting the hash-bound planning inputs.

**Earlier source epochs exercised the required Phase 4 Claude Code CLI lifecycle cases**, including
default Stop next-argv continuation, bound human pause/session re-entry, a real
blocking revision, interrupted claims, partial-panel decisions, cancellation,
and done-before-deactivate recovery. Evidence below separates retained failures,
controlled fixture faults, source-change epochs and stable successful processes.
Direct capture was audited against actual native hand-back bytes and accepted
receipts. No compaction event or mechanical advisor write confinement is claimed.
Phase 5 and OpenHands work were not started; Cursor's deferred lifecycle gate
remains owed. The installed schema-3 root build remains untouched and active. The correctness
revision below records a new source epoch; the next configured panel is pending,
and this record does not certify the whole revised candidate as complete.

## Milestones

| Milestone | Implemented evidence |
|---|---|
| 1. Characterization/state extraction | Pinned legacy recipe trace; loop_state typed evidence, transactions and common verdict guard; compatibility CLI remains |
| 2. State/review composition | Schema 4 optional journal; loop_review binding; pointer-independent preparation; accepted-action evidence; owner/attempt receipts; pinned schema-3 downgrade fixtures |
| 3. Measure-twice actions | Code-rendered interview; strict request grammar; frozen source; fresh advisor actions; owned promotion; decisions; explicit safety restart and legacy quiescence |
| 4. Transport | Issued argv; deterministic capture/hash/envelope helpers; consumed submission replay; injected notification runtime; whole native batch launched before first wait |
| 5. Command/hook cutover | Thin command adapter; mt-only lifecycle projection; cancellation/status; bound/pending run protection; advisor and maintainer/user docs; build arms preserved |
| 6. Regression/evidence | Four green suites, deterministic paired trace, actual Claude Code CLI lifecycle cases and exact direct-return audits |

The final plan review's minor clarifications are implemented in
[measure-twice-protocol.md](measure-twice-protocol.md): stable ordered question
keys/identities, exact typed request and UTF-8 JSON file grammar, whole-task
single-document detection, readable-source diagnostics, static production
advisor admission in HostRoles, and explicit synthesis-only retry preserving
successful reviewers. The stage machine is host-neutral. Only Claude's existing
advisor/inherit route is admitted in production; no route or global config was
changed to make tests pass.

## Checks

Run all suites with Python 3.11+ and a process-local PATH whose `python3` resolves
to that interpreter. This machine used `/opt/homebrew/bin/python3` (3.14.5) and
`PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`. New modules also parse with
Python 3.11 grammar; an actual 3.11 interpreter is not installed here.

```bash
rtk proxy env PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin /opt/homebrew/bin/python3 plugins/crew/scripts/tests/test-measure-twice.py
rtk proxy env PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin /opt/homebrew/bin/python3 plugins/crew/scripts/tests/test-review-workflow.py
rtk proxy env PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin /opt/homebrew/bin/python3 plugins/crew/scripts/tests/test-hooks.py
```

Multiagent catalog-default checks use the suite's existing isolation helper;
personal Sol/Grok overrides are not source regressions:

```bash
rtk proxy env PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin /opt/homebrew/bin/python3 - <<'PYTEST'
import runpy
suite = runpy.run_path('plugins/crew/scripts/tests/test-multiagent.py')
with suite['crew_config']():
    suite['main']()
PYTEST
```

The prior epoch’s four suites were rerun after the native boundary and persisted-counter fixes.
Current revision results are recorded below.
Logs are `/private/tmp/crew-phase4-final-{measure,review,hooks,multiagent}.log`.
No provider/catalog/global configuration changes were made.

| Check | Result |
|---|---:|
| test-measure-twice.py | 76 tests pass |
| test-review-workflow.py (unchanged test file) | 197 tests pass |
| test-hooks.py | 674 checks pass |
| test-multiagent.py under crew_config isolation | 2026 checks pass |
| Python 3.11 grammar parse | 18 changed/new modules pass |
| Documentation contract synchronization after evidence/status updates | 7 checks pass |
| mt command/projection deletion check | No legacy prep/persist/repair/collect/verdict recipe |
| git diff --check | Pass |

The multiagent count changes because mt's deleted Markdown recipe sentinels were replaced by protocol
checks and the new behavioral suite; build recipe goldens remain.

Behavioral evidence covers input/admission, exact source bytes, claim/replay,
sealing/promotion crash windows and late writes, preparation creation-before-bind
crashes/config changes, accepted outcome/application/finalization crashes, every
verdict path, advisory force and target retry, synthesis-only and partial-seat
retry, retained raw formatter failures, safety/owner fences, concurrent actual
Stop-counter writes, bounded lock failure, legacy prior-verdict/quiescence/
completion adoption and schema-3 downgrade application mutation/cleanup.
The old raw whole-state save helper remains a fresh replacement operation;
live application writers use guarded update_state_json.

The cutover deletes the mt panel/prep/persist/repair/collect/verdict recipe from
command and lifecycle guidance. Shared legacy verbs remain for build. The command
is now a thin transport adapter for issued steps; this is an ownership change,
not a claim that the total codebase shrank. Typed state/recovery and behavioral
tests add code.

## Deterministic trace and cost limits

Fixtures:

- `scripts/tests/fixtures/measure_twice_baseline_trace.json`: recipe
  characterization from baseline command/state/review contracts, not a live
  tool/token trace.
- `scripts/tests/fixtures/measure_twice_migrated_trace.json`: reproduced public
  API/direct-runtime trace with identical target UTF-8 bytes/SHA-256, seats and
  pins. Its timestamps are simulated units, not provider latency.

Reproduce the migrated artifact without touching installed/live loop state:

```bash
rtk proxy env PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin /opt/homebrew/bin/python3 plugins/crew/scripts/tests/measure_twice_trace.py
```

| Invocation count | Legacy characterization | Direct code runtime | Claude Markdown contract |
|---|---:|---:|---:|
| Advisor | 1 | 1 | 1 |
| Reviewers | 2 | 2 | 2 |
| Formatter | 0 | 0 | 0 |
| Reviewer scribes | 2 | 0 | 2 retained fallback; 0 with demonstrated direct capture |
| Parent synthesis | 1 | 1 | 1 |
| Paid work on terminal replay | Not observed | 0 | 0 observed in CLI fixtures |

The fake admits both reviewers before reading any completion notification;
no polling, duplicate reviewer/advisor or model-generated hash/envelope is used.
A separate fake fixture consumes two provider-labelled test pins already frozen
on the native seam. That fixture patches admission metadata only in its temporary
process; it is not evidence of real multi-provider native support. Production
model selection and native/external route admission are unchanged.

The deterministic fixtures contain no live orchestrator token count, provider
interval, wall time, scribe fidelity or interrupted-resume cost. Their fields
remain null; the separate CLI observation below does not establish a baseline
speedup. Claude advisor completion reports use parent Write without an extra
bookkeeping scribe. Reviewer Task returns retain issued scribe transport and a
distinct fallback for surfaces without demonstrated direct capture. The scribe
bridge and advisor staging restrictions remain
advisory, not mechanically enforced byte or write boundaries. Existing review
provider settlement deadlines and the loop's absolute budget remain in force.
Non-POSIX state writes retain the existing loud unlocked degradation.

## Installed-host gate and retained failures

This build is orchestrated by installed Crew 0.83.1. The existing other-session
build (`01a0f9e3-b687-7e63-b9e5-d1e8a07774b5`) was inspected read-only and remains
active with schema 3. No upgraded source state verb targeted it or any live
installed measure-twice state. Every source API exercise used a temporary project
and explicit isolated session. No installed package or global configuration was
changed.

The earlier computer-use availability probe targeted the Claude desktop app
and returned exactly:
`Computer Use was not approved to use Claude`.
That probe did not exercise Claude Code. It cannot establish that the real
Claude Code CLI host route is unavailable.

### Real Claude Code CLI observation (2026-10-01)

The operator ran Claude Code CLI **2.1.287** with `--plugin-dir` in isolated
project `/private/tmp/crew-phase4-cli-gate-01a0f9e3`. The stream's init event
identifies `crew@inline`, manifest version `0.83.1`, loaded from the candidate
path `/Volumes/Dock/Dev/GitHub/luinstra/claude_crew/plugins/crew`. These were real
plugin hooks and native Agent roles. Manifest version alone does not identify
the uncommitted candidate bytes.

Evidence retained in that project: `session-stream.jsonl`, `claude-debug.log`,
`stop-probe-issued.txt`, `stop-probe-continuation.txt`, `validation-summary.txt`,
and its `.crew/` plans, bound review, accepted receipts and final state. This
follow-up read relevant events/commands and those artifacts; it did not mutate
them or the repository's live schema-3 build loop.

- Session: `370c8b91-15be-467d-a225-06d4a267f8b4`; lifetime:
  `7bcfdc47-4462-48bb-a8aa-6b6fd8797551`; initial action: `action-0001`.
- `STOP_PROBE_READY` ended the active turn with no claimed work or human wait.
  Stream line 123 and the debug hook response show an actual default Stop block
  at fire 1/150. Line 131 records Claude's automatic re-entry and explicit report:
  "It issued no explicit next command". Claude then guessed the documented
  `measure-twice-resume --session-id ...` command (line 142). Owner/action were
  retained, but this **fails the required next-argv guidance gate**.
- Initial planning used a fresh native `crew:advisor`, inheriting the parent
  model by omitting the Agent model parameter. The panel used external `sol`
  (`codex`, `gpt-6.1-sol`) and native `crew:reviewer`/`opus`. The native return
  used one actual fresh `crew:scribe`/`haiku`; synthesis ran in the parent.
  The external reviewer was launched before the native wait and overlapped it;
  the reported external result interval was 40.8 seconds. The fallback was not
  exercised. These observations do not change any model or route configuration.
- Parent synthesis first captured `--verdict APPROVED --minor-only`. The
  validator correctly refused `invalid_submission`; the parent inspected source
  and resubmitted APPROVED without the flag. This was a guidance defect, not a
  reason to weaken judgment validation or add a second policy owner.
- The plan reached `terminal`, `approved`, verdict `APPROVED`, no overrides;
  review `run-9e63b0dd7e4d` / `attempt-0001` had 2/2 usable reviewers. Final
  state is inactive, `phase=done`, `exit_kind=approved`, with six stop fires.
  Terminal resume returned the same outcome; `hello.txt` was not created.
- The raw Opus return is **1630 bytes** and its accepted scribe ingress is
  **1631**; the raw advisor return is **1509 bytes** and accepted ingress is
  **1510**. Each accepted file equals the exact raw return plus one final LF,
  with no other content difference. These are transport mismatches, not
  byte-for-byte preservation. Raw extracts and original accepted files remain
  unchanged. The
  operator also observed an advisor Bash `sed` edit despite its advisory
  Write-only authoring convention. Current role instructions do not mechanically
  enforce write-tool or byte-fidelity compliance. No permission redesign was
  attempted. Reviewer/scribe attribution remains as observed above.
- Reviewing Stop fires 2–6 occurred while the parent awaited a scribe notice.
  The actual `SubagentHandback.input.message` had already delivered the result;
  the later task notification only said it was already delivered. Waiting for
  that redundant notification caused five unnecessary Stop/parent turns. Later Stop
  responses in the completed stream allowed exit, but that is only this case's
  post-terminal evidence.

### Narrow follow-up and regression evidence

Before the fix, new default-mode hook subprocess tests failed for schema-4
journal planning and journal-less legacy drafting: their block payload had no
`Next:` argv. The synthesis-prompt test also failed because APPROVED's qualifier
was unspecified. Existing done-finalization guidance already passed.

`persistent-mode.py` now supplies the pure leaf projection as measure-twice's
terse body as well as its verbose/finalization body. Build's body selection is
unchanged. No bounds, parked/human wait, force-exit order, lock acquisition or
fail-open paths changed. Tests execute the issued public CLI command and check
the same lifetime/action, claimed-work wait, bound reviewer batch, legacy
quiescence question, legacy forced finalization and journal finalization with
no new paid work. They are hook/CLI subprocess regressions, not a substitute
for corrected automatic host re-entry.

The issued loop synthesis prompt and Markdown transport now explicitly say
APPROVED means `minor_only=false` and omit `--minor-only`; that flag is valid
only with REVISE. A public CLI regression retains refusal of the invalid
combination, leaves the claimed workflow unchanged and accepts corrected
APPROVED without rerunning reviewers.

Follow-up checks: the four focused tests passed after the fix; full
`test-measure-twice.py` passed 69 tests, `test-hooks.py` passed 674 checks and
`test-review-workflow.py` passed 197 tests with the interpreter/PATH above.
All four edited Python files parse with Python 3.11 grammar; `git diff --check`
passes. No packaging or global configuration files changed.

At that follow-up the host gate was still pending. The actual reruns and remaining
cases below close those required Claude cases; the original failing observations
remain evidence and are not retroactively marked as passing.

### Completed real CLI cases and source epochs

All actual processes below used Claude Code **2.1.287**, candidate
`crew@inline` **0.83.1**, and the candidate path named above. They used real
native plugin roles/hooks, no `CREW_HOST` override, no `--bare`, no permission
bypass, no global installation/configuration changes and no desktop automation.
The opt-in runner `scripts/tests/claude_cli_gate.py` scrubs inherited harness
identity through the shipped Claude provider's child environment, preserves
authentication/model settings, supplies an empty strict MCP configuration and
records invocation, stream, debug file, actual process exit, and complete
before/after source manifests. PATH includes `/opt/homebrew/bin` and
`~/.local/bin`; `CREW_HOOK_DEBUG=1` records actual payload keys/scalar loop facts.
The runner's CLI `--verbose` emits stream events; `CREW_VERBOSE` is absent, so
the Crew Stop payloads use their real default mode.

An initial sandboxed CLI launch loaded the real hooks but could not access
existing authentication (`Not logged in`). A mistaken resume of that empty
session also failed. Fresh execution with approved access to existing local
authentication succeeded, without login/configuration changes. Those failed
setup files remain, as does one invalid-UUID setup attempt in the partial
fixture. None launched paid workflow roles or passes a workflow gate.

| Case | Actual evidence and result |
|---|---|
| Default next-argv Stop and cancellation | Original fixture `stop-cancel-stream.jsonl`, session `4504bf5e-4f32-4af8-94da-105093b5dc76`, lifetime `23f097c3-ae0f-401e-9e97-c5ceadcae9f3`: real block with exact next argv, automatic execution retaining initial owner/action, typed cancel, terminal replay, final Stop `{}`. Exit 0; source SHA manifests identical; no advisor/reviewer launched. |
| Requirements interview | `/private/tmp/crew-phase4-cli-native-lifecycle/pause-auth-start.stream.jsonl`: pure ordered interview before activation, supplied harmless fixture answers with exact question identity, then activation. `pause-interview-response-1/2.json` retain both responses. |
| Interrupted never-spawned claim and human pause | Same process claimed `action-0001` but deliberately never spawned it, confirmed `not_running`, and parked on the exact `advisor_retry` question. Zero native launches. `pause-claim.json`, `pause-question.json`, `pause-state.json`; actual Stop allowed the human wait. |
| Session re-entry | Actual `--resume` of session `a1789644-7161-4b7c-9179-f4b331a49703`, lifetime `ab0aceea-1406-48e2-acc7-7b817d24303d`: same question, lifetime, `started_at`, caps and no-deadline policy; only normal parked counters advanced. Explicit fixture decision `retry_advisor`; no duplicate advisor from the lost claim. This proves session re-entry, not a compaction event. |
| Real blocking review and revision | `resume-revision.stream.jsonl`: original staged advisor plan retained, then a labelled TEST replaced it with a plan missing overwrite refusal and exact EOF verification. Real external Sol and native Opus both returned REVISE. Real parent synthesis counted two distinct blockers; the engine issued a fresh advisor revision. Revised canonical `plan-3.md` addresses both causes. No review or verdict was fabricated. |
| Real external interruption and partial advisory retry | The second generation overlapped external Sol/native Opus; Opus succeeded by direct capture. Ending the print process killed the still-running owned Sol shell. `sol-round2-interruption.json` records the actual host kill plus owned-process check. Confirmed recovery retained Opus, and the engine bound a quorum advisory. `stable-retry-finalize.stream.jsonl` applied the exact fixture-operator `retry_review` decision, issued only Sol in attempt-0002, ran it in the foreground to actual completion, and reached 2/2 usable reviews and honest APPROVED synthesis. No force override or repeated Opus launch. |
| Partial-panel advisory and explicit cancellation | `/private/tmp/crew-phase4-cli-native-partial/stable-partial.stream.jsonl`, session `d71d18c8-124b-457f-851a-34f7370e3faa`, lifetime `8f22ec38-c5dd-4b38-84f0-dc3beb23bf5d`: actual fresh Opus/Sonnet reviewers launched before any wait, both completed. A labelled TEST ignored the real Sonnet return using a typed failure; raw bytes remain. The resulting 1/2 quorum advisory was answered by the exact explicit fixture-operator cancellation. Terminal replay and final Stop allowed exit; no force or cap reset. |
| Done-before-deactivate recovery | `stable-retry-finalize.stream.jsonl`: after actual guarded APPROVED application, retained `done-fault.py` injected `OSError` at deactivation only, leaving `phase=done`, `active=true`. `DONE_RECOVERY_PROBE_READY` ended the turn with nothing in flight. Real Stop line 236 blocked with exact next argv and matching `stop fires: 1/150` / `fires=1/150`; line 271 automatically executed it. Terminal approved, replay identical, final Stop `{}`. `done-stop-audit.json` independently verifies byte-equal journals, unchanged owner/budget/plan/revision, and zero new native launches during finalization. |

The original default Stop probe's counter display was also defective: its header
said 1 while the leaf projection said 0. The mt-only projection callback now
renders **after** the persisted counter transaction. A default-payload regression
failed before that fix. Actual subsequent Stop payloads, including the finalizing
probe above and the retained partial-fixture fires 17/18, have matching counters.
Build's terse/verbose/finalization selection, bounds, parked/human waits,
force-exit order, lock ownership and failure-open behavior remain unchanged.

Source changes are recorded rather than hidden: `resume-revision` and
`partial-real` began with a blanket alias refusal. The actual public output was
a Claude-owned symlink, so the initial binds correctly refused that unsupported
shape and used retained fallback/failed-run evidence. Supporting the demonstrated
exact alias changed the helper/tests during those processes;
their `process.json` files explicitly say `source_unchanged=false` despite actual
CLI exit 0. They are not immutable whole-candidate runs. `pause-auth-start`,
`stable-finalize`, `stable-partial` and `stable-retry-finalize` all exited 0 with
identical per-process source manifests. No counters, caps or failed artifacts were
reset to retry a case.

The final stable recovery epoch pins `scripts/persistent-mode.py` to
`81b9bd3ae4de48a1e51f66e55d4a4b93d0d8c586f9edfc0a14b7226a055159ee`
and `scripts/multiagent/claude_native_transport.py` to
`bd37562ceccd388463294cc1b99c99fa0e15b36d75c82c71337fe1ffc4cde21e`.
Earlier demonstrated direct captures used helper hash
`68aa766404791aa07840fe3ac68f9ef3ffdf77e6ab54c5f614836d4727084526`;
the final helper additionally preserves accepted replay after the known host
alias/transcript is removed. That cleanup boundary was red/green regression
tested; no live alias was deleted to manufacture a host observation. Complete
source hashes, including the exact command text used in each process, remain in
each `*.source-before.json` / `*.source-after.json`.
After the evidence/status documentation update, `final-candidate-source.json`
records the final tree; all 33 non-test Python source hashes still match the
final stable CLI process. Later documentation edits do not rewrite the actual
process manifests or imply that those new document bytes were loaded earlier.

### Exact native transport audit and residual limits

`multiagent/claude_native_transport.py` is a narrow opt-in boundary for the
demonstrated 2.1.287 public `agentId`/`output_file` surface. Bind follows the
actual launch; capture follows actual hand-back/notification authority. Every
record binds the handle, session, project and CLI version; the first user message
must be the exact issued prompt reference. The helper extracts precisely one
`SubagentHandback.input.message` string as UTF-8 bytes. It accepts the known
public alias only to that exact home-project/session/handle transcript, refuses
foreign aliases/parents, and retains raw bytes before existing guarded capture.
The receipt binds owner/action/prompt bytes/handle/path. File existence, size,
EOF framing, timestamps or a report-shaped record never establish completion.

Independent `native-byte-audit.json` in both new fixtures compares the actual
parent stream hand-back (including its parent tool ID), public transcript,
retained raw bytes, prompt binding, accepted ingress and receipt digest:

| Fixture/action | Exact raw bytes | Final LF | Accepted transport |
|---|---:|---|---|
| Partial advisor `action-0001` | 2868 | absent | Equal ingress and accepted mt digest |
| Partial Opus `attempt-0001:reviewer:0001` | 3076 | absent | Equal ingress and accepted review receipt |
| Partial Sonnet `attempt-0001:reviewer:0002` | 1618 | absent | Equal retained raw/receipt digest; deliberately failed TEST result, no accepted review artifact |
| Lifecycle revised Opus `attempt-0001:reviewer:0002` | 3453 | absent | Equal ingress and accepted receipt; unchanged by Sol-only retry |

These four direct captures invoke **zero scribes** and preserve EOF exactly.
The partial fixture's already-completed advisor was captured after real session
re-entry using its original handle, with no new advisor. Its reviewers caused no
extra Stop turns waiting for redundant notices. Actual notices still appear
after hand-back in the stream; no claim is made that the host suppresses them.

Native shape/owner/prompt/handle/claim, unsupported version, incomplete/ambiguous
JSONL, exact CRLF/Unicode/EOF, missing completion attestation, modified prompt/raw
receipt, known/foreign aliases and public CLI replay have behavioral coverage.
The missing-completion test refuses before any artifact read. Replay after host
cleanup uses accepted retained bytes and guards, rather than another paid task.
The boundary neither launches work nor owns judgments or completion policy.
Requested model pins remain unchanged; workflow native attribution stays
`requested-only`, while host stream models are retained separately.

The unsupported-host scribe/host-Write fallback remains advisory. In the lifecycle
fixture's first generation the real Haiku scribe added one final LF; that
mismatch is retained (3139 raw bytes versus 3140 scribe bytes). The actually
accepted host-Write fallback preserved all 3139 bytes exactly. The two advisor
host-Write reports preserved 2712 and 4060 bytes; `fallback-byte-audit.json`
compares those raw hand-backs to accepted ingress/digests independently.
This does not certify every fallback or mechanically confine writes:
the original advisor's Bash `sed` edit remains an observed convention violation.
No permission redesign, future-version admission, generic capability registry,
unsupported planning host, deferred Cursor work or Phase 5 change was added.

### Observed counts, timing and runnable evidence

`host-event-audit.json` in each new fixture retains deduplicated tool IDs/counts,
role launches, requested pins, actual hook outputs, CLI result usage/model usage,
reported costs and durations. Counts include fixture fault/audit operations;
they are not ordinary-workflow efficiency baselines. Repeated result snapshots
can cover subturns or repeat cumulative costs, so they must not be summed.

| Process | New native roles | Parent tool calls | CLI observations |
|---|---|---:|---|
| pause-auth-start | none | 30 | 31 reported turns; 103224 ms result duration; $0.864703 reported cost |
| resume-revision | 2 advisors, 2 Opus reviewers, 1 Haiku scribe | 62 | Six result snapshots; source changed mid-process; two independent mixed review batches |
| stable-finalize | none | 22 | Actual interrupted-Sol recovery and bound advisory; final result 23 turns / 119078 ms; $6.825809 cumulative reported cost |
| stable-partial | 2 reviewers (Opus/Sonnet); original advisor retained | 47 | Three result snapshots; direct capture/cancel; final result 28 turns / 225492 ms; $6.2159763 cumulative reported cost |
| stable-retry-finalize | none; one external Sol retry | 24 | 26 reported turns; 219506 ms result duration; $8.0789292 cumulative reported cost |

Both lifecycle generations launched the external command before native waiting.
In the first, both native Opus and external Sol were launched before either
completed (stream launch/hand-back/command completion order is retained). The
external result reports 81.035 seconds; the real foreground Sol retry reports
102.947 seconds. The second overlapped until host teardown killed Sol. Absolute
cross-provider overlap duration and a comparable live legacy baseline are
unknown. Tokens/cached tokens and per-model costs remain the CLI's raw reported
fields in the audit; no speedup or savings is inferred. Terminal/finalization
replay emitted no new paid roles, reviewer actions or outcomes.

Audit reproduction reads workflow/host evidence and writes only audit records
(the scripts are retained with the fixtures):

```bash
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-cli-native-lifecycle/audit-native.py /private/tmp/crew-phase4-cli-native-lifecycle
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-cli-native-lifecycle/audit-native.py /private/tmp/crew-phase4-cli-native-partial
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-cli-native-lifecycle/audit-host.py /private/tmp/crew-phase4-cli-native-lifecycle
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-cli-native-lifecycle/audit-host.py /private/tmp/crew-phase4-cli-native-partial
```

New live probes use `scripts/tests/claude_cli_gate.py --fixture <explicit tmp
project> --prompt <retained prompt file> --session-id <UUID> --label <new label>`;
use `--resume` only for an actual existing session. Existing prompt/invocation
files retain exact owner/question/controlled-fault instructions for the observed
phases. Do not replay those old phase-specific decisions against terminal state,
or target the root build. These earlier runs exercised the required **Claude Code** lifecycle surfaces
for their recorded source epochs. The revision below rechecks materially changed
host surfaces; it does not retrospectively replace the earlier source manifests. Compaction itself, general fallback byte
guarantees, baseline performance comparisons and Cursor lifecycle remain
unproved/outside the required session-re-entry route; they are not reported as
passed observations.

Retain this procedure for the real Claude Code CLI/native Agent host, using an
isolated project/session and a loaded candidate containing the reviewed source,
without replacing/mutating the live 0.83.1 loop:

1. Capture the loaded plugin version, exact paths, source revision/diff and
   detected host. Use a harmless plan-only fixture and an explicitly selected
   small panel. Record actual requested pins and actual native/external routes;
   do not change global models or use a host override as host evidence.
2. Exercise requirements input, bound human pause, compaction/session re-entry,
   a real blocking revision, review and completion. Retain actual returned reviews,
   synthesis, promoted plans, LoopState and accepted receipts. Verify same
   lifetime/budgets and no duplicate paid work across re-entry.
3. Deliberately end an active working turn with no human wait or work in flight.
   Observe the installed Stop hook block, inject next argv and automatically
   re-enter the host. Observe the engine resume that same lifetime/action and
   make progress without manual command re-invocation. Save the actual host
   continuation transcript and before/after state. A manually repeated command
   or terminal fake does not pass this step.
4. Separately exercise cancel, confirmed interrupted-claim recovery, partial-panel
   advisory/explicit human decision and done-before-deactivate recovery. Verify
   actual fresh Tasks, issued roles/pins, truthful scribe/fallback transport,
   external/native overlap and intact counters/deadlines.
5. Record observed tool/token counts, role counts, provider intervals, overlap,
   wall time and replay costs for the same baseline/candidate inputs when
   available. Keep unobservable fields unknown. Only this installed-host evidence
   can close Phase 4 and unlock preparing the Phase 5 specification.

## Files edited or added by this implementation

Existing planning edits preserved unchanged: `.claude/CLAUDE.md`,
`multi-harness-engine-roadmap.md`, `operator-followups.md` and
`phase-4-measure-twice-plan.md`.

- Engine: `scripts/loop_state.py`, `scripts/loop_projection.py`,
  `scripts/models.py`, `scripts/crew-state.py`, `scripts/artifact_prune.py`,
  `scripts/persistent-mode.py`, `scripts/session-start.py`,
  `scripts/multiagent/measure_twice.py`, `scripts/multiagent/workflow_transport.py`,
  `scripts/multiagent/claude_native_transport.py`,
  `scripts/multiagent/review_workflow.py`, `scripts/multiagent/prompts.py`,
  `scripts/multiagent/cli.py`.
- Role/commands: `agents/advisor.md`, `commands/measure-twice.md`,
  `commands/cancel-measure-twice.md`, `commands/status.md`.
- Documentation: root `README.md`, plugin `README.md`, `scripts/CLAUDE.md`,
  `docs/engine-notes.md`, `docs/codex-host.md`, `docs/cursor-host.md`,
  `docs/measure-twice-protocol.md`, this evidence record.
- Tests: `scripts/tests/test-measure-twice.py`, `scripts/tests/test-hooks.py`,
  `scripts/tests/claude_cli_gate.py`,
  `scripts/tests/test-multiagent.py`, `scripts/tests/measure_twice_trace.py`,
  `scripts/tests/fixtures/measure_twice_baseline_trace.json`,
  `scripts/tests/fixtures/measure_twice_migrated_trace.json`,
  `scripts/tests/fixtures/schema3_models.py`,
  `scripts/tests/fixtures/schema3_cleanup.py`.

All paths above except the root README are under `plugins/crew/`.


## Correctness revision after run-7b6d624035ef (2026-10-02)

The authoritative full panel and synthesis were read before editing. The source
baseline and branch remain `b39f64783d38642dd93304c818dfe263ae7e9c31` /
`codex/multi-harness-engine`. The final panel identified eight causes, including
the provider-probe lock failure. Seventeen behavioral tests were added (76 -> 93).
The first ten new regressions reproduced the reported defects before the fixes;
`/private/tmp/crew-phase4-revision-red.log` retains those failures. All later
checks use process-local Homebrew Python/PATH and isolated source state.

| Cause | Correction and regression evidence |
|---|---|
| Inactive terminal admission | Identical immutable requests and safety-force-exit replay terminal. Different requests after approval, cancellation or `review_failed` admit a new lifetime. Expected prior state is revalidated under the owner lock; a racing replacement cannot be overwritten. Active lifetimes remain immutable. |
| Scoped/legacy conflicts | Admission independently checks scoped and adoptable legacy build **and** mt files; an inactive scoped file cannot hide active legacy state. Future-schema conflicts refuse without rewriting them. |
| Legacy continuation | Missing `loop` defaults to the valid empty discriminator. Confirmed quiescence plus recovered requirements returns through one retained-plan/REVISE/REJECT continuation function, preserving rounds/budgets and prior-feedback validation. |
| Synthesis decision identity | Question identity includes the actual failed review reference/attempt. A consumed retry cannot authorize a later attempt. `synthesis_retry` accepts only `retry_synthesis`; `completion_advisory` accepts only `retry_review` (plus their documented cancel/force alternatives). |
| Force bounds | An explicit force decision checks deadline/Stop bounds under the owner lock before applying any verdict. Cancellation stays available; an already-recorded done state still only finalizes. |
| Completed invalid advisor output | Capture derives a failed result for absent/unreadable/empty/invalid-UTF-8 staging, settles the claim and exposes bound `advisor_retry`. Exact raw bytes and the observed result are retained; a later staging repair cannot change replay. Direct submit judgment/plan guards remain strict. |
| Provider work under locks | Availability, provider construction and effective-timeout probing occur outside owner/review locks. Preparation is computed outside and checkpointed only after current owner/plan/generation revalidation. Claiming similarly reloads owner/action/bounds after probing. Blocking fake probes prove the real default Stop subprocess and cancellation can acquire the owner lock; no provider run occurs after cancellation. These are unit tests, not installed-host evidence. |

Local minors were also corrected: actual full panel path in revision prompts,
no-follow latest discovery of flat legacy or canonical UUID-lifetime `plan-N.md`
files (never nested staging/sealed/raw files), typed missing-state JSON errors,
sanitized journal session projection, truthful pending `LoopReviewSource`
binding (no accepted-outcome digest until completion), quoted YAML hint,
PATH-based Claude discovery/explicit executable override, REJECT diagnostic,
build diagnostic hints, and current command/user/developer documentation.
Pending evidence cannot apply a verdict: accepted-outcome validation still
requires a full SHA-256. Successful reviewers survive retries unchanged.
Build production recipe, routes, continuation policy and pointers are unchanged.

### Exact checks

Logs: `/private/tmp/crew-phase4-revision-{measure,review,hooks,multiagent}.log`.
The commands in the Checks section above reproduce these suites.

| Check | Current result |
|---|---:|
| test-measure-twice.py | 93 pass |
| test-review-workflow.py | 197 pass |
| test-hooks.py | 674 pass |
| test-multiagent.py, existing `crew_config()` isolation | 2026 pass |
| Python 3.11 grammar parse | 20 changed/new modules pass |
| git diff --check | pass |
| Protected planning/guidance SHA checks | all four unchanged |
| Staged changes / HEAD / branch | none / unchanged / unchanged |

Actual interpreter: `/opt/homebrew/bin/python3`, Python 3.14.5. Suite subprocess
PATH: `/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`. No dependency, newer
Python syntax, global configuration or provider/model selection change was made.

### New real CLI epoch and targeted host rechecks

All three successful processes loaded the actual candidate at
`/Volumes/Dock/Dev/GitHub/luinstra/claude_crew/plugins/crew`, manifest version
0.83.1, in Claude Code CLI **2.1.287**. Stream initialization records the existing
parent model `claude-opus-5-5`; the advisor requested `inherit` and omitted the
model parameter. Authentication/model settings were preserved. No `CREW_HOST`
override, `--bare`, disabled hooks, desktop automation or external native-role
substitute was used. Per-process before/after manifests are identical. Their
canonical manifest SHA-256 is
`f2394fb1396d884b742668fa386d5dff158d3a26bc73714f62aa01e96f3b0267`.
Subsequent documentation and synthesis rejection-diagnostic edits do not claim
that this is a hash of the entire final dirty tree. The diagnostic now lists
REJECT only for loop reviews; standalone guidance retains its actual allowed
verdict set. Public submission regressions cover both refusal messages without
changing either validator. These edits do not change the host paths exercised
in the three successful targeted processes. Earlier fixtures/manifests and failure observations are intact.

- **Completed advisor failure, replay and fresh lifetime:** fixture
  `/private/tmp/crew-phase4-revision-cli`, session
  `37e773bf-96bc-429b-8ef4-388a9819bff6`, process `advisor-failure`.
  One actual fresh `crew:advisor` launched and completed via native
  `SubagentHandback`; handle `ad2eae0f32174c6bd`, parent tool
  `toolu_01TxFcNFNP3LUvqAZHMX3sPe`. After actual completion, the operator-authorized
  TEST removed only its staged plan (retained exact copy and `injection.json`).
  This was controlled invalid output, **not a provider failure**. Direct capture
  settled `action-0001` failed and issued `advisor_retry`; replay after repair
  kept the exact same question/receipt. Bound cancellation and identical request
  replay kept lifetime `81d1c612-2b3c-40d4-a052-3872645b903d`. A different document
  admitted lifetime `9faa675e-4dee-4c74-9ed3-569f2e2752f4` with fires=0 and a ready
  advisor; it was immediately cancelled without claim/spawn. Final next replay
  launched no paid work and actual final Stop payloads were `{}`. Exit 0.
  Raw native result **1444 bytes, no final LF**, retained native artifact and
  accepted ingress are exactly equal. Digest:
  `cec626c3e8f354051992a9c234b82e2651cae1d8f4a3aaafaf94c6c5df544d04`.
  The accepted mt result equals the durable transport receipt’s derived failed
  result; the original capture status was `ok`, preserving the observed failure.
- **Legacy missing discriminator, human pause and real re-entry:** fixture
  `/private/tmp/crew-phase4-revision-legacy-cli-2`, session
  `5abda7ee-7e50-4c6f-ab49-4366ff832523`, lifetime
  `e303e525-3e78-420d-83b4-91b9edc621d9`, processes `legacy-human-pause` and actual
  `--resume` `legacy-reentry`. The explicitly retained controlled schema-3 seed
  omitted `loop` and had a reviewing plan but absent requirements. No task had
  ever spawned, so `not_running` was truthful. Real Stop allowed the exact bound
  requirements wait; `.crew/hook-payload-keys.txt` records `crew.emit=allow`,
  `crew.parked_fires=1`, and `crew.stop_fires=7` after the actual hook. Operator-supplied harmless requirements and the bound
  `retry_requirements` adopted the retained plan into canonical `plan-1.md`,
  returning a ready reviewer, never an initial-planning launch. Real re-entry
  preserved the owner/question, stop_fires=7 and revision_round=2. Plan, sealed
  copy and frozen review target bytes match. The ready review was cancelled;
  final replay/Stop exited normally. Both processes exit 0; **zero native roles
  or external reviewers launched**. Legacy adoption’s internal settled
  advisor-shaped promotion record is not counted as an observed advisor launch.

The initial `/private/tmp/crew-phase4-revision-legacy-cli` remains intact. Its
controlled seed used an old timestamp and invalid no-deadline marker (`-1`
instead of the required `0` plus true flag). The engine correctly enforced the
240-minute fallback and force-exited it. It was never reset or reused. A separate
fresh fixture corrected the seed; this is test setup evidence, not a provider or
source regression.

`host-event-audit.json` retains actual roles, unique tool IDs, hooks, pins,
result token/model-usage/cost/timing fields and extra launch keys. Observations:
advisor-failure: 1 advisor, 59 parent tool calls, 60 reported turns, 295612 ms,
$2.2738216 reported cost; legacy pause: 0 roles, 10 parent tool calls, 11 turns,
35175 ms, $0.4119388; real re-entry: 0 roles, 30 parent calls, 31 turns, 132461 ms,
$1.2446164 cumulative reported cost. Do not sum cumulative session snapshots or
infer baseline savings/overlap from these targeted cases.
The advisor Agent used `run_in_background` despite the Markdown bare-Task
convention, and its native tools included Bash inspection. This fixture validates
owned completion/capture and recovery, not compliance with every authoring or
launch-parameter convention. The original advisor’s Bash `sed` write and scribe
added-LF observations remain recorded; no mechanical confinement is claimed.

Runnable audits (read evidence, write only audit records):

```bash
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-revision-cli/audit-host.py /private/tmp/crew-phase4-revision-cli
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-revision-cli/audit-revision.py
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-revision-legacy-cli-2/audit-host.py /private/tmp/crew-phase4-revision-legacy-cli-2
rtk proxy /opt/homebrew/bin/python3 /private/tmp/crew-phase4-revision-legacy-cli-2/audit-revision.py
```

The portable opt-in runner now discovers `claude` on process PATH or accepts
`--claude /path/to/claude`; it preserves supplied PATH and writes exact invocation,
prompt path, plugin hashes, debug/hook stream and process result. New fixtures use
explicit disposable projects, empty strict MCP configuration and shipped-provider
identity scrubbing. The live root build remains active schema 3, executor Sol;
no upgraded source API targeted it.

### Scope decisions and remaining review

The host-neutral injected runtime/evidence interfaces remain production source:
the approved plan explicitly requires notification-driven transport seams and
public loop-review APIs, so “only tests currently inject them” is not grounds to
remove them. Broad publication/redesign of private under-owner-lock accessors is
deferred per this targeted revision’s scope. Direct capture remains exact-version
2.1.287 admission; other versions keep the explicit scribe/host-Write fallback
until validated. No availability/capability registry or provider was added.

`.claude/CLAUDE.md` and the phase-4 plan, roadmap and operator-followups remain
byte-identical to the task-start hashes. Their stale state/test/status wording is
intentionally deferred because these are protected review inputs. Current docs
and this evidence carry the corrected status. The next configured panel must
review this revision. Earlier CLI lifecycle evidence remains valid for its
observations; this targeted run does not re-certify every lifecycle scenario,
compaction, arbitrary fallback fidelity, launch-convention compliance or another
host/version. No Phase 5 work was started.

### Files touched in this correctness revision

- Engine: `scripts/loop_state.py`, `scripts/loop_projection.py`,
  `scripts/multiagent/measure_twice.py`, `scripts/multiagent/review_workflow.py`,
  `scripts/multiagent/workflow_transport.py`.
- Compatibility/diagnostics: `scripts/crew-state.py`, `scripts/persistent-mode.py`
  (the latter’s change in this revision is its writer docstring).
- Tests/runner: `scripts/tests/test-measure-twice.py`,
  `scripts/tests/claude_cli_gate.py`.
- Commands: `commands/measure-twice.md`, `commands/cancel-measure-twice.md`,
  `commands/review.md`.
- Documentation: root `README.md`, plugin `docs/CLAUDE.md`,
  `docs/codex-host.md`, `docs/engine-notes.md`, `scripts/CLAUDE.md`,
  `docs/measure-twice-protocol.md`, `docs/phase-4-measure-twice-evidence.md`.

Paths without a root prefix above are relative to `plugins/crew/`. Existing local
planning and implementation edits are preserved; every edit remains unstaged
and uncommitted for human review.

## Narrow blocking revision after run-e1f5ea40ff69 (2026-10-02)

Read `panel.md`, `panel-full.md` and `synthesis.md`; corrected only the two
blocking protocol boundaries. Earlier source epochs and installed-Claude
lifecycle/transport observations above remain historical evidence for their
exercised paths. No paid lifecycle probe or new panel was run this round.

- Start admission and public CLI start/resume use the shared review session
  validator before state resolution, storage guards or initialization. Empty
  IDs return `missing_session_id`; fully sanitized invalid IDs and placeholders
  return `invalid_session_id`. API/CLI regressions compare every fixture file
  and directory, retain consumed-request spills on refusal, preserve existing
  sessionless legacy state and prove valid IDs/normal sanitization still work.
- The shared verdict/evidence guard discloses and compares current target
  spec/base, plan path, resolved fingerprint or resolution failure, and all
  completion advisories under the existing owner lock before forced recording.
  A mismatch parks a replacement question without applying the outcome or
  launching review work. Replacement identity includes the prior question ID,
  preventing old-force replay even after target reversion. Regressions cover
  quorum-only to drift, changed already-drifted bytes with unchanged advisory
  names, missing/restored targets, stale public CLI replay and unchanged explicit
  force. Existing bound refusal, cancellation, owner/replay and paid-work
  preservation regressions pass. No new journal fields or verdict policy.

Eight focused API/public CLI regressions were added (93 -> 101). After code
settled, the four required suites ran once with the interpreter/PATH and
`crew_config()` isolation documented above:

| Check | Result |
|---|---:|
| test-measure-twice.py | 101 pass |
| test-review-workflow.py | 197 pass |
| test-hooks.py | 674 pass |
| test-multiagent.py, existing `crew_config()` isolation | 2026 pass |
| Python 3.11 grammar parse of four modified modules | pass |
| git diff --check | pass |
| Four protected inputs and root loop-state hashes | unchanged |
| Staging / HEAD / branch | none / b39f64783d38642dd93304c818dfe263ae7e9c31 / codex/multi-harness-engine |

Logs: `/private/tmp/crew-round2-{measure-twice,review-workflow,hooks,multiagent}.log`.
Exact tested source SHA-256s: `/private/tmp/crew-round2-source.json` (the three
modified engine modules and test file). This round touched only `loop_state.py`,
`multiagent/measure_twice.py`, `multiagent/cli.py`, `tests/test-measure-twice.py`
and this evidence record, relative to the existing dirty tree. All source paths
in this list are under `plugins/crew/scripts/`. No provider/model/config changes,
live root state changes, protected-input edits or nonblocking follow-up fixes
were made. The orchestrator's final configured panel remains pending.

## Focused structural revision after run-17cbb1de2fa0 (2026-10-02)

Read the full panel, digest and synthesis. Three blocking findings were fixed
as two causes; other minor findings remain outside this correction.

- Retry authority is an optional typed `retry_authorization` in the existing
  loop journal, containing the exact MeasureDecision and source ReviewRef.
  Shared loop retry checks it against the question/owner/attempt and existing
  retry receipt; standalone retry policy is unchanged. Ordinary next/resume
  commits or replays the authorized retry and reconciles the owner's attempt
  before returning a question. New native/parent claims and external execution
  reject human waits; external admission is checked again after provider probes
  outside locks. Settlement/evidence remain available through the common owner
  guard. Loop-before-review lock order, cancellation/replacement and expired
  bounds remain enforced. Preparation checkpoints explicitly refuse inactive
  owners, preserving pending inputs/generation after cancellation.
- Successful advisor capture validates and seals the observed staging bytes
  inside its owner transaction before writing the immutable transport receipt.
  Submit/replay validate and use those retained bytes and the captured result;
  direct submit without a receipt still seals the issued staging plan. Ingress
  conflicts are checked before writes. Failed-plan capture, exact returned bytes,
  native/fallback capture and once-only promotion remain intact.

Twelve behavioral/public CLI regressions were added (101 -> 113). They cover
unauthorized retries including synthesis-only, paused claims/execution and
provider-probe interleavings, crashes before/after retry commit, next/resume and
decision replay, wrong question/attempt/owner/selection, cancellation/replacement
and bounds, staging overwrite/removal after capture, capture owner-write failure,
retained-byte/result conflicts, ingress preservation and direct submit.

The four required suites ran once after code settled, using the interpreter/PATH
and existing `crew_config()` isolation documented above:

| Check | Result |
|---|---:|
| test-measure-twice.py | 113 pass |
| test-review-workflow.py | 197 pass |
| test-hooks.py | 674 pass |
| test-multiagent.py, existing `crew_config()` isolation | 2026 pass |
| Python 3.11 grammar parse of four modified modules | pass |
| git diff --check | pass |
| Protected inputs / live root state / staging / HEAD / branch | unchanged / unchanged / none / unchanged / unchanged |

Logs: `/private/tmp/crew-round3-{measure-twice,review-workflow,hooks,multiagent}.log`.
Exact tested source epoch: `/private/tmp/crew-round3-source.json`. Earlier source
epochs and real Claude lifecycle/byte observations above remain historical
evidence for their exercised paths; no paid host probe or panel was rerun.
Older journals load missing retry authorization as absent. A successful capture
receipt without retained sealed bytes is refused, never reconstructed from
mutable staging. This round touched only `scripts/multiagent/measure_twice.py`,
`scripts/multiagent/review_workflow.py`, `scripts/multiagent/workflow_transport.py`,
`scripts/tests/test-measure-twice.py` and this evidence record, under
`plugins/crew/`. Provider/model/config and four protected inputs remain unchanged;
all edits are unstaged/uncommitted on `codex/multi-harness-engine` at
`b39f64783d38642dd93304c818dfe263ae7e9c31`. The next configured panel belongs to
the orchestrator and remains pending.

## Focused boundary revision after run-f931bd3c76f3 (2026-10-02)

Read the plan, panel, full panel and synthesis. Fixed the three confirmed
boundaries and the two requested adjacent recovery cases:

- Admission checks raw sticky safety-exit markers on every adoptable same-session
  mt candidate, including legacy state hidden by an inactive scoped file. Other
  sessions stay isolated; compatibility init with explicit human force remains
  the restart authority. SessionStart and Stop project a known current session
  for unowned legacy state without assigning ownership in the hook. A shared
  lightweight session predicate suppresses invalid executable guidance; owned
  identities and build guidance are preserved.
- Review capture validates the typed result and exact observed bytes with the
  existing submission guard before writes. The first receipt, ingress and
  envelope cannot be replaced by a changed capture while still claimed. Receipts
  bind path as well as returned digest/status/judgment/diagnostic; direct submit
  also refuses replacement of captured evidence. Advisor capture has the same
  path/envelope conflict checks and rejects malformed diagnostics before derived
  plan-failure handling. Historical receipt formats replay without rewriting
  them; direct submission without a capture receipt remains supported. Partial
  batch admission/launch/completion errors cancel owned outstanding runtime
  handles when supported, with explicit unavailable/refused/error diagnostics.
- Only an observed BLOCKING_CAUSES count compares or updates the baseline;
  omission preserves the prior observation and explicit zero resets it.
  Unreadable/invalid-encoding feedback parks a typed feedback_recovery question
  after the outcome/application receipt commits. Ordinary next/resume can retry
  access to the original feedback, without reapplying verdicts or duplicating
  paid work. Cancellation and bounds remain ahead of recovery.

Fourteen deterministic API/public CLI regressions were added (113 -> 127),
covering hidden force markers and explicit restart, both hooks' emitted commands
executed through the real parser, pre-submit crashes and immutable conflicting
or identical capture replay, raw failed returns, judgment/path/envelope conflicts,
direct-submit fencing, standalone/historical receipt compatibility,
5 -> omitted -> 8 and explicit zero, feedback recovery, and partial-launch
cancellation/error diagnostics. No new paid host scenario or nested panel ran.

Checks used /opt/homebrew/bin/python3 with process-local
PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin and existing crew_config()
isolation for catalog-default multiagent checks:

| Check | Result |
|---|---:|
| test-measure-twice.py | 127 pass |
| test-review-workflow.py | 197 pass |
| test-hooks.py | 674 pass |
| test-multiagent.py, existing crew_config() isolation | 2026 pass |
| Python 3.11 grammar, all 43 source/test Python files | pass |
| git diff --check | pass |
| Protected inputs / live root state / staging / HEAD / branch | unchanged / unchanged / none / unchanged / unchanged |

The four suites ran once at /private/tmp/crew-round4-source.json; logs are
/private/tmp/crew-round4-{measure-twice,review-workflow,hooks,multiagent}.log.
A subsequent advisor diagnostic-type check and its regression were the only
source edits after that epoch. The affected measure-twice suite ran again at
/private/tmp/crew-round4-source-final.json, with log
/private/tmp/crew-round4-measure-twice-final.log; the other three suites were
not rerun for this advisor-only edit. Grammar and diff checks cover the final epoch. Earlier
real Claude lifecycle/byte observations and source epochs remain historical
for their exercised paths; they were not rerun or promoted to evidence for
these new crash boundaries. The orchestrator's next configured panel is pending.

This round touched only scripts/loop_state.py, scripts/loop_projection.py,
scripts/session-start.py, scripts/persistent-mode.py,
scripts/multiagent/review_runs.py, scripts/multiagent/review_workflow.py,
scripts/multiagent/measure_twice.py, scripts/multiagent/workflow_transport.py,
scripts/tests/test-measure-twice.py and this evidence file, under plugins/crew/.
Every change remains unstaged/uncommitted on codex/multi-harness-engine at
b39f64783d38642dd93304c818dfe263ae7e9c31; provider/model/config and the four
protected inputs are unchanged.

## Narrow shared-boundary revision after run-252f86e6324b (2026-10-02)

Read the authoritative plan, panel, full panel and synthesis. Inspected
pre-extraction HEAD crew-state.py::_usable_seats/cmd_record_verdict and existing
compatibility tests before changing recovery policy. The old reader did not read
run.json; failed/zero-result recovery therefore did not depend on its readability.

- Legacy evidence now counts flat seat files only after a manifest reads and
  verifies. Missing, unreadable, undecodable or non-object records yield null
  manifest identity and zero usable seats, even with surviving valid seat stamps.
  Existing FAILED progression and zero-usable/completion advisories apply.
  Verification remains outside the read-error fallback: readable missing identity
  fields, mismatched run IDs and tampered identity/target digests remain hard,
  unforceable refusals. Normal verified evidence retains its authority checks.
- Latest-plan scanning closes each lifetime directory in finally, retaining only
  path/file/parent metadata. Selection preserves mtime/path ordering and existing
  flat/canonical filters and text decoding. The chosen lifetime parent is reopened
  with no-follow and compared against retained device/inode metadata and its
  current directory entry before the chosen regular file's inode check/read.
  History is retained; descriptor use no longer grows with lifetime count.

Eight focused regressions were added: public compatibility verdicts for both
build and legacy mt, missing/corrupt/unreadable manifests with surviving seats,
FAILED progression, completion advisories, verified success and unforceable
identity tampering; public empty/latest review with 160 lifetimes under a
32-descriptor limit; bounded directory handles and scan-error cleanup;
directory/file replacement and symlink interleavings; ties/filtering/text semantics.

After source settled, all four required suites ran once with
/opt/homebrew/bin/python3 and process-local
PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:

| Check | Result |
|---|---:|
| test-measure-twice.py | 131 pass |
| test-review-workflow.py | 201 pass |
| test-hooks.py | 674 pass |
| test-multiagent.py under existing crew_config() isolation | 2026 pass |
| Python 3.11 grammar, all 43 source/test Python files | pass |
| git diff --check | pass |
| Protected inputs / live root state / staging / HEAD / branch | unchanged / unchanged / none / unchanged / unchanged |

Logs: /private/tmp/crew-round5-{measure-twice,review-workflow,hooks,multiagent}.log.
Exact tested source epoch: /private/tmp/crew-round5-source.json. Previous real
Claude lifecycle/transport evidence remains historical for its exercised source
and paths; no paid host probe or nested panel was run. The next configured panel
remains the orchestrator's responsibility. This round touched only
scripts/loop_state.py, scripts/multiagent/review_workflow.py,
scripts/tests/test-measure-twice.py, scripts/tests/test-review-workflow.py and this
record under plugins/crew/. Provider/model/config remain unchanged. All edits
remain unstaged/uncommitted on codex/multi-harness-engine at
b39f64783d38642dd93304c818dfe263ae7e9c31.
