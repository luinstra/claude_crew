---
description: Implement and verify a task through the engine-owned build workflow
argument-hint: "[--panel preset | --seats subset] [--executor seat] <task>"
allowed-tools: Bash, Task, Read, Write, AskUserQuestion
---

# Build

Python owns selection, implementation/review cycles, retries, human decisions and
completion. Transport only issued BuildStep actions; do not implement in the
parent, select another executor, infer a stage from files or record verdicts yourself.

Write one schema-1 UTF-8 JSON spill under `.crew/requests/`, preserving $ARGUMENTS
exactly as raw_arguments. Task text is file data; never interpolate it into shell.

```json
{"schema":1,"raw_arguments":"<exact user arguments>"}
```

Use the literal harness session ID injected by SessionStart:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" build -f <request-spill> --session-id <literal-session-id> --consume
```

For re-entry without a task:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" build-resume --session-id <literal-session-id>
```

For every active or retained owner, follow the exact issued BuildRef:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" build-next --session-segment <issued-segment> --loop-instance-id <issued-lifetime>
```

- needs_input: show the exact bound question and wait for the human. The journal
  already persisted awaiting_input. Use build-decide with the issued owner,
  question-id, kind and only applicable human-supplied fields. Never originate
  force, stacking, adoption or not_running confirmation. See docs/build-protocol.md.
- work_batch: external commands.execute claims internally. Honor the issued
  provider timeout and host allowance (timeout plus settlement grace). Launch all
  independent review commands before waiting. Keep background commands owned and
  the host process alive until actual completion (use the host-owned blocking
  task-result wait for a background Bash handle if available, never output-file polling).
  When that wait tool is unavailable, launch independent foreground Bash/Task
  calls together before awaiting their hand-backs. Never end a CLI print process
  while owned background work is running. If there is only one external action,
  run it in the foreground. If an actually observed host limit cannot
  honor the allowance, refuse before launch with its diagnostic. Do not invent a
  limit or replace the route.
- For non-external work run commands.claim first. Only spawn on spawn authorization,
  using the issued role/model and exact prompt by reference:
  Task(subagent_type="<issued role>", model="<issued model>",
  prompt="Read <issued prompt_path> and perform exactly the issued action.").
  Native executor inherits: omit model when null/inherit. Use bare fresh Tasks,
  with no name, team, resume, continuation or background Task parameters.
  A Task hand-back is actual completion; consume it immediately. A duplicate
  notification is not another result. On perform, read and perform the issued
  parent prompt. Wait/reconcile on other authorization values.
- waiting: await actual completion notifications. Do not poll files, relaunch a
  claimed item, clear results or call models for bookkeeping. Unknown writing work
  stays fenced until the operator confirms actual quiescence through build-recover.
  Show the exact recovery argv in the waiting display; run it only after the human
  supplies `not_running`. Recovery then issues the bound question for build-decide.
- terminal: summarize the engine outcome, verification, remaining work and forced
  advisories honestly. Do not launch paid work. The engine finalizes phase=done.

After every completed operation/batch call commands.next (build-next); review-local
steps cannot authorize further implementation or completion. Reviewers stay fresh;
the executor-only continuation is managed by Python. Never reconstruct dispatch
arguments or copy the session's last envelope into an action.

For Claude Code 2.1.287 with actual agentId/output_file metadata, use direct capture:
run issued commands.native_bind with --handle <actual agentId> and --output-file
<actual output_file>. Only after actual hand-back/completion run commands.native_capture
with the same fields plus --completion-observed. Do not Read/tail full transcripts
into context. Shared code validates and extracts exact SubagentHandback message bytes
without adding an LF. Unsupported shapes refuse; use host Write of the completed
return at the issued returned_path/ingress. For executor `build-capture`,
commands.capture already includes `--return-file <that path>`: run it as issued.
For review `review-capture`,
append `--returned-file <that path>` (or `-f <that path>`). Preserve exact bytes,
including final LF. Do not launch a scribe. Parent synthesis/formatter uses the
review host-Write capture with `--returned-file`; append the issued verdict and
minor-only fields for synthesis. Retain full panel evidence and report.

Both executor routes require the final nonblank LF-delimited report line to be
exactly CREW_BUILD_STATUS: COMPLETED or CREW_BUILD_STATUS: BLOCKED. COMPLETED asserts
no unresolved task blocker. Transport success cannot override BLOCKED or an invalid
marker; Python parks these before review. Every work item preserves unstaged,
uncommitted changes on the same branch. Unknown or changed HEAD/index/branch facts
park before review, including native execution. Do not clean, reset or waive guards.

To cancel use /crew:cancel-build. Cancellation ends authority before requesting
owned-handle cancellation, and retains uncertain writer fences until real completion
or explicit operator recovery. Independent builds need separate workspaces; workspace
facts do not mechanically isolate file edits by other processes.
