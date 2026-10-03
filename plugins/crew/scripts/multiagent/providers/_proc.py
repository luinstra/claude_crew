"""Shared subprocess runner with bounded process-group quiescence checks.

The session leader may exit while its children still write. Escalate TERM/KILL
against the owned group, then confirm absence before declaring it quiescent.
If the bounded attempt cannot prove termination, return explicit uncertainty.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass

# Hard deadline (seconds) for reaping after SIGTERM/SIGKILL so a hung invocation
# can't pin its worker thread indefinitely. Split half/half across the two
# escalation waits (matches agy's original REAP_DEADLINE_SECONDS behavior).
REAP_DEADLINE_SECONDS = 10


@dataclass(frozen=True, slots=True)
class ReapedTimeout:
    """Timeout with explicit confirmation of process-group termination."""

    termination_confirmed: bool = True

    @property
    def transport_status(self) -> str:
        return "timeout" if self.termination_confirmed else "timeout_unconfirmed"

    @property
    def diagnostic_suffix(self) -> str:
        return (
            ""
            if self.termination_confirmed
            else "; process-group termination unconfirmed; writer may still be running"
        )


# Singleton sentinel — callers compare with ``is TIMEOUT``.
TIMEOUT = ReapedTimeout()
TIMEOUT_UNCONFIRMED = ReapedTimeout(False)


@dataclass(frozen=True, slots=True)
class ReapedFailure:
    """Post-launch failure, retaining output and group termination certainty."""

    error: str
    stdout: str | bytes
    stderr: str | bytes
    termination_confirmed: bool

    @property
    def transport_status(self) -> str:
        return "failed" if self.termination_confirmed else "termination_unconfirmed"


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class _HeldExit:
    """Keep this Popen's child unreaped until all owned-group signals are sent.

    communicate() calls wait (and _wait on KeyboardInterrupt). Temporarily bind
    those waits and polls to non-reaping observation. No other code owns this
    child. Linux/newer Python provides waitid; macOS Python 3.11/3.12 has kqueue.
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise OSError(
                "child identity cannot be held with a custom SIGCHLD disposition"
            )
        self.proc = proc
        self.held = True
        self.exited = False
        self.queue = None
        self.originals = {
            name: getattr(proc, name)
            for name in ("wait", "_wait", "poll", "_internal_poll")
        }
        self.waitid = getattr(os, "waitid", None)
        if not callable(self.waitid):
            if not hasattr(select, "kqueue"):
                raise OSError("non-reaping exit observation is unavailable")
            self.queue = select.kqueue()
            try:
                event = select.kevent(
                    proc.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                self._events(self.queue.control([event], 1, 0))
            except BaseException:
                self.queue.close()
                raise
        proc.wait = self.wait
        proc._wait = self.wait
        proc.poll = self.poll
        proc._internal_poll = self.poll

    def _events(self, events: list) -> None:
        for event in events:
            if event.flags & select.KQ_EV_ERROR:
                # A very short-lived owned child can exit before registration.
                # It has never been reaped: ESRCH here still pins its zombie PID.
                if event.data != errno.ESRCH:
                    raise OSError(event.data, "cannot observe owned child exit")
            elif not event.fflags & select.KQ_NOTE_EXIT:
                raise OSError("unexpected child exit notification")
            self.exited = True

    def poll(self, *args: object, **kwargs: object) -> int | None:
        if not self.held:
            raise OSError("owned child identity was released")
        if not self.exited:
            if callable(self.waitid):
                result = self.waitid(
                    os.P_PID, self.proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
                )
                self.exited = result is not None and result.si_pid == self.proc.pid
            else:
                self._events(self.queue.control([], 1, 0))
        # communicate uses this only as an exit notification. The actual status
        # and Popen.returncode come from the sole real wait after signalling.
        return 0 if self.exited else None

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while self.poll() is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(self.proc.args, timeout)
            time.sleep(0.01 if remaining is None else min(0.01, remaining))
        return 0

    def release(self) -> None:
        if self.held:
            self.held = False
            for name, original in self.originals.items():
                setattr(self.proc, name, original)
            if self.queue is not None:
                self.queue.close()


def terminate_process_group(
    proc: subprocess.Popen, observation: _HeldExit | None
) -> bool:
    """Signal only while the unreaped owned leader pins the PGID; then reap.

    Once reaped, group probes are read-only. A reused PGID can at worst make
    confirmation uncertain; it can never receive TERM/KILL from this runner.
    """
    if observation is None or not observation.held:
        return False
    deadline = time.monotonic() + REAP_DEADLINE_SECONDS
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            # Check that observation has not failed before every signal.
            observation.poll()
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass  # Group absence is established after the sole real reap.
            try:
                observation.wait(
                    timeout=max(
                        0, min(deadline - time.monotonic(), REAP_DEADLINE_SECONDS / 2)
                    )
                )
            except subprocess.TimeoutExpired:
                continue
            if sig == signal.SIGKILL:
                break
        if not observation.exited:
            return False
    except Exception:  # noqa: BLE001 - every post-launch observation failure retains writer uncertainty
        # Failed/unsupported observation cannot authorize uncertain signals.
        return False
    finally:
        observation.release()
    try:
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        return False
    while _group_exists(proc.pid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def run_reaped(
    argv: list[str],
    *,
    input_text: str | None = None,
    timeout: float,
    cwd: str | None = None,
    env: dict | None = None,
    capture_bytes: bool = False,
) -> tuple[int, str | bytes, str | bytes] | ReapedTimeout | ReapedFailure:
    """Run ``argv`` and return captured completion or a confirmed/uncertain timeout.

    stdout/stderr are text by default; ``capture_bytes`` preserves exact reports
    before text-mode newline conversion. ``input_text`` (when not None) is fed on
    stdin (else stdin is DEVNULL). On timeout the child's whole process group is
    signalled (SIGTERM → SIGKILL) to a bounded deadline. TIMEOUT means the group
    is confirmed absent; TIMEOUT_UNCONFIRMED retains uncertainty. Ordinary exits
    also check and terminate surviving group members before returning completion.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not capture_bytes,
        start_new_session=True,
        cwd=cwd,
        env=env,
    )
    stdout = stderr = b"" if capture_bytes else ""
    observation = None
    try:
        observation = _HeldExit(proc)
    except Exception:  # noqa: BLE001,S110 - uncertainty is returned through the transport contract
        # Retain uncertainty if this platform cannot safely pin child identity.
        pass
    try:
        payload = (
            input_text.encode("utf-8")
            if capture_bytes and input_text is not None
            else input_text
        )
        stdout, stderr = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        confirmed = terminate_process_group(proc, observation)
        return TIMEOUT if confirmed else TIMEOUT_UNCONFIRMED
    except Exception as exc:  # noqa: BLE001 - any post-launch communication failure requires group teardown
        confirmed = terminate_process_group(proc, observation)
        return ReapedFailure(
            f"process communication failed: {exc}", stdout, stderr, confirmed
        )
    except BaseException:
        terminate_process_group(proc, observation)
        raise
    else:
        if not terminate_process_group(proc, observation):
            return ReapedFailure(
                "process-group termination unconfirmed; writer may still be running",
                stdout,
                stderr,
                False,
            )
        return proc.returncode, stdout, stderr
    finally:
        if observation is not None:
            observation.release()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
