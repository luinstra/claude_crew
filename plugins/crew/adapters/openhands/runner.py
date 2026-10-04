"""Dedicated process entry point; configure SDK persistence before importing it."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tomllib
from pathlib import Path


def load_settings(path: Path) -> dict:
    settings = tomllib.loads(path.read_text())
    allowed = {"max_concurrency", "max_iterations", "support_model", "models"}
    if set(settings) - allowed or not {
        "max_concurrency",
        "support_model",
        "models",
    } <= set(settings):
        raise ValueError(
            "SDK settings require max_concurrency, support_model and models"
        )
    for field in ("max_concurrency", "max_iterations"):
        value = settings.get(field, 50)
        if type(value) is not int or value < 1:
            raise ValueError(f"{field} must be a positive integer")
    routes = settings["models"]
    if not isinstance(routes, dict) or settings["support_model"] not in routes:
        raise ValueError("support_model must name an explicitly configured model route")
    for model, route in routes.items():
        if (
            not isinstance(model, str)
            or len(model.split("/", 1)) != 2
            or not all(model.split("/", 1))
            or any(c.isspace() for c in model)
        ):
            raise ValueError("Every model needs its exact provider/model ID")
        if not isinstance(route, dict) or set(route) - {
            "api_key_env",
            "base_url",
            "max_input_tokens",
            "max_output_tokens",
        }:
            raise ValueError(f"Unsupported settings for {model}")
        env = route.get("api_key_env")
        if not isinstance(env, str) or not env or not os.environ.get(env):
            raise ValueError(
                f"An explicit populated api_key_env is required for {model}"
            )
        for field in ("max_input_tokens", "max_output_tokens"):
            if field in route and (type(route[field]) is not int or route[field] < 1):
                raise ValueError(f"{field} must be a positive integer")
        if "base_url" in route and (
            not isinstance(route["base_url"], str)
            or not route["base_url"].startswith(("https://", "http://"))
        ):
            raise ValueError("base_url must be an explicit HTTP(S) endpoint")
    return settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Execute Crew actions through native OpenHands SDK agents"
    )
    result.add_argument("--workspace", type=Path, required=True)
    result.add_argument(
        "--config",
        type=Path,
        required=True,
        help="SDK routes TOML; keys are referenced by environment variable name",
    )
    result.add_argument("--session-id", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("review", "debate"):
        command = commands.add_parser(name)
        command.add_argument("target")
        selection = command.add_mutually_exclusive_group()
        selection.add_argument("--seats")
        selection.add_argument("--panel")
        if name == "debate":
            command.add_argument("--rounds", type=int, default=1)
    for name in ("measure-twice", "build"):
        command = commands.add_parser(name)
        command.add_argument(
            "--arguments",
            required=True,
            help="Exact Crew arguments, including a requirements document for planning",
        )
    resume = commands.add_parser("resume")
    resume.add_argument(
        "--kind", choices=("review", "debate", "measure-twice", "build"), required=True
    )
    resume.add_argument(
        "--ref", type=Path, required=True, help="JSON ref from the previous result"
    )
    return result


async def execute(args: argparse.Namespace, settings: dict, root: Path) -> dict:
    from bridge import CrewBridge
    from multiagent import build_workflow as build
    from multiagent import measure_twice as measure
    from multiagent import review_workflow as review
    from openhands.sdk import LLM
    from runtime import OpenHandsRuntime

    models = {}
    for model, route in settings["models"].items():
        options = {key: value for key, value in route.items() if key != "api_key_env"}
        models[model] = LLM(
            model=model, api_key=os.environ[route["api_key_env"]], **options
        )
    runtime = OpenHandsRuntime(
        root / "actions",
        models,
        max_concurrency=settings["max_concurrency"],
        max_iterations=settings.get("max_iterations", 50),
    )
    bridge = CrewBridge(runtime, support_model=settings["support_model"])
    kind = args.kind if args.command == "resume" else args.command
    if args.command == "resume":
        value = json.loads(args.ref.read_text())
        parse = {
            "review": review.parse_review_ref,
            "debate": review.parse_review_ref,
            "measure-twice": measure.parse_measure_ref,
            "build": build.parse_build_ref,
        }[kind]
        ref = parse(value)
        from multiagent.review_runs import session_segment

        if ref.session_segment != session_segment(args.session_id):
            raise ValueError("Resume reference belongs to another session")
        next_step = {
            "review": review.next_review,
            "debate": review.next_review,
            "measure-twice": measure.next_measure_twice,
            "build": build.next_build,
        }[kind]
        step = next_step(ref)
    elif kind == "review":
        step = review.start_review(
            review.ReviewRequest(
                args.target,
                panel=args.panel,
                seats=args.seats,
                session_id=args.session_id,
            )
        )
    elif kind == "debate":
        step = review.start_debate(
            review.DebateRequest(
                args.target,
                panel=args.panel,
                seats=args.seats,
                session_id=args.session_id,
                rounds=args.rounds,
            )
        )
    elif kind == "measure-twice":
        step = measure.start_measure_twice(
            measure.MeasureRequest(args.arguments, args.session_id)
        )
    else:
        step = build.start_build(build.BuildRequest(args.arguments, args.session_id))
    if kind in {"review", "debate"}:
        result = await bridge.run_review(step, discuss=kind == "debate")
        return review.review_step_to_dict(result)
    if kind == "measure-twice":
        return measure.step_to_dict(await bridge.run_measure(step))
    return build.step_to_dict(await bridge.run_build(step))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.config = args.config.resolve(strict=True)
        if args.command == "resume":
            args.ref = args.ref.resolve(strict=True)
        workspace = args.workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")
        settings = load_settings(args.config)
        os.umask(0o077)
        root = workspace / ".crew" / "openhands"
        if (workspace / ".crew").is_symlink() or root.is_symlink():
            raise ValueError("Adapter persistence cannot use symlink directories")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.environ["OH_PERSISTENCE_DIR"] = str(root / "sdk-config")
        os.environ["CREW_HOST"] = "openhands"
        os.environ["CLAUDE_PROJECT_DIR"] = str(workspace)
        os.chdir(workspace)
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        with contextlib.redirect_stdout(sys.stderr):
            from multiagent.review_workflow import WorkflowError

            try:
                result = asyncio.run(execute(args, settings, root))
            except WorkflowError as exc:
                print(
                    json.dumps({"error": exc.code, "message": str(exc)}),
                    file=sys.stderr,
                )
                return 2
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
