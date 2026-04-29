---
description: Restore context from a saved snapshot
allowed-tools: Read, Bash
---

[RESTORE CONTEXT]

Check if `.crew/context-snapshot.md` exists. If not, inform the user: "No context snapshot found. Use /crew:save-context to create one."

If the snapshot exists:

1. Read the contents of `.crew/context-snapshot.md`
2. Present the restored context to the user:
   - When it was saved
   - What task was in progress
   - Key decisions made
   - Files that were modified
   - Next steps that were planned
3. Rename the snapshot to `.crew/context-snapshot.restored.md` to prevent re-prompting

Note: Todo list persistence is handled separately by Claude Code. This command restores narrative context only (what we were doing, why, and what's next).
