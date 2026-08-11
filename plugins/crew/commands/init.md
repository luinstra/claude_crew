---
description: Onboarding scaffolder for the crew ENGINE-TUNING config (the .toml, NOT CLAUDE.md) — detects installed provider CLIs and writes a commented ~/.crew-config.toml (or .crew/config.toml with a leading --repo), never clobbering an existing file silently.
argument-hint: "[--repo]"
allowed-tools: Bash, Read
---

[INIT — config scaffolder / provider detection]

$ARGUMENTS

> **What this is.** The onboarding companion to crew's two tuning files: the
> global `~/.crew-config.toml` and the per-repo `.crew/config.toml` read by
> the engine's `config.py` loader (`default_panel`, `[debate].panel`,
> `[dispatch].seat`, `[dispatch].timeout` (default 1800 seconds for dispatch WORK
> only; provider floors raise the effective timeout only when the resolved
> `[dispatch].timeout` is below the floor; agy's floor is its print timeout plus
> grace, about 8 minutes by default, so the 1800-second default is not floored),
> `[dispatch.<kind>]`,
> per-seat `model`/`available`,
> `[tuning].timeout` (review, council, run, and probe seats), `[build].executor`, `[build].executor_retries`,
> `[build].resume_executor` (default ON; `false` opts out; codex/cursor only),
> `[panels]`). It DETECTS installed provider CLIs, then writes a **commented**
> starter config. Not to be confused with `/crew:crew-config`, which copies
> the CLAUDE.md operating-instructions doc and touches NO `.toml`.

> **`allowed-tools` scopes THIS orchestrator only.** Every file write goes
> through the engine (`doctor` writes the anchored `doctor.json`;
> `scaffold-config` owns the config write + the no-clobber invariant), so
> there is NO `Write` here. The diff in step 5 is a REAL `diff` command, never
> an LLM-generated diff.

> **Detection is honest, NOT auth.** `doctor` proves a CLI is on PATH (and,
> for Cursor, that the `agent` binary is really Cursor); it does NOT verify
> auth and makes NO billable/network call. Surface every present seat as
> "installed (auth unverified)"; never claim a seat is "ready" or "authed".

## Step 1 — Parse an optional LEADING `--repo`

`--repo` is recognized ONLY as the LEADING token of `$ARGUMENTS`. With
`--repo`, the scaffold targets the per-repo `.crew/config.toml`: seeded
VERBATIM from an existing global `~/.crew-config.toml` (then lightly
adjusted), or a fresh cost-safe scaffold when no global exists. Without
`--repo`, it targets the global file with cost-safe defaults.

## Step 2 — Detect installed providers (`doctor`)

Substitute your actual session id (the `[Session ID: …]` value) for
`<session-id>` as a LITERAL, never a `${CLAUDE_SESSION_ID}` expansion:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" doctor --session-id <session-id>
```

`doctor` prints the detection JSON to **stdout** (the machine payload; build
the table from it). Its JSON carries `session_segment`, the sanitized dir
stem the engine actually wrote to: substitute THAT (never the raw session id)
for `<session_segment>` in the `--detection` paths below. `doctor` also
writes the same JSON to the anchored
`<project-root>/.crew/reviews/<session_segment>/doctor.json`; step 4 names it
as the plain RELATIVE `.crew/reviews/<session_segment>/doctor.json`, which
the ENGINE anchors to the same project root it wrote under (never the shell
cwd; no `${…}` expansion on the line, which would defeat permission
allowlisting). Only a NON-engine read (Read tool, `cat`) needs the absolute
path.

**Guard the result.** If `doctor` exits NONZERO or its stdout is not
parseable JSON, SURFACE the error and STOP. Do NOT proceed with a
missing/empty payload: that would silently trip the engine's "detection
omitted" fallback and emit a config with no honest `available` flags.

On success, SHOW the detection table: each present subprocess seat as
"installed (auth unverified)", each absent one with its diag, the task seats
(`opus`/`sonnet`/`fable`) as "subscription-backed (in-session)".

- **Optional follow-up: `probe`.** For a stronger does-it-answer guarantee,
  offer `"${CLAUDE_PLUGIN_ROOT}/crew" probe --all` (or a seat subset). It is
  BILLABLE (one real prompt per seat): ask before running, never unprompted.
  Exit-code contract: `0` = at least one seat passed; `1` = a seat
  failed/degraded; `2` = usage error OR every seat skipped (overloaded, so on
  `2` also read the JSON to tell which).

## Step 3 — Short PREFERENCE interview (detect-then-CONFIRM)

Ask ONLY what detection can't infer:

1. **Confirm / correct availability.** Opt-INs (a seat detected absent that
   you actually have) via `--add-seat <name>`; opt-OUTs (a detected-present
   seat you don't want, or a task seat to keep out of default panels) via
   `--disable-seat <name>` (both repeatable). Explicit correction beats
   detection.
2. **`default_panel`** (default `full`).
<!-- seat-roster:premium-add-back -->
3. **Opt-in add-back**: `cursor-glm`, `cursor-gpt`, `cursor-gemini`, `cursor-grok`, `codex-terra`
   default OFF (the cursor seats draw the premium/metered buckets; `codex-terra` is
   redundant since the two default codex seats already cover the OpenAI lineage). Offer them
   as opt-IN `--add-seat` only, with that note (per-seat WHY → engine-notes).
4. **`[dispatch].seat`**: the default `/crew:dispatch` WRITE seat (default
   `codex`; must be a subprocess seat).
5. **Declared seats**: mention (don't interview) that the config can REGISTER
   brand-new seats via `[seats.<name>]` with one-element `via` + explicit `model`
   (`provider` remains the accepted legacy spelling); the README's "Add a model
   seat" section has the recipe.

## Step 4 — Scaffold the config

Pass `--repo` ONLY if step 1 saw it; append the interview corrections
(`--add-seat` / `--disable-seat`). `--detection` reads the file `doctor`
already wrote (no orchestrator spill). Pass **NO `--session-id`** (config is
not session-scoped), **NO `--out`**, and **NO `--force`** on the first pass
(an existing target diverts to `<target>.new`):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" scaffold-config --detection ".crew/reviews/<session_segment>/doctor.json" --default-panel <panel> --dispatch-seat <seat>
```

Read the JSON envelope `{target, wrote, diverted}` from stdout.

## Step 5 — No-clobber UX

- **`diverted` false**: a fresh file was written. Report `envelope.wrote` and
  STOP.
- **`diverted` true**: the existing config was preserved; the proposal landed
  at `<target>.new`. Show a REAL diff, guarding availability first:

```bash
command -v diff && diff -u <target> <target>.new
```

  If `diff` is ABSENT, fall back to **Read**-ing `<target>.new` and showing
  its path + content (never error out). On the user's explicit confirm,
  RE-RUN the SAME `scaffold-config` command with `--force` appended (the
  engine owns the clobber; no shell `mv`):

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" scaffold-config --force --detection ".crew/reviews/<session_segment>/doctor.json" --default-panel <panel> --dispatch-seat <seat>
```

  (carry the same `--repo` / `--add-seat` / `--disable-seat` flags). If the
  user declines, leave the live config untouched and point at `<target>.new`.

## Step 6 — What's next

Point the user at the knobs, documented inline in the file they just got:
per-seat tuning (`model`, codex `reasoning_effort`, agy `print_timeout`),
`[dispatch].timeout` (default 1800 seconds, dispatch WORK only; provider floors
raise the effective timeout only when the resolved value is below the floor;
agy's floor is its print timeout plus grace, about 8 minutes by default, so the
1800-second default is not floored),
`[tuning].timeout` (review, council, run, and probe seats), `[tuning].deadline_minutes` (the persistence loops' wall
clock, 1-1440, or 0 for no deadline, global file only), `[build].executor` /
`executor_retries` / `[build].resume_executor` (default ON; `false` opts out;
codex/cursor only), the `[dispatch.<kind>]` write-mode options
(`"${CLAUDE_PLUGIN_ROOT}/crew" dispatch --options` lists every key,
non-billable), and `[panels]` rosters. For the operating-instructions doc (a
different file), mention `/crew:crew-config`.

---

Subscription safety: `doctor` drives only `is_available()` (a PATH check per
external CLI; cursor = one local `agent --version` identity probe). No
`claude -p`, no Anthropic API, no metered/network call. `scaffold-config`
makes no probe at all.
