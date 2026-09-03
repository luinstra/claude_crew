---
description: Crew-native multi-model debate on a question — single round (council) or multi-round with rebuttals across a config-aware panel; override with --panel/--seats. Use for open-ended discussion, not plan/code review.
argument-hint: "[--rounds N] [--panel ...] [--seats ...] [--timeout S] [--force-external ...] <question>"
allowed-tools: Bash, Task, Read, Glob, Write
---

# Debate transport

The question is the frozen target. Python owns question freeze, the roster,
panelist prompts, claims, quorum, and synthesis readiness. This file only
transports issued work and never infers a seat, action, path, result shape, or
next transition.

Parse and preserve the substituted `$ARGUMENTS` before invoking Python. Inspect
only its leading option prefix. Starting at the first token, recognize
`--rounds VALUE`, `--panel VALUE`, `--seats VALUE`, `--timeout VALUE`, and
`--force-external VALUE`, consuming each recognized option and its value until
the first non-option token or an explicit `--` terminator. The first non-option
token and everything after it are the question in original order and byte for
byte. An explicit `--` is removed as the terminator, and every token after it
belongs to the question. Do not scan question text for options. Reject a missing
value or duplicate option in the leading prefix. An omitted `--rounds` means 1.
Before invoking the engine, reject anything else: a supplied `--rounds` value must be an
integer from 1 through 5. On this command, an explicit `--seats` value is the
whole roster and `--panel` is ignored; review keeps its `--panel` and `--seats`
axes independent.
`--rounds` is forwarded to the engine. The remaining recognized options are
forwardable. Append
only flags actually supplied by the user, and every valid command is the
no-option form plus only the supplied forwardable option/value pairs. Encode
every extracted question or option value with the POSIX algorithm below. Place
those supplied options first, then the literal `--`, then the one encoded
question argv value. The separator is required even for a question such as
`--plan.md`; it preserves that question's bytes instead of letting argparse
treat it as another option. An empty question is therefore the final `''` value
after the separator.
Encode every runtime argv value with this exact POSIX algorithm before placing
it in a command: reject NUL; emit a value matching
`^[A-Za-z0-9_@%+=:,./-]+$` unchanged; otherwise replace every single quote with
the five characters `'"'"'`, then wrap the whole value in single quotes. The
only shell expansion below is `${CLAUDE_PLUGIN_ROOT}`.

The engine owns every requested round and the final synthesis. With `--rounds N`
the engine freezes each completed round into the successor run before opening
the next one.

`--force-external` is forwardable on every round. It is the user's option: pass
it only when supplied, never add it to work around a refused spawn or a role
that did not resolve, and let Python resolve it over configuration.

Start with the literal harness session id. The first fence is the no-option
form; the second demonstrates the supplied option form. After compaction,
repeat the identical start command and adopt the returned current reference.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" debate --session-id '<session-id>' -- '<question>'
```

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" debate --session-id '<session-id>' --rounds '<rounds>' --panel '<panel>' --seats '<seats>' --timeout '<seconds>' --force-external '<channels>' -- '<question>'
```

The harness this runs on is Python's to determine, not yours. It reads the
ambient environment of the shell the command runs in and freezes that answer
into the run. Pass nothing about the harness, and never set or clear an
environment variable to steer the answer. Every later command re-derives the
same answer and returns a typed conflict if it changed, so a wrong one is loud
rather than silent.

A decoded top-level object containing `error` is a typed error envelope, not a
`ReviewStep`: surface its `message` verbatim and stop without reading step
fields or doing work. The only retryable typed error is `stale_ref` returned by
the single post-batch `review-next`; handle only that case by repeating the
identical start command and adopting its returned reference. For `stale_ref`
from any other command, and for every other typed error, surface `message` and
stop.

On `needs_input`, ask the returned `question` verbatim, do no work, and stop.
After the answer, restart with it as the question and the same supplied options.
For every other response, print `display` verbatim before claiming or executing
work.

For every `work_batch`, claim only native or parent actions before performing
them; an external action is claimed only by `review-execute`, so never call
`review-claim` for it. On Claude, start each external `review-execute` Bash call
with `run_in_background=true` and tool timeout
`(work_item.timeout_seconds + 60) * 1000` milliseconds. Retain every returned
background task id and output path, then immediately spawn authorized native
Task calls in the foreground. Do not wait for an external result before
spawning native work: the background external process must overlap the native
Task. Never use shell `&` or a compound shell command. On a host without a
background command tool, separate parallel tool calls are best-effort and
Python's frozen provider timeout remains authoritative.

For each external WorkItem on Claude, invoke the Bash tool with exactly this
three-field shape, substituting only issued reference/action values and the
computed timeout:

`Bash(command="\"${CLAUDE_PLUGIN_ROOT}/crew\" review-execute --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>'", timeout=<computed-ms>, run_in_background=true)`

`run_in_background=true` is a Bash tool input field, not shell text. Do not omit
it, set it false, or wait for that Bash result before the native Task call.

If Python returns `provider_timeout_config_drift` for Agy, leave the old action
untouched. For a current effective timeout at or below 540 seconds, start a new
debate with the returned explicit `--timeout <current-effective-seconds>` value.
If the current effective timeout exceeds 540 seconds, lower the Agy timeout
configuration first, then start a new debate with the resulting explicit value.
Never add `--force-external` yourself to work around a refused spawn.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-claim --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>'
```

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-execute --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>'
```

Every authorized native Task is a bare foreground one-shot: no `name`,
`team_name`, background, resume, or continuation argument. Branch on the issued
`channel` value, never on a guess about which harness is running. The role and
model are always the issued values; the fences are stated per channel so a
change to one channel's mechanic cannot silently retarget the other.

With `work_item.channel` = `claude`, spawn a panelist with exactly:

`Task(subagent_type="<work_item.role>", model="<work_item.model>", prompt="You are the <work_item.seat> seat. Read <work_item.prompt_path> and follow it exactly.")`

With `work_item.channel` = `cursor`, spawn a panelist with exactly:

`Task(subagent_type="<work_item.role>", model="<work_item.model>", prompt="You are the <work_item.seat> seat. Read <work_item.prompt_path> and follow it exactly.")`

The two channel fences are byte-identical. The issued role is the
provider-specific panelist role; the action remains reviewer-kind so the six
reference-taking verbs stay unchanged.

On the `cursor` channel, only a Cursor host issues a native Task, and its model
is the seat's `native_model` pin. A seat without that pin is dropped during
resolution. On any other host, a cursor seat runs externally through the
`cursor-agent` CLI at its issued model, and no native work item is issued for
it. Never substitute a neighboring model or spawn a role the action did not
name. A native seat's read-only posture is the issued access tier and the role
adapter's discipline.

Native actions are stamped `model_attribution: requested-only` by the engine for
exactly this reason, and nothing the host submits can change that stamp. The
issued model values are requested model pins; do not infer, substitute, or
report them as proof of the model the harness actually ran.

Also on the `cursor` channel, a spawned panelist inherits the tools of the
session that spawned it because that host offers no per-role tool field. The
panelist adapter's read-only posture is prose the role holds itself to, not a
boundary the harness enforces. The issued action says which tier applies:
`access=read-only` means the constraint is mechanical, and
`access=read-only-advisory` means it is prose. Every native action on this host
is advisory.

For native panelist Tasks only, the parent must not Read or inline the prompt
contents. The scribe is the required exception: Read the issued primary scribe
prompt, replace its one exact `{{REVIEWER_RETURN_DATA}}` marker with the
panelist's returned text, and pass the fully substituted contents to a fresh
bare Task on the same channel.

When that panelist's `channel` is `claude`, invoke exactly:

`Task(subagent_type="<return_transport.primary.role>", model="<return_transport.primary.model>", prompt="<fully substituted primary prompt contents>")`

When that panelist's `channel` is `cursor`, invoke exactly:

`Task(subagent_type="<return_transport.primary.role>", model="<return_transport.primary.model>", prompt="<fully substituted primary prompt contents>")`

The scribe prompt names the exact primary ingress path. Use the distinct issued
host-write fallback path after an observable scribe or primary landing failure,
and never run the scribe against the fallback path. The Task RESULT is the sole
completion signal; the landed file and hash remain the result authority.

A claimed parent synthesis is a separate current-host operation. When a claimed
parent synthesis has `driver=parent` and `access=parent-context`, do not spawn or
emulate a panelist Task. Read exactly `work_item.prompt_path`, perform the
synthesis in the current host context, and Write only `work_item.ingress_path`.
Then copy the issued `host_result_template`, replace its hash placeholder with
the SHA-256 of those exact bytes, and submit it. For a debate, leave the
template's judgment null
because the engine rejects any verdict.

For native and parent completions, copy the issued `host_result_template`, write
only the issued artifact, and replace its hash placeholder with the SHA-256 of
those exact bytes. A native panelist's primary success keeps the template's
primary artifact path. If the primary path cannot be landed, read, or hashed,
perform the fallback Write before producing a failed HostResult. When the
host-write fallback is used, replace BOTH `host_result_template.artifact.path`
with the exact issued `return_transport.fallback.ingress_path` and the artifact
hash with the hash of those fallback bytes. Never pair fallback bytes with the
unchanged primary path. A debate synthesis never switches artifact paths.
Immediately after the artifact Write and before the HostResult Write, replace
the one `'<artifact-path>'` argument below with the exact selected issued
artifact path encoded by the POSIX algorithm above, then run this one process:

```bash
python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' '<artifact-path>'
```

Accept exactly one stdout line matching `[0-9a-f]{64}` and copy that literal
into the existing `artifact.sha256`; never invent or transform a digest. For a
native panelist, run this hash check first on the primary and then, after any
primary failure, on the selected fallback. Only if the selected artifact cannot
be written or hashed, or its stdout does not match exactly, submit the
documented non-ok form with `status=failed`, artifact and judgment null, and the
non-empty diagnostic `artifact_sha256_failed`. Write that exact object to
`submission_path`, and submit it. For `failed`, `timeout`, or `cancelled`, set
artifact and judgment to null and provide a non-empty diagnostic. Do not
construct JSON in Bash or add fields. A rejected submission remains unconsumed
for explicit correction outside this adapter; surface the typed error and stop.

Map every panelist or parent-synthesis execution outcome exactly: an execution
error becomes `status=failed`, an explicit timeout becomes `status=timeout`, and
a cancellation becomes `status=cancelled`. In each case use the same issued
`HostResult` and `submission_path`, set artifact and judgment to null, omit
artifact and hash data, add a nonblank diagnostic, submit, and continue the
state machine. A native panelist scribe transport is the explicit exception:
when the panelist returned text but the primary scribe or landing fails, follow
the host-write fallback rule above before producing a failed HostResult. Submit
an authenticated `ok` artifact exactly once; do not classify its textual
content or retry it in this Markdown adapter. Python owns unusable-content
admission and converts authenticated unusable evidence into an ordinary failed
action. A path, hash, ref, schema, state, or other integrity rejection must be
surfaced and stopped. A nonempty unstructured panelist response remains a
valid `ok` result.

A debate never issues a formatter action; if a work item's kind is formatter, surface it as a protocol error and stop.

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

Recover only after a positive host check proves a claimed action is no longer
running, using the issued kind and driver mapping:

- native panelist: `native_task_lost`
- external panelist: `external_process_lost`
- parent synthesis: `parent_synthesis_lost`

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-recover --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --action-id '<action-id>' --confirm-not-running --diagnostic-code '<diagnostic-code>'
```

A lost native panelist settles failed with the same action id and driver. The
panel degrades rather than dying: quorum recounts usable seats and the digest
synthesizes from whatever returned. Retry pending panelist seats only on an
explicit user request, using the frozen-roster subset or all pending seats. A
round that already closed refuses the retry with `round_superseded`, pending
seats included: the following round reissues the whole roster fresh, so the
answer to a seat that failed in a closed round is `review-next`, not a retry.
Separately, repeating the identical start after `synthesis_failed` triggers the
engine's attempt-local synthesis restart; adopt that returned reference without
inventing a seat retry.

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-retry --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>'
```

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" review-retry --session-segment '<session-segment>' --run-id '<run-id>' --attempt-id '<attempt-id>' --target-sha256 '<target-sha256>' --seats '<seats>'
```

For a successful synthesis leave the template's null judgment exactly as
issued; a debate synthesis carries no verdict, and a judgment object is rejected.

On `terminal`, branch only on the returned status:

- `complete`: read and present the returned `synthesis_path` contents together
  with the panel facts.
- `quorum_not_met`: read and present the returned `synthesis_path` contents and
  panel facts, labeled explicitly as non-certifying.
- `all_failed`: present the diagnostic and panel facts, then read and present
  the returned `panel_path`; do not invent or request synthesis.
- `synthesis_failed`: present the diagnostic and panel facts, then read and
  present the returned `panel_path`; do not invent or request synthesis.
- round_complete: not a final outcome; run the review-next fence once with the returned reference and continue the loop with the step it returns (its ref names the next round's run).

The same presentation rules apply after compaction or an identical-start resume.

---

Subscription safety: on a Claude Code host, native Claude voices are in-session
Task seats, using no `claude -p` or Anthropic API. On other hosts, the engine
may drive external `claude` CLI; a missing CLI is a named skipped result, never
a host-native emulation. A stray `ANTHROPIC_API_KEY` is irrelevant to the
NATIVE path. This council is fully self-contained in crew: NEVER call `agy -p` /
`codex exec` / `cursor-agent` directly, and NEVER hand off to any external debate
plugin/shell.
