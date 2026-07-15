"""The seat catalog LOADER: shipped ``seats.toml`` merged with the config layers.

``seats.toml`` (next to this file) is the single source of the roster: every seat
and every panel, in order. This module reads it, merges the user's
``[seats.<name>]`` / ``[panels]`` tables over it (per-repo > global > shipped,
resolved per-seat-per-key), and hands back ``SeatSpec`` rows. Adding a seat is a
TOML edit, in this repo OR in a personal config: a declared seat with a known
``provider`` and an explicit ``model`` is a first-class seat (registry entry,
panel member, dispatch seat). Its fields may be SPLIT across the two config
files: rows are folded per key across the layers before any requirement is
judged, so the provider in one file and the tunes in the other still make one
seat.

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
(a shipped panel name or a group token, which resolve to seat LISTS), when it
declares a provider but never (in any layer) an explicit model, or when a
``claude-code`` seat's MERGED spec pins a model the Task tool would reject (a
SHIPPED seat whose tune fails that rule reverts to its shipped row instead).
A configured ``[panels]`` entry named after a live seat is resolved on the
PANEL side: config's panel parsing drops that row, the seat wins.
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

    declared: the seat is ABSENT from the shipped catalog, i.e. a config layer
    introduced it. Tuning a shipped seat does NOT set it: the scaffold filter and
    the premium-off derivation key on it, and pinning a knob on a built-in seat
    must not change their answer.
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

    # declared marks a seat ABSENT from the shipped catalog, not "a config layer
    # touched it": a tuned shipped seat keeps declared=False, so the consumers
    # keyed on it (the scaffold filter, the premium-off derivation) do not change
    # their answer just because the user pinned a knob on a built-in seat.
    spec = base or SeatSpec(name=name, provider=provider or "", declared=True)
    fields: dict[str, Any] = {}
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
    # The alias rule is judged on the MERGED spec in the final sweep, never on a
    # config fold: a bad or not-yet-complete value one layer holds may be fixed
    # by the next (repo over global, per key), and rejecting mid-fold leaves a
    # part-applied seat. Only the shipped file's own rows are checked here.
    if spec.kind.model_rule == "alias" and layer == "shipped" \
            and spec.model not in TASK_MODEL_ALIASES:
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
        # Kept verbatim for the final sweep's per-key fallback: a shipped seat
        # whose merged provider/model combination fails validation falls back to
        # these two keys (independent tunes like `available` are preserved).
        shipped_specs = dict(catalog)
        # Per-seat model candidates in precedence order (shipped first, then the
        # config layers low to high): the final alias sweep picks the highest
        # VALID one, so a bad override degrades to the previous value per key,
        # never to a dropped seat or a silently-reverted row.
        model_candidates: dict[str, list[str]] = {
            name: [s.model] for name, s in catalog.items() if s.model is not None
        }
        # Low precedence FIRST: each layer folds over the previous, so a per-repo
        # value lands last and wins, per key.
        #
        # A declared seat may be SPLIT across layers: its tunes (model, available,
        # ...) in one file and its provider in the other. A provider-less row for
        # an unknown name is therefore DEFERRED, not dropped: dropping it at its
        # own layer would silently lose the tunes (including an explicit
        # available = false) the moment a later layer completes the seat. Only a
        # row no layer ever completes is the typo'd-name case worth a warn.
        pending: dict[str, list[tuple[str, dict]]] = {}
        for layer, data in reversed(config.raw_layers()):
            for name, tbl in _seat_tables(data).items():
                if not isinstance(tbl, dict):
                    _warn_once(
                        f"seat-table:{layer}:{name}",
                        f"[seats.{name}] must be a table; ignoring {tbl!r}",
                    )
                    continue
                base = catalog.get(name)
                if base is None:
                    provider = _str_field(tbl, "provider", name, layer)
                    if provider is None:
                        pending.setdefault(name, []).append((layer, tbl))
                        continue
                    if provider in PROVIDER_KINDS:
                        # Seed with the provider so the deferred lower-precedence
                        # rows fold first and this row's keys still win, per key.
                        base = SeatSpec(name=name, provider=provider, declared=True)
                        for p_layer, p_tbl in pending.pop(name, []):
                            pm = _str_field(p_tbl, "model", name, p_layer)
                            if pm is not None:
                                model_candidates.setdefault(name, []).append(pm)
                            base = _spec_from_table(name, p_tbl, p_layer, base) or base
                spec = _spec_from_table(name, tbl, layer, base)
                if spec is not None and _validate(spec, layer):
                    m = _str_field(tbl, "model", name, layer)
                    if m is not None:
                        model_candidates.setdefault(name, []).append(m)
                    catalog[name] = spec
        for name, rows in pending.items():
            for layer, _tbl in rows:
                _warn_once(
                    f"unknown-seat:{layer}:{name}",
                    f"[seats.{name}] names no known seat and declares no provider; "
                    f"ignoring it",
                )
        # Final-state validation, judged on the MERGED spec so a requirement one
        # layer satisfies is never enforced mid-fold. A failing DECLARED seat is
        # dropped; a failing SHIPPED seat degrades per key (a config typo must
        # never remove a built-in seat).
        # (Seat-name/panel-name collisions are NOT handled here: config's
        # [panels] parsing drops a panel row named after a live seat, so the
        # seat wins and the namespace stays unambiguous without this module
        # re-deriving panel validity.)
        for name, spec in list(catalog.items()):
            if spec.declared and spec.model is None:
                _warn_once(
                    f"no-model:{name}",
                    f"[seats.{name}] declares provider {spec.provider!r} but no "
                    f"model; a declared seat needs an explicit model, ignoring it",
                )
                del catalog[name]
            elif spec.kind.model_rule == "alias" and spec.model not in TASK_MODEL_ALIASES:
                # Per-key fallback: the highest-precedence VALID candidate wins,
                # so a bad override degrades to the value beneath it and every
                # independent field (available, opt_in, ...) is preserved.
                chosen = next((m for m in reversed(model_candidates.get(name, []))
                               if m in TASK_MODEL_ALIASES), None)
                if chosen is not None:
                    _warn_once(
                        f"task-model:merged:{name}",
                        f"[seats.{name}].model={spec.model!r} is not a Task-tool "
                        f"model alias ({', '.join(sorted(TASK_MODEL_ALIASES))}); "
                        f"falling back to {chosen!r}",
                    )
                    catalog[name] = replace(spec, model=chosen)
                elif spec.declared:
                    _warn_once(
                        f"task-model:merged:{name}",
                        f"[seats.{name}].model={spec.model!r} is not a Task-tool "
                        f"model alias ({', '.join(sorted(TASK_MODEL_ALIASES))}); "
                        f"the Task tool would reject it at spawn, so the seat "
                        f"is ignored",
                    )
                    del catalog[name]
                else:
                    # A shipped seat converted to claude-code with no valid alias
                    # anywhere: the conversion itself is the invalid part, so
                    # provider and model revert to the shipped pair and the
                    # independent tunes stay.
                    _warn_once(
                        f"task-model:merged:{name}",
                        f"[seats.{name}] converts a shipped seat to claude-code "
                        f"but {spec.model!r} is not a Task-tool model alias; "
                        f"ignoring the conversion",
                    )
                    catalog[name] = replace(
                        spec,
                        provider=shipped_specs[name].provider,
                        model=shipped_specs[name].model,
                    )
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
    """The SHIPPED opt-in subprocess seats: what the scaffolder writes an
    ``available = false`` line for. Config-declared seats are excluded (the user
    who declared one needs no reminder it exists). Derived, memoized (never an
    import-time constant: the catalog is not final until the config is read)."""
    global _premium_off
    if _premium_off is None:
        _premium_off = tuple(
            n for n, s in merged_catalog().items()
            if s.has_executor and s.opt_in and not s.declared
        )
    return _premium_off
