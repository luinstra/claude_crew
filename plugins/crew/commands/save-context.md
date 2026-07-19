---
description: Save current working context to survive compaction
allowed-tools: Bash, Write
---

[CONTEXT SNAPSHOT]

First, get the current timestamp:
```bash
date -u +"%Y-%m-%dT%H:%M:%SZ"
```

Save a context snapshot to the ABSOLUTE anchored path
`<project-root>/.crew/context-snapshot.md` (substitute your `CLAUDE_PROJECT_DIR`
value for `<project-root>`) with the **Write tool**. Anchor it because the reader
(session-start's restore banner and `/crew:restore-context`) resolves this file
against the project root, so a cwd-relative write from a shell cwd that is not the
project root would land where the reader never looks. The snapshot has the
following structure:

```markdown
# Context Snapshot
Saved: [current timestamp]

## Current Task
[What we're working on - be specific]

## Progress
- [x] Completed items
- [ ] In progress items
- [ ] Pending items

## Key Decisions
- [Decision 1]: [Rationale]
- [Decision 2]: [Rationale]

## Files Modified This Session
- `path/to/file1` - [what changed]
- `path/to/file2` - [what changed]

## Important Context
[Anything that would be lost in compaction - edge cases discovered, gotchas, user preferences expressed]

## Next Steps
1. [Immediate next action]
2. [Following action]
```

The Write tool creates the `<project-root>/.crew/` parent directory if it doesn't exist.

After saving, confirm: "Context saved to `<project-root>/.crew/context-snapshot.md` — you'll be prompted to restore it on next session."
