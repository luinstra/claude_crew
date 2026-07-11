"""Layered crew config loaders + typed validating getters.

Two small, optional, PERSONAL config files tune the default panel choice (when
the user names no panel), the panel ROSTER (``[panels]`` — what's *in* a preset),
per-seat AVAILABILITY (``available`` opt-out), and per-seat tuning (model pins,
codex ``reasoning_effort``, agy ``print_timeout``, and the global per-seat
``timeout``):

  * per-repo  ``<project>/.crew/config.toml`` (resolved against
    ``CLAUDE_PROJECT_DIR`` → cwd — the SAME resolution the other ``.crew/`` paths
    use), and
  * global    ``~/.crew-config.toml`` (resolved against ``$HOME`` via
    ``Path.home()``).

This module is a PURE loader: it imports NOTHING from ``cli``/``providers`` at
module load (so the providers can import it without a cycle, and it stays
independently testable) — the only ``seats``/``providers`` imports are LAZY,
inside the getters that validate against the catalog. Each file is loaded ONCE
per process (memoized, one cache per layer), and every getter VALIDATES and
returns ``None``/the default on a missing/invalid value so callers never see a
raw exception or a bad-typed value.

Precedence (enforced HERE — each getter resolves per-key, per-repo first):

    explicit CLI flag  >  per-repo .crew/config.toml  >  global ~/.crew-config.toml  >  built-in

For ``[seats.<name>]`` tables the resolution is PER-SEAT-PER-KEY: a per-repo
``[seats.codex].model`` wins over a global one, and a seat tuned only in the
global file still applies (no whole-table deep-merge — key-by-key resolution is
predictable). The ``CREW_MA_*`` env surface is RETIRED — it is consulted nowhere.

Error handling (a review seat must NEVER die because of a config typo):
  * Missing file → no-op; getters return ``None`` (callers use defaults).
  * No ``tomllib`` (Python < 3.11) → file ignored; behaves as today, PLUS a
    MANDATORY one-time stderr note (so a user who set ``default_panel="lite"`` to
    SAVE cost isn't silently given the full panel on the minimum interpreter).
  * Malformed TOML (``TOMLDecodeError``) → warn once, ignore the WHOLE file.
  * Bad individual field (wrong type / out-of-range) → that getter drops the
    field and keeps the built-in default, warning once; siblings unaffected.
  * Unknown ``default_panel``/``[panels]`` name or unknown seat in a roster list
    → validated against ``seats.PANEL_PRESETS`` ∪ ``panels()`` keys / the known
    seat names; returns ``None`` / drops the entry (with a one-time warn) so
    callers never hit a raw ``KeyError``.

ALL diagnostics go to STDERR (never stdout — the orchestrator parses stdout
JSON). Warnings fire AT MOST ONCE per process: each memoized load plus a
per-warning-key guard. The keys are LAYER-QUALIFIED (``<field>:<layer>``) so a
bad per-repo value and a bad global value each warn once, independently — a bad
per-repo value never swallows a legitimate global warning.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # < 3.11: file gracefully ignored (see _load_uncached)
    tomllib = None  # type: ignore[assignment]


# Sentinel distinguishing "never loaded" from a legitimately empty ({}) config.
_UNLOADED: Any = object()
_cache: Any = _UNLOADED          # per-repo .crew/config.toml
_global_cache: Any = _UNLOADED   # global ~/.crew-config.toml

# Keys of warnings already emitted this process (one-time-note guard).
_warned: set[str] = set()


def _project_dir() -> Path:
    """Project dir — ``CLAUDE_PROJECT_DIR`` → cwd (same as the other .crew paths)."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


def _config_path() -> Path:
    return _project_dir() / ".crew" / "config.toml"


def _global_config_path() -> Path:
    """Global per-user config: ``~/.crew-config.toml`` (a single home dotfile, NOT
    a ``~/.crew/`` dir, which would collide with a per-repo ``.crew/`` if crew were
    ever run from ``$HOME``). Resolved against ``$HOME`` via ``Path.home()``."""
    return Path.home() / ".crew-config.toml"


# --- public target-path wrappers (single source of truth for WHERE) ----------
# The scaffolder (``crew scaffold-config``) resolves its write targets through
# THESE so init and the runtime loader can never disagree on the file location.
# Thin delegators — the private functions stay the canonical implementation.

def repo_config_path() -> Path:
    """Public wrapper for the per-repo ``.crew/config.toml`` target path.

    Delegates to ``_config_path()`` so the scaffolder writes EXACTLY where the
    per-repo loader (``_load``) reads — no second copy of the path logic."""
    return _config_path()


def global_config_path() -> Path:
    """Public wrapper for the global ``~/.crew-config.toml`` target path.

    Delegates to ``_global_config_path()`` so the scaffolder writes EXACTLY where
    the global loader (``_global_load``) reads — no second copy of the path logic."""
    return _global_config_path()


def _warn_once(key: str, message: str) -> None:
    """Emit a one-time stderr note keyed by ``key`` (never to stdout)."""
    if key in _warned:
        return
    _warned.add(key)
    print(f"crew config: {message}", file=sys.stderr)


def _load_uncached(path: Path) -> dict:
    """Read + parse the config file at ``path``, returning ``{}`` on any failure.

    Shared by BOTH the per-repo and global loaders so the parse/error posture
    (missing → ``{}``; no-tomllib → one-time note; malformed → warn + ``{}``) is
    written once. Warn keys are path-qualified so the per-repo and global files
    each warn independently.
    """
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return {}
    except OSError:
        return {}

    # The file EXISTS. On Python < 3.11 there is no tomllib — emit the mandatory
    # one-time note and ignore the file (behavior identical to today otherwise).
    if tomllib is None:
        _warn_once(
            f"no-tomllib:{path}",
            f"found {path} but TOML parsing requires Python 3.11+; ignoring",
        )
        return {}

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        # tomllib cannot partially parse a syntactically broken file — drop the
        # whole file and fall back to defaults for everything.
        _warn_once(f"malformed:{path}", f"{path} is not valid TOML; ignoring the whole file")
        return {}

    return data if isinstance(data, dict) else {}


def _load() -> dict:
    """Return the memoized per-repo config dict (loaded once per process)."""
    global _cache
    if _cache is _UNLOADED:
        _cache = _load_uncached(_config_path())
    return _cache


def _global_load() -> dict:
    """Return the memoized global config dict (loaded once per process)."""
    global _global_cache
    if _global_cache is _UNLOADED:
        _global_cache = _load_uncached(_global_config_path())
    return _global_cache


def _reset_cache_for_tests() -> None:
    """Clear the memoized configs + one-time-warning guards (TESTS ONLY)."""
    global _cache, _global_cache
    _cache = _UNLOADED
    _global_cache = _UNLOADED
    _warned.clear()


def _first(*vals: Any) -> Any:
    """Return the first non-``None`` value (the per-repo → global combinator)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _seat_table(seat: str, data: dict) -> dict | None:
    """The ``[seats.<seat>]`` table in ``data``, or ``None`` if absent/not a table."""
    seats_tbl = data.get("seats")
    if not isinstance(seats_tbl, dict):
        return None
    tbl = seats_tbl.get(seat)
    return tbl if isinstance(tbl, dict) else None


# --- panel-name validation set (PANEL_PRESETS ∪ configured [panels] keys) ----

def _known_panel_names() -> set[str]:
    """Valid panel names: built-in ``PANEL_PRESETS`` keys ∪ configured ``[panels]``
    keys. Lazy-imports ``seats`` so this module stays a pure leaf at load."""
    from multiagent import seats

    names = set(seats.PANEL_PRESETS)
    roster = panels()
    if roster:
        names |= set(roster)
    return names


# --- default_panel ------------------------------------------------------------

def _extract_default_panel(data: dict, layer: str) -> str | None:
    val = data.get("default_panel")
    if val is None:
        return None
    if not isinstance(val, str) or val not in _known_panel_names():
        _warn_once(
            f"default_panel:{layer}",
            f"default_panel={val!r} is not a known panel "
            f"({', '.join(sorted(_known_panel_names()))}); ignoring",
        )
        return None
    return val


def default_panel() -> str | None:
    """The configured default panel name, validated against ``PANEL_PRESETS`` ∪
    ``panels()`` keys. ``None`` when unset/invalid in BOTH layers so the caller
    falls back to ``full`` and never hits a raw ``KeyError`` (per-repo wins)."""
    return _first(
        _extract_default_panel(_load(), "repo"),
        _extract_default_panel(_global_load(), "global"),
    )


# --- debate_panel -------------------------------------------------------------

def _extract_debate_panel(data: dict, layer: str) -> str | None:
    debate_tbl = data.get("debate")
    if not isinstance(debate_tbl, dict):
        return None
    val = debate_tbl.get("panel")
    if val is None:
        return None
    if not isinstance(val, str) or val not in _known_panel_names():
        _warn_once(
            f"debate_panel:{layer}",
            f"[debate].panel={val!r} is not a known panel "
            f"({', '.join(sorted(_known_panel_names()))}); ignoring",
        )
        return None
    return val


def debate_panel() -> str | None:
    """The configured ``/crew:debate`` default panel (``[debate].panel``), validated
    against ``PANEL_PRESETS`` ∪ ``panels()`` keys (per-repo wins). ``None`` when
    unset/invalid so the caller falls back to ``default_panel()`` then ``full``."""
    return _first(
        _extract_debate_panel(_load(), "repo"),
        _extract_debate_panel(_global_load(), "global"),
    )


# --- dispatch seat ------------------------------------------------------------

def _extract_dispatch_seat(data: dict, layer: str) -> str | None:
    """Validate one layer's ``[dispatch].seat`` against the SEAT registry.

    ⚠ Mirrors ``_extract_debate_panel``'s SHAPE but NOT its validation TARGET:
    dispatch resolves to ONE concrete registered subprocess seat, so the value is
    checked against ``known_seat_names()`` (the seat registry) — NOT
    ``_known_panel_names()``. A panel name or a group token like ``"cursor"`` is
    therefore INVALID here (warn once + ``None``), since dispatch is single-seat
    and a group token names no concrete seat.
    """
    dispatch_tbl = data.get("dispatch")
    if not isinstance(dispatch_tbl, dict):
        return None
    val = dispatch_tbl.get("seat")
    if val is None:
        return None
    # Lazy import (call-time) keeps this module a pure leaf at load (same pattern
    # as _known_panel_names).
    from multiagent.providers import known_seat_names

    if not isinstance(val, str) or val not in known_seat_names():
        _warn_once(
            f"dispatch_seat:{layer}",
            f"[dispatch].seat={val!r} is not a known subprocess seat "
            f"({', '.join(known_seat_names())}); ignoring",
        )
        return None
    return val


def dispatch_seat() -> str | None:
    """The configured ``/crew:dispatch`` default seat (``[dispatch].seat``),
    validated against ``known_seat_names()`` (per-repo wins). ``None`` when
    unset/invalid in BOTH layers so the caller falls back to the built-in
    ``codex``."""
    return _first(
        _extract_dispatch_seat(_load(), "repo"),
        _extract_dispatch_seat(_global_load(), "global"),
    )


# --- per-seat model -----------------------------------------------------------

def _extract_seat_model(data: dict, layer: str, seat: str) -> str | None:
    tbl = _seat_table(seat, data)
    if tbl is None:
        return None
    val = tbl.get("model")
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        _warn_once(
            f"seat_model:{layer}:{seat}",
            f"[seats.{seat}].model must be a non-empty string; ignoring {val!r}",
        )
        return None
    return val


def seat_model(seat: str) -> str | None:
    """The ``[seats.<seat>].model`` pin (per-repo over global), or ``None``."""
    return _first(
        _extract_seat_model(_load(), "repo", seat),
        _extract_seat_model(_global_load(), "global", seat),
    )


# --- codex reasoning_effort ---------------------------------------------------

def _extract_codex_reasoning_effort(data: dict, layer: str, seat: str) -> str | None:
    tbl = _seat_table(seat, data)
    if tbl is None:
        return None
    val = tbl.get("reasoning_effort")
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        _warn_once(
            f"codex_reasoning_effort:{layer}:{seat}",
            f"[seats.{seat}].reasoning_effort must be a non-empty string; "
            f"ignoring {val!r}",
        )
        return None
    return val


def codex_reasoning_effort(seat: str = "codex") -> str | None:
    """``[seats.<seat>].reasoning_effort`` (per-repo over global), or ``None``.

    Per-seat lookup: each codex seat (codex, codex-luna) reads its own table.
    The ``seat`` default keeps existing bare-``codex`` callers unchanged."""
    return _first(
        _extract_codex_reasoning_effort(_load(), "repo", seat),
        _extract_codex_reasoning_effort(_global_load(), "global", seat),
    )


# --- agy print_timeout --------------------------------------------------------

def _extract_agy_print_timeout(data: dict, layer: str) -> str | None:
    tbl = _seat_table("agy", data)
    if tbl is None:
        return None
    val = tbl.get("print_timeout")
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        _warn_once(
            f"agy_print_timeout:{layer}",
            f"[seats.agy].print_timeout must be a non-empty string; ignoring {val!r}",
        )
        return None
    return val


def agy_print_timeout() -> str | None:
    """``[seats.agy].print_timeout`` (per-repo over global), or ``None``."""
    return _first(
        _extract_agy_print_timeout(_load(), "repo"),
        _extract_agy_print_timeout(_global_load(), "global"),
    )


# --- default timeout ----------------------------------------------------------

def _extract_default_timeout(data: dict, layer: str) -> int | None:
    tuning = data.get("tuning")
    if not isinstance(tuning, dict):
        return None
    val = tuning.get("timeout")
    if val is None:
        return None
    # bool is a subclass of int — reject it explicitly so `timeout = true`
    # doesn't sneak through as 1.
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        _warn_once(
            f"timeout:{layer}",
            f"[tuning].timeout must be a positive integer; ignoring {val!r}",
        )
        return None
    return val


def default_timeout() -> int | None:
    """``[tuning].timeout`` per-seat wall-clock default (per-repo over global), or
    ``None`` so the caller keeps its built-in default."""
    return _first(
        _extract_default_timeout(_load(), "repo"),
        _extract_default_timeout(_global_load(), "global"),
    )


# --- [panels] roster (new) ----------------------------------------------------

def _extract_panels(data: dict, layer: str) -> dict[str, list[str]] | None:
    """Validate one layer's ``[panels]`` table → ``{name: [seat, ...]}`` or ``None``.

    Each entry must be a list of KNOWN seat names (registry subprocess seats ∪
    ``TASK_SEAT_NAMES`` ∪ the ``cursor`` group token, for parity with ``--seats``);
    unknown names are dropped (warn once). An entry that isn't a list, or whose
    every element drops, is omitted. A present-but-non-table ``[panels]`` → ``None``
    (warn once)."""
    tbl = data.get("panels")
    if tbl is None:
        return None
    if not isinstance(tbl, dict):
        _warn_once(f"panels:{layer}", f"[panels] must be a table; ignoring {tbl!r}")
        return None

    # Lazy import (call-time) keeps this module a pure leaf at load.
    from multiagent import seats
    from multiagent.providers import known_seat_names

    valid = set(known_seat_names()) | set(seats.TASK_SEAT_NAMES) | {"cursor"}
    out: dict[str, list[str]] = {}
    for name, members in tbl.items():
        if not isinstance(members, list):
            _warn_once(
                f"panels:{layer}:{name}",
                f"[panels].{name} must be a list of seat names; ignoring {members!r}",
            )
            continue
        kept: list[str] = []
        for m in members:
            if isinstance(m, str) and m in valid:
                kept.append(m)
            else:
                _warn_once(
                    f"panels:{layer}:{name}:{m!r}",
                    f"[panels].{name} names unknown seat {m!r}; dropping it",
                )
        if kept:
            out[name] = kept
    return out or None


def panels() -> dict[str, list[str]] | None:
    """The resolved ``[panels]`` roster (per-repo overrides global PER NAME — whole
    list per name, no deep-merge of list contents), or ``None`` when neither layer
    defines a valid ``[panels]`` table."""
    repo = _extract_panels(_load(), "repo")
    glob = _extract_panels(_global_load(), "global")
    if repo is None and glob is None:
        return None
    merged: dict[str, list[str]] = {}
    if glob:
        merged.update(glob)
    if repo:
        merged.update(repo)  # per-repo wins per NAME
    return merged or None


# --- per-seat availability (new, opt-out) -------------------------------------

def _extract_seat_available(data: dict, layer: str, seat: str) -> bool | None:
    tbl = _seat_table(seat, data)
    if tbl is None:
        return None
    val = tbl.get("available")
    if val is None:
        return None
    if not isinstance(val, bool):
        # Availability is opt-OUT: a misconfigured value must NEVER silently HIDE a
        # seat (a quietly-degraded panel), so a non-bool is treated as available.
        _warn_once(
            f"seat_available:{layer}:{seat}",
            f"[seats.{seat}].available must be a boolean; ignoring {val!r} "
            f"(treating the seat as available)",
        )
        return True
    return val


def seat_available(seat: str) -> bool:
    """Whether ``seat`` is available (per-repo ``available`` over global), default
    ``True`` (opt-out). An unavailable seat is filtered out of any resolved panel
    (with a skip-note when explicitly named)."""
    val = _first(
        _extract_seat_available(_load(), "repo", seat),
        _extract_seat_available(_global_load(), "global", seat),
    )
    return True if val is None else val
