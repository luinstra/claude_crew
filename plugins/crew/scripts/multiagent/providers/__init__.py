"""Provider ABC, the normalized ProviderResult shape, and the seat registry.

This module pins the Step 1.0 orchestration contract for SUBPROCESS seats:
the six-field ``ProviderResult`` that BOTH seat kinds conform to. Task seats
(opus/sonnet) are normalized to this same shape by the orchestrator markdown.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass


# =============================================================================
# Normalized result shape (Step 1.0 / 1.1) — the ONE shape both seat kinds use
# =============================================================================

@dataclass
class ProviderResult:
    """Normalized per-seat result. Six fields, exact per spec.

    Subprocess seats emit this directly. Task seats are normalized to the same
    six fields by the orchestrator (commands/review.md) before synthesis.
    """

    name: str            # seat name, e.g. "codex"
    model: str | None
    ok: bool
    output: str          # cleaned stdout (ANSI + banner noise stripped)
    error: str | None    # diagnostic when ok is False
    elapsed: float

    def to_dict(self) -> dict:
        """Return all six fields as a plain dict.

        Mirrors the ``models.py`` convention (``asdict(self)``). The CLI/render
        layer batches a list of these and calls ``json.dumps`` ONCE over the
        list for ``--json`` output (see render.py / cli.py); we deliberately do
        NOT add a per-result ``to_json()`` because the renderer owns the
        array-level serialization.
        """
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d) -> "ProviderResult":
        """Inverse of to_dict — rebuild a result from a decoded seat JSON.

        CONTRACT: the returned ProviderResult is ALWAYS safe to pass to
        render_block/render_panel without raising, for ANY decoded JSON input.
        Every field is coerced to a render-safe type below; nothing is passed
        through raw. A non-object top-level value (list/null/str/number — valid
        JSON but not a dict) degrades to a skipped/failed result instead of
        crashing or faking an OK seat.

        The name="unknown" default is a LAST-RESORT fallback used only when NO
        name is available (non-object input, or a missing/empty name field). The
        collect path NEVER relies on it: cmd_collect always overrides name to the
        requested seat label S before rendering (see cmd_collect).
        """
        if not isinstance(d, dict):
            # File decoded to a non-object (list/null/str/number). Do NOT crash
            # and do NOT fake an OK seat — render this as a skipped/failed block.
            # The "unknown" name is a fallback; cmd_collect overrides it with the
            # requested seat label, so the digest never shows "unknown".
            return cls(name="unknown", model=None, ok=False, output="",
                       error="skipped: malformed result (not a JSON object)",
                       elapsed=0.0)

        # --- comprehensive, render-safe coercion of EVERY field ---
        name = d.get("name")
        name = "unknown" if name is None else str(name)  # None/missing -> "unknown"
                                                         # (fallback only; collect
                                                         # overrides). Any present
                                                         # value (incl. 0, "", lists)
                                                         # stringifies — never crashes
                                                         # _model_label.
        model = d.get("model")
        model = None if model is None else str(model)  # None stays None
                                                       # (_model_label handles None)

        ok = d.get("ok") is True                     # STRICT identity — only literal
                                                     # JSON `true` is OK.

        out = d.get("output")
        output = "" if out is None else str(out)     # None/missing -> "";
                                                     # int/list/etc -> str so
                                                     # .strip() never crashes

        err = d.get("error")
        error = None if err is None else str(err)    # None stays None; else stringify

        try:
            elapsed = float(d.get("elapsed", 0.0))   # "bad"/list/None -> 0.0
        except (TypeError, ValueError):
            elapsed = 0.0
        return cls(name=name, model=model, ok=ok, output=output,
                   error=error, elapsed=elapsed)


# =============================================================================
# Provider ABC
# =============================================================================

class Provider(ABC):
    """A subprocess seat (an external CLI driven by this engine).

    ``timeout`` on ``run()`` is a GENERIC per-provider default, NOT a uniform
    cap. Subclasses MAY raise their effective wall-clock timeout above the
    300s default when they have a longer internal deadline. In particular
    ``AgyProvider`` derives its effective wall-clock timeout from / >= its
    ``--print-timeout`` (default >= 8m); the ``300`` literal here is a FLOOR
    for providers with no longer-running internal deadline, never a ceiling
    that overrides agy's ``--print-timeout``.
    """

    name: str = ""

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """PATH check only — no auth probe, no network, no spawning the CLI.

        Returns ``(True, "")`` when the CLI is on PATH, else
        ``(False, "<diagnostic>")`` so the seat is *skipped* before any run
        (never timed out). Authentication problems are NOT detected here; they
        surface at run time.
        """
        ...

    @abstractmethod
    def run(
        self,
        prompt: str,
        *,
        sandbox: str = "read-only",
        model: str | None = None,
        timeout: int = 300,
    ) -> ProviderResult:
        """Run the provider against ``prompt`` and return a ProviderResult."""
        ...


# =============================================================================
# Registry — adding a fifth subprocess seat is a one-line change here
# =============================================================================

def _build_registry() -> dict:
    # Imported lazily to avoid a circular import (codex.py / agy.py import this
    # module for ProviderResult / Provider).
    from .codex import CodexProvider
    from .agy import AgyProvider
    from .cursor import CursorProvider, CURSOR_SEATS

    # codex is the live OpenAI seat; agy stays registered as an opt-in seat
    # (`--seats agy`) though it is no longer in the default panel.
    registry: dict = {
        "codex": CodexProvider,
        "agy": AgyProvider,
    }

    # Each Cursor model is its own seat. The registry maps name -> ZERO-ARG
    # factory (get_provider calls factory()), so bind each seat's config in a
    # closure. Adding a model is a one-line edit to CURSOR_SEATS (cursor.py).
    def _cursor_factory(seat: str, model: str, env: str):
        return lambda: CursorProvider(seat, model, env)

    for seat, (model, env) in CURSOR_SEATS.items():
        registry[seat] = _cursor_factory(seat, model, env)
    return registry


_REGISTRY: dict | None = None


def _registry() -> dict:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def get_provider(name: str) -> Provider:
    """Return a Provider instance for ``name``; raise a clear error otherwise."""
    registry = _registry()
    factory = registry.get(name)
    if factory is None:
        known = ", ".join(sorted(registry))
        raise ValueError(
            f"unknown subprocess seat {name!r}; known subprocess seats: {known}"
        )
    return factory()


def available_seats(names: list[str]) -> list[Provider]:
    """Return Provider instances for each requested subprocess seat name.

    Unknown names raise (via get_provider) so a typo is loud, not silent.
    Availability/auth is NOT checked here — call ``is_available()`` per seat.
    """
    return [get_provider(n) for n in names]


def known_seat_names() -> list[str]:
    """Names of all registered subprocess seats (stable sorted order)."""
    return sorted(_registry())
