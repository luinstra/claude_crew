---
description: Run a multi-model debate by handing off to claude-octopus
argument-hint: "<prompt>"
allowed-tools: SlashCommand
---

[DEBATE — SHIM TO CLAUDE-OCTOPUS]

$ARGUMENTS

## What this does

`crew` does **not** implement a native debate engine. A unanimous 4-model
build-vs-keep debate (2026-06-19) concluded that re-implementing claude-octopus's
mature `grapple` debate (multi-round propose → critique → rebut → judge) is
max-cost / min-gain. Octopus's debate already works — it was used to design
this very capability.

So this command is a **thin shim**: hand the user's prompt to claude-octopus's
debate and get out of the way.

## Do exactly this

Invoke claude-octopus's debate with the user's prompt:

```
/octo:debate $ARGUMENTS
```

That's it. No round choreography, no prompt assembly, no engine call, no seat
fan-out — octopus owns the entire debate protocol (rounds, seat headers,
state-threading, judge synthesis, and any per-seat constraints).

If the `octo` plugin is not installed, tell the user that `/crew:debate`
requires claude-octopus (`octo`) and point them at it; do not attempt a native
debate.
