"""Run-id grammar shared with review runs and the prior-round fold."""

from __future__ import annotations

import re

RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9_-]+$")


class RoundError(Exception):
    """Raised with a clear, user-facing message (never a raw traceback)."""


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


def fold_prior_rounds(records: list[str]) -> str | None:
    """Fold ordered round records into the frozen prompt representation."""
    if not records:
        return None
    return "\n\n".join(
        f"### Round {number}\n{record}"
        for number, record in enumerate(records, 1)
    )
