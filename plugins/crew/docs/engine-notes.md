# Engine Notes — Rationale, Decisions & History

> **Not auto-loaded.** This file is maintainer reference material — the WHY behind
> the contracts. The binding rules live in
> [`plugins/crew/scripts/CLAUDE.md`](../scripts/CLAUDE.md); this file only records
> the rationale/history/decision-gloss that used to sit inline there. Nothing here
> is a rule; if a statement below conflicts with scripts/CLAUDE.md, scripts/CLAUDE.md
> wins.

## Provider / seat model

- **Why provider = executor, and Claude seats are NOT providers.** This is the
  debate-validated shape: we deliberately did NOT adopt the Enterprise fork's
  prompt-builder `BaseProvider`/`TaskProvider`, which modelled non-executable
  Claude seats as providers. codex/agy/cursor-* are executors (they invoke a CLI →
  `ProviderResult`); opus/sonnet Task seats are owned by the orchestrator.
- **"One prompt builder" evolves an older contract.** The rule that `render`
  BUILDS the prompt for ANY seat (including Claude Task seats) *evolves* the older
  "the engine only knows subprocess seats" framing — the engine still EXECUTES
  only subprocess seats, but it BUILDS for all so the subprocess and Task prompts
  can never drift. The parity test in `test-multiagent.py` asserts
  `render` output == `prompts.build_prompt(...)`.

## agy seat re-validation (history)

`agy` was re-validated 2026-07-01: a 3-run `crew probe agy` = pass/pass/pass
(6–11s) — the CLI connects and follows instructions; historical empty/off-target
REVIEWS were prompt-level, not a dead seat. Re-probe before any future demotion
decision.

## Why the default panel leaves some cursor seats opt-in

The default panel is `codex + agy + cursor-auto/composer + opus + sonnet`.
`cursor-gpt`, `cursor-gemini`, and `cursor-glm` are registered but opt-in:

- `cursor-gpt` — codex already covers the GPT lineage, so it isn't defaulted.
- `cursor-gemini` — `agy` covers the Gemini lineage flat-rate, so the metered
  `cursor-gemini` is left opt-in.
- `cursor-glm` — glm-max draws on Cursor's shared premium MAX allotment, so it's
  opt-in too; `cursor-auto` fills that slot from the cheap/dedicated bucket.

## Why the debate panel uses `seats --debate`, not a `debate-prep` mirror

`/crew:debate` cannot call `review-prep` (it resolves its panel in the command
markdown), so `crew seats --debate` prints the FULL debate panel. Extending the
existing `seats` subcommand is the lightest hook — not a heavyweight `debate-prep`
mirror of `review-prep`. Note debate adds the `debate_panel` tier that
`review-prep` has no equivalent for.

## Why `crew state …` routes through the dispatcher instead of a plugin-root shim

`crew-state.py` does a bare `from models import …` with NO `sys.path` guard. A
plugin-root `crew-state.py` shim would put the plugin root (not `scripts/`) on
`sys.path[0]` and break that import. The `crew` dispatcher's guard already adds
`scripts/`, so routing `crew state …` through it resolves the import with NO edit
to `crew-state.py`.

## Why `debate` is scaffold-only

The old internal `_fan_out` branch inside `debate` was a killability split-brain
vs. `review-prep` (seats hidden in a thread pool instead of per-seat visible
shells). Making `debate` scaffold-only — it writes the dir + `question.md` + an
empty `subprocess.json` and never runs seats internally — removed that split-brain;
the single-round path now fans seats out per-seat with `run <seat>`, same as the
review-bearing commands.

## `collect --group` merge decisions (labels)

- **Decision-C** — a finding is NEVER dropped: any non-parseable seat is rendered
  verbatim in the RAW / UNPARSED SEATS section.
- **Decision-D** — merge predicate: severity is the only hard partition; within a
  severity, COMPLETE-LINKAGE clustering on
  `path_compatible AND line_compatible AND jaccard>=0.5`.

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

A `readonly: true` frontmatter key is NOT real — Claude Code ignores unknown
fields, so it's a no-op, not enforcement.
