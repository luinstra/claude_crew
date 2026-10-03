---
description: Cancel the session's engine-owned build
allowed-tools: Bash
---

The user requested early exit. Discover this session's retained owner without
launching paid work. Resume may reconcile receipts, apply bounds, park a question
or issue an action; do not launch it:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" build-resume --session-id <literal-session-id>
```

Use its exact BuildRef:

```bash
"${CLAUDE_PLUGIN_ROOT}/crew" build-cancel --session-segment <issued-segment> --loop-instance-id <issued-lifetime> --reason "User cancelled via /crew:cancel-build"
```

Cancel is allowed after bounds and without a verdict. Summarize the terminal
outcome. Request cancellation only for the returned owned_handle if the host exposes
an API; report unavailable/failed cancellation honestly. Actual completion or an
explicit operator not_running confirmation is required to clear the writer fence.
Do not infer quiescence, run recovery automatically, clean edits or adopt another
session. A late return may be retained but cannot advance this cancelled lifetime.
