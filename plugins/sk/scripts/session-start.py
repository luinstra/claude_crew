#!/usr/bin/env python3
"""
sk Session Start Hook
Detects the project's tech stack and names the sk skills that cover it.

Passive activation (Claude reading each SKILL.md `description` keyword net) almost
never fired in practice, so the matching skills are named up front instead. This
lives in sk rather than in a consumer plugin because only the plugin that SHIPS the
skills can tell which of them are actually present: a mapping kept anywhere else
advertises skills that may not exist, and goes stale silently when sk changes.
"""

# --- Version guard: stdlib-only, 3.9-parseable, BEFORE anything else.
# The floor is 3.11, matching the rest of this marketplace. The guard exists
# because the shebang resolves to whatever `python3` PATH hands over, which on
# macOS can still be the system 3.9: below the floor this must be a VISIBLE
# no-op (allow + loud diagnostic), never a traceback that greets the session.
# It has to precede the imports below, since a `X | None` annotation is a
# def-time TypeError on 3.9 and would crash before any guard could speak.
import json
import sys

MIN_PYTHON = (3, 11)

if sys.version_info < MIN_PYTHON:
    _VERSION_DIAG = (
        f"[sk] SessionStart hook disabled: Python "
        f"{sys.version_info[0]}.{sys.version_info[1]} is below this "
        f"marketplace's {MIN_PYTHON[0]}.{MIN_PYTHON[1]} floor. Skill hints "
        f"skipped for this session (fix the hook interpreter, e.g. put a newer "
        f"python3 first on PATH)."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _VERSION_DIAG,
        }
    }))
    print(_VERSION_DIAG, file=sys.stderr)
    sys.exit(0)

import os
import subprocess
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

# The plugin root is this script's grandparent (scripts/ sits beside skills/).
# Derived from __file__ rather than CLAUDE_PLUGIN_ROOT so the hook resolves its
# own skills identically when run directly (tests) and from the harness.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"

# File list comes from the git index when there is one, and from a tree walk
# otherwise. The index is preferred for CORRECTNESS, not speed: the walk needs a
# hard entry cap to stay inside the 10s hook budget, and a cap plus arbitrary
# directory order means a large generated directory (benchmark output, fixtures)
# can exhaust the budget before a real source dir is ever reached, silently
# under-reporting the stack. The index has no such failure mode and is already
# gitignore-aware (a better prune list than any hardcoded set can be). Both
# sources scale with the paths they see; the index just sees far fewer, after a
# fixed process spawn (measured 9-13ms here against a 1.5-21ms walk).
GIT_LS_FILES = ("ls-files", "-z", "--cached", "--others", "--exclude-standard")
# --others --exclude-standard is what makes this honest on a repo mid-edit: a
# file written but not yet `git add`ed still counts, and a fresh `git init` with
# nothing committed is not read as an empty project.
GIT_TIMEOUT_SECONDS = 5

# Fallback walk bounds. Canonical artifact/vendor dirs are pruned (real sources
# never live under them); the entry cap is a hard ceiling on files visited. Both
# are module-level so tests can dial the cap down.
PRUNE_DIRS = {".git", ".crew", "node_modules", "build", ".venv", "venv", ".gradle",
              "dist", "target", "__pycache__", ".tox"}
WALK_MAX_ENTRIES = 20_000

# Suffix to the markers it implies, LONGEST FIRST: a .gradle.kts is also a .kts,
# so the first match wins and the order is load-bearing.
SUFFIX_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (".gradle.kts", ("gradle.kts", "kts")),
    (".kts", ("kts",)),
    (".kt", ("kt",)),
    (".py", ("py",)),
)

# Every marker the scan can learn. Once all are seen there is nothing left to
# find, so the scan stops early rather than reading the rest of the tree.
ALL_MARKERS = frozenset(m for _suffix, ms in SUFFIX_MARKERS for m in ms)

# Stack hint to the sk skills that cover it, in the order they should be offered.
# A named skill is advertised only if its directory is actually present, so a
# detector kept for a skill sk no longer ships stays silent instead of lying
# (that drift is why detection moved here from a plugin that could not check).
STACK_SKILLS: dict[str, tuple[str, ...]] = {
    "Kotlin": ("kotlin", "kotlin-testing"),
    "Python": ("python",),
    "Exposed": ("exposed",),
    "Gradle": ("gradle",),
}
# sk:git is deliberately absent: it maps to an ACTION (commit, rebase) rather
# than to anything structural, so there is nothing here to detect, and its own
# description already activates it reliably.



def project_dir() -> Path:
    """The tree to scan: CLAUDE_PROJECT_DIR when the harness sets it, else cwd."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def index_paths(directory: Path) -> list[str] | None:
    """Tracked + unignored paths from the git index, or None when unavailable.

    None means "no usable index here" for every reason (not a repo, git not on
    PATH, git too slow, git unhappy) and is the caller's signal to walk instead.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), *GIT_LS_FILES],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.split("\0") if p]


def walk_filenames(directory: Path) -> Iterator[str]:
    """Yield filenames under directory, pruning artifact dirs and capping entries."""
    visited = 0
    for _root, dirnames, filenames in os.walk(str(directory)):
        # Prune in-place so os.walk never descends into artifact/vendor dirs.
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for name in filenames:
            # Check the cap INSIDE the file loop so one pathological directory
            # can't be fully iterated before the ceiling fires.
            if visited >= WALK_MAX_ENTRIES:
                return
            visited += 1
            yield name


def marker_for(name: str) -> tuple[str, ...]:
    """The markers a file name implies, empty when it implies none."""
    for suffix, markers in SUFFIX_MARKERS:
        if name.endswith(suffix):
            return markers
    return ()


def source_markers(
    names: Iterable[str],
    verify: Callable[[str], bool] | None = None,
) -> set[str]:
    """Reduce file names (or paths, the suffix test is the same) to markers.

    The ONE place an extension becomes a marker, so the index and the walk can
    never disagree about what a file means.

    ``verify`` is the on-disk existence check an index-sourced list needs (see
    ``detect_project_stack``); a walked name is a file that was just listed, so
    the walk passes None.
    """
    markers: set[str] = set()
    for name in names:
        found = marker_for(name)
        # Skip anything that teaches nothing: no marker, or one already known.
        # That keeps `verify` down to roughly one call per marker rather than
        # one per file.
        if not found or markers.issuperset(found):
            continue
        if verify is not None and not verify(name):
            continue
        markers.update(found)
        if markers >= ALL_MARKERS:
            break
    return markers


def detect_project_stack(directory: Path) -> list[str]:
    """Detect project tech stack and return hint names (keys of STACK_SKILLS).

    Python is detected by extension rather than by a root pyproject.toml /
    setup.py / requirements.txt: a repo can carry real Python with none of those
    present (this marketplace is one), and a marker check would miss it.
    """
    def on_disk(relative: str) -> bool:
        return (directory / relative).is_file()

    # The index lists ENTRIES, not files: a tracked path deleted from the
    # working tree is still cached, and would otherwise report a stack that is
    # gone. Walked names need no such check, they were just listed.
    names = index_paths(directory)
    if names is None:
        markers = source_markers(walk_filenames(directory))
    else:
        markers = source_markers(names, verify=on_disk)

    hints: list[str] = []
    if markers & {"kt", "kts"}:
        hints.append("Kotlin")
    if "gradle.kts" in markers:
        hints.append("Gradle")
    if "py" in markers:
        hints.append("Python")

    # Dependency hints are a root build-file read either way.
    build_file = directory / "build.gradle.kts"
    if build_file.is_file():
        try:
            content = build_file.read_text().lower()
            if "exposed" in content:
                hints.append("Exposed")
        except OSError:
            pass

    return hints


def is_installed(skill: str) -> bool:
    """True when this sk install actually ships the named skill."""
    return (SKILLS_DIR / skill / "SKILL.md").is_file()


def relevant_skills(stack_hints: list[str]) -> list[str]:
    """The `sk:<name>` skills covering the detected stack, deduped, order-stable."""
    names: list[str] = []
    for hint in stack_hints:
        for skill in STACK_SKILLS.get(hint, ()):
            qualified = f"sk:{skill}"
            if qualified not in names and is_installed(skill):
                names.append(qualified)
    return names


def build_guidance(stack_hints: list[str]) -> str:
    """Build the skill guidance block, or "" when sk has nothing to offer here."""
    skills = relevant_skills(stack_hints)
    if not skills:
        return ""

    skill_list = ", ".join(skills)
    return "\n".join([
        "<system-reminder>",
        f"Project stack detected: {', '.join(stack_hints)}.",
        "",
        f"**sk skills for this stack**: {skill_list}",
        "Invoke the matching `Skill(<name>)` BEFORE writing code in that domain (lazy-load).",
        "When you invoke a skill, prefix your NEXT user-facing message with "
        "`[skill: <name>]` so the user can see it loaded.",
        "",
        "**In your first reply this session**, include a single status line "
        f"(after any leading thought, before substantive content): `🔧 [sk] skills ready: {skill_list}`",
        "</system-reminder>",
    ])


def main() -> None:
    # The payload is read and discarded: the tree to scan comes from the
    # environment, matching how the harness scopes a session.
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        pass

    guidance = build_guidance(detect_project_stack(project_dir()))
    if not guidance:
        # Nothing to say beats an empty reminder in every non-Kotlin session.
        print(json.dumps({}))
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": guidance,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001. Hooks are fail-open;
        # make the failure LOUD (stderr + the SessionStart context channel),
        # never silent, and never fatal to the session.
        _diag = (
            f"[sk] SessionStart hook crashed ({exc.__class__.__name__}: {exc}), "
            "continuing without skill hints for this session."
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _diag,
            }
        }))
        print(_diag, file=sys.stderr)
        sys.exit(0)
