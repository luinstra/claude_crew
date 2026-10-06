# Agent Canvas implementation evidence

Recorded on 2026-10-05 for session `01a10d6c-3b01-7181-b9e1-52cc7503b666`.

**Current status: packaging and native transport implemented and fixture-tested;
live Canvas installation and workflow validation pending.** The initial spike
below is historical and superseded by the implementation and revision updates.
It does not certify Canvas compatibility.

## Implementation baseline

The canonical workspace is
`/Users/luinstra/.codex/worktrees/codex-integration/claude_crew`.
The local branch remains `codex/native-integration` at
`c881fefca93b3a02ae40f5a2de1579c833ea3841` (Crew 0.87.2).
Both `d56364f` and `c881fef` are ancestors of HEAD. The requested delivery branch
remains `codex/multi-harness-engine`; remote delivery was not independently
checked, and no push was attempted.

The immutable inputs read in this attempt are:

| Input | SHA-256 |
| --- | --- |
| `.crew/plans/crew-requests-agent-canvas-fresh-requirements-md-09b0ef60-29b1-4133-a616-feb9de0b528b/plan-1.md` | `9ae678deb64931ff788278163663e68da54630706cea02212f8a7143ffbe05c2` |
| `.crew/reviews/01a0f9e3-b687-7e63-b9e5-d1e8a07774b5/run-ad4be5a3ba10/attempts/attempt-0001/ingress/synthesis/fd25e08a66a4c3067ca906ddc1da23bb0db19128e4fef61646795614794ae23b.md` | `5837e8a4641da5b30e45eaeaaf5a1828d6fba971b2a2ca14e16948818a54088b` |

Initial `git status --short --untracked-files=all`, `git diff --exit-code`, and
`git diff --cached --exit-code` were clean. Existing ignored `.crew` artifacts
and cached SDK material were preserved. No staging, commit, branch switch,
reset, clean, or publishing operation was performed.

## Observed access and runtime

| Contract | Observation |
| --- | --- |
| Canvas UI access | Two computer-use inventories failed because the Mac was locked. Neither exposed apps or browser tabs. |
| Operator / target | Mac unlock and the intended Canvas URL/app and backend were requested; no answer was available at the checkpoint. |
| Default local endpoint | `curl --head --silent --show-error --connect-timeout 3 --max-time 5 http://127.0.0.1:8000/` failed with exit 7, connection refused. This says nothing about a remote backend or another port. |
| Canvas version | Unobserved. |
| Active backend identity, version, source revision | Unobserved. |
| Native OpenHands agent / actual tool schema | Unobserved. No ACP agent was substituted. |
| Existing LLM / agent profile identifiers | Unobserved. No credential files were read or new provider settings created. |
| Backend workspace path and host-to-workspace mapping | Unobserved. Only the local canonical path was verified. |
| Local engine prerequisites | `python3 --version` returned Python 3.14.3; importing `fcntl` succeeded. These are local facts, not a backend sandbox proof. |
| Shared project resolver | `./plugins/crew/crew project-root` and `state_discovery.crew_base()` both returned the canonical workspace. Future integration must consume this resolver. |
| Installation source / installed package revision | None issued or installed. |
| Native task / event / exact result interface | Unobserved. No native action or capture was run. |

The exact repeated computer-use diagnostic was:

```text
The Mac is locked and automatic unlock could not unlock it. Ask the user to unlock the Mac manually before continuing.
```

The [official Canvas plugin guide](https://docs.openhands.dev/openhands/usage/agent-canvas/plugins)
documents backend-scoped plugin management and notes that cloud backend actions
may be unavailable. The [Canvas installation guide](https://docs.openhands.dev/openhands/usage/agent-canvas/setup)
documents port 8000 as the default local ingress. Neither identifies the user's
active deployment. No upgrade requirement can be established without that
deployment's version and capabilities.

## Packaging finding from cached source inspection

An existing cached `openhands-sdk` 1.51.0 distribution was inspected read-only.
It is **not evidence of the deployed Canvas/backend version**. Its source root is
`/private/tmp/crew-openhands-uv-cache/archive-v0/7sBn_gUu1UiGt_FxOy3GS`.

| Module under `openhands/sdk/plugin/format/` | SHA-256 |
| --- | --- |
| `claude_code.py` | `0a5ffe63edad641fd96c772e1cbb7ff3c5d1e05de610f7be655641f224be4358` |
| `base.py` | `e17a8f3036874e47d51fd1139748eddf3792bff6f9fa6af25963e0922af6ee4c` |

In this distribution, `ClaudeCodePluginFormat.load_manifest` checks `.plugin`
before `.claude-plugin` (`PLUGIN_MANIFEST_DIRS`, line 40). However,
`load_hooks`, `load_agents`, and `load_commands` (lines 152, 156, 160) discover
`hooks/hooks.json`, `agents/`, and `commands/` directly. The shared
`PluginFormat.load_skills` discovers `skills/` (`base.py`, line 107).

Consequently, adding `.plugin/plugin.json` to the existing Crew root alone would
not isolate its Claude hooks, commands, or agent metadata in this loader.
Manifest precedence does not establish host-specific component selection.
This is a structural packaging risk to resolve against the actual deployed
loader before installation. A package must include only validated OpenHands
entry points/roles and the shared engine, with no out-of-package dependencies,
credentials, adapter virtualenv, or unrelated ignored artifacts. The accepted
refinement prefers direct host-specific packaging when supported; a deterministic
export remains an option only after that support is inspected. No package layout
has been frozen and no loader compatibility test was claimed.

## Existing build writer (historical; resolved)

At the initial spike, the previous owner `01a0f9e3-b687-7e63-b9e5-d1e8a07774b5`, loop
`ec99930b-40af-4bc0-b0fd-2c5b93cb4971`, retains `action-0001` as a claimed
outstanding writer with no bound handle or result. A missing handle is not proof
that it never launched. Explicit operator confirmation of actual quiescence was
requested as required by the session recovery instruction. No recovery,
cancellation, adoption, or state edit was performed.

## Initial gate ledger and bounded continuation (historical)

| Gate | Status | Evidence / missing prerequisite |
| --- | --- | --- |
| G1 installation and discovery | BLOCKED | Local baseline verified; actual Canvas/backend access and version unavailable. No installation or discovery proof. |
| G2 one native review and exact capture | NOT RUN | Requires G1 and the actual native task/event/result schema. |
| G3 complete roles, review, debate and model attribution | NOT RUN | Requires G2's observed contract checkpoint. |
| G4 measure-twice and build | NOT RUN | Requires native transport and prior gates. |
| G5 interruption, cancellation and explicit resume | NOT RUN | Requires owned native task identities and observed quiescence evidence. |
| G6 clean-install replay and regressions | NOT RUN | No candidate package or shared-engine change to validate. |

This attempt used two UI access checks and one default-port check, made zero
package revisions, and launched zero native or standalone SDK conversations.
The required runtime, packaging, and workflow suites were not run; the local
dispatcher/import checks above do not substitute for them.

Resume with an unlocked Mac and the intended configured Canvas backend. Record
its exact versions/source, operator, native agent, profiles, sandbox Python/fcntl,
workspace mapping, plugin loader/routes, and conversation event interface before
making the minimal package. Retain the plan's bound of at most two evidence-driven
package revisions. Clear the previous writer only after explicit operator
confirmation, or let its owner finish it normally.

After G1, prove one owned native action and exact UTF-8 capture before expanding.
At the G2 checkpoint, freeze the initial native roster, duplicate-profile policy,
requested/observed attribution tier, and sequential scheduling/deadline policy;
test that global Crew settings cannot inject external seats. Later work must
preserve debate's null judgment/no-formatter contract, prove planning revision,
and use concrete build and interruption fixtures. Dependent slices remain gated;
no Canvas support or successful installation is asserted.

## Implementation update, 2026-10-05

The earlier instruction to wait for an unlocked Mac before implementing is
superseded by the user's explicit sequencing correction, recorded separately in
`openhands-execution-addendum.md`. The old executor completed; its cancelled owner
has `writer_fence: null`. The preceding outstanding-writer warning is historical.

Implemented portable root `plugin.json`, `dev.openhands` agents/commands, the native
host channel, native-only review/debate/default executor selection, shared loop
routing, exact persisted-event capture, bounded sequential intent, explicit
quiescence recovery, generated profile variants, and package export/version sync.
No source changes were staged, committed or pushed by this executor.

Source contract: cached distribution `openhands-sdk==1.51.0` inspected directly.
Relevant symbols: `AgentPluginsFormat.load_agents/load_commands/load_hooks`,
`Plugin.load`, `register_plugin_agents`, `agent_definition_to_factory`,
`ConversationState._save_base_state`, `EventLog`, and `Observation.text`.
Official current Task schema and execution were read at:

- https://raw.githubusercontent.com/OpenHands/software-agent-sdk/main/openhands-tools/openhands/tools/task/definition.py
- https://raw.githubusercontent.com/OpenHands/software-agent-sdk/main/openhands-tools/openhands/tools/task/impl.py
- https://docs.openhands.dev/sdk/guides/plugins
- https://docs.openhands.dev/sdk/guides/task-tool-set

The SDK Task observation serializes `content`; `.text` concatenates text blocks.
TaskAction has no per-call model argument: generated native definitions reference
existing LLM profiles. Plugin role registration is first-wins. The portable loader
isolates OpenHands components and takes precedence over compatibility manifests.

The real 1.51.0 loader test loads eight agents and six commands, selects
`/crew:review`, and finds no hooks/MCP configuration. It installs an exported
package in a disposable directory, disables/re-enables it, reloads it, and invokes
its copied engine. Named-profile export loads 24 roles for inherit plus two profile
references without reading/copying credentials. These are loader results, not
Canvas installation or native model execution.

The persisted-event bridge supports only a backend where its actual native
conversation files are readable in the tool workspace and its recorded workspace
path matches. Remote server files are not automatically sandbox files. There is
no authenticated HTTP event bridge yet, and this implementation does not widen
mounts or permissions. The active registry cannot be proven from base_state.json;
registration collision verification remains a required live host check.

Live G1-G5 remain pending: actual Canvas/backend version, installed discovery and
reopen behavior, reachable event-store mapping, active role registry, effective
profiles/models and security policy, all native role probes, four workflows and
real cancellation. No native or standalone SDK conversations were created during
this implementation. See `openhands-transport.md` for concrete prerequisites and
commands; missing backend capabilities fail explicitly.

Historical initial verification (superseded by the correction results below):
14 OpenHands-native tests and 3 SDK-loader
tests pass. Existing suites pass: multiagent 1,990 checks, hooks 674, review 201,
measure-twice 131, build 89, Codex-native 43, version bump 63, project root 6.
The registry test now isolates personal config; native-only provider kinds are
excluded from external dispatch parity assertions. No provider was invoked.
New Python files pass Ruff, role-generation parity and diff whitespace checks.

## Revision update, 2026-10-05

The retained review's lifecycle findings shared a cross-file transaction defect.
Reservations now precede claims. Capture evidence is distinct from retirement;
only retirement releases the conversation fence. Quiescence is written before
owner settlement and both writes reconcile idempotently. Cancelled advisors and
loop reviewers retire without advancing, including after replacement by new work.
The engine's reserved-action recovery option is used only by this bridge after
validating its immutable reservation; public recovery commands retain their usual
claimed-action contract. Build writer fencing remains owned by the build engine.

Context generations preserve the old owner binding while explicit `--new-context`
admits later work after transport retirement. Effective agent/LLM/tool/security
settings are fingerprinted, including same-model effort, provider routing and task
parameters. Only hashes are persisted, not raw endpoints or credentials. Recovery
can use its old snapshot during drift; execution/capture cannot retarget it.
Hooks are ignored during terminal-event selection; scaffold errors correlate by
tool-call identity. Exact output bytes still include CR/LF and Unicode.

### Role-provenance finding: narrower trusted-host contract

The ordinary stale-definition cause is fixed with complete generated-content
SHA-256 role names, verified by the wrapper. Real SDK tests register old legacy
names, unchanged current names and a revised role together: revised content gets
a distinct registration, unknown names refuse, and no generic-role fallback occurs.

The stronger demand to prove the running callable from terminal-side package or
state files is not claimed. SDK 1.51.0 `subagent/registry.py` documents
`factory_func` as authoritative, `register_agent_if_absent` as first-wins, and
`get_agent_factory` as the in-process inspection mechanism. TaskManager calls
that factory and copies the parent's LLM configuration. An exact-name preexisting
impostor still wins; the real SDK fixture deliberately demonstrates it. Even an
identical `AgentDefinition` would not prove an arbitrary callable. A fresh-process
registry or the file/builtin `/sub-agents` discovery endpoint is not proof of the
conversation's registry. Crew trusts the native host to load/honor the selected
plugin, as its native transports do; this is an explicit narrower-contract rebuttal
for review, not a claim that the stronger runtime-provenance requirement passed.
No route is represented as runtime-attested. Actual installation, role behavior,
models and confirmation/security policy remain live gates.

Sources: SDK 1.51.0 `openhands/sdk/subagent/registry.py`,
`openhands/sdk/conversation/state.py`, `openhands/sdk/hooks/executor.py`,
`openhands/sdk/event/llm_convertible/observation.py`, and current official
[TaskManager source](https://raw.githubusercontent.com/OpenHands/software-agent-sdk/main/openhands-tools/openhands/tools/task/manager.py).
The local backend contract and exact input provenance are documented in
`openhands-transport.md`; there is no inferred Canvas path or remote HTTP bridge.

Revision fixture coverage: 31 native tests, including failure injection before/after
claim, before quiescence receipt, after owner recovery, after source retention and
after capture for review/measure/build; cancellation, replacement, subsequent work,
hooks, scaffold errors, exact bytes, effort refusal, named-profile review and drift.
All remain fixture/source evidence, never a live Canvas gate.

Historical first-review revision verification: native fixtures 31; real SDK loader/registration tests
6; review 201; measure-twice 131; build 89; Codex-native 43; multiagent 1,990;
hooks 674; version bump 63; project root 6. All passed. Generated role parity,
Ruff for new Python modules/tests, Git whitespace checks and `claude plugin
validate plugins/crew` also passed. Regression logs are retained locally at
`/private/tmp/crew-oh-revision-checks/`. No files were staged or committed.


## Interruption and effective-routing correction, 2026-10-05

This update supersedes the first revision's acceptance of scaffold errors as
terminal. The inspected SDK 1.51.0 `LocalConversation._emit_orphaned_action_errors`
adds synthetic `AgentErrorEvent` records after interruption to repair provider
message history. Its `arun` cleanup explicitly says interrupted tool workers may
outlive that call. Generic agent errors now keep the invocation outstanding.
Capture accepts a later actual `TaskObservation` or a host-denial event, while
explicit `not_running` recovery still requires real quiescence evidence.
Both active and cancelled build fixtures retain the transport reservation and
writer fence after an interruption error, then retire only after completion or
confirmed recovery. Hooks remain excluded; UTF-8/CRLF byte capture is unchanged.

Official source contracts used (cached SDK distribution version 1.51.0):

- `openhands/sdk/conversation/impl/local_conversation.py`, `arun` and
  `_emit_orphaned_action_errors`: [upstream source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py).
- `openhands/sdk/llm/llm_profile_store.py`, `LLMProfileStore.__init__`, `load`,
  `_resolve_provider_connection`: [upstream source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/llm/llm_profile_store.py).
- `openhands/sdk/llm/provider_connection_store.py`, `ProviderConnection`,
  `PersistedProviderConnections`, `ProviderConnectionStore._read`:
  [upstream source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/llm/provider_connection_store.py).
- `openhands/sdk/llm/llm.py`, `LLM_SECRET_FIELDS`, and SDK `Cipher` serialization:
  [upstream source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/llm/llm.py).

Named-profile fingerprints now cover effective nonsecret settings after applying
the referenced sibling provider connection's endpoint, including a null override.
The bridge rejects missing or malformed connections; credentials and update times
do not become routing identity. A real SDK test creates a disposable provider
connection and linked profile, loads the resolved endpoint through `LLMProfileStore`,
proves encrypted saves change bytes without changing Crew's fingerprint, then
proves connection-only endpoint changes alter both the SDK route and Crew's hash.
No model or conversation is started by this test. Secret-field exclusion is
checked against SDK `LLM_SECRET_FIELDS`, including AWS access ID and session token.

Recovery now renders the owner's frozen metadata without live revalidation;
`prepare` and `capture` still validate current routing before execution/acceptance.
A multi-seat fixture proves the first recovery call reports successful settlement
under endpoint drift, retires the old invocation, admits an explicit new context,
and refuses retargeting remaining old-owner work. Issued native actions now expose
executable wrapper `prepare`/`capture`/`recover` commands and retained schema-bearing
ref files, without advertising an ordinary claim bypass.

The backend remains a trusted native host. Task delegation approval can authorize
child actions under SDK semantics; policy copying does not guarantee individual
child confirmation prompts or identical analyzer inheritance. Lowercase tool
registration names are an intentionally narrower admission contract. No runtime
registry attestation or stronger host approval promise is added.

The wrapper refuses split and equals-form session overrides and unsupported panel
options before the target separator. Malformed object/list evidence returns typed
errors. Known other-session intent damage is isolated using the frozen owner
context; own or unknown corrupt ownership remains fail-closed. Event scans stream
one JSON object at a time and retain at most two matching candidates, preserving
conflict detection without an index. The unsupported-Python visible no-op follows
the existing repository convention. Exported docs use a runtime-guide allowlist;
this evidence file and the execution addendum stay outside the package.

Historical second-review correction verification: 42 native fixtures, 7 SDK loader/configuration
tests, 201 review, 131 measure-twice, 89 build, 43 Codex-native, 674 hook,
1,990 multiagent, 63 version-bump and 6 project-root checks pass (3,246 total).
Ruff, generated role parity and diff whitespace checks pass. Detailed outputs are
retained as `/private/tmp/crew-oh-action3-*.log`. The branch and baseline remain
unchanged; all changes are unstaged and uncommitted.
Live Canvas installation, model execution, remote event access and G1-G5 remain
pending and are not established by these fixtures or SDK contracts.


## Owner command and claim-refusal correction, 2026-10-05

Both retained blocking findings are corrected in the isolated worktree at unchanged
HEAD `c881fefca93b3a02ae40f5a2de1579c833ea3841` on `codex/native-integration`.
No source changes were staged or committed.

Every emitted OpenHands work-item continuation now reloads its frozen context,
including parent synthesis/formatting, nested loop review and synthesis-only retry
attempts with no native work. Loop steps also expose context-bound next, resume,
cancel and decision commands. The wrapper accepts the installed Crew executable
as the leading command element. Outstanding-writer recovery goes through the
native bridge, and its emitted argv never pre-fills quiescence confirmation.
Source projection writes only idempotent transport bindings, never claims work.

Reservation still precedes claim. The owners explicitly identify refusals before
claim mutation. A durable refusal proof retains the original claim response and
reservation; a new reservation identity permits retrying the same intended action
without colliding with completion or recovery records. Real workspace-guard
refusals return their original needs-input question. Generic workflow errors,
wait responses, claimed owners and failures before refusal persistence remain
fenced. A durable never-launched refusal is not recovered as a lost task and
cannot overwrite the original question. Settlement and retirement retain their
separate retryable write boundaries.

Fresh subprocess tests run the actual emitted argv from a different working
directory with CREW_HOST, CREW_OPENHANDS_CONTEXT and project-root variables removed.
They cover standalone review and debate, advisor/executor-to-review transitions,
parent synthesis, synthesis-only retries, resume/cancel, bound workspace decisions,
and outstanding-writer recovery. A disposable Git fixture moves HEAD between
issuance and preparation, preserves the workspace_guard question, restores the
baseline, resolves the guard, then prepares and captures the same action.
Additional fixtures exercise refusal persistence before/after write, typed advisor
admission refusals, generic failures/wait responses and a post-claim refusal tag.
These fixtures do not launch an OpenHands agent or pass a live Canvas gate.

Verification: 51 native fixtures, 7 real SDK 1.51.0 loader/configuration tests,
201 review, 131 measure-twice, 89 build, 43 Codex-native, 674 hook, 1,990 multiagent,
63 version-bump and 6 project-root checks passed (3,255 total). The review and
Codex suites were rerun after the parent-only retry correction. Native fixtures
were rerun after the last cancellation-command fix. Logs for the repository suites
are `/private/tmp/crew-oh-action4-*.log`; the SDK result is retained in the executor
tool output. Ruff lint/format, generated-role parity and Git whitespace checks pass.

Planning host admission, user/developer guides, cancellation command prose and
operator implementation status now reflect the implemented transport. Role source
reads explicitly use UTF-8; Codex signatures still require reasoning_effort.
The guide documents configuration-drift report rejection, linear event/intent
scan cost, partial-event retry, and distinct missing/duplicate invocation errors.
SDK loader/profile contracts are versioned evidence; the TaskToolSet schema
remains inspected upstream-source evidence. Live Canvas installation and G1-G5
validation remain pending, with no standalone runner or weakened host controls.


## Retained context lifetime and parent boundaries, 2026-10-05

This correction retains one context binding for a review run across native and
synthesis-only retry attempts. Separate per-attempt action/ref files remain
immutable. Initial admission establishes the binding before projection; new
context generations cannot retarget retained owners. New debate successors and
nested loop reviews inherit validated source context. Parent formatter/synthesis
claims and acceptance through both capture and direct submit check frozen
configuration and backend/conversation identity before accepting new work.
Confirmed recovery and rendering remain available after drift, as does exact
parent receipt replay. Nested retry decisions validate before changing owner
state, preserving the unanswered question and prior authorization on refusal.

Tests use disposable workspaces and SDK-shaped local events/configuration files.
They cover unchanged retries, model/profile/provider connection drift, backend and
conversation mismatch, synthesis/formatter claim/capture/direct submit, nested
loop retries, missing-fcntl import/serialization and unsupported-Python exits.
They do not prove a live Canvas session. Final verification counts follow below.

The shipped GitHub package still has only the inherited-profile route: its default
review panel contains one seat using the parent's configured model. This is
single-model review, not verified cross-model review. Named profile/multi-model
use requires a local package export using the backend's existing absolute profile
store path. G1-G5 still require observed Canvas installation, agent/profile/tool
selection, event mapping and end-to-end execution; no live gate passes here.

Correction verification: 65 native persisted-event fixtures; 7 real SDK 1.51.0
loader contracts; 201 review, 131 measure-twice, 89 build, 43 Codex-native,
674 hook and 1,990 multiagent checks passed (3,200 checks). Targeted build and
measure retry checks passed again after the nested authorization correction.
Generated-role parity, Ruff and whitespace checks pass. These are local contract
and regression results; no Canvas installation, live workflow, multi-model
execution or runtime security-policy verification is claimed.


## Retained panel correction: factory, retry and admission evidence

2026-10-05. The earlier unsupported-Python "visible no-op" note is superseded:
`openhands.py` and `openhands-package.py` exit 2 below Python 3.11. Hook behavior
is unchanged. The earlier lowercase-only tool admission and encrypted-profile
rotation claims are also superseded by the current transport guide.

Inspected cached SDK 1.51.0 `subagent/registry.py:agent_definition_to_factory`
calls `store.load(profile_name)` without a cipher. Actual generated-role factory
tests confirm encrypted profile fields and linked provider credentials remain
ciphertext. Admission now refuses those named routes before work is created or
claimed; inherited LLM identity and existing supported named routes are preserved.
`llm/llm.py:LLM_SECRET_FIELDS` and `utils/cipher.py:FERNET_TOKEN_PREFIX` are
asserted against the runtime SDK. Only disposable test credentials are used.

SDK `Tool`/`Agent` serialization and real registry tests cover lowercase names
and `TerminalTool`, `FileEditorTool`, `TaskToolSet`. Factory tests use disposable
tool implementations; no `openhands-tools` distribution is installed in that cache.
The official [preset source](https://raw.githubusercontent.com/OpenHands/software-agent-sdk/main/openhands-tools/openhands/tools/preset/default.py)
uses `TerminalTool.name`, `FileEditorTool.name`, and `TaskToolSet.name`; the cached
SDK's default-name constants remain the generated-role metadata contract. This
does not attest a live backend's registry or task schema. Pin TaskToolSet to the
deployed release during live validation.

Failure injection covers settlement -> failed retirement -> retry -> historical
capture or confirmed recovery -> new dispatch, with byte-identical current owner
state and forged/stale reference rejection. Orphan-context admission is retried
under the review lock until workflow publication; published owners stay immutable.
Standalone review/debate retained stop is tested through confirmed quiescence
recovery, rejection of late results, and no implicit claim/dispatch.

Live Canvas installation, remote event-read access and G1-G5 remain pending.

Executor verification for this correction: 71 distinct native fixtures, 9 SDK
1.51.0 loader/factory contracts, 201 review tests, 131 planning tests, 89 build
tests, 43 Codex-native tests, 674 hook checks and 1,990 shared-engine checks passed
(3,208 distinct checks). The native suite ran 70 tests, then the additional
retirement-reason test passed separately; the final SDK supported-credential
addition passed in a focused factory rerun after its 9-test suite. Ruff, Python
error checks, generated-role parity and diff whitespace checks passed. SDK cost
metadata was forced to its local bundled map for the final contract runs.


## Implementation review completion, 2026-10-05

The engine-owned build ended approved with no writer fence. Final review run
`run-1ec92435b9c9` reviewed snapshot
`444b8d31abb9f35a5784fc85b6cf9203eb4f9eab6f2d5fe9d5e2fc4ff92b49e2`.
Six seats explicitly approved, with no blocking findings. Grok's joined header
was not counted by the digest parser; five parsed approvals still exceeded quorum
four. Fable supplied only a progress note and was not counted as approval.
Current executor verification is 3,208 distinct passing checks; earlier counts
above describe historical runs with different suite selections.

Minor follow-ups retained from review: normalize or reject `.json`-suffixed profile
references (use extensionless SDK profile IDs today); scope optional profile
admission to selected routes; measure event/intent scan and retained-artifact costs;
and validate exact prompt copying and effective-agent fingerprint stability in
live Canvas. The shipped inherited-only package does not require named profiles.
All exported named routes must currently be usable, even if not selected.
Live G1-G5 remain pending, with POSIX locking and readable backend persistence
required. This review does not certify live Canvas installation or remote access.
