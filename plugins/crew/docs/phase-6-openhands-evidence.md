# OpenHands adapter evidence

Recorded 2026-10-03 in the isolated `codex/native-integration` worktree, based on
Crew `3b98e45` (0.86.0). The adapter changes are an initial file-tool implementation
increment. This is offline SDK/engine evidence, not live provider verification
or full Phase 6 completion.

## Runtime and engine results

Pinned `openhands-sdk==1.51.0`, Python 3.12.13, in a separate locked uv project.
The core remains Python 3.11+ and stdlib-only. Tests exercise real SDK agents,
asynchronous conversations, tool dispatch, persistence and Crew transitions;
only provider responses are fixtures. The five original SDK probes deny network
connections as well. Fixture models are not an operator configuration.

Verification completed on this worktree:

| Suite | Result |
| --- | --- |
| Optional SDK/runtime/bridge/runner | 28 passed |
| Review workflow | 201 passed |
| Measure-twice | 131 passed |
| Build workflow | 89 passed |
| Codex native | 43 passed |
| Hook assertions | 674 passed |
| Multiagent/routing assertions | 1,990 passed |

Ruff checks pass for the new adapter and shared binding modules; changed core
files parse with the Python 3.11 grammar; `git diff --check` and the frozen uv
lockfile check pass. Personal model overrides exposed two existing registry-test
leaks; the registry test now uses the existing isolated-config helper.
Local logs are retained under `.crew/openhands-offline-evidence-20261003/`.

The combined adapter suite covers:

- Barrier-proven overlap of two distinct native model conversations, with exact
  finish text including whitespace. No dispatch/scribe model call is used.
- Native review and synthesis through engine approval; a two-round debate with
  null judgment; advisor staging and plan approval; file-writing build approval;
  and a review/revise/review build with new owned executor and reviewer actions.
- One provider failure leaves the other seat running and preserves Crew quorum.
- Missing SDK routes fail before claims. Empty finish text fails. Duplicate or
  foreign handles, prompt drift and unsettled replay are refused.
- Completed review results can be captured after runtime restart without new
  reviewer calls. The command entry point and explicit terminal resume are tested.
- Read-only tools, exact advisor staging, nested executor file writes, denied
  traversal/symlink/hardlink/metadata writes and capability revocation.
- Cancellation during a model wait and during an active file write. The latter
  remains pending until that write finishes; Crew clears its writer fence only
  after the capability is revoked and completion is captured.
- Initialization refusal settles an unlaunched claim. Uncertain results keep
  ownership and fences rather than triggering a duplicate launch.

Run from `plugins/crew/adapters/openhands`:

```sh
uv run --frozen python -m unittest test_sdk_contract test_runtime test_bridge test_runner
```

The original five SDK probes showed that SDK interruption alone does not join a
synchronous tool thread: the fixture wrote after `arun()` stopped. The implemented
capability lock and revocation are the additional boundary tested above.

## Ambient state and model identity

The pinned `CrewConversation` subclass disables automatic ambient plugin loading.
User/project/public skills and memory are disabled; the actual tool map is checked
before model execution. Tests assert that the ambient discovery function is not
called. The runner sets `OH_PERSISTENCE_DIR` before SDK imports, isolates profiles
and persists each action under an owned directory. Shared role instructions and
issued prompt text participate in the SDK action fingerprint.

Every model route has an explicit provider/model ID and environment-key reference.
Model fallback is refused. Authentication is not imported from provider CLIs.
Action receipts bind owner, action ID, requested model/configuration and prompt
hash to the conversation handle. This proves configured identity, not that a
remote provider honored that identity; the latter needs the live gate.

## Limits and remaining evidence

No live provider call has run. The operator's SDK models and authentication route
remain pending. There is no performance/speedup claim against another harness;
the overlap barrier demonstrates concurrency, and control flow makes no model
calls for dispatch or receipt bookkeeping. Live token/cost metrics are pending.

The executor has confined file tools, not shell/test commands. It must return
BLOCKED if required verification needs unavailable tools. Revisions currently
use fresh executor conversations. Sandboxed command execution, executor-only
continuation, remote Agent Server, product UI and automatic resume remain unclaimed.

The preceding Codex reload gate is recorded in
[Codex evidence](phase-7-codex-evidence.md); it used the installed 0.86.0 plugin,
not the subsequent OpenHands adapter changes.

## Sources

- [SDK package source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/pyproject.toml)
- [Async conversations](https://docs.openhands.dev/sdk/guides/convo-async)
- [Custom tools](https://docs.openhands.dev/sdk/guides/custom-tools)
- [Pause and resume](https://docs.openhands.dev/sdk/guides/convo-pause-and-resume)

Behavior was also checked against installed 1.51.0 source in
`conversation/impl/local_conversation.py`, `agent/parallel_executor.py`,
`tool/builtins/finish.py` and `utils/path.py`.
