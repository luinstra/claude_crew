# Cursor host contract

What crew has verified about running inside the Cursor harness.
Every fact carries an evidence tag: (verified live, 2026-08-17) or
(verified live, 2026-08-18), or (research-sourced, unverified). Facts
without a verified tag are expectations, not guarantees.

## Install and component routing

- Cursor installs from the repo's `.claude-plugin/marketplace.json` using Claude semantics: the whole plugin directory is copied. It also installed a plugin that the `.cursor-plugin` twin's marketplace entry did not list (verified live, 2026-08-17)
- The per-plugin `.cursor-plugin/plugin.json` IS honored for component routing (commands, agents, hooks fields) once a plugin is installed (verified live, 2026-08-17)
- A locally added marketplace is cloned to `~/.cursor/plugins/marketplaces/` and PINNED at the registration-time commit; plugin remove/reinstall reuses that same clone, so refreshing installed bytes requires refreshing the clone itself, not just reinstalling the plugin (observed repeatedly, verified live, 2026-08-17/18)
- Plugin copies land under `~/.cursor/plugins/cache/<marketplace>/<plugin>/<sha>/` (verified live, 2026-08-17)

## Detection and environment

- Two distinct env surfaces exist in this host. Agent-spawned shells are sandbox-scrubbed to a whitelist: operator exports like `CREW_HOST` are DROPPED, and `__CURSOR_SANDBOX_ENV_RESTORE` is present as evidence of the scrub. Hook processes, by contrast, DO inherit the app's launch-shell env (verified live, 2026-08-17)
- Native env markers observed in the agent shell: `CURSOR_AGENT`, `CURSOR_CONVERSATION_ID`, `CURSOR_INVOKED_AS`, `CURSOR_RIPGREP_PATH` (verified live, 2026-08-17)
- No `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, or `CODEX_*` markers appear: Cursor emulates neither the Claude Code host nor the Codex host (verified live, 2026-08-17)
- Unaided host detection currently reads `unknown`: the cursor marker table ships empty. A fill is staged on a parking branch pending a second capture (verified live, 2026-08-17; fill: unverified). The all-external seat routing this host needs is correct under `unknown` too, so reviews and dispatch work without any override in the meantime

## Hook integration

- SessionStart and Stop both DELIVER, with Cursor-native payloads (verified live, 2026-08-17)
- Payload keys observed: `conversation_id`, `cursor_version`, `composer_mode`, `generation_id`, `is_background_agent`, `model`, `model_id`, `model_params`, `session_id`, `status`, `loop_count`, token counts, `user_email`, `workspace_roots`. `session_id` arrives NON-EMPTY; `transcript_path` is empty at SessionStart (verified live, 2026-08-17)
- The shipped relative hook commands (`python3 ./scripts/...`) resolve from the plugin root and work as shipped, unmodified (verified live, 2026-08-17)
- The Stop hook is NOT invoked on every turn end: a pure-text turn ended with no Stop fire observed (verified live, 2026-08-17)

## Loops

- Whether Cursor honors a `followup_message` stop-coercion response is UNVERIFIED on this host (unverified, 2026-08-17)
- Applied scope, per that gap: persistence loops stay guarded off on this host WHEN THE HOST IS IDENTIFIED. The build and measure-twice command markdown carries the guard, which stopped with its named message instead of arming a loop in a session bound via `CREW_HOST=cursor` (verified live, 2026-08-17)
- Honest residual: unaided detection currently reads `unknown` (the Cursor marker table ships empty pending the two-capture fill), and the guard deliberately fails open past its host check, so an UNBOUND Cursor session can still arm a loop whose stop coercion is unverified. Until the marker fill lands, the guard is only as good as the `CREW_HOST=cursor` binding from the launch shell (verified live, 2026-08-17)

## Seat execution

- Commands (both with the `/crew:` prefix and bare) import and execute in Cursor (verified live, 2026-08-17)
- The full `/crew:review` pipeline ran live end to end: review-prep, per-seat engine runs, collect digest. Quorum was MET at 5/6 usable seats; the one failure was the known cursor-auto empty-output flake, not a host defect (verified live, 2026-08-17)
- All three external channels authenticate inside Cursor: the codex CLI, the cursor-agent CLI, and the claude CLI. Opus and fable ran as external Claude-channel seats with truthful channel provenance stamps (verified live, 2026-08-17)
- Panel prep is host-truthful under `unknown` detection: with no native Claude channel on this host, every seat, including Claude voices, routes external through the `claude` CLI, and that routing is correct regardless of the marker-table gap above (verified live, 2026-08-17)
- Commands whose recipes spawn Claude Task agents (`analyze`, `code-search`, `execute`, `deepinit`) run with SILENT SUBSTITUTION: a Cursor-native agent answers in the role instead of the named crew agent. This is documented-unsupported, not guarded (verified live, 2026-08-18)

## Subagents

- The plugin ships an empty `agents-cursor/` directory, deliberately: crew's Claude agent definitions are never exposed to Cursor (verified live, 2026-08-17)

## sk plugin

- sk's skills ARE exposed to Cursor through the marketplace (verified live, 2026-08-17)
- sk ships NO hooks entry for Cursor: its hook is Claude-format and unproven on this host (verified live, 2026-08-17)

## Probe log

- 2026-08-17: install/component-routing capture, env captures (both surfaces), hook payload captures, live `/crew:review` pipeline run, sk exposure check
- 2026-08-18: subagent-substitution check for Task-spawning commands
