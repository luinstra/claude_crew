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

In `/crew:build` and `/crew:measure-twice` completion is normally reached by a completing verdict (`APPROVED`, or `REVISE --minor-only`). The panel's sign-off is advisory, not a hard gate: a human may authorize completion over an advisory (a `NOT MET` quorum, target drift) with build’s `--force` or measure-twice’s exact bound `force` decision, which is stamped in `last_verdict_overrides` as an explicit override for the audit trail (never laundered into a clean sign-off).

**Panel size (build / measure-twice / review / debate):** prefix the argument with a panel flag. Not every change needs the full panel — e.g. `/crew:build --panel lite "fix the bug"`.
- No flag: the configured `default_panel` (or built-in `full` if unset). The standalone review and debate engines resolve panel names themselves; `"${CLAUDE_PLUGIN_ROOT}/crew" seats` prints the configured default panel's external subset for the current host, while Claude-host Task seats remain in the orchestrator's native split. `--panel full` selects the `full` preset, which a `[panels].full` config entry can redefine. **On a Cursor host, the default roster is affected too:** built-in `full` carries cursor seats, so standalone `/crew:review` issues a cursor seat natively only when its `native_model` is set and drops a seat without that pin before the freeze. The `--panel cursor` bullet below states both rules in full; they apply to any roster carrying a cursor seat, named or default, so a user who never passes `--panel` still meets them.
- `--panel lite`: the two Claude voices · `--panel solo`: one Claude voice · `--panel quick`: the cheapest cross-model panel (good for routine diffs)
- `--panel cursor`: every registered Cursor model-seat (no codex-channel seat, no Claude). The shipped catalog carries exactly one `native_model`, so on a Cursor host this panel is a one-seat roster today and meets quorum trivially. In standalone `/crew:review`, seats are issued as NATIVE in-session only when `native_model` is set; a seat without that pin warns once and is dropped from the panel. **Setup precondition:** set `native_model` to the exact variant slug the Task path offers after one badge read on the app. A nameable but unenabled or variant-mismatched model is still issued native, may be refused at spawn, and settles failed, with no CLI fallback. The panel degrades rather than erroring, so with no enabled native family a cursor-only panel returns nothing usable. Enable the model family and match the native pin, use a roster with non-cursor seats, or take the opt-out below and send the seat back out through the `cursor-agent` CLI
- `--seats sol,opus`: any subset of the registered seats (some are opt-in and never in a built-in default panel); list every seat with `"${CLAUDE_PLUGIN_ROOT}/crew" doctor`. `cursor`, `codex`, and `agy` are group tokens, not seats: `cursor` expands to every cursor-* seat and `codex` to every codex-channel seat (opt-in included), while `agy` is EMPTY unless a config declares a seat on that channel under another name (`[seats.agy-gemini]` with `via = ["agy"]`), since no shipped seat rides it

**Config:** `full` is the built-in default. A per-repo `.crew/config.toml` or global `~/.crew-config.toml` can set `default_panel`, redefine/add `[panels]` presets (usable via `--panel <name>`), mark seats `available = false` (drops an un-authed seat), or override per-command (`[debate].panel`, `[dispatch].seat`, `[dispatch].timeout`, `[build].executor`, `[build].executor_retries`, `[build].resume_executor`). `[dispatch].timeout` defaults to 1800 seconds and applies only to dispatch WORK; provider floors raise the effective timeout only when the resolved value is below the floor; agy's floor is its print timeout plus grace, about 8 minutes by default, so the 1800-second default is not floored. Standalone `/crew:review` resolves a raw timeout via CLI `--timeout` > per-repo/global `[tuning].timeout` > builtin 600, caps external-provider work at 540 seconds to reserve 60 seconds for settlement, accepts an Agy floor only at or below 540, and fails the start above that. A fresh over-cap resolution warns once on stderr; a matching-pointer resume adopts its frozen timeout without warning. Run and probe keep the ordinary `[tuning].timeout` wall clock. `[review].force_external_channels` is the native opt-out for standalone review and debate: list a channel there (for example `force_external_channels = ["cursor"]` under `[review]`) and its seats run through their ordinary external CLI instead of being issued as in-session seats, and nothing on that channel is driven in-session at all. `crew review --force-external <channels>` outranks it, an empty value forces nothing, native admission stays the default, and the resolved choice is recorded in the run identity. Precedence: **CLI flag > per-repo config > global config > built-in** governs panel/config resolution on a fresh resolve (per-repo wins over global; no env tier). Needs Python 3.11+ (stdlib `tomllib`); the `crew` engine refuses to run below that. Standalone review and debate resolve through `review_workflow` using the shared resolver; build retains `review-prep`; measure-twice uses the engine-owned `loop_review` workflow; `/crew:dispatch` has its own config-aware resolver (`[dispatch].seat`, `[dispatch].timeout`). Per-provider write-mode dispatch tuning lives under `[dispatch.<kind>]` (keys declared by each provider; `crew dispatch --options` lists them). `/crew:build` also routes its IMPLEMENT step through a resolved executor: `--executor <seat>` (or `[build].executor`, per-repo then global) picks the write-capable subprocess seat that implements each round instead of the default `crew:executor` Task, and `[build].executor_retries` (int `0..2`, default `0`) bounds how many times a failed external-executor round retries before it surfaces and stops. `[build].resume_executor` is default-ON reuse of the external executor's provider conversation; `resume_executor = false` opts out, and only codex/cursor support resume. For a build loop, the implement-step executor resolves active-loop stamp (`source: "state"`, only when an active valid loop with a non-empty stamp matches the resolved session) > `--executor` flag > `[build].executor` config > builtin, so a resumed loop keeps the executor it started with; the generic flag-over-config precedence above describes only a fresh resolve with no active loop.

For debate runs, the explicit option example is `crew debate --force-external <channels>`.

The matching-pointer timeout adoption above is silent only when standalone
`--timeout` is omitted. An explicitly supplied timeout re-resolves normally and
may re-warn; only a different effective value creates a new identity.

For `[seats.<name>]`, `via = ["<channel>"]` is the current execution key and must name exactly one known channel (`codex`, `cursor`, `agy`, or `claude`); `provider` is accepted as the legacy spelling. A declared seat also needs an explicit `model`. Do not put both `provider` and `via` in the same table: that row is ignored with a migration warning. A legacy `provider` in a user layer may legally override a shipped seat's `via`; it translates mechanically to the equivalent one-element channel.

**Onboarding:** `/crew:init` detects which provider CLIs are installed and writes a commented config file, never clobbering an existing one silently. A fresh scaffold (`~/.crew-config.toml`, or `.crew/config.toml` with `--repo` when no global exists) gets cost-safe defaults + honest per-seat `available` flags; with `--repo` AND an existing global `~/.crew-config.toml`, the per-repo file is instead seeded from that global verbatim (comments preserved), then lightly adjusted. (Distinct from `/crew:crew-config`, which copies this `CLAUDE.md`, not the engine-tuning `.toml`.)

## Planning Workflow

1. Use `/crew:plan` to start a planning session (or `/crew:measure-twice` for panel-verified plans)
2. Answer clarifying questions about preferences and requirements
3. Say "Create the plan" when ready
4. Use `/crew:review` to evaluate the plan if needed
5. Use `/crew:execute` to run the plan via executor agent (keeps main context clean)

**Measure-Twice Loop:** For critical tasks, `/crew:measure-twice` uses the engine-owned planning workflow and iterates toward `APPROVED` or `REVISE --minor-only`. Python records review evidence and completion. A `completion_advisory` parks the lifetime on an exact owner/question-bound `measure-twice-decide` decision: the human may authorize `force --confirmation force`, request `retry_review`, or cancel. Force cannot extend an expired deadline or Stop cap. See [the protocol](measure-twice-protocol.md).

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

Active build / measure-twice loops are enforced by the Stop hook: they won't end until the panel reaches a completing verdict (`APPROVED`, or `REVISE --minor-only`; a human may authorize build’s `--force` or measure-twice’s exact bound `force` decision over an advisory), a SECOND consecutive `FAILED` verdict ends the loop terminally in place (`review_failed`; a single FAILED does not), you cancel (`/crew:cancel-build` / `/crew:cancel-measure-twice`), or an enforced safety limit trips (the stop-fire cap or the wall-clock deadline, which force-exit the loop).

A **parked turn** is the exception: while the session has background work in flight (panel seats, but also any unrelated background shell), the Stop is ALLOWED and the turn ends with the loop still active. Waiting is not quitting, and the session resumes when that work completes. Consecutive parked turns are capped (20), so a background process that never exits cannot silently switch the loop off.
