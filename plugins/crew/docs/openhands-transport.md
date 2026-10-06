# Native OpenHands transport

This integration is implemented against OpenHands SDK 1.51.0 source and tested
with its real plugin loader. Canvas installation and model-backed workflow gates
have not yet been run. It does not create SDK conversations or launch provider
CLIs. The native parent calls its existing `task` tool and the shared Crew engine
continues to own every workflow decision.

## Supported backend contract

Use a native OpenHands `Agent` with task, terminal and file-editor tools configured.
Admission accepts `task_tool_set`/`TaskToolSet`, `terminal`/`TerminalTool`, and
`file_editor`/`FileEditorTool`. Unknown names do not satisfy these requirements.
Original tool names and parameters remain in the configuration fingerprint.
Generated role definitions use the SDK preset's lowercase registration names;
the host must register those native implementations normally. ACP agents are
refused. Python 3.11+ and POSIX `fcntl` are required by the Crew engine.

Native delegation copies the parent's confirmation policy. In the inspected SDK,
TaskManager automatically continues pending child actions when no confirmation
handler is configured, and does not promise identical security-analyzer inheritance.
Approval of native delegation can authorize child work; Crew adds no stronger
per-child approval guarantee. Preserve the host's controls. Read-only role
instructions remain advisory because terminal can write.

The first transport requires the active backend's existing persisted conversation
directory (`base_state.json` and `events/event-*.json`) to be readable from the
same filesystem as the terminal workspace. The persisted `workspace.working_dir`
must resolve to that workspace. The SDK persists those files server-side; a remote
Canvas client does not automatically expose them to a sandbox. Backend operators
must supply the actual configured directory and backend identity. Do not invent
paths, copy a model-rendered transcript, disable security, or create a second
conversation to meet this prerequisite. A backend without this normal supported
mapping needs an authenticated event-read bridge; that remote capability is not
implemented here. No credentials or endpoints are copied into Crew state.

Native role names include the full SHA-256 of their generated definition (canonical
body, host instructions, profile reference/store location and tool metadata). The
wrapper recomputes that identity before admitting the route. Old plugin versions
and ordinary `crew-reviewer` definitions therefore cannot shadow the current role.
Unknown exact names fail at the native task tool; Crew never falls back to a generic
agent. Attribution remains requested/configured only.

The trusted native host must honor its loaded plugin definitions. SDK registration
is first-wins; deliberate registration of a different callable under the exact
content-derived name is outside this contract. Neither a name nor matching metadata
attests the callable. SDK `get_agent_factory` can inspect the effective definition
only in the running task process; a fresh Python registry and `/sub-agents` discovery
are not live registry proof. The SDK collision tests retain this limitation explicitly.
Live installation validation must include actual role/tool behavior. There is no
claim of cryptographic runtime attestation or resistance to a hostile host.

### Obtaining the local backend inputs

The backend operator supplies its actual stable backend ID and the active
conversation's persisted directory, from the deployment's existing conversation
configuration, and the native terminal's working directory. The SDK's
`ConversationState.persistence_dir` is the directory containing `base_state.json`
and the `EventLog`'s `events/event-*.json`; it is not necessarily the configured
parent persistence root. Check `base_state.json.id` against the selected Canvas
conversation and `workspace.working_dir` against the terminal workspace. Run the
wrapper without a Crew command first to obtain the validated snapshot path, then
use that snapshot in the command invocation. Missing files/tools, wrong workspace
or ACP agent fail before any action claim.

The plugin cannot discover a remote server's private filesystem from the Canvas
browser. OpenHands hook environment provides project/session IDs, but no backend
identity or readable persistence path; those variables must not be used to invent
one. Installation setup must supply these inputs through the existing backend's
normal filesystem mapping. No added mount, separate conversation, secret export or
HTTP bridge is implied. Without the mapping this local bridge is unavailable.

## Installation

Install the source `github:luinstra/claude_crew`, a reviewed immutable `ref`, and
`repo_path: plugins/crew` through the active backend's normal plugin management.
The root `plugin.json` selects the Agent Plugins 1.0.0 loader, and `dev.openhands/`
contains six commands and all eight native agents. The OpenHands loader ignores
the sibling Claude/Cursor hooks and manifests. Older backends lacking that loader
must upgrade to the verified loader contract before using this source. No Canvas
UI click path has been verified. After installation, use a fresh native conversation
and verify `/crew:review` and the other commands appear. Disable/re-enable and
restart/reopen in that actual backend are part of pending live validation.

For a self-contained local package, run:

```sh
python3 plugins/crew/scripts/openhands-package.py --output /tmp/crew-openhands-package
```

Install that directory using the backend's normal local plugin flow. The exporter
copies an allowlist of shared engine sources; it excludes tests, bytecode, caches,
other host manifests, `.crew` state, and private investigation/addendum documents.
Only the runtime transport/protocol guides and shared-command host references are
included from `docs/`. Generated role bodies retain the canonical
`agents/*.md` instructions. `--check` at the source checks exact generation parity.
The portable manifest participates in the existing transactional version bump.

The default is one native inherited-profile review seat and a native inherited
executor. Global Crew review/debate panels and global external build executors
cannot inject external processes. Explicit external seats or executors fail.
This default is single-model, with configured-profile attribution only.

To use existing named backend LLM profiles, export with repeated `--profile NAME`
and `--profile-store-dir /actual/backend/profiles`. The directory is the backend's
existing profile store, not a new Crew store. The package generates native role
variants whose `model` is that profile reference and never copies the profile.
Declare seats in the existing Crew config, for example:

```toml
[seats.native-a]
via = ["openhands"]
model = "configured-a"
```

Pass `--seats native-a,native-b` explicitly to select packaged profiles. Duplicate
profile votes, unregistered profiles, mixed channels, `--panel`, and per-seat
reasoning-effort overrides are refused. Configure effort in OpenHands' own profile.
Role hashes and fingerprints of effective nonsecret named profiles, parent agent,
LLM, tool parameters and security settings are frozen in the context. Named profiles
resolve `provider_connection_id` through the SDK's sibling
`provider-connections/provider_connections.json`; the referenced connection's
`base_url` wins, including null. Missing, malformed or changed routing fails closed.
This includes same-model effort, output limits and provider endpoints. SDK secret
fields (including AWS access IDs and session tokens) and connection save timestamps
are excluded from fingerprints. SDK 1.51.0's native named-agent factory calls
`LLMProfileStore.load` without a cipher. Crew therefore rejects named routes with
Fernet-encrypted secrets in any SDK LLM secret field or in the linked provider
connection, before creating or claiming workflow work (`encrypted_native_profile`).
This includes the SDK's serialized encrypted strings; values are never displayed.
Use the inherited parent route, whose LLM is already resolved by the host, or an
existing supported native profile. Do not decrypt/copy credentials to plaintext
or create a second credential store. Existing unencrypted native profiles and
credential-free profiles remain supported; rotation of supported secret values
does not change routing fingerprints. Only hashes
are retained; no raw credentials, endpoints or profile settings are copied. Configuration drift prevents execution/capture
of existing actions. If drift occurs while a task runs, its report cannot be accepted
until the frozen configuration is restored. Recovering instead discards that round
and rejects its late report. Actual runtime model identity is not supplied
by `TaskObservation`, so profile selection is not runtime model proof.

## Invocation and exact return capture

The command's source location identifies the installed package root. Always enter
through `scripts/openhands.py run`; bare `crew` outside that wrapper is not a
supported Canvas entry path and may auto-detect a different host. Read the
corresponding shared command for workflow/request semantics. Replace its
host-specific Task, scribe, bind and transcript-copy instructions with this protocol:

1. Run `python3 <plugin>/scripts/openhands.py run --backend-id <actual-backend>
   --conversation-dir <actual-persistence-dir> --workspace <actual-workspace> --
   <crew-command> <arguments>`. The wrapper selects `CREW_HOST=openhands`, sets
   `CREW_PROJECT_DIR`, and derives the session from backend, conversation and workspace.
   It supplies `--session-id` for workflow start/resume; do not provide another.
   Repeat this wrapper for every engine command; no operator exports are necessary.
2. Follow the engine-issued actions. For a native action, save its issued `ref`
   object unchanged as a JSON file. For loop reviews use the item's `review_ref`
   and owner `review`; for advisor/executor actions use owner `measure`/`build`.
   Through the wrapper, invoke `prepare --owner <owner> --ref-file <file>
   --action-id <issued-id>`. This durably reserves the invocation before claiming the action. Call the native `task` tool only when authorization is `spawn`, with
   exactly the returned `task` arguments. Do not also call the ordinary claim,
   Claude native bind/capture, or Codex collaboration tools.
3. Invoke tasks sequentially. Their schema is `prompt` and `subagent_type`; omit
   `resume`. New tasks do not inherit parent conversation history. Fresh native
   actions have no external CLI timeout. One outstanding invocation blocks further
   invocations until terminal capture or confirmed quiescence has been reconciled
   with the owner and a retirement record is durable. A source-event receipt alone
   does not release the fence.
   A proven pre-claim refusal returns `never_launched: true` and the original
   `needs_input` step or typed refusal. Resolve that owner question, then prepare
   the same intended action again. Each retry has a distinct reservation identity;
   immutable refusal proofs do not collide with later completion records. A failure
   before the refusal proof is durable remains uncertain and fenced. `wait` or a
   generic workflow error is never treated as proof that no task was launched.
   If preparation returns `do_not_spawn`, inspect/capture the existing invocation.
   A crash in the claim/intent/launch window never authorizes another launch.
4. After the native task returns, run the wrapper with `capture --owner <owner>
   --ref-file <file> --action-id <issued-id>`. It finds the exact persisted
   `ActionEvent` by the unique issued prompt, verifies role/freshness, joins its
   `ObservationEvent` or `UserRejectObservation` by action and tool-call IDs
   and ignores interleaved hooks and scaffold `AgentErrorEvent` events. Generic
   agent errors do not establish quiescence: the SDK emits synthetic interruption
   errors while tool workers may still run. Capture waits for an actual task result
   or host denial; otherwise explicit confirmed recovery is required. A later real
   result can be captured even after an interruption error. Only structured task
   text content is extracted. Each decoded string is joined in order and encoded once as UTF-8.
   CR/LF, Unicode, indentation, fences and final newlines are preserved. The model
   never retypes the return. Source event/task IDs and event/output hashes are
   retained beside the invocation; changed or duplicate conflicting events fail.
5. Execute the engine-issued `next` argv exactly as returned. Native actions,
   parent synthesis/formatting, nested loop reviews and loop decision/cancellation/
   resume commands carry their owner's frozen `--context-file`, so fresh shells
   need no inherited environment. Loop step `commands` supplies owner operations;
   `decide_template` requires the original question's `--question-id`, `--kind`
   and answer fields, appended only after its human gate.
   Parent synthesis/formatting
   actions retain their existing engine capture path and judgment rules. Debate
   has no judgment and no formatter. Honor human decision gates and terminal results.

Owner admission freezes `context.json`; every retry retains the same review-run
binding. Under the review lock, a failed start with no published `workflow.json`
can replace its orphan binding on re-admission. Once the workflow exists, the
binding remains immutable, including when no action has yet been claimed. Debate successors and nested loop reviews inherit their source owner's
validated context. Command projection may idempotently write per-attempt `ref.json`
transport metadata, but never establishes or replaces context authority. Parent
synthesis/formatter claims and new capture/direct-submission acceptance validate
both the frozen configuration and session. Rendering, confirmed recovery and exact
parent accepted-receipt replay remain available after live configuration changes. The wrapper also accepts the exact installed
`crew` executable as a leading argv element. Native bridge recovery argv deliberately omits
`--confirmation`; supply `not_running` only after actual quiescence is known.
Parent review recovery retains the existing `--confirm-not-running` flag.

For `prepare`, `capture`, and `recover`, the wrapper routes to the bridge rather
than the Crew CLI. The bridge performs no model invocation. Review and loop owner
checks reject manual native submissions that bypass the deterministic capture.
A completed task without an owned report is refused. Host-denied tasks and error
observations settle as failed actions through normal engine policy.

## Cancellation and explicit resume

For build and measure-twice, use the emitted loop-owner `cancel` command first,
then use the native host's normal stop facility for a running synchronous task
and confirm it actually stopped. Standalone review and debate have no owner-cancel
API: stop the parent dispatch loop, stop the native task through the host, and
confirm quiescence before bridge recovery. Do not run a nonexistent review/debate
cancel command. Crew has no independent task cancellation API for this SDK contract. A build cancellation retains its writer fence until actual
completion or explicit quiescence recovery. An uncertain task is still outstanding.

After actual quiescence is known, run the bridge's `recover` command with the same
owner/ref/action and `--confirmation not_running`. It first retains the quiescence decision, then reconciles engine settlement and
transport retirement. Every boundary is retryable, including a reservation whose
claim never completed. Cancelled or replaced owners are never resumed or advanced. This confirmation must
come from real host evidence or an operator; elapsed time is not evidence. Cancelled planning advisors and loop reviewers can retire after completion or
confirmed quiescence without promoting plans or advancing their owners. Do not delete
intent files to resume. Late output for a recovered action is rejected.

For standalone review/debate, recovery settles the interrupted action as failed
and retains the workflow. Its response may describe ready work; do not claim it,
call `next`/`retry`, launch another task or synthesize while stopped. There is no
automatic dispatcher: supported retained stop is quiescence plus ending the parent
protocol. Only an explicit user resume authorizes following returned work or retrying
failed seats. Recovery replay does not dispatch. If capture settled an action but
retirement was interrupted before a review retry, replay capture or confirmed
recovery with the original ref/action retires that historical invocation. It
validates retained action/receipt authority without advancing the current attempt.

Resume by repeating the same workflow request or the existing resume command in
the same backend/conversation/workspace. Engine terminal replay does no extra work.
Each owner keeps an immutable context generation. After configuration changes,
use `run --context-file <old-snapshot> -- recover ... --confirmation not_running`
to retire old invocations; recovery checks identity without requiring the old
configuration to be restored. Snapshots are the `context_path` returned by `run`
with no command, under `.crew/openhands/contexts/`. Once all invocations are retired,
use `run --new-context --backend-id ... --conversation-dir ... --workspace ...`
to admit new work explicitly. Old owner bindings remain unchanged and reject
retargeting. A plugin upgrade uses this same path. Cancel any old active workflow
before starting a different request; for standalone review/debate use the retained-stop procedure above.

Parent capture retains its issued return-file and judgment requirements. Use the
emitted loop-owner `cancel` command to end a build/planning request and the emitted
`resume` command only on an explicit resume request. Standalone review/debate follow
the retained-stop procedure above. These operations keep the frozen context.

Event capture scans the conversation log for the owned action and observation;
owner submission independently verifies it again against tampering. Reservation
admission scans retained intent history. The action and observation are found by separate bounded-memory scans, repeated
by capture validation and owner submission. These multiple passes remain linear
in retained events; intent admission is linear in retained intents. A concurrently partial event JSON file causes a typed
failure; retry after the host finishes writing it, without launching another task.
A missing matching action reports `native_launch_not_observed`; multiple matches
report `ambiguous_native_launch` and require inspection.

## Evidence levels

`test-openhands-native.py` uses disposable host-event fixtures for review, debate,
planning revision, build revision, denial, exact capture, drift, replay, and recovery.
It does not run OpenHands agents. `test-openhands-loader.py` uses the real SDK 1.51.0
loader, generated-role factories, profile serializers and tool registry in temporary
installation directories without creating conversations or calling models. Factory
and registry tests use disposable tool implementations because the cached environment
contains SDK 1.51.0 but no `openhands-tools` distribution. They establish SDK naming
and credential contracts, not runtime native tool behavior or live registry proof. Existing-host suites exercise regression behavior. Neither suite
passes the live Canvas gates. SDK loader/profile evidence is pinned to 1.51.0;
the TaskToolSet action/observation schema and completion fields were inspected
from upstream source, not a versioned `openhands-tools` distribution in that cache.
The persisted-event fixtures exercise that source contract and do not establish
the deployed backend's tool schema. The source repository's investigation evidence and
execution addendum record historical checks and the authorized sequencing correction;
those private work records are intentionally excluded from exported packages.
