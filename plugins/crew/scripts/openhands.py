#!/usr/bin/env python3
"""Native Canvas context wrapper and exact event bridge, without an SDK runner."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    sys.stderr.write("crew openhands requires Python 3.11+; no action taken\n")
    raise SystemExit(2)

from multiagent import build_workflow as build
from multiagent import measure_twice as measure
from multiagent import openhands_native_transport as native
from multiagent import review_workflow as review


def parse_ref(path: Path, owner: str) -> native.Ref:
    value = native.read_json(path)
    try:
        return {
            "review": review.parse_review_ref,
            "measure": measure.parse_measure_ref,
            "build": build.parse_build_ref,
        }[owner](value)
    except (review.WorkflowError, KeyError, TypeError, ValueError) as exc:
        raise review.WorkflowError(
            "invalid_ref", f"invalid {owner} ref in {path}: {exc}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--backend-id")
    run.add_argument("--new-context", action="store_true")
    run.add_argument("--context-file", type=Path)
    run.add_argument("--conversation-dir", type=Path)
    run.add_argument("--workspace", type=Path)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    for name in ("prepare", "capture", "recover"):
        action = sub.add_parser(name)
        action.add_argument(
            "--owner", choices=("review", "measure", "build"), required=True
        )
        action.add_argument("--ref-file", type=Path, required=True)
        action.add_argument("--action-id", required=True)
        if name == "recover":
            action.add_argument(
                "--confirmation", choices=("not_running",), required=True
            )
    args = parser.parse_args()
    try:
        if args.command == "run":
            if args.context_file:
                if (
                    args.new_context
                    or args.backend_id
                    or args.conversation_dir
                    or args.workspace
                ):
                    raise review.WorkflowError(
                        "invalid_context",
                        "--context-file cannot be combined with new context inputs",
                    )
                value = native.read_json(args.context_file)
                host = native.parse_context(value)
                context_path = args.context_file.resolve()
                os.environ["CREW_PROJECT_DIR"] = host.workspace
            else:
                if (
                    not args.backend_id
                    or not args.conversation_dir
                    or not args.workspace
                ):
                    raise review.WorkflowError(
                        "missing_host_context",
                        "supply actual backend-id, conversation-dir and workspace, or a retained context-file",
                    )
                host = native.inspect_context(
                    args.backend_id, args.conversation_dir, args.workspace
                )
                os.environ["CREW_PROJECT_DIR"] = host.workspace
                context_path = native.select_context(
                    host, new_generation=args.new_context
                )
            os.environ["CREW_HOST"] = "openhands"
            env = {**os.environ, "CREW_OPENHANDS_CONTEXT": str(context_path)}
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if argv and argv[0] == str(native.PLUGIN / "crew"):
                argv = argv[1:]
            if not argv:
                sys.stdout.write(
                    json.dumps(
                        {
                            "session_id": host.session_id,
                            "context_path": str(context_path),
                        }
                    )
                    + "\n"
                )
                return 0
            if argv[0] in {"prepare", "capture", "recover"}:
                command = [sys.executable, str(Path(__file__).resolve()), *argv]
            else:
                if argv[0] in {
                    "review",
                    "debate",
                    "measure-twice",
                    "measure-twice-resume",
                    "build",
                    "build-resume",
                }:
                    options = argv[: argv.index("--") if "--" in argv else len(argv)]
                    if any(
                        arg == "--session-id" or arg.startswith("--session-id=")
                        for arg in options
                    ):
                        raise review.WorkflowError(
                            "session_mismatch",
                            "the wrapper supplies the native session identity",
                        )
                    if any(
                        arg == "--panel" or arg.startswith("--panel=")
                        for arg in options
                    ):
                        raise review.WorkflowError(
                            "unsupported_panel", "OpenHands uses --seats; omit --panel"
                        )
                    argv = [argv[0], "--session-id", host.session_id, *argv[1:]]
                command = [str(native.PLUGIN / "crew"), *argv]
            return subprocess.run(
                command, cwd=host.workspace, env=env, check=False
            ).returncode
        ref = parse_ref(args.ref_file, args.owner)
        if args.command == "recover":
            result = native.recover(ref, args.action_id, args.confirmation)
        else:
            result = (
                native.prepare(ref, args.action_id)
                if args.command == "prepare"
                else native.capture(ref, args.action_id)
            )
        if dataclasses.is_dataclass(result):
            result = (
                {
                    "review": review.review_step_to_dict,
                    "measure": measure.step_to_dict,
                    "build": build.step_to_dict,
                }[args.owner]
            )(result)
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    except (review.WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        sys.stderr.write(f"crew openhands: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
