"""Layered crew config loaders + typed validating getters.

Two small, optional, PERSONAL config files tune the default panel choice (when
the user names no panel), the panel ROSTER (``[panels]``: what's *in* a preset),
the SEAT catalog (``[seats.<name>]`` for availability, model pins, per-provider
tunes, and whole new seats: resolved in ``seats.py`` over ``raw_layers()``, not
by a getter here), the global per-seat ``timeout``, and the persistence loops'
wall clock (``[tuning].deadline_minutes``):

  * per-repo  ``<project>/.crew/config.toml`` (resolved against the shared
    ``crew_base()`` root, ``CLAUDE_PROJECT_DIR`` first then cwd: the ONE resolver
    every ``.crew/`` path in the codebase now derives from, state layer and review
    engine alike), and
  * global    ``~/.crew-config.toml`` (resolved against ``$HOME`` via
    ``Path.home()``).

This module is a PURE loader: it imports NOTHING from ``cli``/``providers`` at
module load (so the providers can import it without a cycle, and it stays
independently testable): the only ``seats``/``providers`` imports are LAZY,
inside the getters that validate against the catalog. Each file is loaded ONCE
per process (memoized, one cache per layer), and every getter VALIDATES and
returns ``None``/the default on a missing/invalid value so callers never see a
raw exception or a bad-typed value.

Precedence (enforced HERE, each getter resolves per-key, per-repo first):

    explicit CLI flag  >  per-repo .crew/config.toml  >  global ~/.crew-config.toml  >  built-in

For ``[seats.<name>]`` tables the resolution is PER-SEAT-PER-KEY: a per-repo
``[seats.codex].model`` wins over a global one, and a seat tuned only in the
global file still applies (no whole-table deep-merge, key-by-key resolution is
predictable). The ``CREW_MA_*`` env surface is RETIRED: it is consulted nowhere.

Error handling (a review seat must NEVER die because of a config typo):
  * Missing file → no-op; getters return ``None`` (callers use defaults).
  * Malformed TOML (``TOMLDecodeError``) → warn once, ignore the WHOLE file.
  * Bad individual field (wrong type / out-of-range) → that getter drops the
    field and keeps the built-in default, warning once; siblings unaffected.
  * Unknown ``default_panel``/``[panels]`` name or unknown seat in a roster list
    → validated against ``seats.merged_panels()`` keys / the known seat names;
    returns ``None`` / drops the entry (with a one-time warn) so callers never hit
    a raw ``KeyError``.

ALL diagnostics go to STDERR (never stdout, the orchestrator parses stdout
JSON). Warnings fire AT MOST ONCE per process: each memoized load plus a
per-warning-key guard. The keys are LAYER-QUALIFIED (``<field>:<layer>``) so a
bad per-repo value and a bad global value each warn once, independently: a bad
per-repo value never swallows a legitimate global warning.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import tomllib  # stdlib 3.11+; the dispatcher (plugins/crew/crew) enforces that floor

from state_discovery import crew_base  # the ONE `.crew` root resolver (leaf, no cycle)


# Sentinel distinguishing "never loaded" from a legitimately empty ({}) config.
_UNLOADED: Any = object()
_cache: Any = _UNLOADED          # per-repo .crew/config.toml
_global_cache: Any = _UNLOADED   # global ~/.crew-config.toml

# Keys of warnings already emitted this process (one-time-note guard).
_warned: set[str] = set()


def _project_dir() -> Path:
    """Project dir: the shared ``crew_base()`` resolver (``CLAUDE_PROJECT_DIR`` →
    cwd), the ONE source every ``.crew/`` path derives from."""
    return crew_base()


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
    (missing → ``{}``; malformed → warn + ``{}``) is written once. Warn keys are
    path-qualified so the per-repo and global files each warn independently.
    """
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return {}
    except OSError:
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


def raw_layers() -> list[tuple[str, dict]]:
    """The config layers as raw dicts, HIGHEST precedence first: per-repo, global.

    The one way out of this module for un-validated config data. The seat catalog
    (``seats.py``) does its own per-seat-per-key resolution over these, because a
    seat's tunes belong to the seat, not to a getter per key.
    """
    return [("repo", _load()), ("global", _global_load())]


def _reset_cache_for_tests() -> None:
    """Clear the memoized configs + one-time-warning guards (TESTS ONLY).

    The ONE reset path: it also clears everything DERIVED from the config, since a
    stale derived cache outlives the config it was built from. That means the seat
    catalog + its own warn guard (``seats._reset_for_tests``) and the provider
    registry, whose factories are bound to the catalog's rows. The registry import
    is function-local so this module stays a leaf that providers can import.
    """
    global _cache, _global_cache
    _cache = _UNLOADED
    _global_cache = _UNLOADED
    _warned.clear()

    from multiagent import providers, seats

    seats._reset_for_tests()
    providers._REGISTRY = None


def _first(*vals: Any) -> Any:
    """Return the first non-``None`` value (the per-repo → global combinator)."""
    for v in vals:
        if v is not None:
            return v
    return None


# --- panel-name validation set (shipped panels ∪ configured [panels] keys) ---

def _known_panel_names() -> set[str]:
    """Valid panel names: the shipped panels ∪ the configured ``[panels]`` keys,
    which is exactly what the catalog's ``merged_panels()`` resolves. Lazy-imports
    ``seats`` so this module stays a pure leaf at load."""
    from multiagent import seats

    return set(seats.merged_panels())


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
    """The configured default panel name, validated against the resolved panel
    names (``seats.merged_panels()``). ``None`` when unset/invalid in BOTH layers so
    the caller falls back to ``full`` and never hits a raw ``KeyError`` (per-repo
    wins)."""
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
    against the resolved panel names (``seats.merged_panels()``, per-repo wins).
    ``None`` when unset/invalid so the caller falls back to ``default_panel()`` then
    ``full``."""
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


# --- loop deadline ------------------------------------------------------------

def _extract_deadline_minutes(data: dict, layer: str) -> int | None:
    tuning = data.get("tuning")
    if not isinstance(tuning, dict):
        return None
    val = tuning.get("deadline_minutes")
    if val is None:
        return None
    # Lazy import (call-time) keeps this module a pure leaf at load, and keeps the
    # policy ceiling single-sourced with the one `crew state init` enforces.
    from models import MAX_DEADLINE_MINUTES, NO_DEADLINE

    # bool is an int subclass: reject it explicitly. The bounds are the same
    # ones `--deadline-minutes` is policed by. Exactly 0 is the deliberate
    # clock-off opt-out (the stop-fires cap keeps bounding the loop), honored
    # from the GLOBAL layer only: ~/.crew-config.toml lives outside the project
    # and takes an out-of-band edit, while the per-repo .crew/config.toml sits
    # in the tree the bounded agent writes to routinely, which would make the
    # opt-out one Write away from the thing it bounds.
    #
    # This closes the ROUTINE channels (templated CLI, in-tree config), which is
    # the whole promise: like every bound here, it is a convention, not a
    # sandbox. A caller that fakes HOME or hand-writes the state marker has left
    # the sanctioned paths as deliberately and visibly as one that edits the
    # real global file, and no config-layer logic can attest filesystem
    # identity.
    if (not isinstance(val, bool) and isinstance(val, int)
            and val == NO_DEADLINE):
        if layer == "global":
            return NO_DEADLINE
        _warn_once(
            f"deadline_minutes:{layer}",
            f"[tuning].deadline_minutes = 0 (no deadline) is honored only in "
            f"the global ~/.crew-config.toml; ignoring it here",
        )
        return None
    if (isinstance(val, bool) or not isinstance(val, int)
            or val <= 0 or val > MAX_DEADLINE_MINUTES):
        _warn_once(
            f"deadline_minutes:{layer}",
            f"[tuning].deadline_minutes must be an integer between 1 and "
            f"{MAX_DEADLINE_MINUTES} (or 0, global file only); ignoring {val!r}",
        )
        return None
    return val


def deadline_minutes() -> int | None:
    """``[tuning].deadline_minutes``, the persistence loops' wall-clock bound
    (per-repo over global), or ``None`` so `crew state init` keeps its built-in
    default. An explicit ``--deadline-minutes`` flag still wins over both."""
    return _first(
        _extract_deadline_minutes(_load(), "repo"),
        _extract_deadline_minutes(_global_load(), "global"),
    )


# --- [panels] roster (new) ----------------------------------------------------

def _extract_panels(data: dict, layer: str) -> dict[str, list[str]] | None:
    """Validate one layer's ``[panels]`` table → ``{name: [seat, ...]}`` or ``None``.

    Each entry must be a list of KNOWN seat names (registry subprocess seats ∪ the
    catalog's Task seats ∪ the group tokens, for parity with ``--seats``); unknown
    names are dropped (warn once). An entry that isn't a list, or whose every
    element drops, is omitted. A present-but-non-table ``[panels]`` → ``None``
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

    seat_names = set(known_seat_names()) | set(seats.task_seats())
    valid = seat_names | set(seats.group_tokens())
    out: dict[str, list[str]] = {}
    for name, members in tbl.items():
        if not isinstance(members, list):
            _warn_once(
                f"panels:{layer}:{name}",
                f"[panels].{name} must be a list of seat names; ignoring {members!r}",
            )
            continue
        # Panel names and SEAT names are one namespace (--panel vs --seats would
        # otherwise resolve the same word to two different rosters). The seat
        # wins: a panel row named after a live seat is dropped. Group tokens are
        # deliberately NOT in this check: `cursor` is both a group token and a
        # built-in preset name, and redefining the built-in presets via [panels]
        # is a documented capability (no seat can bear a group-token name, so no
        # real collision is possible there).
        if name in seat_names:
            _warn_once(
                f"panels:{layer}:{name}:seat-collision",
                f"[panels].{name} collides with a seat of the same name; "
                f"ignoring the panel (the seat wins)",
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
