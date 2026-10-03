# Codex collaboration transport

This is the transport supplement for the four workflow skills and their cancel/context helpers. Read the matching
`commands/review.md`, `commands/debate.md`, `commands/measure-twice.md`, or
`commands/build.md` for the shared workflow recipe. Python remains authoritative
for routing, claims, transitions, prompts, verdicts, questions, recovery and
completion. Apply this supplement to Codex native mechanics; the Task/scribe and
Claude output-file instructions in the shared recipes apply to their own hosts.

The Codex manifest declares `skills-codex/`. Claude and Cursor retain their
existing command directories; do not copy these host-specific skills into the
shared `skills/` directory.

## Locate and enter

Resolve the plugin root from the physical location of the invoked
`skills-codex/<workflow>/SKILL.md`: two directories above its containing directory.
Use that root's `crew` executable in place of the recipes' `${CLAUDE_PLUGIN_ROOT}`.
No environment variable or global plugin/configuration change is needed. Run
from the requested project root and use the literal current harness session ID.
Do not set host-detection variables or alter a live session's owner.

Use the shared request grammar. Preserve user task text as file data for build
and measure-twice. Review and debate use their shared quoted argv grammar.
Subsequent work uses engine-issued argv: review/build `commands`, measure-twice
`commands_argv`. Older hosts retain measure-twice's rendered `commands` strings.
Quote each argv element using the shared POSIX quoting rule; do not reconstruct
references, dispatch options, model pins or artifact paths. Prefix shell calls
with `rtk proxy` when the project requires RTK.

For explicit re-entry, repeat the identical review/debate start and adopt its
returned current reference. For planning use `<plugin>/crew measure-twice-resume
--session-id <literal-id>`; for build use `<plugin>/crew build-resume --session-id
<literal-id>`. These commands resume the engine journal, not an agent conversation.
Installed discovery and automatic Stop re-entry passed the bounded 2026-10-03
Codex app-server gate; see [evidence](phase-7-codex-evidence.md).

## Launch and observe

For a non-external work item, run its issued claim first. Only `spawn` authorizes
launch. For `perform`, execute the issued parent prompt in the current context.
Other authorizations never permit a second spawn. External actions use their
issued execute argv and the existing provider route and allowance.

For an admitted Codex native item, pass `native_transport.spawn` unchanged to
`collaboration.spawn_agent`. The engine generates `task_name`, `message`,
`fork_turns="none"`, and, when pinned, `model` and `reasoning_effort`. The message
contains the body of the canonical `agents/<role>.md` and the issued prompt path.
There is no `role` or `subagent_type` spawn parameter. Reviewers and panelists
must never inherit parent history or receive an inline copy of the target.
The native advisor and built-in executor inherit model/effort when omitted;
named native review seats require explicit model pins; `inherit` is rejected
before freezing review inputs. Omitted reviewer effort freezes the external
Codex default, `xhigh`; explicit unsupported efforts fail before preparation.
The allowed effort strings match this collaboration API (low, medium, high,
xhigh, max, ultra); external-only values such as minimal are not substituted.

Save the actual returned handle and immediately run issued `commands.native_bind`
(or measure-twice `commands_argv.native_bind`) with `--handle <actual-handle>`.
Do not supply an output-file argument: this collaboration surface exposes no
machine-readable result file. Launch external seats independently. For native
seats, inspect the host's currently available child-agent slots and launch only
that many. Do not claim deferred seats. Await actual completions, capture their
returns, and use the owner-issued next step to fill available slots until the
whole frozen roster settles. Capacity exhaustion is a scheduling wait, not a
failed seat or permission to reduce the roster. With no free slots, wait for
owned running agents before claiming more work.

`run_build_batch` and `run_measure_batch` take `max_concurrency` from the host's
available capacity and run bounded waves. Every Codex-native batch refuses a
missing capacity before any claim, including a single action. An incomplete completion wave returns with
remaining handles still owned and later seats unclaimed. Passing that step back
to the batch runner cannot launch more work; capture its outstanding returns
and ask the owner for the next step first. External route selection
and frozen roster/quorum stay unchanged.
If a model/effort is unsupported, record the exact refusal. Never substitute,
change the route, use a CLI fallback, or retry the spawn on another model.

Use `collaboration.list_agents()` for host observations and
`collaboration.wait_agent(timeout_ms=...)` for notifications. A wait result is a
notification summary; consume the actual final message delivered for the bound
handle. A status of `complete`, elapsed time, or file existence alone is not the
owned final reply. Retain handles and binding paths for explicit re-entry. If
completion cannot be established after re-entry, leave claimed work outstanding
and follow the engine's existing confirmed recovery path.

`collaboration.followup_task(target,message)` exists but is not used for native
executor continuation in this increment: each issued native round is a fresh
agent. External executor exact-conversation continuation remains engine-owned.
Do not send workflow actions to an old native handle.

## Capture the final reply

After the actual owned final message, host-write its exact text to the issued
`native_transport.returned_path`, preserving CR/LF, indentation and final LF.
This is explicit host-written capture. Hashing and immutable receipts verify the
written bytes; they do not prove a byte-exact extraction from the host return.
Do not claim automatic extraction, parse transcripts, or launch a scribe.

Run the issued capture argv, adding `--handle <same-actual-handle>` and
`--completion-observed`. For review and measure-twice also add `--returned-file
<issued-path>`; build's capture argv already includes `--return-file`. Parent
formatter/synthesis captures use the existing capture CLI without native-handle
flags; synthesis includes its issued judgment. After review-local bookkeeping,
advance the owning build/measure-twice through its issued `commands.next` or
`commands_argv.next`, never through a review-local next operation.

Only a positively observed refusal **before any agent was launched** may be
captured without a handle: host-write the refusal text to the issued path and
append `--status failed --diagnostic <exact-refusal> --launch-refused`. Omit
`--completion-observed` in that case. A lost or uncertain launch is outstanding
work, not a refused launch. Once bound, the no-handle path is rejected.

An advisor writes its plan only to the issued staging path before replying.
Python seals and promotes that plan. A writer's final reply and workspace facts
are checked by the existing build guards; a final marker cannot waive a guard.
Duplicate callbacks with identical bytes replay existing receipts. Wrong handles,
changed bytes, superseded actions and stale owners cannot create a new result.

## Access and cancellation

Native agents inherit the parent's sandbox and tools. Canonical role discipline
is advisory; role Markdown does not enforce per-role tool access. Native review
model attribution is requested-only, not a runtime-model assertion.

Cancel the engine owner first through its existing cancellation command. Stop
only actual handles bound to its actions, using
`collaboration.interrupt_agent(target=<actual-handle>)`. Its return reports the
**previous** status, not quiescence. An interrupt request cannot release a build
writer fence. Only actual final completion or the existing explicit operator
confirmation of quiescence can do so. Report unavailable/failed interrupts and
leave uncertain writers fenced. Do not cancel another session's agents.
