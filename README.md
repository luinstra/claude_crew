# Claude Crew

A Claude Code plugin for persistence, specialized agents, and tech-stack guidance.

## What It Does

- **Persistence** — Don't stop until work is complete (build loops, todo continuation)
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
  "allowedPrompts": [
    "Bash(*/crew-state.py *)"
  ]
}
```

This allows the `crew-state.py` script (used by `/crew:build`, `/crew:measure-twice`, and their cancel commands) to run without confirmation.

### Requirements

- **Python 3.10+** — macOS 12+ and most modern Linux distributions include this. Check with `python3 --version`.

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

For work requiring verification before declaring "done," use `/crew:build` instead — it won't let you stop until an advisor approves completion.

## Commands

### Core Workflow

| Command | Description |
|---------|-------------|
| `/crew:plan "description"` | Start a planning session with the advisor agent |
| `/crew:execute "task or plan"` | Execute a task or plan via executor agent (keeps main context clean) |
| `/crew:review "the plan \| the diff"` | Multi-model review of a plan OR code diff (natural-language dispatch) → `APPROVED`/`REVISE` verdict |
| `/crew:debate "question"` | Crew-native council — single-round multi-model take on a question (codex + agy + opus + sonnet), synthesized into agreement/disagreement/recommendation |
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
| `/crew:crew-config` | Copy CLAUDE.md to current project |
| `/crew:crew-config global` | Copy CLAUDE.md to global config |

### Multi-Model Review

`/crew:review` fans the same review prompt across a panel of models and
synthesizes one verdict. The default panel is **codex + agy + opus + sonnet**:
`codex` and `agy` run via the bundled `multiagent` engine
(`plugins/crew/scripts/multiagent/`), while the two Claude voices are the
`crew:reviewer` agent spawned at `model: opus` and `model: sonnet`. It dispatches
plan-vs-code from natural language and reports `APPROVED`/`REVISE` with
`[BLOCKING]`/`[MINOR]` findings. A skipped or failed seat (any kind) is reported
but never sinks the panel — the verdict is synthesized from whichever seats
succeed, and only an all-seats-failed panel skips the verdict.

**Choosing the panel.** `/crew:review`, `/crew:debate`, `/crew:build`, and
`/crew:measure-twice` all accept panel flags at the start of their argument:

- `--panel full` = `codex,agy,opus,sonnet` (the default) · `--panel lite` =
  `opus,sonnet` · `--panel solo` = `opus`
- `--seats <list>` — an explicit subset of `codex,agy,opus,sonnet`
  (e.g. `/crew:build --seats codex,opus "fix the bug"`).

Not every change needs the full panel — `lite` (the two Claude voices, no
external CLI) or a single seat is plenty for routine work. (At the engine level,
`CREW_MA_SEATS` still pins the default subprocess seats.)

#### Ad-hoc single-provider runs

To call **one** provider directly (no fan-out, no review rubric) — e.g. for a
quick codex/agy query — use the engine's `run` subcommand instead of a
hand-built `agy -p "$(cat …)"`. That raw form can't be permission-allowlisted
because the prompt path changes every time; the `run` wrapper is one stable,
allowlistable command:

```bash
# prompt from a file (handles "the cat" for you)
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" run agy -f <prompt-file>

# or a direct prompt string
python "${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py" run codex "summarize this"
```

`run <seat>` accepts `codex` or `agy`, plus `-m/--model`,
`-s/--sandbox read-only|workspace-write`, `--timeout N`, and `--json` (emits the
same six-field result). It reuses the exact provider classes the review panel
uses, so ANSI-stripping, agy auth-banner detection, timeout floors, and the
`CREW_MA_*` env all apply unchanged. On success it prints the cleaned output and
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

### crew plugin (core)

| Skill | Triggers On | Provides |
|-------|-------------|----------|
| **git** | Git commits, branching | Commit style, atomic commits |

### sk plugin (tech-stack)

| Skill | Triggers On | Provides |
|-------|-------------|----------|
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
| **SessionStart** | Session begins | Restores build loop / measure-twice state, saved context, pending todos |
| **Stop** | Claude tries to stop | Enforces build loop / measure-twice continuation, todo completion |

### Persistence Behavior

When a build loop, measure-twice loop, or incomplete todos exist:
1. Claude attempts to stop
2. The Stop hook intercepts
3. Claude is reminded to continue working
4. Loop continues until truly complete

Priority order: build loop → measure-twice → todos

To exit early: `/crew:cancel-build` or `/crew:cancel-measure-twice`

## Project Structure

```
claude-crew/
├── .claude-plugin/
│   └── marketplace.json      # Marketplace manifest
├── plugins/
│   ├── crew/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json   # Plugin manifest
│   │   ├── agents/           # Agent definitions
│   │   ├── commands/         # Slash commands
│   │   ├── hooks/
│   │   │   └── hooks.json    # Hook configuration
│   │   ├── scripts/
│   │   │   ├── crew-state.py       # Loop state CLI
│   │   │   ├── models.py           # Dataclasses for JSON structures
│   │   │   ├── persistent-mode.py  # Build loop + todo enforcement
│   │   │   ├── session-start.py    # State restoration
│   │   │   └── test-hooks.py       # Test suite
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
| `.crew/build-state.json` | Active build loop state |
| `.crew/measure-twice-state.json` | Active measure-twice loop state |
| `.crew/context-snapshot.md` | Saved context (via /crew:save-context) |
| `.crew/plans/*.md` | Generated plans |

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
- Or delete `.crew/build-state.json`

**Measure-twice loop won't stop**
- Run `/crew:cancel-measure-twice`
- Or delete `.crew/measure-twice-state.json`

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
