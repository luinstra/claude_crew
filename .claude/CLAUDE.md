# Claude Crew - Development Guide

> **Type:** Claude Code Plugin Marketplace
> **Plugins:** `crew` (agents, commands, persistence), `sk` (tech-stack skills)
> **Python:** the lifecycle hooks run on 3.10+; the engine (the `multiagent`
> package and `crew state`, both reached through the `crew` dispatcher) needs
> 3.11+ because it parses TOML config with stdlib `tomllib`

## Overview

This is a **marketplace** containing two plugins distributed via Claude Code's plugin system. There's no build step — plugins are installed directly from the repo.

## ⚠️ IMPORTANT: Conventional Commits Required

**This project uses conventional commits for automatic versioning.** The post-commit git hook (`scripts/post-commit-version-bump.sh`) analyzes commit messages and bumps versions immediately after each commit.

### Commit Message Format

```
type(scope): description

[optional body]

[optional footer]
```

### Version Bump Rules

| Commit Type | Version Bump | Example |
|-------------|--------------|---------|
| `feat:` | **minor** (0.1.0 → 0.2.0) | `feat: add new agent for code review` |
| `type!:` subject **or** `BREAKING CHANGE:` footer | **major** (0.1.0 → 1.0.0) | `feat!: change state file format` |
| `fix:`, `chore:`, `docs:`, etc. | **patch** (0.1.0 → 0.1.1) | `fix: correct command prefix in docs` |

> **Major bump triggers are precise:** the `!:` marker is only honored on the
> commit **subject** line (`type!:` / `type(scope)!:`), and `BREAKING CHANGE:`
> must be a **footer** (line start, colon). A bare prose mention of "breaking
> change" in the body no longer forces a major bump.

### Common Types

| Type | Use For |
|------|---------|
| `feat` | New features (agents, commands, skills) |
| `fix` | Bug fixes |
| `docs` | Documentation only |
| `chore` | Maintenance, dependencies |
| `refactor` | Code changes that don't add features or fix bugs |
| `test` | Adding or updating tests |

### Scopes (Optional)

Use scopes to indicate what changed:
- `feat(crew):` — Changes to crew plugin
- `fix(sk):` — Changes to sk plugin
- `docs(readme):` — README updates

### What Gets Bumped

The workflow is smart about what it bumps:
- **Marketplace version** — Only if `.claude-plugin/marketplace.json`, `README.md`, or `docs/` changed
- **Plugin versions** — Only plugins with actual file changes get bumped

### Examples

```bash
# Minor bump (new feature)
git commit -m "feat(crew): add code-review agent"

# Patch bump (bug fix)
git commit -m "fix: correct command prefix in build-loop docs"

# Major bump (breaking change)
git commit -m "feat!: change state file location from .crew to .claude-crew"

# No version bump (CI only)
git commit -m "ci: update workflow permissions"
```

**Bottom line:** Start your commit message with `feat:` for new stuff, `fix:` for fixes, and add `!` after the type for breaking changes.

## Quick Commands

| Command | Purpose |
|---------|---------|
| `/plugin` | Install plugin from current directory (run from `plugins/crew/` or `plugins/sk/`) |
| `python plugins/crew/scripts/tests/test-hooks.py` | Run hook unit tests |
| `python plugins/crew/scripts/tests/test-multiagent.py` | Run the multi-model engine + dispatcher tests |
| `python plugins/crew/scripts/tests/test-version-bump.py` | Run the post-commit version-bump hardening tests |
| `"${CLAUDE_PLUGIN_ROOT}/crew" state show bl` | Debug build loop state (via the bare dispatcher) |

### Install Git Hook (Required for Contributors)

```bash
ln -sf ../../scripts/post-commit-version-bump.sh .git/hooks/post-commit
```

This enables automatic version bumping after each commit based on conventional commits. Just `git push` once — both your commit and the version bump go together.

## Architecture

```
.claude-plugin/marketplace.json     ← Marketplace registry
plugins/
├── crew/                           ← Core plugin
│   ├── agents/                     ← execution contexts
│   ├── commands/                   ← slash commands
│   ├── hooks/hooks.json            ← Lifecycle integration
│   └── scripts/                    ← Python state machine
└── sk/                             ← Tech-stack plugin
    └── skills/                     ← tech-stack skills
```

## Documentation Index

| Guide | Coverage |
|-------|----------|
| [Scripts Guide](../plugins/crew/scripts/CLAUDE.md) | State machine, hooks, crew-state CLI |
| [docs/CLAUDE.md](../docs/CLAUDE.md) | **User template** (copied to user projects) |

## Adding Components

### Adding an Agent

1. Create `plugins/crew/agents/my-agent.md`
2. Add frontmatter:
   ```yaml
   ---
   name: my-agent
   description: What this agent does and when to use it.
   model: inherit
   color: blue
   tools: Read, Grep, Glob, Bash, Edit, Write
   ---
   ```
   - `model:` — use `inherit` for varied work; pin `sonnet` for cost-optimized agents whose work is consistently simple (search, doc writing).
   - `color:` — pick an unused one (blue, green, cyan, yellow).
   - Don't put `#` comment lines inside frontmatter — document optional MCP tools in the agent body instead.
3. Write system prompt instructions below frontmatter
4. Reference with `Task(subagent_type="crew:my-agent", ...)`

**Tool access patterns:**
- `advisor`: All tools (analysis needs everything)
- `executor`: All tools (implementation needs everything)
- `reader`: Read + Web + Graph (Glob, Grep, Read, WebSearch, WebFetch, Bash)

### Adding a Command

1. Create `plugins/crew/commands/my-command.md`
2. Add frontmatter:
   ```yaml
   ---
   description: What this command does (shown in /help)
   ---
   ```
3. Write instructions below frontmatter
4. Use `$ARGUMENTS` to access user input
5. Invoke with `/crew:my-command [args]`

**Important:** Commands need the `crew:` prefix when referenced in docs.

### Adding a Skill

1. Create `plugins/<plugin>/skills/my-skill/SKILL.md`
2. Add frontmatter:
   ```yaml
   ---
   name: my-skill
   description: Keywords that trigger this skill. Use when X, Y, or Z.
   ---
   ```
3. Write guidance below frontmatter
4. Skills auto-activate based on conversation keywords

**Activation is passive** — Claude reads the `description` and injects the skill when relevant.

### Adding a Hook

1. Edit `plugins/crew/hooks/hooks.json`
2. Add hook configuration:
   ```json
   {
     "event": "PreToolUse",
     "matcher": { "tool_name": "WebFetch" },
     "script": "${CLAUDE_PLUGIN_ROOT}/scripts/url-blocker.py"
   }
   ```
3. Create Python script in `scripts/`
4. Script receives JSON on stdin, outputs `HookResult` JSON

**Hook events:** `SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`

## State Machine

The crew plugin uses Python scripts to manage persistence:

```
User starts loop → crew state init → JSON state file created (phase: drafting)
    ↓
Claude works → session continues
    ↓
Claude tries to stop → Stop hook reads state → blocks if loop active
    ↓
Panel prepped → crew state begin-review → run identity frozen (phase: reviewing)
    ↓
Panel verdict → crew state record-verdict → a COMPLETING verdict (APPROVED, or
                                            REVISE --minor-only) leaves phase: done
                                            (a REVISE with [BLOCKING] issues,
                                            REJECT, and a lone FAILED return it to
                                            drafting; a SECOND consecutive FAILED
                                            deactivates the loop terminally in the
                                            same transaction, exit_kind: review_failed)
    ↓
Loop complete → crew state deactivate → JSON kept with active: false
```

`deactivate` is GATED: it turns a live loop off only from `phase: done` (either
completing verdict on record, stamped `exit_kind: approved`) or with an explicit
`--cancel` (stamped `cancelled`), so an unreviewed finish cannot pass for an
approved one.

State files are **session-scoped** and are **not deleted** on deactivate — the
loop is turned off in place by flipping `active: false` (plus a `completed_at`
timestamp), so the record survives for inspection. `session-start` cleanup later
sweeps inactive files older than a day.

**Orphan Cleanup (report-only for dirs).** `session-start` still auto-cleans the
ephemeral bookkeeping classes (stale state files, context snapshots, lock/temp
set-asides, and aged agent signal markers). But it NO LONGER deletes review-run
or debate directories: destructive rmtree of the user's disk from an unattended
hook was the wrong venue. Instead it REPORTS those orphans (a one-line count,
pointing at `crew swab`; exact sizing is deferred to the attended command),
enumerated by the one shared `artifact_prune` finder. The attended `crew swab` command OWNS their deletion (dry-run by default,
`--yes` to remove).

**State files** (in project's `.crew/`):
- `build-state-<session-id>.json` — Build loop (legacy unsuffixed `build-state.json` still read)
- `measure-twice-state-<session-id>.json` — Measure-twice loop (legacy unsuffixed still read)
- `context-snapshot.md` — Saved context

## Testing

```bash
# Run hook tests
python plugins/crew/scripts/tests/test-hooks.py

# Manual state inspection (via the bare crew dispatcher)
"${CLAUDE_PLUGIN_ROOT}/crew" state show bl
"${CLAUDE_PLUGIN_ROOT}/crew" state show mt
```

## Key Patterns

### Code Comments Stay Repo-Local

Comments are concise and reference only what's in the repo. State a non-obvious constraint or a "why" the code can't show, then stop, with no restating the code or padding. No em-dashes (use a colon, comma, or parentheses). Never cite a plan/phase/tier name or decision/ticket label ("Phase 5", "Decision-D", "the C2 smoke", "per the plan"), even if the task prompt uses that vocabulary, because a future reader has only the repo and those labels resolve to nothing.

### Command References Need Plugin Prefix

All command references in documentation must use `/crew:` prefix:

```markdown
❌ `/build "task"`
✅ `/crew:build "task"`
```

### Agent Delegation Pattern

Commands typically delegate to agents rather than doing work directly:

```markdown
## In your command file:

Spawn the **advisor agent** to analyze:

Task(subagent_type="crew:advisor", prompt="Analyze: $ARGUMENTS")
```

### Hook Result Format

Python hooks must output valid JSON:

```python
from models import HookResult

# Allow the action — emits {}
print(HookResult.allow().to_json())

# Allow with a loud user-visible diagnostic — emits {"systemMessage": "..."}
print(HookResult.allow_with_diagnostic("Hook degraded: ...").to_json())

# Block with reason — emits {"decision": "block", "reason": "..."}
print(HookResult.block("Reason to block").to_json())
```

**Note:** SessionStart crash diagnostics belong on `SessionStartResult.with_context()` (the `hookSpecificOutput.additionalContext` channel), distinct from Stop's `HookResult.allow_with_diagnostic()` (the `systemMessage` channel). The one exception is SessionStart's pre-import 3.9 version-guard fallback, which is stdlib-only and can't build a `SessionStartResult`, so it emits `systemMessage`.

## Common Mistakes

❌ **Referencing commands without prefix**
```markdown
Use `/build` to start
```

✅ **Always include plugin prefix**
```markdown
Use `/crew:build` to start
```

---

❌ **Hardcoding paths in hooks**
```python
state_file = "/Users/me/project/.crew/state.json"
```

✅ **Use the one shared resolver (`crew_base()`)**
```python
from state_discovery import crew_base
state_file = crew_base() / ".crew" / "state.json"
```
`crew_base()` resolves the project root once (`CLAUDE_PROJECT_DIR`, else cwd); every
`.crew` path derives from it, so nothing drifts to a phantom tree when the cwd moves.

## Working Here Checklist

- [ ] **Install post-commit hook** (`ln -sf ../../scripts/post-commit-version-bump.sh .git/hooks/post-commit`)
- [ ] **Use conventional commit format** (`feat:`, `fix:`, etc.)
- [ ] Run `tests/test-hooks.py` after modifying scripts
- [ ] Update `README.md` when adding commands
- [ ] Update `docs/CLAUDE.md` if changing user-facing behavior
- [ ] Use `/crew:` prefix in all command references
- [ ] Test plugin installation with `/plugin` from plugin directory
