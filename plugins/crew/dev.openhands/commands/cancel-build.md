---
description: Crew cancel-build through native OpenHands agents
---

Resolve the installed plugin root from this command's source location (two
parents above its containing directory). Read `<plugin-root>/docs/openhands-transport.md`.
Through `scripts/openhands.py run`, invoke `crew build-resume` using the
retained context file, or the current backend/conversation/workspace inputs.
This reads the existing owner and the wrapper supplies its native session identity.
Do not perform any returned work item. Execute the emitted `commands.cancel`
exactly, including its bound owner and context file. Do not use a Claude injected
session ID or the shared Claude state-deactivation recipe.
Stop the owned task through the native host and retain any outstanding writer
fence until actual completion or confirmed quiescence. Cancellation ends this
request; never automatically restart it or launch external provider CLIs.
