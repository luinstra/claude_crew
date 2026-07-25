---
description: Configure Claude Crew for current project or globally
allowed-tools: Bash, Read, Edit, Write
---

## Configure Claude Crew

Installs crew's working-style instructions as a SEPARATE crew-owned file, then
links it from your `CLAUDE.md` with one `@` import line. Your `CLAUDE.md` is
never overwritten: crew appends at most one line to it.

The split is the point. A crew-owned file can be replaced wholesale on every
update, so it never goes stale; your `CLAUDE.md` holds only what you wrote, so
an update can never destroy it.

**Usage:**
- `/crew:crew-config` : current project (default)
- `/crew:crew-config global` : all projects

### Step 1 - install the crew-owned file

Project scope (default):

```bash
mkdir -p .claude
```

```bash
cp "${CLAUDE_PLUGIN_ROOT}/docs/CLAUDE.md" .claude/crew-instructions.md
```

Global scope, when `$ARGUMENTS` contains "global":

```bash
mkdir -p ~/.claude
```

```bash
cp "${CLAUDE_PLUGIN_ROOT}/docs/CLAUDE.md" ~/.claude/crew-instructions.md
```

`crew-instructions.md` is CREW-OWNED: every run overwrites it wholesale, so
re-run this command after a crew update to pick up changes. Do NOT hand-edit
it; your own rules belong in `CLAUDE.md`, which crew leaves alone.

### Step 2 - link it from CLAUDE.md (never clobber)

Read the scope's `CLAUDE.md` (`.claude/CLAUDE.md`, or `~/.claude/CLAUDE.md`
for global) and handle exactly one of three cases:

| State | Action |
|-------|--------|
| File does not exist | Write it containing exactly `@crew-instructions.md` |
| Has a STANDALONE line that is exactly `@crew-instructions.md` | Done. Change nothing |
| Otherwise | Edit to append `@crew-instructions.md` on its own line at the end. Change NOTHING else |

"Standalone" is literal: a line whose entire content is `@crew-instructions.md`.
The string appearing inside prose, a table, or a fenced code block does NOT
count, because only a standalone line is an actual import; treating a mention
as one leaves crew's instructions silently unloaded.

**NEVER `cp` over an existing `CLAUDE.md`.** It holds the user's own rules and
may hold other `@` imports; overwriting it destroys work crew did not author.
That is what this command used to do, and it silently ate user config.

### This command is strictly additive

It writes ONE crew-owned file and appends AT MOST one line to `CLAUDE.md`. It
deletes nothing, edits no existing line, and removes no section, in any scope,
ever. If something in `CLAUDE.md` looks redundant with crew's instructions,
that is the user's to resolve: crew's own sections have no reliable boundary in
a file the user has edited, so cutting them is a guess, and a wrong guess
destroys work crew did not author.

**Note:** Project-scoped config takes precedence over global.
