"""Plain-data panel catalog — TODAY's roster encoded VERBATIM.

This module is PURE DATA: module-level constants plus one tiny pure resolver.
It imports NOTHING from ``providers/`` or ``cli`` — that physical separation is
what makes the ``TASK_SEAT_NAMES``⟂registry disjointness self-evident and
testable, and keeps ``cli.py`` focused on argparse/dispatch.

It is a relocation of the existing roster into data, NOT a roster change:
  - ``PANEL_PRESETS`` — the ``full``/``lite``/``solo``/``cursor`` preset → seat
    lists from the command markdown (review.md/build.md/measure-twice.md/
    debate.md). The ``full`` preset's subprocess subset (the names that are NOT
    Task seats) reproduces ``cli._DEFAULT_SUBPROCESS_PANEL`` exactly — that
    equality is the roster-fidelity DRIFT GUARD asserted by the tests. We keep
    the duplication intentional (importing the subset from ``cli`` would couple
    this pure-data module to ``cli``); the test is the single guard.
  - ``TASK_SEAT_NAMES`` — the Claude seats as STRINGS (opus/sonnet, plus opt-in
    ``opus-4.6``). Disjoint from the subprocess registry by design.
  - ``MODEL_OVERRIDES`` — seat-name → model id for the only override today
    (``opus-4.6`` → ``claude-opus-4-6``). Seats not in the map pin their own name.

The ``cursor`` preset stores the literal group TOKEN ``["cursor"]`` verbatim —
NOT an expanded ``cursor-*`` list. This module is pure data and CANNOT expand
it; group expansion happens at resolution time in ``cli.py`` (via
``_expand_seat_groups`` / ``known_seat_names()``), so it stays registry-derived
and grows with ``CURSOR_SEATS``.
"""

# The Claude (Task) seats, as plain strings. Disjoint from the subprocess
# registry. ``opus-4.6`` is opt-in — in NO preset, added explicitly via --seats.
TASK_SEAT_NAMES = {"opus", "sonnet", "opus-4.6"}

# Seat-name → model id. The only override today; seats not present pin their own
# name (e.g. opus → opus, sonnet → sonnet).
MODEL_OVERRIDES = {"opus-4.6": "claude-opus-4-6"}

# The named panel presets. ``full``'s subprocess subset (codex + agy + two Cursor
# model-seats) MUST equal cli._DEFAULT_SUBPROCESS_PANEL as a sequence — the
# roster-fidelity drift guard. ``cursor`` is the literal group token (expanded
# later in cli.py), NOT a baked-in cursor-* list.
PANEL_PRESETS = {
    "full": ["codex", "agy", "cursor-glm", "cursor-composer", "opus", "sonnet"],
    "lite": ["opus", "sonnet"],
    "solo": ["opus"],
    "cursor": ["cursor"],
}


def resolve_panel(name: str) -> list[str]:
    """Return a COPY of the seat list for a named preset.

    Pure dict lookup over ``PANEL_PRESETS`` — no provider logic, no group
    expansion (the ``cursor`` token stays verbatim). Raises ``KeyError`` for an
    unknown preset name.
    """
    return list(PANEL_PRESETS[name])


def resolve_model(name: str) -> str:
    """Resolve a seat name to its model id (``MODEL_OVERRIDES`` or the name)."""
    return MODEL_OVERRIDES.get(name, name)
