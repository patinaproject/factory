"""factory-uploads: a gateway platform plugin that uploads evidence images.

Importing this package starts nothing and imports nothing from Hermes. The
adapter module loads only when Hermes calls `register`.
"""

from __future__ import annotations


def register(ctx) -> None:
    from .adapter import register as register_platform

    register_platform(ctx)
