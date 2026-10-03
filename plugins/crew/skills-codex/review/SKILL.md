---
name: review
description: Run a Crew multi-model review of a plan or code scope, or explicitly resume that review, from Codex.
---

Resolve the physical plugin root from this file's location: `../..` from this
skill directory. Read `<plugin-root>/commands/review.md` and
`<plugin-root>/docs/codex-transport.md`, then transport the requested review
through the existing Python protocol. The Codex transport supplement supplies
native launch and capture mechanics. Use engine-issued work, argv and metadata;
do not duplicate workflow policy here. Explicit resume repeats the identical
start with the same supplied options and literal session ID.
