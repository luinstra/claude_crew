# Phase 6: Crew inside OpenHands Agent Canvas

Status: corrected scope, 2026-10-05; implementation pending.
Delivery branch: `codex/multi-harness-engine`. Use an isolated worktree for edits
so active sessions and uncommitted files in the original checkout stay intact.

## Outcome

Install Crew through Agent Canvas's Plugins UI, enable or attach it to a new
conversation, and run review, debate, measure-twice, and build from that
conversation. Reviewers, panelists, advisors, and executors run as native
OpenHands agents using the backend's configured models, tools, and workspace.
Crew's existing Python engine retains workflow decisions, prompts, quorum,
budgets, retries, action identity, and completion.

The standalone SDK adapter introduced in `b9d21fe` did not satisfy this outcome.
Its runner, dependencies, runtime-specific engine changes, and offline evidence
are reverted. The implementation remains recoverable from Git history for
selective reuse after the Canvas integration seam is verified. Crew 0.87.0's SDK
tests are not evidence of Agent Canvas support. The Codex integration remains.

## Product boundary

- The user installs a plugin and invokes Crew inside an OpenHands conversation.
  Ordinary use must not require launching a separate Python runner, supplying a
  second model credentials file, or manually transporting agent reports.
- Use native OpenHands agents for the native workflow. Running Claude Code,
  Codex, or another provider CLI through ACP is a different capability.
- Resolve explicit seat choices against OpenHands' model/profile configuration.
  Record requested and resolved identities. Do not silently substitute models,
  translate CLI aliases into unrelated model IDs, or change existing defaults.
- Use the host's workspace and tools, including command execution needed for
  builds and tests. Declare actual access guarantees; a prompt asking for
  read-only behavior does not establish enforcement.
- Preserve Claude, Codex, Cursor, and existing external routes. Keep Crew's
  Python 3.11 stdlib core independent of optional host libraries.

The first supported target is Agent Canvas with a local backend. Record the
exact Canvas/backend versions exercised. Remote and cloud support need their
own observed installation and execution gates. A separate Canvas dashboard App,
new orchestration framework, general runtime registry, and rebranding are out
of scope.

## Verified host contracts and open questions

Primary documentation checked 2026-10-05:

- [Plugins in Agent Canvas](https://docs.openhands.dev/openhands/usage/agent-canvas/plugins)
  describes backend-scoped installation from source, enablement, and attaching
  plugins to conversations. Test that full path, including command discovery.
- [Plugin format](https://docs.openhands.dev/overview/plugins) describes bundled
  commands, skills, agents, hooks, and MCP configuration. Verify the chosen
  format with the actual backend loader before changing shared packaging.
- [Task Tool Set](https://docs.openhands.dev/sdk/guides/task-tool-set) documents
  sequential blocking delegation and task-ID resume. That is not evidence of
  concurrent panels or an arbitrary per-call model argument. Inspect the host's
  available delegation and model-profile interfaces before selecting transport.

Resolve plugin-root expansion, project/session identity, native role loading,
model/profile resolution, result capture, lifecycle events, and cancellation
against the pinned host. Existing Claude command syntax and hook environment
must not be assumed compatible merely because the manifest loads.

## Implementation sequence

1. **Install and run one native action.** Package Crew so Canvas can install it
   from the delivery branch. Enable/attach it in a new native OpenHands
   conversation, discover a Crew command, and execute one engine-issued review
   action with the actual host's model and tools. Capture its exact return and
   bind its task/conversation identifier to the Crew action. Use this vertical
   slice to choose the smallest transport seam before expanding engine routes.
2. **Review and debate.** Drive engine-issued native roles from the conversation;
   keep prompts and policy in the engine. Resolve multiple configured models,
   capture raw reports without a scribe, handle independent failures, and prove
   real reviewer overlap before claiming concurrency. If the host only offers
   sequential delegation, report the limitation and resolve it explicitly.
3. **Measure-twice.** Add the native advisor and plan promotion through existing
   actions. Preserve budgets, user questions, explicit resume, and receipt
   replay. Reconcile interrupted actions without launching duplicate work.
4. **Build.** Add the executor using host editing and command tools. Exercise
   implementation, tests, review, and revision. Resume only an authorized
   executor conversation when supported; reviewers stay fresh. Cancellation
   must settle owned writers before review or replacement execution proceeds.
5. **Installation guide and evidence.** Document the exact tested source/ref,
   supported backend versions, model/profile setup, commands, and limits. Keep
   support claims tied to observed Canvas behavior.

Prefer deterministic transport for mechanical binding and capture. Do not add
bookkeeping agents or a second workflow-completion loop. Review the initial
packaging/transport design once; use focused checks for subsequent slices.

## Acceptance and regression gates

- Install Crew from source in Canvas, inspect its contents, enable/attach it,
  and invoke the discovered workflows in a new native OpenHands conversation.
- Complete review, multi-round debate, a plan revision, and a disposable
  implementation/test/review/revision build through the same Crew engine.
- Record source and host versions, resolved models, native task/action IDs,
  exact reports, tool/access behavior, and concurrent reviewer timing.
- Exercise partial failure, malformed returns, duplicate/late results, human
  waits, interruption, explicit resume, and cancellation while work is active.
  Keep uncertain writers fenced until their termination is established.
- Run affected engine, host, provider, Codex, and lifecycle regression suites
  using disposable workspaces and isolated configuration.

Offline loader and adapter tests support these gates but cannot replace the
Canvas installation and in-conversation run. Missing access to a suitable
Canvas backend leaves the live gate pending; it must never be replaced with a
standalone SDK run and reported as complete.
