# Finish Codex integration

Status: source implementation and live native gates complete; final review closed
with minor follow-ups on 2026-10-03. Installed discovery and automatic hook re-entry passed the separate gate. Baseline `6b16ba3` on
`codex/multi-harness-engine`. The operator scheduled this before OpenHands.

## Scope

Connect Codex's native agent tools to the existing review, debate, measure-twice,
and build protocols. Preserve the working external CLI routes and existing
configuration. This is adapter completion: no new workflow engine, migration,
legacy support, capability registry, or restart subsystem.

The installed 0.83.1 plugin successfully orchestrated Phase 5 here using external
seats. Source now owns all four workflows but lacks a Codex native role row,
production advisor admission, a native build route, and discoverable core skills.
Existing Stop-hook evidence is useful; it does not certify the new engine's
installed lifecycle. The configured Sol executor remains the implementation route.

## Adapter contract

- Reuse issued prompts, frozen models/routes, claims, return capture, receipts,
  owner locks, workspace guards, and human decisions. Python retains all policy.
- Native reviewers and panelists use fresh Codex subagents without parent history.
  Spend the issued model and reasoning setting exactly. Never substitute a model
  or silently fall back to a CLI. Preserve explicit force-external selection.
- Codex tools inherit session permissions. Stamp native read roles as advisory
  unless an actually enforced per-role boundary is demonstrated. Do not claim
  that a read-only prompt changes the sandbox. Model attribution stays requested-only.
- Use the existing host-return capture path for exact final replies, without a
  scribe. This is host-written capture, not deterministic extraction from a
  host-owned output artifact: the current collaboration tools expose no such
  artifact. Do not invent a transcript parser or claim automatic byte extraction.
  An observed final reply is completion; a status string or an interrupt
  request alone is not proof that an owned writer has stopped. Bind actual native
  handles to actions and preserve existing uncertain-writer fences on cancellation.
- Admit the advisor through the existing HostRoles seam and use its issued plan
  staging path. Admit the native build executor at the existing workspace-guard
  boundary. Native executor continuation may be enabled only with a bound exact
  handle and observed safe settlement; otherwise explicitly advertise fresh native
  rounds while preserving existing external executor continuation.
- Keep `task_native_channel`'s existing external-executor selection behavior;
  broadening that table would make the configured Sol executor resolve native
  and be rejected. Native review uses its separate, existing per-seat seam.
- Provide thin discoverable Codex skills for review, debate, measure-twice, and
  build. Keep common command recipes and role prompts authoritative. Codex-only
  transport instructions map spawn/wait/interrupt/return mechanics; they do not
  replicate workflow transitions or verdict policy.
- Keep installed old orchestration separate from candidate sessions. Exercise
  candidate state only in disposable roots with unique session IDs.

## Work and acceptance

1. Complete the narrow native routing, role, launch, and result/handle seams.
   Preserve existing external build selection and Claude/Cursor behavior. Add
   focused tests for model/effort identity, force-external, fresh review contexts,
   wrong/duplicate/late returns, interruption, and uncertain writers.
2. Wire the four command skills and explicit resume. Keep automatic lifecycle
   support limited to observed hook behavior. Use the actual tools exposed by
   the host; refuse unsupported shapes rather than inventing a tool interface.
3. Run affected review, planning, build, shared routing, and hook tests with
   isolated configuration. Test the public workflow boundary, not duplicate
   internal policy. Verify Python 3.11 compatibility and stdlib-only imports.
4. Run a bounded real Codex-native disposable gate: review/debate, plan staging
   and promotion, implementation/review/revision, explicit resume, and cancellation
   fencing. Retain source version, exact models, action IDs, actual handles, and
   returned bytes. Separate demonstrated behavior from synthetic coverage.
5. Record results in the host contract and narrow Phase 7's roadmap wording.
   Verify plugin skill discovery after packaging/install where available. Do not
   mark an unrun installed or automatic-resume gate complete.

One focused design pass and one implementation review are sufficient unless
findings require another round. Do not repeat the architecture review or expand
the scope to OpenHands during this phase.

Design pass: the existing advisor confirmed these seams and identified the lack
of a machine-readable Codex return artifact and the external executor selection
hazard above. Both are addressed by the explicit host-written capture boundary
and unchanged external selection. Native executor revisions will use fresh
agents in this increment; exact-turn native continuation is outside this scope.

## Host evidence

The current session exposes `collaboration.spawn_agent`, `followup_task`,
`list_agents`, `wait_agent`, and `interrupt_agent`; native transport must honor
their documented arguments. Official [subagent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents)
describes inherited permissions, model selection, and custom agents. Those facts
support the adapter design; only live observations establish its exercised tier.

## Implementation handoff, 2026-10-03

Source adapter completed in
`/Users/luinstra/.codex/worktrees/codex-integration/claude_crew`, with intentionally
detached HEAD at `6b16ba30471407c691f944af2316c869227ae061`. All delivered changes
remain unstaged and uncommitted. The original checkout, installed root plugin,
global configuration and root loop state were not changed.

The adapter adds Codex HostRoles, per-seat review routing, frozen native
model/effort transport metadata, owned actual-handle bindings and explicit
host-written capture. Production advisor staging/promotion and native built-in
executor admission reuse their existing protocols. `task_native_channel` stays
Claude-only, so configured Sol and other named external executors retain their
selection and continuation. Codex native executor continuation is explicitly off;
rounds are fresh. The four discoverable source skills read shared commands and
one [transport supplement](codex-transport.md).

Final synthetic verification (Python 3.14.3; disposable roots, isolated ambient
config, pinned interpreter/PATH, no paid provider calls or agent delegation):

| Suite | Final result |
|---|---|
| `scripts/tests/test-codex-native.py` | 43 tests passed |
| `scripts/tests/test-review-workflow.py` | 201 tests passed |
| `scripts/tests/test-measure-twice.py` | 131 tests passed |
| `scripts/tests/test-build-workflow.py` | 89 tests passed |
| `scripts/tests/test-multiagent.py` | 1,990 checks passed |
| `scripts/tests/test-hooks.py` | 674 checks passed |

The first runs exposed old all-external Codex test assumptions, missing
parent-formatter admission, advisor transport/journal serialization and a Cursor
executor-routing spillover in the transferred partial implementation; all were
corrected. Ambient project overrides and the asdf shim caused isolated test
failures, resolved by clearing those overrides and pinning the interpreter and
child-process PATH. Final suite failures: zero.

Ruff 0.15.7 was run only on the 12 changed Python files with target `py311`.
The full-file scan reports 158 diagnostics already present in baseline HEAD;
comparison by diagnostic code/message found zero introduced diagnostics. Both
new Python files pass Ruff outright and were the only files formatted. All 12
changed files parse with Python 3.11's grammar. No Python 3.11 interpreter is
installed here, so that check is syntax compatibility, not a 3.11 runtime gate.
The core remains stdlib-only. `git diff --check` passes.

Real native collaboration gates, final implementation review, installed skill
discovery and new automatic-resume evidence remain parent-host follow-ups. None
was run or marked complete by this implementation executor. These expected
follow-ups do not block source implementation. OpenHands follows Codex.

## Executor handoff file list (historical)

Includes transferred documentation edits; `phase-5-build-evidence.md` was
preserved without additional edits during this executor.

- `README.md`
- `plugins/crew/README.md`
- `plugins/crew/commands/build.md`
- `plugins/crew/commands/debate.md`
- `plugins/crew/commands/measure-twice.md`
- `plugins/crew/commands/review.md`
- `plugins/crew/docs/codex-host.md`
- `plugins/crew/docs/codex-transport.md`
- `plugins/crew/docs/engine-notes.md`
- `plugins/crew/docs/multi-harness-engine-roadmap.md`
- `plugins/crew/docs/operator-followups.md`
- `plugins/crew/docs/phase-5-build-evidence.md`
- `plugins/crew/docs/phase-6-openhands-plan.md`
- `plugins/crew/docs/phase-7-codex-plan.md`
- `plugins/crew/scripts/CLAUDE.md`
- `plugins/crew/scripts/multiagent/build_workflow.py`
- `plugins/crew/scripts/multiagent/channels.py`
- `plugins/crew/scripts/multiagent/cli.py`
- `plugins/crew/scripts/multiagent/codex_native_transport.py`
- `plugins/crew/scripts/multiagent/execution.py`
- `plugins/crew/scripts/multiagent/measure_twice.py`
- `plugins/crew/scripts/multiagent/review_workflow.py`
- `plugins/crew/scripts/multiagent/workflow_transport.py`
- `plugins/crew/scripts/tests/test-codex-native.py`
- `plugins/crew/scripts/tests/test-measure-twice.py`
- `plugins/crew/scripts/tests/test-multiagent.py`
- `plugins/crew/scripts/tests/test-review-workflow.py`
- `plugins/crew/skills-codex/build/SKILL.md`
- `plugins/crew/skills-codex/debate/SKILL.md`
- `plugins/crew/skills-codex/measure-twice/SKILL.md`
- `plugins/crew/skills-codex/review/SKILL.md`

## Parent native validation, 2026-10-03

The bounded native gate is complete; see [evidence](phase-7-codex-evidence.md).
It exercised all four workflows, an actual fresh executor revision, explicit
resume/replay, wrong-handle refusal, and interruption retaining the writer fence.
All four workflow skills passed structural validation. Full implementation review
identified and resolved packaging, pin-validation, capacity and capture-race findings.
The final panel completed with six approvals and one revision request; parent
synthesis classified the remaining nested-next interface issue as minor and
closed the loop without overrides. See the evidence for the disposition. Installed discovery and automatic hook re-entry subsequently passed;
this task leaves active installed plugins untouched.


Packaging follow-up: `.codex-plugin/plugin.json` explicitly selects
`skills-codex/` (four workflow adapters plus four cancel/context helpers), keeping
Claude/Cursor command discovery separate. The version hook mirrors the Codex
manifest; 56 version checks and all eight skill validations pass. Native
transport and packaging coverage now totals 36 tests. Final panel review is complete;
the requested follow-up cleanup and installed gate are complete. OpenHands is next.


Final follow-ups: completed in branch `codex/native-integration` in the isolated
worktree. The historical executor handoff above describes its earlier detached
state. See the final evidence section for the code fixes and installed lifecycle gate.
