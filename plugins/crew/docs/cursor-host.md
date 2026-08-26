# Cursor host contract

What crew has verified about running inside the Cursor harness.
Every fact carries an evidence tag: (verified live, 2026-08-17),
(verified live, 2026-08-18), (verified live, 2026-08-20, cursor-agent
2026.08.11-e8db854), (verified live, 2026-08-25, cursor-agent
2026.08.11-e8db854), or (research-sourced, unverified). Facts without a
verified tag are expectations, not guarantees. The 2026-08-20 and 2026-08-25
batches are EXTERNAL `cursor-agent` CLI evidence only; see the scope warning.

## Install and component routing

- Cursor installs from the repo's `.claude-plugin/marketplace.json` using Claude semantics: the whole plugin directory is copied. It also installed a plugin that the `.cursor-plugin` twin's marketplace entry did not list (verified live, 2026-08-17)
- The per-plugin `.cursor-plugin/plugin.json` IS honored for component routing (commands, agents, hooks fields) once a plugin is installed (verified live, 2026-08-17)
- A locally added marketplace is cloned to `~/.cursor/plugins/marketplaces/` and PINNED at the registration-time commit; plugin remove/reinstall reuses that same clone, so refreshing installed bytes requires refreshing the clone itself, not just reinstalling the plugin (observed repeatedly, verified live, 2026-08-17/18)
- Plugin copies land under `~/.cursor/plugins/cache/<marketplace>/<plugin>/<sha>/` (verified live, 2026-08-17)

## Detection and environment

- Two distinct env surfaces exist in this host. Agent-spawned shells are sandbox-scrubbed to a whitelist: operator exports like `CREW_HOST` are DROPPED, and `__CURSOR_SANDBOX_ENV_RESTORE` is present as evidence of the scrub. Hook processes, by contrast, DO inherit the app's launch-shell env (verified live, 2026-08-17)
- Native env markers observed in the agent shell: `CURSOR_AGENT`, `CURSOR_CONVERSATION_ID`, `CURSOR_INVOKED_AS`, `CURSOR_RIPGREP_PATH` (verified live, 2026-08-17)
- No `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, or `CODEX_*` markers appear: Cursor emulates neither the Claude Code host nor the Codex host (verified live, 2026-08-17)
- The marker table ships FILLED with `CURSOR_AGENT` and `CURSOR_CONVERSATION_ID`, two of the four agent-shell names on the line above (verified live, 2026-08-17). The other two are deliberately left out: detection matches on ANY name, so a wider table adds no detection power and only more chances for a non-Cursor shell to collide. It follows from the fill, not from a capture, that unaided detection now reads `cursor` in the agent shell: no live run has been captured since the table was empty, when it demonstrably read `unknown`. That shell keeps these markers even though it drops `CREW_HOST`, which is what puts an unaided signal exactly where the override cannot survive. Provenance, stated plainly: the fill rests on ONE live capture (2026-08-17) and the second capture it was once parked pending has NOT been taken. What a second one would confirm is that both names appear in a FRESH agent shell on a different client build and a different conversation, rather than being artifacts of that one session, since detection matching on ANY name means a single absent-in-general marker would still be covered by the other but both being session-local would send this host back to reading `unknown`
- The cursor tier is DELIBERATELY the LAST marker tier: `CREW_HOST` first, then the codex markers, then the Claude markers, then these. The reason is the other Cursor env surface, the INTEGRATED TERMINAL a human types into, which is not the agent shell the line above describes: it hands `CURSOR_AGENT` to anything launched from it, another harness included. A `claude` session started there carries `CLAUDECODE` and `CURSOR_AGENT` at once and the harness executing crew is Claude Code, so a cursor answer would mint native cursor actions Claude Code cannot spawn. Ranking claude above cursor does not touch pure-cursor detection: the agent shell carries no Claude marker at all. Codex keeps its standing above claude because its reachable collision runs the opposite way (crew scrubs codex and cursor markers from a `claude` child but nothing scrubs `CLAUDECODE` from a codex child, so a codex seat re-invoking crew is a genuine codex executor)
- Honest residual of that ordering: env alone cannot show which harness nests inside which, so the mirror case loses. A `cursor-agent` process spawned BY a Claude session inherits `CLAUDECODE`, and crew re-invoked from inside it now reads `claude` even though cursor is the executor. The remedy is the override, `CREW_HOST=cursor`. The integrated-terminal case was chosen over this one because it is an ordinary human setup and this one is a nested re-invocation
- Adjacent but DISTINCT residual, from the marker fill rather than the ordering: a bare `crew review` typed by a HUMAN into the integrated terminal, with no harness executing it, now reads `cursor` where it once read `unknown`. It mints native actions nothing is there to spawn, and unlike the `unknown` read it does NOT reach the all-external route: a lost native formatter reroutes to parent context, but a lost native reviewer settles FAILED, so every cursor-channel seat is lost and the panel yields nothing usable. The fix is `force_external_channels = ["cursor"]` under `[review]` in config (see the Subagents section), which asks for the all-external route up front while the run still records the host it actually ran on. `CREW_HOST=codex` reaches the same routing, because a host with no role row and a channel forced external both drive nothing in-session, but it makes crew assert a host name that is false and has to be re-exported for every later command in the run; prefer the config key
- The HOOK shell is the other surface and is NOT covered by that: it inherits the app's launch-shell env, where these markers have not been observed, so a hook still reads `unknown` unless the operator exports `CREW_HOST=cursor` in the shell that launches Cursor. The override is therefore retired for seat routing and still load-bearing for hook output shape

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

Standalone `/crew:review` uses the shared Python workflow protocol in Cursor and
now routes cursor-channel seats NATIVELY: a seat whose model has a shipped
Cursor reviewer role is issued as an in-session Task action (`driver=native`,
`channel=cursor`) instead of a `cursor-agent` subprocess. Claude voice seats on
this host still resolve external through the configured CLI, and codex and agy
seats are external as before. The formatter and the reviewer return-transport
scribe are native here too, so a Cursor review that spawns cleanly needs no
`cursor-agent` process for its own channel at all. The two SUPPORT roles are not
hard dependencies: a lost native formatter reroutes to parent context and the
scribe keeps its host-write fallback (see the Subagents section). A SEAT is a
different matter, and the next bullets say how.

Four consequences worth stating plainly:

- **Unattributable models are warned about and dropped, not silently rerouted.**
  A cursor-channel seat whose model the host cannot NAME (`cursor-auto` at
  `auto`, which Cursor echoes back rather than resolving, or a seat carrying no
  model) prints one warning naming the seat and its model and is removed from
  the roster before the run is minted. Any concrete model string is admitted,
  so a config `model` override keeps its seat native. The surviving seats run
  and the quorum denominator counts only them; a roster reduced to zero fails
  with the ordinary `no_seats` error. The drop happens BEFORE the freeze, so
  such a seat never becomes an action at all.
- **A lost native reviewer settles FAILED. There is no CLI fallback for it.**
  `review-recover` with `native_task_lost` settles the action, because a seat's
  answer belongs to the model that gave it and its own channel is one this host
  drives in-session rather than as a subprocess (the same routing policy that
  drops an unattributable seat). The panel degrades rather than dying: quorum recounts
  the usable seats and the digest synthesizes from whatever returned. A retry
  mints a fresh attempt on the FROZEN native route, so it gets a fresh in-session
  spawn and no more.
- **Only review is routed.** `/crew:analyze`, `/crew:code-search`,
  `/crew:execute`, `/crew:deepinit`, and the loop commands' executor and advisor
  steps still run under the documented-unsupported silent substitution described
  in the routing row below: a Cursor-native agent answers in the role instead of
  the named crew agent. Build and measure-twice retain their `review-prep`
  orchestration until their later phases.
- **Native role tools are inherited; `readonly: true` is SHIPPED but unverified
  here.** A reviewer, formatter, or scribe spawned in-session gets the launching
  session's tools: this host has no per-role tool field. The two roles that must
  not write, `crew-reviewer` and `crew-formatter`, ship `readonly: true`, but the
  only enforcement evidence for that key comes from the external CLI surface (see
  its scope warning below), not from the in-session surface these roles run on.
  The scribe does not carry it, because it exists to write. So plan for the key
  being inert here and treat the read-only and write-only constraints as held by
  role prose: the adapters say so in their own words, and the run record stamps
  every native action on this host `access: read-only-advisory` rather than the
  `read-only` this channel's own external route carries (the tier is per adapter,
  and agy's, for one, is advisory too). The Claude comparison is per ROLE, not
  wholesale: `agents/formatter.md` is `tools: Read` and `agents/scribe.md` is
  `tools: Write`, so those two are tool-scoped and a Claude support role that
  ignored its prose still could not write, which is a tier no role here holds.
  The Claude REVIEWER is not in that tier either: it carries `Bash` for git
  inspection with nothing sandboxing it, so it is read-only by convention and
  stamps `read-only-advisory` too. What this host loses against Claude is the
  two support roles' enforcement, not the reviewer's.
  Routing a cursor seat natively also GIVES UP enforcement the external route
  had: `providers/cursor.py` passes `--mode plan`, which applies no edits, and an
  in-session Task has no equivalent. The trade is one review's worth of
  `cursor-agent` processes against losing the only mechanical read-only boundary
  on this host's seats. `review.md` states this at the spawn fences.

Seat routing needs no override: the filled marker table answers `cursor` in the
agent shell every crew command runs in, and every native route follows from that
answer. `CREW_HOST=cursor` still forces it where the markers do not reach (the
hook shell) or where a test pins a host deliberately.

Native admission is the default and has an opt-out that does not touch the host
name: `[review].force_external_channels = ["cursor"]` sends every cursor-channel
seat back out through `cursor-agent` and leaves this host driving nothing
in-session. The Subagents section states it in full, beside the two unverified
assumptions it exists to answer.

- Commands (both with the `/crew:` prefix and bare) import and execute in Cursor (verified live, 2026-08-17)
- Historical pre-Phase-1 probe: the former `/crew:review` pipeline ran live end to end through review-prep, per-seat engine runs, and collect; this records the old route and is not evidence for the current workflow protocol (verified live, 2026-08-17)
- All three external channels authenticate inside Cursor: the codex CLI, the cursor-agent CLI, and the claude CLI. Opus and fable ran as external Claude-channel seats with truthful channel provenance stamps (verified live, 2026-08-17)
- Panel prep is host-truthful: with no native Claude channel on this host, every seat, including Claude voices, routes external through the `claude` CLI. Captured while detection still read `unknown`, and the routing it records is unchanged by the marker fill, since it follows from the absent Claude channel rather than from the host name (verified live, 2026-08-17)
- Commands whose recipes spawn Claude Task agents (`/crew:analyze`, `/crew:code-search`, `/crew:execute`, `/crew:deepinit`, and the loop commands' default executor and advisor steps) run with SILENT SUBSTITUTION: a Cursor-native agent answers in the role instead of the named crew agent. This is documented-unsupported, not guarded (verified live, 2026-08-18)

### In-flight reviews across this change

A standalone review started before the native routing landed froze its seats as
`subprocess` and its host as whatever detection returned then. Finish or abandon
ANY in-flight standalone review before adopting these bytes, on any host: a run
frozen with `host=unknown` returns `conflict/host_mismatch`, and one frozen
`host=cursor` with external seats hits `provider_config_drift` at
`review-execute`. Both are guards working; the remedy is a fresh review, not a
migration.

Two more in-flight cases are NOT Cursor-specific (a formatter action minted
before the reroute mark existed, and a run identity frozen before it carried
`force_external_channels`); both are recorded in `docs/engine-notes.md`, where a
Claude-host operator will find them.

## Subagents

- Crew's Claude agent definitions are still never exposed to Cursor. What
  `agents-cursor/` ships instead is three THIN role adapters written for this
  host: `crew-reviewer.md`, `crew-formatter.md`, and `crew-scribe.md`. Each
  carries static metadata plus the host-safety prose its role needs, and nothing
  else: no rubric, no flow, no verdict vocabulary, no panel knowledge. All
  canonical instructions arrive through the issued `prompt_path`, exactly as on
  Claude. The empty-directory state is retired (it was recorded as deliberate on
  2026-08-17, before any native role existed)
- Every shipped name carries the `crew-` prefix, because a project
  `.cursor/agents/` takes precedence over other sources and an unprefixed name
  would be easy to shadow
- The shipped files assume the agent NAME comes from the filename, so
  `crew-reviewer.md` is invoked as `crew-reviewer` and the role table's names are
  those stems. VERIFIED on the app (exit-gate run `run-c73927a5f904`,
  2026-08-25): the parent spawned `crew-reviewer` by that stem as the
  `cursor-composer` seat and the seat returned a well-formed review. A source
  test still pins the file set to the role table; if a future app build wants a
  `name:` frontmatter key instead, every role resolves to nothing and the
  routing is inert, so that is the first thing to check when a live run finds
  no role. Check it with one command:
  `cursor-agent -p --trust --plugin-dir plugins/crew "list your available
  subagents by exact name"`, and look for the three stems verbatim. That is the
  external CLI surface (see the scope warning below), so it indicates rather than
  settles; the app-surface confirmation is the Task subagent picker offering the
  same three stems
- **What to do while a live run finds no role: force the channel external.** Put
  `force_external_channels = ["cursor"]` under `[review]` in the per-repo
  `.crew/config.toml` (or the global `~/.crew-config.toml`; per-repo wins, and
  `crew review --force-external cursor` wins over both, while
  `--force-external ""` forces nothing). Standalone review then resolves every
  cursor-channel seat to the `cursor-agent` provider it used before native
  admission existed, instead of dropping it for having no native route, and it
  drives NOTHING in-session on that channel: the formatter mints on the
  parent-context route directly rather than needing a recovery call every run,
  and the scribe never mints, because it rides only a native reviewer action. The
  choice is frozen into the run identity as `force_external_channels`, so the
  record says which route the answers came from and a config edit mid-run cannot
  change how the run is judged. Native stays the default; this is the escape
  hatch for a future app build that breaks either app-surface assumption (the
  role name and the Task spawn form, both verified live 2026-08-25), and for an
  account whose seat models are not
  enabled for subagents. It is a config FILE and not an env var because the agent
  shell here scrubs operator exports. It does NOT cover the bare-human
  integrated-terminal case: that one has no agent to perform the parent actions
  either, and its remedy is in the detection section above
- There is no per-model reviewer map: the reviewer role file pins no model, so
  the host admits any model string it can name and `seats.toml` stays the one
  place a cursor seat's model is declared. Repinning a seat needs no second
  edit; only a run-time alias (`auto`) or a missing model drops a seat
- Frontmatter is `description`, plus `model` on the two support roles only.
  The reviewer file deliberately omits `model`: it is shared by every model the
  host can name, so any single pin would be wrong for all but one seat, and if
  a pin beat the model the Task call carries, every seat would run one model
  while the run record named several. Omitting the key inherits the caller's
  model, which is exactly what a shared role file needs. The scribe and formatter DO pin,
  because each is a single-model role and its pin is the same string the role
  table drives it at. `readonly: true` IS SHIPPED on
  the reviewer and the formatter, and its app-surface enforcement is UNVERIFIED:
  the only evidence for that key is from the external `cursor-agent` CLI surface
  (see the scope warning below), and no app-surface capture has tested it. It is
  shipped anyway because all three outcomes are acceptable: enforced restores a
  boundary, ignored costs nothing, and rejected makes the roles resolve to
  nothing at the first probe, which is loud rather than silent. Per file, the
  reviewer carries it as its ONE unverified key (it deliberately pins no
  `model`), while the formatter adds it beside a live `model: composer-2.5` pin,
  so a rejection there would take a working pin down with it. The formatter's
  blast radius is the smaller one even so: a lost formatter reroutes to the
  parent-context route, while a lost reviewer settles that seat failed. Until an
  app-surface capture exists, plan for the key being inert: the read-only
  constraint here is prose plus an unverified key, a weaker tier than the Claude
  SUPPORT roles hold (those are tool-scoped, `tools: Read` on the formatter and
  `tools: Write` on the scribe) though not weaker than the Claude reviewer's,
  which is read-only by convention over an unsandboxed `Bash`. The run record
  says which is which by stamping `access: read-only-advisory` on every native
  action here. Revisit when app-surface evidence exists
- Cursor subagents have no `tools` frontmatter field and inherit the parent's
  tools, so the Cursor scribe cannot be tool-restricted to write-only the way
  `agents/scribe.md` is. Its single-path, verbatim, no-other-tool constraint is
  prose-only, and the file says so
- The invocation form the driver writes is the app's Task call carrying the
  model, which is what the app-surface finding below records ("the parent passes
  the model on the Task call"). VERIFIED as a shipped fence (exit-gate run
  `run-c73927a5f904`, 2026-08-25): the driver's fence spawned the native
  reviewer subagent, which landed its review through the scribe transport
- The two support roles (scribe, formatter) run at `composer-2.5`: first of the
  catalog's cursor models in cost order, because Cursor bills composer from the
  cheap bucket
- Before native seats can run, the operator must ENABLE the seat models in
  Cursor's subagent surface (see the enabled-models section below). Until then
  those seats are `unentitled`, and the drop rule does NOT cover them: it keys
  solely on whether the host can name the model, so an unentitled but nameable
  model resolves native, is issued native, and is refused loudly at spawn. That
  refusal is a lost action, recovered with `native_task_lost`, which SETTLES the
  seat FAILED: no CLI fallback exists for a seat issued native. On the account
  state recorded below, where all five seat models are rejected as subagent
  models, every cursor-channel seat takes that path, so a cursor-only panel
  yields nothing usable. The panel degrades rather than erroring (quorum
  recounts usable seats and the digest synthesizes from whatever returned), and
  enabling the models is the mitigation. This is a setup precondition, not a
  code defect
- The two SUPPORT roles need no entitlement to keep a roster whole. The formatter is
  minted native on this host for every roster, an all-external one included, and
  the support model is `composer-2.5`, which the recorded session allowlist
  REJECTED. So a lost native formatter is REROUTED to a parent-context action by
  `review-recover` instead of failing: a repair step is never the reason a panel
  loses a seat. The scribe needs no equivalent because it rides only a native
  reviewer action, whose seat already depends on this host, and it keeps its
  host-write fallback

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

### The authoritative model catalog

`cursor-agent --list-models` prints the full `slug - Display Name` catalog. That
is the source of truth for any seat pin; do not infer the set from an example in
prose. The six registered cursor-channel seat models: `auto`,
`composer-2.5`, `gpt-5.5-extra-high`, `gemini-3.7-flash-high`, `glm-5.2-max`,
and `cursor-grok-4.6-xhigh` (Cursor Grok 4.6 Extra High). All six are exact
slugs from one authenticated capture (verified live, 2026-08-20, cursor-agent
2026.08.11-e8db854).

`gemini-3.7-flash-high` is the 2026-08-25 repin off `gemini-3.1-pro`. That same
capture listed `gemini-3.7-flash-high - Gemini 3.7 Flash`, alongside
`gemini-3.7-flash-{low,medium}`, `gemini-3.6-flash-*`, `gemini-3.1-pro`,
`gemini-3.5-flash`, and `gemini-3-flash`; no Gemini Pro newer than 3.1 exists in
the catalog, so 3.7 Flash is the newest Gemini generation (verified live,
2026-08-20, cursor-agent 2026.08.11-e8db854). The repin is an operator decision
to track the newest Gemini generation, accepting a model-CLASS change: 3.7
Flash is a Flash-class model where 3.1 Pro was Pro-class. A live recheck needs
an authenticated session (`cursor-agent --list-models` answers "Authentication
required" otherwise), so log the `cursor-agent` CLI in first. CLOSED: a fresh
authenticated `--list-models` on 2026-08-25 (204 models) still lists
`gemini-3.7-flash-high - Gemini 3.7 Flash` as the newest Gemini with no Pro
newer than 3.1, and `crew probe cursor-gemini` PASSED (9.6s), so the seat has
answered at this pin, not merely been listed at it; `crew probe cursor-auto
cursor-composer` passed in the same session (verified live, 2026-08-25,
cursor-agent 2026.08.11-e8db854).

**Recheck every cursor seat pin against `--list-models` whenever the provider
ships a new model generation.** A retired id is fuzzy-matched down without an
error, while a never-valid one is rejected outright (see below), so a pin cannot
be trusted to keep meaning what it meant. `test-multiagent.py` pins today's
answer for every cursor seat, which stops a silent near-miss edit; only the
recheck catches tomorrow's retirement. The catalog pin is the ONLY place the
recheck applies: native admission in `review_workflow.py` keeps no per-model
list, it admits any model string the host can name and excludes only the
run-time aliases in `CURSOR_UNATTRIBUTABLE_MODELS` (`auto`), so a repin needs
no second edit and a retired pin is issued as-is rather than dropped.

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

## App-surface findings (Cursor app, operator-reported)

- The app can launch a subagent on a model OTHER than the parent chat's: the
  parent passes the model on the Task call (operator-reported from a live Cursor
  session, 2026-08-20)
- A custom subagent in `.cursor/agents/` or `~/.cursor/agents/` may pin `model:`
  in frontmatter; omitting it inherits the parent. Both P8 mechanisms therefore
  exist on the app surface, so the delegated-subagent transport stands and needs
  no rework (operator-reported, 2026-08-20)
- The app UI renders a MODEL BADGE per response, naming the model that produced
  it. That is an app-rendered artifact rather than a subagent's claim about
  itself, so it is admissible as the P7 oracle, unlike the CLI's `init.model`
  which attributes only the session (operator-reported, 2026-08-20)
- Plan and team limits can still override model selection (blocked models, Max
  Mode), so an available slug is not guaranteed usable on every account
  (operator-reported, 2026-08-20)
- CLOSED, a SUBAGENT run carries its OWN badge. A parent running Cursor Grok 4.6
  High launched a subagent and the subagent's chip showed `Composer 2.5 Fast`,
  its own model, not the parent's (operator-reported with a UI capture,
  2026-08-20). This is the P7 oracle: app-rendered, seat-scoped attribution
- The subagent CANNOT self-report its model. Asked directly, it answered "I do
  not know the model id" (operator-reported, 2026-08-20). That is stronger than
  the bar required: self-report is not merely disqualified, it is impossible, so
  the badge is the only channel and an echo of the requested pin cannot occur
- CAVEAT for P8: the parent was asked for "Composer" and the badge resolved to
  `Composer 2.5 Fast`, the `-fast` variant, not plain `Composer 2.5`. Whether
  pinning the exact slug `composer-2.5` yields exactly `Composer 2.5` is a live
  per-string question, and the badge is what answers it
- `/best-of-n` is a separate path (parallel runs in worktrees), NOT a nested Task
  subagent, and is not the mechanism crew would drive (operator-reported,
  2026-08-20)

## P8: the Task-subagent model set is the account's ENABLED models

`cursor-agent --list-models` is the catalog the CLI accepts. The app's **Task
subagent** surface offers a smaller set, and that set is what the operator has
ENABLED in Cursor, not a platform limit (operator-confirmed, 2026-08-20). More
models can be enabled at will, so a rejection here is `unentitled`, a setup
state, never `unsupported`.

Observed session allowlist, five slugs:
`claude-opus-5-thinking-high`, `composer-2.5-fast`, `cursor-grok-4.5-high-fast`,
`cursor-grok-4.6-high`, `gpt-5.6-sol-medium`.

ALL FIVE registered cursor-channel seat models were REJECTED as subagent models:
`composer-2.5`, `gpt-5.5-extra-high`, `gemini-3.1-pro`, `glm-5.2-max`,
`cursor-grok-4.6-xhigh`. Being in the catalog does not make a slug usable as a
subagent model. (`gemini-3.1-pro` was the `cursor-gemini` pin on that date; the
seat has since moved to `gemini-3.7-flash-high`, and whether the subagent
surface accepts that slug has not been measured, so it carries no row here.)

Rejection is LOUD and there is no silent substitution on this surface: the parent
declined every unlisted slug and did not fall back to a neighbouring one (for
example `composer-2.5-fast` for `composer-2.5`). That is the opposite of the
retired-id hazard on the CLI, where a near-miss degrades quietly.

Requested slug to badge, for the four allowed slugs launched:

| requested | badge |
| --- | --- |
| `composer-2.5-fast` | `Composer 2.5 Fast` |
| `cursor-grok-4.5-high-fast` | `Cursor Grok 4.5 High Fast` |
| `cursor-grok-4.6-high` | `Cursor Grok 4.6 High` |
| `gpt-5.6-sol-medium` | `GPT-5.6 Sol Medium` |

Every badge matched its requested slug exactly, so an allowed slug is honored
without substitution and the badge remains a truthful oracle.

Consequence: this is a SETUP PRECONDITION, not a design constraint and not a
code defect. Before native seats can run, the operator enables the seat models
in Cursor. Until then every cursor-channel seat here is `unentitled`, and
warn-and-drop does NOT apply to it: the drop keys on attributability, and these
five strings are all concrete models. Such a seat resolves native, is issued
native, and is refused at spawn. `review-recover` then SETTLES it FAILED. There
is no CLI fallback for a seat issued native, so on an unentitled account every
cursor-channel seat takes that path and `--panel cursor` yields nothing usable.
The panel degrades rather than dying: quorum recounts the usable seats and the
digest synthesizes from whatever returned, so a mixed roster still produces a
review from its other voices. Enabling the seat models in Cursor is the
mitigation. Nothing here forces a repin. What is still worth recording, per string, is
whether a rejection is `unentitled` (enable it and retry) or `unsupported` (the
app will not honor it at all); only the latter would be a design input.

## Phase 2 exit gate (Cursor app, live)

Run `run-c73927a5f904` under session segment
`ef47bb67-91b7-4304-ab7c-337e2e0db822`, launched from the Cursor app as
`/crew:review` on the Phase 2 plan (`kind=plan`, target sha `cab15c96...`),
client cursor-agent 2026.08.11-e8db854 (verified live, 2026-08-25).

The roadmap gate asks for a live review from the app that completes with at
least one Cursor-native seat and one Claude CLI seat, truthful route
provenance, and no `cursor-agent` dependency for the native seat. Outcome:

| seat | route (frozen in `run.json` / `workflow.json`) | result |
| --- | --- | --- |
| `cursor-composer` | native: `kind=task`, `driver=native`, `channel=cursor`, role `crew-reviewer`, model `composer-2.5` | ok, full review (APPROVED), landed via scribe ingress |
| `opus` | external `claude` CLI, `kind=subprocess` | ok, 243s (REVISE) |
| `fable` | external `claude` CLI, `kind=subprocess` | ok, 180s (REVISE) |
| `codex` | external `codex`, `kind=subprocess` | FAILED: timed out at 540s, empty output |
| `codex-luna` | external `codex`, `kind=subprocess` | FAILED: timed out at 540s, empty output |

Digest: 5 launched, 3 usable, quorum 3 MET; synthesis settled. **GATE PASSED**:
the native seat and two Claude CLI seats completed, provenance is truthful, and
the native seat was served by an in-session subagent, not a shell: the app's
Tasks pane showed four external shells (codex, codex-luna, opus, fable) and one
Subagent for `cursor-composer`, and its result entered through the scribe
transport rather than a provider result file. That is UI plus run-record
evidence, not the PATH-shim artifact the plan specified, so it is recorded as
such.

What this run settles: the role name resolves from the filename stem, and the
driver's Task fence spawns the native reviewer (both previously the page's
unverified assumptions). What it does NOT settle: `readonly: true` enforcement
on the app (the reviewer never attempted a write); the per-run model badge on
the `cursor-composer` chip, which the operator has not yet reported for this
run, so its attribution rests on the request metadata until then; and the P12
and P13 environment captures.

Anomaly, attributed to the codex channel rather than this host: BOTH codex
seats hit the 540s provider timeout with no output. In the same window a codex
review seat on the Claude Code host timed out at 1800s and a `crew probe codex`
there hung past 600s on a one-line prompt, so the codex service was
unresponsive everywhere, not throttled by the Cursor agent shell. On
2026-08-18 the same seats completed inside Cursor in roughly 80s
(`run-18675bf73d80`). Not a Phase 2 defect; re-probe codex before reading a
codex timeout on this host as a host effect.

## Probe log

- 2026-08-17: install/component-routing capture, env captures (both surfaces), hook payload captures, live `/crew:review` pipeline run, sk exposure check
- 2026-08-18: subagent-substitution check for Task-spawning commands
- 2026-08-20, cursor-agent 2026.08.11-e8db854: EXTERNAL CLI batch. Plugin agent
  discovery, assistant-initiated named delegation, `readonly` enforcement plus a
  write control, SIGTERM cancellation, `stream-json` model attribution with an
  invalid-model negative control, and a six-string per-invocation model sweep.
  No app-surface probe ran; native execution remains unmeasured
- 2026-08-25, cursor-agent 2026.08.11-e8db854: re-authenticated CLI. Fresh
  `--list-models` (204 models) re-confirms `gemini-3.7-flash-high` as the newest
  Gemini; `crew probe cursor-gemini`, `cursor-auto`, and `cursor-composer` all
  pass. Closes the cursor-gemini open item. No app-surface probe ran
- 2026-08-25, Cursor app: Phase 2 exit-gate run `run-c73927a5f904`, PASSED.
  Native `cursor-composer` plus external opus/fable completed, both codex seats
  timed out. Role-name-from-filename and the Task fence verified on the app
