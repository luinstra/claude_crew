#!/usr/bin/env python3
"""CLI entry point for the multi-model review engine (SUBPROCESS seats only).

Invocation mechanism (Step 1.6 — mechanism **C**, the bare-script path):
the command markdown references this as
``${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py``. A sys.path guard at the
top inserts the package's PARENT dir (``.../scripts``) onto ``sys.path`` so the
package-relative imports (``from multiagent...``) resolve when run as a bare
script. ``python -m multiagent.cli`` also works (the guard is a harmless no-op
when the parent is already importable).

Subcommands: ``review`` (fan-out over a resolved target), ``council`` (fan-out
of a free-form question — the engine half of /crew:debate's crew-native
council), ``debate`` (scaffold a debate dir + council in one call), ``run`` (one
seat ad-hoc), and ``render`` (build ONE seat's prompt, no execution).

The engine EXECUTES only subprocess seats (codex/cursor-*, plus opt-in agy), but ``render`` BUILDS the
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

from multiagent import prompts, render, rounds, targets
from multiagent.providers import (
    ProviderResult,
    available_seats,
    get_provider,
    known_seat_names,
)

# The default subprocess panel. codex (OpenAI) + three Cursor model-seats for
# cross-model diversity at one Cursor price. `cursor-gpt` is NOT a default —
# codex already covers the GPT lineage, so defaulting it too is redundant model
# coverage at extra token cost; it stays registered and opt-in (`--seats cursor-gpt`
# / `--panel cursor`). agy is likewise registered-but-opt-in (`--seats agy`).
# Override the whole list with CREW_MA_SEATS. Any name here must be a registered
# subprocess seat (see providers/__init__._build_registry); unknown names dropped.
_DEFAULT_SUBPROCESS_PANEL = (
    "codex", "cursor-gemini", "cursor-glm", "cursor-composer",
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
    env = os.environ.get("CREW_MA_TIMEOUT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return 600


def _codex_model() -> str | None:
    return os.environ.get("CREW_MA_CODEX_MODEL") or None


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
    """
    role = re.sub(r"[^A-Za-z0-9_-]", "", (seat_role or "").strip()) or "seat"
    seg = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    base = Path(_REVIEWS_BASE) / seg if seg else Path(_REVIEWS_BASE)
    return str(base / f"prompt-{role}.txt")


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
    allowlistable ``python cli.py …`` command: callers no longer wrap it in
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
    """Scaffold a debate dir + run the subprocess-seat council in ONE allowlistable call.

    Folds the steps /crew:debate used to do in the shell — `mkdir` the dir, a
    heredoc to write the question, a redirect to capture results — into a single
    ``python cli.py debate …`` invocation that matches the allowlist rule (so it
    never prompts). Creates ``<base-dir>/<timestamp>-<slug>/``, writes
    ``question.md`` + ``subprocess.json`` into it, and prints a JSON summary
    (including ``dir``) so the orchestrator knows where to drop the Claude-seat
    outputs + synthesis. The opus/sonnet Task seats are still spawned by the
    orchestrator — the engine cannot spawn in-session Claude agents.
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

    # A Claude-only panel (e.g. --panel lite/solo) has NO subprocess seat but
    # still wants the dir + question.md scaffolded so the orchestrator can run
    # the opus/sonnet Task seats and log there. `--seats none` (or "") means
    # exactly that — distinct from OMITTING --seats (which defaults to the
    # subprocess panel: codex,cursor-gemini,cursor-glm,cursor-composer).
    if args.seats is not None and args.seats.strip().lower() in ("", "none"):
        seats: list[str] = []
    else:
        seats = _resolve_seats(args.seats)

    timeout = _resolve_timeout(args.timeout)
    results = _fan_out(seats, prompts.council(question), timeout) if seats else []
    try:
        (debate_dir / "subprocess.json").write_text(
            render.render_json(results), encoding="utf-8"
        )
    except OSError as exc:
        print(f"error: cannot write subprocess.json to {debate_dir}: {exc}", file=sys.stderr)
        return 2

    # The orchestrator reads this to find the dir + see how the subprocess seats
    # fared, then spawns the opus/sonnet Task seats and writes the synthesis here.
    print(json.dumps({
        "dir": str(debate_dir),
        "question_file": str(debate_dir / "question.md"),
        "subprocess_results": str(debate_dir / "subprocess.json"),
        "seats": [
            {"name": r.name, "ok": r.ok, "elapsed": round(r.elapsed, 1)}
            for r in results
        ],
    }, indent=2))
    return _all_failed_exit(results)


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
    """
    seat = args.seat
    if seat not in known_seat_names():
        known = ", ".join(known_seat_names())
        print(
            f"error: unknown or non-subprocess seat {seat!r}; "
            f"valid subprocess seats: {known}",
            file=sys.stderr,
        )
        return 2

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
        _emit(json.dumps(result.to_dict()), args.out)
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


def cmd_render(args: argparse.Namespace) -> int:
    """Build ONE seat's prompt and emit it — no execution.

    This is the single prompt source the orchestrator uses to obtain a Claude
    Task seat's prompt (so it is byte-identical to what a subprocess seat would
    receive for the same target/mode/round — every prompt, every seat, every
    round flows through ``prompts.build_prompt``/``council`` and nowhere else).
    It builds for either a git/plan TARGET or, in discuss mode, a free-form
    QUESTION (``-q``/``-f``). It never spawns a seat.
    """
    try:
        prior = _render_prior(args)
    except rounds.RoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

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
        return 2
    # A free-form question and an explicit positional target are mutually
    # exclusive — passing both is ambiguous (the target would be silently
    # ignored). The default target is "auto"; only a NON-default target conflicts.
    if (args.question is not None or args.file) and args.target != "auto":
        print(
            "error: provide either a positional <target> OR a free-form question "
            "(-q/--question, -f/--file), not both",
            file=sys.stderr,
        )
        return 2
    question: str | None = None
    if args.file:
        try:
            question = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read question file {args.file!r}: {exc}", file=sys.stderr)
            return 2
    elif args.question is not None:
        question = args.question

    if question is not None:
        if args.mode != "discuss":
            print(
                "error: a free-form question (-q/-f) is only valid with --mode discuss",
                file=sys.stderr,
            )
            return 2
        text = prompts.council(question, seat_role=args.seat_role, prior_round=prior)
    else:
        try:
            target = targets.resolve(args.target, base=args.base)
        except targets.TargetError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = prompts.build_prompt(
            target,
            seat_role=args.seat_role,
            mode=args.mode,
            prior_round=prior,
            inline=args.inline_diff,
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


def cmd_seats(args: argparse.Namespace) -> int:
    """Print the resolved subprocess seat list — one per line.

    Resolves ``--seats`` (group tokens like ``cursor`` expanded, filtered to the
    registry, de-duped) the SAME way ``review``/``council`` do, but prints the
    concrete names instead of running them. This lets an orchestrator that fans
    out PER-SEAT — one ``run <seat>`` call each, so every subprocess seat is its
    own visible shell — obtain the expanded list without hardcoding the cursor
    group, keeping the registry the single source of truth.
    """
    for s in _resolve_seats(args.seats):
        print(s)
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
    review.add_argument("--seats", default=None, help="comma-separated subprocess seats (e.g. codex,cursor-gemini,cursor-glm,cursor-composer)")
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
    council.add_argument("--seats", default=None, help="comma-separated subprocess seats (e.g. codex,cursor-gemini,cursor-glm,cursor-composer)")
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
        help="Scaffold a debate dir + run the subprocess-seat council in one call "
             "(no mkdir/heredoc/redirect to approve).",
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
        help="comma-separated subprocess seats (e.g. codex,cursor-gemini,cursor-glm,cursor-composer); 'none' scaffolds the "
             "dir but runs no subprocess seats (a Claude-only panel)",
    )
    debate.add_argument("--timeout", type=int, default=None, help="per-seat wall-clock timeout (s)")
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
    seats_p.set_defaults(func=cmd_seats)

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
    rndr.set_defaults(func=cmd_render)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
