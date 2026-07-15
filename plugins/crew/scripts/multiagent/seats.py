"""The seat catalog LOADER: shipped ``seats.toml`` merged with the config layers.

``seats.toml`` (next to this file) is the single source of the roster: every seat
and every panel, in order. This module reads it, merges the user's
``[seats.<name>]`` / ``[panels]`` tables over it (per-repo > global > shipped,
resolved per-seat-per-key), and hands back ``SeatSpec`` rows. Adding a seat is a
TOML edit, in this repo OR in a personal config: a declared seat with a known
``provider`` is a first-class seat (registry entry, panel member, dispatch seat).

This module stays a LEAF: it imports nothing from ``providers/`` or ``cli``.
``PROVIDER_KINDS`` is the DATA half of that split (what a provider kind can do);
``providers`` CONSUMES it to build the registry. Nothing anywhere greps a literal
provider name to decide behavior.

Never-choke, as everywhere else in the config path: a bad row is DROPPED with a
one-time stderr warn, never an exception, so one typo cannot take down a panel.
A seat is dropped when it names an unknown provider, when its name is empty or
would not survive the staged-filename slug (``name != slug(name)``: the dot-strip
is not injective, so two seats could collide on one prompt file, and an empty
name falls back to the literal ``seat`` slug), when its name is a reserved token
(a panel name or a group token, which resolve to seat LISTS), or when a
``claude-code`` seat pins a model the Task tool would reject.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import tomllib  # stdlib 3.11+; the dispatcher (plugins/crew/crew) enforces that floor

from multiagent import config

_SHIPPED_CATALOG_PATH = Path(__file__).with_name("seats.toml")

# Seat names must survive cli._seat_role_slug UNCHANGED (it strips everything
# outside this charset to build `prompt-<seat>.txt` / `<seat>.json`). The strip is
# not injective, so a name that changes under it could collide with another seat's
# files. Same charset, asserted rather than applied.
_SLUG_CHARSET = re.compile(r"[^A-Za-z0-9_-]")


# =============================================================================
# Provider kinds — the DATA half of the seats/providers split
# =============================================================================

@dataclass(frozen=True)
class ProviderKind:
    """What one kind of provider can do. Consumed by ``providers`` (which owns the
    classes) and by this loader (which validates rows against it).

    has_executor: the kind drives an external CLI, so its seats are subprocess
    seats the engine RUNS. A kind without one (``claude-code``) is a Task seat:
    the orchestrator spawns it in-session, and the engine never executes it.

    model_rule: how a seat's model string is validated. ``free`` = any non-empty
    string the CLI accepts. ``alias`` = it must be a Task-tool model alias, which
    the Task tool VALIDATES at spawn (a full model id is rejected there, so a seat
    pinning one could never be seated).
    """
    name: str
    has_executor: bool
    model_rule: str


# The model aliases the Task tool accepts for a claude-code seat.
TASK_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable"})

PROVIDER_KINDS: dict[str, ProviderKind] = {
    "codex":       ProviderKind("codex", has_executor=True, model_rule="free"),
    "cursor":      ProviderKind("cursor", has_executor=True, model_rule="free"),
    "agy":         ProviderKind("agy", has_executor=True, model_rule="free"),
    "claude-code": ProviderKind("claude-code", has_executor=False, model_rule="alias"),
}


# =============================================================================
# SeatSpec — one resolved row of the catalog
# =============================================================================

@dataclass(frozen=True)
class SeatSpec:
    """One seat, fully resolved across the shipped catalog and the config layers.

    Every per-seat tune the providers need is bound HERE and passed to the ctor by
    the registry factory, so no provider reads the config itself and a declared
    seat gets the same tunes a shipped one does.

    declared: a config layer had a ``[seats.<name>]`` table for it (whether that
    table tuned a shipped seat or introduced a new one). It is what tells the
    premium-off derivation that the user already knows about this seat.
    """
    name: str
    provider: str
    model: str | None = None
    opt_in: bool = False
    available: bool = True
    reasoning_effort: str | None = None
    print_timeout: str | None = None
    declared: bool = False

    @property
    def kind(self) -> ProviderKind:
        return PROVIDER_KINDS[self.provider]

    @property
    def has_executor(self) -> bool:
        return self.kind.has_executor


# =============================================================================
# Load + merge
# =============================================================================

_shipped_raw: dict | None = None
_catalog: dict[str, SeatSpec] | None = None
_panels: dict[str, list[str]] | None = None
_premium_off: tuple[str, ...] | None = None

# Keys of warnings already emitted this process (one-time-note guard, mirroring
# config._warned). Cleared by the shared test reset: a warn fires ONCE per
# process, so a stale guard makes later warn assertions order-dependent (whichever
# test ran second would see silence and pass for the wrong reason).
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(f"crew seats: {message}", file=sys.stderr)


def _reset_for_tests() -> None:
    """Clear the catalog + panel + premium-off memos AND the warn guard (TESTS ONLY).

    Called from ``config._reset_cache_for_tests`` so ONE reset clears every layer.
    """
    global _catalog, _panels, _premium_off
    _catalog = None
    _panels = None
    _premium_off = None
    _warned.clear()


def _shipped() -> dict:
    """The parsed ``seats.toml``, loaded once. Shipped DATA: a failure here is a
    broken install, not a user typo, so it degrades to an empty catalog with a loud
    note rather than a traceback in the middle of a panel."""
    global _shipped_raw
    if _shipped_raw is None:
        try:
            _shipped_raw = tomllib.loads(
                _SHIPPED_CATALOG_PATH.read_bytes().decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            _warn_once(
                "shipped-catalog",
                f"the shipped catalog {_SHIPPED_CATALOG_PATH} could not be read "
                f"({exc}); no built-in seats are available",
            )
            _shipped_raw = {}
    return _shipped_raw


def _seat_tables(data: dict) -> dict:
    tbl = data.get("seats")
    return tbl if isinstance(tbl, dict) else {}


def _shipped_panel_names() -> set[str]:
    tbl = _shipped().get("panels")
    return set(tbl) if isinstance(tbl, dict) else set()


def _shipped_group_tokens() -> set[str]:
    """The group tokens, from ``seats.toml`` ALONE.

    STRUCTURAL: an executor-bearing kind mints a token named after it UNLESS a
    shipped seat already bears that name (``codex`` and ``agy`` are seats, so they
    mint nothing; ``cursor`` is not, so it mints the ``cursor`` group).

    Reads the shipped file only, deliberately: ``merged_catalog`` needs the
    reserved tokens to validate a name, and going through the merged panels here
    would re-enter the catalog on a cold cache.
    """
    shipped_seats = set(_seat_tables(_shipped()))
    return {
        name for name, kind in PROVIDER_KINDS.items()
        if kind.has_executor and name not in shipped_seats
    }


def reserved_tokens() -> set[str]:
    """Names a seat may NOT take: they already resolve to a seat LIST (a shipped
    panel name, or a group token). Shipped-only, so it is safe to call from inside
    the catalog build."""
    return _shipped_panel_names() | _shipped_group_tokens()


def _str_field(tbl: dict, key: str, seat: str, layer: str) -> str | None:
    """A non-empty string field, or ``None`` (dropping a bad value with a warn)."""
    val = tbl.get(key)
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        _warn_once(
            f"{key}:{layer}:{seat}",
            f"[seats.{seat}].{key} must be a non-empty string; ignoring {val!r}",
        )
        return None
    return val


def _bool_field(tbl: dict, key: str, seat: str, layer: str, *, on_bad: bool) -> bool | None:
    val = tbl.get(key)
    if val is None:
        return None
    if not isinstance(val, bool):
        _warn_once(
            f"{key}:{layer}:{seat}",
            f"[seats.{seat}].{key} must be a boolean; ignoring {val!r} "
            f"(treating it as {str(on_bad).lower()})",
        )
        return on_bad
    return val


def _spec_from_table(name: str, tbl: dict, layer: str, base: SeatSpec | None) -> SeatSpec | None:
    """Fold one layer's ``[seats.<name>]`` table over ``base`` (``None`` = a new,
    config-DECLARED seat). Returns ``None`` when the row must be dropped."""
    provider = _str_field(tbl, "provider", name, layer)
    if provider is None and base is None:
        # A table for a name no shipped seat carries and no provider to drive it:
        # nothing to tune and nothing to build. Almost always a typo'd seat name.
        _warn_once(
            f"unknown-seat:{layer}:{name}",
            f"[seats.{name}] names no known seat and declares no provider; ignoring it",
        )
        return None
    if provider is not None and provider not in PROVIDER_KINDS:
        _warn_once(
            f"provider:{layer}:{name}",
            f"[seats.{name}].provider={provider!r} is not a known provider "
            f"({', '.join(sorted(PROVIDER_KINDS))}); ignoring the seat",
        )
        return None

    spec = base or SeatSpec(name=name, provider=provider or "")
    fields: dict[str, Any] = {"declared": True}
    if provider is not None:
        fields["provider"] = provider
    for key in ("model", "reasoning_effort", "print_timeout"):
        val = _str_field(tbl, key, name, layer)
        if val is not None:
            fields[key] = val
    opt_in = _bool_field(tbl, "opt_in", name, layer, on_bad=False)
    if opt_in is not None:
        fields["opt_in"] = opt_in
    # Availability is opt-OUT: a misconfigured value must never silently HIDE a
    # seat (a quietly-degraded panel), so a non-bool reads as available.
    available = _bool_field(tbl, "available", name, layer, on_bad=True)
    if available is not None:
        fields["available"] = available
    return replace(spec, **fields)


def _validate(spec: SeatSpec, layer: str) -> bool:
    """Whether ``spec`` may enter the catalog (warn-once + drop when not)."""
    name = spec.name
    # The name must be non-empty AND survive the slug unchanged: an empty name
    # slugs to the literal "seat" fallback, and any stripped character changes the
    # prompt/result file names the seat is written to (the strip is not injective,
    # so two names could collide on one file).
    if not name or _SLUG_CHARSET.sub("", name) != name:
        _warn_once(
            f"seat-name:{layer}:{name}",
            f"seat name {name!r} is empty or contains characters that are stripped "
            f"from the file names the seat's prompt and result are written to; "
            f"ignoring it",
        )
        return False
    if name in reserved_tokens():
        _warn_once(
            f"reserved:{layer}:{name}",
            f"seat name {name!r} is already a panel or group token (it resolves to "
            f"a list of seats); ignoring it",
        )
        return False
    if spec.kind.model_rule == "alias" and spec.model not in TASK_MODEL_ALIASES:
        _warn_once(
            f"task-model:{layer}:{name}",
            f"[seats.{name}].model={spec.model!r} is not a Task-tool model alias "
            f"({', '.join(sorted(TASK_MODEL_ALIASES))}); the Task tool would reject "
            f"it at spawn, so the seat is ignored",
        )
        return False
    return True


def shipped_catalog() -> dict[str, SeatSpec]:
    """The BUILT-IN roster from ``seats.toml`` alone, in file order. No config."""
    out: dict[str, SeatSpec] = {}
    for name, tbl in _seat_tables(_shipped()).items():
        if not isinstance(tbl, dict):
            continue
        spec = _spec_from_table(name, tbl, "shipped", None)
        if spec is None:
            continue
        spec = replace(spec, declared=False)
        if _validate(spec, "shipped"):
            out[name] = spec
    return out


def merged_catalog() -> dict[str, SeatSpec]:
    """The resolved catalog: shipped seats tuned by the config layers, plus any
    seat a config layer DECLARES. Memoized per process.

    Order: the shipped seats first (file order, which is fan-out order), then the
    config-declared ones in the order they were read (global, then per-repo).
    """
    global _catalog
    if _catalog is None:
        catalog = shipped_catalog()
        # Low precedence FIRST: each layer folds over the previous, so a per-repo
        # value lands last and wins, per key.
        for layer, data in reversed(config.raw_layers()):
            for name, tbl in _seat_tables(data).items():
                if not isinstance(tbl, dict):
                    _warn_once(
                        f"seat-table:{layer}:{name}",
                        f"[seats.{name}] must be a table; ignoring {tbl!r}",
                    )
                    continue
                spec = _spec_from_table(name, tbl, layer, catalog.get(name))
                if spec is not None and _validate(spec, layer):
                    catalog[name] = spec
        _catalog = catalog
    return _catalog


def seat_spec(name: str) -> SeatSpec | None:
    """The resolved spec for ``name``, or ``None`` when no such seat exists."""
    return merged_catalog().get(name)


def merged_panels() -> dict[str, list[str]]:
    """The resolved panels: the shipped ``[panels]`` table, overridden BY NAME by a
    configured one (whole list per name, no deep-merge of members). Memoized."""
    global _panels
    if _panels is None:
        shipped = _shipped().get("panels")
        panels: dict[str, list[str]] = {}
        if isinstance(shipped, dict):
            for name, members in shipped.items():
                if isinstance(members, list):
                    panels[name] = [m for m in members if isinstance(m, str)]
        configured = config.panels()
        if configured:
            panels.update(configured)
        _panels = panels
    return _panels


def group_tokens() -> dict[str, list[str]]:
    """Each group token → the catalog seats it expands to, in catalog order.

    A token names an executor-bearing provider KIND, so it grows with the catalog:
    declaring a new cursor seat puts it in the ``cursor`` group automatically.
    """
    catalog = merged_catalog()
    return {
        token: [n for n, s in catalog.items() if s.provider == token]
        for token in _shipped_group_tokens()
    }


def task_seats() -> list[str]:
    """The catalog's Task (Claude) seats, in catalog order — the seats whose
    provider kind has NO executor, so the orchestrator spawns them in-session."""
    return [n for n, s in merged_catalog().items() if not s.has_executor]


def premium_off_seats() -> tuple[str, ...]:
    """The opt-in subprocess seats the user has NOT declared: what the scaffolder
    writes an ``available = false`` line for. Derived, memoized (never an
    import-time constant: the catalog is not final until the config is read)."""
    global _premium_off
    if _premium_off is None:
        _premium_off = tuple(
            n for n, s in merged_catalog().items()
            if s.has_executor and s.opt_in and not s.declared
        )
    return _premium_off
