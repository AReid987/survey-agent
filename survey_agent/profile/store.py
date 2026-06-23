"""Profile store — persistent user profile with versioned history."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .categories import CATEGORIES, get_all_field_keys


class ProfileChange(BaseModel):
    """A single change to the profile."""

    timestamp: str
    key: str
    old_value: Any = None
    new_value: Any = None
    source: str = "manual"  # manual | interview | correction | auto


class ProfileStore:
    """Persistent user profile with versioned history.

    Data is stored as a nested dict (category -> field -> value).
    History is append-only.
    """

    def __init__(self, profile_path: str | Path, history_path: str | Path | None = None):
        self._path = Path(profile_path)
        self._history_path = Path(history_path) if history_path else self._path.parent / "profile_history.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = {}
        self._history: list[ProfileChange] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            with open(self._path) as f:
                self._data = json.load(f)
        if self._history_path.exists():
            with open(self._history_path) as f:
                raw = json.load(f)
                self._history = [ProfileChange(**c) for c in raw]

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)
        with open(self._history_path, "w") as f:
            json.dump([c.model_dump() for c in self._history], f, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a profile value by dotted key (e.g. 'demographics.age')."""
        parts = key.split(".", 1)
        if len(parts) != 2:
            return default
        cat, field = parts
        return self._data.get(cat, {}).get(field, default)

    def set(self, key: str, value: Any, source: str = "manual") -> None:
        """Set a profile value by dotted key. Records history."""
        parts = key.split(".", 1)
        if len(parts) != 2:
            raise ValueError(f"Key must be in 'category.field' format, got: {key}")
        cat, field = parts
        old_value = self._data.get(cat, {}).get(field)
        if old_value == value:
            return
        if cat not in self._data:
            self._data[cat] = {}
        self._data[cat][field] = value
        self._history.append(
            ProfileChange(
                timestamp=datetime.now(UTC).isoformat(),
                key=key,
                old_value=old_value,
                new_value=value,
                source=source,
            )
        )
        self._save()

    def get_category(self, category: str) -> dict[str, Any]:
        """Get all fields in a category."""
        return dict(self._data.get(category, {}))

    def get_all(self) -> dict[str, dict[str, Any]]:
        """Get the full profile data."""
        return dict(self._data)

    def history(self, key: str | None = None, limit: int = 50) -> list[ProfileChange]:
        """Get change history, optionally filtered by key."""
        items = self._history
        if key:
            items = [c for c in items if c.key == key]
        return items[-limit:]

    def correct(self, key: str, new_value: Any, note: str = "") -> None:
        """Apply a correction to the profile. Same as set but source='correction'."""
        self.set(key, new_value, source="correction")

    def summary(self) -> str:
        """Return a human-readable summary of the profile."""
        lines = []
        for cat in CATEGORIES:
            cat_data = self._data.get(cat.key, {})
            if not cat_data:
                continue
            lines.append(f"\n## {cat.label}")
            for field in cat.fields:
                val = cat_data.get(field.key)
                if val is not None:
                    lines.append(f"  {field.label}: {val}")
        return "\n".join(lines) if lines else "(empty profile)"

    def to_context_string(self) -> str:
        """Return a compact string representation suitable for LLM context."""
        lines = ["User Profile:"]
        for cat in CATEGORIES:
            cat_data = self._data.get(cat.key, {})
            if not cat_data:
                continue
            for field in cat.fields:
                val = cat_data.get(field.key)
                if val is not None:
                    lines.append(f"- {field.label}: {val}")
        return "\n".join(lines)

    def completion_pct(self) -> float:
        """Return the percentage of defined fields that have values."""
        all_keys = get_all_field_keys()
        filled = 0
        for key in all_keys:
            if self.get(key) is not None:
                filled += 1
        return (filled / len(all_keys) * 100) if all_keys else 0.0
