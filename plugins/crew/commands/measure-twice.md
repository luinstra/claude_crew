---
description: Build a reviewed plan through the engine-owned planning workflow
argument-hint: "[--panel preset | --seats subset] <task or design.md>"
allowed-tools: Bash, Read, Write, Task, AskUserQuestion
---

# Measure twice

Python owns the workflow. Transport only the issued `MeasureStep`; never infer a
stage from files or drive the loop from a review-local response.

Resolve the literal harness session ID. Write one schema-1 UTF-8 JSON request
spill under `.crew/requests/`, preserving `$ARGUMENTS` exactly as the
`raw_arguments` string (JSON escaping preserves its bytes after decoding):

```json
{"schema":1,"raw_arguments":"<exact user arguments>","requirements":null}
```

Run the dispatcher with the spill path and literal session ID; user text is
file data and must never enter a shell line:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" measure-twice -f <request-spill> --session-id <literal-session-id> --consume
```

For session re-entry use:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" measure-twice-resume --session-id <literal-session-id>
```

For an active reference use the exact `MeasureRef` returned by Python:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" measure-twice-next --session-segment <issued-segment> --loop-instance-id <issued-lifetime>
```

Transport the four response types:

- `needs_input`: show the exact ordered questions or bound human question. Before
  activation, capture the ordered answers and question identity in a new request
  envelope and call `measure-twice` again. For an active question, call
  `measure-twice-decide` with the exact owner and question identity and the human's
  decision. The engine has already persisted its human wait. Never originate a
  force or restart authorization. Request/decision examples and grammar are in
  `docs/measure-twice-protocol.md`.
- `work_batch`: launch every independent external action in the background using
  its issued `commands.execute` before waiting on native work. For non-external
  items run `commands.claim` first. Spawn a bare **fresh** Task only on `spawn`,
  at the issued role/model. Pass the prompt by reference exactly:
  `Task(subagent_type="<issued role>", model="<issued model>", prompt="Read <issued prompt_path> and perform exactly the issued action.")`.
  For `model=inherit`, omit the host model parameter. No name, team, resume,
  continuation, or background Task parameters. An actual Task hand-back is a
  completion signal; consume it immediately. A later notification saying that
  the report was already delivered is not another result to await. On `perform`, read and
  perform the issued parent prompt. `do_not_spawn` / `do_not_perform` means wait
  or reconcile; it never permits another launch.
- `waiting`: wait for actual host completion notifications. Do not poll files,
  rerun claimed actions, clear results, or call a model for bookkeeping.
- `terminal`: present the engine's summary and plan path, including any forced
  advisory text. Implementation requires a separate user request.

Preserve overlap: launch the entire independent review batch before any native
wait. After each completion notification/batch call `measure-twice-next` once;
that is the sole authority for subsequent work and completion.
Keep a `--print` host process alive until its owned background commands complete;
ending the process can kill them. If an explicit retry has only one pending
external action, run its issued execute command in the foreground. Recover an
interrupted command only after confirming actual host termination, retaining
successful reviewers.

For Claude Code 2.1.287 native advisors/reviewers with the actual launch's
`agentId` and `output_file` JSONL surface, use deterministic direct capture:
after launch run `commands.native_bind`, appending `--handle <actual agentId>`
and `--output-file <actual output_file>`. Bind once; never substitute another
handle. Only after the actual owned hand-back or completion notification run
`commands.native_capture` with those same fields and `--completion-observed`.
The helper validates owner, action, handle, project and exact prompt reference;
it extracts the single `SubagentHandback.input.message` without adding a final
LF or model recopying. The existing code helper hashes and submits those exact
bytes. It never infers completion from an artifact.
The known public task alias is accepted only for the exact host-owned
project/session/handle transcript; arbitrary aliases are refused.
Do not Read/tail the full task transcript into model context. Unsupported,
ambiguous or incomplete shapes refuse; retain evidence and use the existing
fallback only if the completed host return is actually available.
Admission is pinned to exactly CLI 2.1.287. Other CLI versions use the issued
scribe/host-Write fallback until their actual result surface is validated.

For hosts without that demonstrated direct surface, retain issued scribe hygiene: use the issued scribe role/model
and prompt template, insert the returned text only at its data marker, and Write
verbatim to the issued ingress. If that scribe fails, use the issued host-Write
fallback at its distinct issued ingress, copying the returned text with no
summarization or indentation cleanup. Preserve whether the original return ends
with LF; an added final LF is a transport mismatch. Do not claim byte fidelity
without an exact raw-byte audit. A partial or nonempty file is never proof that
the scribe returned successfully. Advisor
completion reports use parent Write at the issued `returned_path` without an
extra scribe; the advisor's staged plan remains the output named by its prompt. Parent actions
and formatter results use parent Write at the issued `returned_path`, without
another scribe. Pass that returned-text file to
the issued `commands.capture` with `-f`; the helper hashes, constructs the exact
envelope, submits, and handles replay. Use `--status failed|timeout|cancelled`
with the actual diagnostic for a failed return. For synthesis include the
judgment from the issued prompt using `--verdict`. APPROVED means
`minor_only=false`: omit `--minor-only`. The `--minor-only` flag is only valid
with REVISE and encodes `minor_only=true`; omit it for every false value.
Never generate a digest, hand-build a result envelope, select an
artifact path, or spawn a bookkeeping agent.

Shared `review-execute`, `review-capture`, `review-submit`, `review-recover`, and
`review-retry` responses are review-local bookkeeping. Always call the owning
`measure-twice-next` afterwards. Recovery requires the runtime/operator to confirm
`not_running`; elapsed time or an absent file is insufficient. Stop only owned
handles if the host supports cancellation; report unavailable cancellation APIs.
Only after that confirmation, append `--confirm-not-running` to the issued
`commands.recover`; the issued review recovery command already pins its diagnostic code.
