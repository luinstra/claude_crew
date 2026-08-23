---
description: Multi-model standalone review
argument-hint: "<plan, code scope, or .md path>"
allowed-tools: Bash, Task, Read, Write
---

# Standalone review transport

Python owns target resolution, the frozen roster, prompts, claims, repair,
quorum, retry, and synthesis readiness. This file only transports issued work.
Never infer a seat, action, path, result shape, or next transition.

Encode every runtime argv value with this exact POSIX algorithm before placing
it in a command: reject NUL; emit a value matching
`^[A-Za-z0-9_@%+=:,./-]+$` unchanged; otherwise replace every single quote with
the five characters `'"'"'`, then wrap the whole value in single quotes. The
only shell expansion allowed below is `${CLAUDE_PLUGIN_ROOT}`.

Parse and preserve the command's substituted `$ARGUMENTS` before invoking
Python. Recognize `--panel VALUE`, `--seats VALUE`, `--base VALUE`,
`--timeout VALUE`, and `--force-external VALUE` independently, plus the
valueless `--inline-diff`. Reject a
missing value or a duplicate option. Remove only those option tokens; the
remaining positional tokens, in their original order, are the target. Use the
empty string as the target when none remain. `--panel` never consumes or
replaces `--seats`, and vice versa. Append only flags actually supplied by the
user, encoding every extracted target/value with the POSIX algorithm above.
Place those supplied options first, then the literal `--`, then the one encoded
target argv value. The separator is required even for a target such as
`--plan.md`; it preserves that target's bytes instead of letting argparse treat
it as another option. An empty target is therefore the final `''` value after
the separator.

Start with the literal harness session id. The first fence is the no-option
form; the second demonstrates the exact all-options form. Other valid forms are
the first command with only their supplied flag/value pairs appended. After
compaction, repeat the identical start command and adopt the returned current
reference.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review --session-id '<session-id>' -- '<target>'
```

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review --session-id '<session-id>' --base '<base>' --panel '<panel>' --seats '<seats>' --timeout '<seconds>' --force-external '<channels>' --inline-diff -- '<target>'
```

`--force-external` is the user's, exactly like the others: pass it only when
they supplied it, and never add it yourself to work around a refused spawn or a
role that did not resolve. It names the channels this run must execute as
external subprocesses, and Python resolves it over the configured value.

The harness this runs on is Python's to determine, not yours. It reads the
ambient environment of the shell the command runs in and freezes that answer
into the run. Pass nothing about the harness, and never set or clear an
environment variable to steer the answer: an operator who has to override it
does so in the shell that launches the harness, outside this file. Every later
command re-derives the same answer and returns a typed conflict if it changed,
so a wrong one is loud rather than silent.

A decoded top-level object containing `error` is a typed error envelope, not a
`ReviewStep`: surface its `message` verbatim and stop without reading step fields
or doing work. The only retryable typed error is `stale_ref` returned by the
single post-batch `review-next`; handle only that case by repeating the identical
start command and adopting its returned reference. For `stale_ref` from any
other command, and for every other typed error, surface `message` and stop.

On `needs_input`, ask the returned `question` verbatim, do no work, and stop.
After the user answers, restart with that answer as the target and the same
supplied options. For every non-`needs_input` response, print `display` verbatim
before claiming or executing any work.

For every `work_batch`, claim only native or parent actions before performing
them; an external action is claimed only by `review-execute`, so never call
`review-claim` for it. On Claude, start each external `review-execute` Bash call
with `run_in_background=true` and tool timeout
`(work_item.timeout_seconds + 60) * 1000` milliseconds (600000 at the 540-second
provider ceiling). Retain every returned background task ID and output-file path,
then immediately spawn the authorized native Task calls in the foreground. Do
not wait for an external result before spawning native work: the background
external process must overlap the native Task. Never use shell `&` or a compound
shell command. On a host without a background command tool, separate parallel
tool calls are best-effort and Python's frozen provider timeout remains
authoritative.

For each external WorkItem on Claude, invoke the Bash tool with exactly this
three-field shape, substituting only issued reference/action values and the
computed timeout:

`Bash(command="\"${CLAUDE_PLUGIN_ROOT}/crew\" review-execute --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>'", timeout=<computed-ms>, run_in_background=true)`

`run_in_background=true` is a Bash **tool input field**, not shell text. Do not
omit it, set it false, or wait for that Bash result before the native Task call.

If Python returns `provider_timeout_config_drift` for Agy, leave the old action
untouched. For a current effective timeout at or below 540 seconds, start a new
review with the returned explicit `--timeout <current-effective-seconds>` value;
that value re-resolves normally, and only a different canonical effective value
changes the review identity. If the current effective timeout exceeds 540
seconds, lower the Agy timeout configuration first, then start a new review with
the resulting explicit value.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-claim --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>'
```

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-execute --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>'
```

Every authorized native Task is a bare foreground one-shot: NO `name`,
`team_name`, background, resume, or continuation argument. Branch on the issued
`channel` value, never on a guess about which harness is running. The role and
model are always the issued values; the fences are stated per channel so a
change to one channel's mechanic can never silently retarget the other.

With `work_item.channel` = `claude`, spawn a reviewer with exactly:

`Task(subagent_type="<work_item.role>", model="<work_item.model>", prompt="You are the <work_item.seat> seat. Read <work_item.prompt_path> and follow it exactly.")`

With `work_item.channel` = `cursor`, spawn a reviewer with exactly:

`Task(subagent_type="<work_item.role>", model="<work_item.model>", prompt="You are the <work_item.seat> seat. Read <work_item.prompt_path> and follow it exactly.")`

With `work_item.channel` = `claude`, spawn a native formatter with exactly:

`Task(subagent_type="<work_item.role>", model="<work_item.model>", prompt="Read <work_item.prompt_path> and follow it exactly.")`

With `work_item.channel` = `cursor`, spawn a native formatter with exactly:

`Task(subagent_type="<work_item.role>", model="<work_item.model>", prompt="Read <work_item.prompt_path> and follow it exactly.")`

The formatter fences deliberately omit the seat preamble the reviewer fences
carry, on both channels.

Each pair of fences above is byte-identical across the two channels, and keeping
them that way is the invariant: there is ONE mechanic, spawn the issued role at
the issued model and hand it the issued path, and the channel decides only which
fence you read, never how an issued value is spent. They are written out twice so
a future host-specific change lands on one channel alone instead of silently
retargeting both. Any divergence must therefore be a recorded host fact in
`docs/cursor-host.md`, with the reason on the fence that differs.

On the `cursor` channel the issued model must be one the account has enabled for
subagents. A model that is not enabled is refused loudly at spawn. Treat that
refusal as a lost action and recover it with the codes below; there is no CLI
fallback for a reviewer seat issued native, so that seat settles FAILED and the
panel degrades by one voice (quorum recounts the usable seats and the digest
synthesizes from whatever returned). Never substitute a neighbouring model
yourself, and never spawn a role the action did not name. If every native seat
is refused this way, the fix is enabling those models in Cursor, not anything
you can do from here.

Also on the `cursor` channel: a spawned reviewer, formatter, or scribe INHERITS
the tools of the session that spawned it, because that host offers no per-role
tool field. The reviewer and formatter adapters ship `readonly: true`, but the
one enforcement evidence for that key comes from the external CLI, not from the
in-session surface these Tasks run on, so plan for it being inert here. The
reviewer's read-only posture and the formatter's and scribe's write restraint
are then prose those roles hold themselves to, not boundaries the harness
enforces. The issued action says which tier applies: `access=read-only` means the
constraint is mechanical, `access=read-only-advisory` means it is prose. Every
native action on this host is advisory. That is less than the `claude` channel's
support roles, which are tool-scoped (the `claude` reviewer is convention too, so
that one is the same tier), and less than the `cursor-agent` subprocess route
these seats used before, which ran under `--mode plan` and applied no edits.
Weigh that before reviewing material you do not trust.

For native reviewer and native formatter Tasks only, the parent must not Read or
inline the prompt contents; those Tasks receive the issued path by reference.
The scribe is the required exception because it does not read the file itself:
the parent Reads the issued primary scribe prompt, then replace its one exact
`{{REVIEWER_RETURN_DATA}}` marker with the reviewer's returned text. Pass those
fully substituted contents to a fresh bare Task on the same channel the reviewer
used.

When that reviewer's `channel` is `claude`, invoke exactly:

`Task(subagent_type="<return_transport.primary.role>", model="<return_transport.primary.model>", prompt="<fully substituted primary prompt contents>")`

When that reviewer's `channel` is `cursor`, invoke exactly:

`Task(subagent_type="<return_transport.primary.role>", model="<return_transport.primary.model>", prompt="<fully substituted primary prompt contents>")`

The scribe prompt names the exact primary ingress path; do not change it. For a
native reviewer, use the distinct host-write fallback path after any observable
scribe failure, primary-landing failure, primary-read failure, or primary-hash failure,
and never run the scribe against the fallback path. The fallback is
the exact issued `return_transport.fallback.ingress_path`, written by the host
with the same returned review bytes. Formatter and synthesis actions have no fallback transport.
The Task RESULT is the sole completion signal, while the landed file and hash
remain the result authority. The issued model values are requested model pins;
do not infer, substitute, or report them as proof of the model the harness
actually ran.

When a claimed formatter instead has `driver=parent` and
`access=parent-context`, do not spawn or emulate a formatter Task. Read exactly
`work_item.prompt_path`, perform that formatter work in the current host
context, Write the transformed text only to `work_item.ingress_path`, then land,
hash, and submit the issued HostResult below. A claimed parent synthesis is a
separate current-host operation: read its issued prompt, perform the synthesis
in this context, and write only its issued ingress artifact before hashing and
submission.

For native and parent completions, copy the issued `host_result_template`, write
only the issued artifact, replace its hash placeholder with the SHA-256 of those
exact bytes. A native reviewer's primary success keeps the template's primary
artifact path. If the primary path cannot be landed, read, or hashed, perform
the fallback Write before producing a failed HostResult. When the host-write
fallback is used, replace BOTH `host_result_template.artifact.path` with the
exact issued `return_transport.fallback.ingress_path` and the artifact hash
with the hash of those fallback bytes. Never pair fallback bytes with the
unchanged primary path. Formatter and synthesis never switch artifact paths.
Immediately after the artifact Write and before the HostResult Write, replace
the one `'<artifact-path>'` argument below with the exact selected issued
artifact path encoded by the POSIX algorithm above, then run this one process:

```bash
python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' '<artifact-path>'
```

Accept exactly one stdout line matching `[0-9a-f]{64}` and copy that literal
into the existing `artifact.sha256`; never invent or transform a digest. For a
native reviewer, run this hash check first on the primary and then, after any
primary failure, on the selected fallback. Only if the selected artifact
cannot be written or hashed, or its stdout does not match exactly, submit the
documented non-ok form with `status=failed`, artifact and judgment null, and
the non-empty diagnostic `artifact_sha256_failed`. Write that exact object to
`submission_path`, and submit it. For a successful
synthesis, also replace the template's null judgment with exactly a valid
verdict (`APPROVED` or `REVISE`) and boolean `minor_only`; the unmodified null
judgment is deliberately rejected. For `failed`, `timeout`, or `cancelled`, set
artifact and judgment to null and provide a non-empty diagnostic. Do not
construct JSON in Bash or add fields. A rejected submission remains unconsumed
for explicit correction outside this adapter; this adapter surfaces the typed
error and stops as specified below.

Map every reviewer, formatter, or synthesis Task/parent-context execution
outcome exactly: an execution error becomes `status=failed`, an explicit timeout
becomes `status=timeout`, and a cancellation becomes `status=cancelled`. In each
case use the same issued `HostResult` and `submission_path`, set artifact and
judgment to null, omit artifact/hash data, add a nonblank diagnostic, submit,
and continue the state machine. A native reviewer scribe transport is the
explicit exception: when the reviewer returned text but the primary scribe or
landing fails, follow the host-write fallback rule above before producing a
failed HostResult. Submit an authenticated `ok` artifact exactly once; do not classify its textual content or retry it in this Markdown adapter. The Python workflow owns unusable-content admission and converts authenticated unusable evidence into an ordinary failed action. A path, hash, ref, schema, state, or
other integrity rejection must be surfaced and stopped. A nonempty unstructured
reviewer response remains a valid `ok` result.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-submit -f '<submission-path>' --consume
```

After every known completion response, derive aggregate state once. A
post-batch `stale_ref` race is handled only by repeating the identical start.
For a Claude background external process, await its host completion notification
without polling, then Read its exact returned output file once and handle that
typed response before deriving aggregate state. For `waiting`, await handles
still owned by this host; after compaction, yield the returned in-flight ids
without polling or reclaiming.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-next --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>'
```

Recover only after a positive host check proves the claimed action is no longer
running, using exactly the action-kind/driver diagnostic mapping:

- native reviewer → `native_task_lost`
- external reviewer → `external_process_lost`
- native formatter → `formatter_task_lost`
- parent formatter → `parent_formatter_lost`
- parent synthesis → `parent_synthesis_lost`

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-recover --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>' --confirm-not-running --diagnostic-code '<diagnostic-code>'
```

Recovering a lost NATIVE FORMATTER does not fail it. Read the returned step
rather than assuming the action settled: it comes back as the same action id
with `driver` = `parent`, so do the repair in this context from its unchanged
`prompt_path` and submit it like any parent action. A repair must never be the
reason a panel loses a seat, so a role this host will not spawn falls back to
the parent context rather than costing the seat its findings. It happens at most
once; a `parent_formatter_lost` on the reissued action settles it failed.

A lost native REVIEWER settles failed. A seat's answer belongs to the model that
gave it, and its own channel is one this host drives in-session rather than as a
subprocess, so there is nothing to move it to. The panel degrades rather than
dying: quorum recounts the usable seats and the digest synthesizes from whatever
returned.

Retry pending reviewer seats only on explicit user request. Omit `--seats` to
retry every pending seat, or supply the requested frozen-roster subset. This
explicit rule governs seat retries. Separately, repeating the identical start
after `synthesis_failed` intentionally lets Python issue its attempt-local
synthesis restart; adopt that returned reference without inventing a seat retry.
Use exactly one of these explicit seat-retry forms.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-retry --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>'
```

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-retry --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --seats '<seats>'
```

On `terminal`, branch only on the returned status:

- `complete`: read and present the returned `synthesis_path` contents together
  with the judgment and panel facts.
- `quorum_not_met`: read and present the returned `synthesis_path` contents,
  judgment, and panel facts, labeled explicitly as non-certifying.
- `all_failed`: present the diagnostic and panel facts, then read and present
  the returned `panel_path`; do not invent or request synthesis.
- `synthesis_failed`: present the diagnostic and panel facts, then read and
  present the returned `panel_path`; do not invent or request synthesis.

The same presentation rules apply after compaction or an identical-start
resume. Build and measure-twice continue to use `review-prep`; this adapter does
not migrate either loop.
