---
description: Cancel active measure-twice loop
allowed-tools: Bash
---

[MEASURE-TWICE CANCELLED]

The user has requested early exit from the measure-twice loop.

## MANDATORY ACTION

Execute this command to fully cancel the measure-twice loop. Pass `--session-id`
(the `[Session ID: …]` value injected this session, as a literal — NOT a
`${CLAUDE_SESSION_ID}` shell expansion) so cancel deactivates the exact
session-scoped state file the loop was init'd with; otherwise the scoped file
stays active and keeps blocking Stop:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate mt --cancel --reason "User cancelled via /crew:cancel-measure-twice" --session-id <session-id>
```

`--cancel` is required and is the point of this command: it ends the loop WITHOUT
a panel verdict, which is exactly what the user asked for, and records the exit as
a cancellation rather than an approval. Without it, a loop that no panel signed off
on is refused. It is legal from any phase (an escape hatch a phase check could
veto would not be one).

## After Running

The guarded cancellation invalidates migrated claims and review bindings in
that same loop-state transaction. Stop only this loop's owned handles if the
host exposes a cancellation API; report if it does not. Late staging/evidence
files may remain but cannot advance the cancelled lifetime.

The measure-twice loop is now cancelled.

**You may:**
- Summarize current plan state (if any plan was generated)
- Stop working
- Start a new task

An identical request replays this terminal lifetime. A different immutable
request can start a fresh lifetime after normal completion or cancellation.
Only safety-force-exited lifetimes require explicit human restart authorization;
see `docs/measure-twice-protocol.md`. Resuming never resets an active lifetime.
