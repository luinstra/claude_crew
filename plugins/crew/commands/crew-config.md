---
description: Configure Claude Crew for current project or globally
---

## Configure Claude Crew

**Usage:**
- `/crew:crew-config` — Configure for current project (default)
- `/crew:crew-config global` — Configure globally

### Project-Scoped (default)

```bash
mkdir -p .claude && cp "${CLAUDE_PLUGIN_ROOT}/docs/CLAUDE.md" .claude/CLAUDE.md && echo "Configured for project"
```

Creates `.claude/CLAUDE.md` in your current project.

### Global

If `$ARGUMENTS` contains "global":

```bash
cp "${CLAUDE_PLUGIN_ROOT}/docs/CLAUDE.md" ~/.claude/CLAUDE.md && echo "Configured globally"
```

Creates `~/.claude/CLAUDE.md` which applies to all projects.

**Note:** Project-scoped config takes precedence over global.
