---
description: Onboarding scaffolder for the crew ENGINE-TUNING config (the .toml, NOT CLAUDE.md) — detects installed provider CLIs and writes a commented ~/.crew-config.toml (or .crew/config.toml with a leading --repo), never clobbering an existing file silently.
argument-hint: "[--repo]"
allowed-tools: Bash, Read
---

[INIT — config scaffolder / provider detection]

$ARGUMENTS

> **What this is.** The onboarding companion to crew's two tuning files: the global
> `~/.crew-config.toml` and the per-repo `.crew/config.toml` that the engine's
> `config.py` loader reads (`default_panel`, `[debate].panel`, `[dispatch].seat`,
> per-seat `model`/`available`, `[tuning].timeout`, `[panels]`). It DETECTS which
> provider CLIs are installed, then writes a **commented** starter config so you
> discover the knobs without hand-authoring TOML. A fresh scaffold gets cost-safe
> defaults; `--repo` with an existing global `~/.crew-config.toml` instead seeds the
> per-repo file from that global verbatim (then lightly adjusts).

> **Not to be confused with `/crew:crew-config`.** `/crew:crew-config` copies the
> **`CLAUDE.md` operating-instructions doc** into `.claude/CLAUDE.md` (project) or
> `~/.claude/CLAUDE.md` (global) — it touches NO `.toml`. `/crew:init` (this command)
> scaffolds the **engine-tuning `.toml`** the `config.py` loader reads. Different files,
> different concerns.

> **`allowed-tools` scopes THIS orchestrator only.** Every file write goes through the
> engine: `doctor` writes `doctor.json`, `scaffold-config` OWNS the config write +
> the no-clobber invariant. The short interview answers ride as CLI flags, so nothing
> is spilled to a file — hence NO `Write` here. The diff in step 5 is produced by a
> REAL `diff` command via Bash (already granted), never an LLM-generated diff.

> **Detection is honest, NOT auth.** `doctor` proves a CLI is on PATH (and, for Cursor,
> that the `agent` binary is really Cursor) — it does NOT verify you are authenticated,
> and it makes NO billable/network call. Surface every present seat as "installed
> (auth unverified)"; never claim a seat is "ready" or "authed".

## Step 1 — Parse an optional LEADING `--repo`

`--repo` is recognized ONLY as the LEADING token of `$ARGUMENTS` (same place the other
crew commands look for flags). With `--repo`, the scaffold targets the per-repo
`.crew/config.toml`: if a global `~/.crew-config.toml` exists it is seeded VERBATIM
from that global (then lightly adjusted); if no global exists it gets a fresh
cost-safe-defaults scaffold. Without `--repo`, the scaffold targets the global
`~/.crew-config.toml` with cost-safe defaults.

## Step 2 — Detect installed providers (`doctor`)

Run the engine probe via the bare `crew` dispatcher. Substitute your actual session id
(the `[Session ID: …]` value) for `<session-id>` — pass it as a LITERAL `--session-id`
value, NEVER a `${CLAUDE_SESSION_ID}` shell expansion:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" doctor --session-id <session-id>
```

**Guard the result.** If the `doctor` invocation exits NONZERO, or
`.crew/reviews/<session-id>/doctor.json` is missing / empty / not readable JSON
(equivalently, stdout did not carry parseable JSON), SURFACE the error and STOP. Do
**NOT** proceed to `scaffold-config --detection` with a missing/empty file — that would
silently trip the engine's "detection omitted" fallback and emit a config with no honest
`available` flags.

On success, **Read** `.crew/reviews/<session-id>/doctor.json` (the same JSON is also on
stdout) and SHOW the detection table to the user — label each present subprocess seat as
"installed (auth unverified)", each absent one with its diag, and the task seats
(`opus`/`sonnet`/`fable`) as "subscription-backed (in-session)".

- **Optional follow-up: `probe`.** `doctor` proved each seat's CLI is installed; it did
  NOT prove any seat actually answers. If the user wants that stronger guarantee, offer
  `"${CLAUDE_PLUGIN_ROOT}/crew" probe --all` (or a subset of seat names) — it sends one
  trivial prompt through each seat's real CLI and reports pass/degraded/fail/skipped.
  This is BILLABLE (it runs each seat's real CLI once), so ask before running it — never
  run it unprompted.
  - **Exit-code contract** (a health check should branch on this, not just the status
    text): `0` = at least one seat passed; `1` = a seat failed/degraded; `2` = usage
    error OR every seat was skipped. Because `2` is overloaded (bad-usage AND
    all-skipped), a caller that gets `2` must ALSO read the JSON to tell a real usage
    error from an all-skipped (no-CLI-available) run.

## Step 3 — Short PREFERENCE interview (detect-then-CONFIRM)

Ask ONLY what detection can't infer. Keep it short:

1. **Confirm / correct availability.** The doctor table is detect-then-CONFIRM. Collect:
   - opt-INs (a seat detected absent that you actually have / will install) →
     `--add-seat <name>` (repeatable);
   - opt-OUTs / detection overrides (a detected-present seat you don't want, or a task
     seat like `opus` you don't want in your default panels) → `--disable-seat <name>`
     (repeatable).
   Either a subprocess seat OR a Claude task seat is a valid correction target; explicit
   correction beats detection.
2. **`default_panel`** (default `full`).
<!-- seat-roster:premium-add-back -->
3. **Opt-in add-back**: `cursor-glm`, `cursor-gpt`, `cursor-gemini`, `cursor-grok`, `codex-terra`
   default OFF (the cursor seats draw the premium/metered buckets; `codex-terra` is
   redundant since the two default codex seats already cover the OpenAI lineage). Offer them
   as opt-IN `--add-seat` only, with that note (per-seat WHY → engine-notes).
4. **`[dispatch].seat`** — the default `/crew:dispatch` WRITE seat (default `codex`;
   must be a subprocess seat).
5. **Declared seats** — mention (do not interview for it) that the generated config can
   also REGISTER brand-new seats: a `[seats.<name>]` table with `provider` (codex/cursor/
   agy/claude-code) and an explicit `model` adds a first-class seat with no code change;
   the README's "Add a model seat" section has the recipe.

## Step 4 — Scaffold the config

Call `scaffold-config` through the dispatcher. Pass `--repo` ONLY if step 1 saw it. Pass
the `doctor.json` you read as `--detection`, plus the interview answers as flags. Pass
**NO `--session-id`** (this writes the non-session-scoped config path) and **NO `--out`**
on the first pass (so it writes the resolved target) and **NO `--force`** (so an existing
target diverts to `<target>.new`):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" scaffold-config --detection .crew/reviews/<session-id>/doctor.json --default-panel <panel> --dispatch-seat <seat>
```

(Add `--repo` first when the user asked for it; append each `--add-seat <name>` /
`--disable-seat <name>` correction; the `--detection` path is the file `doctor` wrote.)

Read the JSON envelope `{target, wrote, diverted}` from stdout.

## Step 5 — No-clobber UX

- If **`diverted` is false**: the engine wrote the config directly (a fresh file). Report
  `envelope.wrote` and STOP.
- If **`diverted` is true**: an existing config was preserved and the proposal landed at
  `<target>.new` (`envelope.wrote`). Show the user what would change with a REAL diff,
  then ask them to confirm. Guard `diff` availability first:

```bash
command -v diff && diff -u <target> <target>.new
```

  If `diff` is ABSENT (a minimal environment), fall back to **Read**-ing `<target>.new`
  and showing its path + content instead of a unified diff (never error out). On the
  user's explicit confirm, RE-RUN the SAME `scaffold-config` command with `--force`
  appended (the engine owns the write + the clobber — no shell `mv`):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" scaffold-config --force --detection .crew/reviews/<session-id>/doctor.json --default-panel <panel> --dispatch-seat <seat>
```

  (carry the same `--repo` / `--add-seat` / `--disable-seat` flags). If the user declines,
  leave the live config untouched and point them at `<target>.new`.

## Step 6 — What's next

Point the user at the knobs: the commented config file they just got is the reference —
per-seat tuning (`model`, codex `reasoning_effort`, agy `print_timeout`, `[tuning].timeout`,
`[tuning].deadline_minutes`, the persistence loops' wall clock, 1-1440, or 0 for no deadline in the global file only)
and `[panels]` rosters are documented inline. For the project
operating-instructions doc (a different file), mention `/crew:crew-config`.

---

Subscription safety: `doctor` drives only `is_available()` (a PATH check per
external CLI; cursor = one local `agent --version` identity probe) — no `claude -p`, no Anthropic API, no
metered/network call. `scaffold-config` makes no probe at all.
