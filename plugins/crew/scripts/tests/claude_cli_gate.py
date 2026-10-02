#!/usr/bin/env python3
"""Opt-in real Claude Code fixture runner; no simulated hooks or native roles."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multiagent.providers.claude import _child_env

logger = logging.getLogger(__name__)


def source_manifest(plugin: Path) -> dict[str, str]:
    return {str(path.relative_to(plugin)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(plugin.rglob("*"))
            if path.is_file() and path.suffix in {".py", ".md", ".json"} and "__pycache__" not in path.parts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--label", required=True)
    parser.add_argument("--claude", type=Path, help="Claude Code executable (default: discover on PATH)")
    args = parser.parse_args()
    executable = str(args.claude) if args.claude is not None else shutil.which("claude")
    if executable is None:
        parser.error("Claude Code is not on PATH; supply --claude /path/to/claude")
    fixture = args.fixture.resolve(strict=True)
    if not fixture.is_relative_to(Path("/tmp").resolve()):
        parser.error("fixture must be an explicit disposable project under /tmp")
    plugin = Path(__file__).resolve().parents[2]
    env = _child_env()
    for name in ("CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT"):
        env.pop(name, None)
    env["PATH"] = os.environ.get("PATH", os.defpath)
    env["CREW_HOOK_DEBUG"] = "1"
    env.pop("CREW_VERBOSE", None)
    empty_mcp = fixture / "empty-mcp.json"
    if not empty_mcp.exists():
        empty_mcp.write_text('{"mcpServers":{}}\n')
    prefix = fixture / args.label
    before = source_manifest(plugin)
    prefix.with_suffix(".source-before.json").write_text(json.dumps(before, indent=2) + "\n")
    argv = [executable, "--print", "--output-format", "stream-json", "--verbose",
            "--include-hook-events", "--plugin-dir", str(plugin), "--strict-mcp-config",
            "--mcp-config", str(empty_mcp), "--debug-file", str(prefix.with_suffix(".debug.log")),
            "--resume" if args.resume else "--session-id", args.session_id,
            "--allowedTools", "Read", "Write", "Edit", "Agent", "Bash(rtk *)"]
    prefix.with_suffix(".invocation.json").write_text(json.dumps({"argv": argv, "cwd": str(fixture),
        "prompt_file": str(args.prompt.resolve()), "session_id": args.session_id,
        "crew_host_override": False, "hook_debug": True}, indent=2) + "\n")
    logger.info("Starting real Claude Code fixture %s in %s", args.label, fixture)
    with prefix.with_suffix(".stream.jsonl").open("wb") as output, prefix.with_suffix(".stderr.log").open("wb") as errors:
        completed = subprocess.run(argv, input=args.prompt.read_bytes(), cwd=fixture, env=env,
                                   stdout=output, stderr=errors, check=False)
    after = source_manifest(plugin)
    prefix.with_suffix(".source-after.json").write_text(json.dumps(after, indent=2) + "\n")
    summary = {"exit_code": completed.returncode, "source_unchanged": before == after}
    prefix.with_suffix(".process.json").write_text(json.dumps(summary) + "\n")
    logger.info("Fixture result: %s", summary)
    return completed.returncode if before == after else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
