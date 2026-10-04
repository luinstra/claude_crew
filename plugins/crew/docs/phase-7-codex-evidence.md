# Codex native gate evidence

Observed 2026-10-03 in the current Codex collaboration surface, parent session
`01a0f9e3-b687-7e63-b9e5-d1e8a07774b5`. Source: isolated worktree based on
`6b16ba30471407c691f944af2316c869227ae061`, with this adapter unstaged.
No `CREW_HOST` override was present. Candidate commands ran only in the disposable
project `/private/tmp/crew-codex-phase7.c1PhWx`.

Current status: source follow-ups and installed discovery/lifecycle checks are
complete. The current native suite has 43 tests; smaller counts below record
earlier revision checkpoints. The initial seven-seat review closed minor-only;
the requested follow-up fixes and gate results are recorded at the end.

## Results

| Case | Observed result |
|---|---|
| Standalone review | Native Sol and Luna; 2/2 usable, quorum met, terminal APPROVED |
| Debate | Native Luna panelist; 1/1 usable, null judgment, terminal advisory record |
| Planning | Native advisor wrote the issued staging path; engine sealed/promoted plan; native Sol/Luna review completed with REVISE minor-only and no override |
| Build | Native executor wrote the exact 18-byte greeting; native Sol/Luna review 2/2; terminal approved, clean HEAD/index/branch guards |
| Revision | Controlled wrong greeting inserted by the gate operator after the first executor returned; native Luna found a real BLOCKING defect; REVISE issued a fresh executor action and handle; repair and fresh review led to terminal approved |
| Explicit resume | Planning, build, and revised build replayed terminal state without paid work |
| Capture replay | Correct-handle duplicate replayed terminal; wrong handle refused with invalid_native_binding and exit 2 |
| Cancellation | Engine cancelled first; interrupt returned previous_status=running; writer fence remained and replacement refused with outstanding_writer, exit 2 |
| Skill structure | All four source skills passed skill-creator quick_validate.py |

Sol requested `gpt-6.1-sol` with low reasoning; Luna requested `gpt-6-luna` with
medium reasoning. Advisors and built-in native executors requested inherited
model/effort. All native launches used `fork_turns=none`. These are requested
pins, not observed runtime-model attribution. No scribe or provider fallback ran.

The revision's second reviewer misattributed the fixture's existing `.gitignore`
as a newly added deliverable. The parent verified that the operator created it
at 15:51:09 UTC, before the revision lifetime at 17:10:02 UTC; its only content
remained `.crew/`. The unborn repository snapshot contains preexisting untracked
scaffolding. The synthesis retained the finding and explained its rejection;
it did not waive a completion guard. The greeting's actual defect was repaired.

The cancelled fixture deliberately retains its fence: an interrupt acknowledgement
is not a final reply. No quiescence confirmation was fabricated. Its separate
session cannot block the worktree build or the other disposable gate sessions.

## Identities and retained artifacts

Local evidence bundle: `.crew/codex-native-gate-20261003/`. It retains issued
items and argv, claims, bindings, host-written replies, captures, frozen review
snapshots, panels, synthesis, terminal/resume results, and a SHA-256 manifest.
Original absolute paths remain recorded as provenance; the bundle is inspection
evidence, not a relocated live workflow. Multiagent Python source digest:

`b2619503e9f67ef28bd2e3972c859cca3443f52615f10c946c196166b18888bb`

| Workflow | Identity |
|---|---|
| Review | codex-native-review-20261003 / run-4b5e67dfde51 |
| Planning | codex-native-plan-20261003 / 40009a92-0b49-4484-94e1-8d995ef9216e / run-23dc2c8229af |
| Build | codex-native-build-20261003 / 6bc28a70-587c-4af5-bac7-da4ef159cd6b / run-b7c70311e812 |
| Debate | codex-native-debate-20261003 / run-80445ba94c58 |
| Revision | codex-native-revision-20261003 / 50fb47e9-956c-483c-b478-ae49f6898baf / run-94ab9c354853 then run-1a0c36d1db88 |
| Cancellation | codex-native-cancel-20261003 / 22ed0a6a-d2c7-4440-8d7c-fa2657c74c9d |

Actual handles and action identities are retained in each `*-bind.json` under
`artifacts/gate-observations/`. The revision changed from
`/root/crew_3fa1eba9c4e787209e85899a` (action-0001) to
`/root/crew_92cb17284aa00c3a4d8fb6ed` (action-0002), both fresh.
Cancellation bound `/root/crew_8225722cb702457b87926dc4`.

## Evidence limits

Returns were host-written from actual final replies, with immutable capture
receipts. This does not demonstrate automatic extraction or byte-perfect host
transport. One advisor role quotation accidentally changed a generic rationale
sentence and was immediately corrected by a message; one executor quotation had
literal newline escapes before a generic heading. Paths, roles, fresh contexts,
and requested model/effort were unchanged. These manual transport deviations
are recorded in `host-transport-note.txt`; no exact spawn-message fidelity is claimed.

No plugin was installed or refreshed by this task. Installed discovery and new
automatic hook re-entry remain unverified to avoid affecting active sessions.
Python 3.11 grammar passed; runtime tests used 3.14.3. Unsupported-model refusal,
late callbacks, and additional recovery cases have synthetic coverage in the
36-test native suite (including review and packaging regressions). The [implementation handoff](phase-7-codex-plan.md) records
all six passing suites and the preexisting Ruff diagnostics.


## Implementation review follow-up

The first implementation panel (`run-d45eaa019c5d`) retained two completed
reports: Astra requested revision and Luna approved with a documentation minor.
No review process remained after the interrupted launches. The parent recorded
REVISE through the state CLI, preserving both reports and the frozen target.

The confirmed blockers were fixed locally: unsupported Codex native reasoning
effort is rejected during seat resolution before review inputs are checkpointed,
and the documentation check now distinguishes passed live gates from pending
installed discovery and automatic hook re-entry. The READMEs, scripts guide,
engine notes and planning protocol were synchronized. New regression cases
exercise configuration correction and successful completion of both loop types.

Follow-up verification: 28 native tests, 201 review workflow tests, 674 hook
checks, and all seven targeted documentation checks passed. Changed Python files
parse with Python 3.11 grammar. The native test file passes Ruff 0.16.10.
The earlier live gate digest identifies the pre-review source; these focused
fixes have synthetic coverage and do not constitute another live gate run.

Final panel approval remains outstanding. Automatic approval review denied
external seat launches on private source-sharing grounds, including the Sol
retry despite the standing Crew authorization. No provider substitution or
approval override was used. A new frozen review is required for the revised
source; the old target and its landed reports remain retained.


## Packaging revision after the full panel

After the user's explicit sharing approval, all seven seats completed
`run-189e1ce90c75`: six approved, while Fable requested isolation of the new Codex
skills from Claude's same-named commands. The parent recorded REVISE and moved
the adapters to `skills-codex/`, explicitly declared by `.codex-plugin/plugin.json`.
The manifest retains existing hooks and the cancel/context helpers. Claude and
Cursor manifests keep their existing component paths. No installed plugin or
global configuration was changed.

The source test now verifies host-specific skill discovery. All eight skills
pass structural validation. The native suite passes 29 tests and the version
suite passes 56 checks, including Codex version mirroring, version-only changes,
untracked manifest preservation and rollback. The version hook synchronizes
all three Crew manifests. Stale build, planning and Cursor-host descriptions
and the native test total were corrected. Unset native effort is explicitly
documented as the host default, without claiming external CLI depth parity.

The host-specific manifest uses the documented Codex compatibility format:
[OpenAI manifest fields](https://developers.openai.com/plugins/deploy/submission#manifest-fields).
The collision concern is consistent with
[Claude skill precedence](https://code.claude.com/docs/en/skills#resolve-skills-that-share-a-name).
Installed discovery and new automatic re-entry remain separate, unverified gates.
The revised package is awaiting final panel review.


## Exact review-model validation

The packaging revision panel (`run-0eaeffc48fb8`) completed with six approvals.
Luna reproduced one remaining edge case: a review seat could freeze the literal
model `inherit` while the native launcher omitted the model argument. The parent
reproduced that mismatch and recorded REVISE. Native reviewer/panelist model
inheritance is now rejected before either standalone or loop review inputs are
frozen; launch metadata also refuses an inherited review model. Advisor and
executor inheritance remains intentional. Three new tests cover review/debate,
both loop preparation paths and defensive launch validation; all 32 native tests
pass. The final source is awaiting panel review.


## Native scheduling and capture transaction

Panel `run-c884e5cd4d53` completed with five approvals and two revision requests.
Sol identified all-at-once native fan-out exceeding the host's child-agent limit.
The transport now requires explicit available capacity for multi-item Codex
batches and launches bounded waves, leaving deferred seats unclaimed. The Codex
host instructions apply the same scheduling rule to standalone review/debate.
Six-seat panels complete in two three-slot waves for both build and planning;
an incomplete wave retains its owned handles and does not claim later seats.

Grok identified a lock gap between launch-refusal validation and build result
settlement. Native capture now validates its binding and retains/accepts the
completion in one owner transaction. A deterministic interleaving test binds a
writer during workspace observation and confirms the refusal cannot clear its
fence. The native suite passes 36 tests including both fixes. Final review is
pending; these additions do not change the retained historical live-gate digest.


## Final implementation review and disposition

All seven configured CLI seats completed `run-61b6e0604878` against target
`b1576a05c66167ce5c8225c9c965f8f621f579ffe66f5076798e470aba9641b0`.
Astra, Sol, Luna, Composer, Opus and Fable approved. Opus's first incomplete
progress-only reply was preserved as `opus-incomplete-original.json`; only that
seat retried the same frozen prompt. Grok requested revision of the review-local
`commands.next` exposed on loop-owned review items.

Parent disposition: retain that finding as a minor interface inconsistency.
The supported adapters explicitly require the outer build/planning owner-next
command (`commands/build.md`, `commands/measure-twice.md`, and
`docs/codex-transport.md`), and the retained live build/planning gates completed
through those routes. The review-local next refuses loop advancement, so this
misleading nested command fails closed rather than changing workflow ownership.
Removing it and correcting its owner-specific error remain cleanup work; this
record does not claim the code was changed. The parent recorded `REVISE
--minor-only` against the unchanged frozen target, passed the normal completion
guards without `--force`, and deactivated the build loop.

Other minor suggestions remain in the full panel: default native effort parity,
manual unsupported capture diagnostics, redundant command/import assignments,
frozen-host bind dispatch, prompt-hash diagnostics, and caller handling of
already-owned handles or a full host. Capacity arguments represent currently
available slots; callers must wait when no slot is available, including for a
single native action. These do not expand this source-completion increment.

Final verification: 36 native tests, 201 review tests, 131 planning tests,
89 build tests and 674 hook checks passed after the capacity/race fixes.
Earlier routing and packaging verification passed 1,990 and 56 checks.
All 14 changed Python files parse with Python 3.11 grammar; runtime tests used
3.14.3. The two new Python files pass Ruff 0.16.10. Final edits after the verdict
only correct documentation and record completion; no implementation changed.
The adapter remains uncommitted and uninstalled in the isolated worktree.
OpenHands is the next planned implementation phase.


## Follow-up closure and installed gate

The operator requested completion of the remaining follow-ups before commit and
push. Loop-owned review items now omit the misleading review-local next command,
and rejection messages name their build or planning owner. Native bind dispatch
reads frozen owners; manual Codex artifact-capture verbs fail explicitly. Capture
flags cannot silently pass on another channel. Prompt integrity is checked
against the launch binding, and advisor promotion permits binding replay only.

Every Codex-native batch requires explicit capacity, including one-item batches;
a partially completed batch cannot launch more work while its handles remain
owned. Native reviewer defaults now freeze the same `xhigh` effort as the external
Codex provider. Explicit unsupported efforts remain an intentional pre-freeze
error: this collaboration API does not accept external-only values such as
`minimal`. Duplicate assignments/imports, stale host diagnostics and guide entries
were cleaned up. Seven additional native regression cases cover these changes;
43 native tests pass, alongside 201 review, 131 planning and 89 build tests.

A separate package, `crew@crew-followup-gate-20261003`, was installed through
Codex app-server 0.159.2 from the candidate source. `plugin/read` reported all eight
skills and both hooks; `skills/list` loaded the installed entries. Hook command
expansion resolved `${CLAUDE_PLUGIN_ROOT}` to that test package's installed cache.
Its hooks used their exact reviewed hashes for process-local trust, with other
hooks disabled only in that test process. No trust bypass was used. The normal
`crew@claude-crew` installation and its settings were unchanged. The test package
was disabled globally, enabled only for the test, then uninstalled.

The live gate used model `gpt-6-luna`, low reasoning, workspace-write sandbox,
and an ephemeral thread `01a103ac-3e63-7bf3-8a62-8be2fb613c2d` in a disposable
Git project. SessionStart restored engine-owned build lifetime
`14f8d73e-eda8-44cc-88d9-4801eaf01e32`. The model first returned `GATE_FIRST`.
Stop blocked that reply, incremented the stop counter, and injected the correct
build-next instruction. Without another user turn, the model re-entered and
executed the gate's preauthorized cancellation of the unclaimed build, returned
`GATE_REENTERED`, and the second Stop completed without feedback. The final turn
completed successfully. This proves installed discovery and automatic lifecycle
re-entry; it does not claim a second installed end-to-end native panel run.

Evidence is retained at `.crew/codex-installed-gate-20261003/`, including install,
skill/hook discovery, resolved commands, trust state, thread/build identities,
full event sequence and a SHA-256 manifest. The installed snapshot preceded the
final shared-runtime argument correction; hook and manifest behavior was unchanged.
Final source regression suites verify that correction separately.

The installation follows the documented [local marketplace format](https://developers.openai.com/plugins/build/plugins)
and [exact-definition hook trust](https://learn.chatgpt.com/docs/hooks).


Final commit verification: 43 native, 201 review, 131 planning, 89 build,
674 hook and 56 version checks passed. The full routing run passed 1,989 checks
and found one obsolete assertion requiring installed discovery to remain pending.
That assertion was updated to the observed gate status; all seven documentation
contract checks then passed. No runtime routing failure remained. Both new
Python files pass Ruff; all 14 changed Python files parse with Python 3.11 grammar,
and `git diff --check` passes. The deliverable branch is `codex/native-integration`;
the primary checkout remains on its existing integration branch.

## Reloaded release smoke, 2026-10-03

At the operator's request, installed the committed Crew 0.86.0 from isolated
branch `codex/native-integration` (`3b98e45`) as the real `crew@claude-crew` plugin.
Codex reported it installed and enabled with all eight Codex skills.

A fresh disposable Codex thread `01a1048c-ca83-7d81-ac9c-4522bfb1068d` ran the
installed SessionStart and Stop hooks from the 0.86.0 cache. It emitted
`GATE_FIRST`, received the blocking Stop hook, cancelled its own unclaimed build,
and emitted `GATE_REENTERED` without a second user turn. The final Stop completed;
the build was cancelled with `writer_fence=null`. The exact hook sequence was
`sessionStart/completed`, `stop/blocked`, `stop/completed`.

This was a bounded installed lifecycle smoke, not another full native panel.
The original checkout and other active workflow state were untouched. Evidence
is retained locally under `.crew/codex-reloaded-gate-20261003/`.
