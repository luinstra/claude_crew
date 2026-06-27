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

The engine EXECUTES only subprocess seats (codex/agy/cursor-*), but ``render`` BUILDS the
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

from multiagent import config, prompts, render, rounds, seats, targets
from multiagent.providers import (
    ProviderResult,
    available_seats,
    get_provider,
    known_seat_names,
)

# The default subprocess panel. codex (OpenAI) + agy (Gemini via Antigravity,
# flat-rate) + two Cursor model-seats for cross-model diversity. `cursor-gpt` is
# NOT a default — codex already covers the GPT lineage, so defaulting it too is
# redundant model coverage at extra token cost; it stays registered and opt-in
# (`--seats cursor-gpt` / `--panel cursor`). `cursor-gemini` is likewise
# registered-but-opt-in (`--seats cursor-gemini` / `--panel cursor`) — agy covers
# the Gemini lineage flat-rate, so the metered Cursor gemini seat isn't defaulted.
# `cursor-glm` is ALSO registered-but-opt-in (`--seats cursor-glm` / `--panel
# cursor`): glm-max draws on Cursor's shared premium MAX allotment, so it's pulled
# from the default to keep the default off the premium bucket. `cursor-auto`
# (Cursor's auto model routing) is defaulted in its place — auto bills from
# Cursor's cheap/dedicated bucket (like composer), so no default crew panel touches
# the premium allotment.
# Override the whole list with CREW_MA_SEATS. Any name here must be a registered
# subprocess seat (see providers/__init__._build_registry); unknown names dropped.
_DEFAULT_SUBPROCESS_PANEL = (
    "codex", "agy", "cursor-auto", "cursor-composer",
)


def _default_subprocess_seats() -> list[str]:
    """Subprocess-seat default for the engine.

    The engine only knows subprocess seats (codex/agy/cursor-*; opus/sonnet Task
    seats are owned by the orchestrator). The allowlist is REGISTRY-DERIVED:
    ``known_seat_names()`` returns every registered subprocess seat, so adding a
    seat to the registry makes it usable here automatically — no hardcoded list
    to keep in sync. ``CREW_MA_SEATS`` overrides the default panel.

    Safe BECAUSE a failed seat NEVER sinks the panel: any seat failure (nonzero
    exit, hang/timeout, empty output, auth/error banner, unavailable) degrades to
    an ``ok=False`` result while the other seats still return (see ``cmd_review``).
    """
    raw = os.environ.get("CREW_MA_SEATS", ",".join(_DEFAULT_SUBPROCESS_PANEL))
    # Expand group tokens (e.g. `cursor`) BEFORE the known-seat filter, so
    # CREW_MA_SEATS=cursor behaves like the CLI `--seats cursor` rather than
    # silently dropping the unknown bare token and falling back to the default.
    names = _expand_seat_groups([n.strip() for n in raw.split(",") if n.strip()])
    known = set(known_seat_names())
    subprocess_only = [n for n in names if n in known]
    return subprocess_only or [s for s in _DEFAULT_SUBPROCESS_PANEL if s in known]


def _resolve_timeout(arg: int | None) -> int:
    # Default per-seat wall-clock ceiling. Reference mode (the default) makes the
    # seat do its own fetch + scoped read, which is more work than reviewing an
    # inlined diff — a thorough seat (codex) can legitimately need several
    # minutes on a large review. We'd rather give a real review room than
    # silently drop a default seat to a too-short timeout, so the floor is
    # generous (10 min). The reference prompt keeps the seat SCOPED to the diff
    # so normal reviews finish well under this; genuinely large reviews still
    # get the headroom. Override with --timeout or CREW_MA_TIMEOUT (either
    # direction) for faster interactive loops or bigger jobs.
    if arg is not None:
        return arg
    # Precedence: --timeout arg (above) > .crew/config.toml [tuning].timeout >
    # CREW_MA_TIMEOUT env (legacy) > built-in 600s floor.
    cfg = config.default_timeout()
    if cfg is not None:
        return cfg
    env = os.environ.get("CREW_MA_TIMEOUT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return 600


def _codex_model() -> str | None:
    # Precedence: .crew/config.toml [seats.codex].model > CREW_MA_CODEX_MODEL env
    # (legacy) > None (codex's own default). An explicit CLI --model is applied
    # ABOVE this at the call sites (cmd_run).
    return config.seat_model("codex") or os.environ.get("CREW_MA_CODEX_MODEL") or None


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
    model = _codex_model() if name == "codex" else None
    return provider.run(prompt, model=model, timeout=timeout)


def _expand_seat_groups(names: list[str]) -> list[str]:
    """Expand group tokens to seat names. ``cursor`` -> every registered
    ``cursor-*`` seat (registry-derived, so it grows automatically as cursor
    models are added to CURSOR_SEATS). Non-group names pass through unchanged.
    """
    cursor_seats = [n for n in known_seat_names() if n.startswith("cursor-")]
    out: list[str] = []
    for n in names:
        if n == "cursor":
            out.extend(cursor_seats)
        else:
            out.append(n)
    return out


def _resolve_seats(seats_arg: str | None) -> list[str]:
    """Resolve the comma-separated --seats arg to subprocess seats only.

    Accepts the ``cursor`` group token (expands to all cursor-* seats), so
    ``--seats cursor`` runs the whole Cursor panel and ``--seats cursor,codex``
    adds codex.
    """
    seats = (
        [s.strip() for s in seats_arg.split(",") if s.strip()]
        if seats_arg
        else _default_subprocess_seats()
    )
    seats = _expand_seat_groups(seats)
    # Engine drives subprocess seats only — the allowlist is the registry.
    # Filter to known seats, de-duplicating while preserving order.
    known = set(known_seat_names())
    seen: set[str] = set()
    return [s for s in seats if s in known and not (s in seen or seen.add(s))]


def _resolve_debate_panel_name(panel_arg: str | None) -> str:
    """Effective DEBATE panel NAME when no explicit ``--seats`` is given.

    Debate precedence (differs from review-prep, which has NO debate tier):

        explicit ``--panel`` > ``config.debate_panel()`` > ``config.default_panel()``
        > built-in ``full``

    ``config.debate_panel()`` reads ``[debate].panel`` and ``config.default_panel()``
    reads ``default_panel`` — both validate against ``seats.PANEL_PRESETS`` and
    return ``None`` on a missing/unknown value, so the returned name is ALWAYS a
    known preset key (``PANEL_PRESETS[name]`` can never ``KeyError``).
    """
    if panel_arg is not None:
        return panel_arg
    return config.debate_panel() or config.default_panel() or "full"


def _resolve_debate_seats(panel_arg: str | None, seats_arg: str | None) -> list[str]:
    """Resolve the FULL debate panel seat list — subprocess AND Claude Task seats.

    Unlike ``_resolve_seats`` (subprocess-only, registry-filtered), this KEEPS the
    Claude Task seats (opus/sonnet/opus-4.6) so the debate orchestrator can split
    the resolved panel into its per-seat subprocess fan-out and its Task dispatch.
    Group tokens (``cursor``) are expanded; order preserved, de-duplicated.

    Precedence: explicit ``--seats`` (wins) > explicit ``--panel`` >
    ``config.debate_panel()`` > ``config.default_panel()`` > built-in ``full``.
    """
    if seats_arg is not None:
        names = [s.strip() for s in seats_arg.split(",") if s.strip()]
    else:
        names = list(seats.PANEL_PRESETS[_resolve_debate_panel_name(panel_arg)])
    names = _expand_seat_groups(names)
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
    ``.``-bearing role like ``opus-4.6`` stages AND is read as ``prompt-opus-46.txt``).
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
    timeout floor, ARG_MAX guard, and ``CREW_MA_*`` env all apply unchanged.
    Valid seats are whatever the registry holds (codex, agy, cursor-*).

    With a TRUTHY, non-blank ``--session-id <id>``, ``-f`` and ``-o`` are DERIVED
    from ``.crew/reviews/<id>/`` when omitted: ``-f`` defaults to the shared
    ``prompt-seat.txt`` and ``-o`` to ``<seat>.json``. Explicit ``-f``/``-o``
    (and a positional prompt, for input) override their derivation independently,
    so debate's explicit paths and ad-hoc stdout use are untouched.
    """
    seat = args.seat
    if seat in seats.TASK_SEAT_NAMES:
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

    provider = get_provider(seat)
    avail, diag = provider.is_available()
    if not avail:
        result = ProviderResult(
            name=seat, model=None, ok=False, output="",
            error=f"skipped: {diag}", elapsed=0.0,
        )
    else:
        # Same model resolution as the review path: --model overrides the
        # CREW_MA_* default; agy resolves its own default internally.
        if args.model:
            model = args.model
        elif seat == "codex":
            model = _codex_model()
        else:
            model = None
        timeout = _resolve_timeout(args.timeout)
        result = provider.run(
            prompt, sandbox=args.sandbox, model=model, timeout=timeout,
        )

    if args.json:
        _emit(json.dumps(result.to_dict(), ensure_ascii=False), args.out)
        return 0

    if not result.ok:
        print(result.error or "unknown error", file=sys.stderr)
        return 1
    _emit(result.output, args.out)
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
        for s in _resolve_debate_seats(args.panel, args.seats):
            print(s)
        return 0
    for s in _resolve_seats(args.seats):
        print(s)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """Collapse the named per-seat ``<seat>.json`` result files into ONE markdown
    digest (a FAITHFUL ``render_panel`` projection — never a summarizer/voter).

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

    text = render.render_panel(results)
    _emit(text, args.out)
    # With -o the digest went to a file, so stdout carries ONLY the path
    # (mirrors --stage's path-only stdout contract; keeps the call allowlist-safe
    # and the digest off stdout). Print args.out VERBATIM — the path the caller
    # passed is the path to echo.
    if args.out:
        print(args.out)
    return 0


def cmd_review_prep(args: argparse.Namespace) -> int:
    """Prepare a review/build/measure-twice fan-out and PRINT a JSON contract —
    it runs NOTHING.

    Consolidates the deterministic PREP the command markdown used to do inline
    across three engine calls (``seats`` + ``render --mode review --stage`` + the
    prose seat-splitting): (a) resolve the target via the SAME ``targets.resolve``
    path ``render``/``review`` use; (b) resolve ``--panel``/``--seats`` into the
    SUBPROCESS seats (through the registry) AND the TASK seats (the Claude voices,
    via ``seats.PANEL_PRESETS`` + ``seats.TASK_SEAT_NAMES``); (c) build the
    ``task_seat_models`` map (``seats.MODEL_OVERRIDES`` is the SINGLE source of
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
    loop into ``_fan_out`` here is the EXACT regression Phase-2 reversed — it
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

    # 2. --panel/--seats → unified source name list, then split into subprocess
    #    vs task seats. Sentinel mechanics (--seats default is None, --panel
    #    default is None/absent):
    #      * --seats <given> (incl. "")  -> seats_given; classify ITS names
    #      * --seats omitted + --panel P -> classify the PANEL_PRESETS[P] names
    #      * BOTH omitted                -> the per-repo config's default_panel
    #                                       (.crew/config.toml) → built-in "full"
    #                                       (see the default-panel fallback below)
    #    A name classifies as exactly one of: a registry subprocess seat
    #    (-> subprocess_seats, via _resolve_seats), a seats.TASK_SEAT_NAMES member
    #    (-> task_seats), or NEITHER (silently DROPPED — matching _resolve_seats's
    #    existing drop behavior; never loud-error).
    seats_given = args.seats is not None
    source_names = (
        [s.strip() for s in args.seats.split(",") if s.strip()]
        if seats_given else []
    )
    preset_names = (
        seats.PANEL_PRESETS[args.panel] if args.panel is not None else None
    )

    # Default-panel fallback: when the user named NEITHER a panel NOR seats, seed
    # the EFFECTIVE preset from .crew/config.toml's default_panel (→ "full" if
    # unset/invalid). This drives BOTH both-omitted branches below — the
    # subprocess split (2a) AND the task split (2b) — via the same `preset_names`
    # the explicit --panel path uses, so the default review keeps its opus/sonnet
    # Claude voices, not just its codex/agy subprocess seats. (An explicit
    # --seats "" or --panel still wins; this only fills the truly-omitted case.)
    if args.panel is None and args.seats is None:
        default_name = config.default_panel() or "full"
        preset_names = seats.PANEL_PRESETS[default_name]

    # 2a. Subprocess seats WITH the empty-guard (the lite/solo fix): _resolve_seats
    #     ("") returns the DEFAULT panel, so an explicit-empty/omitted source MUST
    #     short-circuit to []. Only a non-empty source is resolved through the
    #     registry (reusing _expand_seat_groups / known_seat_names, so the `cursor`
    #     group token + registry filter behave exactly as `crew seats`). SUBPROCESS
    #     SEATS ONLY — task names + unknowns are dropped by the registry filter.
    if seats_given:
        subprocess_seats = _resolve_seats(",".join(source_names)) if source_names else []
    elif preset_names is not None:
        subprocess_seats = _resolve_seats(",".join(preset_names))
    else:
        subprocess_seats = []

    # 2b. Task seats. Precedence: explicit --task-seats override > task names in an
    #     explicit --seats > the --panel preset's task names > []. NEVER resolved
    #     through the subprocess registry, NEVER spawned/run (opaque-echo contract).
    explicit_task_seats = [
        s.strip() for s in (args.task_seats or "").split(",") if s.strip()
    ]
    if explicit_task_seats:
        task_seats = explicit_task_seats
    else:
        seats_task_names = [n for n in source_names if n in seats.TASK_SEAT_NAMES]
        if seats_given and seats_task_names:
            task_seats = seats_task_names
        elif preset_names is not None:
            task_seats = [n for n in preset_names if n in seats.TASK_SEAT_NAMES]
        else:
            task_seats = []

    # 2c. task_seat_models: the per-seat model pin for each Task seat. MODEL_OVERRIDES
    #     is the SINGLE source — a seat not in the map pins its own name. The
    #     orchestrator reads `model` FROM here; it never hardcodes a pin.
    task_seat_models = {n: seats.MODEL_OVERRIDES.get(n, n) for n in task_seats}

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
        help="print the resolved subprocess seat list (group tokens expanded), "
             "one per line — for per-seat fan-out",
    )
    seats_p.add_argument(
        "--seats", default=None,
        help="comma-separated seats / group tokens (e.g. 'cursor'); "
             "default = the default subprocess panel",
    )
    seats_p.add_argument(
        "--panel", default=None, choices=["full", "lite", "solo", "cursor"],
        help="named preset (only used with --debate): resolved via "
             "seats.PANEL_PRESETS into the full debate seat list",
    )
    seats_p.add_argument(
        "--debate", action="store_true",
        help="print the FULL debate panel (subprocess AND Claude Task seats), "
             "applying debate precedence --seats > --panel > "
             "config.debate_panel() ([debate].panel) > config.default_panel() > "
             "built-in 'full'. The engine hook that lets debate.md honor "
             ".crew/config.toml when no --panel/--seats is named.",
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
        help="comma-separated resolved subprocess-seat names. collect reads EXACTLY "
             "<seat>.json for each; a missing seat renders as a SKIPPED block; "
             "stale/foreign files are never read. (collect never globs.)",
    )
    coll.add_argument(
        "-o", "--out", default=None,
        help="write the digest to this file (keeps the call shell-redirect-free)",
    )
    coll.set_defaults(func=cmd_collect)

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
        choices=["full", "lite", "solo", "cursor"],
        help="named preset resolved via seats.PANEL_PRESETS into BOTH subprocess "
             "and task seats. Default absent (None): with no --seats either, "
             "review-prep resolves the per-repo config's default_panel "
             "(.crew/config.toml), falling back to the built-in `full`. --seats "
             "overrides the subprocess subset; --task-seats overrides the task subset.",
    )
    rp.add_argument(
        "--seats", dest="seats", default=None,
        help="comma-separated unified seat spec (subprocess + task names, group "
             "'cursor' allowed). Sentinel: omitted (None) falls back to --panel; "
             "explicit '' => no subprocess seats (Claude-only lite/solo: "
             "subprocess_seats=[], no subprocess prompt staged); non-empty => "
             "split into subprocess_seats (registry-resolved) + task_seats "
             "(seats.TASK_SEAT_NAMES members), unknown names dropped. Both --seats "
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
