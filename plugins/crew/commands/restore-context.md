---
description: Restore context from a saved snapshot
allowed-tools: Read, Bash
---

[RESTORE CONTEXT]

Check if `<project-root>/.crew/context-snapshot.md` exists (substitute your
`CLAUDE_PROJECT_DIR` value for `<project-root>`; the writer, `/crew:save-context`,
anchors the same absolute path). If not, inform the user: "No context snapshot
found. Use /crew:save-context to create one."

If the snapshot exists:

1. Read the contents of `<project-root>/.crew/context-snapshot.md`
2. Present the restored context to the user:
   - When it was saved
   - What task was in progress
   - Key decisions made
   - Files that were modified
   - Next steps that were planned
3. Rename the snapshot to `<project-root>/.crew/context-snapshot.restored.md` to
   prevent re-prompting. `mv` is a shell command with no engine anchoring, so it
   takes ABSOLUTE paths: substitute your `CLAUDE_PROJECT_DIR` value for
   `<project-root>` (the same root steps 1-2 read from), keeping each path a
   double-quoted LITERAL. `${…}` expansions are banned in these recipes (they
   defeat permission allowlisting), but the ban is on expansions, not literals:
   a substituted literal stays allowlistable and renames the right tree's
   snapshot even when the shell cwd has drifted:

```bash
mv "<project-root>/.crew/context-snapshot.md" "<project-root>/.crew/context-snapshot.restored.md"
```

Note: Todo list persistence is handled separately by Claude Code. This command restores narrative context only (what we were doing, why, and what's next).
