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

## What crew provides

Crew ships agents, skills, and `/crew:*` slash commands. That inventory is NOT
listed here on purpose: the harness already advertises the live set, and a
hand-maintained copy in your CLAUDE.md goes stale the moment crew updates (it
has, naming agents and commands that no longer existed). Read it from the
source instead:

- **Agents**: the Task tool lists each agent with its description. Crew's
  SessionStart hook also names the ones you invoke directly; the rest
  (review-panel seats and their helpers) are driven by the commands, not called
  by hand.
- **Slash commands**: `/help`, or the command list in the crew README.
- **Skills**: they self-activate from their own description; no roster needed.

What follows is the part no registry can tell you: how to drive the panel, and
the working style crew expects.

## Panels & config

In `/crew:build` and `/crew:measure-twice` completion is normally reached by a completing verdict (`APPROVED`, or `REVISE --minor-only`). The panel's sign-off is advisory, not a hard gate: a human may authorize completion over an advisory (a `NOT MET` quorum, target drift) with `--force`, which is stamped in `last_verdict_overrides` as an explicit override for the audit trail (never laundered into a clean sign-off).

**Panel size (build / measure-twice / review / debate):** prefix the argument with a panel flag. Not every change needs the full panel — e.g. `/crew:build --panel lite "fix the bug"`.
- No flag: the configured `default_panel` (or built-in `full` if unset); `"${CLAUDE_PLUGIN_ROOT}/crew" seats` prints its available external-CLI seats (the Claude voices complete it). `--panel full` selects the `full` preset, which a `[panels].full` config entry can redefine; print any preset's resolved roster with `"${CLAUDE_PLUGIN_ROOT}/crew" seats --debate --panel <name>`.
- `--panel lite`: the two Claude voices · `--panel solo`: one Claude voice · `--panel quick`: the cheapest cross-model pair (good for routine diffs)
- `--panel cursor`: every registered Cursor model-seat (no codex, no Claude)
- `--seats codex,opus`: any subset of the registered seats (some are opt-in and never in a built-in default panel); list every seat with `"${CLAUDE_PLUGIN_ROOT}/crew" doctor`

**Config:** `full` is the built-in default. A per-repo `.crew/config.toml` or global `~/.crew-config.toml` can set `default_panel`, redefine/add `[panels]` presets (usable via `--panel <name>`), mark seats `available = false` (drops an un-authed seat), or override per-command (`[debate].panel`, `[dispatch].seat`, `[dispatch].timeout`, `[build].executor`, `[build].executor_retries`, `[build].resume_executor`). `[dispatch].timeout` defaults to 1800 seconds and applies only to dispatch WORK; provider floors raise the effective timeout only when the resolved value is below the floor; agy's floor is its print timeout plus grace, about 8 minutes by default, so the 1800-second default is not floored; review, council, run, and probe seats still use `[tuning].timeout`. Precedence: **CLI flag > per-repo config > global config > built-in** governs panel/config resolution on a fresh resolve (per-repo wins over global; no env-var tier). Needs Python 3.11+ (stdlib `tomllib`); the `crew` engine refuses to run below that. Review/build/measure-twice resolve their panel through `review-prep`; `/crew:debate` and `/crew:dispatch` have their own config-aware resolvers (`[debate].panel`, `[dispatch].seat`, `[dispatch].timeout`). Per-provider write-mode dispatch tuning lives under `[dispatch.<kind>]` (keys declared by each provider; `crew dispatch --options` lists them). `/crew:build` also routes its IMPLEMENT step through a resolved executor: `--executor <seat>` (or `[build].executor`, per-repo then global) picks the write-capable subprocess seat that implements each round instead of the default `crew:executor` Task, and `[build].executor_retries` (int `0..2`, default `0`) bounds how many times a failed external-executor round retries before it surfaces and stops. `[build].resume_executor` is default-ON reuse of the external executor's provider conversation; `resume_executor = false` opts out, and only codex/cursor support resume. For a build loop, the implement-step executor resolves active-loop stamp (`source: "state"`, only when an active valid loop with a non-empty stamp matches the resolved session) > `--executor` flag > `[build].executor` config > builtin, so a resumed loop keeps the executor it started with; the generic flag-over-config precedence above describes only a fresh resolve with no active loop.

For `[seats.<name>]`, `via = ["<channel>"]` is the current execution key and must name exactly one known channel (`codex`, `cursor`, `agy`, or `claude`); `provider` is accepted as the legacy spelling. A declared seat also needs an explicit `model`. Do not put both `provider` and `via` in the same table: that row is ignored with a migration warning. A legacy `provider` in a user layer may legally override a shipped seat's `via`; it translates mechanically to the equivalent one-element channel.

**Onboarding:** `/crew:init` detects which provider CLIs are installed and writes a commented config file, never clobbering an existing one silently. A fresh scaffold (`~/.crew-config.toml`, or `.crew/config.toml` with `--repo` when no global exists) gets cost-safe defaults + honest per-seat `available` flags; with `--repo` AND an existing global `~/.crew-config.toml`, the per-repo file is instead seeded from that global verbatim (comments preserved), then lightly adjusted. (Distinct from `/crew:crew-config`, which copies this `CLAUDE.md`, not the engine-tuning `.toml`.)

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
