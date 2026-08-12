"""Multi-model review engine for the crew plugin.

This package resolves the catalog's seats per host. Claude seats (the catalog's
``via = [\"claude\"]`` rows: opus, sonnet, plus opt-in fable) are driven by native
Task dispatch inside Claude Code on a Claude host, and by ``ClaudeProvider``
through the claude CLI where no native Claude channel exists. Codex, cursor, and
agy seats use their external provider adapters.

Both execution paths resolve to ONE normalized result shape: the six-field core
of ``ProviderResult`` (name, model, ok, output, error, elapsed), with optional
channel provenance, continuation outcome and continuation ID, run identity
stamps, and repair output. External seats emit it directly from here; native
Task-seat outputs are normalized to the same shape by the orchestrator (see
commands/review.md). This contract is pinned so host-specific execution does not
change the result shape.
"""
