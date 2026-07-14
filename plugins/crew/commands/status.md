---
description: Show active loops, their remaining budget, and crew state
allowed-tools: Bash
---

## Current Crew Status

Check the state of active loops by running these commands. Pass `--session-id`
(the `[Session ID: …]` value injected this session, as a literal, NOT a
`${CLAUDE_SESSION_ID}` shell expansion) so show reports THIS session's loop
rather than a legacy or other-session state file:

```bash
# Check build loop
"${CLAUDE_PLUGIN_ROOT}/crew" state show bl --verbose --session-id <session-id>

# Check measure-twice loop
"${CLAUDE_PLUGIN_ROOT}/crew" state show mt --verbose --session-id <session-id>
```

### Report Format

After running both commands, report the status in this format:

A loop has no round counter: the only bounds that can end it are `stop_fires` and
the wall clock, so report those as the budget (compute elapsed from `started_at`).

**Build Loop:**
- Status: [Active/Inactive]
- If active: `stop_fires`/`max_stop_fires`, elapsed vs `deadline_minutes`, Task: "..."

**Measure-Twice Loop:**
- Status: [Active/Inactive]
- If active: `stop_fires`/`max_stop_fires`, elapsed vs `deadline_minutes`, Task: "...", Plan: "...", last verdict

**Context Snapshot:**
- Check if `.crew/context-snapshot.md` exists
- If yes, report its age in days

Run both `crew state show` commands and present the results clearly to the user.
