"""Safe conversation ID validation shared by providers and the continuation store."""

from __future__ import annotations


def valid_conversation_id(value: object) -> bool:
    """Accept printable, non-blank conversation tokens that are not options."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not value.startswith("-")
        and value.isprintable()
    )
