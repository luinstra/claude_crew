<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/claude_crew-logo-dark.svg">
    <img alt="claude_crew" src="assets/claude_crew-logo-light.svg" width="440">
  </picture>
</p>

A Claude Code plugin for persistence, specialized agents, and tech-stack guidance.

## What It Does

- **Persistence**: loops persist toward completion (they won't stop on a premature stop), and also end on a completing verdict, your cancel, or a safety bound (build & measure-twice loops)
- **Specialized Agents** — Delegate to focused agents for specific tasks
- **Tech-Stack Skills** — Auto-injected guidance for Kotlin, Exposed, Gradle, Python (via `sk` plugin)
- **Session Restoration** — Resume where you left off after restarts

## Hosts

- **Claude Code** is the full experience: 8 agents, Stop-enforced persistence loops, and sk stack detection.
- **Codex** is supported; see [`plugins/crew/docs/codex-host.md`](plugins/crew/docs/codex-host.md).
- **Cursor** is supported for the one-shot flows (review, dispatch, debate; commands import and hooks deliver). Persistence loops are guarded off whenever the host is identified as Cursor (stop-coercion there is unproven); until automatic Cursor detection ships, that guard relies on the `CREW_HOST=cursor` binding from the launch shell, and an unbound session reads as an unknown host and is NOT guarded. Subagent-dependent commands are unsupported. See [`plugins/crew/docs/cursor-host.md`](plugins/crew/docs/cursor-host.md).

## The Workflow

The intended path for real work, in order:

1. **Frame and discuss the problem** in plain conversation. No commands yet, just get the shape of it right.
2. **`/crew:debate "question"`** at a decision point you're unsure about. A multi-model council argues it out and hands back where the seats agree, where they don't, and a recommendation.
3. **`/crew:measure-twice "task"`** to loop on the *plan document*, revising until the loop completes (normally a review panel's completing verdict, or a human `--force` over an advisory — see the panel section below).
4. **`/crew:build "task"`** to loop on *executing* that plan, persisting until the loop completes (same completing-verdict-or-`--force` rule).

Steps 2 and 3 are cheap next to step 4. A decision settled in a debate costs one round; the same decision re-litigated mid-build costs a rewrite.

Steps 3 and 4 also protect your context. Work done directly puts every file read, grep result, and edit diff into your context window, which drives frequent compaction and lost continuity. Delegating to an agent keeps that noise in its context, and you get back the summary.

Not every task earns all four. A one-line fix earns none of them: just make the change.

## Installation

### From GitHub (Recommended)

```bash
# Add the marketplace
/plugin marketplace add luinstra/claude_crew

# Install crew plugin (agents, commands, hooks)
/plugin install crew@claude-crew

# Install tech-stack skills (optional)
/plugin install sk@claude-crew
```

### From Local Directory (Development)

```bash
git clone https://github.com/luinstra/claude_crew.git
cd claude_crew
# Install from plugins directory
cd plugins/crew && /plugin && cd ../..
cd plugins/sk && /plugin && cd ../..
```

The plugins activate automatically when Claude Code starts.

### Recommended: Allow List Entry

To avoid permission prompts for loop state management, add this to your Claude Code settings:

```json
{
  "permissions": {
    "allow": [
      "Bash(*/crew *)"
    ]
  }
}
```

This allows the plugin-root `crew` dispatcher to run without confirmation. A
single `*/crew *` rule covers BOTH the review/debate engine (`crew render`,
`crew run`, `crew seats`, `crew debate`) AND loop state management
(`crew state …`, used by `/crew:build`, `/crew:measure-twice`, and their cancel
commands). A project-relative variant proven in live use is
`Bash(plugins/crew/crew:*)`, which matches the dispatcher invoked by its
repo-relative path.

> **Migrating from an older install?** Earlier versions invoked
> `…/scripts/multiagent/cli.py` and `…/scripts/crew-state.py` directly. The
> shipped commands now call the bare `…/crew` dispatcher instead, so replace any
> `Bash(*/crew-state.py *)` (and `Bash(*/cli.py *)`) allowlist rule with the
> single `Bash(*/crew *)` above. The old direct paths still work (nothing forbids
> them), so an un-migrated allowlist won't hard-break — but the shipped commands
> now emit only `crew`, so the new rule is what they need.

### Requirements

- **Python 3.11+**: the engine parses its TOML config with stdlib `tomllib`, which lands in 3.11. macOS 13+ and most modern Linux distributions include it. Check with `python3 --version`; below 3.11 the `crew` engine exits with a one-line diagnostic.

## Commands

### Core Workflow

| Command | Description |
|---------|-------------|
| `/crew:plan "description"` | Start a planning session with the advisor agent |
| `/crew:execute "task or plan"` | Execute a task or plan via executor agent (keeps main context clean) |
| `/crew:review "the plan \| the diff"` | Multi-model review of a plan OR code diff (natural-language dispatch) → `APPROVED`/`REVISE` verdict |
| `/crew:debate "question"` | Crew-native council — single-round multi-model take on a question (the default review panel), synthesized into agreement/disagreement/recommendation |
| `/crew:dispatch "[--seat <name>] <task>"` | Delegate a WORK task to ONE non-Claude seat (default `codex`) in write mode — it edits the working tree and leaves changes UNCOMMITTED + UNSTAGED for you to review (keep / revert / pipe into `/crew:review`) |
| `/crew:build "task"` | Start a persistence loop: persists toward completion, also ending on a completing verdict, your cancel, or a safety bound |
| `/crew:cancel-build` | Exit an active build loop early |
| `/crew:measure-twice "task"` | Start a self-refining plan loop — generates plan, reviews, revises toward loop completion (a panel's completing verdict, or a human `--force`) |
| `/crew:cancel-measure-twice` | Exit an active measure-twice loop early |

### Utilities

| Command | Description |
|---------|-------------|
| `/crew:code-search "query"` | Search codebase via reader agent |
| `/crew:analyze "target"` | Deep analysis via advisor agent |
| `/crew:status` | Show active loops and crew state |
| `/crew:deepinit` | Initialize CLAUDE.md hierarchy for a codebase |
| `/crew:save-context` | Save context snapshot for session recovery |
| `/crew:restore-context` | Restore a previously saved context snapshot |

### Setup

| Command | Description |
|---------|-------------|
| `/crew:init` | Onboarding scaffolder for the engine-tuning config — detects installed provider CLIs, then writes a commented `~/.crew-config.toml` (or `.crew/config.toml` with `--repo`, seeded from the global) with cost-safe defaults + honest `available` flags. Never clobbers an existing file silently. |
| `/crew:crew-config` | Install crew's operating instructions for the current project: writes the crew-owned `.claude/crew-instructions.md` and links it from `.claude/CLAUDE.md` with one `@` import. Never overwrites your `CLAUDE.md`. |
| `/crew:crew-config global` | Same, against `~/.claude/` |

> **`/crew:init` vs `/crew:crew-config`** — different files, different concerns.
> `/crew:init` scaffolds the **engine-tuning `.toml`** (`default_panel`, panels, seats,
> `available`, `[dispatch].seat`, `[dispatch].timeout` (default 1800 seconds,
> dispatch WORK only; provider floors raise the effective timeout only when the
> resolved `[dispatch].timeout` is below the floor; agy's floor is its print
> timeout plus grace, about 8 minutes by default, so the 1800-second default is
> not floored; `--timeout` > `[dispatch].timeout` > builtin 1800),
> per-provider `[dispatch.<kind>]` dispatch tuning)
> that the `config.py` loader reads. `/crew:crew-config`
> installs the **operating-instructions doc** as the crew-owned
> `crew-instructions.md` (project `.claude/`, or `~/.claude/` for global) and links
> it from that scope's `CLAUDE.md` with a single `@crew-instructions.md` line: it
> touches no `.toml`.
>
> The two files are split by OWNER, deliberately. `crew-instructions.md` is crew's
> and is overwritten wholesale on every run, so re-running after an update is how
> you pick up changes and nothing goes stale. `CLAUDE.md` is yours and is never
> overwritten (crew appends at most that one import line). The command is strictly
> additive: it writes the crew-owned file, appends at most that one line, and
> deletes nothing.

### Multi-Model Review

`/crew:review` fans the same review prompt across a panel of models and
synthesizes one verdict. The built-in default panel and the presets:

<!-- seat-roster:default -->
- Default panel: `codex`, `codex-luna`, `agy`, `cursor-auto`, `cursor-composer`, `opus`, `sonnet`
<!-- seat-roster:preset:lite -->
- `--panel lite`: `opus`, `sonnet`
<!-- seat-roster:preset:solo -->
- `--panel solo`: `opus`
<!-- seat-roster:preset:quick -->
- `--panel quick`: `codex`, `sonnet`
<!-- seat-roster:opt-in -->
- Opt-in external seats (add via `--seats`): `codex-terra`, `cursor-gpt`, `cursor-gemini`, `cursor-glm`, `cursor-grok`
<!-- seat-roster:task-opt-in -->
- Opt-in Claude voice (add via `--seats`): `fable`
<!-- seat-roster:all -->
- Every registered seat: `codex`, `codex-luna`, `codex-terra`, `agy`, `cursor-gpt`, `cursor-gemini`, `cursor-glm`, `cursor-grok`, `cursor-auto`, `cursor-composer`, `opus`, `sonnet`, `fable`

These lines document the BUILT-IN roster; a configured `default_panel`/`[panels]`
override changes what actually runs. `"${CLAUDE_PLUGIN_ROOT}/crew" seats` prints
the resolved AVAILABLE seats that are external for the current host. Claude
voices appear in the Task split on a Claude host and in the external list on a
non-Claude host. If the `claude` CLI is missing, they remain listed and are
recorded as named skipped seats.

External seats run via the bundled `multiagent` engine
(`plugins/crew/scripts/multiagent/`): the two codex seats are distinct OpenAI
voices (`gpt-5.6-sol` and `gpt-5.6-luna`) on the one codex CLI, while Claude
voices use the native `crew:reviewer` Task path on Claude Code and the
read-only `claude` CLI elsewhere. It dispatches plan-vs-code from natural
language and reports `APPROVED`/`REVISE` with `[BLOCKING]`/`[MINOR]` findings.
A skipped or failed seat (any kind) is reported by name but never sinks the
panel: the verdict is synthesized from whichever seats succeed, and only an
all-seats-failed panel skips the verdict. A missing external CLI is a named
skipped seat; a CLI failure is a named failed seat. Hosts without a native
Claude channel do not emulate Claude seats with host-native subagents.
Certification is quorum-gated, though: the grouped digest opens with a
`PANEL: … quorum <n>: MET|NOT MET` header (a strict majority of the launched
panel), and a `NOT MET` panel cannot certify an `APPROVED`; the orchestrator
relaunches the pending seats or surfaces the shortfall instead. In the `/crew:build`
and `/crew:measure-twice` loops a human may still authorize completion over a
`NOT MET` panel with `--force`, which is recorded as an explicit override for the
audit trail.

**Why some seats are opt-in.** `codex` already covers the GPT lineage and `agy`
covers the Gemini lineage flat-rate, so the premium cursor seats stay off the
default: their `glm-max` and `grok-4.5-xhigh` models draw on Cursor's shared
premium MAX allotment, so the cheap/dedicated `cursor-auto` takes the default
slot. `fable` is a premium Mythos-class Claude voice, in no built-in default panel so
routine reviews never silently spend it; add it explicitly for the hardest calls
(falls back to the inherited model if Fable isn't on your plan).

**Choosing the panel.** `/crew:review`, `/crew:debate`, `/crew:build`, and
`/crew:measure-twice` all accept panel flags at the start of their argument:

- `--panel full` = the default panel listed above · `--panel lite`/`solo`/`quick`
  as listed above (`quick` is the cheapest cross-model pair)
- `--panel cursor` = all Cursor model-seats (`--seats cursor`, which the engine
  expands to every registered `cursor-*` seat) — a pure cross-model Cursor panel,
  no codex and no Claude Task seats
- `--seats <list>` — an explicit subset of the registered seats listed above
  (e.g. `/crew:build --seats codex,opus "fix the bug"`). The opt-in seats work
  via `--seats` (or `--panel cursor`) but are never in a built-in default panel

Not every change needs the full panel: `lite` (the two Claude voices, native
on a Claude host and external through `claude` elsewhere), `quick` (the
cheapest cross-model pair) for routine diffs, or a single seat is plenty for
routine work.

#### Per-repo config

`full` is the **built-in** default panel. An optional, personal per-repo
`.crew/config.toml` (already gitignored under `.crew/`) can override that default, tune per-seat
models (plus codex `reasoning_effort`, agy `print_timeout`) and the
NON-DISPATCH (review/council/run/probe) seat wall-clock `[tuning].timeout`,
while dispatch WORK has its own `[dispatch].timeout` wall-clock (default 1800
seconds; provider floors raise the effective timeout only when the resolved
`[dispatch].timeout` is below the floor; agy's floor is its print timeout plus
grace, about 8 minutes by default, so the 1800-second default is not floored), and declare brand-new model seats ([Add a model seat](#add-a-model-seat)). When you name no panel, `default_panel` resolves from
config, falling back to built-in `full`; the per-seat tuning knobs follow
**CLI flag > per-repo config > global config > built-in**. A malformed file is
ignored with a one-time stderr note, so a config typo never takes down a panel.

`/crew:debate` honors config too, with its own `[debate].panel` override so a
repo can default its debates fuller than its reviews (debate's value is
cross-model diversity). Debate precedence is **`--panel`/`--seats` (explicit) >
`[debate].panel` > `default_panel` > built-in `full`**.
`/crew:dispatch` picks its default seat from `[dispatch].seat` (validated
against the known seats — a panel name or group token like `cursor` is
rejected), falling back to the built-in `codex`; an explicit `--seat` overrides.
Per-provider write-mode dispatch tuning lives under `[dispatch.<kind>]` (keyed
by provider kind, keys declared by each provider; `crew dispatch --options`
lists them, non-billable).
`[dispatch].timeout` supplies the dispatch-WORK wall-clock. Provider floors raise
the effective timeout only when the resolved `[dispatch].timeout` is below the
floor; agy's floor is its print timeout plus grace, about 8 minutes by default,
so the 1800-second default is not floored. The NON-DISPATCH (review/council/run/probe)
seats use `[tuning].timeout`. Its precedence is
`--timeout` > `[dispatch].timeout` > builtin 1800.

> **Migration:** `[dispatch].timeout` is a new behavior split: `crew dispatch`
> no longer reads `[tuning].timeout` and now uses `[dispatch].timeout` (default
> 1800). If you previously raised `[tuning].timeout` above 1800 specifically to
> give dispatch WORK more time, set `[dispatch].timeout` instead, because raising
> `[tuning].timeout` no longer affects dispatch.
`/crew:build`'s implement step normally runs the `crew:executor` Task agent
(Claude); `[build].executor` can instead route each round through a write-capable
subprocess seat (e.g. `codex-luna`), and `[build].executor_retries` (0..2, default
0) caps how many times a FAILED external-executor round is retried. `[build].resume_executor`
(bool, default `true`) lets a supporting external executor REUSE its provider
conversation across rounds; set `false` to opt out and run each round fresh; only
codex and cursor resume, agy and any unsupported binary always start fresh
regardless of the toggle. On a fresh start with no active loop, the
`--executor <seat>` flag overrides `[build].executor` config for that invocation;
on a resumed build loop the resolution is active-loop stamp (`source: "state"`,
only when an active valid loop with a non-empty stamp matches the resolved
session) > `--executor` flag > `[build].executor` config > builtin, so a resumed
loop cannot drift its executor.

```toml
# .crew/config.toml
default_panel = "lite"            # default panel when you name none

[debate]
panel = "full"                    # /crew:debate-only default; falls back to default_panel, then full

[dispatch]
seat = "codex"                    # default seat for /crew:dispatch; validated vs known seats, falls back to built-in codex
# timeout = 1800                   # dispatch WORK wall-clock; provider floors raise
#                                   the effective timeout only when the resolved
#                                   [dispatch].timeout is below the floor; agy's floor is
#                                   its print timeout plus grace, about 8 minutes by default,
#                                   so the 1800-second default is not floored;
#                                   --timeout > [dispatch].timeout > builtin 1800

# Per-provider write-mode tuning, keyed by provider KIND (opt-in; unknown keys warn
# and drop; `crew dispatch --options` lists every declared key, non-billable):
# [dispatch.codex]
# profile = "<name>"   # codex exec --profile <name>: layers $CODEX_HOME/<name>.config.toml over the base user config
# [dispatch.cursor]
# force = true   # pass --force: allow commands unless explicitly denied (headless-write approval)
# approve_mcps = true   # pass --approve-mcps: auto-approve every configured MCP server

[build]
executor = "crew:executor"        # /crew:build implement-step executor; a write-capable seat (e.g. codex-luna) routes each round through that model
executor_retries = 0              # retries for a FAILED external-executor round (0..2); retries STACK on partial edits, so the cap is low
resume_executor = true             # default ON; codex/cursor reuse conversations, other executors always start fresh

[seats.codex]
model = "gpt-5.5"
reasoning_effort = "high"

[seats.agy]
print_timeout = "8m"

[tuning]
timeout = 600                     # NON-DISPATCH (review/council/run/probe) seat wall-clock seconds
deadline_minutes = 240            # the persistence loops' wall clock (1-1440); read by `crew state init`.
                                  # 0 = no deadline exists but is honored ONLY in the global
                                  # ~/.crew-config.toml (this repo file sits in the tree the agent edits)
```

#### Global per-user config

A second, optional file at `~/.crew-config.toml` (your home dir) carries the
same knobs for **every** repo, including the `[panels]` **roster** (redefine a
built-in preset or add a custom one) and per-seat `available` (opt-out for a
provider you haven't authed on this machine). Both of those work in EITHER config
file — set them globally in `~/.crew-config.toml` or per-repo in
`.crew/config.toml`; the per-repo value wins. Resolution is **per-key**: a
per-repo value wins over the global one, which wins over the built-in default
(`CLI flag > per-repo .crew/config.toml > global ~/.crew-config.toml >
built-in`). For `[seats.<name>]` tables this is per-seat-per-key, so a seat tuned
only in the global file still applies. Same TOML/one-time-stderr-note posture as
the per-repo file.

```toml
# ~/.crew-config.toml
default_panel = "lite"            # your machine-wide default

[panels]
full    = ["codex", "opus", "sonnet"]   # redefine a built-in preset's roster
nightly = ["codex", "opus"]             # add a custom preset (use via --panel nightly)

[seats.cursor-glm]
available = false                 # not authed here -> dropped from any panel;
                                  # an explicit --seats cursor-glm is skipped
                                  # with a one-time note (not an opaque failure)
```

A `[panels]` entry names known seats (registered external or Claude-channel
seats, or the `cursor` group token); an unknown name is dropped with a one-time
note. Panel names and seat names share ONE namespace: a `[panels]` entry named
after a live seat is ignored with a note (the seat wins), so `--panel` and
`--seats` can never resolve the same word to two different rosters. Group
tokens are exempt (redefining the built-in `cursor` preset stays legal). An unavailable seat is filtered out of any resolved panel **after** panel
resolution and **before** the run; if a filter would empty a panel entirely, crew
warns once and runs the unfiltered panel rather than nothing.

#### Add a model seat

A new model seat is pure config — no code, no version bump, no plugin reinstall.
Give a `[seats.<name>]` table a one-element `via` list (`codex`, `cursor`, `agy`,
or `claude`) and the `model` string that channel accepts, in either config file.
`provider` is accepted as the legacy spelling. Seat names are lowercase
(`[A-Za-z0-9_-]` minus uppercase: result file names are not case-sensitive on
every filesystem, so `Opus` would collide with `opus` on disk):

```toml
[seats.codex-nano]
via = ["codex"]
model = "gpt-5.6-nano"
```

That's the whole thing: the seat is registered on the next run, usable via
`--seats codex-nano` or by adding it to a `[panels]` roster. A declared seat
never joins the built-in panels on its own (that is the anti-silent-billing
rule; `opt_in = true` just tags it premium/off-by-default in listings). The
provider CLI must be installed and authed. `"${CLAUDE_PLUGIN_ROOT}/crew" seats`
prints the resolved default panel's external seats (not the whole catalog);
`crew doctor` shows every registered seat.

#### Ad-hoc single-provider runs

To call **one** provider directly (no fan-out, no review rubric) — e.g. for a
quick codex/agy query — use the engine's `run` subcommand instead of a
hand-built `agy -p "$(cat …)"`. That raw form can't be permission-allowlisted
because the prompt path changes every time; the `run` wrapper is one stable,
allowlistable command:

The engine is invoked through the plugin-root bare `crew` dispatcher (a thin
launcher run directly via its shebang + exec bit — no `python` prefix, no
`.py` tail), which keeps each call short and allowlistable:

```bash
# prompt from a file (handles "the cat" for you)
"${CLAUDE_PLUGIN_ROOT}/crew" run agy -f <prompt-file>

# or a direct prompt string
"${CLAUDE_PLUGIN_ROOT}/crew" run codex "summarize this"
```

`run <seat>` accepts any registered seat resolved external for the current host,
including Claude seats off-host, plus `-m/--model`,
`-s/--sandbox read-only|workspace-write`, `--timeout N`, and `--json` (emits the
same six-field core plus optional channel provenance). It reuses the exact provider classes the review panel
uses, so ANSI-stripping, agy auth-banner detection, timeout floors, and config
resolution all apply unchanged. On success it prints the cleaned output and
exits 0; on failure it prints the error to stderr and exits nonzero (in `--json`
mode it always exits 0 with the `ok` flag in the JSON). **Always use this wrapper
for ad-hoc calls — never raw `agy -p` / `codex exec`.**

#### Cleaning up stale artifacts (`crew swab`)

Attended cleanup of stale crew artifacts under the project `.crew/`: orphaned
review-run dirs (no active loop and no current-run pointer names them) and stale
debate dirs (past the 1-day threshold, no synthesis). It is DRY-RUN by default,
modelled on `git clean -n`: with no flag it only lists what it would remove, so
reading the list is the safety step.

```bash
# list candidates, delete nothing (default)
"${CLAUDE_PLUGIN_ROOT}/crew" swab

# the destructive step: rmtree the listed candidates
"${CLAUDE_PLUGIN_ROOT}/crew" swab --yes
```

`--yes` is the only destructive mode (it exits nonzero if any delete failed). It
also REFUSES (exit 2, deletes nothing) when run from a terminal `.crew` cwd with
`CLAUDE_PROJECT_DIR` unset: the project root is only a guess there, so cd back to
the project root or set the env var (the dry-run listing still works). A review
run is protected while an active loop or a current-run pointer names it, and
plans and loop state are never in scope, so swab only ever removes stale
review/debate debris. `--json` emits the machine payload (`prunable`, `removed`,
`failed`, `total_bytes`).

## Agents

Specialized agents for different tasks. Use via `Task(subagent_type="crew:agent-name", prompt="...")`.

| Agent | Use For | Example |
|-------|---------|---------|
| **advisor** | Architecture decisions, debugging, root cause analysis | "Why is the auth service slow?" |
| **reader** | Finding code, external docs, images, code graph analysis | "Where is the payment logic?" |
| **executor** | Implementing well-defined tasks (no delegation) | "Add createdAt field to User entity" |
| **document-writer** | README, API docs, technical writing | "Document the OrderService API" |
| **reviewer** | Panel seat for `/crew:review` (read-only by convention — has `Bash` for git inspection, not sandbox-enforced; spawned at `model: opus` / `model: sonnet`) | (driven by `/crew:review`) |
| **panelist** | Discuss-mode council seat for `/crew:debate` (independent critical take — direct take, strongest objection, risks/tradeoffs; no verdict; read-only by convention) | (driven by `/crew:debate`) |
| **formatter** | Reformats one review seat's raw output into the structured FINDINGS schema (faithful transform, read-only, `model: haiku`) for the per-seat repair fallback | (driven by the review/build/measure-twice repair step) |
| **scribe** | Persists one review seat's text to disk on the orchestrator's behalf so the persist-Write does not render in the terminal (verbatim transcribe, `Write`-only, `model: haiku`) | (driven by the review/build/measure-twice success-path persist step) |

### Quick Reference

```
Need to find code?           → reader
Need to understand code?     → advisor
Need to write code?          → executor
Need external docs?          → reader
Need to write docs?          → document-writer
Need to read an image/PDF?   → reader
```

## Skills

Skills auto-inject guidance when Claude detects relevant keywords in the conversation.

All skills ship in the **sk plugin** (the `crew` plugin ships no skills).

### sk plugin (tech-stack)

| Skill | Triggers On | Provides |
|-------|-------------|----------|
| **git** | Git commits, branching, rebase, stash, cherry-pick, history | Commit style, atomic commits, rebase/conflict handling |
| **python** | Python code, exceptions, logging, dataclasses, pathlib, uv/ruff/pytest | Python idioms, project tooling patterns |
| **kotlin** | Kotlin code, data classes, null safety | Kotlin patterns (null safety, data classes, extensions) |
| **kotlin-testing** | Tests, Kotest, MockK, TestIdProvider | Test utilities, DatabaseTest, Testcontainers patterns |
| **exposed** | Exposed ORM, database queries | Repository patterns, transaction handling |
| **gradle** | Gradle builds, dependencies | Version catalogs, lockfiles, plugin patterns |

Skills are in `plugins/*/skills/*/SKILL.md`. They activate based on the `description` field in their frontmatter, and sk's SessionStart hook names the ones matching the detected stack (see below).

## Hooks

Hooks run automatically at specific points in the session.

| Plugin | Hook | When | What It Does |
|--------|------|------|--------------|
| crew | **SessionStart** | Session begins | Restores build loop / measure-twice state and saved context |
| crew | **Stop** | Claude tries to stop | Enforces build loop / measure-twice continuation |
| sk | **SessionStart** | Session begins | Detects the project's stack and names the matching sk skills |

### Persistence Behavior

When a build loop or measure-twice loop is active:
1. Claude attempts to stop
2. The Stop hook intercepts
3. Claude is reminded to continue working
4. Loop persists toward completion (it can also end via your cancel, a safety bound, or a second consecutive FAILED verdict, per the exits listed below)

Priority order: build loop → measure-twice

Generic todo continuation is not enforced by the Stop hook — Claude Code handles
that natively.

To exit early: `/crew:cancel-build` or `/crew:cancel-measure-twice`

A build loop normally ends on a completing verdict from the multi-model review panel (`APPROVED`, or `REVISE` with only `[MINOR]` issues; a human may `--force` completion over an advisory), and it ends terminally on a second consecutive `FAILED` verdict (`review_failed`). Three other things can end a turn:

- **Cancelling** (`/crew:cancel-build`).
- **A hook-owned safety limit** (a stop-fire cap or the wall-clock deadline), which force-exits the loop and asks for a status report instead of an approval.
- **A parked turn.** While the session still has background work in flight (its own panel seats, but also any unrelated background shell: a dev server, a `tail -f`), the Stop is ALLOWED and the turn ends: waiting is not quitting, and the session resumes when that work completes. The loop stays active. Consecutive parked turns are capped (20), after which a parked stop is nudged like any other, so a background process that never exits cannot switch the loop off.

### SessionStart Agent Guidance (crew)

crew's SessionStart hook names its own agents in every session. Output volume is controlled by the `CREW_VERBOSE` environment variable:

| `CREW_VERBOSE` | Agent output |
|----------------|--------------|
| unset (default) | Single line listing agents |
| `1` | Full `<system-reminder>` block with per-agent notes |

crew names no skills. It cannot see whether the sk plugin is installed, so a skill list kept here would advertise skills that may not exist, and would go stale whenever sk changed.

### SessionStart Stack Detection (sk)

sk's own SessionStart hook (`plugins/sk/scripts/session-start.py`) scans the project for stack markers and names the sk skills that cover what it finds:

| Detected | Marker | Names |
|----------|--------|-------|
| Kotlin | any `.kt` / `.kts` | `sk:kotlin`, `sk:kotlin-testing` |
| Gradle | any `.gradle.kts` | `sk:gradle` |
| Python | any `.py` | `sk:python` |
| Exposed | `exposed` in the root `build.gradle.kts` | `sk:exposed` |

`sk:git` is deliberately absent. It maps to an action (commit, rebase) rather than to anything structural about the codebase, so there is nothing to detect, and its own `description` already activates it reliably. Python is detected by extension rather than by a root `pyproject.toml` / `setup.py` / `requirements.txt`, because a repo can carry real Python with none of those present (this marketplace is one).

This exists because passive activation (Claude matching a conversation against each SKILL.md `description`) rarely fired on its own.

Two properties keep it honest:

- **A skill is named only if sk actually ships it.** A detector whose skill has been removed stays silent instead of pointing at nothing, and lights up on its own if that skill is added back. This is the guard against the drift that had the old crew-side mapping advertising a skill for days after it was deleted.
- **Nothing to say means nothing is said.** With no stack detected (or no shipped skill matching it), the hook emits an empty result rather than an empty reminder.

#### Where the file list comes from

The scan reads `git ls-files --cached --others --exclude-standard` when the project is a git repo, and falls back to a bounded directory walk when it isn't (no repo, or no `git` on PATH).

The index is preferred for **correctness**, not speed. The walk needs a hard entry cap (20,000) to stay inside the 10s hook budget, and a cap plus arbitrary directory order means a large generated directory can exhaust the budget before a real source directory is ever reached — detection then under-reports the stack with no error. That is not hypothetical: a repo here has a `benchmarks/` directory with 33,213 files on disk and 14 tracked, and the walk caps out inside it and never reaches the `service/` directory holding the project's only `.py`.

The index has no such failure mode. It is also gitignore-aware for free, which is a better prune list than any hardcoded set: build output, virtualenvs, and `.crew/` artifacts drop out because the project already declared them uninteresting. `--others --exclude-standard` keeps it honest mid-edit — a file written but not yet `git add`ed still counts, and a fresh `git init` is not read as an empty project. Deleted-but-still-tracked paths are confirmed on disk before they count, since the index lists entries rather than files.

Both sources scale with the number of paths they see; the index simply sees far fewer of them, and pays a fixed process spawn up front. Measured on repos here (385 to 1,075 indexed paths, 783 to 34,039 on-disk files), the index route ran 9-13ms and the walk 1.5-21ms — so the walk wins on small trees, the index on larger ones, and a repo with a very large index would narrow the gap again. At these magnitudes neither matters against a 10s budget, which is why correctness decided it and not speed.

Fallback triggers on anything that makes the index unusable: not a git repo, no `git` on PATH, a nonzero exit, or the 5s subprocess timeout. A capped walk that may under-report beats no detection at all.

## Project Structure

```
claude-crew/
├── .claude-plugin/
│   └── marketplace.json      # Marketplace manifest
├── plugins/
│   ├── crew/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json   # Plugin manifest
│   │   ├── crew              # Plugin-root engine dispatcher (bare executable)
│   │   ├── agents/           # Agent definitions
│   │   ├── commands/         # Slash commands
│   │   ├── hooks/
│   │   │   └── hooks.json    # Hook configuration
│   │   ├── scripts/
│   │   │   ├── crew-state.py       # Loop state CLI
│   │   │   ├── models.py           # Dataclasses for JSON structures
│   │   │   ├── persistent-mode.py  # Build + measure-twice loop enforcement
│   │   │   ├── session-start.py    # State restoration
│   │   │   └── tests/              # Test suite (test-hooks.py, test-multiagent.py, fixtures/)
│   │   └── docs/
│   │       └── CLAUDE.md     # User config template
│   └── sk/
│       ├── .claude-plugin/
│       │   └── plugin.json   # Plugin manifest
│       ├── hooks/
│       │   └── hooks.json    # Hook configuration
│       ├── scripts/
│       │   ├── session-start.py  # Stack detection + skill naming
│       │   └── tests/            # Test suite (test-sk-hook.py)
│       └── skills/
│           ├── git/
│           ├── python/
│           ├── kotlin/
│           ├── kotlin-testing/
│           ├── exposed/
│           └── gradle/
└── README.md
```

## State Files

The plugin creates state files in `.crew/`:

| File | Purpose |
|------|---------|
| `.crew/build-state-<session-id>.json` | Build loop state (session-scoped) |
| `.crew/measure-twice-state-<session-id>.json` | Measure-twice loop state (session-scoped) |
| `.crew/context-snapshot.md` | Saved context (via /crew:save-context) |
| `.crew/plans/*.md` | Generated plans |

State files are session-scoped (`-<session-id>` suffix) so concurrent sessions
don't collide; a legacy unsuffixed `.crew/build-state.json` is still read for
backward compatibility. Cancelling a loop **deactivates in place** (sets
`"active": false`) rather than deleting the file — session-start cleanup sweeps
inactive files after a day.

## Customization

### Adding a Skill

1. Create `plugins/<plugin>/skills/my-skill/SKILL.md`
2. Add frontmatter:
   ```yaml
   ---
   name: my-skill
   description: Guidance for X. Use when working with Y or Z.
   ---
   ```
3. Add guidance content

### Adding an Agent

1. Create `plugins/crew/agents/my-agent.md`
2. Add frontmatter:
   ```yaml
   ---
   name: my-agent
   description: What this agent does
   tools: Read, Grep, Glob, ...
   ---
   ```
3. Add instructions

## Troubleshooting

**Build loop won't stop**
- Run `/crew:cancel-build`
- Or delete the session's `<project-root>/.crew/build-state-<session-id>.json`

**Measure-twice loop won't stop**
- Run `/crew:cancel-measure-twice`
- Or delete the session's `<project-root>/.crew/measure-twice-state-<session-id>.json`

**Skills not activating**
- Check the skill's `description` matches conversation keywords
- Skills auto-activate; no explicit invocation needed

**Session state not restored**
- Verify the project root's `.crew/` directory exists and is writable

## Uninstallation

```bash
# Remove the plugin (the sk plugin, if installed, is a separate uninstall: claude plugin uninstall sk@claude-crew)
claude plugin uninstall crew@claude-crew

# Clean up project state (optional, per-project). Substitute the project's
# absolute path for <project-root>, keeping it a double-quoted literal (the
# command then works from any shell cwd):
rm -rf "<project-root>/.crew"
```

The `.crew/` directory contains project-specific state (build loop, context snapshots, plans). Remove it from any projects where you used the plugin.
