"""Multi-model review engine for the crew plugin (subprocess seats only).

This package drives the *subprocess* seats (codex/codex-luna + agy +
cursor-gpt/cursor-gemini/cursor-glm/cursor-grok/cursor-auto/cursor-composer;
cursor-gpt/cursor-gemini/cursor-glm/cursor-grok opt-in) of a multi-model
review panel. Task seats (Claude voices — ``seats.TASK_SEAT_NAMES``: opus,
sonnet, plus opt-in fable) are driven from inside Claude Code by the
orchestrating command markdown, NOT by this engine.

Both seat kinds resolve to ONE normalized result shape — the six-field
``ProviderResult`` (name, model, ok, output, error, elapsed). Subprocess seats
emit it directly from here; Task-seat outputs are normalized to the same shape
by the orchestrator (see commands/review.md). This contract is pinned so the
two seat kinds never drift.
"""
