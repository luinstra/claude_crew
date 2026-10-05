#!/usr/bin/env python3
"""Capture Cursor hook environment names and a narrow safe value allowlist.

Root selection prefers ``CREW_PROJECT_DIR``, then legacy ``CLAUDE_PROJECT_DIR``,
then the first
existing directory the hook payload names, read by ``crew_base()`` from these
payload keys in order: a ``workspace_roots`` entry, then ``directory``, then
``cwd``. Without either source, the hook skips because its
installation-directory cwd is not a project root. The header records the host
detector answer, payload-shape verdict, and caller. ``session-start.py`` loads
this module in-process and reuses ``_has_cursor_payload_shape`` so both hooks
judge a payload the same way.
"""

import sys

if sys.version_info < (3, 11):
    print("{}")
    print(
        "[crew] cursor env capture skipped: Python %d.%d is unsupported "
        "(crew requires Python 3.11+): fix the hook interpreter "
        "(install python3 >= 3.11 on PATH)"
        % sys.version_info[:2],
        file=sys.stderr,
    )
    sys.exit(0)

import contextlib
import datetime
import json
import os
import re

from state_discovery import crew_base
from host_detect import detect_host


ENV_NAME_RE = re.compile(r"^(CURSOR|__CURSOR|VSCODE|ELECTRON|TERM_PROGRAM)")
SAFE_VALUE_NAMES = frozenset({
    "CURSOR_AGENT",
    "CURSOR_INVOKED_AS",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
})


def _redacted(name: str, value: str) -> str:
    return value if name in SAFE_VALUE_NAMES else "(set)"


def _has_cursor_payload_shape(payload: object) -> bool:
    return isinstance(payload, dict) and any(
        key in payload for key in ("cursor_version", "workspace_roots")
    )


def _payload() -> object:
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return None


def _body(payload: object, root: str, invocation_source: str = "manual") -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    captured_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        detected_host = detect_host()
    except Exception:
        detected_host = "error"
    if isinstance(payload, dict):
        payload_keys = ", ".join(sorted(str(key) for key in payload))
    else:
        payload_keys = "(none)"
    lines = [
        f"captured_at: {captured_at}",
        "hook: sessionStart",
        f"python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"cwd: {os.getcwd()}",
        f"root: {root}",
        f"detect_host: {detected_host}",
        f"cursor_payload_shape: {str(_has_cursor_payload_shape(payload)).lower()}",
        f"invocation_source: {invocation_source}",
        f"payload_keys: {payload_keys}",
        "",
    ]
    captured_any = False
    for name, value in sorted(os.environ.items()):
        if ENV_NAME_RE.match(name):
            lines.append(f"{name}={_redacted(name, value)}")
            captured_any = True
    if not captured_any:
        lines.append("(none)")
    return "\n".join(lines) + "\n"


def capture(
    root: str | os.PathLike[str],
    payload: object,
    *,
    invocation_source: str = "manual",
) -> None:
    try:
        root = os.fspath(root)
        crew_dir = os.path.join(root, ".crew")
        probes_dir = os.path.join(crew_dir, "probes")
        if os.path.islink(crew_dir) or os.path.islink(probes_dir):
            return
        try:
            os.makedirs(probes_dir, exist_ok=True)
        except OSError:
            return
        expected_dir = os.path.join(os.path.realpath(root), ".crew", "probes")
        if os.path.realpath(probes_dir) != expected_dir:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        filename = f"cursor-hook-env-{now.strftime('%Y-%m-%d')}.txt"
        path = os.path.join(probes_dir, filename)
        body = _body(payload, root, invocation_source)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(path)
            return
        try:
            with handle:
                handle.write(body)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(path)
    except Exception:
        return


def main() -> None:
    payload = _payload()
    root = crew_base(payload, fallback_to_cwd=False)
    if root is not None:
        capture(root, payload, invocation_source="cursor-env-capture.py")
    print("{}")


if __name__ == "__main__":
    main()
