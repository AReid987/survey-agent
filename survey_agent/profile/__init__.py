"""Profile system — persistent user profile with versioned history."""

from .categories import CATEGORIES
from .store import ProfileStore

__all__ = ["ProfileStore", "CATEGORIES"]
