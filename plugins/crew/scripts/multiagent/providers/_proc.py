"""Shared subprocess runner that reaps the whole process GROUP on timeout.

Extracted verbatim from agy's proven ``start_new_session`` + ``_terminate``
pattern so codex and cursor reap orphaned grandchildren too. A timed-out seat's
CLI commonly spawns its OWN children (billable API calls, MCP servers); killing
only the direct child leaves those grandchildren running. ``start_new_session``
puts the child in its own process group; on timeout we escalate
SIGTERM → SIGKILL across the whole GROUP (``killpg``) up to a hard deadline, so a
hung invocation can neither orphan grandchildren nor pin its worker thread.
"""

from __future__ import annotations

import os
import signal
import subprocess


# Hard deadline (seconds) for reaping after SIGTERM/SIGKILL so a hung invocation
# can't pin its worker thread indefinitely. Split half/half across the two
# escalation waits (matches agy's original REAP_DEADLINE_SECONDS behavior).
REAP_DEADLINE_SECONDS = 10


class ReapedTimeout:
    """Sentinel returned by :func:`run_reaped` when the wall-clock timeout
    fired and the child's process group has been reaped."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "<ReapedTimeout>"


# Singleton sentinel — callers compare with ``is TIMEOUT``.
TIMEOUT = ReapedTimeout()


def terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM → SIGKILL → hard-deadline reap of ``proc``'s process GROUP.

    Requires ``proc`` to have been started with ``start_new_session=True`` so it
    leads its own group; otherwise it falls back to signalling the single child.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    def _signal(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    _signal(signal.SIGTERM)
    try:
        proc.wait(timeout=REAP_DEADLINE_SECONDS / 2)
        return
    except subprocess.TimeoutExpired:
        pass

    _signal(signal.SIGKILL)
    try:
        proc.wait(timeout=REAP_DEADLINE_SECONDS / 2)
    except subprocess.TimeoutExpired:
        # Last resort: don't block the worker thread forever.
        pass


def run_reaped(
    argv,
    *,
    input_text: str | None = None,
    timeout: float,
    cwd: str | None = None,
    env: dict | None = None,
):
    """Run ``argv`` and return ``(returncode, stdout, stderr)`` or :data:`TIMEOUT`.

    stdout/stderr are captured as text. ``input_text`` (when not None) is fed on
    stdin (else stdin is DEVNULL). On timeout the child's whole process group is
    reaped (SIGTERM → SIGKILL) so no billable grandchild is orphaned, and the
    :data:`TIMEOUT` sentinel is returned.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group → reap children on timeout
        cwd=cwd,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_group(proc)
        return TIMEOUT
    return proc.returncode, stdout, stderr
