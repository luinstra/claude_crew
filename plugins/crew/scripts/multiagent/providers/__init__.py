"""Provider ABC, the normalized ProviderResult shape, and the seat registry.

This module pins the Step 1.0 orchestration contract for SUBPROCESS seats:
the six-field ``ProviderResult`` that BOTH seat kinds conform to. Task seats
(opus/sonnet) are normalized to this same shape by the orchestrator markdown.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderContinuation:
    """Opaque provider conversation input at the provider seam."""

    conversation_id: str | None = None


@dataclass(frozen=True)
class ContinuationOutcome:
    """Structured continuation result consumed by the pure chain classifier."""

    capability: str
    phase: str
    conversation_id: str | None = None
    failure: str = "none"


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

    # OPTIONAL 7th field — the haiku-reformatted, findings-schema text produced by
    # `repair-seat`. It is GROUPING-ONLY: `findings.parse_seat` reads it (when set)
    # to extract findings/verdict/criteria for the grouped digest, but `output`
    # (the seat's GENUINE review) is NEVER overwritten — `render_panel`/`--full`
    # and the RAW/UNPARSED section ALWAYS render the original `output`. So NO
    # repair — lossy, garbled, or faithful — can ever remove or alter a seat's
    # original words on disk; the worst a bad repair can do is make ONE seat's
    # grouped digest slightly inaccurate, while the faithful --full/RAW record
    # stays byte-intact. Defaults None (no repair); omitted from `to_dict` when
    # None so an un-repaired seat's JSON keeps the byte-identical SIX-field shape.
    repaired_output: str | None = None

    # OPTIONAL run-identity stamps, same additive contract as repaired_output
    # (serialized only when set, coerced by from_dict, round-tripping through
    # repair-seat untouched, ignored by render). `run` and `persist-seat` stamp
    # both from the run dir's own run.json at write time whenever a result lands
    # in a run-scoped review dir, so a result's stamp always describes the dir it
    # physically sits in, so location and attribution cannot diverge. Everything
    # that COUNTS a result validates these against the run's record; the ad-hoc
    # flat layout never sets them.
    run_id: str | None = None
    target_sha256: str | None = None

    # OPTIONAL continuation fields.  The structured outcome is the classifier's
    # input; the flat ID is the value a chain-store write persists.  Both are
    # omitted when unset so every review/unchained result retains the exact
    # legacy six-field JSON shape.
    continuation: ContinuationOutcome | None = None
    continuation_id: str | None = None

    def to_dict(self) -> dict:
        """Return the seat's fields as a plain dict.

        Mirrors the ``models.py`` convention (``asdict(self)``) BUT drops each
        optional field (``repaired_output``, ``run_id``, ``target_sha256``,
        ``continuation``, ``continuation_id``) when
        it is None, so an un-repaired/un-stamped seat's JSON keeps the
        byte-identical SIX-field shape existing consumers expect. The CLI/render
        layer batches a list of these and calls ``json.dumps`` ONCE over the
        list for ``--json`` output (see render.py / cli.py); we deliberately do
        NOT add a per-result ``to_json()`` because the renderer owns the
        array-level serialization.
        """
        d = dataclasses.asdict(self)
        for opt in (
            "repaired_output", "run_id", "target_sha256",
            "continuation", "continuation_id",
        ):
            if getattr(self, opt) is None:
                d.pop(opt, None)
        return d

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

        # OPTIONAL grouping-only field — present only after a `repair-seat`. Absent
        # / None -> the seat has no repaired projection (parse the original output).
        rep = d.get("repaired_output")
        repaired_output = None if rep is None else str(rep)

        # OPTIONAL run-identity stamps, present only on run-scoped writes.
        # Coerced like the other optionals so a repair round-trip cannot launder
        # or mutate a stamp.
        rid = d.get("run_id")
        run_id = None if rid is None else str(rid)
        tsha = d.get("target_sha256")
        target_sha256 = None if tsha is None else str(tsha)

        continuation_data = d.get("continuation")
        continuation = None
        if isinstance(continuation_data, ContinuationOutcome):
            continuation = continuation_data
        elif isinstance(continuation_data, dict):
            conversation_id = continuation_data.get("conversation_id")
            continuation = ContinuationOutcome(
                capability=str(continuation_data.get("capability", "unsupported")),
                phase=str(continuation_data.get("phase", "fresh")),
                conversation_id=(
                    None if conversation_id is None else str(conversation_id)
                ),
                failure=str(continuation_data.get("failure", "none")),
            )

        continuation_id_value = d.get("continuation_id")
        continuation_id = (
            None if continuation_id_value is None else str(continuation_id_value)
        )
        return cls(name=name, model=model, ok=ok, output=output,
                   error=error, elapsed=elapsed, repaired_output=repaired_output,
                   run_id=run_id, target_sha256=target_sha256,
                   continuation=continuation, continuation_id=continuation_id)


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

    # Capability flag for /crew:dispatch (write-mode WORK seat). FAIL-CLOSED /
    # opt-in: the ABC default is ``False`` so a NEW provider is NOT
    # write-dispatchable until it DELIBERATELY sets this ``True`` on its class.
    # cmd_dispatch fails fast (exit 2) when a resolved seat's provider leaves this
    # ``False``. A fail-OPEN default (silently treating a brand-new provider as
    # write-capable) is exactly the kind of silent behavior that bites later, so
    # the safe default is the conservative one. The three shipping providers
    # (codex/cursor/agy) each set it ``= True`` EXPLICITLY (decision visible per
    # class). The flag itself adds nothing to ``run()``: the read-only vs
    # workspace-write split is the existing ``sandbox`` arg, and per-provider
    # write-mode tuning arrives via ``run()``'s keyword-only ``dispatch_options``.
    supports_workspace_write: bool = False

    # Continuation is independently fail-closed.  A provider must opt in only
    # after its adapter has an exact-ID capability probe and resume path; this
    # coarse flag never replaces the runtime probe's structured outcome.
    supports_continuation: bool = False

    # Per-provider /crew:dispatch write-mode tuning declaration:
    # ``{key: (expected_type, help)}``. FAIL-CLOSED like supports_workspace_write:
    # a new provider supports NO dispatch options until it deliberately declares
    # some. The type rides in the one dict because the config getter validates
    # per LAYER (a bad repo value must not shadow a good global one); a parallel
    # types dict would drift. Declared types are restricted to str and bool (the
    # validator + the --options/scaffold display define only those; the parity
    # test rejects anything else). Every subclass declares its OWN dict, empty
    # included: this ABC default is a shared mutable class attribute, so relying
    # on it (or mutating it) from a subclass would leak keys across providers.
    DISPATCH_OPTIONS: dict = {}

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
        dispatch_options: dict | None = None,
        continuation: ProviderContinuation | None = None,
    ) -> ProviderResult:
        """Run the provider against ``prompt`` and return a ProviderResult.

        ``continuation`` is an optional opaque exact-conversation request.  The
        default is disabled so review/debate and ordinary dispatch behavior do
        not change.

        ``dispatch_options`` is workspace-write-only tuning (validated values
        from ``config.dispatch_provider_options``): read-only callers
        (review/debate/probe) never pass it, and each provider ADDITIONALLY
        gates application on ``sandbox == "workspace-write"``, so a read-only
        argv stays byte-identical even if a future caller misuses it.
        """
        ...


# =============================================================================
# Registry — every executor-bearing seat in the catalog, through ONE factory
# =============================================================================

def _provider_classes() -> dict:
    """The executor classes, keyed by the kind they implement, PAIRED with the
    tunes each ctor binds: ``{kind: (provider_class, tunes_lambda)}``.

    The pairing is load-bearing: class and tunes binding live in ONE structure
    keyed once, so no parallel kind map can drift. Both consumers
    (``_build_registry`` and ``dispatch_options_by_kind``) read this one dict.
    Imported lazily to avoid a circular import (codex.py / agy.py import this
    module for ProviderResult / Provider).
    """
    from .agy import AgyProvider
    from .codex import CodexProvider
    from .cursor import CursorProvider

    # The ctor shapes differ (each takes the tunes its CLI actually has), so
    # each kind also says which of the spec's tunes to bind.
    return {
        "codex": (CodexProvider, lambda spec: {"reasoning_effort": spec.reasoning_effort}),
        "cursor": (CursorProvider, lambda spec: {}),
        "agy": (AgyProvider, lambda spec: {"print_timeout": spec.print_timeout}),
    }


def dispatch_options_by_kind() -> dict[str, dict]:
    """kind -> its provider class's DISPATCH_OPTIONS (executor-bearing kinds only).

    The ONE public map every options-consuming surface reads (the config getter,
    the ``crew dispatch --options`` listing, the scaffold-config comment blocks),
    derived from the same paired structure the registry builds from, so the two
    surfaces cannot disagree about which kinds exist. Each kind's dict is a
    shallow copy (the ``(type, help)`` values are immutable tuples), so a caller
    mutating the returned map cannot alter a provider class's declaration.
    """
    return {kind: dict(entry[0].DISPATCH_OPTIONS) for kind, entry in _provider_classes().items()}


def _build_registry() -> dict:
    # Imported lazily to avoid a circular import (see _provider_classes). The
    # catalog is the ONLY source of seats: a seat declared in a user's config
    # registers exactly like a shipped one, and no name here is special-cased.
    from multiagent import seats as catalog

    classes = _provider_classes()

    def _factory(spec):
        # The registry maps name -> ZERO-ARG factory (get_provider calls
        # factory()), so bind the seat's tunes in a closure. Every per-seat tune
        # reaches the provider HERE, which is why no provider reads the config.
        cls, tunes = classes[spec.provider]
        kwargs = tunes(spec)
        return lambda: cls(spec.name, spec.model, **kwargs)

    return {
        name: _factory(spec)
        for name, spec in catalog.merged_catalog().items()
        # A kind with no executor (the Claude Task seats) is orchestrator-owned:
        # it has no CLI to drive, so it never enters the registry.
        if spec.has_executor and spec.provider in classes
    }


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
