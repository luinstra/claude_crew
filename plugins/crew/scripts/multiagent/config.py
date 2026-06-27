"""Per-repo ``.crew/config.toml`` loader + typed validating getters.

A small, optional, PERSONAL per-repo config file. It controls the default panel
choice (when the user names no panel) and per-seat tuning (model pins, codex
``reasoning_effort``, agy ``print_timeout``, and the global per-seat
``timeout``) — a file form of the ``CREW_MA_*`` env surface.

This module is a PURE loader: it imports NOTHING from ``cli``/``providers`` (so
the providers can import it without a cycle, and it stays independently
testable). It resolves ``.crew/config.toml`` relative to the project dir
(``CLAUDE_PROJECT_DIR`` → cwd — the SAME resolution the other ``.crew/`` paths
use), loads it ONCE per process (memoized), and exposes small typed getters that
VALIDATE and return ``None`` on a missing/invalid value so callers never see a
raw exception or a bad-typed value.

Precedence (enforced at the CALL SITES, not here):

    explicit CLI flag  >  this config  >  CREW_MA_* env (legacy)  >  built-in

so each call site reads ``cli_arg or config.x() or os.environ.get(env, default)``
— config goes ABOVE the existing env read, NOT in place of the built-in default.

Error handling (a review seat must NEVER die because of a config typo):
  * Missing file → no-op; getters return ``None`` (callers use defaults).
  * No ``tomllib`` (Python < 3.11) → file ignored; behaves as today, PLUS a
    MANDATORY one-time stderr note (so a user who set ``default_panel="lite"`` to
    SAVE cost isn't silently given the full panel on the minimum interpreter).
  * Malformed TOML (``TOMLDecodeError``) → warn once, ignore the WHOLE file.
  * Bad individual field (wrong type / out-of-range) → that getter drops the
    field and keeps the built-in default, warning once; siblings unaffected.
  * Unknown ``default_panel`` value → validated against ``seats.PANEL_PRESETS``
    keys; returns ``None`` (with a one-time warn) so callers never hit a raw
    ``KeyError``.

ALL diagnostics go to STDERR (never stdout — the orchestrator parses stdout
JSON). Warnings fire AT MOST ONCE per process (a property of the memoized load
plus a per-warning-key guard).
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
_cache: Any = _UNLOADED

# Keys of warnings already emitted this process (one-time-note guard).
_warned: set[str] = set()


def _project_dir() -> Path:
    """Project dir — ``CLAUDE_PROJECT_DIR`` → cwd (same as the other .crew paths)."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


def _config_path() -> Path:
    return _project_dir() / ".crew" / "config.toml"


def _warn_once(key: str, message: str) -> None:
    """Emit a one-time stderr note keyed by ``key`` (never to stdout)."""
    if key in _warned:
        return
    _warned.add(key)
    print(f"crew config: {message}", file=sys.stderr)


def _load_uncached() -> dict:
    """Read + parse the config file, returning ``{}`` on any failure."""
    path = _config_path()
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
            "no-tomllib",
            f"found {path} but TOML parsing requires Python 3.11+; ignoring",
        )
        return {}

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        # tomllib cannot partially parse a syntactically broken file — drop the
        # whole file and fall back to defaults for everything.
        _warn_once("malformed", f"{path} is not valid TOML; ignoring the whole file")
        return {}

    return data if isinstance(data, dict) else {}


def _load() -> dict:
    """Return the memoized parsed config dict (loaded once per process)."""
    global _cache
    if _cache is _UNLOADED:
        _cache = _load_uncached()
    return _cache


def _reset_cache_for_tests() -> None:
    """Clear the memoized config + one-time-warning guards (TESTS ONLY)."""
    global _cache
    _cache = _UNLOADED
    _warned.clear()


def _seat_table(seat: str) -> dict | None:
    """The ``[seats.<seat>]`` table, or ``None`` if absent/not a table."""
    seats_tbl = _load().get("seats")
    if not isinstance(seats_tbl, dict):
        return None
    tbl = seats_tbl.get(seat)
    return tbl if isinstance(tbl, dict) else None


def default_panel() -> str | None:
    """The configured default panel name, validated against ``PANEL_PRESETS``.

    Returns ``None`` when unset, the wrong type, or not a known preset — so the
    caller falls back to ``full`` and never hits a raw ``KeyError``.
    """
    val = _load().get("default_panel")
    if val is None:
        return None
    # Lazy import keeps this module a pure leaf (seats.py is pure data, but the
    # lazy import means config imports literally nothing at module load).
    from multiagent import seats

    if not isinstance(val, str) or val not in seats.PANEL_PRESETS:
        _warn_once(
            "default_panel",
            f"default_panel={val!r} is not a known panel "
            f"({', '.join(sorted(seats.PANEL_PRESETS))}); ignoring",
        )
        return None
    return val


def debate_panel() -> str | None:
    """The configured ``/crew:debate`` default panel, validated against ``PANEL_PRESETS``.

    Reads the ``[debate].panel`` key. Returns ``None`` when unset, the wrong
    type, or not a known preset — so the caller falls back to ``default_panel()``
    and then the built-in ``full`` (debate precedence: CLI ``--panel``/``--seats``
    > ``[debate].panel`` > ``default_panel`` > ``full``). Validated EXACTLY like
    ``default_panel()`` so an unknown value never reaches a raw ``KeyError``.
    """
    debate_tbl = _load().get("debate")
    if not isinstance(debate_tbl, dict):
        return None
    val = debate_tbl.get("panel")
    if val is None:
        return None
    # Lazy import keeps this module a pure leaf (see default_panel).
    from multiagent import seats

    if not isinstance(val, str) or val not in seats.PANEL_PRESETS:
        _warn_once(
            "debate_panel",
            f"[debate].panel={val!r} is not a known panel "
            f"({', '.join(sorted(seats.PANEL_PRESETS))}); ignoring",
        )
        return None
    return val


def seat_model(seat: str) -> str | None:
    """The ``[seats.<seat>].model`` pin, or ``None`` if unset/invalid."""
    tbl = _seat_table(seat)
    if tbl is None:
        return None
    val = tbl.get("model")
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        _warn_once(
            f"seat_model:{seat}",
            f"[seats.{seat}].model must be a non-empty string; ignoring {val!r}",
        )
        return None
    return val


def codex_reasoning_effort() -> str | None:
    """The ``[seats.codex].reasoning_effort`` value, or ``None`` if unset/invalid."""
    tbl = _seat_table("codex")
    if tbl is None:
        return None
    val = tbl.get("reasoning_effort")
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        _warn_once(
            "codex_reasoning_effort",
            f"[seats.codex].reasoning_effort must be a non-empty string; "
            f"ignoring {val!r}",
        )
        return None
    return val


def agy_print_timeout() -> str | None:
    """The ``[seats.agy].print_timeout`` value, or ``None`` if unset/invalid."""
    tbl = _seat_table("agy")
    if tbl is None:
        return None
    val = tbl.get("print_timeout")
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        _warn_once(
            "agy_print_timeout",
            f"[seats.agy].print_timeout must be a non-empty string; ignoring {val!r}",
        )
        return None
    return val


def default_timeout() -> int | None:
    """The ``[tuning].timeout`` per-seat wall-clock default (seconds).

    Returns ``None`` when unset or invalid (wrong type / non-positive), so the
    caller keeps its env/built-in default.
    """
    tuning = _load().get("tuning")
    if not isinstance(tuning, dict):
        return None
    val = tuning.get("timeout")
    if val is None:
        return None
    # bool is a subclass of int — reject it explicitly so `timeout = true`
    # doesn't sneak through as 1.
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        _warn_once(
            "timeout",
            f"[tuning].timeout must be a positive integer; ignoring {val!r}",
        )
        return None
    return val
