"""Resolve one catalog seat to one host/channel execution."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from multiagent import seats


@dataclass(frozen=True)
class ChannelSpec:
    """Static channel shape; capability facts belong to ``resolve_seat``."""

    name: str
    legacy_kind: str
    native: bool
    model_rule: str


@dataclass(frozen=True)
class ChannelCapability:
    """Effective capability facts for one external channel."""

    supports_workspace_write: bool
    probe_available: Callable[[], bool]


@dataclass(frozen=True)
class ResolvedExecution:
    """The selected execution for one seat on one host."""

    seat: str
    model: str | None
    channel: str
    native: bool
    engine_runnable: bool
    supports_workspace_write: bool


_capabilities_override: Mapping[str, ChannelCapability] | None = None

# Two independent Codex-host captures agreed on these exact names (see
# docs/codex-host.md); neither appears in a genuine Claude host env.
# CODEX_COMPANION_* names are deliberately not included: genuine Claude hosts
# carry those companion vars. CODEX_CI was observed too but not adopted (a
# CI-flavored name is the likeliest future collision).
_CODEX_HOST_MARKERS: tuple[str, ...] = (
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX_NETWORK_DISABLED",
)

# Observed live in the Cursor agent shell (see docs/cursor-host.md), which is
# the shell every crew command runs in on that host. That shell scrubs operator
# exports such as CREW_HOST but keeps these, so they are the only unaided signal
# available there. CURSOR_INVOKED_AS and CURSOR_RIPGREP_PATH were observed too
# and deliberately left out: detection matches on ANY name, so a wider table
# buys no detection power and only widens the false-positive surface, and a
# tool-path or invocation-name variable is the likeliest thing a non-Cursor
# shell exports by coincidence.
_CURSOR_HOST_MARKERS: tuple[str, ...] = (
    "CURSOR_AGENT",
    "CURSOR_CONVERSATION_ID",
)

_CLAUDE_HOST_MARKERS: tuple[str, ...] = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)

_warned: set[str] = set()


def codex_host_markers() -> tuple[str, ...]:
    """Return the configured exact Codex-host marker names."""
    return _CODEX_HOST_MARKERS


def cursor_host_markers() -> tuple[str, ...]:
    """Return the configured exact Cursor-host marker names."""
    return _CURSOR_HOST_MARKERS


def _warn_once(key: str, message: str) -> None:
    """Emit one process-local warning for a malformed host override."""
    if key in _warned:
        return
    _warned.add(key)
    print(message, file=sys.stderr)


def _detect_host(env: Mapping[str, str]) -> str:
    """Detect the current harness from an environment mapping.

    ``CREW_HOST`` is the explicit operator override and outranks every marker.
    Below it the tiers answer one question: when markers for more than one host
    are present, which harness is actually EXECUTING crew?

    Cursor ranks LAST for that reason. Its markers ride in the integrated
    terminal a human types into, so they survive into whatever that human
    launches there, another harness included: a Claude Code session started
    from a Cursor terminal carries ``CLAUDECODE`` and ``CURSOR_AGENT`` at once,
    and the executor is Claude Code. Reading that env as cursor mints native
    actions Claude Code cannot spawn, so the Claude tier has to win it. The
    Cursor agent shell, the shell every crew command runs in on that host,
    carries no Claude marker at all, so pure-cursor detection is unaffected.

    Codex stays above Claude. Its reachable collision runs the other way: crew
    scrubs the codex and cursor markers from a claude child but nothing scrubs
    ``CLAUDECODE`` from a codex child, so a codex seat re-invoking crew sees
    both names with codex as the true executor.

    Empty-valued markers are treated as absent.
    """
    known_hosts = ("claude", "codex", "cursor")
    override = env.get("CREW_HOST", "")
    if override:
        normalized = override.casefold()
        if normalized in known_hosts:
            return normalized
        _warn_once(
            "invalid-crew-host",
            f"crew: CREW_HOST={override!r} is not a known host (claude, codex, cursor); "
            "treating the host as unknown (no native channel)",
        )
        return "unknown"

    if any(env.get(marker) for marker in _CODEX_HOST_MARKERS):
        return "codex"
    if any(env.get(marker) for marker in _CLAUDE_HOST_MARKERS):
        return "claude"
    if any(env.get(marker) for marker in _CURSOR_HOST_MARKERS):
        return "cursor"
    return "unknown"


def current_host() -> str:
    return _detect_host(os.environ)


# The one native-channel table. A row here is only half the answer: whether the
# review workflow can drive it depends on that host also having a role row, and
# `native_channel_for` returns None without one, so a row added here alone
# declares nothing native rather than failing at mint. A test pins the two key
# sets equal on top of that, so a half-added host is named rather than silent.
_NATIVE_CHANNELS: dict[str, str] = {"claude": "claude", "cursor": "cursor", "codex": "codex"}


def native_channel_hosts() -> frozenset[str]:
    """Return every host with a native channel row."""
    return frozenset(_NATIVE_CHANNELS)


def native_channel(host: str) -> str | None:
    """Return the channel ``host`` can run in-session, or ``None`` when none is.

    This is the host's TRUE native channel. The standalone review path declares
    from it, per seat, because it can drive an in-session cursor action; every
    other caller passes ``task_native_channel`` below, the narrower answer the
    Task recipes and subprocess runners share. Whether a host CAN run a channel
    and whether a given workflow can drive a given SEAT through it are separate
    questions: the second depends on which roles ship, which is review policy
    and lives with the workflow.
    """
    return _NATIVE_CHANNELS.get(host)


def task_native_channel(host: str) -> str | None:
    """Return the native channel the Task recipes and subprocess runners know.

    The seat-prep split and the subprocess runners have to classify one seat
    the same way: whatever prep calls a subprocess seat, ``crew run`` has to be
    willing to run. Both sides pass this answer, so neither drifts from the
    other when a host gains an in-session channel the recipes cannot drive.
    """
    return {"claude": "claude"}.get(host)


def channel_table(*, declared_native: str | None) -> dict[str, ChannelSpec]:
    """Return the capability-free static channel table.

    ``declared_native`` is required and has no default: the CALLER states which
    channel it can drive in-session, and a caller that can drive none passes
    ``None``. The table therefore needs no host: the native column is the
    caller's declaration, not a fact derived from the harness.
    """
    # A caller that can drive no native channel marks every channel external.
    return {
        channel: ChannelSpec(
            name=channel,
            legacy_kind=legacy_kind,
            native=channel == declared_native,
            model_rule=seats.PROVIDER_KINDS[legacy_kind].model_rule,
        )
        for channel, legacy_kind in seats.CHANNEL_TO_LEGACY_KIND.items()
    }


def _default_capabilities() -> dict[str, ChannelCapability]:
    """Build fresh channel capabilities from the provider registry classes."""
    from multiagent import providers

    return {
        seats.LEGACY_PROVIDER_TO_CHANNEL[kind]: ChannelCapability(
            supports_workspace_write=facts.supports_workspace_write,
            probe_available=facts.probe_available,
        )
        for kind, facts in providers.executor_capabilities().items()
    }


def set_capabilities(
    capabilities: Mapping[str, ChannelCapability] | None,
) -> None:
    """Set or clear the process-local capability override used by commands."""
    global _capabilities_override
    _capabilities_override = capabilities


def active_capabilities() -> Mapping[str, ChannelCapability]:
    """Return the override, or freshly derive the default capability mapping."""
    return (_capabilities_override
            if _capabilities_override is not None else _default_capabilities())


def resolve_seat(
    spec: seats.SeatSpec,
    *,
    capabilities: Mapping[str, ChannelCapability] | None = None,
    declared_native: str | None,
) -> ResolvedExecution | None:
    """Resolve ``spec`` without changing its model or selecting another seat.

    ``declared_native`` is required and has no default, for the reason stated
    on the two accessors above: the caller, not the host, names the route it
    can drive in-session.
    """
    effective = capabilities if capabilities is not None else active_capabilities()
    table = channel_table(declared_native=declared_native)
    eligible: list[ChannelSpec] = []
    for channel in spec.via:
        channel_spec = table.get(channel)
        if channel_spec is None:
            continue
        if channel_spec.model_rule == "alias" \
                and spec.model not in seats.TASK_MODEL_ALIASES:
            continue
        eligible.append(channel_spec)

    if not eligible:
        return None

    selected = eligible[0]
    if len(eligible) > 1:
        for candidate in eligible:
            if candidate.native:
                selected = candidate
                break
            # A runtime failure remains a failed or skipped seat; resolution
            # never reroutes after the selected channel starts running.
            capability = effective.get(candidate.name)
            if capability is not None and capability.probe_available():
                selected = candidate
                break

    capability = effective.get(selected.name)
    engine_runnable = not selected.native and capability is not None
    return ResolvedExecution(
        seat=spec.name,
        model=spec.model,
        channel=selected.name,
        native=selected.native,
        engine_runnable=engine_runnable,
        supports_workspace_write=(
            capability.supports_workspace_write
            if not selected.native and capability is not None
            else False
        ),
    )
