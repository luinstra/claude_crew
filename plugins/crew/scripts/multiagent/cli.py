#!/usr/bin/env python3
"""CLI entry point for the multi-model review engine (SUBPROCESS seats only).

Invocation mechanism: the shipped command markdown reaches this via the
plugin-root bare dispatcher ``"${CLAUDE_PLUGIN_ROOT}/crew" <sub> …`` (which
imports ``multiagent.cli.main`` after putting ``scripts/`` on ``sys.path``). This
module ALSO still runs directly as a bare script
(``${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py``) or via
``python -m multiagent.cli`` — a sys.path guard at the top inserts the package's
PARENT dir (``.../scripts``) onto ``sys.path`` so the package-relative imports
(``from multiagent...``) resolve in every case (the guard is a harmless no-op when
the parent is already importable, e.g. under the dispatcher).

Subcommands: ``review`` (fan-out over a resolved target), ``council`` (fan-out
of a free-form question — the engine half of /crew:debate's crew-native
council), ``debate`` (scaffold-only: writes the debate dir + question.md + an
empty subprocess.json; NEVER runs seats internally), ``run`` (one
seat ad-hoc), ``render`` (build ONE seat's prompt, no execution), ``seats``
(print the resolved subprocess seat list, one per line, for per-seat fan-out),
``collect`` (collapse the named per-seat ``<seat>.json`` result files into
ONE faithful ``render_panel`` markdown digest — reads EXACTLY the seats named in
``--seats``, never globs), and ``review-prep`` (the deterministic PREP for the
review-bearing commands: resolve target + ``--panel``/``--seats`` → subprocess +
task split, stage the ONE shared subprocess prompt, and PRINT
``{prompt_path, subprocess_seats, task_seats, task_seat_models}`` — runs NOTHING;
the per-seat ``run`` loop + the Task-seat dispatch stay in the command markdown).

The engine EXECUTES only subprocess seats (codex-*/agy/cursor-*), but ``render`` BUILDS the
prompt for ANY seat — including the Claude Task seats (opus/sonnet) the
orchestrator dispatches — so every seat's prompt comes from the one builder
(``prompts.build_prompt``/``council``) and the subprocess and Task paths can
never drift. Multi-round context lives on disk (``rounds.py``: run-id, round-NN.md)
and is threaded into both paths the same way (``--run-id``/``--round`` →
``prior_round``). Task seats are still DISPATCHED by the orchestrator, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- sys.path guard (mechanism C): make the bare-script path importable ------
_PKG_PARENT = str(Path(__file__).resolve().parent.parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)
# -----------------------------------------------------------------------------

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess

from multiagent import config, findings, prompts, render, rounds, seats, targets
from multiagent.providers import (
    ProviderResult,
    available_seats,
    get_provider,
    known_seat_names,
)
# One-time stderr notes for panel/availability resolution (mirrors config.py's
# memoized-warn posture; keyed so each distinct note fires at most once/process).
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(message, file=sys.stderr)


def _panel_seat_list(name: str) -> list[str]:
    """Resolve a panel NAME to its RAW seat-name list — the SINGLE shared roster
    resolution point every panel-consuming path routes through.

    Resolution: ``seats.merged_panels()`` (a configured ``[panels]`` entry over the
    shipped one, per name) → unknown name: ``full`` (one-time warn), which is the
    configured ``full`` when there is one. Returns the raw list (group tokens like
    ``cursor`` left VERBATIM — callers expand via ``_expand_seat_groups``). NEVER
    raises ``KeyError``.
    """
    roster = seats.merged_panels()
    if name in roster:
        return list(roster[name])
    _warn_once(
        f"unknown-panel:{name}",
        f"crew config: panel {name!r} is not a known preset or [panels] entry; "
        "falling back to 'full'",
    )
    return list(roster.get("full", []))


def _seat_available(name: str) -> bool:
    """Whether ``name`` is available (its catalog ``available`` flag). A name with
    no catalog row is treated as available: availability is opt-OUT, and a name the
    catalog does not know is dropped by the registry filter, not hidden here."""
    spec = seats.seat_spec(name)
    return True if spec is None else spec.available


def _task_seat_model(name: str) -> str:
    """The model a Task seat is spawned with: its catalog pin, else its own name
    (every shipped Task seat pins its own name, which IS a Task-tool alias). An
    --task-seats override names a seat the catalog need not know, so it falls back
    rather than raising."""
    spec = seats.seat_spec(name)
    return (spec.model if spec and spec.model else name)


def _drop_unavailable(names: list[str], explicit: set[str]) -> list[str]:
    """Drop seats marked ``available = false`` — applied AFTER panel/roster
    resolution. An EXPLICITLY-named dropped seat (via ``--seats`` or a named
    ``--panel`` preset member) emits a one-time stderr skip-note rather than a
    silent drop.

    NO empty-after-filter fallback here — the CALLER owns the empty decision.
    This is the load-bearing split that lets ``review-prep`` apply a WHOLE-PANEL
    fallback (subprocess + task seats considered TOGETHER) instead of the
    per-list fallback ``_filter_available`` bakes in: a per-list fallback re-adds
    a deliberately-disabled seat-KIND whenever the OTHER kind still kept a seat
    (e.g. disabling both task seats in ``full`` would wrongly restore opus+sonnet
    just because the five subprocess seats remain).
    """
    kept: list[str] = []
    for n in names:
        if _seat_available(n):
            kept.append(n)
        elif n in explicit:
            _warn_once(
                f"seat-skip:{n}",
                f"crew config: seat {n!r} is marked unavailable; skipping it",
            )
    return kept


def _all_unavailable_warn() -> None:
    """One-time warn that a resolved panel was entirely unavailable and the
    unfiltered panel is being used instead of running nothing."""
    _warn_once(
        "all-unavailable",
        "crew config: all seats in the resolved panel are marked unavailable; "
        "using the unfiltered panel instead of running nothing",
    )


def _filter_available(names: list[str], explicit: set[str]) -> list[str]:
    """SINGLE-UNIFIED-LIST availability filter — drop ``available = false`` seats
    (skip-noting an explicitly-named one), and if filtering empties a NON-empty
    list, warn once and return the UNFILTERED list (a misconfiguration must never
    silently produce an empty review/run).

    Used by the paths that resolve ONE unified panel list (``_resolve_seats``,
    ``_resolve_debate_seats``): there the whole panel IS this list, so the
    fallback is already whole-panel. Callers that split one resolution into TWO
    seat-KINDS (``cmd_review_prep``: subprocess vs task) must NOT use this — its
    per-list fallback would re-add a disabled kind. They compose
    ``_drop_unavailable`` + a single whole-panel fallback instead.
    """
    kept = _drop_unavailable(names, explicit)
    if names and not kept:
        _all_unavailable_warn()
        return names
    return kept


def _resolve_timeout(arg: int | None) -> int:
    # Default per-seat wall-clock ceiling. Reference mode (the default) makes the
    # seat do its own fetch + scoped read, which is more work than reviewing an
    # inlined diff — a thorough seat (codex) can legitimately need several
    # minutes on a large review. We'd rather give a real review room than
    # silently drop a default seat to a too-short timeout, so the floor is
    # generous (10 min). The reference prompt keeps the seat SCOPED to the diff
    # so normal reviews finish well under this; genuinely large reviews still
    # get the headroom. Override with --timeout (faster interactive loops or
    # bigger jobs) or the config [tuning].timeout knob.
    if arg is not None:
        return arg
    # Precedence: --timeout arg (above) > per-repo .crew/config.toml > global
    # ~/.crew-config.toml ([tuning].timeout, both via config.default_timeout())
    # > built-in 600s floor.
    cfg = config.default_timeout()
    if cfg is not None:
        return cfg
    return 600


def _run_seat(name: str, prompt: str, timeout: int) -> ProviderResult:
    provider = available_seats([name])[0]
    avail, diag = provider.is_available()
    if not avail:
        # Skipped seats are surfaced as ok=False with a skip diagnostic; the
        # caller distinguishes skip vs fail by the diagnostic. (Renderer has a
        # dedicated skipped block; the CLI marks unavailable seats here.)
        return ProviderResult(
            name=name, model=None, ok=False, output="",
            error=f"skipped: {diag}", elapsed=0.0,
        )
    # No model override: the registry bound each seat's resolved model (config over
    # the catalog pin) when it built the provider.
    return provider.run(prompt, timeout=timeout)


def _expand_seat_groups(names: list[str]) -> list[str]:
    """Expand group tokens to seat names. ``cursor`` -> every cursor seat in the
    catalog, SORTED, so the group grows with the catalog (a declared cursor seat
    joins it) and the fan-out order is stable by name rather than by catalog
    position. Non-group names pass through unchanged.
    """
    groups = seats.group_tokens()
    out: list[str] = []
    for n in names:
        if n in groups:
            out.extend(sorted(groups[n]))
        else:
            out.append(n)
    return out


def _resolve_seats(seats_arg: str | None) -> list[str]:
    """Resolve the comma-separated --seats arg to subprocess seats only.

    Accepts the ``cursor`` group token (expands to all cursor-* seats), so
    ``--seats cursor`` runs the whole Cursor panel and ``--seats cursor,codex``
    adds codex. When no ``--seats`` is given, the CONFIGURED default panel
    (``config.default_panel()`` + ``[panels]``) resolves through the single
    shared ``_panel_seat_list`` point (→ built-in ``full``), so ad-hoc
    ``crew review``/``council``/``seats`` honor the roster too. Availability
    (``_filter_available``) is applied AFTER roster resolution.
    """
    if seats_arg:
        raw = [s.strip() for s in seats_arg.split(",") if s.strip()]
        explicit = set(_expand_seat_groups(raw))
    else:
        raw = _panel_seat_list(config.default_panel() or "full")
        explicit = set()
    names = _expand_seat_groups(raw)
    # Engine drives subprocess seats only — the allowlist is the registry.
    # Filter to known seats, de-duplicating while preserving order.
    known = set(known_seat_names())
    seen: set[str] = set()
    resolved = [s for s in names if s in known and not (s in seen or seen.add(s))]
    return _filter_available(resolved, explicit)


def _resolve_debate_panel_name(panel_arg: str | None) -> str:
    """Effective DEBATE panel NAME when no explicit ``--seats`` is given.

    Debate precedence (differs from review-prep, which has NO debate tier):

        explicit ``--panel`` > ``config.debate_panel()`` > ``config.default_panel()``
        > built-in ``full``

    ``config.debate_panel()`` reads ``[debate].panel`` and ``config.default_panel()``
    reads ``default_panel`` — both validate against the resolved panel names (``seats.merged_panels()``,
    configured ``[panels]`` keys and return ``None`` on a missing/unknown value.
    The returned name is resolved to seats through ``_panel_seat_list`` (which
    handles an unknown ``--panel`` name → ``full``), so no caller hits a raw
    ``KeyError``.
    """
    if panel_arg is not None:
        return panel_arg
    return config.debate_panel() or config.default_panel() or "full"


class _DebateSeatError(Exception):
    """Internal: a ``seats --debate --seats`` validation failure carrying the CLI
    exit code. The message is already printed to stderr; ``cmd_seats`` translates
    this into the nonzero exit (mirroring ``_RenderInputError``).
    """

    def __init__(self, code: int) -> None:
        super().__init__()
        self.code = code


def _resolve_debate_seats(panel_arg: str | None, seats_arg: str | None) -> list[str]:
    """Resolve the FULL debate panel seat list — subprocess AND Claude Task seats.

    Unlike ``_resolve_seats`` (subprocess-only, registry-filtered), this KEEPS the
    Claude Task seats (opus/sonnet/fable) so the debate orchestrator can split
    the resolved panel into its per-seat subprocess fan-out and its Task dispatch.
    Group tokens (``cursor``) are expanded; order preserved, de-duplicated.

    Precedence: explicit ``--seats`` (wins) > explicit ``--panel`` >
    ``config.debate_panel()`` > ``config.default_panel()`` > built-in ``full``.

    Explicit ``--seats`` entries are VALIDATED after group expansion by EXACT
    membership in the fixed UNION allowlist ``known_seat_names() ∪
    seats.task_seats()``. The union is the key — it KEEPS the Task seats
    (opus/sonnet/fable, which are NOT in the registry) while REJECTING garbage. Membership in
    this enumerated allowlist IS the path-safety guarantee (strictly stronger than
    a charset filter — no path-unsafe string can be a member), so no separate
    regex guard is needed here. An unknown name (e.g. ``cursor-../../x``, which is
    in NEITHER set) raises ``_DebateSeatError(2)`` with a stderr message — debate.md
    classifies ``cursor-*`` as a subprocess seat and writes
    ``.crew/debates/<dir>/<seat>.json``, so a traversal name must not reach the
    debate dir (it can't: it's not in the allowlist). This differs from
    ``cmd_collect``, which DOES need a charset guard because it accepts ARBITRARY
    user seat names with no registry/allowlist to check against. The preset path
    (no explicit ``--seats``) is unaffected: preset/group-expanded names are
    always valid.
    """
    if seats_arg is not None:
        names = [s.strip() for s in seats_arg.split(",") if s.strip()]
        names = _expand_seat_groups(names)
        valid = set(known_seat_names()) | set(seats.task_seats())
        for n in names:
            if n not in valid:
                print(
                    f"error: invalid debate seat name {n!r} in --seats; debate "
                    "seats must be a known seat (a registered subprocess seat or a "
                    "Claude Task seat: " + ", ".join(sorted(valid)) + "). debate "
                    "writes per-seat results as .crew/debates/<dir>/<seat>.json and "
                    "must not let a seat name escape the debate dir; membership in "
                    "this fixed allowlist is itself the path-safety guarantee.",
                    file=sys.stderr,
                )
                raise _DebateSeatError(2)
        explicit = set(names)
    else:
        # Roster resolution through the single shared point (honors [panels]).
        names = _expand_seat_groups(_panel_seat_list(_resolve_debate_panel_name(panel_arg)))
        # A NAMED --panel's members are EXPLICIT (mirror review-prep): an
        # unavailable one is skipped WITH a one-time note, not silently dropped.
        # A default/[debate].panel resolution (no --panel given) keeps the silent
        # drop — there is no user-named seat to annotate.
        explicit = set(names) if panel_arg is not None else set()
    # Availability filter (skip-note for an explicitly-named unavailable seat;
    # empty-after-filter falls back to the unfiltered panel). This is ONE unified
    # debate panel list (subprocess + task together), so _filter_available's
    # fallback is already whole-panel here.
    names = _filter_available(names, explicit)
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


_REVIEWS_BASE = ".crew/reviews"


def _resolve_session_id(arg: str | None) -> str:
    """Session id for staging paths: ``--session-id`` arg -> ``CLAUDE_SESSION_ID``
    env -> ``""`` (no session).

    Mirrors crew-state.py's resolution order. Resolved HERE in Python — NOT via
    a shell ``${CLAUDE_SESSION_ID}`` expansion in the calling command, because
    that expansion (a) makes the command un-allowlistable (the permission layer
    flags every ``$…``) and (b) is not reliably exported into the command shell
    anyway, so it silently collapses to an empty segment. The orchestrator passes
    the id it already knows as a literal ``--session-id`` value instead.
    """
    return (arg or os.environ.get("CLAUDE_SESSION_ID") or "").strip()


def _reviews_subdir(session_id: str) -> Path:
    """Resolve ``.crew/reviews/<sanitized-session-id>`` — the SHARED traversal
    containment guard used by BOTH ``_stage_path`` and ``cmd_collect``.

    The session segment is charset-guarded to ``[A-Za-z0-9_-]`` so it can never
    escape ``.crew/reviews/``: a hostile ``--session-id ../../x`` does NOT raise —
    it COLLAPSES (every ``/`` and ``.`` stripped) to a single child dir
    ``.crew/reviews/x``. Containment is by SANITIZATION, not by raising. With no
    session id the dir is flat (``.crew/reviews/``) — sequential same-project use
    still works; only cross-session isolation is lost.
    """
    seg = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    return Path(_REVIEWS_BASE) / seg if seg else Path(_REVIEWS_BASE)


def _seat_role_slug(role: str) -> str:
    """Derive the staged-filename slug for a seat role: charset-guard the role to
    ``[A-Za-z0-9_-]`` (stripping ``.``/``/`` etc. so it can never escape
    ``.crew/reviews/``) and fall back to ``seat`` when empty. This is the ONE
    canonical role→filename-slug source that ``_stage_path``/``--stage-all`` funnel
    through, so staging and spawn always agree on the staged filename (e.g. a
    ``.``-bearing role like a hypothetical ``opus-4.6`` stages AND is read as
    ``prompt-opus-46.txt``).
    """
    return re.sub(r"[^A-Za-z0-9_-]", "", (role or "").strip()) or "seat"


def _stage_path(session_id: str, seat_role: str | None) -> str:
    """Derive the session-scoped staging path for a rendered prompt:
    ``.crew/reviews/<session-id>/prompt-<seat-role>.txt``.

    The seat-role supplies the filename (so the caller passes no path at all);
    the session id supplies a per-session dir so concurrent sessions in one
    project never clobber each other's prompts. BOTH the session segment AND the
    seat-role are charset-guarded to ``[A-Za-z0-9_-]`` so neither can ever escape
    ``.crew/reviews/`` — a hostile ``--seat-role ../../x`` or ``--session-id``
    can only ever name a single child dir/file (path-traversal guard). With no
    session id the path is flat (``.crew/reviews/prompt-<role>.txt``) — sequential
    same-project use still works; only cross-session isolation is lost.

    The session-segment containment is shared with ``cmd_collect`` via
    ``_reviews_subdir`` so both resolve the dir identically.
    """
    role = _seat_role_slug(seat_role or "")
    return str(_reviews_subdir(session_id) / f"prompt-{role}.txt")


def _fan_out(seats: list[str], prompt: str, timeout: int) -> list[ProviderResult]:
    """Run the subprocess seats in parallel over one prompt.

    Shared by ``review`` and ``council``: same parallel fan-out, same
    ``ProviderResult`` shape, same graceful degradation (one seat failing or
    hanging never sinks the panel). Pool sized to absorb one hung thread
    (>= number of seats) so a hung agy thread cannot block codex's result.
    Returns results in stable seat order (as requested).
    """
    results_by_name: dict[str, ProviderResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(seats), 2)) as pool:
        futures = {
            pool.submit(_run_seat, name, prompt, timeout): name for name in seats
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results_by_name[name] = fut.result()
            except Exception as exc:  # never let one seat sink the panel
                results_by_name[name] = ProviderResult(
                    name=name, model=None, ok=False, output="",
                    error=f"seat raised: {exc}", elapsed=0.0,
                )
    return [results_by_name[name] for name in seats]


def _all_failed_exit(results: list[ProviderResult]) -> int:
    """Never-choke guarantee: a partial panel (>=1 ok seat) is a success.

    ONLY when EVERY seat failed/was skipped do we surface a clear all-failed
    signal (nonzero exit + an stderr summary) — still a clean list of all-failed
    results, never a traceback.
    """
    if results and not any(r.ok for r in results):
        diags = "; ".join(
            f"{r.name}: {(r.error or 'unknown error').strip()}" for r in results
        )
        print(
            f"error: all {len(results)} seats failed: {diags}",
            file=sys.stderr,
        )
        return 1
    return 0


def _emit(text: str, out_path: str | None) -> None:
    """Write engine output to a file (``--out``) or stdout.

    A file destination is what keeps the whole invocation a single,
    allowlistable ``"${CLAUDE_PLUGIN_ROOT}/crew" …`` command: callers no longer
    wrap it in
    ``> "$dir/file"`` (an output redirect the permission matcher can't whitelist)
    or a ``$(…)``-derived path. The engine owns the write; the shell stays out of
    it. stderr (the all-seats-failed diagnostic) is intentionally NOT redirected
    here — it only fires on total failure and belongs on the terminal.
    """
    if out_path:
        # Create parent dirs so a standalone `render`/`review`/`run -o .crew/…`
        # doesn't crash in a fresh repo where .crew/ hasn't been created yet
        # (only build/measure-twice init .crew via crew-state; review/debate don't).
        p = Path(out_path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            text if text.endswith("\n") else text + "\n", encoding="utf-8"
        )
    else:
        print(text)


def cmd_review(args: argparse.Namespace) -> int:
    try:
        target = targets.resolve(args.target, base=args.base)
    except targets.TargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prompt = prompts.build_prompt(target, inline=args.inline_diff)

    seats = _resolve_seats(args.seats)
    if not seats:
        print("error: no subprocess seats requested", file=sys.stderr)
        return 2

    timeout = _resolve_timeout(args.timeout)
    results = _fan_out(seats, prompt, timeout)

    if args.json:
        body = render.render_json(results)
    else:
        lines = [f"TARGET: {target.descriptor}"]
        lines += [f"NOTE: {note}" for note in target.notes]
        lines += ["", render.render_panel(results)]
        body = "\n".join(lines)
    _emit(body, args.out)

    return _all_failed_exit(results)


def cmd_council(args: argparse.Namespace) -> int:
    """Fan a free-form QUESTION across the subprocess council seats.

    The engine half of the crew-native council (/crew:debate): single round,
    free-form prompt, REUSING ``review``'s fan-out + ``ProviderResult`` + render
    + graceful-degradation. No target resolution. The orchestrator adds the
    opus/sonnet Task seats and synthesizes.
    """
    # Exactly one prompt source: -f <file> XOR positional <question>.
    if args.file and args.question is not None:
        print(
            "error: provide either -f <question-file> OR a positional "
            "<question>, not both",
            file=sys.stderr,
        )
        return 2
    if not args.file and args.question is None:
        print(
            "error: no question given; provide -f <question-file> or a "
            "positional <question>",
            file=sys.stderr,
        )
        return 2

    if args.file:
        try:
            question = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read question file {args.file!r}: {exc}", file=sys.stderr)
            return 2
    else:
        question = args.question

    prompt = prompts.council(question)

    seats = _resolve_seats(args.seats)
    if not seats:
        print("error: no subprocess seats requested", file=sys.stderr)
        return 2

    timeout = _resolve_timeout(args.timeout)
    results = _fan_out(seats, prompt, timeout)

    if args.json:
        body = render.render_json(results)
    else:
        body = "COUNCIL (single round)\n\n" + render.render_panel(results)
    _emit(body, args.out)

    return _all_failed_exit(results)


def _slugify(text: str, max_words: int = 6) -> str:
    """Derive a short kebab-case slug from the question's first words."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    slug = "-".join(words[:max_words])
    return slug[:60] or "debate"


def cmd_debate(args: argparse.Namespace) -> int:
    """Scaffold a debate dir in ONE allowlistable call — SCAFFOLD-ONLY, never runs seats.

    Folds the shell steps /crew:debate used to do — `mkdir` the dir, a heredoc
    to write the question — into a single allowlistable ``crew debate …`` call
    (so it never prompts). Creates ``<base-dir>/<timestamp>-<slug>/``, writes
    ``question.md`` + an ALWAYS-EMPTY ``subprocess.json`` into it, and prints a
    JSON summary (``dir`` + ``seats: []``) so the orchestrator knows where to
    drop the per-seat outputs + synthesis.

    It NEVER fans out subprocess seats internally — that was a split-brain
    foot-gun (review-prep forbids internal `_fan_out` for killability, but
    debate used to call it). The orchestrator fans the subprocess seats out
    per-seat via ``crew run <seat>`` (visible, killable shells) and spawns the
    opus/sonnet Task seats (the engine cannot spawn in-session Claude agents).
    ``--seats none``/`""` is the only meaning; a non-empty ``--seats`` does NOT
    execute those seats — it prints a one-line stderr advisory and still
    scaffolds (exit 0).
    """
    # Exactly one question source: -f <file> XOR positional <question>.
    if args.file and args.question is not None:
        print(
            "error: provide either -f <question-file> OR a positional "
            "<question>, not both",
            file=sys.stderr,
        )
        return 2
    if not args.file and args.question is None:
        print(
            "error: no question given; provide -f <question-file> or a "
            "positional <question>",
            file=sys.stderr,
        )
        return 2
    if args.file:
        try:
            question = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read question file {args.file!r}: {exc}", file=sys.stderr)
            return 2
    else:
        question = args.question

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # Sanitize BOTH slug sources through _slugify so an explicit --slug like
    # "../../etc/foo" can't escape --base-dir (path traversal) — the result is
    # always restricted to [a-z0-9-].
    slug = _slugify(args.slug) if args.slug else _slugify(question)

    # Unique dir: NEVER silently overwrite a same-second/same-slug debate
    # (exist_ok=True would merge two debates' question.txt/subprocess.json).
    base = Path(args.base_dir)
    debate_dir = base / f"{ts}-{slug}"
    suffix = 2
    while True:
        try:
            debate_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            debate_dir = base / f"{ts}-{slug}-{suffix}"
            suffix += 1
        except OSError as exc:
            print(f"error: cannot create debate dir {debate_dir}: {exc}", file=sys.stderr)
            return 2

    # Guard the writes: an IO failure (full disk, bad perms) must not dump a
    # traceback or leave an orphan dir — clean up and exit nonzero cleanly.
    try:
        (debate_dir / "question.md").write_text(question, encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(debate_dir, ignore_errors=True)
        print(f"error: cannot write question.md: {exc}", file=sys.stderr)
        return 2

    # --consume: the question is now safely in the dir, so the CLI removes the
    # transient -f staging file the orchestrator wrote (best-effort; the CLI was
    # told to own it). Nothing lingers in .crew/debates/.
    if args.consume and args.file:
        try:
            Path(args.file).unlink()
        except OSError:
            pass

    # Scaffold-only: debate NEVER runs subprocess seats internally — that path
    # was a split-brain foot-gun (one prep path killable, the other an opaque
    # `_fan_out` thread pool). The orchestrator fans seats out per-seat via
    # `crew run <seat>` (visible, killable shells), so the engine just writes an
    # EMPTY subprocess.json here. ONLY an EXPLICIT `--seats none` (or "") is the
    # silent no-op; ANY other case — a non-empty `--seats` (e.g. `--seats codex`)
    # OR an OMITTED `--seats` (None, which pre-Step-5 ran the default panel) —
    # no longer executes those seats: emit a one-line advisory and still scaffold
    # (exit 0). Short-circuit-safe: when args.seats is None the first operand is
    # True and `.strip()` is never evaluated.
    if args.seats is None or args.seats.strip().lower() not in ("", "none"):
        print(
            "debate no longer runs subprocess seats internally — fan them out "
            "per-seat via 'crew run <seat>'; scaffolding the dir only",
            file=sys.stderr,
        )
    results: list[ProviderResult] = []
    try:
        (debate_dir / "subprocess.json").write_text(
            render.render_json(results), encoding="utf-8"
        )
    except OSError as exc:
        print(f"error: cannot write subprocess.json to {debate_dir}: {exc}", file=sys.stderr)
        return 2

    # The orchestrator reads this to find the dir, then fans the subprocess
    # seats out per-seat (`crew run <seat>`) and spawns the opus/sonnet Task
    # seats, writing the synthesis here. `seats` is always [] (scaffold-only).
    print(json.dumps({
        "dir": str(debate_dir),
        "question_file": str(debate_dir / "question.md"),
        "subprocess_results": str(debate_dir / "subprocess.json"),
        "seats": [
            {"name": r.name, "ok": r.ok, "elapsed": round(r.elapsed, 1)}
            for r in results
        ],
    }, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run ONE subprocess seat ad-hoc via the registered provider.

    This is the single, allowlistable entry point for ad-hoc seat calls: instead
    of hand-constructing a per-seat CLI invocation (whose path varies every time
    and so can't be permission-allowlisted), callers invoke
    ``cli.py run <seat> -f <prompt-file>`` (or a direct prompt string). The seat
    is dispatched through the SAME ``get_provider``/``Provider`` machinery as
    ``review`` so invocation shape, ANSI-strip, auth/error-banner detection,
    timeout floor, ARG_MAX guard, and config resolution all apply unchanged.
    Valid seats are whatever the registry holds (codex-*, agy, cursor-*).

    With a TRUTHY, non-blank ``--session-id <id>``, ``-f`` and ``-o`` are DERIVED
    from ``.crew/reviews/<id>/`` when omitted: ``-f`` defaults to the shared
    ``prompt-seat.txt`` and ``-o`` to ``<seat>.json``. Explicit ``-f``/``-o``
    (and a positional prompt, for input) override their derivation independently,
    so debate's explicit paths and ad-hoc stdout use are untouched.
    """
    seat = args.seat
    if seat in seats.task_seats():
        print(
            f"error: '{seat}' is a Task seat — owned by the orchestrator, not "
            f"runnable by the engine (engine runs subprocess seats only: "
            f"{', '.join(known_seat_names())})",
            file=sys.stderr,
        )
        return 2
    if seat not in known_seat_names():
        known = ", ".join(known_seat_names())
        print(
            f"error: unknown or non-subprocess seat {seat!r}; "
            f"valid subprocess seats: {known}",
            file=sys.stderr,
        )
        return 2

    # Derive -f/-o from .crew/reviews/<session-id>/ when a TRUTHY, non-blank
    # --session-id is given. The truthy + non-blank gate is load-bearing: an
    # omitted (None), explicit-empty ("") or whitespace-only ("   ") arg must NOT
    # derive — neither re-entering _resolve_session_id's env fallback nor
    # deriving the flat .crew/reviews/ dir (Decision A). Runs AFTER the seat
    # guards (so the seat is a registry-clean [A-Za-z0-9_-] name and the derived
    # <seat>.json can never escape the dir — no path-char guard needed, Decision E)
    # and BEFORE the prompt-source checks (so a positional prompt / explicit -f
    # overrides input derivation without tripping the "both sources" error).
    if args.session_id and args.session_id.strip():
        sid = _resolve_session_id(args.session_id)
        if "<" in sid or ">" in sid:
            print(
                f"error: session id looks like an unsubstituted placeholder "
                f"({sid!r}); pass your actual session id (the "
                "[Session ID: …] value), not the literal template",
                file=sys.stderr,
            )
            return 2
        if args.file is None and args.prompt is None:
            args.file = str(_reviews_subdir(sid) / "prompt-seat.txt")
        if args.out is None:
            args.out = str(_reviews_subdir(sid) / f"{seat}.json")

    # Exactly one prompt source: -f <file> XOR positional <prompt-string>.
    if args.file and args.prompt is not None:
        print(
            "error: provide either -f <prompt-file> OR a positional "
            "<prompt-string>, not both",
            file=sys.stderr,
        )
        return 2
    if not args.file and args.prompt is None:
        print(
            "error: no prompt given; provide -f <prompt-file> or a positional "
            "<prompt-string>",
            file=sys.stderr,
        )
        return 2

    if args.file:
        try:
            prompt = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read prompt file {args.file!r}: {exc}", file=sys.stderr)
            return 2
    else:
        prompt = args.prompt

    # NOTE (config `available` interaction): `run <seat>` is an EXPLICIT single-seat
    # call, so it does NOT apply the catalog `available` filter — the seat is always run.
    # The panel-level availability filter lives upstream in _filter_available
    # (review-prep / _resolve_seats / debate): a non-explicit unavailable seat is
    # dropped THERE and never reaches this fan-out, while an EXPLICITLY-named
    # unavailable seat survives via the empty-after-filter fallback (skip-note then
    # run) and must execute here — re-filtering it would wrongly drop it. PATH-level
    # is_available() (below) is a different check (the CLI isn't installed).
    provider = get_provider(seat)
    avail, diag = provider.is_available()
    if not avail:
        result = ProviderResult(
            name=seat, model=None, ok=False, output="",
            error=f"skipped: {diag}", elapsed=0.0,
        )
    else:
        # Same model resolution as the review path: --model overrides the seat's
        # resolved model, which the registry already bound from its catalog row.
        timeout = _resolve_timeout(args.timeout)
        result = provider.run(
            prompt, sandbox=args.sandbox, model=args.model, timeout=timeout,
        )

    if args.json:
        _emit(json.dumps(result.to_dict(), ensure_ascii=False), args.out)
        return 0

    if not result.ok:
        print(result.error or "unknown error", file=sys.stderr)
        return 1
    _emit(result.output, args.out)
    return 0


# =============================================================================
# /crew:dispatch — send ONE subprocess seat at the working tree in write mode
# =============================================================================
#
# The execution complement to read-only review/debate: ONE chosen seat (default
# codex) runs in ``sandbox="workspace-write"`` and may edit files, but is
# instructed to leave everything UNCOMMITTED and UNSTAGED and on the same branch
# (prompts.dispatch). A Python guard captures HEAD + staged-index + branch state
# BEFORE and AFTER the seat runs — pinned to the SAME tree the seat edits — so a
# commit / stage / branch-flip against instruction is LOUD and recoverable
# (detect-not-prevent). dispatch emits its OWN 15-field JSON envelope (never a
# polluted six-field ProviderResult).

# A sentinel distinct from any 40-hex sha and from None: a valid git work tree
# with NO commit yet. Serializes as a self-describing JSON string so an
# unborn->sha FIRST commit fires head_moved=true (not masked to false).
_UNBORN = "<unborn>"

# A sentinel distinct from any branch name and from None: a valid git work tree
# whose HEAD is DETACHED (no symbolic ref). Serializes as a self-describing JSON
# string so a `git checkout <sha>` (main -> detached) fires branch_changed=true
# (not masked to false the way a bare None would). The SPACE makes it an
# IMPOSSIBLE refname (git refnames cannot contain a space —
# `git check-ref-format --branch '<detached HEAD>'` exits nonzero), so NO real
# branch can ever equal it and mask a branch change (BLOCKING-1: the old
# `<detached>` spelling IS a valid branch name and would collide).
_DETACHED = "<detached HEAD>"


def _git_head(repo_dir: str) -> str | None:
    """TRI-STATE HEAD probe in ``repo_dir`` (BLOCKING-2):

    * a real **sha** — a born repo (incl. detached HEAD: ``rev-parse HEAD``
      resolves the checked-out commit);
    * the sentinel ``"<unborn>"`` — a valid work tree with zero commits
      (``rev-parse HEAD`` fails BUT ``rev-parse --is-inside-work-tree`` succeeds);
    * ``None`` — not-a-repo / git error (neither succeeds).
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True,
        )
    except OSError:
        return None
    if proc.returncode == 0:
        sha = (proc.stdout or "").strip()
        return sha or None
    # rev-parse HEAD failed — distinguish an unborn repo from a non-repo.
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_dir, capture_output=True, text=True,
        )
    except OSError:
        return None
    if inside.returncode == 0 and (inside.stdout or "").strip() == "true":
        return _UNBORN
    return None


def _git_staged(repo_dir: str) -> str | None:
    """Index-tree-hash snapshot in ``repo_dir`` (BLOCKING-2) — the index's
    CONTENT, not a clean/dirty boolean:

    ``git write-tree`` serializes the current index to a tree object and prints
    the tree SHA WITHOUT touching the index, HEAD, refs, or the working tree, so
    it is safe for a guard. Returns the tree SHA string on success (exit 0), or
    ``None`` on failure / not-a-repo / git error.

    Comparing the before/after tree SHA catches BOTH ``clean -> staged`` AND an
    already-staged index that the seat stages MORE into (``staged -> more
    staged``, where a clean/dirty boolean would read ``True -> True`` and miss
    it). Dispatch explicitly runs on dirty trees, so the already-staged case is
    real.
    """
    try:
        proc = subprocess.run(
            ["git", "write-tree"],
            cwd=repo_dir, capture_output=True, text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    tree = (proc.stdout or "").strip()
    return tree or None


def _git_branch(repo_dir: str) -> str | None:
    """TRI-STATE branch probe in ``repo_dir`` (mirrors ``_git_head``'s structure):

    * the **branch name** — ``git symbolic-ref --quiet --short HEAD`` succeeds
      (HEAD points symbolically at refs/heads/<branch>; also true in an unborn
      repo before the first commit);
    * the sentinel ``"<detached HEAD>"`` — symbolic-ref FAILS but the repo is a
      valid work tree with a DETACHED HEAD (``rev-parse --is-inside-work-tree``
      succeeds). An IMPOSSIBLE refname (the space) so no real branch can equal
      it; distinct from any branch name and from ``None`` so a
      ``git checkout <sha>`` (main -> detached) fires ``branch_changed=true``
      instead of being silently masked to false;
    * ``None`` — not-a-repo / git error (neither succeeds).

    ``symbolic-ref`` (NOT ``rev-parse --abbrev-ref HEAD``) is load-bearing: on a
    detached HEAD it exits nonzero (so we fall through to the work-tree probe),
    whereas ``rev-parse --abbrev-ref`` returns the literal string ``"HEAD"``
    which would defeat null-safety AND blur detached vs. a branch named HEAD.
    """
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True,
        )
    except OSError:
        return None
    if proc.returncode == 0:
        name = (proc.stdout or "").strip()
        if name:
            return name
        # symbolic-ref succeeded but printed nothing — fall through to probe.
    # symbolic-ref failed — distinguish a detached HEAD from a non-repo.
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_dir, capture_output=True, text=True,
        )
    except OSError:
        return None
    if inside.returncode == 0 and (inside.stdout or "").strip() == "true":
        return _DETACHED
    return None


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Send ONE subprocess seat at the working tree in WRITE mode (/crew:dispatch).

    Steps run in the SAME canonical order as ``cmd_run`` (derive the session
    output path BEFORE the availability check), with the HEAD/staged/branch guard
    wrapped around the seat run and a 15-field JSON envelope emitted.
    """
    # (a) Resolve the seat: --seat > [dispatch].seat > built-in codex. Compute the
    # pinned guard tree ONCE — every _git_* helper runs with cwd=repo_dir, the
    # SAME tree the codex/agy workspace-write cwd pin (and cursor's own pin) edit.
    seat = args.seat or config.dispatch_seat() or "codex"
    repo_dir = os.environ.get("CLAUDE_WORKING_DIRECTORY") or os.getcwd()

    # (b) Seat-name guards — pre-run, exit 2, NO envelope (parity with cmd_run).
    if seat in seats.task_seats():
        print(
            f"error: '{seat}' is a Task seat — owned by the orchestrator, not "
            f"runnable by the engine (engine runs subprocess seats only: "
            f"{', '.join(known_seat_names())})",
            file=sys.stderr,
        )
        return 2
    if seat not in known_seat_names():
        known = ", ".join(known_seat_names())
        print(
            f"error: unknown or non-subprocess seat {seat!r}; "
            f"valid subprocess seats: {known}",
            file=sys.stderr,
        )
        return 2
    # An EXPLICIT --seat / configured [dispatch].seat BYPASSES the catalog `available` filter
    # (parity with cmd_run's explicit single-seat path) — a config-disabled seat
    # still runs when named directly. is_available() (step e) is the distinct
    # "is the CLI installed" check.
    provider = get_provider(seat)
    if not provider.supports_workspace_write:
        # Fail-CLOSED capability guard (D6): a seat that has not opted into
        # workspace-write must not be silently run read-only.
        print(
            f"error: seat {seat!r} does not support workspace-write "
            f"(cannot honor /crew:dispatch's write mode); refusing to run it "
            f"read-only",
            file=sys.stderr,
        )
        return 2

    # (c) Resolve the task source + derive the output path — BEFORE is_available()
    # (matching cmd_run). Deriving the path here is what lets the unavailable-seat
    # skip envelope in (e) be written to a real path.
    if args.file and args.task is not None:
        print(
            "error: provide either -f <task-file> OR a positional <task>, "
            "not both",
            file=sys.stderr,
        )
        return 2
    if not args.file and args.task is None:
        print(
            "error: no task given; provide -f <task-file> or a positional <task>",
            file=sys.stderr,
        )
        return 2
    if args.file:
        try:
            task = Path(args.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # MINOR-2: a non-UTF-8 task file raises UnicodeDecodeError (a
            # ValueError, NOT an OSError) — catch it alongside OSError so a bad
            # task file prints the graceful error + exits cleanly instead of
            # crashing with a traceback.
            print(f"error: cannot read task file {args.file!r}: {exc}", file=sys.stderr)
            return 2
    else:
        task = args.task

    # Derive -o to dispatch-<seat>.json from --session-id (run's derive block +
    # placeholder guard). An explicit -o overrides; no session + no -o -> stdout.
    if args.session_id and args.session_id.strip():
        sid = _resolve_session_id(args.session_id)
        if "<" in sid or ">" in sid:
            print(
                f"error: session id looks like an unsubstituted placeholder "
                f"({sid!r}); pass your actual session id (the "
                "[Session ID: …] value), not the literal template",
                file=sys.stderr,
            )
            return 2
        if args.out is None:
            args.out = str(_reviews_subdir(sid) / f"dispatch-{seat}.json")

    prompt = prompts.dispatch(task)

    # (d) Capture git BEFORE-state (all in repo_dir).
    head_before = _git_head(repo_dir)
    staged_before = _git_staged(repo_dir)
    branch_before = _git_branch(repo_dir)

    # (e) Availability — unavailable-seat skip (mirrors cmd_run EXACTLY): build a
    # SKIPPED ok=false envelope, the seat NEVER runs (after==before, all guards
    # false), write + print the path, exit 0 under --json. DISTINCT from the
    # exit-2 pre-run rejections in (b).
    avail, diag = provider.is_available()
    if not avail:
        result = ProviderResult(
            name=seat, model=None, ok=False, output="",
            error=f"skipped: {diag}", elapsed=0.0,
        )
        head_after, staged_after, branch_after = head_before, staged_before, branch_before
    else:
        # (f) Run the seat in workspace-write. Model precedence mirrors cmd_run.
        timeout = _resolve_timeout(args.timeout)
        result = provider.run(
            prompt, sandbox="workspace-write", model=args.model, timeout=timeout,
        )
        # (g) Capture git AFTER-state (all in repo_dir).
        head_after = _git_head(repo_dir)
        staged_after = _git_staged(repo_dir)
        branch_after = _git_branch(repo_dir)

    # (h) Compute the three guards NULL-SAFE (typed booleans, never null) on the
    # internal tri-state values, then build + emit the 15-field envelope.
    head_moved = (
        head_before is not None and head_after is not None
        and head_before != head_after
    )
    staged_changed = (
        staged_before is not None and staged_after is not None
        and staged_before != staged_after
    )
    branch_changed = (
        branch_before is not None and branch_after is not None
        and branch_before != branch_after
    )

    envelope = {
        "seat": seat,
        "model": result.model,
        "ok": result.ok,
        "output": result.output,
        "error": result.error,
        "elapsed": result.elapsed,
        # head_before/after: a commit sha, the "<unborn>" sentinel, or null.
        "head_before": head_before,
        "head_after": head_after,
        "head_moved": head_moved,
        # staged_before/after: the index tree hash (git write-tree) or null —
        # NOT a bool; its CHANGE (incl. already-staged -> more-staged) drives
        # staged_changed.
        "staged_before": staged_before,
        "staged_after": staged_after,
        "staged_changed": staged_changed,
        # branch_before/after: a branch name, the "<detached HEAD>" sentinel, or null.
        "branch_before": branch_before,
        "branch_after": branch_after,
        "branch_changed": branch_changed,
    }

    if args.json:
        _emit(json.dumps(envelope, ensure_ascii=False), args.out)
        # collect-style: announce the resolved envelope path as the LAST clean
        # stdout line so dispatch.md reads it back without constructing the name.
        if args.out:
            print(args.out)
        return 0

    # Human (non-JSON) output: header + seat output + warnings LAST in fixed order.
    if not result.ok:
        err = result.error or "unknown error"
        # When -o is set, still WRITE the envelope file on failure so the caller
        # has a file to read regardless of ok. Human mode WRITES the envelope but
        # does NOT print the path (unlike the JSON path, which writes AND prints).
        # The diagnostic still goes to stderr and the exit stays 1.
        if args.out:
            _emit(json.dumps(envelope, ensure_ascii=False), args.out)
        print(err, file=sys.stderr)
        return 1
    header = f"dispatch: {seat}" + (f" ({result.model})" if result.model else "")
    lines = [header, "", result.output]
    if head_moved:
        if head_before == _UNBORN:
            lines.append(
                f"WARNING: the dispatched seat moved HEAD {head_before} -> "
                f"{head_after} (it made the repo's FIRST commit against "
                f"instruction; undo with: git update-ref -d HEAD)"
            )
        else:
            lines.append(
                f"WARNING: the dispatched seat moved HEAD {head_before} -> "
                f"{head_after} (it committed against instruction; undo with: "
                f"git reset --soft HEAD@{{1}})"
            )
    # BLOCKING-1: ALWAYS surface a staged change when staged_changed fires — a
    # safety guard must never silently drop a real staged violation. The earlier
    # `not head_moved` suppression was too coarse: a seat that COMMITS and then
    # STAGES additional changes has REAL staged content with head_moved=true, and
    # suppressing the staged line would hide it (the HEAD remedy `git reset --soft
    # HEAD@{1}` leaves those extra staged changes behind). Over-warning is safe for
    # a guard; under-warning is not. To stay non-misleading in BOTH cases — a pure
    # commit (where the index is clean vs the NEW HEAD and `git reset` would be a
    # no-op) AND a real independent stage — the line is DESCRIPTIVE: it reports
    # that the index content changed and points at `git status` to inspect, rather
    # than asserting `git reset` is THE fix (which would be a false no-op promise
    # on a pure commit). The JSON envelope's staged_changed stays computed
    # faithfully.
    if staged_changed:
        lines.append(
            "WARNING: staged/index content changed since before the run — "
            "inspect with `git status`; unstage any unintended changes with "
            "`git reset`"
        )
    if branch_changed:
        # BLOCKING-2: pick the recovery target for branch_before. When dispatch
        # STARTED on a detached HEAD, branch_before is the (impossible-refname)
        # sentinel — `git checkout <sentinel>` is nonsense; the way back to the
        # original detached state is `git checkout --detach <head_before>` (the
        # ORIGINAL commit sha). A real branch name uses a plain `git checkout`.
        # MINOR-1: null-guard head_before — if the original HEAD probe returned
        # None (a detached HEAD whose _git_head errored while _git_branch
        # succeeded — extremely unlikely but unguarded), fall back to a safe
        # generic hint rather than emitting the literal `--detach None`.
        if branch_before == _DETACHED:
            recover = (
                f"git checkout --detach {head_before}"
                if head_before is not None
                else "re-detach to your original commit"
            )
        else:
            recover = f"git checkout {branch_before}"
        lines.append(
            f"WARNING: the dispatched seat changed branch {branch_before} -> "
            f"{branch_after} against instruction (return with: {recover})"
        )
    _emit("\n".join(lines), args.out)
    if args.out:
        print(args.out)
    return 0


def _render_prior(args: argparse.Namespace) -> str | None:
    """Resolve the prior-round DATA for a render, from --prior-round or --run-id/--round.

    Priority: an explicit ``--prior-round <file>`` wins; otherwise, given both
    ``--run-id`` and ``--round`` (>1), read rounds 1..round-1 from the run dir.
    Raises ``rounds.RoundError`` for an invalid run-id (traversal guard) or a
    lone --run-id/--round (the two must travel together).
    """
    if args.prior_round:
        try:
            return Path(args.prior_round).read_text(encoding="utf-8")
        except OSError as exc:
            raise rounds.RoundError(f"cannot read --prior-round file {args.prior_round!r}: {exc}")
    has_id, has_round = bool(args.run_id), args.round is not None
    if has_id != has_round:
        raise rounds.RoundError("--run-id and --round must be given together")
    if has_id and has_round:
        d = rounds.run_dir(args.run_id, base_dir=args.base_dir)
        return rounds.read_prior_rounds(d, args.round)
    return None


class _RenderInputError(Exception):
    """Internal: a render input-resolution failure carrying the CLI exit code.

    Lets ``_resolve_render_inputs`` raise a single error type (with the message
    already printed to stderr) that both the single-seat and ``--stage-all`` code
    paths translate into the SAME nonzero exit, so neither path can drift on
    input validation.
    """

    def __init__(self, code: int) -> None:
        super().__init__()
        self.code = code


def _resolve_render_inputs(
    args: argparse.Namespace,
) -> tuple[str | None, "targets.Target | None", str | None]:
    """Resolve the prompt-content inputs shared by EVERY rendered seat.

    Returns ``(question, target, prior)`` — only ``seat_role`` and the output
    path vary per seat, so this single helper guarantees the single-seat
    ``--stage`` path and the per-role ``--stage-all`` loop feed BYTE-IDENTICAL
    inputs to the builder (a ``--stage-all opus`` file == ``--stage --seat-role
    opus``; a ``--stage-all seat`` file == ``--stage`` with no role). Exactly one
    of ``question`` / ``target`` is non-None. Prints the error and raises
    ``_RenderInputError`` (with the exit code) on any invalid combination.
    """
    try:
        prior = _render_prior(args)
    except rounds.RoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise _RenderInputError(2)

    # Loud (not silent) when a later round has no prior context to thread.
    if args.round is not None and args.round > 1 and not prior:
        print(
            f"warning: --round {args.round} but no prior-round content found "
            "(prompt will omit prior-round context)",
            file=sys.stderr,
        )

    # Free-form question (discuss) XOR a target.
    if args.question is not None and args.file:
        print("error: provide either -q/--question OR -f/--file, not both", file=sys.stderr)
        raise _RenderInputError(2)
    # A free-form question and an explicit positional target are mutually
    # exclusive — passing both is ambiguous (the target would be silently
    # ignored). The default target is "auto"; only a NON-default target conflicts.
    if (args.question is not None or args.file) and args.target != "auto":
        print(
            "error: provide either a positional <target> OR a free-form question "
            "(-q/--question, -f/--file), not both",
            file=sys.stderr,
        )
        raise _RenderInputError(2)
    question: str | None = None
    if args.file:
        try:
            question = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read question file {args.file!r}: {exc}", file=sys.stderr)
            raise _RenderInputError(2)
    elif args.question is not None:
        question = args.question

    if question is not None:
        if args.mode != "discuss":
            print(
                "error: a free-form question (-q/-f) is only valid with --mode discuss",
                file=sys.stderr,
            )
            raise _RenderInputError(2)
        return question, None, prior

    try:
        target = targets.resolve(args.target, base=args.base)
    except targets.TargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise _RenderInputError(2)
    return None, target, prior


def _build_render_text(
    question: str | None,
    target: "targets.Target | None",
    prior: str | None,
    seat_role: str | None,
    inline_diff: bool,
    mode: str,
) -> str:
    """Build ONE seat's prompt text from pre-resolved inputs.

    The SINGLE place the builder is invoked for render — both ``--stage`` and the
    ``--stage-all`` loop call here, varying only ``seat_role``, so the per-role
    content can never diverge from the single-seat content.
    """
    if question is not None:
        return prompts.council(question, seat_role=seat_role, prior_round=prior)
    return prompts.build_prompt(
        target,
        seat_role=seat_role,
        mode=mode,
        prior_round=prior,
        inline=inline_diff,
    )


def cmd_render(args: argparse.Namespace) -> int:
    """Build ONE seat's prompt and emit it — no execution.

    This is the single prompt source the orchestrator uses to obtain a Claude
    Task seat's prompt (so it is byte-identical to what a subprocess seat would
    receive for the same target/mode/round — every prompt, every seat, every
    round flows through ``prompts.build_prompt``/``council`` and nowhere else).
    It builds for either a git/plan TARGET or, in discuss mode, a free-form
    QUESTION (``-q``/``-f``). It never spawns a seat.

    ``--stage-all <comma-roles>`` collapses N single-seat ``--stage`` renders into
    ONE call: it stages a LABELED prompt per role into
    ``.crew/reviews/<session-id>/prompt-<role>.txt`` and prints a JSON
    ``{role: path}`` map. (See ``cmd_render_stage_all``.)
    """
    if args.stage_all is not None:
        return cmd_render_stage_all(args)

    try:
        question, target, prior = _resolve_render_inputs(args)
    except _RenderInputError as exc:
        return exc.code

    text = _build_render_text(
        question, target, prior, args.seat_role, args.inline_diff, args.mode
    )

    out_path = args.out
    if args.stage:
        if args.out:
            print("error: --stage and -o/--out are mutually exclusive", file=sys.stderr)
            return 2
        # Fail LOUD on an unsubstituted template. The orchestrator must replace
        # the `<session-id>` placeholder with its real id; if it forgets, the
        # charset guard would silently strip `<`/`>` to a CONSTANT `session-id`
        # segment — reintroducing the cross-session clobber --stage exists to
        # prevent. Reject the placeholder outright so the mistake is obvious.
        # Resolve first (arg → CLAUDE_SESSION_ID env), THEN check — so an env
        # value that is itself an unsubstituted placeholder is caught too, not
        # only the --session-id arg.
        session_id = _resolve_session_id(args.session_id)
        if "<" in session_id or ">" in session_id:
            print(
                f"error: session id looks like an unsubstituted placeholder "
                f"({session_id!r}); pass your actual session id (the "
                "[Session ID: …] value), not the literal template",
                file=sys.stderr,
            )
            return 2
        out_path = _stage_path(session_id, args.seat_role)

    _emit(text, out_path)
    # In --stage mode the prompt went to a file, so stdout is free to carry the
    # resolved path: the orchestrator reads THAT file (with the Read tool — no
    # shell expansion) and hands its contents to the Task seat.
    if args.stage:
        print(out_path)
    return 0


def cmd_render_stage_all(args: argparse.Namespace) -> int:
    """Stage one LABELED prompt PER comma-listed role in a SINGLE call.

    Collapses the N per-seat ``render --stage --seat-role <role>`` calls the
    review-bearing commands used to emit into ONE. Each role gets its own
    ``.crew/reviews/<session-id>/prompt-<role>.txt`` carrying its "acting as the
    **<role>** seat" label; the special role ``seat`` maps to ``seat_role=None``
    (no label — matching today's shared ``prompt-seat.txt``). Prints a JSON
    ``{role: path}`` map on success.

    Reuses ``_resolve_session_id`` + the placeholder guard + ``_stage_path``'s
    charset/traversal guard + the SAME ``_resolve_render_inputs`` /
    ``_build_render_text`` machinery as the single-seat path, so a
    ``--stage-all opus`` file is byte-identical to ``--stage --seat-role opus``.
    The roles are PROMPT LABELS, NOT registry seats, so they are NOT filtered
    through ``known_seat_names()``.
    """
    # Mutually exclusive with the single-destination flags (each names ONE path;
    # --stage-all owns the destinations for every listed role).
    if args.stage or args.out or args.seat_role:
        print(
            "error: --stage-all is mutually exclusive with --stage, "
            "--seat-role, and -o/--out",
            file=sys.stderr,
        )
        return 2

    # Resolve + placeholder-guard the session id ONCE, up front (same shape as the
    # single-seat --stage path) — before any file is written.
    session_id = _resolve_session_id(args.session_id)
    if "<" in session_id or ">" in session_id:
        print(
            f"error: session id looks like an unsubstituted placeholder "
            f"({session_id!r}); pass your actual session id (the "
            "[Session ID: …] value), not the literal template",
            file=sys.stderr,
        )
        return 2

    roles = [r.strip() for r in args.stage_all.split(",") if r.strip()]
    if not roles:
        print(
            "error: --stage-all requires a non-empty comma-separated role list",
            file=sys.stderr,
        )
        return 2

    # Reject duplicate / normalization-colliding roles BEFORE writing anything:
    # two roles that collapse to the same staged filename (e.g. `opus,opus`, or
    # `a.b,ab` → both prompt-ab.txt) would silently stage fewer files than asked.
    staged: dict[str, str] = {}
    path_to_role: dict[str, str] = {}
    for role in roles:
        path = _stage_path(session_id, None if role == "seat" else role)
        if path in path_to_role:
            print(
                f"error: roles {path_to_role[path]!r} and {role!r} both stage to "
                f"{path!r}; give distinct roles (no duplicate/normalization collision)",
                file=sys.stderr,
            )
            return 2
        path_to_role[path] = role
        staged[role] = path

    # Resolve the prompt-content inputs ONCE (the per-role loop varies ONLY
    # seat_role + output path — no duplicated resolution logic).
    try:
        question, target, prior = _resolve_render_inputs(args)
    except _RenderInputError as exc:
        return exc.code

    for role, path in staged.items():
        # role == "seat" → seat_role=None (no spurious "seat" label; matches the
        # shared prompt-seat.txt). Every other role passes through as its label.
        builder_role = None if role == "seat" else role
        text = _build_render_text(
            question, target, prior, builder_role, args.inline_diff, args.mode
        )
        _emit(text, path)

    print(json.dumps(staged))
    return 0


def cmd_seats(args: argparse.Namespace) -> int:
    """Print the resolved seat list — one per line.

    DEFAULT (no ``--debate``): the resolved SUBPROCESS seat list — ``--seats``
    (group tokens like ``cursor`` expanded, filtered to the registry, de-duped)
    the SAME way ``review``/``council`` resolve it, but printed instead of run.
    This lets an orchestrator that fans out PER-SEAT — one ``run <seat>`` call
    each, every subprocess seat its own visible shell — obtain the expanded list
    without hardcoding the cursor group, keeping the registry the single source.

    ``--debate``: the FULL debate panel (subprocess AND Claude Task seats, so the
    /crew:debate orchestrator can split it into its subprocess fan-out + Task
    dispatch), applying debate precedence ``--seats > --panel >
    config.debate_panel() > config.default_panel() > full``. This is the engine
    hook that lets the LLM-driven ``debate.md`` honor ``.crew/config.toml`` when
    the user names no ``--panel``/``--seats`` (the markdown cannot read TOML).
    """
    if args.debate:
        try:
            resolved = _resolve_debate_seats(args.panel, args.seats)
        except _DebateSeatError as e:
            return e.code
        if args.json:
            # Split the ONE resolved debate panel into the same three fields
            # review-prep emits, so debate.md consumes an identical shape and never
            # classifies seat names itself. The debate resolver already applied
            # precedence + availability + expansion, so this only partitions: a
            # registry name is a subprocess seat, a catalog Task seat is a task seat
            # (the union is exhaustive — _resolve_debate_seats validates against it).
            known = set(known_seat_names())
            task_names = set(seats.task_seats())
            subprocess_seats = [s for s in resolved if s in known]
            task_seats = [s for s in resolved if s in task_names]
            task_seat_models = {n: _task_seat_model(n) for n in task_seats}
            payload = {
                "subprocess_seats": subprocess_seats,
                "task_seats": task_seats,
                "task_seat_models": task_seat_models,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        for s in resolved:
            print(s)
        return 0
    if args.json:
        # --json only shapes the --debate split; without --debate the subprocess
        # panel is a plain one-per-line list. Fail loud rather than silently ignore.
        print(
            "error: --json is only valid with --debate; without --debate, "
            "`seats` prints one seat per line.",
            file=sys.stderr,
        )
        return 2
    if args.panel is not None:
        # --panel only steers the DEBATE panel resolver; without --debate it would
        # be silently ignored (the default path resolves the subprocess panel from
        # --seats only). Fail loud rather than no-op.
        print(
            "error: --panel is only valid with --debate; without --debate, "
            "`seats` resolves the subprocess panel from --seats only.",
            file=sys.stderr,
        )
        return 2
    for s in _resolve_seats(args.seats):
        print(s)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """Collapse the named per-seat ``<seat>.json`` result files into ONE markdown
    digest.

    THREE modes over the same named-seats read:
      * default (no flag) — a FAITHFUL ``render_panel`` concatenation, byte-
        identical to before this feature (the existing ``test_collect`` contract).
      * ``--group`` — the deduped GROUPED digest (``findings.render_digest``):
        VERDICTS roster + CRITERIA MATRIX + GROUPED FINDINGS ``M/N`` + RAW seats.
        ``--full <path>`` ALSO writes the faithful sibling. Both the subprocess
        AND the (orchestrator-persisted, dot-stripped) Task seats flow through it.
      * ``--report-unparsed`` — print ONLY the ran-but-non-findings-parsed seat
        names (the haiku-repair candidates, Part 2); write no digest.

    Reads EXACTLY the seats named in ``--seats`` (in that order) — NO glob. This
    is the deterministic fix for the stale-seat bug: the reviews dir is reused
    across reviews within a session and seat filenames are STABLE, so a blind
    ``*.json`` glob would fold a STALE ``<seat>.json`` from an earlier panel into
    a later verdict. By reading only the named seats, a stale/foreign file is
    never read, and a named-but-missing seat renders as a SKIPPED block labeled
    with its name (never silently dropped).

    Per-file outcome (precise): a MISSING, UNREADABLE, or non-JSON file → a
    labeled SKIPPED block; a valid-JSON-but-NON-OBJECT file (``[]``/``null``/etc.)
    → a labeled SKIPPED block (via ``from_dict``'s non-object guard); a
    WELL-FORMED JSON OBJECT renders per its fields (missing fields coerced via
    ``from_dict``'s render-safe defaults — e.g. ``{}`` renders as a FAILED block,
    ``{"ok": true}`` as an OK block). This faithfully reflects whatever ``run
    --json`` wrote; collect does not validate the schema.

    EVERY block is labeled with the REQUESTED seat name ``s`` (valid, malformed,
    non-object, or missing alike) — never "unknown". The dir is resolved through
    the SAME sanitizing containment guard ``_stage_path`` uses
    (``_reviews_subdir``), so ``--session-id ../../x`` collapses to
    ``.crew/reviews/x`` (contained), reading nothing outside ``.crew/reviews/``.
    """
    # Fail LOUD on an unsubstituted template (same posture as cmd_render --stage):
    # resolve first (arg -> CLAUDE_SESSION_ID env), THEN check.
    session_id = _resolve_session_id(args.session_id)
    if "<" in session_id or ">" in session_id:
        print(
            f"error: session id looks like an unsubstituted placeholder "
            f"({session_id!r}); pass your actual session id (the "
            "[Session ID: …] value), not the literal template",
            file=sys.stderr,
        )
        return 2

    results_dir = _reviews_subdir(session_id)

    seats = [s.strip() for s in (args.seats or "").split(",") if s.strip()]

    # Validate each seat name BEFORE building a path: a seat name supplies a
    # FILENAME (`<seat>.json`), so a name with `/`, `.`, or `..` could escape the
    # session dir. Mirror _stage_path's charset guard, but REJECT (loud, nonzero)
    # rather than silently sanitize — a sanitized name would name the WRONG file
    # (e.g. `../../foo` -> `foo`). Registry seat names (codex, cursor-*) pass.
    for s in seats:
        if re.sub(r"[^A-Za-z0-9_-]", "", s) != s:
            print(
                f"error: invalid seat name {s!r} in --seats; seat names must "
                "match [A-Za-z0-9_-] (no path separators or dots). collect reads "
                "EXACTLY <seat>.json inside the session dir and never outside it.",
                file=sys.stderr,
            )
            return 2

    results: list[ProviderResult] = []
    for s in seats:
        p = results_dir / f"{s}.json"
        if not p.exists():
            # Named-but-missing seat -> SKIPPED block labeled with its name.
            results.append(ProviderResult(
                name=s, model=None, ok=False, output="",
                error="skipped: no result file for seat", elapsed=0.0,
            ))
            continue
        try:
            result = ProviderResult.from_dict(json.loads(p.read_text(encoding="utf-8")))
            # ALWAYS label by the requested seat — never "unknown". from_dict's
            # "unknown" fallback (non-object/missing-name files) can never reach
            # the digest: cmd_collect knows s, from_dict does not.
            result.name = s
        except (OSError, ValueError) as exc:
            # "not valid JSON at all" (JSONDecodeError is a ValueError subclass)
            # or unreadable file -> SKIPPED block labeled with the requested seat.
            result = ProviderResult(
                name=s, model=None, ok=False, output="",
                error=f"skipped: unreadable result file ({exc})", elapsed=0.0,
            )
        results.append(result)

    # --report-unparsed: QUERY mode for the orchestrator-side haiku repair (Part
    # 2). Print ONLY the seats that RAN but did NOT parse into structured findings
    # (newline-separated, --seats order), write no digest. The orchestrator reads
    # this list, reformats each via a haiku Task seat, writes it back with
    # `repair-seat`, then re-collects with --group.
    if getattr(args, "report_unparsed", False):
        # Query mode ignores the digest-shaping flags — say so rather than
        # silently no-op'ing them (same loud-warn discipline as --full below).
        ignored = [flag for flag, given in (
            ("--group", getattr(args, "group", False)),
            ("--full", getattr(args, "full", None)),
            ("-o/--out", getattr(args, "out", None)),
        ) if given]
        if ignored:
            print(
                f"warning: {', '.join(ignored)} ignored with --report-unparsed "
                "(query mode writes no digest; seat names go to stdout).",
                file=sys.stderr,
            )
        for name in findings.unparsed_seats(results):
            print(name)
        return 0

    # --full only does something WITH --group (it writes the faithful sibling of
    # the grouped digest). Warn loudly rather than silently no-op'ing it.
    if getattr(args, "full", None) and not getattr(args, "group", False):
        print(
            "warning: --full has no effect without --group; no faithful sibling "
            "was written (pass --group to enable it).",
            file=sys.stderr,
        )

    if getattr(args, "group", False):
        # GROUPED digest: the deduped "N/N seats flagged X" panel.
        # --full ALSO writes the byte-faithful render_panel sibling (the recovery
        # artifact + the advisory size denominator).
        text = findings.render_digest(results)
        if getattr(args, "full", None):
            full_text = render.render_panel(results)
            _emit(full_text, args.full)
            digest_bytes = len(text.encode("utf-8"))
            full_bytes = len(full_text.encode("utf-8"))
            pct = round(100 * digest_bytes / max(1, full_bytes))
            print(f"collect: digest {digest_bytes}B vs full {full_bytes}B "
                  f"({pct}%)", file=sys.stderr)
    else:
        # No --group -> byte-identical to today (the faithful projection).
        text = render.render_panel(results)
    _emit(text, args.out)
    # With -o the digest went to a file, so stdout carries ONLY the path
    # (mirrors --stage's path-only stdout contract; keeps the call allowlist-safe
    # and the digest off stdout). Print args.out VERBATIM — the path the caller
    # passed is the path to echo.
    if args.out:
        print(args.out)
    return 0


def cmd_repair_seat(args: argparse.Namespace) -> int:
    """Store a haiku-reformatted, findings-schema projection of a seat's review in
    a SEPARATE ``repaired_output`` field of ``<seat>.json`` — but ONLY IF that
    text actually parses into the FINDINGS schema. The seat's original ``output``
    is NEVER touched.

    Part 2 (per-seat haiku repair): the orchestrator Reads a non-compliant seat's
    raw ``output``, spawns a haiku Task agent to REFORMAT it into the FINDINGS
    schema (a faithful format transform, NOT a re-review), then writes the result
    back here. It is written to ``repaired_output``; ``name/model/ok/output/error/
    elapsed`` are ALL preserved byte-intact.

    **Never-lose-a-finding guarantee (the core invariant):** the repaired text is
    used for GROUPING ONLY. ``findings.parse_seat`` reads ``repaired_output`` (when
    present) to extract the grouped digest's findings/verdict/criteria, but the
    faithful ``--full`` render and the RAW / UNPARSED section ALWAYS render the
    ORIGINAL ``output``. So even a repair that PARSES but silently DROPPED a finding
    during reformatting cannot lose it: the dropped finding is still in the
    untouched ``output`` and remains recoverable from ``panel-full.md`` / RAW. The
    worst a bad repair can do is make THIS one seat's grouped rows slightly
    inaccurate; the genuine review on disk is untouchable.

    ``repaired_output`` is populated ONLY IF it parses (``parse_seat`` →
    ``findings_parsed=True``). If the reformat STILL does not parse, the write is a
    NO-OP — ``repaired_output`` is left UNSET, a stderr note says the original was
    kept, and the command exits 0: a kept-original no-op is SUCCESSFUL graceful
    degradation (the seat file is correct either way), not an error the
    orchestrator should react to. Exit 2 stays reserved for real usage/read
    errors. The seat falls back to its raw original in BOTH the grouped digest
    and ``--full``/RAW. ``collect``/``findings`` stay PURE — this helper only
    validates + records a field; it spawns NO agent."""
    seat_path = Path(args.seat)
    if not seat_path.exists():
        print(f"error: no seat file at {args.seat!r}", file=sys.stderr)
        return 2
    try:
        data = json.loads(seat_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read seat file {args.seat!r}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(f"error: seat file {args.seat!r} is not a JSON object", file=sys.stderr)
        return 2

    if args.file:
        try:
            replacement = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read replacement file {args.file!r}: {exc}",
                  file=sys.stderr)
            return 2
    else:
        replacement = sys.stdin.read()

    # Rebuild the render-safe shape (coercion). Validate the REPLACEMENT parses
    # BEFORE recording it — a repair that doesn't parse is a no-op. Validate on a
    # candidate whose ``output`` is the replacement (repaired_output unset) so we
    # test the REPLACEMENT's own parseability, not any prior repair.
    result = ProviderResult.from_dict(data)
    candidate = ProviderResult(
        name=result.name, model=result.model, ok=result.ok,
        output=replacement, error=result.error, elapsed=result.elapsed,
    )
    if not findings.parse_seat(candidate).findings_parsed:
        # No-op: the reformat still doesn't parse. Leave the seat UNCHANGED (no
        # repaired_output) so it falls back to its raw original in BOTH the grouped
        # digest and RAW/panel-full.
        print(
            f"repair-seat: reformatted text for {seat_path.name} still does not "
            "parse into the FINDINGS schema; leaving the seat UNCHANGED "
            "(no-op, the seat will render raw).",
            file=sys.stderr,
        )
        return 0

    # Record the repaired text in the SEPARATE grouping-only field. The original
    # ``output`` is NEVER overwritten: --full/RAW always render the seat's genuine
    # review, so a repair that dropped a finding cannot make it unrecoverable.
    result.repaired_output = replacement
    seat_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(str(seat_path))
    return 0


def cmd_persist_seat(args: argparse.Namespace) -> int:
    """Write ONE normalized Task-seat result to the session dir (engine-owned).

    Replaces the command-markdown convention where the ORCHESTRATOR hand-built
    the six-field JSON with the Write tool (slug computed by the LLM, name==
    filename kept by discipline). The engine now owns slug derivation, the
    render-safe shape, stale-file replacement, and the path echo — the same
    fragility-collapse rationale as ``run``'s -f/-o derivation. Subprocess seats
    never come through here (their runs write ``<seat>.json`` directly).

    Task seats ONLY. The slug is derived from ``_seat_role_slug`` — the ONE
    canonical role->filename source shared with staging/collect — so the filename
    and the ``name`` field can never diverge (they come from one call). The
    session dir resolves via ``_reviews_subdir`` (the SAME sanitizing resolver
    ``collect`` reads from), so a persisted seat always lands where the grouped
    ``collect`` looks. Overwriting an existing ``<slug>.json`` IS the stale-file
    clear the convention did with ``rm -f``."""
    # Resolve + placeholder-guard the session id up front (same posture as
    # cmd_render --stage / cmd_collect): resolve (arg -> CLAUDE_SESSION_ID env),
    # THEN reject an unsubstituted <...> template so a templating slip fails loud
    # rather than silently collapsing to a constant `session-id` segment.
    session_id = _resolve_session_id(args.session_id)
    if "<" in session_id or ">" in session_id:
        print(
            f"error: session id looks like an unsubstituted placeholder "
            f"({session_id!r}); pass your actual session id (the "
            "[Session ID: …] value), not the literal template",
            file=sys.stderr,
        )
        return 2
    if not (args.seat or "").strip():
        print("error: empty seat name", file=sys.stderr)
        return 2
    slug = _seat_role_slug(args.seat)
    if args.file:
        try:
            output = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {args.file!r}: {exc}", file=sys.stderr)
            return 2
    else:
        output = sys.stdin.read()
    error = None
    if args.failed:
        error = (args.error or "").strip() or "Task seat failed (no diagnostic provided)"
    result = ProviderResult(
        name=slug, model=args.model, ok=not args.failed,
        output=output, error=error, elapsed=float(args.elapsed),
    )
    out_dir = _reviews_subdir(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.json"
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(str(path))
    return 0


def cmd_review_prep(args: argparse.Namespace) -> int:
    """Prepare a review/build/measure-twice fan-out and PRINT a JSON contract —
    it runs NOTHING.

    Consolidates the deterministic PREP the command markdown used to do inline
    across three engine calls (``seats`` + ``render --mode review --stage`` + the
    prose seat-splitting): (a) resolve the target via the SAME ``targets.resolve``
    path ``render``/``review`` use; (b) resolve ``--panel``/``--seats`` into the
    SUBPROCESS seats (through the registry) AND the TASK seats (the Claude voices,
    via ``seats.merged_panels()`` + ``seats.task_seats()``); (c) build the
    ``task_seat_models`` map (the catalog's per-seat ``model`` is the SINGLE source of
    every model pin); (d) stage the ONE shared subprocess prompt via the SAME
    ``_build_render_text`` + ``_stage_path`` machinery ``render --mode review
    --stage`` uses — so ``prompt_path``'s file is BYTE-IDENTICAL to ``render
    --mode review --stage`` for the same target. Then it prints ONE JSON object
    ``{prompt_path, subprocess_seats, task_seats, task_seat_models}`` (insertion
    key order, ``ensure_ascii=False``) and exits.

    THIS function owns ``--panel``/``--seats`` → seat-split + model-pin
    resolution. The orchestrator only READS the JSON: it iterates
    ``subprocess_seats`` for the per-seat ``crew run`` fan-out, joins
    ``task_seats`` into ``render --stage-all`` to stage the Task prompts, and
    spawns each Task seat with ``model = task_seat_models[seat]``. The
    orchestrator NEVER classifies seat names or hardcodes a model pin.

    LOAD-BEARING — prep PREPARES; it does NOT run seats. This function MUST NOT
    call ``_fan_out``, ``_run_seat``, ``_run_cli``, or any provider ``.run()``.
    The per-seat ``crew run <seat>`` loop stays in the command markdown, where
    each seat is a SEPARATE, visible, individually-killable shell. Folding that
    loop into ``_fan_out`` here is the EXACT regression this design reverses: it
    re-hides every seat in one opaque thread pool and destroys per-shell
    killability. DO NOT add a run/fan-out call to this function.

    ``--task-seats`` is a PURE OPAQUE ECHO and an EXPLICIT task-only override: its
    value is split on commas and echoed verbatim into ``task_seats`` (taking
    precedence over the ``--panel``/``--seats``-derived task list). The engine
    NEVER resolves a task seat through the subprocess registry, NEVER spawns it,
    NEVER runs it — Claude-seat ownership (the dispatch) stays with the
    orchestrator (the deliberately-rejected ``TaskProvider`` boundary;
    scripts/CLAUDE.md "Provider = executor"). What this function DOES own is the
    NAME-level split + model-pin resolution that feeds that dispatch.
    """
    # 1. Target dispatch — the SAME call cmd_review/cmd_render make. --base
    #    defaults to "main" (parity with render/review); coerce before resolve.
    base = args.base if args.base is not None else "main"
    try:
        target = targets.resolve(args.target, base=base)
    except targets.TargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # 2. --panel/--seats → subprocess + task seats. --seats and --panel are
    #    INDEPENDENT axes: --seats overrides the SUBPROCESS subset (and supplies
    #    task names found within it); --panel supplies the TASK seats when --seats
    #    names none. Sentinel mechanics (--seats default None, --panel default None):
    #      * --seats <given> (incl. "")  -> seats_given; classify ITS names
    #      * --panel P (named)           -> preset_names = _panel_seat_list(P)
    #      * BOTH omitted                -> preset_names = _panel_seat_list(
    #                                       default_panel → "full"; per-repo → global
    #                                       → built-in)
    #    preset_names is the SINGLE panel-resolved backing list feeding BOTH the
    #    subprocess (2a) and task (2b) splits (refinement 5). Panel-name resolution
    #    goes through the shared _panel_seat_list (consults [panels] before
    #    the shipped panels; unknown name → 'full', never a raw KeyError). Availability
    #    is applied WHOLE-PANEL: each split is drop-filtered (skip-noting an
    #    explicitly-named unavailable seat) WITHOUT a per-split empty-fallback, and
    #    the unfiltered-panel fallback fires ONCE, only when the ENTIRE resolved
    #    panel (subprocess + task TOGETHER) would be empty — so disabling a whole
    #    seat-KIND (e.g. both task seats in `full`) drops it for real instead of
    #    re-adding it because the other kind still has seats. A name classifies as
    #    exactly one of: a registry subprocess seat (-> subprocess_seats), a
    #    catalog Task seat (-> task_seats), or NEITHER (silently DROPPED;
    #    never loud-error). A NAMED --panel's members are EXPLICIT (an unavailable one
    #    is skipped WITH a note); a default_panel's are silent drops (refinement 3).
    seats_given = args.seats is not None
    source_names = (
        [s.strip() for s in args.seats.split(",") if s.strip()]
        if seats_given else []
    )
    panel_named = args.panel is not None
    if panel_named:
        preset_names: list[str] | None = _panel_seat_list(args.panel)
    elif not seats_given:
        preset_names = _panel_seat_list(config.default_panel() or "full")
    else:
        preset_names = None

    known = set(known_seat_names())

    def _dedup(xs: list[str]) -> list[str]:
        seen: set[str] = set()
        return [x for x in xs if not (x in seen or seen.add(x))]

    # 2a. Subprocess RAW backing (registry-filtered, pre-availability) + explicit
    #     set. An explicit-empty/omitted --seats yields []. Availability is NOT
    #     applied yet — that happens whole-panel in 2d so a per-split empty can't
    #     trigger the fallback on its own.
    if seats_given:
        sub_expanded = _expand_seat_groups(source_names)
        sub_explicit = set(sub_expanded)                       # --seats names explicit
    elif preset_names is not None:
        sub_expanded = _expand_seat_groups(preset_names)
        sub_explicit = set(sub_expanded) if panel_named else set()  # default: silent
    else:
        sub_expanded = []
        sub_explicit = set()
    sub_raw = [s for s in sub_expanded if s in known]

    # 2b. Task RAW backing + explicit set. Precedence: explicit --task-seats override
    #     (OPAQUE echo, never resolved/availability-filtered) > task names in an
    #     explicit --seats > the --panel/default preset's task names > []. NEVER
    #     resolved through the subprocess registry, NEVER spawned/run. When the
    #     opaque override is in play the task list bypasses availability entirely
    #     (task_raw stays empty so 2d's whole-panel fallback considers ONLY the
    #     subprocess seats — the override IS the task panel).
    task_names = set(seats.task_seats())
    explicit_task_seats = [
        s.strip() for s in (args.task_seats or "").split(",") if s.strip()
    ]
    override_task = bool(explicit_task_seats)
    if override_task:
        task_raw: list[str] = []
        task_explicit: set[str] = set()
    else:
        seats_task_names = (
            [n for n in _expand_seat_groups(source_names) if n in task_names]
            if seats_given else []
        )
        if seats_given and seats_task_names:
            task_raw = seats_task_names
            task_explicit = set(_expand_seat_groups(source_names))
        elif preset_names is not None:
            preset_expanded = _expand_seat_groups(preset_names)
            task_raw = [n for n in preset_expanded if n in task_names]
            task_explicit = set(preset_expanded) if panel_named else set()
        else:
            task_raw = []
            task_explicit = set()

    # 2d. Availability — WHOLE-PANEL. Drop unavailable seats per-split (skip-noting
    #     an explicitly-named one) with NO per-split fallback, then apply the
    #     unfiltered-panel fallback ONCE only if the ENTIRE resolved panel
    #     (subprocess + task + the opaque --task-seats override) is empty. This is
    #     the BLOCKING fix: disabling both task seats in `full` yields task_seats ==
    #     [] with the five subprocess seats intact (no opus/sonnet restoration);
    #     disabling EVERY seat triggers a single whole-panel fallback (one warn)
    #     restoring the unfiltered panel. The opaque --task-seats override
    #     (explicit_task_seats; task_raw stays [] for it) counts toward the final
    #     panel being NON-empty, so `--task-seats opus` with every subprocess seat
    #     unavailable must NOT restore the disabled subprocess seats — the override
    #     IS the (non-empty) panel.
    sub_kept = _drop_unavailable(sub_raw, sub_explicit)
    task_kept = _drop_unavailable(task_raw, task_explicit)
    if (
        (sub_raw or task_raw or explicit_task_seats)
        and not sub_kept
        and not task_kept
        and not explicit_task_seats
    ):
        _all_unavailable_warn()
        sub_kept, task_kept = sub_raw, task_raw
    subprocess_seats = _dedup(sub_kept)
    task_seats = explicit_task_seats if override_task else _dedup(task_kept)

    # 2c. task_seat_models: the per-seat model pin for each Task seat. The CATALOG
    #     is the SINGLE source (a seat's `model` is a Task-tool alias). The
    #     orchestrator reads `model` FROM here; it never hardcodes a pin.
    task_seat_models = {n: _task_seat_model(n) for n in task_seats}

    # 4. Stage the shared SUBPROCESS prompt ONLY when subprocess seats exist —
    #    via the SAME machinery cmd_render --stage uses, so prompt_path's file is
    #    BYTE-IDENTICAL to `render <target> --mode review --stage`. When there are
    #    no subprocess seats (Claude-only lite/solo), stage NOTHING and leave
    #    prompt_path == "".
    prompt_path = ""
    if subprocess_seats:
        # Resolve + placeholder-guard the session id the SAME way cmd_render
        # --stage does (arg -> CLAUDE_SESSION_ID env, THEN reject an
        # unsubstituted <...> placeholder so a templating slip fails loud).
        session_id = _resolve_session_id(args.session_id)
        if "<" in session_id or ">" in session_id:
            print(
                f"error: session id looks like an unsubstituted placeholder "
                f"({session_id!r}); pass your actual session id (the "
                "[Session ID: …] value), not the literal template",
                file=sys.stderr,
            )
            return 2
        # The shared subprocess prompt has NO per-seat label (seat_role=None),
        # no prior round, and forwards --inline-diff — exactly the inputs
        # `render <target> --mode review --stage` feeds the builder.
        text = _build_render_text(
            None, target, None, None, args.inline_diff, args.mode
        )
        prompt_path = _stage_path(session_id, None)
        _emit(text, prompt_path)

    # 5. Print the JSON contract — the ONLY output. FIXED insertion key order
    #    (prompt_path, subprocess_seats, task_seats, task_seat_models) IS the
    #    contract, so do NOT sort_keys. ensure_ascii=False (Plan B's escaping
    #    lesson — no \uXXXX).
    payload = {
        "prompt_path": prompt_path,
        "subprocess_seats": subprocess_seats,
        "task_seats": task_seats,
        "task_seat_models": task_seat_models,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


# =============================================================================
# /crew:init support — `doctor` (probe) + `scaffold-config` (template generator)
# =============================================================================
#
# Both are registry-derived and NON-BILLABLE: doctor calls each provider's
# is_available() (codex/agy = pure shutil.which; cursor = its single local
# `agent --version` identity probe, run ONCE and fanned to every cursor-* seat).
# scaffold-config performs NO probe at all — it consumes doctor's JSON via
# --detection. The stdout/stderr discipline is load-bearing: stdout carries ONLY
# the machine payload (doctor JSON / {target,wrote,diverted} envelope / `--out -`
# TOML); EVERY note/error goes to stderr (init.md parses stdout).

# Generic trailer for every opt-in seat's scaffold line — the curated per-seat WHY
# lives in ONE place (docs/engine-notes.md's "Why some registered seats are opt-in").
_OPT_IN_COMMENT = "opt-in; see docs/engine-notes.md for why"

# Honest task-seat diagnostics (subscription-backed; not CLI-detectable).
_TASK_SEAT_DIAGS = {
    "opus": "Claude subscription (in-session); not CLI-detectable",
    "sonnet": "Claude subscription (in-session); not CLI-detectable",
    "fable": "Claude subscription (in-session); opt-in (premium tier)",
}


def cmd_doctor(args: argparse.Namespace) -> int:
    """Probe per-seat provider availability and emit a JSON detection map (D1).

    Iterates ``known_seat_names()`` → ``get_provider(n).is_available()`` into a
    ``subprocess`` map, then adds a static ``task`` map (the subscription-backed
    Claude seats, in ``sorted(task_seats())`` order for deterministic JSON).

    Availability is probed once per provider KIND, not once per seat: seats of one
    kind share a binary and answer identically, so the first seat's result is
    reused for the rest. This matters for cursor, whose ``is_available`` SPAWNS
    ``agent --version`` — without the dedupe it would spawn once per cursor seat
    (6x today, more as seats are declared). codex/agy use a cheap in-process
    ``shutil.which`` where the dedupe is a harmless micro-optimization.

    NON-BILLABLE: no metered/network call. stdout carries ONLY the JSON; every
    diagnostic/error goes to stderr. File-write destination follows the D1 matrix
    (``-o`` wins; else the session path; else no file) — the JSON is ALWAYS
    printed to stdout regardless.
    """
    subprocess_map: dict[str, dict] = {}
    avail_by_kind: dict[str, tuple[bool, str]] = {}
    for name in known_seat_names():
        kind = seats.seat_spec(name).provider
        if kind not in avail_by_kind:
            avail_by_kind[kind] = get_provider(name).is_available()
        avail, diag = avail_by_kind[kind]
        subprocess_map[name] = {"available": bool(avail), "diag": diag}

    task_map: dict[str, dict] = {}
    for name in sorted(seats.task_seats()):
        task_map[name] = {
            "available": True,
            "diag": _TASK_SEAT_DIAGS.get(name, "Claude subscription (in-session)"),
        }

    text = json.dumps(
        {"subprocess": subprocess_map, "task": task_map},
        ensure_ascii=False, indent=2,
    )

    # File-write destination matrix (D1): -o wins; else session path; else none.
    out_path = args.out
    if out_path:
        out_path = os.path.expanduser(out_path)
    else:
        session_id = _resolve_session_id(args.session_id)
        if "<" in session_id or ">" in session_id:
            print(
                f"error: session id looks like an unsubstituted placeholder "
                f"({session_id!r}); pass your actual session id (the "
                "[Session ID: …] value), not the literal template",
                file=sys.stderr,
            )
            return 2
        if session_id:
            out_path = str(_reviews_subdir(session_id) / "doctor.json")

    if out_path:
        p = Path(out_path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")

    # ALWAYS print the JSON to stdout (the machine payload).
    print(text)
    return 0


# =============================================================================
# `probe` — live, opt-in, BILLABLE end-to-end seat smoke test
# =============================================================================
#
# `doctor` proves a seat's CLI is on PATH (non-billable, no exec). `probe`
# proves a seat actually RETURNS a usable review: it sends a trivial canned
# prompt through the real provider machinery (the same `get_provider(...).run`
# path `cmd_run` uses) and checks for a marker string in the reply. This is the
# opt-in check for the agy-silently-dead-across-sessions failure mode that
# `doctor`'s PATH-only probe cannot see.

_PROBE_PROMPT = (
    "This is a connectivity probe, not a review. Reply with exactly the "
    "single line: PROBE-OK\n"
    "Do not run any commands. Do not read any files. Reply with that one "
    "line and nothing else."
)


def cmd_probe(args: argparse.Namespace) -> int:
    """Live end-to-end seat smoke test. BILLABLE (runs each seat's real CLI).

    doctor answers "is the CLI installed?"; probe answers "does the seat
    actually return usable output?". Subprocess seats only — Task seats are
    orchestrator-dispatched and rejected with the same loud message cmd_run
    uses. Exit 0 = at least one seat passed and none failed/degraded;
    1 = any fail/degraded; 2 = usage error OR every probed seat was skipped
    (nothing actually passed — an all-skipped probe must NOT masquerade as a
    healthy green to a scripted health check keying on the exit code).
    """
    names = list(known_seat_names()) if args.all else args.seats
    if not names:
        print("error: name at least one seat, or pass --all", file=sys.stderr)
        return 2
    for n in names:
        if n in seats.task_seats():
            print(
                f"error: {n!r} is a Task seat — orchestrator-dispatched, "
                "not probeable by the engine",
                file=sys.stderr,
            )
            return 2
        if n not in known_seat_names():
            print(f"error: unknown seat {n!r}", file=sys.stderr)
            return 2

    # -o / session-path write matrix (D1, copied from cmd_doctor's exact
    # branch): -o wins; else the session path (.crew/reviews/<id>/probe.json);
    # else no file. The JSON is ALWAYS also printed to stdout. Resolved (and
    # the session-id placeholder usage error raised) BEFORE any billable
    # provider.run below — mirroring cmd_run's session-id guard ahead of its
    # own provider.run — so a malformed --session-id exits 2 with NO seat
    # CLI having been invoked.
    out_path = args.out
    if out_path:
        out_path = os.path.expanduser(out_path)
    else:
        session_id = _resolve_session_id(args.session_id)
        if "<" in session_id or ">" in session_id:
            print(
                f"error: session id looks like an unsubstituted placeholder "
                f"({session_id!r}); pass your actual session id (the "
                "[Session ID: …] value), not the literal template",
                file=sys.stderr,
            )
            return 2
        if session_id:
            out_path = str(_reviews_subdir(session_id) / "probe.json")

    # The BILLABLE warning always prints before any provider runs.
    print(
        "probe: BILLABLE — this runs each seat's real CLI once.",
        file=sys.stderr,
    )
    results: dict[str, dict] = {}
    for n in names:
        provider = get_provider(n)
        avail, diag = provider.is_available()
        if not avail:
            results[n] = {"status": "skipped", "detail": diag, "elapsed": 0.0}
            continue
        # Provider invocation copied from cmd_run's real call: the seat's own
        # resolved model (probe exposes no --model override) and the same
        # _resolve_timeout chain.
        timeout = _resolve_timeout(args.timeout)
        r = provider.run(_PROBE_PROMPT, timeout=timeout)
        out = (r.output or "").strip()
        if r.ok and any(ln.strip() == "PROBE-OK" for ln in out.splitlines()):
            status, detail = "pass", ""
        elif r.ok and out:
            status, detail = "degraded", f"no PROBE-OK marker; got: {out[:120]!r}"
        else:
            status, detail = "fail", (r.error or "empty output")
        results[n] = {
            "status": status, "detail": detail, "elapsed": float(r.elapsed or 0.0),
        }

    text = json.dumps(results, ensure_ascii=False, indent=2)

    if out_path:
        p = Path(out_path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")

    print(text)
    statuses = [v["status"] for v in results.values()]
    if any(s in ("fail", "degraded") for s in statuses):
        return 1
    if any(s == "pass" for s in statuses):
        return 0
    # No pass and no fail → every probed seat was skipped. A fully-skipped
    # probe proved nothing ran; exit 2 so it can't read as a healthy green.
    return 2


# --- scaffold-config: template rendering -------------------------------------

def _toml_str(s: str) -> str:
    """A seat/panel name as a TOML string literal. Names are restricted to a
    simple charset (validated upstream), so no escaping is needed."""
    return '"' + s + '"'


def _det_lookup(detection: dict, seat: str) -> dict | None:
    """The per-seat detection record from a doctor JSON payload, or None."""
    sub = detection.get("subprocess")
    if isinstance(sub, dict) and isinstance(sub.get(seat), dict):
        return sub[seat]
    tsk = detection.get("task")
    if isinstance(tsk, dict) and isinstance(tsk.get(seat), dict):
        return tsk[seat]
    return None


def _seat_avail(
    seat: str, *, detection: dict | None, opted_in: set[str], disabled: set[str],
) -> tuple[str, str | None] | None:
    """Decide a seat's emitted ``available`` line for a FRESH template.

    Returns ``(value, comment)`` to emit ``available = <value>`` (with an optional
    trailing comment), or ``None`` to OMIT the line. Precedence (explicit
    correction beats detection):
      1. ``--disable-seat`` → ``false`` (always).
      2. ``--add-seat``     → omit (the seat defaults to available).
      3. detection PRESENT + seat detected absent → ``false  # not found on PATH``.
      4. detection PRESENT + premium-off seat (not opted in) → ``false  # opt-in …``.
      5. otherwise → omit.
    When ``detection`` is None (no --detection flag) only an explicit
    ``--disable-seat`` emits a line; every other seat is omitted (D4).
    """
    if seat in disabled:
        return ("false", None)
    if seat in opted_in:
        return None
    if detection is not None:
        det = _det_lookup(detection, seat)
        if det is not None and det.get("available") is False:
            return ("false", "not found on PATH")
        if seat in seats.premium_off_seats():
            return ("false", _OPT_IN_COMMENT)
    return None


def _avail_line(av: tuple[str, str | None]) -> str:
    value, comment = av
    return f"available = {value}   # {comment}" if comment else f"available = {value}"


def _render_config_template(
    detection: dict | None, *, scope: str, default_panel: str, dispatch_seat: str,
    opted_in: set[str], disabled: set[str],
) -> str:
    """Render the COMMENTED starter config (D4) — ONLY keys the loader reads.

    ``reasoning_effort`` lives under the codex seat tables (``[seats.codex]`` /
    ``[seats.codex-luna]``, resolved per seat) and ``print_timeout`` ONLY under
    ``[seats.agy]`` (where the getters read them). Per-seat ``available``
    lines are decided by ``_seat_avail`` (cost-safe defaults; premium seats off).
    """
    today = datetime.date.today().isoformat()
    if scope == "repo":
        path_label = ".crew/config.toml"
        tier_note = "per-repo overrides of the global ~/.crew-config.toml"
    else:
        path_label = "~/.crew-config.toml"
        tier_note = "machine-wide defaults below per-repo .crew/config.toml"

    def av(seat: str) -> tuple[str, str | None] | None:
        return _seat_avail(seat, detection=detection, opted_in=opted_in, disabled=disabled)

    L: list[str] = []
    L.append(f"# {path_label} — generated by /crew:init on {today}")
    L.append(f"# {tier_note}.")
    L.append("# Resolution: CLI flag > per-repo .crew/config.toml > global ~/.crew-config.toml > built-in.")
    L.append("# Every key below is OPTIONAL; delete what you don't need. Python 3.11+ required to load.")
    L.append("")
    L.append("# Default panel when you name neither --panel nor --seats.")
    L.append("# Cost-safe built-in full = codex + codex-luna + agy + cursor-auto + cursor-composer + opus + sonnet.")
    L.append(f"default_panel = {_toml_str(default_panel)}")
    L.append("")
    L.append("# [debate].panel — /crew:debate's default panel (can default fuller than reviews).")
    L.append("# [debate]")
    L.append('# panel = "full"')
    L.append("")
    L.append("# [dispatch].seat — /crew:dispatch's default WRITE seat (must be ONE known subprocess")
    L.append("# seat: codex-*/agy/cursor-*; a panel name or the 'cursor' group token is rejected).")
    L.append("[dispatch]")
    L.append(f"seat = {_toml_str(dispatch_seat)}")
    L.append("")
    L.append("# [panels] — redefine a built-in preset or add a custom one (usable via --panel <name>).")
    L.append("# [panels]")
    L.append('# nightly = ["codex", "opus"]')
    L.append("")
    L.append("# ---- Per-seat tuning + availability -------------------------------------------------")
    L.append("# available = false drops a seat from EVERY resolved panel. It is an opt-OUT: runtime")
    L.append("# already skips a CLI that isn't installed, so these flags are mainly for DISCOVERABILITY,")
    L.append("# a DELIBERATE opt-out, and capturing your confirmed preferences — NOT a requirement.")
    L.append("")
    # The default (non-opt-in) executor seats, catalog-derived: the list, the
    # model hint, and the per-kind tuning knob all grow with seats.toml. The knob
    # comment keys off the provider KIND (codex kinds carry reasoning_effort, agy
    # carries print_timeout); a kind with no knob emits none. The opt-in executor
    # seats are emitted below by the premium_off_seats() loop, so they are skipped
    # here.
    for name, spec in seats.merged_catalog().items():
        if not spec.has_executor or spec.declared or spec.opt_in:
            continue
        L.append(f"[seats.{name}]")
        a = av(name)
        if a is not None:
            L.append(_avail_line(a))
        L.append(f'# model = "{spec.model}"')
        if spec.provider == "codex":
            L.append(
                f'# reasoning_effort = "xhigh"     # codex-seat knob '
                f'(read per seat: [seats.{name}] here)'
            )
        elif spec.provider == "agy":
            L.append(
                f'# print_timeout = "8m"           # agy-only knob '
                f'(read only from [seats.{name}])'
            )
        L.append("")
    for seat in seats.premium_off_seats():
        a = av(seat)
        if a is not None:
            L.append(f"[seats.{seat}]")
            L.append(_avail_line(a))
        else:
            L.append(f"# [seats.{seat}]   # {_OPT_IN_COMMENT}")
        L.append("")
    # Lazy import (call-time), like config.py's: the engine stays a leaf that does
    # not need the state models at load. Emitting the real numbers keeps the
    # scaffolded default and the enforced bound from drifting apart.
    from models import DEFAULT_DEADLINE_MINUTES, MAX_DEADLINE_MINUTES

    L.append("# [tuning].timeout — per-seat wall-clock default (positive integer seconds).")
    L.append("# [tuning].deadline_minutes: the persistence loops' wall clock, read by")
    L.append(f"#   `crew state init` (1-{MAX_DEADLINE_MINUTES}; an out-of-range value is")
    L.append("#   dropped with a warning, never silently clamped).")
    L.append("# [tuning]")
    L.append("# timeout = 600")
    L.append(f"# deadline_minutes = {DEFAULT_DEADLINE_MINUTES}")

    # Task seats: only an explicit --disable-seat (opt-out) emits a line.
    task_blocks: list[str] = []
    for seat in sorted(seats.task_seats()):
        a = av(seat)
        if a is not None:
            task_blocks.append("")
            task_blocks.append(f"[seats.{seat}]")
            task_blocks.append(_avail_line(a))
    if task_blocks:
        L.append("")
        L.append("# ---- Claude (subscription) seats — opt-out only -----------------------------------")
        L.extend(task_blocks)

    return "\n".join(L)


# --- scaffold-config: conservative section-scoped --repo line overrides (D3) --

def _override_note(label: str) -> str:
    return (
        f"could not safely apply override {label!r} — your global config defines it via "
        "an inline table / dotted key / multi-line value, or defines it more than once; "
        "left verbatim; edit .crew/config.toml by hand"
    )


# A TOML table header: ``[name]`` tolerating an optional trailing inline comment
# (``[seats.codex] # note``). The captured name is then NORMALIZED before any
# comparison so quoted components / internal whitespace also resolve.
_HEADER_RE = re.compile(r"\s*\[([^\]]+)\]\s*(#.*)?$")


def _normalize_section(raw: str) -> str | None:
    """Normalize a TOML table-header name to its bare dotted form for comparison.

    Splits the dotted key into segments (a ``.`` inside a quoted segment does NOT
    separate), strips surrounding whitespace per segment, and strips matching
    quotes from a quoted segment — so ``seats."cursor-glm"``, ``[ seats.codex ]``
    and ``seats.'agy'`` compare equal to the bare ``seats.cursor-glm`` /
    ``seats.codex`` / ``seats.agy`` targets. Returns ``None`` for a header the
    parser cannot make sense of (empty / unterminated quote / stray char), so the
    caller treats it as UNRECOGNIZED rather than guessing."""
    s = raw.strip()
    segs: list[str] = []
    i, n = 0, len(s)
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            return None  # empty header or trailing dot
        if s[i] in ("\"", "'"):
            quote = s[i]
            i += 1
            start = i
            while i < n and s[i] != quote:
                i += 1
            if i >= n:
                return None  # unterminated quote
            segs.append(s[start:i])
            i += 1  # consume closing quote
        else:
            start = i
            while i < n and s[i] not in " \t.":
                i += 1
            segs.append(s[start:i])
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        if s[i] != ".":
            return None  # stray char after a segment
        i += 1
    return ".".join(segs)


def _header_name(line: str) -> str | None:
    """The NORMALIZED section name of ``line`` if it is a live table header, else
    None. (Commented lines must be filtered by the caller.)"""
    m = _HEADER_RE.match(line)
    if not m:
        return None
    return _normalize_section(m.group(1))


def _live_sections(lines: list[str]) -> list[str | None]:
    """For each line index, the CURRENT live ``[table]`` section (or None for the
    top level). Commented lines (``^\\s*#``) never change the section. Recognizes
    commented-trailing / quoted / whitespace header forms (normalized name)."""
    cur: str | None = None
    out: list[str | None] = []
    for line in lines:
        if not line.lstrip().startswith("#"):
            name = _header_name(line)
            if name is not None:
                cur = name
        out.append(cur)
    return out


def _find_live_header(lines: list[str], section: str) -> int | None:
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if _header_name(line) == section:
            return i
    return None


def _leading_comment_end(lines: list[str]) -> int:
    """Index of the first line that is NOT part of the leading ``#``/blank block
    (i.e. where a top-level key may be inserted). 0 when the file starts directly
    with a header / has no leading comment block (the position-0 edge)."""
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "" or s.startswith("#"):
            continue
        return i
    return len(lines)


def _apply_toplevel(
    lines: list[str], key: str, value: str, label: str,
) -> tuple[list[str], list[str]]:
    sections = _live_sections(lines)
    keyre = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    hits = [
        i for i, sec in enumerate(sections)
        if sec is None and not lines[i].lstrip().startswith("#") and keyre.match(lines[i])
    ]
    if len(hits) == 1:
        rhs = lines[hits[0]].split("=", 1)[1].strip()
        if rhs.startswith("{"):
            return lines, [_override_note(label)]
        out = list(lines)
        out[hits[0]] = f"{key} = {value}"
        return out, []
    if len(hits) > 1:
        return lines, [_override_note(label)]
    out = list(lines)
    out.insert(_leading_comment_end(out), f"{key} = {value}")
    return out, []


def _toplevel_table_other_form(lines: list[str], table: str) -> bool:
    """True when ``table`` is defined at the top level via an inline table
    (``table = { … }``) or a dotted key (``table.x = …``) — shapes a header
    append would duplicate."""
    sections = _live_sections(lines)
    inline = re.compile(r"^\s*" + re.escape(table) + r"\s*=\s*\{")
    dotted = re.compile(r"^\s*" + re.escape(table) + r"\s*\.")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if sections[i] is None and (inline.match(line) or dotted.match(line)):
            return True
    return False


def _apply_single_table_key(
    lines: list[str], table: str, key: str, value: str, label: str,
) -> tuple[list[str], list[str]]:
    """Section-scoped override for a simple top-level ``[table]`` (e.g. dispatch).
    Case 1 (key present once) → replace; case 2 (header present, key absent) →
    insert after header; case 3 (absent in every form) → append a new table;
    inline/dotted/duplicate → leave verbatim + note (case 5)."""
    sections = _live_sections(lines)
    keyre = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    hits = [
        i for i, sec in enumerate(sections)
        if sec == table and not lines[i].lstrip().startswith("#") and keyre.match(lines[i])
    ]
    if len(hits) == 1:
        rhs = lines[hits[0]].split("=", 1)[1].strip()
        if rhs.startswith("{"):
            return lines, [_override_note(label)]
        out = list(lines)
        out[hits[0]] = f"{key} = {value}"
        return out, []
    if len(hits) > 1:
        return lines, [_override_note(label)]
    header_idx = _find_live_header(lines, table)
    if header_idx is not None:
        out = list(lines)
        out.insert(header_idx + 1, f"{key} = {value}")
        return out, []
    if _toplevel_table_other_form(lines, table):
        return lines, [_override_note(label)]
    out = list(lines)
    if out and out[-1].strip() != "":
        out.append("")
    out.append(f"[{table}]")
    out.append(f"{key} = {value}")
    return out, []


def _seat_defined_other_form(lines: list[str], seat: str) -> bool:
    """True when ``seats.<seat>`` is defined via a shape a ``[seats.<seat>]``
    append would DUPLICATE: an inline ``<seat> = { … }`` under ``[seats]``, a
    dotted ``seats.<seat>`` / ``<seat>.x`` key, or a whole inline ``seats = { … }``.
    Skips commented lines (a commented-out form must not block a correction)."""
    sections = _live_sections(lines)
    inline_under_seats = re.compile(r"^\s*" + re.escape(seat) + r"\s*=\s*\{")
    dotted_under_seats = re.compile(r"^\s*" + re.escape(seat) + r"\s*\.")
    dotted_top = re.compile(r"^\s*seats\s*\.\s*" + re.escape(seat) + r"\b")
    seats_inline_top = re.compile(r"^\s*seats\s*=\s*\{")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        sec = sections[i]
        if sec == "seats" and (inline_under_seats.match(line) or dotted_under_seats.match(line)):
            return True
        if sec is None and (dotted_top.match(line) or seats_inline_top.match(line)):
            return True
    return False


def _apply_seat_available(
    lines: list[str], seat: str, value: str, label: str,
) -> tuple[list[str], list[str]]:
    """Section-scoped ``[seats.<seat>].available`` override (D3 cases 1/2/3/5)."""
    section = f"seats.{seat}"
    sections = _live_sections(lines)
    keyre = re.compile(r"^\s*available\s*=")
    hits = [
        i for i, sec in enumerate(sections)
        if sec == section and not lines[i].lstrip().startswith("#") and keyre.match(lines[i])
    ]
    if len(hits) == 1:
        out = list(lines)
        out[hits[0]] = f"available = {value}"
        return out, []
    if len(hits) > 1:
        return lines, [_override_note(label)]
    header_idx = _find_live_header(lines, section)
    if header_idx is not None:
        out = list(lines)
        out.insert(header_idx + 1, f"available = {value}")
        return out, []
    if _seat_defined_other_form(lines, seat):
        return lines, [_override_note(label)]
    out = list(lines)
    if out and out[-1].strip() != "":
        out.append("")
    out.append(f"[seats.{seat}]")
    out.append(f"available = {value}")
    return out, []


def _apply_repo_overrides(
    base: str, *, default_panel: str, dispatch_seat: str, seat_avail: dict[str, bool],
) -> tuple[str, list[str]]:
    """Seed the repo config from the global text VERBATIM (comments preserved),
    then apply the CONSERVATIVE section-scoped line overrides (D3). The whole-seed
    pre-gate routes any multi-line / array-of-tables base to leave-verbatim."""
    if '"""' in base or "'''" in base or "[[" in base:
        return base, [
            "could not safely apply overrides — your global config contains a multi-line "
            "string or array-of-tables; left verbatim; edit .crew/config.toml by hand"
        ]
    notes: list[str] = []
    lines = base.split("\n")
    lines, n = _apply_toplevel(lines, "default_panel", _toml_str(default_panel), "default_panel")
    notes += n
    lines, n = _apply_single_table_key(lines, "dispatch", "seat", _toml_str(dispatch_seat), "dispatch.seat")
    notes += n
    for seat, val in seat_avail.items():
        lines, n = _apply_seat_available(lines, seat, "true" if val else "false", seat)
        notes += n
    result = "\n".join(lines)
    # Parse-guard BACKSTOP: the base is known to parse (validated by the caller).
    # If our line-edits somehow produced an UNLOADABLE result (a header form the
    # recognizer still misses → a duplicate table, etc.), DISCARD the edit and
    # leave the base verbatim — never hand back a config that won't load.
    try:
        config.tomllib.loads(result)
    except (config.tomllib.TOMLDecodeError, UnicodeDecodeError):
        return base, [
            "could not safely apply overrides to your global — left it "
            "unchanged; edit .crew/config.toml by hand"
        ]
    return result, notes


# --- scaffold-config: command -------------------------------------------------

def _scaffold_stderr(notes: list[str]) -> None:
    for note in notes:
        print(f"crew scaffold-config: {note}", file=sys.stderr)


def cmd_scaffold_config(args: argparse.Namespace) -> int:
    """Render + write the commented starter config (D2/D3/D4).

    ONE output contract: resolved-target default / explicit ``--out <path>`` /
    ``--out -`` stdout TOML preview. The target write OWNS no-clobber (absent →
    write; present → ``<target>.new`` + ``diverted:true``; ``--force`` →
    overwrite). stdout = machine payload ONLY (envelope JSON or ``--out -`` TOML);
    ALL notes/errors → stderr.
    """
    scope = "repo" if args.repo else "global"

    # 1. Validate inputs BEFORE any emit (reject nonzero + stderr message).
    default_panel = args.default_panel or "full"
    valid_panels = set(seats.merged_panels())
    if default_panel not in valid_panels:
        print(
            f"error: --default-panel {default_panel!r} is not a known panel "
            f"({', '.join(sorted(valid_panels))})",
            file=sys.stderr,
        )
        return 2
    dispatch_seat = args.dispatch_seat or "codex"
    known = set(known_seat_names())
    if dispatch_seat not in known:
        print(
            f"error: --dispatch-seat {dispatch_seat!r} is not a known subprocess seat "
            f"({', '.join(sorted(known))}); [dispatch].seat must be a subprocess WRITE "
            "seat (a panel name or a Task seat is rejected)",
            file=sys.stderr,
        )
        return 2
    avail_union = known | set(seats.task_seats())
    opted_in: set[str] = set()
    for s in (args.add_seat or []):
        if s not in avail_union:
            print(
                f"error: --add-seat {s!r} is not a known seat "
                f"({', '.join(sorted(avail_union))})",
                file=sys.stderr,
            )
            return 2
        opted_in.add(s)
    disabled: set[str] = set()
    for s in (args.disable_seat or []):
        if s not in avail_union:
            print(
                f"error: --disable-seat {s!r} is not a known seat "
                f"({', '.join(sorted(avail_union))})",
                file=sys.stderr,
            )
            return 2
        disabled.add(s)
    contradictory = sorted(opted_in & disabled)
    if contradictory:
        print(
            f"error: seat(s) {', '.join(contradictory)} passed to BOTH --add-seat and "
            "--disable-seat — a seat cannot be enabled and disabled at once",
            file=sys.stderr,
        )
        return 2

    # 2. Detection handling — ABSENT flag (omit available) vs BAD file (error).
    detection: dict | None = None
    if args.detection is not None:
        det_path = Path(os.path.expanduser(args.detection))
        try:
            raw = det_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read --detection file {args.detection!r}: {exc}", file=sys.stderr)
            return 2
        if not raw.strip():
            print(f"error: --detection file {args.detection!r} is empty", file=sys.stderr)
            return 2
        try:
            detection = json.loads(raw)
        except ValueError as exc:
            print(f"error: --detection file {args.detection!r} is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(detection, dict):
            print(f"error: --detection file {args.detection!r} must be a JSON object", file=sys.stderr)
            return 2

    # 3. Build the content.
    notes: list[str] = []
    if scope == "repo":
        try:
            base = config.global_config_path().read_text(encoding="utf-8")
            has_global = True
        except OSError:
            base = ""
            has_global = False
        if has_global:
            try:
                config.tomllib.loads(base)
            except (config.tomllib.TOMLDecodeError, UnicodeDecodeError):
                print(
                    "error: existing global ~/.crew-config.toml does not parse as TOML; "
                    "refusing to seed an unloadable base into .crew/config.toml — fix or "
                    "remove the global file first",
                    file=sys.stderr,
                )
                return 2
            seat_avail: dict[str, bool] = {}
            for s in sorted(opted_in):
                seat_avail[s] = True
            for s in sorted(disabled):
                seat_avail[s] = False
            content, onotes = _apply_repo_overrides(
                base, default_panel=default_panel, dispatch_seat=dispatch_seat,
                seat_avail=seat_avail,
            )
            notes += onotes
            if detection is None:
                notes.append(
                    "detection skipped (no --detection flag) — no per-seat 'available' "
                    "lines auto-emitted; only explicit --add-seat/--disable-seat "
                    "corrections were applied"
                )
        else:
            content = _render_config_template(
                detection, scope="repo", default_panel=default_panel,
                dispatch_seat=dispatch_seat, opted_in=opted_in, disabled=disabled,
            )
            if detection is None:
                notes.append(
                    "detection skipped (no --detection flag) — per-seat 'available' lines "
                    "omitted; availability falls back to the runtime is_available() skip"
                )
    else:
        content = _render_config_template(
            detection, scope="global", default_panel=default_panel,
            dispatch_seat=dispatch_seat, opted_in=opted_in, disabled=disabled,
        )
        if detection is None:
            notes.append(
                "detection skipped (no --detection flag) — per-seat 'available' lines "
                "omitted; availability falls back to the runtime is_available() skip"
            )

    content = content.rstrip("\n")

    # 4. Output contract.
    if args.out == "-":
        # STDOUT PREVIEW: pure TOML, NO envelope, NO file, notes to stderr.
        _scaffold_stderr(notes)
        print(content)
        return 0

    if args.out:
        target = os.path.expanduser(args.out)
    elif scope == "repo":
        target = str(config.repo_config_path())
    else:
        target = str(config.global_config_path())

    _scaffold_stderr(notes)

    # No-clobber write (NOT via _emit) — this function owns the invariant.
    p = Path(target)
    text = content + "\n"
    if args.force or not p.exists():
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(json.dumps({"target": target, "wrote": target, "diverted": False}, ensure_ascii=False))
        return 0

    newp = Path(target + ".new")
    if newp.exists():
        print(
            f"crew scaffold-config: replaced an existing {newp} from a prior diverted run",
            file=sys.stderr,
        )
    if newp.parent and not newp.parent.exists():
        newp.parent.mkdir(parents=True, exist_ok=True)
    newp.write_text(text, encoding="utf-8")
    print(json.dumps({"target": target, "wrote": str(newp), "diverted": True}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multiagent",
        description="Multi-model review engine (subprocess seats only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="Run a review fan-out over subprocess seats.")
    review.add_argument(
        "target",
        nargs="?",
        default="auto",
        help="plan .md path, working-tree, branch, commit:<sha>, a SHA, or auto",
    )
    review.add_argument("--seats", default=None, help="comma-separated subprocess seats (e.g. codex,cursor-auto,cursor-composer)")
    review.add_argument("--json", action="store_true", help="emit a JSON array of results")
    review.add_argument("--base", default="main", help="base ref for branch/auto diffs")
    review.add_argument("--timeout", type=int, default=None, help="per-seat wall-clock timeout (s)")
    review.add_argument(
        "--inline-diff", action="store_true",
        help="embed the diff/plan in the prompt instead of referencing it "
             "(seats fetch it themselves by default — smaller, no ARG_MAX cap)",
    )
    review.add_argument(
        "-o", "--out", default=None,
        help="write results to this file instead of stdout (keeps the call "
             "shell-redirect-free and allowlistable)",
    )
    review.set_defaults(func=cmd_review)

    council = sub.add_parser(
        "council",
        help="Fan a free-form question across subprocess seats (single round).",
    )
    council.add_argument(
        "question",
        nargs="?",
        default=None,
        help="the council question (omit when using -f)",
    )
    council.add_argument(
        "-f", "--file",
        default=None,
        help="read the question from this file instead of the positional string",
    )
    council.add_argument("--seats", default=None, help="comma-separated subprocess seats (e.g. codex,cursor-auto,cursor-composer)")
    council.add_argument("--json", action="store_true", help="emit a JSON array of results")
    council.add_argument("--timeout", type=int, default=None, help="per-seat wall-clock timeout (s)")
    council.add_argument(
        "-o", "--out", default=None,
        help="write results to this file instead of stdout (keeps the call "
             "shell-redirect-free and allowlistable)",
    )
    council.set_defaults(func=cmd_council)

    debate = sub.add_parser(
        "debate",
        help="Scaffold a debate dir (question.md + empty subprocess.json) in one "
             "call — SCAFFOLD-ONLY, never runs subprocess seats internally "
             "(no mkdir/heredoc to approve).",
    )
    debate.add_argument(
        "question",
        nargs="?",
        default=None,
        help="the debate question (omit when using -f)",
    )
    debate.add_argument(
        "-f", "--file",
        default=None,
        help="read the question from this file instead of the positional string",
    )
    debate.add_argument(
        "--slug",
        default=None,
        help="short kebab-case slug for the dir name (default: derived from the question)",
    )
    debate.add_argument(
        "--base-dir",
        default=".crew/debates",
        help="parent dir for debate logs (default: .crew/debates)",
    )
    debate.add_argument(
        "--seats", default=None,
        help="ignored for execution — debate is SCAFFOLD-ONLY and never runs "
             "subprocess seats internally (fan them out per-seat via 'crew run "
             "<seat>'). A non-empty value (e.g. codex) prints a one-line stderr "
             "advisory and still scaffolds; 'none'/'' is the no-op default.",
    )
    debate.add_argument(
        "--timeout", type=int, default=None,
        help="ignored — debate is scaffold-only and runs no seats (compat only)",
    )
    debate.add_argument(
        "--consume", action="store_true",
        help="delete the -f staging file after copying the question into the "
             "debate dir (so no transient question file lingers)",
    )
    debate.set_defaults(func=cmd_debate)

    seats_p = sub.add_parser(
        "seats",
        help="print the resolved seat list (group tokens expanded), one per line "
             "— for per-seat fan-out. Default: the subprocess panel. With "
             "--debate: the FULL debate panel (subprocess AND Claude Task seats).",
    )
    seats_p.add_argument(
        "--seats", default=None,
        help="comma-separated seats / group tokens (e.g. 'cursor'); "
             "default = the default subprocess panel. With --debate, explicit "
             "entries are validated against known subprocess + Task seats and "
             "path-unsafe/unknown names are rejected.",
    )
    seats_p.add_argument(
        "--panel", default=None,
        help="named preset (only used with --debate): resolved via the configured "
             "[panels] roster then the shipped panels (e.g. full/lite/solo/cursor/quick "
             "or a custom [panels] name) into the full debate seat list; an unknown "
             "name falls back to 'full'",
    )
    seats_p.add_argument(
        "--debate", action="store_true",
        help="print the FULL debate panel (subprocess AND Claude Task seats), "
             "applying debate precedence --seats > --panel > "
             "config.debate_panel() ([debate].panel) > config.default_panel() > "
             "built-in 'full'. The engine hook that lets debate.md honor "
             ".crew/config.toml when no --panel/--seats is named.",
    )
    seats_p.add_argument(
        "--json", action="store_true",
        help="with --debate: emit {subprocess_seats, task_seats, task_seat_models} "
             "as JSON (mirrors review-prep's split) instead of one seat per line, so "
             "debate.md reads the roster split without classifying seat names itself.",
    )
    seats_p.set_defaults(func=cmd_seats)

    coll = sub.add_parser(
        "collect",
        help="Collapse the named per-seat <seat>.json result files into ONE markdown "
             "digest (faithful render_panel projection — never a summarizer). Reads "
             "EXACTLY the seats named in --seats; never globs (avoids stale-seat "
             "contamination across reused session dirs).",
    )
    coll.add_argument(
        "--session-id", dest="session_id", default=None,
        help="session id for .crew/reviews/<session-id>/ (default: CLAUDE_SESSION_ID env)",
    )
    coll.add_argument(
        "--seats", dest="seats", default="",
        help="comma-separated resolved seat names: subprocess (codex/agy/cursor-*) "
             "AND the (dot-stripped) Task seats (opus, sonnet, fable). collect reads "
             "EXACTLY <seat>.json for each; a missing seat renders as a SKIPPED "
             "block; stale/foreign files are never read. (collect never globs.)",
    )
    coll.add_argument(
        "-o", "--out", default=None,
        help="write the digest to this file (keeps the call shell-redirect-free)",
    )
    coll.add_argument(
        "--group", action="store_true",
        help="emit the GROUPED/deduped digest (VERDICTS roster + CRITERIA MATRIX + "
             "GROUPED FINDINGS 'M/N' + RAW seats) instead of the faithful "
             "concatenation. No --group = byte-identical to today.",
    )
    coll.add_argument(
        "--full", default=None, metavar="PATH",
        help="with --group, ALSO write the byte-faithful render_panel concatenation "
             "of all named seats to PATH (the recovery artifact / size denominator).",
    )
    coll.add_argument(
        "--report-unparsed", dest="report_unparsed", action="store_true",
        help="print ONLY the seats that ran but did NOT parse into structured "
             "findings (newline-separated) — the haiku-repair candidates. Writes no "
             "digest.",
    )
    coll.set_defaults(func=cmd_collect)

    rs = sub.add_parser(
        "repair-seat",
        help="Write the haiku-reformatted findings from -f/stdin to a SEPARATE "
             "`repaired_output` field of a seat's <seat>.json — NEVER overwriting "
             "the original `output`. Non-destructive: `repaired_output` is "
             "populated ONLY IF it parses (else a kept-original no-op: stderr "
             "note, exit 0; exit 2 = real usage/read error). "
             "Grouping uses `repaired_output` when present; --full/RAW always "
             "render the original `output`.",
    )
    rs.add_argument(
        "--seat", dest="seat", required=True, metavar="PATH",
        help="path to the seat's <seat>.json file to rewrite in place",
    )
    rs.add_argument(
        "-f", "--file", dest="file", default=None,
        help="read the replacement output text from this file (default: stdin)",
    )
    rs.set_defaults(func=cmd_repair_seat)

    ps = sub.add_parser(
        "persist-seat",
        help="Write ONE normalized Task-seat result to .crew/reviews/<id>/"
             "<slug>.json (six-field ProviderResult, `name` = slug). The engine "
             "owns slug derivation, the render-safe shape, stale-file replacement, "
             "and the path echo — no LLM hand-assembly. Task seats only — "
             "subprocess seats persist themselves via `run`. Prints ONLY the "
             "written path (exit 0); exit 2 on usage errors (empty seat, unreadable "
             "-f, placeholder/unresolvable session id).",
    )
    ps.add_argument("seat", help="seat/role name; the slug (dot-stripped) is BOTH "
                                 "the filename and the `name` field")
    ps.add_argument("--session-id", dest="session_id", default=None,
                    help="session id for .crew/reviews/<session-id>/ "
                         "(default: CLAUDE_SESSION_ID env)")
    ps.add_argument("--model", dest="model", required=True,
                    help="the seat's model pin, written verbatim to `model`")
    ps.add_argument("-f", "--file", dest="file", default=None,
                    help="read the seat's output text from this file (default: stdin)")
    ps.add_argument("--failed", dest="failed", action="store_true",
                    help="mark the seat failed: `ok` becomes false")
    ps.add_argument("--error", dest="error", default=None,
                    help="diagnostic recorded in `error` when --failed (default: a "
                         "generic 'Task seat failed' message)")
    ps.add_argument("--elapsed", dest="elapsed", type=float, default=0.0,
                    help="wall-clock seconds recorded in `elapsed` (default: 0.0)")
    ps.set_defaults(func=cmd_persist_seat)

    rp = sub.add_parser(
        "review-prep",
        help="Prepare a review/build/measure-twice fan-out: resolve target + "
             "--panel/--seats into subprocess + task seats, stage the ONE shared "
             "subprocess prompt, and PRINT {prompt_path, subprocess_seats, "
             "task_seats, task_seat_models} as JSON. Runs NOTHING — the per-seat "
             "`crew run` loop + the Task-seat dispatch stay in the command markdown.",
    )
    rp.add_argument(
        "target", nargs="?", default="auto",
        help="plan .md path, working-tree, branch, commit:<sha>, a SHA, or auto "
             "(same target vocabulary as render/review)",
    )
    rp.add_argument("--base", dest="base", default=None,
                    help="base ref for branch/auto diffs (default: main)")
    rp.add_argument(
        "--panel", dest="panel", default=None,
        help="named preset resolved via the configured [panels] roster then "
             "the shipped panels (e.g. full/lite/solo/cursor/quick or a custom [panels] "
             "name; unknown → 'full') into BOTH subprocess and task seats. Default "
             "absent (None): with no --seats either, review-prep resolves the "
             "default_panel (per-repo .crew/config.toml → global ~/.crew-config.toml), "
             "falling back to the built-in `full`. --seats overrides the subprocess "
             "subset; --task-seats overrides the task subset.",
    )
    rp.add_argument(
        "--seats", dest="seats", default=None,
        help="comma-separated unified seat spec (subprocess + task names, group "
             "'cursor' allowed). Sentinel: omitted (None) falls back to --panel; "
             "explicit '' => no subprocess seats (Claude-only lite/solo: "
             "subprocess_seats=[], no subprocess prompt staged); non-empty => "
             "split into subprocess_seats (registry-resolved) + task_seats "
             "(catalog Task seats), unknown names dropped. Both --seats "
             "and --panel omitted => the per-repo config's default_panel "
             "(.crew/config.toml), falling back to the built-in `full`.",
    )
    rp.add_argument(
        "--session-id", dest="session_id", default=None,
        help="session id for the staged prompt path (default: CLAUDE_SESSION_ID "
             "env). Pass the literal id; the engine resolves it, so the command "
             "needs no shell ${…} expansion.",
    )
    rp.add_argument(
        "--mode", dest="mode", default="review", choices=["review", "discuss"],
        help="prompt family for the staged subprocess prompt (default review)",
    )
    rp.add_argument(
        "--inline-diff", action="store_true",
        help="forward to the staging path (parity with `render --stage "
             "--inline-diff`): embed the diff/plan instead of referencing it",
    )
    rp.add_argument(
        "--task-seats", dest="task_seats", default="",
        help="OPAQUE echo: resolved Claude-seat LABELS; echoed verbatim in "
             "task_seats, never resolved/registry-filtered/spawned by the engine",
    )
    rp.set_defaults(func=cmd_review_prep)

    run = sub.add_parser(
        "run",
        help="Run ONE subprocess seat (codex|agy|cursor-*) ad-hoc through the engine.",
    )
    run.add_argument("seat", help="subprocess seat name (codex, agy, or a cursor-* seat)")
    run.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="prompt string (omit when using -f)",
    )
    run.add_argument(
        "-f", "--file",
        default=None,
        help="read the prompt from this file instead of the positional string",
    )
    run.add_argument("-m", "--model", default=None, help="override the seat's model")
    run.add_argument(
        "-s", "--sandbox",
        choices=["read-only", "workspace-write"],
        default="read-only",
        help="sandbox mode for codex (default: read-only); agy maps any value "
             "to its single confined --sandbox mode",
    )
    run.add_argument("--json", action="store_true", help="emit the six-field result as JSON")
    run.add_argument("--timeout", type=int, default=None, help="wall-clock timeout (s)")
    run.add_argument(
        "-o", "--out", default=None,
        help="write output to this file instead of stdout (keeps the call "
             "shell-redirect-free and allowlistable)",
    )
    run.add_argument(
        "--session-id", dest="session_id", default=None,
        help="derive -f and -o from .crew/reviews/<session-id>/ when omitted: "
             "-f defaults to prompt-seat.txt, -o defaults to <seat>.json. "
             "Explicit -f/-o override independently.",
    )
    run.set_defaults(func=cmd_run)

    disp = sub.add_parser(
        "dispatch",
        help="Send ONE subprocess seat (default codex) at the working tree in "
             "WRITE mode (workspace-write): it edits files in place and leaves "
             "changes UNCOMMITTED + UNSTAGED. Emits a 15-field JSON envelope "
             "(seat output + HEAD/staged/branch guards) and prints the resolved "
             "envelope path.",
    )
    disp.add_argument(
        "task",
        nargs="?",
        default=None,
        help="the task to perform (the RAW task, framed internally; omit when "
             "using -f). Exactly one of task or -f.",
    )
    disp.add_argument(
        "-f", "--file",
        default=None,
        help="read the RAW task from this file instead of the positional string "
             "(for multiline/large tasks; avoids ARG_MAX + quoting). XOR positional.",
    )
    disp.add_argument(
        "--seat", dest="seat", default=None,
        help="the subprocess seat to dispatch (default: [dispatch].seat config, "
             "else codex). An explicit --seat bypasses the config availability "
             "filter (the named seat always runs).",
    )
    disp.add_argument("--model", dest="model", default=None, help="override the seat's model")
    disp.add_argument("--timeout", type=int, default=None, help="wall-clock timeout (s)")
    disp.add_argument(
        "--json", action="store_true",
        help="emit the 15-field envelope as JSON (exit 0 even when ok=false)",
    )
    disp.add_argument(
        "-o", "--out", default=None,
        help="write the envelope to this file (else derived from --session-id, "
             "else stdout). Normally OMITTED — the engine derives + prints it.",
    )
    disp.add_argument(
        "--session-id", dest="session_id", default=None,
        help="derive -o to .crew/reviews/<session-id>/dispatch-<seat>.json when "
             "-o is omitted. Pass the literal id; the engine resolves it.",
    )
    disp.set_defaults(func=cmd_dispatch)

    rndr = sub.add_parser(
        "render",
        help="Build ONE seat's prompt (no execution) — the single prompt source "
             "for Task seats, review/discuss, single- or multi-round.",
    )
    rndr.add_argument(
        "target", nargs="?", default="auto",
        help="target spec for review / discuss-over-target (default auto)",
    )
    rndr.add_argument(
        "-q", "--question", default=None,
        help="free-form question for --mode discuss (instead of a target)",
    )
    rndr.add_argument(
        "-f", "--file", default=None,
        help="read a free-form discuss question from this file",
    )
    rndr.add_argument(
        "--mode", choices=["review", "discuss"], default="review",
        help="prompt family: review (rubric + verdict) or discuss (advisory)",
    )
    rndr.add_argument(
        "--seat-role", dest="seat_role", default=None,
        help="label this seat in the prompt (e.g. opus, sonnet, panelist)",
    )
    rndr.add_argument(
        "--run-id", dest="run_id", default=None,
        help="debate run-id; with --round, folds prior rounds in as DATA",
    )
    rndr.add_argument(
        "--round", type=int, default=None,
        help="round number (>=1); a round >1 folds in prior rounds 1..n-1",
    )
    rndr.add_argument(
        "--prior-round", dest="prior_round", default=None,
        help="explicit prior-round text file (alternative to --run-id/--round)",
    )
    rndr.add_argument(
        "--base-dir", dest="base_dir", default=".crew/debates",
        help="parent dir for debate runs (used to resolve --run-id)",
    )
    rndr.add_argument("--base", default="main", help="base ref for branch/auto diffs")
    rndr.add_argument(
        "--inline-diff", action="store_true",
        help="embed the diff/plan content instead of referencing it",
    )
    rndr.add_argument(
        "-o", "--out", default=None,
        help="write the prompt to this file instead of stdout (keeps the call "
             "shell-redirect-free and allowlistable)",
    )
    rndr.add_argument(
        "--session-id", dest="session_id", default=None,
        help="session id for the --stage path (default: CLAUDE_SESSION_ID env). "
             "Pass the literal id; the engine resolves it, so the command needs "
             "no shell ${…} expansion.",
    )
    rndr.add_argument(
        "--stage", action="store_true",
        help="stage the prompt to .crew/reviews/<session-id>/prompt-<seat-role>.txt "
             "— path derived from --seat-role + session id, so no -o and no shell "
             "expansion. Prints the staged path to stdout.",
    )
    rndr.add_argument(
        "--stage-all", dest="stage_all", default=None,
        help="stage one labeled prompt PER comma-listed seat-role into "
             ".crew/reviews/<session-id>/prompt-<role>.txt in a single call; "
             "prints a JSON {role: path} map. The role 'seat' maps to no label "
             "(matching the shared prompt-seat.txt). Mutually exclusive with "
             "--stage/--seat-role/-o.",
    )
    rndr.set_defaults(func=cmd_render)

    doctor = sub.add_parser(
        "doctor",
        help="Probe per-seat provider availability (registry-derived, NON-billable: "
             "codex/agy = which; cursor = one local `agent --version`) and emit a JSON "
             "detection map to stdout. Always JSON; notes/errors go to stderr.",
    )
    doctor.add_argument(
        "--session-id", dest="session_id", default=None,
        help="write the JSON to .crew/reviews/<session-id>/doctor.json (default: "
             "CLAUDE_SESSION_ID env). The JSON is ALWAYS also printed to stdout.",
    )
    doctor.add_argument(
        "-o", "--out", default=None,
        help="write the JSON to this path instead of the session path (~ expanded). "
             "-o wins over the session path; the JSON is still printed to stdout.",
    )
    doctor.set_defaults(func=cmd_doctor)

    probe = sub.add_parser(
        "probe",
        help="BILLABLE — live end-to-end seat smoke test: sends a trivial "
             "canned prompt through each named seat's real CLI and checks for "
             "a marker in the reply. doctor proves installed; probe proves it "
             "works. A BILLABLE warning always prints to stderr before any "
             "provider runs.",
    )
    probe.add_argument(
        "seats", nargs="*", default=[],
        help="subprocess seat name(s) to probe (codex, agy, or cursor-*)",
    )
    probe.add_argument(
        "--all", action="store_true",
        help="probe every registered subprocess seat (known_seat_names())",
    )
    probe.add_argument("--timeout", type=int, default=None, help="wall-clock timeout (s)")
    probe.add_argument(
        "--session-id", dest="session_id", default=None,
        help="write the JSON to .crew/reviews/<session-id>/probe.json (default: "
             "CLAUDE_SESSION_ID env). The JSON is ALWAYS also printed to stdout.",
    )
    probe.add_argument(
        "-o", "--out", default=None,
        help="write the JSON to this path instead of the session path (~ expanded). "
             "-o wins over the session path; the JSON is still printed to stdout.",
    )
    probe.set_defaults(func=cmd_probe)

    sc = sub.add_parser(
        "scaffold-config",
        help="Render a COMMENTED starter ~/.crew-config.toml (or .crew/config.toml "
             "with --repo, seeded from the global) with cost-safe defaults + honest "
             "`available` flags. Owns no-clobber (present target → <target>.new). "
             "ONE output contract: resolved target / --out <path> / --out - (TOML preview).",
    )
    sc.add_argument(
        "--repo", action="store_true",
        help="scaffold the per-repo .crew/config.toml (seeded VERBATIM from the global "
             "if one exists) instead of the global ~/.crew-config.toml",
    )
    sc.add_argument(
        "--force", action="store_true",
        help="overwrite an existing target directly (used only after explicit confirm); "
             "without it, an existing target diverts to <target>.new",
    )
    sc.add_argument(
        "-o", "--out", default=None,
        help="write to this path instead of the resolved target (~ expanded); "
             "'-' (a single dash) prints the rendered TOML to stdout (no file, no envelope)",
    )
    sc.add_argument(
        "--default-panel", dest="default_panel", default=None,
        help="default_panel value (validated vs the shipped panels ∪ configured [panels]; "
             "default full)",
    )
    sc.add_argument(
        "--dispatch-seat", dest="dispatch_seat", default=None,
        help="[dispatch].seat value (validated vs known_seat_names() — subprocess WRITE "
             "seats only; default codex)",
    )
    sc.add_argument(
        "--add-seat", dest="add_seat", action="append", default=None,
        help="opt a seat IN (omit its available=false); repeatable. Validated vs "
             "known_seat_names() ∪ the catalog Task seats (task seats are correctable).",
    )
    sc.add_argument(
        "--disable-seat", dest="disable_seat", action="append", default=None,
        help="opt a seat OUT (emit available=false); repeatable. Validated vs "
             "known_seat_names() ∪ the catalog Task seats.",
    )
    sc.add_argument(
        "--detection", dest="detection", default=None,
        help="a doctor.json detection file. ABSENT → omit per-seat available lines + a "
             "stderr 'detection skipped' note; GIVEN-but-missing/empty/malformed → error.",
    )
    sc.set_defaults(func=cmd_scaffold_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
