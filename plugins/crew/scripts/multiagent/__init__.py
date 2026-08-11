"""Multi-model review engine for the crew plugin (subprocess seats only).

This package drives the *subprocess* seats of a multi-model review panel — the
executor-bearing seats in the catalog (``multiagent/seats.toml``: the codex, agy,
and cursor rows). Task seats (the catalog's ``via = [\"claude\"]`` rows: opus,
sonnet, plus opt-in fable) are driven from inside Claude Code by the
orchestrating command markdown, NOT by this engine.

``claude-code`` remains the legacy provider kind for those native-channel rows.

Both seat kinds resolve to ONE normalized result shape: the six-field core of
``ProviderResult`` (name, model, ok, output, error, elapsed), with optional
continuation outcome and continuation ID fields. Subprocess seats emit it
directly from here; Task-seat outputs are normalized to the same shape by the
orchestrator (see commands/review.md). This contract is pinned so the two seat
kinds never drift.
"""
