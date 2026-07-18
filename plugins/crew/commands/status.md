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

The hook-owned livelock bounds that can END a loop are `stop_fires` and the wall
clock, so report those as the budget (compute elapsed from `started_at`). The Stop
HOOK counts its own fires, NOT rounds. (A loop can also end terminally on the review
side: a SECOND consecutive FAILED verdict deactivates it in place, tracked by
`consecutive_review_failures`. That is a verdict outcome, not a running-budget bound,
so surface it if the state shows it but do not present it as part of the budget.) The loop separately carries the review-verb-owned fields
`phase` (drafting/reviewing/done), `revision_round` (advanced by the review verbs,
not the hook), and `last_verdict` (APPROVED/REVISE/REJECT/FAILED): surface those
so the user can see where in the draft-review-done cycle the loop sits, but do NOT
present `revision_round` as a bound (it never ends the loop). The wall clock can be
OFF: when the state carries BOTH `deadline_minutes: 0` AND `no_deadline: true` (the
operator's global-config opt-out), report the deadline as "none (clock off,
stop-fires cap still applies)", never as elapsed-vs-0. A lone `deadline_minutes: 0`
without the marker is a corruption-shaped value the hook treats as the bounded
default, so report the default in that case.

**Build Loop:**
- Status: [Active/Inactive]
- If active: `stop_fires`/`max_stop_fires`, elapsed vs `deadline_minutes` (or "none", per the marker rule above), `phase`, `revision_round`, `last_verdict`, Task: "..."
- If inactive: the terminal `exit_kind` (`approved`/`cancelled`/`review_failed`/`force_exit`) and, when set, `consecutive_review_failures`, so a terminally FAILED loop is not shown as a plain clean finish. An inactive loop may instead have an EMPTY `exit_kind` (a legacy or never-reviewed state): render it plainly, without implying a terminal outcome it lacks

**Measure-Twice Loop:**
- Status: [Active/Inactive]
- If active: `stop_fires`/`max_stop_fires`, elapsed vs `deadline_minutes` (or "none", per the marker rule above), `phase`, `revision_round`, `last_verdict`, Task: "...", Plan: "..."
- If inactive: the terminal `exit_kind` (`approved`/`cancelled`/`review_failed`/`force_exit`) and, when set, `consecutive_review_failures`, so a terminally FAILED loop is not shown as a plain clean finish. An inactive loop may instead have an EMPTY `exit_kind` (a legacy or never-reviewed state): render it plainly, without implying a terminal outcome it lacks

**Context Snapshot:**
- Check if `.crew/context-snapshot.md` exists
- If yes, report its age in days

Run both `crew state show` commands and present the results clearly to the user.
