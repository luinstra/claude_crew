# Cursor host contract

What crew has verified about running inside the Cursor harness.
Every fact carries an evidence tag: (verified live, 2026-08-17),
(verified live, 2026-08-18), (verified live, 2026-08-20, cursor-agent
2026.08.11-e8db854), or (research-sourced, unverified). Facts without a
verified tag are expectations, not guarantees. The 2026-08-20 batch is
EXTERNAL `cursor-agent` CLI evidence only; see its scope warning.

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
- Loops are ENABLED on this host by operator decision (2026-08-18): the build and measure-twice commands arm loops on Cursor exactly as on other hosts. A cursor-host guard that refused to arm shipped briefly and was removed by that decision
- Honest caveat, unchanged by the enablement: whether Cursor honors `followup_message` stop coercion is unverified, and the Stop hook was observed NOT firing on a pure-text turn end, so loop persistence on this host is best-effort. Loop state, budgets, and the panel gates all work; what is unproven is the hook's ability to drag a stopping session back (unverified, 2026-08-18)

## Seat execution

Standalone `/crew:review` uses the shared Python workflow protocol in Cursor.
This phase intentionally routes every resolved seat externally (including Claude
voice seats through the configured CLI); Cursor-native reviewer/formatter roles
remain Phase 2 work. The command still works inside the Cursor app because the
host only transports Python-issued work and typed result files. Build and
measure-twice retain their `review-prep` orchestration until their later phases.

- Commands (both with the `/crew:` prefix and bare) import and execute in Cursor (verified live, 2026-08-17)
- Historical pre-Phase-1 probe: the former `/crew:review` pipeline ran live end to end through review-prep, per-seat engine runs, and collect; this records the old route and is not evidence for the current workflow protocol (verified live, 2026-08-17)
- All three external channels authenticate inside Cursor: the codex CLI, the cursor-agent CLI, and the claude CLI. Opus and fable ran as external Claude-channel seats with truthful channel provenance stamps (verified live, 2026-08-17)
- Panel prep is host-truthful under `unknown` detection: with no native Claude channel on this host, every seat, including Claude voices, routes external through the `claude` CLI, and that routing is correct regardless of the marker-table gap above (verified live, 2026-08-17)
- Commands whose recipes spawn Claude Task agents (`analyze`, `code-search`, `execute`, `deepinit`, and the loop commands' default executor and advisor steps) run with SILENT SUBSTITUTION: a Cursor-native agent answers in the role instead of the named crew agent. This is documented-unsupported, not guarded (verified live, 2026-08-18)

## Subagents

- The plugin ships an empty `agents-cursor/` directory, deliberately: crew's Claude agent definitions are never exposed to Cursor (verified live, 2026-08-17)

## sk plugin

- sk's skills ARE exposed to Cursor through the marketplace (verified live, 2026-08-17)
- sk ships NO hooks entry for Cursor: its hook is Claude-format and unproven on this host (verified live, 2026-08-17)

## Step 0 probe results, EXTERNAL `cursor-agent` CLI surface only

Captured with `cursor-agent -p --trust --plugin-dir <dir> --output-format
stream-json`, client `2026.08.11-e8db854`, on 2026-08-20.

SCOPE WARNING, read before reusing any row below. Every result here is evidence
about the EXTERNAL `cursor-agent` CLI, which is the route crew already ships for
cursor-channel seats. The roadmap's Phase 2 gate requires a native seat with NO
`cursor-agent` dependency, so nothing here settles app-native behavior. Treat
these as indicators the app-surface probes should confirm, never as native
evidence.

- Plugin-shipped agents ARE discovered on this surface: a local plugin dir whose
  `plugin.json` sets `agents` had its agent listed with the built-ins (verified
  live, 2026-08-20, cursor-agent 2026.08.11-e8db854)
- Named delegation initiated by the assistant mid-turn works and returns the
  agent's own text (verified live, 2026-08-20, same client)
- `readonly: true` IS ENFORCED on this surface. Same instruction, same runner,
  same `--force`, only the key differs: the `readonly: true` agent returned its
  token and created NO file; the `readonly: false` agent returned its token and
  wrote the file (verified live, 2026-08-20, same client). This is the OPPOSITE
  of Claude Code, where `engine-notes.md:163` records the key as an ignored
  no-op: Claude ignoring an unknown field was never evidence about Cursor
- SIGTERM to a running invocation terminates it cleanly with no orphan (verified
  live, 2026-08-20, same client)

### Model attribution on this surface

- `--output-format stream-json` emits a `system/init` line carrying `model`, a
  runtime-reported value rather than a subagent self-report (verified live,
  2026-08-20, same client)
- An unknown model string is REJECTED before any run with the valid-model list on
  stderr, so far-miss strings are validated rather than echoed (verified live,
  2026-08-20, same client)
- `init.model` attributes the SESSION. A parent at `composer-2.5` delegating to an
  agent pinned `gemini-3.1-pro` returned the subagent's token while `init.model`
  still read `Composer 2.5`, and no emitted surface named the subagent's model
  (verified live, 2026-08-20, same client). Whether the APP can attribute a
  native subagent is UNRESOLVED and cannot be inferred from this
- Per-invocation `--model` is reflected in `init.model` for a top-level call
  (verified live, 2026-08-20, same client):

  | requested | `init.model` |
  | --- | --- |
  | `auto` | `Auto` |
  | `composer-2.5` | `Composer 2.5` |
  | `gpt-5.5-extra-high` | `GPT-5.5 272K Extra High` |
  | `gemini-3.1-pro` | `Gemini 3.1 Pro` |
  | `glm-5.2-max` | `GLM 5.2 Max` |
  | `grok-4.5-xhigh` (retired) | `Cursor Grok 4.5 High` |
  | `grok-4.6-xhigh` (never valid) | REJECTED |
  | `cursor-grok-4.6-xhigh` | `Cursor Grok 4.6 Extra High` |

- `auto` reports the literal `Auto`, which is the alias echoed back, NOT the
  router's concrete choice. It therefore does NOT establish truthful attribution,
  and `cursor-auto` stays unverifiable (verified live, 2026-08-20, same client)
- A RETIRED model id degrades SILENTLY; a never-valid one errors loudly. This is
  the sharp edge. `grok-4.5-xhigh` (`seats.toml:60`) was a valid pin until Grok
  4.6 replaced it. Once retired it was not rejected: it was fuzzy-matched down to
  `Cursor Grok 4.5 High`, so the seat quietly ran a tier below its request while
  results persisted the old pin and misattributed the answering model. By
  contrast the never-valid spelling `grok-4.6-xhigh` (missing the required
  `cursor-` prefix) is REJECTED outright. So a pin cannot be trusted to keep
  meaning what it meant: any seat model can degrade in place when the provider
  ships a new generation, with no error. FIXED: repinned to the exact advertised
  id `cursor-grok-4.6-xhigh`, which resolves to `Cursor Grok 4.6 Extra High`
  (verified live, 2026-08-20, same client)

## Probe log

- 2026-08-17: install/component-routing capture, env captures (both surfaces), hook payload captures, live `/crew:review` pipeline run, sk exposure check
- 2026-08-18: subagent-substitution check for Task-spawning commands
- 2026-08-20, cursor-agent 2026.08.11-e8db854: EXTERNAL CLI batch. Plugin agent
  discovery, assistant-initiated named delegation, `readonly` enforcement plus a
  write control, SIGTERM cancellation, `stream-json` model attribution with an
  invalid-model negative control, and a six-string per-invocation model sweep.
  No app-surface probe ran; native execution remains unmeasured
