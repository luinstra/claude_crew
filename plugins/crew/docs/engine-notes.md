# Engine Notes — Rationale, Decisions & History

> **Not auto-loaded.** This file is maintainer reference material — the WHY behind
> the contracts. The binding rules live in
> [`plugins/crew/scripts/CLAUDE.md`](../scripts/CLAUDE.md); this file only records
> the rationale/history/decision-gloss that used to sit inline there. Nothing here
> is a rule; if a statement below conflicts with scripts/CLAUDE.md, scripts/CLAUDE.md
> wins.

## Provider / seat model

- **Providers own external execution; the resolver owns host placement.** The
  provider adapters invoke external CLIs and return `ProviderResult`. Claude
  catalog seats are native Task dispatches on a Claude host, but use
  `ClaudeProvider` through the claude CLI on hosts without a native Claude
  channel. This keeps the orchestrator's native ownership while making the
  external path explicit and testable.
- **"One prompt builder" evolves an older contract.** The rule that `render`
  BUILDS the prompt for ANY seat, including Claude Task seats, also supports
  external Claude seats. Host resolution decides whether the engine executes a
  seat or the orchestrator dispatches it, while the shared prompt builder keeps
  the two paths from drifting. The parity test in `test-multiagent.py` asserts
  `render` output == `prompts.build_prompt(...)`.

## agy seat re-validation (history)

`agy` was re-validated 2026-07-01: a 3-run `crew probe agy` = pass/pass/pass
(6–11s) — the CLI connects and follows instructions; historical empty/off-target
REVIEWS were prompt-level, not a dead seat. Re-probe before any future demotion
decision.

The shipped `agy` seat was retired 2026-08-25 (operator decision: the seat is
no longer in use and must not be enabled by default). The adapter, its `agy`
channel, and the `print_timeout` knob stay; an agy seat is reached through a
config-declared row (`[seats.<name>]` with `via = ["agy"]` and a `model`).
With no shipped seat named `agy`, the channel mints an `agy` GROUP TOKEN, so
`--seats agy` names a list (the declared agy seats), and with no declared agy
seat that list is empty and each entry point handles it differently: the debate
workflow raises `WorkflowError("no_seats")` exactly as standalone review, and
`review-prep` has no guard and emits an empty
manifest silently, which only `crew state begin-review` refuses downstream.
`[dispatch].seat = "agy"` resolves to `None` with the generic
`[dispatch].seat='agy' is not a known registered seat (...); ignoring` warning.
A `[seats.agy]` table is ignored in one of two ways: a row that declares `via`
or `provider` reaches `_validate` and warns `seat name 'agy' is already a panel
or group token (it resolves to a list of seats); ignoring it`, while a row with
no execution key (empty or tune-only) never reaches it, is parked in the
loader's `pending` bucket, and warns `[seats.agy] names no known seat and
declares no provider or via; ignoring it`. `crew probe agy` exits 2 with
`error: unknown seat 'agy'`, and `crew run agy` exits 2 with `error: unknown or
unregistered seat 'agy'; valid registered seats: ...`.

## Why some registered seats are opt-in

The default panel roster of record is scripts/CLAUDE.md + `seats.toml`'s `[panels]`
table (loaded via `seats.merged_panels()`)
(the two DEFAULT codex seats are distinct OpenAI voices, `gpt-5.6-sol` and
`gpt-5.6-luna`, on the one codex CLI — a deliberate same-lineage pairing at
different reasoning styles). Seats fall out of the default panel for two distinct
reasons, redundancy or cost.

Opt-in for REDUNDANCY (not bucket cost):

- `codex-terra`: the two default codex-family seats already span the OpenAI
  lineage, so a third OpenAI voice is redundant on every default review (run via
  `--seats codex-terra`).

Opt-in for COST (premium/metered Cursor buckets):

- `cursor-gpt` — codex already covers the GPT lineage, so it isn't defaulted.
- `cursor-gemini` (pinned `gemini-3.7-flash-high`, a Flash-class model) is a
  metered Cursor seat, not the flat-rate composer/auto bucket; its exact
  billing tier for a Flash model is UNVERIFIED. It stays opt-in because no
  default-panel slot has been chosen for it. Its earlier justification, that a
  flat-rate agy seat covered the Gemini lineage, went with the shipped agy seat.
- `cursor-glm` — glm-max draws on Cursor's shared premium MAX allotment, so it's
  opt-in too; `cursor-auto` fills that slot from the cheap/dedicated bucket.
- `cursor-grok` — cursor-grok-4.6-xhigh draws that same shared premium allotment,
  so it's opt-in for the same reason as `cursor-glm`. The pin must stay an exact
  advertised catalog id, and must be rechecked when the provider ships a new
  generation: `grok-4.5-xhigh` was valid until 4.6 replaced it, after which it
  was fuzzy-matched down to Cursor Grok 4.5 High instead of erroring, so the seat
  quietly ran a tier below its request while the result recorded the old pin.

## Why the interim multi-round section still uses `seats --debate`

The interim multi-round section of debate.md uses `crew seats --debate` because
that Markdown transport still resolves its panel before it issues its per-seat
actions. The single-round workflow passes `[debate].panel` into the shared
resolver and does not use this command path.

## Why `crew state …` routes through the dispatcher instead of a plugin-root shim

`crew-state.py` does a bare `from models import …` with NO `sys.path` guard. A
plugin-root `crew-state.py` shim would put the plugin root (not `scripts/`) on
`sys.path[0]` and break that import. The `crew` dispatcher's guard already adds
`scripts/`, so routing `crew state …` through it resolves the import with NO edit
to `crew-state.py`.

## Why debate is workflow-owned

The scaffold verb and the killability split-brain are gone because the review
seam issues one claimable action per seat, the same reason standalone review
never enters an ad-hoc fan-out. The engine therefore owns the single-round
question target, panelist actions, settlement, and advisory synthesis.

## Debate on the review seam

Debate gets a question target with an exact `question.md` snapshot because
the question, not a plan or diff, is the material every seat must receive.

Debate gets its own pointer because a shared standalone pointer would evict
an in-flight review and remove the resume and pruning protection for that run.

Formatter actions are suppressed because a panelist take is already the issued
reviewer-kind artifact and has no findings repair path.

Synthesis requires a null judgment because the advisory record must not
pretend to certify or revise a target.

The strict-majority header stays advisory and findings parsing is short-circuited because
a panel shape that depends on seat prose is not deterministic.

The inherited timeout envelope is reused because the same host transport and
settlement boundary apply to debate seats.

The existing external-channel policy stays because routing is one frozen
workflow decision, not a separate debate configuration source.

Debate stays under `.crew/reviews/<session>/<run>/` because the review seam's
validated record is the synthesis authority and a second debate tree would
duplicate unvalidated state.

## `collect --group` merge decisions (labels)

- **Decision-C** — a finding is NEVER dropped: any non-parseable seat is rendered
  verbatim in the RAW / UNPARSED SEATS section.
- **Decision-D** — merge predicate: severity is the only hard partition; within a
  severity, COMPLETE-LINKAGE clustering on
  `path_compatible AND line_compatible AND jaccard>=0.5`.

## Review-bearing command decisions (labels moved out of the command docs)

The `review`/`build`/`measure-twice` markdown used to carry these bare labels
inline. The RULE each names is still stated in the command docs; only the label
gloss lives here.

- **Decision-H** — persist EACH normalized Task seat through `crew persist-seat`
  with the prep JSON's `--run-id` (engine-owned filename + six-field core shape,
  stamped with the run identity from the run dir's own `run.json`, under the
  preserve-valid write rule; a catalog seat's name is its own slug), NOT a
  hand-rolled Write-tool `<seat>.json`. Without `--run-id` while a session
  pointer exists, `persist-seat` refuses (exit 2): completion-time attribution
  by mutable pointer is the stale-fold bug the run scoping closes.
- **Decision-I** — the WHOLE panel (subprocess AND Task seats) flows through ONE
  grouped `collect`, including the Claude-only branch (subprocess seats empty),
  which collects over the Task seats alone. There is NO "synthesize from raw Task
  returns" path.
- **Decision-J** — `<ran_seats>` order is `subprocess_seats` (resolved prep order)
  THEN the `task_seats` (verbatim: the `name == slug(name)` invariant means no dot-stripping).

## T3a reference-spawn (build and measure-twice history)

This section describes the still-current `review-prep` transport in build and
measure-twice. Standalone review no longer duplicates it in Markdown: Python
issues the reviewer prompt, exact primary scribe ingress, distinct fallback,
typed submission, formatter, and synthesis actions through `review_workflow`.

- **Reference-spawn (T3a).** The review-bearing commands spawn `crew:reviewer`
  Task seats with a REFERENCE to the prep-staged prompt file in the run dir
  (`Read <run_dir>/prompt-<seat>.txt and follow it exactly`, the path from the
  prep JSON's `task_prompt_paths`; the prompt points the seat at the run's
  frozen snapshot as the review authority) instead of pasting the staged
  prompt's contents inline — reference-not-payload applied to Task seats (the
  reviewer has `Read`). build.md also passes the executor summary as a PATH
  (`<run_dir>/executor-summary.md`), not inline.
- **Return side: scribe-mediated persist.** The seat still returns its full
  review block as the Task RESULT (the only completion signal). On the success
  path the orchestrator hands that returned text INLINE to a `crew:scribe`
  sub-agent (Write-only), which Writes it to the tmp-seat file inside its OWN
  transcript, so the persist-Write never renders back into orchestrator context.
  The orchestrator keeps the landing authority. build.md and measure-twice.md
  call `persist-seat --verify` and gate on its exit code for
  the landed record. The scribe's self-reported line is never the gate. On any
  scribe failure it falls back to
  Writing a DISTINCT `-fallback` path itself and persisting that, so a timed-out
  scribe's late write to the original tmp cannot clobber the persisted bytes. The
  reviewer and scribe are separate agents; the reviewer is read-only (no `Write`
  grant) — symmetric with `panelist.md`. Caveats stated honestly in `scribe.md`:
  the target path is PROMPT-pinned, not enforced, and verbatim fidelity is NOT
  byte-guaranteed (byte-verifying against the return would pull the full text back
  into context and defeat the terminal-hygiene purpose). (A reviewer-writes-its-
  own-return-file variant was prototyped and reverted: an unvalidated
  model-returned path plus stale-file risk outweighed the token saving.)

- **Why `--verify` exists.** The old grep gate was markdown prose enforcing an
  engine-checkable fact. Exit codes are allowlistable and un-driftable, while
  the grep string was neither, so all three review-bearing commands now gate
  the landed record through the flag.

## Reviewer / panelist seat — maintainer hardening note (moved from the agents)

This applies to both `crew:reviewer` and `crew:panelist`. Their read-only-by-
discipline posture assumes a **local, trusted repo reviewing its own changes**. If
crew is ever pointed at untrusted / external-contributor diffs, these seats
(general `Bash`, no sandbox) are the first thing to harden. The clean native paths
(verified against the Claude Code subagent frontmatter reference):

- a subagent-scoped `hooks:` PreToolUse gate that allows only read-only
  git/inspection and denies mutations (keeps `Bash`, affects only that seat);
- `disallowedTools: Write, Edit`;
- `isolation: worktree` (runs in a throwaway worktree — note that won't contain
  *uncommitted* changes, so it can't review a working tree).

A `readonly: true` frontmatter key does nothing ON THIS HOST: Claude Code
ignores unknown fields, so on a `claude` role file it is a no-op, not
enforcement. That is a fact about Claude Code and not about the key in general.
The Cursor adapters in `agents-cursor/` DO ship it, because the external
`cursor-agent` CLI was captured enforcing it; whether the in-session Cursor
surface honors it is unverified, and `docs/cursor-host.md` carries that
disclosure.

## Standalone review workflow (Phase 1)

Standalone `/crew:review` is now owned by the Python
`multiagent.review_workflow` module. The shared command is a finite transport
adapter: it passes the literal harness session id, launches Python-issued work,
and submits Python-issued HostResult files. Target intent, seat selection,
prompts, claims, repair admission, barrier, quorum, and terminal status do not
belong in Markdown or shared roles.

The command adapter is exposed for Claude and Cursor. Both may use a native
reviewer channel now (see the Cursor section below); a host with none, codex and
unknown, uses parent-context formatter work and keeps every seat external.
Codex-host all-external protocol compatibility is
covered deterministically through the Python CLI in this phase, but the Codex
plugin does not yet expose standalone `/crew:review`; its app-native adapter
remains deferred. Build and measure-twice continue to use `review-prep` until
their later workflow phases migrate them.

## Cursor host: the native reviewer channel, and why it took a design pass

Cursor-host seat execution WAS all-external, on the reasoning that the observed
per-file model frontmatter for Cursor subagents is static while crew's seats
need a per-spawn model pin. That objection was answered by evidence, not by
working around it: the app passes the model on the Task call, so one role file
serves every model the host can name and the pin rides the invocation. Standalone review
now issues cursor-channel seats natively on a Cursor host. Claude voices there
still run through the external `claude` CLI, and codex and agy stay external.

Three seams carry it, and the split between them is the point.

**The caller declares its route (`channels`).** `native_channel(host)` is the
host's TRUE in-session channel; `task_native_channel(host)` is the narrower
answer the unmigrated Task recipes and the subprocess runners share. Every
consumer passes one explicitly as `declared_native`, with no default, because a
caller that cannot perform native work must not be handed a native route. Prep
and `crew run` in particular have to classify one seat the same way: whatever
prep calls a subprocess seat, `crew run` has to be willing to run.

**The workflow decides per SEAT (`review_workflow.native_channel_for`).** A
channel-level answer is too coarse here. At `host=cursor` every `via=["cursor"]`
seat would resolve native, including one pinned to `auto`, and the run would
freeze it as `kind=task` before anything checked its native pin,
then issue a native action with no role to spawn. So `review_workflow` computes
the declared route per seat: the host's native channel when the seat has a
native pin, otherwise none. `channels` answers "is this channel native
on this host"; `review_workflow` answers "can this workflow drive a native
action for this seat", which depends on the host role table (`_HOST_ROLES`: the
reviewer role name plus `native_pin_field`, which is `model` on Claude and
`native_model` on Cursor) and so cannot live
anywhere else without moving review policy out of the engine. There is no
per-model allowlist: the reviewer role file pins no model, so `seats.toml`
supplies both surface-specific fields: `model` for the CLI route and
`native_model` for the Cursor Task route. Those pins are independent and must
be repinned independently.

**Native admission keys on `native_model`, not runtime attribution.** A
cursor-channel seat without a native pin is warned about and dropped before the
identity freeze, rather than being rerouted. A seat with a native pin resolves
to the host-native Task route; the shipped `cursor-composer` seat uses
`composer-2.5-fast`. A Cursor host does not also open that channel as a
subprocess during resolution, so a native seat has no CLI fallback. A roster
reduced to zero fails with the existing `no_seats` error rather than starting
an empty review.

The role names, the two support-role models, and the channel come from one
literal `_HOST_ROLES` table beside `_reviewer_action`, which replaced five
hardcoded Claude names and three validation mirrors. The TOTAL reviewer name (a
host whose one reviewer role file pins no model, so every model reaches it) is
a table field too, not a default inside the accessor: a name left in the method
is a name a new host inherits by saying nothing, which is how a Cursor row would
have silently answered with the Claude reviewer's name. It is a dict and an
accessor, not a registry: hosts are added by adding a row, and a host with no
row (codex, unknown) drives no native work, which is exactly what
`native_roles` returning `None` already meant.

**The drop rule is one predicate, and minting is fail-closed.** "This seat has
no route on this host" is asked in exactly one place (`has_no_route_here`) by
both the roster resolver and the drift reconstruction. Two copies of that
question is how a stale EXTERNAL cursor action survived on a Cursor host: the
per-seat native declaration returns `None` both for a seat without
`native_model` and for a host with no native channel at all, so the drift check could not tell the two
apart and re-ran an action against a CLI this host does not use for its own
channel. Downstream of resolution, the reviewer and formatter mints REFUSE
(`unresolved_native_role`) before preparing any path when a native action's role
does not resolve. Resolution should make that unreachable; if the two ever
disagree, the honest outcome is a named refusal, never an action issued with
nothing to spawn or a role quietly stood in for.

**A support role is never a hard dependency for a roster that needs nothing
else from the host.** The formatter mint asks only whether the host HAS a role
row, so on a Cursor host an all-external roster (`--panel quick` is codex plus a
Claude voice, both external there) still mints a native `crew-formatter` at the
support model. That model's exact slug has to be one the account's subagent
surface offers (family enabled, variant matching), and
the recorded live capture had every registered cursor seat model rejected on
that surface, so an off-schema seat could lose its repair to a role the roster
never asked for. The fix is a reroute, not a mint-time gate: `formatter_task_lost`
recovery rewrites the action to `driver=parent`, the route codex and unknown
hosts already take, keeping the action id and every path. Two alternatives were
rejected. Gating the mint on "does the roster contain a native seat" would have
demoted a Claude host's `--seats codex` panel to a parent-context repair for no
gain, and would still leave entitled seats with an unentitled support model
hard-failing. Pinning the Cursor support roles unspawnable would encode one
account's current enablement as a permanent design fact, which is exactly what
the P8 evidence says it is not.

The formatter is the ONE action that changes route this way; a SEAT never does.
A seat's answer belongs to the model that gave it, so `channels` never
reroutes a resolved seat once it starts and a lost native reviewer settles
FAILED rather than moving transports. The formatter only reshapes an answer
already given, and its output must still satisfy `findings.parse_seat` before it
is accepted. The scribe needs no equivalent: it exists only on a NATIVE reviewer
action, so the host is already that seat's dependency, and it has the host-write
fallback besides. Mint, reroute, and validation all read one `_formatter_route`
table so a rerouted action cannot describe a route no mint could produce, and
the formatter reroute happens at most once by construction (the rerouted action
is PARENT, whose only recovery code settles it).

**A lost native reviewer settles failed, and the panel degrades.** The mint-time
refusal and the roster drop both cover seats resolution can SEE are unroutable.
A seat that resolves native and is then refused at spawn is a different failure,
and there is nowhere honest to send it: its own channel is one this host drives
in-session rather than as a subprocess, which is the same routing policy
`has_no_route_here` enforces at resolution. So `native_task_lost` recovery
settles the action, quorum recounts the usable seats, and the digest synthesizes
from whatever returned.

A general retry policy that turned lost native actions into external ones was
built and removed, for two reasons, neither of them a capability limit. The
first is the operator's: work policy forbids them from using the `cursor-agent`
CLI, and the app is the only permitted surface there, so a per-action fallback
onto that CLI is a route they cannot take. The binary is present and
authenticated in that environment, as the drop-rule paragraph above records from
a live capture, which is why this has to be stated as the permission it is and
never as a missing client. The second is this codebase's routing and
attribution policy, the one `has_no_route_here` already enforces: a channel the
host drives in-session is not also opened as a subprocess, because a seat's
answer belongs to the model that gave it and moving transports mid-action would
change which surface produced it. On Claude the retry was a live transport and
billing change for the ordinary Task-idle case that nothing asked for. Ordered
`via` fallback is the seam that owns per-action retry, and it stays deferred; a
per-action retry ahead of it would have prejudged the design.

**The channel-level opt-out is `[review].force_external_channels`.** What a
per-action retry could not honestly do, a RUN-level choice can: naming a channel
here makes standalone review resolve every seat on it to its ordinary external
provider, and drives nothing in-session on it at all. Resolution follows the
usual chain, CLI `--force-external` over per-repo `.crew/config.toml` over
global `~/.crew-config.toml` over the built-in (force nothing), so native
admission stays the default and this
is the escape hatch. It is a config file rather than an env var deliberately: the
Cursor agent shell every crew command runs in scrubs operator exports, so an
env-tier opt-out would be unreachable on the host that most needs one. The
resolved choice is frozen into the run identity (`force_external_channels`) and
every later step rebuilds its route from there, so editing the file mid-run
cannot change how an already-minted run is judged, and the record says which
route the answers came from.

The reason it exists NOW, rather than staying deferred with ordered `via`, is
that the cursor-native route replaced a route verified live with one resting on
two app-surface assumptions that were unverified when it shipped (the role
name resolving from the filename, and the app's Task spawn form; both were
verified live on 2026-08-25 by the exit-gate run recorded in `cursor-host.md`). On the operator's only permitted surface that is the concrete
use case the deferral was waiting for.

**Native stays the DEFAULT anyway, and the opt-out is an opt-out.** The obvious
counter-move to two then-unverified assumptions is to invert the default and make
in-session cursor seats opt-IN. It was rejected on the one fact that decides it:
the live-verified alternative those seats would fall back to is the
`cursor-agent` CLI, and the operator's work policy forbids that CLI. Forcing
every cursor channel external by default would therefore ship a default that
resolves to nothing runnable on the exact host it exists for, trading assumptions
that may hold for a certainty that they cannot. Verified-elsewhere is not the
same as usable-here. The assumptions were expected to fail LOUDLY (a seat
refused at spawn settles failed and the digest names it). The 2026-08-25 app
runs qualified that: a refusal is loud, but the same slug was also accepted
once with its answering model unobserved, so a native spawn can succeed without
a runtime report. The attribution gate is now a non-gating runtime stamp:
external stream-json captures `init.model` as `reported_model`, and the digest
renders the derived `model_attribution` without changing quorum. The
surface-specific pin decision is recorded in `docs/cursor-host.md`, and the cost of
being wrong is still one panel, recoverable by naming the channel in the
opt-out. So the default optimizes for the host that has to use
it, and `crew review` prints one stderr note there naming
`[review].force_external_channels` when a run actually mints native cursor
seats, so the escape hatch is learned at the point it is needed rather than from
this document. Do not re-derive this trade as a defect; the app-surface
captures landed on 2026-08-25 and confirmed both assumptions while qualifying
the loud-failure premise as above, and any further contradiction is a different question
with different evidence.

Support roles follow the seats: with a
channel forced external the formatter mints on the parent-context route directly
rather than being minted native and needing a recovery call every run, and the
scribe never mints at all because it rides only a native reviewer action.

The consequence to state plainly is a SETUP one, not a code defect. The shipped
`native_model` pin is `composer-2.5-fast`; the Cursor Task surface still must
offer that exact variant for the account. A cursor seat without that pin is
warned and dropped before the signature freeze, leaving only its external CLI
route. A pinned seat whose family or variant is unavailable resolves native,
is issued native, and can be refused at spawn. There is no CLI fallback after a
native seat is issued, so a cursor-only panel with no available pinned seat
returns nothing usable. Enabling the families in Cursor is the mitigation.

**Finish or abandon an in-flight standalone review before adopting these bytes,
on EVERY host, Claude included.** Two guards catch a run frozen by an older
shape, and both are guards working rather than defects; the remedy is a fresh
review, never a migration.
- The run identity gained `force_external_channels`, and the identity key set is
  validated exactly. A run frozen without it fails validation as
  `corrupt_workflow` at its next command, and an identical restart mints a
  different digest and returns `conflict`.
- A formatter action minted before the reroute mark existed fails the action
  key-set check as `corrupt_workflow` too.

A Cursor host has two further in-flight cases of its own (a run frozen
`host=unknown`, and one frozen `host=cursor` with external seats); they are in
`docs/cursor-host.md`, because only that host can reach them.

### Native pins are a second field, and attribution is stamped rather than gated

The host role table names the reviewer role and the field that supplies an
in-session pin. `HostRoles.native_pin` returns `model` on Claude and
`native_model` on Cursor. An unset Cursor `native_model` means no native route,
so resolution warns and drops the seat; `auto` is refused at catalog load.
Duplicate Cursor seats at one native pin drop the later seat in resolution
order. The frozen signature key remains `model`, and it records the pin the
seat actually spends. `_spent_model`, `task_seat_models`, and the reviewer
action all use that value. The support pin stays host-level at
`composer-2.5-fast`, the two support role files carry no model frontmatter,
and an in-session execution without a pin is refused before any run directory
exists.

External Cursor results capture `system/init.model` as raw `reported_model`
through stream-json on the read-only branch. Both stream branches use one
extraction rule: terminal result text wins when a terminal result was seen,
otherwise joined assistant text is used, ANSI is stripped, and no other
normalization is added. The text printer's trailing linefeed is not reproduced.
The model report is retained on failures when the init event arrived and is
never compared with the requested pin.

`model_attribution` is derived from `reported_model`: a non-empty report is
`runtime-reported`, otherwise it is `requested-only`. The pair is stamped on
run-scoped result files beside `run_id` and `target_sha256`, copied onto
reviewer actions at external settle and reviewer recovery, and rendered in the
digest. Native reviewer actions are `requested-only` at mint. The fields are
optional on read, and flat results may carry `reported_model` but never the
stamp. The validator rejects a stored run-scoped stamp that disagrees with its
derivation. Formatter and synthesis actions remain unchanged. Quorum continues
to use `ok` only: the attributed count is display-only and never gates a panel.

The rejected alternatives each fail for a concrete reason. A seat with no
native pin has no permitted native route on the policy-forbidden-CLI machine,
so resolution drops it. Bracket pins in frontmatter use a surface that
silently substitutes. Excluding unreported seats from the quorum numerator
would create two quorum consumers that must agree forever and would make the
real panel routinely fail. A verified-pin tier or ledger could stamp a later
run from a past session's observation. `--observed-model` on submit is
unfillable for unattended work. A mismatch branch is dead on native and fuzzy
on external. Digit-suffix and strict-prefix heuristics are speculative once
the badge-verified pin is the contract. Deriving the support pin from a seat
row makes the formatter disappear when no Cursor reviewer is in the roster.
Stamping flat results would make a claim about no run.

One known cost is left visible: a native-only roster of one seat meets strict
majority quorum trivially because `len(expected) // 2 + 1` is 1. The digest's
attributed count is the available signal, and no arbitrary floor is added.
An in-flight standalone run frozen before these bytes fails action validation
at its next command because the action key set is exact. Finish or abandon it
first, then start a fresh review.
