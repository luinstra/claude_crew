---
name: cancel-build
description: Cancel the session build loop from Codex.
---

Resolve the physical plugin root from this file's location: `../..` from this
skill directory. Read `<plugin-root>/commands/cancel-build.md` and follow that recipe.
Use this resolved root wherever the recipe shows `${CLAUDE_PLUGIN_ROOT}`, the
current session's literal ID for session placeholders, and the user's arguments
where it shows `$ARGUMENTS`. Do not rely on automatic placeholder substitution.
For owned native cancellation, follow `<plugin-root>/docs/codex-transport.md`;
interruption is not proof that a writer has stopped.
