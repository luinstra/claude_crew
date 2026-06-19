#!/usr/bin/env python3
"""CLI entry point for the multi-model review engine (SUBPROCESS seats only).

Invocation mechanism (Step 1.6 — mechanism **C**, the bare-script path):
the command markdown references this as
``${CLAUDE_PLUGIN_ROOT}/scripts/multiagent/cli.py``. A sys.path guard at the
top inserts the package's PARENT dir (``.../scripts``) onto ``sys.path`` so the
package-relative imports (``from multiagent...``) resolve when run as a bare
script. ``python -m multiagent.cli`` also works (the guard is a harmless no-op
when the parent is already importable).

Only a ``review`` subcommand exists. There is NO ``debate`` subcommand — native
debate is CUT; /crew:debate is a command-level shim to octopus, not an engine
code path. Task seats (opus/sonnet) are added by the orchestrator, not here.
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
import os

from multiagent import prompts, render, targets
from multiagent.providers import ProviderResult, available_seats


def _default_subprocess_seats() -> list[str]:
    """Subprocess-seat default for the engine.

    The engine only knows subprocess seats. Default to ``codex,agy`` (agy is a
    DEFAULT seat — the full default panel is codex + agy + opus + sonnet, with
    opus/sonnet added by the orchestrator). ``CREW_MA_SEATS`` may name
    subprocess seats; non-subprocess names (opus/sonnet) are ignored here (the
    orchestrator owns Task seats).

    Making agy a default seat is safe BECAUSE a failed seat NEVER sinks the
    panel: any seat failure (nonzero exit, hang/timeout, empty output, agy
    auth/error banner, unavailable) degrades to an ``ok=False`` result while the
    other seats still return (see ``cmd_review``).
    """
    raw = os.environ.get("CREW_MA_SEATS", "codex,agy")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    subprocess_only = [n for n in names if n in ("codex", "agy")]
    return subprocess_only or ["codex", "agy"]


def _resolve_timeout(arg: int | None) -> int:
    if arg is not None:
        return arg
    env = os.environ.get("CREW_MA_TIMEOUT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return 300


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


def cmd_review(args: argparse.Namespace) -> int:
    try:
        target = targets.resolve(args.target, base=args.base)
    except targets.TargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prompt = prompts.build_prompt(target)

    seats = (
        [s.strip() for s in args.seats.split(",") if s.strip()]
        if args.seats
        else _default_subprocess_seats()
    )
    # Engine drives subprocess seats only.
    seats = [s for s in seats if s in ("codex", "agy")]
    if not seats:
        print("error: no subprocess seats requested", file=sys.stderr)
        return 2

    timeout = _resolve_timeout(args.timeout)

    # Pool sized to absorb one hung thread (>= number of seats) so a hung agy
    # thread cannot block codex's collected result.
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

    # Stable seat order (as requested).
    results = [results_by_name[name] for name in seats]

    if args.json:
        print(render.render_json(results))
    else:
        print(f"TARGET: {target.descriptor}")
        for note in target.notes:
            print(f"NOTE: {note}")
        print()
        print(render.render_panel(results))

    # Never-choke guarantee: a partial panel (>=1 ok seat) is a success. ONLY
    # when EVERY seat failed/was skipped do we surface a clear all-failed signal
    # (nonzero exit + an stderr summary) — still a clean list of all-failed
    # results above, never a traceback.
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
    review.add_argument("--seats", default=None, help="comma-separated subprocess seats (codex,agy)")
    review.add_argument("--json", action="store_true", help="emit a JSON array of results")
    review.add_argument("--base", default="main", help="base ref for branch/auto diffs")
    review.add_argument("--timeout", type=int, default=None, help="per-seat wall-clock timeout (s)")
    review.set_defaults(func=cmd_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
