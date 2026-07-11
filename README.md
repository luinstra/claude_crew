<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/claude_crew-logo-dark.svg">
    <img alt="claude_crew" src="assets/claude_crew-logo-light.svg" width="440">
  </picture>
</p>

A Claude Code plugin for persistence, specialized agents, and tech-stack guidance.

## What It Does

- **Persistence** — Don't stop until work is complete (build & measure-twice loops)
- **Specialized Agents** — Delegate to focused agents for specific tasks
- **Tech-Stack Skills** — Auto-injected guidance for Kotlin, Exposed, Gradle, Trino (via `sk` plugin)
- **Session Restoration** — Resume where you left off after restarts

## Works With Superpowers

Claude Crew integrates with the [superpowers](https://github.com/obra/superpowers) plugin. Each handles different concerns:

| Plugin | Handles |
|--------|---------|
| **Superpowers** | Universal workflow disciplines (TDD, debugging, verification, brainstorming) |
| **Claude Crew** | Specialized agents, tech-stack patterns, persistence |

### Plugin Responsibility Split

**Use superpowers skills for workflow disciplines:**
- `superpowers:systematic-debugging` — 4-phase root cause analysis
- `superpowers:test-driven-development` — RED-GREEN-REFACTOR cycle
- `superpowers:verification-before-completion` — Rigorous "done" criteria
- `superpowers:brainstorming` — Design exploration before implementation

**Use sk for tech-stack patterns:**
- `sk:kotlin` — Kotlin patterns (null safety, data classes, extensions)
- `sk:exposed` — Exposed ORM patterns
- `sk:gradle` — Gradle Kotlin DSL patterns
- `sk:trino` — Trino query patterns

**Use crew agents for specialized work:**
- Complex analysis → advisor agent
- Implementation → executor agent
- Documentation → document-writer agent

This split is automatically injected into every session via the session-start hook.

## Installation

### From GitHub (Recommended)

```bash
# Add the marketplace
/plugin marketplace add luinstra/claude_crew

# Install crew plugin (agents, commands, hooks)
/plugin install crew@luinstra-claude_crew

# Install tech-stack skills (optional)
/plugin install sk@luinstra-claude_crew
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

- **Python 3.10+** — macOS 12+ and most modern Linux distributions include this. Check with `python3 --version`.
  - The optional per-repo `.crew/config.toml` (see [Per-repo config](#per-repo-config)) needs **Python 3.11+** (stdlib `tomllib`); on 3.10 the config file is gracefully ignored and everything else works.

## Usage

### The Plan → Execute Workflow

For non-trivial work, use the **plan → execute** workflow to keep your main context clean:

```
/crew:plan "add user authentication to the API"
  ↓ (advisor gathers requirements, creates plan in .crew/plans/)
/crew:execute the plan
  ↓ (executor implements, verbose output stays in its context)
[summary returned to main session]
```

**Why this matters:** When you do work directly, every file read, grep result, and edit diff goes into your context window. This causes frequent compaction and lost continuity. Delegating to the executor keeps that noise isolated — you just get the summary.

### When to Use What

| Situation | Approach |
|-----------|----------|
| Quick question about code | Ask directly (maybe `/crew:analyze`) |
| Single small change | Do it directly |
| Multi-file feature | `/crew:plan` → `/crew:execute` |
| Debugging complex issue | `/crew:analyze` or `/crew:build` |
| Need to persist until done | `/crew:build` |
| Want a bulletproof plan | `/crew:measure-twice` (auto plan-review-revise loop) |

### Typical Session Flow

1. **Explore** — Ask questions, `/crew:analyze` to understand the codebase
2. **Plan** — `/crew:plan "the feature"` to design the approach
3. **Review** — `/crew:review` for a multi-model sanity-check of the plan or diff (optional)
4. **Execute** — `/crew:execute` to implement via executor agent
5. **Verify** — Check results, iterate if needed

For work requiring verification before declaring "done," use `/crew:build` instead — it won't let you stop until the multi-model review panel approves completion.

## Commands

### Core Workflow

| Command | Description |
|---------|-------------|
| `/crew:plan "description"` | Start a planning session with the advisor agent |
| `/crew:execute "task or plan"` | Execute a task or plan via executor agent (keeps main context clean) |
| `/crew:review "the plan \| the diff"` | Multi-model review of a plan OR code diff (natural-language dispatch) → `APPROVED`/`REVISE` verdict |
| `/crew:debate "question"` | Crew-native council — single-round multi-model take on a question (the default review panel), synthesized into agreement/disagreement/recommendation |
| `/crew:dispatch "[--seat <name>] <task>"` | Delegate a WORK task to ONE non-Claude seat (default `codex`) in write mode — it edits the working tree and leaves changes UNCOMMITTED + UNSTAGED for you to review (keep / revert / pipe into `/crew:review`) |
| `/crew:build "task"` | Start a persistence loop — Claude won't stop until task is verified complete |
| `/crew:cancel-build` | Exit an active build loop early |
| `/crew:measure-twice "task"` | Start a self-refining plan loop — generates plan, reviews, revises until approved |
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
| `/crew:crew-config` | Copy CLAUDE.md to current project |
| `/crew:crew-config global` | Copy CLAUDE.md to global config |

> **`/crew:init` vs `/crew:crew-config`** — different files, different concerns.
> `/crew:init` scaffolds the **engine-tuning `.toml`** (`default_panel`, panels, seats,
> `available`, `[dispatch].seat`) that the `config.py` loader reads. `/crew:crew-config`
> copies the **`CLAUDE.md` operating-instructions doc** into `.claude/CLAUDE.md` (project)
> or `~/.claude/CLAUDE.md` (global) — it touches no `.toml`.

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
- Opt-in external seats (add via `--seats`): `cursor-gpt`, `cursor-gemini`, `cursor-glm`, `cursor-grok`
<!-- seat-roster:task-opt-in -->
- Opt-in Claude voice (add via `--seats`): `fable`
<!-- seat-roster:all -->
- Every registered seat: `codex`, `codex-luna`, `agy`, `cursor-gpt`, `cursor-gemini`, `cursor-glm`, `cursor-grok`, `cursor-auto`, `cursor-composer`, `opus`, `sonnet`, `fable`

These lines document the BUILT-IN roster; a configured `default_panel`/`[panels]`
override changes what actually runs. `"${CLAUDE_PLUGIN_ROOT}/crew" seats` prints
the resolved AVAILABLE external-CLI seats (the Claude Task voices complete the
panel and are not listed there).

The external-CLI seats run via the bundled `multiagent` engine
(`plugins/crew/scripts/multiagent/`): the two codex seats are distinct OpenAI
voices (`gpt-5.6-sol` and `gpt-5.6-luna`) on the one codex CLI, while the two
Claude voices are the `crew:reviewer` agent spawned at `model: opus`
and `model: sonnet`. It dispatches plan-vs-code from natural language and reports
`APPROVED`/`REVISE` with `[BLOCKING]`/`[MINOR]` findings. A skipped or failed
seat (any kind) is reported but never sinks the panel: the verdict is synthesized
from whichever seats succeed, and only an all-seats-failed panel skips the
verdict.

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

Not every change needs the full panel — `lite` (the two Claude voices, no
external CLI), `quick` (the cheapest cross-model pair) for routine diffs, or a
single seat is plenty for routine work.

#### Per-repo config

`full` is the **built-in** default panel. An optional, personal per-repo
`.crew/config.toml` (already gitignored under `.crew/`) can override that default and tune per-seat
models (plus codex `reasoning_effort`, agy `print_timeout`) and the global
per-seat `timeout`. When you name no panel, `default_panel` resolves from
config, falling back to built-in `full`; the per-seat tuning knobs follow
**CLI flag > per-repo config > global config > built-in**. Needs Python 3.11+
(stdlib `tomllib`); on 3.10 the file is gracefully ignored — with a one-time
stderr note, so a `default_panel = "lite"` set to save cost can't silently hand
you the full panel.

`/crew:debate` honors config too, with its own `[debate].panel` override so a
repo can default its debates fuller than its reviews (debate's value is
cross-model diversity). Debate precedence is **`--panel`/`--seats` (explicit) >
`[debate].panel` > `default_panel` > built-in `full`**.
`/crew:dispatch` picks its default seat from `[dispatch].seat` (validated
against the known seats — a panel name or group token like `cursor` is
rejected), falling back to the built-in `codex`; an explicit `--seat` overrides.

```toml
# .crew/config.toml
default_panel = "lite"            # default panel when you name none

[debate]
panel = "full"                    # /crew:debate-only default; falls back to default_panel, then full

[dispatch]
seat = "codex"                    # default seat for /crew:dispatch; validated vs known seats, falls back to built-in codex

[seats.codex]
model = "gpt-5.5"
reasoning_effort = "high"

[seats.agy]
print_timeout = "8m"

[tuning]
timeout = 600                     # global per-seat wall-clock seconds
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
only in the global file still applies. Same TOML/`tomllib`/one-time-stderr-note
posture as the per-repo file.

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

A `[panels]` entry names known seats (registry subprocess seats, the Claude Task
seats, or the `cursor` group token); an unknown name is dropped with a one-time
note. An unavailable seat is filtered out of any resolved panel **after** panel
resolution and **before** the run; if a filter would empty a panel entirely, crew
warns once and runs the unfiltered panel rather than nothing.

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

`run <seat>` accepts any registered subprocess seat (the non-Claude seats in the
panel section above), plus `-m/--model`,
`-s/--sandbox read-only|workspace-write`, `--timeout N`, and `--json` (emits the
same six-field result). It reuses the exact provider classes the review panel
uses, so ANSI-stripping, agy auth-banner detection, timeout floors, and config
resolution all apply unchanged. On success it prints the cleaned output and
exits 0; on failure it prints the error to stderr and exits nonzero (in `--json`
mode it always exits 0 with the `ok` flag in the JSON). **Always use this wrapper
for ad-hoc calls — never raw `agy -p` / `codex exec`.**

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
| **trino** | Trino queries, analytics | QueryRouter patterns, read-only queries |

Skills are in `plugins/*/skills/*/SKILL.md`. They activate based on the `description` field in their frontmatter.

## Hooks

Hooks run automatically at specific points in the session.

| Hook | When | What It Does |
|------|------|--------------|
| **SessionStart** | Session begins | Restores build loop / measure-twice state and saved context |
| **Stop** | Claude tries to stop | Enforces build loop / measure-twice continuation |

### Persistence Behavior

When a build loop or measure-twice loop is active:
1. Claude attempts to stop
2. The Stop hook intercepts
3. Claude is reminded to continue working
4. Loop continues until truly complete

Priority order: build loop → measure-twice

Generic todo continuation is not enforced by the Stop hook — Claude Code handles
that natively.

To exit early: `/crew:cancel-build` or `/crew:cancel-measure-twice`

### SessionStart Plugin Guidance

The SessionStart hook also injects plugin guidance (agents and skills) into every session. Output volume is controlled by the `CREW_VERBOSE` environment variable:

| `CREW_VERBOSE` | Skill output | Agent output |
|----------------|--------------|--------------|
| unset (default) | One-line list: `Skills available: sk:kotlin, sk:kotlin-testing` | Single line listing agents |
| `1` | Full bulleted descriptions per skill | Full `<system-reminder>` block with per-agent notes |

When a project stack is detected (Kotlin, Exposed, Gradle, Trino), the guidance is wrapped in a `<system-reminder>` block for high salience. For Kotlin projects, a required first action is prepended: load the Kotlin LSP via `ToolSearch(query='select:LSP')` before reading or editing any code, preferring LSP operations over Read+grep for navigation.

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
│       └── skills/
│           ├── git/
│           ├── kotlin/
│           ├── kotlin-testing/
│           ├── exposed/
│           ├── gradle/
│           └── trino/
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
- Or delete the session's `.crew/build-state-<session-id>.json`

**Measure-twice loop won't stop**
- Run `/crew:cancel-measure-twice`
- Or delete the session's `.crew/measure-twice-state-<session-id>.json`

**Skills not activating**
- Check the skill's `description` matches conversation keywords
- Skills auto-activate; no explicit invocation needed

**Session state not restored**
- Verify `.crew/` directory exists and is writable

## Uninstallation

```bash
# Remove the plugin
claude plugins remove crew

# Clean up project state (optional, per-project)
rm -rf .crew/
```

The `.crew/` directory contains project-specific state (build loop, context snapshots, plans). Remove it from any projects where you used the plugin.
