# Engine-owned build protocol

Python owns implementation, revision, review, retry and completion. Commands and
hooks transport issued actions. The state is schema 5 with `bl_workflow` version 1;
reading state never upgrades it. Native Claude, Codex and OpenHands `crew:executor` roles
and external write routes retain their selection precedence. Codex and OpenHands native rounds
are fresh with `resume_executor=false`. Native requested model attribution
is inherit; no runtime model observation is invented. Cursor build review seats
remain external. Cursor native execution/lifecycle support is deferred.

Write a UTF-8 request under `.crew/requests/`:

```json
{"schema":1,"raw_arguments":"--seats sol --executor sol Implement the task"}
```

`crew build -f <spill> --session-id <literal> --consume` parses leading panel,
seats and executor pairs without shell evaluation. It preserves task bytes and
explicit panel/seats presence. Matching active requests retain their lifetime and
frozen executor, including when a later flag differs. Conflicting requests refuse.
`--consume` removes a successfully matched request even when its writer is still
waiting. A lock-busy start that was never admitted retains the request and returns
`ref: null`; retry that same start after the owned writer finishes.
`build-resume --session-id <literal>` discovers state; it never creates a lifetime.

All subsequent commands require issued `--session-segment` and
`--loop-instance-id`. Action commands also require `--action-id`. Use the literal
argv in each work item. `build-next` is the authority after every operation.

| BuildStep | Host responsibility |
|---|---|
| work_batch | Launch every independent review item before waiting; claim native work once and launch only with authorization. External execute claims internally. |
| waiting | Await actual completion; do not redispatch, poll output files, or infer termination from age. |
| needs_input | Show the exact question; the journal has already set awaiting_input. Submit only the human's bound decision. |
| terminal | Summarize the retained outcome and diagnostics. No paid work is issued. |

External work includes its frozen effective timeout and a host allowance of at
least timeout plus 60 seconds. Keep long background commands owned and alive to
completion. A single pending external action can run in the foreground. An
actually observed insufficient host limit refuses before launch; an unverified
limit is unknown. No model calls select executors, hash targets or copy envelopes.
The canonical request workspace is passed per call to the provider subprocess.
If a native or external claim discovers changed guards, it returns the actual
`needs_input` step without authorizing a launch or inventing in-flight work.

Native Claude Code 2.1.287 capture uses the actual launch's agentId/output_file:
run issued native_bind with `--handle` and `--output-file`, then native_capture
with the same values and `--completion-observed` only after actual hand-back.
The shared transport validates the owner, role, prompt, project and exact JSONL
surface, with LF-only framing. It extracts exact SubagentHandback message bytes.
Unsupported shapes use host Write of the completed return at issued returned_path,
then issued capture. Do not read whole transcripts or launch bookkeeping scribes.
External adapters retain the exact final report bytes for build capture before
display normalization. Codex uses its final-message file; Agy uses stdout; Cursor
uses its final response or plain stdout. Ordinary dispatch output and serialized
envelopes keep their existing format. Host Write capture uses `--return-file` for
`build-capture` and `--returned-file` (or `-f`) for `review-capture`, including
parent synthesis. Codex native capture additionally requires the bound actual
`--handle` and `--completion-observed`; see [Codex transport](codex-transport.md).

The final nonblank LF-delimited executor report line is exactly
`CREW_BUILD_STATUS: COMPLETED` or `CREW_BUILD_STATUS: BLOCKED`. For comparison only,
one trailing CR per line is removed. Earlier marker examples are ordinary text;
Unicode separators are not LF. Missing, quoted, indented, fenced or suffixed final
markers park before review or retry. Failed/timeout/cancelled/unavailable transport
overrides any marker. COMPLETED asserts that no task blocker remains.

HEAD, index-tree and branch must be known and unchanged for both executor routes.
A changed/unknown observation parks before review and cannot be forced. Guards
include unborn/detached repositories. They do not isolate file contents from other
processes; independent builds need separate workspaces. External executor-only
continuation reuses build-executor and its existing classifier. Reviewers remain
fresh. Resume-off omits the chain. Configured retry counts 0..2 permit only actually
terminated guard-clean external failures/timeouts, stacking edits with identical
prompt bytes within a round. Native failures, exhausted attempts and unknown
termination require human recovery. Cancellation and unavailable routes do not
authorize automatic retries. Adapter transport facts distinguish an actual timed
out process from an unrelated error mentioning a timeout. No rollback, clean,
commit or staging occurs.

Timeout teardown checks that the owned process group is absent after bounded
TERM/KILL escalation. The leader's exit alone is insufficient. Unconfirmed group
termination retains the claimed writer, forbids automatic retry/review/replacement,
and requires explicit quiescence recovery. Cursor stream JSONL uses LF-only framing
so Unicode separators within the final result cannot replace it with earlier text.

Claim and outstanding writer commit before launch. Capture retains immutable
return/receipt/submission artifacts and accepts status once under the owner lock.
External work holds its continuation lock until settlement; review starts after
release. Prepared review inputs checkpoint before run files, and accepted outcomes,
receipts and counters apply together. These are recoverable filesystem steps,
not a cross-file transaction. A crash before completion remains uncertain even
if a continuation record exists. Replay reconciles retained receipts without work.

| Question | Allowed decision and additional fields |
|---|---|
| executor_blocked, executor_report_recovery, execution_recovery | answer_executor: answer-file + confirmation stack_edits + issued workspace-sha256; retry_executor: confirmation stack_edits + workspace-sha256; adopt_edits: confirmation completed + issued completed-action + workspace-sha256 |
| review_timeout_changed | fresh_review: confirmation fresh_review + workspace-sha256 |
| completion_advisory | force: confirmation force; retry_review |
| synthesis_retry | retry_synthesis |
| route_unavailable | retry_route |
| workspace_guard | recheck_workspace |
| feedback_recovery | retry_feedback |
| Any question | cancel |

`build-decide` additionally requires issued question-id and kind. Inapplicable
fields refuse. Answers are nonblank UTF-8 spill data; original task and bounds
remain unchanged. Changed files/questions/answers cannot reuse authorization.
Force waives only current disclosed verdict advisories and records overrides.
Terminal outcomes and replay include those retained overrides. A bound
`recheck_workspace` decision observes restored guards and resumes in one step;
other changed question facts require fresh authorization.
Partial-panel retry preserves accepted seats; drift starts a fresh generation.
Replacing a review generation clears its obsolete retry authorization and human
pause before preparing new work.
Synthesis-only retry preserves the panel. Review timeout floors are frozen per
generation, with uncapped build base timeout (default 600); changed floors park
before launch. Standalone review and measure-twice retain their 540-second cap.

`build-recover --action-id <issued> --confirmation not_running` requires explicit
operator confirmation of actual quiescence. It clears an uncertain writer and
shows recovery choices; inactive owners remain inactive. Adoption requires known,
matching guard history. Recheck observes restored guards without modifying files.
Cancellation first ends authority, then requests cancellation only for owned
handles if the host API exists; unavailable/failed cancellation is reported.
Late completed returns may be retained but cannot advance an inactive owner.

An active or inactive outstanding writer prevents lifetime replacement in that
session. Status, hooks and swab display its exact recovery argv.
Verbose state status preserves JSON on stdout and shows the fence and recovery
argv on stderr, including for an inactive owner.
Cleanup retains state, locks and evidence regardless of age, rechecking under
locks; malformed writer journals remain protected. After confirmed quiescence ordinary cleanup
applies. Unreadable build state stays in place through Stop and generic admission
refuses replacement. Cleanup rechecks current status, active age policy and mtime
under the owner lock. The admission lock inode is stable. Initial classification
takes admission and sorted state locks, then releases both. Chain acquisition and
the Git baseline run outside admission. Replacement holds the chain before
reacquiring admission and sorted state locks, and revalidates ownership before
mutation. Progress takes continuation before owner state; cleanup takes admission
before sorted states. No path waits for continuation under admission; continuation
and review never nest. Build CLI lock contention returns retryable schema-1 errors
and retains unadmitted request spills.

Terminal replay is within the retained lifetime. An explicit new start after
normal completion/cancellation replaces that lifetime and makes old refs stale,
without paid work or mutation. A different harness session ID is a separate owner;
SessionStart reports old-owner recovery/cancel commands and never adopts it.
Real CLI --resume observations apply only to the recorded session/version/source
epoch, not every interactive resume. See phase-5-build-evidence.md for verification
and live-gate limitations. No migration, special restart, or upgrade workflow is
part of this protocol.
