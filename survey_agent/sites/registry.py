"""Site adapter registry — discover and load adapters by site slug."""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import SiteAdapter

# Registry of site slug -> adapter class
_registry: dict[str, type[SiteAdapter]] = {}


def register(slug: str):
    """Decorator to register a site adapter."""

    def decorator(cls):
        _registry[slug] = cls
        return cls

    return decorator


class SiteRegistry:
    """Registry of available site adapters."""

    def __init__(self):
        self._discover_adapters()

    def _discover_adapters(self) -> None:
        """Auto-discover adapter modules in the sites package."""
        import survey_agent.sites as pkg

        for importer, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
            if modname not in ("base", "registry", "__init__"):
                try:
                    importlib.import_module(f"survey_agent.sites.{modname}")
                except ImportError:
                    pass

    def list_sites(self) -> list[str]:
        """List all registered site slugs."""
        return list(_registry.keys())

    def get_adapter(self, slug: str, **kwargs) -> SiteAdapter:
        """Get an adapter instance by site slug."""
        if slug not in _registry:
            raise ValueError(f"Unknown site: {slug}. Available: {self.list_sites()}")
        return _registry[slug](**kwargs)

    def has_adapter(self, slug: str) -> bool:
        """Check if an adapter is registered for a site."""
        return slug in _registry
