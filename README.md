# Claude Crew

A Claude Code plugin for persistence, specialized agents, and tech-stack guidance.

## What It Does

- **Persistence** — Don't stop until work is complete (feedback loops, todo continuation)
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
cd claude-crew
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

This allows the `crew-state.py` script (used by `/crew:feedback-loop`, `/crew:measure-twice`, and their cancel commands) to run without confirmation.

### Requirements

- **Python 3.9+** — macOS 12+ and most modern Linux distributions include this. Check with `python3 --version`.

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
| Debugging complex issue | `/crew:analyze` or `/crew:feedback-loop` |
| Need to persist until done | `/crew:feedback-loop` |
| Want a bulletproof plan | `/crew:measure-twice` (auto plan-review-revise loop) |

### Typical Session Flow

1. **Explore** — Ask questions, `/crew:analyze` to understand the codebase
2. **Plan** — `/crew:plan "the feature"` to design the approach
3. **Review** — `/crew:review` to sanity-check (optional)
4. **Execute** — `/crew:execute` to implement via executor agent
5. **Verify** — Check results, iterate if needed

For work requiring verification before declaring "done," use `/crew:feedback-loop` instead — it won't let you stop until an advisor confirms completion.

## Commands

### Core Workflow

| Command | Description |
|---------|-------------|
| `/crew:plan "description"` | Start a planning session with the advisor agent |
| `/crew:execute "task or plan"` | Execute a task or plan via executor agent (keeps main context clean) |
| `/crew:review` | Review an existing plan |
| `/crew:feedback-loop "task"` | Start a persistence loop — Claude won't stop until task is verified complete |
| `/crew:cancel-feedback-loop` | Exit an active feedback loop early |
| `/crew:measure-twice "task"` | Start a self-refining plan loop — generates plan, reviews, revises until approved |
| `/crew:cancel-measure-twice` | Exit an active measure-twice loop early |

### Utilities

| Command | Description |
|---------|-------------|
| `/crew:code-search "query"` | Search codebase via file-reader agent |
| `/crew:analyze "target"` | Deep analysis via advisor agent |
| `/crew:deepinit` | Initialize CLAUDE.md hierarchy for a codebase |
| `/crew:save-context` | Save context snapshot for session recovery |
| `/crew:restore-context` | Restore a previously saved context snapshot |

### Setup

| Command | Description |
|---------|-------------|
| `/crew:crew-config` | Copy CLAUDE.md to current project |
| `/crew:crew-config global` | Copy CLAUDE.md to global config |

## Agents

Specialized agents for different tasks. Use via `Task(subagent_type="agent-name", prompt="...")`.

| Agent | Use For | Example |
|-------|---------|---------|
| **advisor** | Architecture decisions, debugging, root cause analysis | "Why is the auth service slow?" |
| **file-reader** | Finding files and code patterns in the codebase | "Where is the payment logic?" |
| **executor** | Implementing well-defined tasks (no delegation) | "Add createdAt field to User entity" |
| **researcher** | External docs, APIs, open source examples | "How do I configure Kafka consumers?" |
| **document-writer** | README, API docs, technical writing | "Document the OrderService API" |
| **media-reader** | Screenshots, PDFs, diagrams | "What error is in this screenshot?" |

### Quick Reference

```
Need to find code?           → file-reader
Need to understand code?     → advisor
Need to write code?          → executor
Need external docs?          → researcher
Need to write docs?          → document-writer
Need to read an image/PDF?   → media-reader
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
| **SessionStart** | Session begins | Restores feedback loop / measure-twice state, saved context, pending todos |
| **Stop** | Claude tries to stop | Enforces feedback loop / measure-twice continuation, todo completion |

### Persistence Behavior

When a feedback loop, measure-twice loop, or incomplete todos exist:
1. Claude attempts to stop
2. The Stop hook intercepts
3. Claude is reminded to continue working
4. Loop continues until truly complete

Priority order: feedback-loop → measure-twice → todos

To exit early: `/crew:cancel-feedback-loop` or `/crew:cancel-measure-twice`

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
│   │   └── scripts/
│   │       ├── crew-state.py       # Loop state CLI
│   │       ├── models.py           # Dataclasses for JSON structures
│   │       ├── persistent-mode.py  # Feedback loop + todo enforcement
│   │       ├── session-start.py    # State restoration
│   │       └── test-hooks.py       # Test suite
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
├── docs/                     # Development documentation
└── README.md
```

## State Files

The plugin creates state files in `.crew/`:

| File | Purpose |
|------|---------|
| `.crew/feedback-loop-state.json` | Active feedback loop state |
| `.crew/measure-twice-state.json` | Active measure-twice loop state |
| `.crew/session-breadcrumb.json` | Last session info |
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

**Feedback loop won't stop**
- Run `/crew:cancel-feedback-loop`
- Or delete `.crew/feedback-loop-state.json`

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

The `.crew/` directory contains project-specific state (feedback loop, context snapshots, plans). Remove it from any projects where you used the plugin.
