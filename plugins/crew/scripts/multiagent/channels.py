"""Resolve one catalog seat to one host/channel execution."""

from __future__ import annotations

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


def current_host() -> str:
    # Host detection remains dormant until a second host exists.
    return "claude"


def native_channel(host: str) -> str:
    # Extend this mapping when a second host exists.
    return {"claude": "claude"}[host]


def channel_table(host: str) -> dict[str, ChannelSpec]:
    """Return the capability-free static channel table for ``host``."""
    native = native_channel(host)
    return {
        channel: ChannelSpec(
            name=channel,
            legacy_kind=legacy_kind,
            native=channel == native,
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
    host: str,
    capabilities: Mapping[str, ChannelCapability] | None = None,
) -> ResolvedExecution | None:
    """Resolve ``spec`` without changing its model or selecting another seat."""
    effective = capabilities if capabilities is not None else active_capabilities()
    table = channel_table(host)
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
