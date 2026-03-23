---
description: Create hierarchical CLAUDE.md documentation for the codebase
---

Target: $ARGUMENTS

## Argument Parsing

- `--update` or `-u`: Update existing CLAUDE.md files only (skip new)
- `--dry-run`: Show what would be created without writing
- `[path]`: Target directory (defaults to current directory)

Examples:
- `/crew:deepinit` → Initialize current directory
- `/crew:deepinit ./src` → Initialize ./src directory
- `/crew:deepinit --update` → Refresh existing CLAUDE.md files

## Philosophy

**Quality over quantity.** A few well-crafted CLAUDE.md files beat dozens of auto-generated ones.

Claude Code automatically loads:
- `~/.claude/CLAUDE.md` (global)
- `CLAUDE.md` in the working directory
- `.claude/CLAUDE.md` in the project root

Nested CLAUDE.md files aren't auto-loaded, but Claude reads them via the Documentation Index in the root guide.

## What Gets Documented

**DO create CLAUDE.md for:**
- Root project directory (always)
- Key architectural layers (api, core, domain, infrastructure)
- Complex subsystems that need explanation
- Test directories with specific conventions

**DON'T create CLAUDE.md for:**
- Every single directory (noise)
- Simple utility folders
- Auto-generated or vendor directories

## Execution Workflow

### Phase 1: Analyze Structure

```
Task(subagent_type="crew:reader",
  prompt="Map the project structure. Identify:
    1. Build system (gradle, npm, cargo, etc.)
    2. Main architectural layers
    3. Key packages/modules
    4. Test structure
    Exclude: node_modules, .git, build, dist, __pycache__, .venv")
```

### Phase 2: Identify Targets

Typical targets by project type:

| Project Type | Key Directories |
|--------------|-----------------|
| Kotlin/JVM | `src/main/kotlin/{domain}/`, `src/test/` |
| Node.js | `src/`, `lib/`, `test/` |
| Python | `src/{package}/`, `tests/` |
| Monorepo | Each package root |

### Phase 3: Create Root CLAUDE.md First

The root guide serves as the index. Must include:
- Project overview
- Architecture summary
- Quick commands
- **Documentation Index** linking to nested guides

### Phase 4: Create Nested Guides

Work top-down (parent directories before children).

## CLAUDE.md Templates

### Root Guide

```markdown
# {Project Name}

> **Tech Stack:** {languages, frameworks}
> **Build:** `{build command}`
> **Test:** `{test command}`

## Overview

{2-3 sentences: what this does and architectural approach}

## Architecture

{Brief description}

## Quick Commands

| Command | Purpose |
|---------|---------|
| `{cmd}` | {what it does} |

## Documentation Index

| Guide | Coverage |
|-------|----------|
| [{Layer} Guide](./{path}/CLAUDE.md) | {what it covers} |

## Key Patterns

### {Pattern Name}
{Brief explanation with example}

## Common Mistakes

❌ **{Bad practice}**
✅ **{Good practice}**
```

### Nested Guide

```markdown
# {Component/Layer Name} - Claude Code Guidance

> **Context:** {What this handles}
> **Parent Guide:** [Project Root](../../CLAUDE.md)
> **See Also:** [{Related}](./{path}/CLAUDE.md)

## Quick Reference

{One paragraph summary}

## Structure

```
{directory}/
├── {subdir}/     # {purpose}
└── {file}        # {purpose}
```

## Key Patterns

### {Pattern Name}

{Explanation with code example}

## Common Mistakes

❌ **{Mistake}**
```{lang}
// Bad
```

✅ **{Correct}**
```{lang}
// Good
```

## Working Here Checklist

- [ ] {Check 1}
- [ ] {Check 2}

## Related Guides

- [{Guide}](./{path}/CLAUDE.md) - {relevance}
```

## Quality Standards

### Must Include
- Actionable patterns with code examples
- Common mistakes (❌/✅ format)
- Working checklist
- Cross-links to related guides

### Must Avoid
- Generic boilerplate stating the obvious
- Empty sections
- Broken cross-references

## Delegation

| Task | Agent |
|------|-------|
| Structure mapping | `reader` |
| Pattern analysis | `advisor` |
| Guide writing | `document-writer` |
| Bulk creation | `executor` |

## Begin Execution

Create a todo list tracking each target directory, then systematically generate CLAUDE.md files from root to leaves.
