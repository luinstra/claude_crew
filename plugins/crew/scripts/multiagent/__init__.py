"""Multi-model review engine for the crew plugin (subprocess seats only).

This package drives the *subprocess* seats (codex + cursor-gpt/cursor-gemini/
cursor-glm/cursor-composer; agy opt-in) of a multi-model
review panel. Task seats (Claude voices: opus, sonnet) are driven from inside
Claude Code by the orchestrating command markdown, NOT by this engine.

Both seat kinds resolve to ONE normalized result shape — the six-field
``ProviderResult`` (name, model, ok, output, error, elapsed). Subprocess seats
emit it directly from here; Task-seat outputs are normalized to the same shape
by the orchestrator (see commands/review.md). This contract is pinned so the
two seat kinds never drift.
"""
