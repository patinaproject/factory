"""email-intake: a gateway platform plugin that creates each forwarded email's card and fetches its stored objects.

Importing this package starts nothing and imports nothing from Hermes. The adapter module loads only when Hermes calls
`register`.
"""

from __future__ import annotations


def register(ctx) -> None:
    from .adapter import register as register_platform

    register_platform(ctx)
