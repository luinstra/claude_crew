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
  `model`), while the formatter adds it beside a `model: composer-2.5` pin that
  the Task path has since REFUSED (model-grammar section below), so that pin is
  not a working one today and the fallback route is what has carried the
  formatter and scribe. The formatter's
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
- Before native seats can run, the operator must ENABLE the seat model
  families in Cursor's subagent surface AND each pin must be the exact variant
  slug that surface offers (model-grammar section below). Until then those
  seats are `unentitled` or variant-mismatched, and the drop rule does NOT
  cover them: it keys solely on whether the host can name the model, so such a
  seat resolves native, is issued native, and is usually refused at spawn,
  though the same slug has also been accepted with its answering model
  unobserved. A refusal is a lost action, recovered with `native_task_lost`,
  which SETTLES the seat FAILED: no CLI fallback exists for a seat issued
  native. On the account state recorded below, where all five seat models are
  rejected as subagent models, every cursor-channel seat takes that path, so a
  cursor-only panel yields nothing usable. The panel degrades rather than
  erroring (quorum recounts usable seats and the digest synthesizes from
  whatever returned), and enabling the families plus matching the pins is the
  mitigation. This is a setup precondition, not a code defect
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

## P8: the Task-subagent model set is one variant slug per ENABLED family

`cursor-agent --list-models` is the catalog the CLI accepts. The app's **Task
subagent** surface offers a smaller set: one flattened variant slug per model
family the operator has ENABLED in Cursor, chosen per session by the app, not a
platform limit (enablement operator-confirmed 2026-08-20; the grammar is
established in the model-grammar section below). Families can be enabled at
will, so a family-off rejection is `unentitled`, a setup state. A rejection of
an enabled family's OTHER variant (bare `composer-2.5` where the surface offers
`composer-2.5-fast`) is a variant mismatch, fixed by repinning. `unsupported`
is reserved for a string the app never offers on any account.

Observed session allowlist, five slugs:
`claude-opus-5-thinking-high`, `composer-2.5-fast`, `cursor-grok-4.5-high-fast`,
`cursor-grok-4.6-high`, `gpt-5.6-sol-medium`.

ALL FIVE registered cursor-channel seat models were REJECTED as subagent models:
`composer-2.5`, `gpt-5.5-extra-high`, `gemini-3.1-pro`, `glm-5.2-max`,
`cursor-grok-4.6-xhigh`. Being in the catalog does not make a slug usable as a
subagent model. (`gemini-3.1-pro` was the `cursor-gemini` pin on that date; the
seat has since moved to `gemini-3.7-flash-high`, and whether the subagent
surface accepts that slug has not been measured, so it carries no row here.)

In THIS session's observation the rejection was loud and nothing was
substituted: the parent declined every unlisted slug and did not fall back to a
neighbouring one (for example `composer-2.5-fast` for `composer-2.5`). That is
a dated observation, not a guarantee: the model-grammar section below records
the same bare slug being ACCEPTED in another session with its answering model
unobserved, so the current contract is "an unlisted slug is usually refused,
sometimes accepted with its answering model unobserved, and a successful spawn
is unattributed until its badge is read".

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
code defect. Before native seats can run, the operator enables the seat model
FAMILIES in Cursor, and the seat pin must also be the exact variant slug the
Task path offers for that family (see the model-grammar section below);
enabling alone is necessary, not sufficient. Until then each cursor-channel
seat here is `unentitled` (family off) or variant-mismatched (family on, pin is
another variant), and warn-and-drop does NOT apply to either: the drop keys on
attributability, and these five strings are all concrete models. Such a seat
resolves native, is issued native, and is usually refused at spawn (the
model-grammar section records one acceptance with the answering model
unobserved). `review-recover` then SETTLES a refused seat FAILED. There is no
CLI fallback for a seat issued native, so on an account where no seat slug is
offered every cursor-channel seat takes that path and `--panel cursor` yields
nothing usable.
The panel degrades rather than dying: quorum recounts the usable seats and the
digest synthesizes from whatever returned, so a mixed roster still produces a
review from its other voices. Enabling the families in Cursor is the first
mitigation; matching each pin to the Task path's variant slug is the second,
and the shipped `composer-2.5` pin needs it (open decision in the model-grammar
section). What is still worth recording, per string, is
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
| `codex-luna` | external, same channel as the row above, `kind=subprocess` | FAILED: timed out at 540s, empty output |

Digest: 5 launched, 3 usable, quorum 3 MET; synthesis settled. **ROUTE GATE
PASSED, MODEL ATTRIBUTION NOT:** the native seat and two Claude CLI seats
completed, route provenance is truthful, and the native seat was served by an
in-session subagent, not a shell: the app's Tasks pane showed four external
shells (the two codex-channel seats and the two Claude voices) and one Subagent for `cursor-composer`,
and its result entered through the scribe transport rather than a provider
result file. That is UI plus run-record evidence, not the PATH-shim artifact
the plan specified, so it is recorded as such. What this run could NOT show is
which model answered: its badge was not captured, and the string it recorded
(`composer-2.5`) is one the Task path later refused outright, so the plan's
oracle criterion (observed requested-model provenance) is unmet here.

**LIVE-REVIEW CRITERION SATISFIED by `run-559dd5d1899d` (2026-08-25):**
native `cursor-composer` at `composer-2.5-fast` with the app's chip reading
exactly `Composer 2.5 Fast`, plus `opus` through the external `claude` CLI;
both ok, quorum 2 of 2 MET, synthesis settled, and again one Subagent and one
external shell in the Tasks pane. That run meets the roadmap's live-review
criterion (a native seat and a Claude CLI seat, truthful route provenance, no
`cursor-agent` for the native seat) AND the plan's model-oracle criterion in
one observation; the c739 run stands as the first route-provenance pass. It is
NOT phase acceptance: still owed are the installed-plugin validation through
Cursor's marketplace refresh path, the installed Claude review regression run,
the P12 and P13 captures (P12: an app-versus-`cursor-agent` environment
discriminator, a marker present and non-empty only in the app's agent shell;
P13: whether the Cursor hook process sees those markers), `readonly`
enforcement on the app, and the seat-pin decision recorded in the model-grammar
section.

What this run settles: the role name resolves from the filename stem, and the
driver's Task fence spawns the native reviewer (both previously the page's
unverified assumptions). What neither run settles: `readonly: true` enforcement
on the app (no reviewer attempted a write) and the P12 and P13 environment
captures. The model badge is settled by `run-559dd5d1899d` above.

Anomaly. Root cause: the `codex` binary on this machine, not Cursor and not
the codex service (verified live, 2026-08-25). BOTH codex seats hit the 540s provider timeout with
no output, and in the same window a codex review seat on the Claude Code host
timed out at 1800s and `crew probe codex` there failed at its 1800s ceiling.
The `codex` Homebrew cask had been upgraded to 0.149.1 that evening (the old
version dir was left as `0.147.0.upgrading/`, an interrupted upgrade), and the
new binary hangs at startup in EVERY context: `codex --version` never returns,
sandboxed or not, with a clean env, with an empty `CODEX_HOME`, under a pty,
with stdio redirected to files. It sleeps (state S) with no files or sockets
open. Its signature verifies valid, quarantine was removed, Apple's policy
endpoints answer, no endpoint-security agent is installed, and no keychain or
TCC prompt was pending. The previous binary at
`/opt/homebrew/Caskroom/codex/0.147.0.upgrading/bin/codex` launched
instantly, and relinking `/opt/homebrew/bin/codex` to it restored every codex
seat (`crew probe codex` 6s, a full review 275s). That relink lasted minutes: a
`brew reinstall` at 21:01 re-pointed the link to 0.149.1 and deleted the
`.upgrading` dir, so a Caskroom relink is NOT a durable fallback. The durable
one, in place since 2026-08-25: the 0.147.0 package extracted from Homebrew's
download cache to `~/.local/opt/codex-0.147.0/` with `~/.local/bin/codex`
symlinked to its `bin/codex`; `~/.local/bin` precedes `/opt/homebrew/bin` on
PATH, so it shadows whatever the cask links until removed
(`rm ~/.local/bin/codex`). Recheck `codex --version` after any brew activity
anyway. Both hosts spawn the same `codex` from PATH, which is why it looked
like a service outage. Not a Phase 2 defect. On 2026-08-18 the same seats completed
inside Cursor in roughly 80s (`run-18675bf73d80`). When every codex seat times
out at once with EMPTY output, run `codex --version` with a short timeout
first; if it hangs, try the previous cask version's binary directly before
blaming a host or the change under review.

## App subagent model grammar is NOT the CLI's

Three model-string surfaces exist, and they differ. Sources, read 2026-08-25
(research-sourced except the rows that carry their own live tag): Cursor docs
https://cursor.com/docs/agent/subagents; forum threads
https://forum.cursor.com/t/parent-agent-overrides-subagent-model-settings-by-explicitly-passing-model-to-task-tool-it-used-all-of-my-api-budget/162601
and https://forum.cursor.com/t/subagent-model-choice-not-respected/163645, both
with replies from Cursor staff (Dean Rie).

| surface | accepted strings | on an unavailable model |
| --- | --- | --- |
| CLI `cursor-agent --model` | the full flattened catalog (`composer-2.5`, `composer-2.5-fast`, `claude-opus-5-thinking-high`, ...) PLUS bracket overrides: `cursor-agent --help` documents `'claude-opus-4-8[context=1m,effort=high,fast=false]'`, and a live `--model 'composer-2.5[fast=false]'` reported `init.model` `Composer 2.5` (verified live, 2026-08-25, cursor-agent 2026.08.11-e8db854) | far-miss rejected; retired id fuzzy-matched down (above) |
| app Task-call `model` (the path crew's driver uses) | a per-session allowlist of ONE flattened variant slug per ENABLED family, e.g. `composer-2.5-fast`, `cursor-grok-4.6-high`, `gpt-5.6-sol-medium`, `claude-opus-5-thinking-high` | REFUSED: `composer-2.5` was refused at spawn with Composer 2.5 enabled in the picker, and the driver settled the seat `native_task_lost` (verified live, 2026-08-25, run `run-64dbfe045d0f`) |
| app frontmatter `model:` in `.cursor/agents/` | base id plus bracket parameters: `composer-2.5[]`, `composer-2.5[fast=false]`, `claude-opus-5[effort=high,context=300k]`; `inherit` is the default | "Cursor falls back to a compatible model" (docs), silently |

Staff guidance (forum 163645): "Control the variant using bracket syntax in
the `model` field: `model: composer-2.5[fast=false]` uses the normal non-fast
variant." The same thread records a user who configured `composer-2.5` and had
`composer-2.5-fast` run instead, and another whose Luna pin ran as Gemini;
staff called it "a known bug" with no ETA. Forum 162601 confirms the Task tool
carries a `model` argument that overrides the subagent file's frontmatter, and
that parents pass flattened slugs on it.

Consequences for crew:

- A seat pin from `seats.toml` (CLI grammar) is not valid on the Task-call path
  unless that exact variant is the one the app currently offers for its
  family. Enabling a model in the picker is necessary but not sufficient.
- The Task-call path is NOT reliably loud. The same bare slug `composer-2.5`
  was accepted once (`run-c73927a5f904`, answering model unobserved and very
  likely substituted) and refused outright later (`run-64dbfe045d0f`). A
  refusal is self-evidencing; a successful Task call is UNATTRIBUTED until its
  badge is read, so the badge check is required after every native spawn, not
  a spot check. Its vocabulary is one variant per family, chosen by the app.
- The two support roles share the mismatch: `CURSOR_SUPPORT_MODEL` in
  `review_workflow.py` pins the scribe and formatter to bare `composer-2.5`, so
  on the Task path the scribe is refused the same way the reviewer was. Run
  `run-559dd5d1899d`'s native result landed through the host-write FALLBACK for
  exactly that reason, while `run-c73927a5f904`'s went through the scribe. The
  fallbacks held, but the support pins are inside the open decision below.
- The frontmatter path honors variants through bracket syntax but substitutes
  SILENTLY when the model is unavailable, so a per-file design makes the badge
  oracle mandatory, not optional.
- OPEN DECISION (operator's): `seats.toml` pins are written in CLI grammar and
  feed BOTH surfaces, and `composer-2.5` is refused on the Task path today. The
  choices are surface-specific pins (keep per-call, repin cursor seats to the
  Task vocabulary such as `composer-2.5-fast`) or per-file bracket pins in the
  role files with a mandatory badge check. Until decided, the interim is the
  temporary `[seats.cursor-composer] model = "composer-2.5-fast"` table in
  `.crew/config.toml`, and the shipped pin is known to be refused natively.
  A review on 2026-08-25 put the gap precisely: the driver's submission records
  the REQUESTED pin and never the observed badge, so an accepted-but-substituted
  answer would enter quorum under the requested model's identity. Closing that
  gap is what the decision must do, by persisting an observed-model field the
  operator reads off the badge, by forcing external when attribution is
  unavailable, or by moving to per-file bracket pins; until then the badge
  check is a manual step the operator performs.
- The exit-gate run `run-c73927a5f904` recorded its native seat as
  `composer-2.5`; given the known bug, what answered was very likely
  `Composer 2.5 Fast`. Its badge was not captured and that row is treated as a
  substitution finding, not as a verified pin. In a later session the same
  string was REFUSED outright (`run-64dbfe045d0f`), so acceptance of a bare
  family id on the Task path is not even stable across sessions.
- FIRST VERIFIED PER-CALL PIN ON A CREW SEAT: with a config repin to the Task
  path's own slug (`[seats.cursor-composer] model = "composer-2.5-fast"` in
  `.crew/config.toml`), run `run-559dd5d1899d` spawned the native reviewer and
  the app's subagent chip read `cursor-composer reviewer  Composer 2.5 Fast`,
  an exact match to the requested slug, attributed to that subagent's own run
  (operator-reported with a UI capture, 2026-08-25). That is the P7 oracle,
  and it shows a per-call pin honored on the shipped reviewer role, which pins
  no model of its own and is the production shape. It is NOT the plan's P8
  measurement-2 control (a per-call override against a role file that pins a
  DIFFERENT model), which has not been run; only that control separates
  "per-call honored" from "no file pin, so the call's model was the only one on
  offer".

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
  Gemini; `crew probe` passes for cursor-gemini and for both default-panel
  cursor seats. Closes the cursor-gemini open item. No app-surface probe ran
- 2026-08-25, codex 0.149.1 cask upgrade: every codex seat on both hosts timed
  out with empty output; `codex --version` hung in every context (sandboxed or
  not, clean env, empty CODEX_HOME, pty, stdio to files) with no fds open and a
  valid signature; quarantine, TCC, keychain, Apple policy endpoints, and
  endpoint-security agents each ruled out; 0.147.0 launched instantly. Relinked
  to 0.147.0; probe 6s, review 275s
- 2026-08-25, Cursor app: Phase 2 exit-gate run `run-c73927a5f904`: route
  gate passed (native `cursor-composer` plus external opus/fable completed,
  both codex seats timed out; role-name-from-filename and the Task fence
  verified on the app); model attribution unmet, settled later by
  `run-559dd5d1899d`
- 2026-08-25, Cursor app: docs and forum establish that the Task-call model
  vocabulary is one variant slug per enabled family, distinct from the CLI
  catalog and from frontmatter bracket syntax. `composer-2.5` refused at spawn
  (`run-64dbfe045d0f`); repinned to `composer-2.5-fast`, the subagent chip
  read `Composer 2.5 Fast` (`run-559dd5d1899d`). P7 oracle confirmed on a
  crew seat; first verified per-call pin
