---
name: build
description: Implement and panel-review a task through Crew, or explicitly resume its build loop, from Codex.
---

Resolve the physical plugin root from this file's location: `../..` from this
skill directory. Read `<plugin-root>/commands/build.md` and
`<plugin-root>/docs/codex-transport.md`, then transport the requested build loop
through the existing Python protocol. The Codex transport supplement supplies
native launch and capture mechanics. Use engine-issued work, argv and metadata;
do not duplicate workflow policy here. Explicit re-entry uses
`<plugin-root>/crew build-resume --session-id <literal-session-id>`.
