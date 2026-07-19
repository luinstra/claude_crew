"""Debate run lifecycle — pure filesystem, no model invocation.

A *debate run* is a directory holding the question and one record per round:

    <base_dir>/<run_id>/
        question.md        the debate question (written once, at run start)
        round-01.md        round 1: all seats' positions
        round-02.md        round 2: cross-critique referencing round 1
        ...

This module owns ONLY run-id minting + validation, the directory layout, and
reading/writing those files. It NEVER spawns a model or builds a prompt — the
engine (cli.py) and the orchestrator markdown call into here for the on-disk
substrate that lets multi-round context flow to every seat by the same means
(prompts.build_prompt(..., prior_round=read_prior_rounds(...))). Keeping it
side-effect-light and model-free is what makes the round machinery unit-testable.

``run_id`` is constrained to ``^run-[A-Za-z0-9_-]+$`` — the ``run-`` prefix marks
a debate dir (so stale-dir cleanup can target ``run-*`` safely) and the charset
admits no ``/``, ``.`` or ``..``, so a hostile ``--run-id`` can never escape
``base_dir`` (path-traversal guard).
"""

from __future__ import annotations

import re
from pathlib import Path

from state_discovery import crew_base  # the ONE `.crew` root resolver (leaf, no cycle)

RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9_-]+$")


def _default_base() -> str:
    """The anchored `.crew/debates` root, resolved at CALL time so a caller that
    omits ``base_dir=`` inherits the shared ``crew_base()`` root, not a cwd-relative
    path."""
    return str(crew_base() / ".crew" / "debates")


class RoundError(Exception):
    """Raised with a clear, user-facing message (never a raw traceback)."""


def slugify(text: str, *, max_len: int = 40) -> str:
    """Lowercase, collapse to ``[a-z0-9-]``, trim — safe for a dir-name segment."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or "debate"


def new_run_id(slug: str = "", *, ts: str) -> str:
    """Mint a run-id ``run-<ts>[-<slug>]``.

    ``ts`` is supplied by the caller (e.g. a timestamp string) so this stays
    deterministic and testable — this module never reads the clock itself.
    """
    ts_clean = re.sub(r"[^A-Za-z0-9]+", "", ts) or "0"
    s = slugify(slug) if slug else ""
    run_id = f"run-{ts_clean}-{s}" if s else f"run-{ts_clean}"
    return validate_run_id(run_id)


def validate_run_id(run_id: str) -> str:
    """Return ``run_id`` unchanged if valid; raise ``RoundError`` otherwise.

    The path-traversal guard: a valid run-id has no separators, so it can only
    ever name a single child directory of ``base_dir``.
    """
    if not run_id or not RUN_ID_RE.match(run_id):
        raise RoundError(
            f"invalid run-id {run_id!r}; must match {RUN_ID_RE.pattern} "
            "(no path separators — traversal guard)"
        )
    return run_id


def run_dir(run_id: str, *, base_dir: str | None = None) -> Path:
    """Resolve the run directory for ``run_id`` (validated; not created).
    ``base_dir=None`` inherits the anchored ``_default_base()``."""
    if base_dir is None:
        base_dir = _default_base()
    return Path(base_dir) / validate_run_id(run_id)


def ensure_run_dir(run_id: str, *, base_dir: str | None = None) -> Path:
    """Create (if needed) and return the run directory for ``run_id``.
    ``base_dir=None`` inherits the anchored ``_default_base()``."""
    d = run_dir(run_id, base_dir=base_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RoundError(f"could not create run dir {d}: {exc}") from exc
    return d


# ---------------------------------------------------------------------------
# question.md
# ---------------------------------------------------------------------------

def question_path(d: Path) -> Path:
    return d / "question.md"


def write_question(d: Path, text: str) -> Path:
    p = question_path(d)
    try:
        p.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise RoundError(f"could not write {p}: {exc}") from exc
    return p


def read_question(d: Path) -> str | None:
    p = question_path(d)
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# round-NN.md
# ---------------------------------------------------------------------------

def round_path(d: Path, n: int) -> Path:
    if n < 1:
        raise RoundError(f"round number must be >= 1, got {n}")
    return d / f"round-{n:02d}.md"


def write_round(d: Path, n: int, text: str) -> Path:
    p = round_path(d, n)
    try:
        p.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise RoundError(f"could not write {p}: {exc}") from exc
    return p


def read_round(d: Path, n: int) -> str | None:
    p = round_path(d, n)
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def list_rounds(d: Path) -> list[int]:
    """Sorted round numbers with a record on disk."""
    nums = []
    for p in d.glob("round-*.md"):
        m = re.match(r"round-(\d+)\.md$", p.name)
        if m:
            nums.append(int(m.group(1)))
    return sorted(nums)


def read_prior_rounds(d: Path, n: int) -> str | None:
    """Concatenate every round record BEFORE round ``n`` (or ``None`` if none).

    This is the value threaded into ``prompts.build_prompt(..., prior_round=...)``
    so round ``n`` sees rounds 1..n-1. Round 1 has no prior → ``None``.
    """
    if n <= 1:
        return None
    parts: list[str] = []
    for i in range(1, n):
        text = read_round(d, i)
        if text:
            parts.append(f"### Round {i}\n{text}")
    return "\n\n".join(parts) if parts else None
