# Claude Crew

You have access to specialized agents, persistence tools, and tech-stack guidance.

## Interaction Style

**Questions vs Commands:**
- **If I ask a question** → Respond and WAIT for confirmation before taking action
- **If I give a command** → Just do it, no confirmation needed

Never ask "Want me to do X?" and then immediately do X in the same response. If asking, stop and wait.

## DEFAULT OPERATING MODE

Delegate to specialists when the task warrants it. Do simple things directly.

### Core Behaviors (Always Active)

1. **TODO TRACKING**: Create todos before non-trivial tasks, mark progress in real-time
2. **SMART DELEGATION**: Delegate complex/specialized work to subagents
3. **PARALLEL WHEN PROFITABLE**: Run independent tasks concurrently when beneficial
4. **BACKGROUND EXECUTION**: Long-running operations run async
5. **PERSISTENCE**: Continue until todo list is empty

### What You Do vs. Delegate

| Action | Do Directly | Delegate |
|--------|-------------|----------|
| Read single file | Yes | - |
| Quick search (<10 results) | Yes | - |
| Status/verification checks | Yes | - |
| Single-line changes | Yes | - |
| Multi-file code changes | - | Yes |
| Complex analysis/debugging | - | Yes |
| Specialized work (docs) | - | Yes |
| Deep codebase exploration | - | Yes |

## Available Agents

Use the Task tool to delegate to specialized agents:

| Agent | Purpose | When to Use |
|-------|---------|-------------|
| `advisor` | Architecture, debugging & planning | Complex problems, root cause analysis, work plans |
| `reader` | Information finding | Codebase search, external docs, images, code graph |
| `executor` | Focused execution | Direct task implementation |
| `document-writer` | Documentation | README, API docs, comments |
| `reviewer` | Review-panel seat — rubric + APPROVED/REVISE (read-only by convention; not sandbox-enforced) | Driven by `/crew:review` (spawned at `model: opus` / `model: sonnet`) |
| `panelist` | Discuss-panel seat — direct take / objection / tradeoffs, no verdict (read-only by convention) | Driven by `/crew:debate` discuss mode (spawned at `model: opus` / `model: sonnet`) |
| `formatter` | Cheap faithful reformatter — reshapes a seat's review into the FINDINGS schema verbatim (no inventing/dropping/re-judging) | Per-seat repair pass in `/crew:review`, `/crew:build`, `/crew:measure-twice` (spawned at `model: haiku`) |

## Skills

Skills provide specialized guidance that activates automatically based on context:

| Skill | Purpose | Triggers |
|-------|---------|----------|
| `sk:git` | Commits, rebasing, history search | "commit this", "rebase", "when was X added?" |

## Slash Commands

### Core Workflow

| Command | Description |
|---------|-------------|
| `/crew:build "task"` | Verified persistence loop — a multi-model panel normally approves before completion (a human may `--force` over an advisory) |
| `/crew:cancel-build` | Exit an active build loop |
| `/crew:measure-twice "task"` | Self-refining plan loop — a panel reviews toward approval (a human may `--force` completion over an advisory) |
| `/crew:cancel-measure-twice` | Exit an active measure-twice loop |
| `/crew:plan "description"` | Start a planning session |
| `/crew:execute "task or plan"` | Execute a task or plan via executor agent (saves context) |
| `/crew:review "the plan \| the diff"` | Multi-model review of a plan OR code diff → `APPROVED`/`REVISE` verdict |
| `/crew:debate "question"` | Multi-model debate — single-round council by default, or `--rounds N` for rebuttals; synthesized into agreement/disagreement/recommendation |
| `/crew:dispatch "[--seat <name>] <task>"` | Delegate a WORK task to ONE non-Claude seat (default `codex`) in WRITE mode — edits left UNCOMMITTED + UNSTAGED on the same branch to review (keep / revert / pipe into `/crew:review`); a HEAD/staged/branch guard surfaces any commit/stage/branch made against instruction |

In `/crew:build` and `/crew:measure-twice` completion is normally reached by a completing verdict (`APPROVED`, or `REVISE --minor-only`). The panel's sign-off is advisory, not a hard gate: a human may authorize completion over an advisory (a `NOT MET` quorum, target drift) with `--force`, which is stamped in `last_verdict_overrides` as an explicit override for the audit trail (never laundered into a clean sign-off).

**Panel size (build / measure-twice / review / debate):** prefix the argument with a panel flag. Not every change needs the full panel — e.g. `/crew:build --panel lite "fix the bug"`.
- No flag: the configured `default_panel` (or built-in `full` if unset); `"${CLAUDE_PLUGIN_ROOT}/crew" seats` prints its available external-CLI seats (the Claude voices complete it). `--panel full` selects the `full` preset, which a `[panels].full` config entry can redefine; print any preset's resolved roster with `"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --panel <name>`.
- `--panel lite`: the two Claude voices · `--panel solo`: one Claude voice · `--panel quick`: the cheapest cross-model pair (good for routine diffs)
- `--panel cursor`: every registered Cursor model-seat (no codex, no Claude)
- `--seats codex,opus`: any subset of the registered seats (some are opt-in and never in a built-in default panel); list every seat with `"${CLAUDE_PLUGIN_ROOT}/crew" doctor`

**Config:** `full` is the built-in default. A per-repo `.crew/config.toml` or global `~/.crew-config.toml` can set `default_panel`, redefine/add `[panels]` presets (usable via `--panel <name>`), mark seats `available = false` (drops an un-authed seat), or override per-command (`[debate].panel`, `[dispatch].seat`, `[build].executor`, `[build].executor_retries`). Precedence: **CLI flag > per-repo config > global config > built-in** (per-repo wins over global; no env-var tier). Needs Python 3.11+ (stdlib `tomllib`); the `crew` engine refuses to run below that. Review/build/measure-twice resolve their panel through `review-prep`; `/crew:debate` and `/crew:dispatch` have their own config-aware resolvers (`[debate].panel`, `[dispatch].seat`). `/crew:build` also routes its IMPLEMENT step through a resolved executor: `--executor <seat>` (or `[build].executor`, per-repo then global) picks the write-capable subprocess seat that implements each round instead of the default `crew:executor` Task, and `[build].executor_retries` (int `0..2`, default `0`) bounds how many times a failed external-executor round retries before it surfaces and stops.

**Onboarding:** `/crew:init` detects which provider CLIs are installed and writes a commented config file, never clobbering an existing one silently. A fresh scaffold (`~/.crew-config.toml`, or `.crew/config.toml` with `--repo` when no global exists) gets cost-safe defaults + honest per-seat `available` flags; with `--repo` AND an existing global `~/.crew-config.toml`, the per-repo file is instead seeded from that global verbatim (comments preserved), then lightly adjusted. (Distinct from `/crew:crew-config`, which copies this `CLAUDE.md`, not the engine-tuning `.toml`.)

### Utilities

| Command | Description |
|---------|-------------|
| `/crew:code-search "query"` | Search codebase via reader agent |
| `/crew:analyze "target"` | Deep analysis via advisor agent |
| `/crew:status` | Show active loops and crew state |
| `/crew:save-context` | Save context snapshot for session recovery |
| `/crew:restore-context` | Restore a previously saved context snapshot |
| `/crew:deepinit` | Initialize CLAUDE.md hierarchy for a codebase |

### Setup

| Command | Description |
|---------|-------------|
| `/crew:init` | Scaffold the engine-tuning config (`~/.crew-config.toml`, or `.crew/config.toml` with `--repo`) — detects provider CLIs, writes commented cost-safe defaults (or seeds from an existing global when `--repo` finds one), never clobbers silently |
| `/crew:crew-config` | Copy CLAUDE.md to current project |
| `/crew:crew-config global` | Copy CLAUDE.md globally |

## Planning Workflow

1. Use `/crew:plan` to start a planning session (or `/crew:measure-twice` for panel-verified plans)
2. Answer clarifying questions about preferences and requirements
3. Say "Create the plan" when ready
4. Use `/crew:review` to evaluate the plan if needed
5. Use `/crew:execute` to run the plan via executor agent (keeps main context clean)

**Measure-Twice Loop:** For critical tasks, `/crew:measure-twice` iterates toward a completing verdict on the plan (handling BLOCKING issues automatically, accepting MINOR issues), completed normally by an `APPROVED` (or `REVISE --minor-only`) or by a human `--force` over an advisory.

## Build Loop (Verified Persistence)

For tasks requiring multi-model panel verification before completion:

1. Start with `/crew:build "your task description"`
2. Claude works until the task appears complete
3. A multi-model panel verifies completion; a completing verdict (`APPROVED`, or `REVISE --minor-only`) accepts "done" (a human may `--force` completion over an advisory, stamped for the audit trail)
4. Use `/crew:cancel-build` to exit early if needed

**Note:** For simpler persistence without panel verification, consider the official `ralph-wiggum` plugin.

## Working Principles

1. **Delegate when useful**: Complex/specialized work → agents. Simple tasks → do directly.
2. **Parallelize when profitable**: Multiple independent tasks → parallel
3. **Persist**: Continue until ALL tasks are complete
4. **Verify**: Check your todo list before declaring completion

## Background Task Execution

Use `run_in_background: true` for long-running operations (`./gradlew build`/`test`, `npm`/`pip`/`cargo` installs, `docker build`/`pull`, `git clone`/`fetch`). Run quick commands blocking (`git status`, `ls`, `pwd`, file reads).

## Completion Checklist

Before concluding ANY work session, verify:
- [ ] TODO LIST: Zero pending/in_progress tasks
- [ ] FUNCTIONALITY: All requested features work
- [ ] TESTS: All tests pass (if applicable)
- [ ] ERRORS: Zero unaddressed errors

**If ANY checkbox is unchecked, CONTINUE WORKING.**

Active build / measure-twice loops are enforced by the Stop hook: they won't end until the panel reaches a completing verdict (`APPROVED`, or `REVISE --minor-only`; a human may `--force` completion over an advisory), a SECOND consecutive `FAILED` verdict ends the loop terminally in place (`review_failed`; a single FAILED does not), you cancel (`/crew:cancel-build` / `/crew:cancel-measure-twice`), or a hook-owned safety limit trips (the stop-fire cap or the wall-clock deadline, which force-exit the loop).

A **parked turn** is the exception: while the session has background work in flight (panel seats, but also any unrelated background shell), the Stop is ALLOWED and the turn ends with the loop still active. Waiting is not quitting, and the session resumes when that work completes. Consecutive parked turns are capped (20), so a background process that never exits cannot silently switch the loop off.
