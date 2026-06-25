"""Render per-seat results side-by-side, plus a --json mode.

A failed/timed-out seat renders its error block and NEVER suppresses the
others. A *skipped* seat (unavailable) renders a clearly-marked skipped block
with its diagnostic. This is the same rendering path the orchestrator reuses
for failed/skipped Task seats.

Guards ``ProviderResult.model is None`` (agy with no --model): never do string
ops on None.
"""

from __future__ import annotations

import json

from .providers import ProviderResult


_SKIP_MARKER = "skipped:"


def _model_label(model) -> str:
    """Render a possibly-None model without TypeError."""
    if model is None:
        return "(default)"
    return str(model)


def is_skipped(result: ProviderResult) -> bool:
    """A seat is *skipped* (unavailable) when it never ran.

    The CLI marks an unavailable seat (``is_available() == False``) with an
    ``ok=False`` result whose ``error`` begins with the ``skipped:`` marker.
    Skipped != failed: skipped seats render via ``render_skipped`` and are
    excluded from the verdict math.
    """
    return (
        not result.ok
        and bool(result.error)
        and result.error.lstrip().lower().startswith(_SKIP_MARKER)
    )


def render_block(result: ProviderResult) -> str:
    """Render a single seat's block.

    A *skipped* (unavailable) seat routes to ``render_skipped`` so it is clearly
    distinct from a FAILED seat in non-JSON output.
    """
    if is_skipped(result):
        diag = (result.error or "").lstrip()
        # strip the leading "skipped:" marker for a clean diagnostic
        if diag.lower().startswith(_SKIP_MARKER):
            diag = diag[len(_SKIP_MARKER):].strip()
        return render_skipped(result.name, diag or "seat unavailable")

    model = _model_label(result.model)
    header = f"### seat: {result.name}  |  model: {model}  |  {result.elapsed:.1f}s"

    if result.ok:
        status = "OK"
        body = result.output.strip() or "(no output)"
    else:
        status = "FAILED"
        body = f"ERROR: {result.error or 'unknown error'}"
        if result.output.strip():
            body += "\n\n" + result.output.strip()

    return f"{header}\nstatus: {status}\n\n{body}"


def render_skipped(name: str, diagnostic: str) -> str:
    """Render a clearly-marked skipped (unavailable) seat block."""
    return (
        f"### seat: {name}  |  model: (skipped)\n"
        f"status: SKIPPED\n\n"
        f"SKIPPED: {diagnostic}"
    )


def render_panel(results: list[ProviderResult]) -> str:
    """Render all results side-by-side in stable seat order (as given)."""
    if not results:
        return "(no seats ran)"
    sep = "\n\n" + ("-" * 72) + "\n\n"
    return sep.join(render_block(r) for r in results)


def render_json(results: list[ProviderResult]) -> str:
    """Emit a JSON array of result dicts — ONE json.dumps over the list."""
    return json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False)
