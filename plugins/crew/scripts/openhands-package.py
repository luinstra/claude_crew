#!/usr/bin/env python3
"""Generate native role metadata or export a self-contained OpenHands package."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        "crew OpenHands packaging requires Python 3.11+; no action taken\n"
    )
    raise SystemExit(2)
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generated(root: Path, profiles: dict[str, Path]) -> dict[Path, bytes]:
    files = {}
    routes = {}
    for profile, store in {"inherit": None, **profiles}.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile):
            raise ValueError(f"invalid profile reference: {profile!r}")
        roles = {}
        for path in sorted((root / "agents").glob("*.md")):
            parts = path.read_text(encoding="utf-8").split("---\n", 2)
            if len(parts) != 3:
                raise ValueError(f"invalid canonical role: {path}")
            body = parts[2]
            name = "__CREW_ROLE__"
            metadata = f"---\nname: {name}\ndescription: Native Crew {path.stem} role\nmodel: {json.dumps(profile)}\ntools: [terminal, file_editor]\n"
            if store is not None:
                metadata += f"profile_store_dir: {json.dumps(str(store))}\n"
            content = metadata + "---\n\n" + body
            content += "\n## Native OpenHands host\n\nUse terminal and file_editor for the canonical read/search/write tasks above. "
            content += "TodoWrite and delegation are unavailable in this role. Maintain a short checklist when needed. "
            content += "Do not invoke Claude-specific Task mechanics. Respect the native parent's confirmation policy. "
            content += (
                "Read-only instructions are advisory because terminal can write.\n"
            )
            name = f"crew-{path.stem}-{hashlib.sha256(content.encode()).hexdigest()}"
            roles[f"crew:{path.stem}"] = name
            content = content.replace("name: __CREW_ROLE__\n", f"name: {name}\n", 1)
            files[Path("dev.openhands/agents") / f"{name}.md"] = content.encode()
        routes[profile] = {"roles": roles}
        if store is not None:
            routes[profile]["profile_path"] = str(store / f"{profile}.json")
    files[Path("dev.openhands/routes.json")] = (
        json.dumps(routes, indent=2) + "\n"
    ).encode()
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--profile-store-dir", type=Path)
    args = parser.parse_args()
    if args.profile and (args.output is None or args.profile_store_dir is None):
        parser.error(
            "named profiles require --output and the backend's existing --profile-store-dir"
        )
    if "inherit" in args.profile or len(set(args.profile)) != len(args.profile):
        parser.error("profile references must be unique; inherit is already included")
    profiles = {name: args.profile_store_dir.resolve() for name in args.profile}
    target = args.output or ROOT
    if args.output:
        if args.output.exists() or ROOT in args.output.resolve().parents:
            parser.error("output must be a new directory outside the source plugin")
        # Allowlisted sources exclude caches, credentials, test output and .crew.
        target.mkdir(parents=True)
        for name in (
            "crew",
            "plugin.json",
            "agents",
            "commands",
            "scripts",
            "dev.openhands",
        ):
            src, dst = ROOT / name, target / name
            if src.is_dir():
                shutil.copytree(
                    src,
                    dst,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", ".ruff_cache", "tests"
                    ),
                )
            else:
                shutil.copy2(src, dst)
        (target / "docs").mkdir()
        for name in (
            "openhands-transport.md",
            "build-protocol.md",
            "measure-twice-protocol.md",
            "codex-transport.md",
            "cursor-host.md",
        ):
            shutil.copy2(ROOT / "docs" / name, target / "docs" / name)
    expected = generated(target, profiles)
    actual = set((target / "dev.openhands/agents").glob("*.md"))
    wanted = {target / path for path in expected if path.suffix == ".md"}
    if args.check:
        if actual != wanted or any(
            not (target / path).is_file() or (target / path).read_bytes() != content
            for path, content in expected.items()
        ):
            raise SystemExit(
                "OpenHands generated roles/routes differ from canonical roles"
            )
    else:
        for obsolete in actual - wanted:
            obsolete.unlink()
        for path, content in expected.items():
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)


if __name__ == "__main__":
    main()
