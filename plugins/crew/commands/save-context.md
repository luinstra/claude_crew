---
description: Save current working context to survive compaction
allowed-tools: Bash, Write
---

[CONTEXT SNAPSHOT]

First, get the current timestamp:
```bash
date -u +"%Y-%m-%dT%H:%M:%SZ"
```

Save a context snapshot to `.crew/context-snapshot.md` with the following structure:

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

Create the `.crew/` directory if it doesn't exist.

After saving, confirm: "Context saved to `.crew/context-snapshot.md` — you'll be prompted to restore it on next session."
