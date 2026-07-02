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
    ``fable``). Disjoint from the subprocess registry by design.
  - ``MODEL_OVERRIDES`` — seat-name → model id for seats whose name is not a
    valid Task-tool model alias. EMPTY today: every current seat (opus, sonnet,
    fable) is a first-class alias and pins its own name. The mechanism stays for
    a future version-locked seat — but note the Task tool's model parameter now
    VALIDATES against the alias set, so a full model id (the removed
    ``opus-4.6`` → ``claude-opus-4-6`` pin) is REJECTED at spawn, not silently
    fallen back from. A new override entry only works if the Task tool accepts
    the target id.

The ``cursor`` preset stores the literal group TOKEN ``["cursor"]`` verbatim —
NOT an expanded ``cursor-*`` list. This module is pure data and CANNOT expand
it; group expansion happens at resolution time in ``cli.py`` (via
``_expand_seat_groups`` / ``known_seat_names()``), so it stays registry-derived
and grows with ``CURSOR_SEATS``.
"""

# The Claude (Task) seats, as plain strings. Disjoint from the subprocess
# registry. ``fable`` is opt-in — in NO preset, added explicitly via --seats
# (fable is the premium Mythos-class tier; keeping it opt-in means no default
# panel silently spends it). ``opus-4.6`` was REMOVED: the Task tool's model
# parameter validates against the alias set (sonnet/opus/haiku/fable), so its
# ``claude-opus-4-6`` pin is rejected at spawn — the seat could never be seated.
TASK_SEAT_NAMES = {"opus", "sonnet", "fable"}

# Seat-name → model id. Empty today — every seat pins its own name (opus →
# opus, fable → fable). See the module docstring before adding an entry.
MODEL_OVERRIDES: dict[str, str] = {}

# The named panel presets. ``full``'s subprocess subset (codex + agy + two Cursor
# model-seats) MUST equal cli._DEFAULT_SUBPROCESS_PANEL as a sequence — the
# roster-fidelity drift guard. ``cursor`` is the literal group token (expanded
# later in cli.py), NOT a baked-in cursor-* list.
PANEL_PRESETS = {
    "full": ["codex", "agy", "cursor-auto", "cursor-composer", "opus", "sonnet"],
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
