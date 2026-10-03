---
name: save-context
description: Save the current Crew working context from Codex.
---

Resolve the physical plugin root from this file's location: `../..` from this
skill directory. Read `<plugin-root>/commands/save-context.md` and follow that recipe.
Use this resolved root wherever the recipe shows `${CLAUDE_PLUGIN_ROOT}`, the
current session's literal ID for session placeholders, and the user's arguments
where it shows `$ARGUMENTS`. Do not rely on automatic placeholder substitution.

Resolve the task's project directory with Crew's `crew_base()` resolver, honoring
an explicit project directory and otherwise the task's physical working directory.
Use that path wherever the recipe shows `${CLAUDE_PROJECT_DIR}`. Snapshot paths
belong to that project, not the installed plugin directory or filesystem root.
