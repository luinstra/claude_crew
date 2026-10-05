---
name: save-context
description: Save the current Crew working context from Codex.
---

Resolve the physical plugin root from this file's location: `../..` from this
skill directory. Read `<plugin-root>/commands/save-context.md` and follow that recipe.
Use this resolved root wherever the recipe shows `${CLAUDE_PLUGIN_ROOT}`, the
current session's literal ID for session placeholders, and the user's arguments
where it shows `$ARGUMENTS`. Do not rely on automatic placeholder substitution.

Run `<plugin-root>/crew project-root` from the task workspace. Use its absolute
output for `<project-root>` in the recipe. It honors `CREW_PROJECT_DIR`, the
legacy Claude alias, and otherwise cwd; no Claude variable needs to be exported.
Snapshot paths belong to that project, not the installed plugin directory.
