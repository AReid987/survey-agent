"""Profile interview — interactive flow to populate the user profile."""

from __future__ import annotations

from .categories import CATEGORIES, ProfileField
from .store import ProfileStore


def interview_prompt_for_field(field: ProfileField, category_label: str) -> str:
    """Generate a prompt for a single profile field."""
    prompt = f"[{category_label}] {field.label}: "
    if field.description:
        prompt += f"({field.description}) "
    if field.field_type == "select" and field.options:
        prompt += f"Options: {', '.join(field.options)}"
    elif field.field_type == "multi_select" and field.options:
        prompt += f"Select any that apply: {', '.join(field.options)}"
    elif field.field_type == "boolean":
        prompt += "Yes or No"
    elif field.field_type == "number":
        prompt += "Enter a number"
    return prompt


def build_interview_questions() -> list[dict]:
    """Build a list of interview questions from category definitions."""
    questions = []
    for cat in CATEGORIES:
        for field in cat.fields:
            questions.append(
                {
                    "category": cat.key,
                    "category_label": cat.label,
                    "field_key": field.key,
                    "field_label": field.label,
                    "field_type": field.field_type,
                    "options": field.options,
                    "description": field.description,
                    "prompt": interview_prompt_for_field(field, cat.label),
                }
            )
    return questions


def apply_answer(  # noqa: PLR0911, PLR0913, PLR0912
    store: ProfileStore, category: str, field_key: str, answer: str, field_type: str, options: list[str] | None = None
) -> bool:
    """Apply an answer to the profile store. Returns True if valid."""
    dotted_key = f"{category}.{field_key}"

    if field_type == "boolean":
        val: bool | int | float = answer.strip().lower() in ("yes", "y", "true", "1")
        store.set(dotted_key, val, source="interview")
        return True
    elif field_type == "number":
        try:
            val = float(answer) if "." in answer else int(answer)
            store.set(dotted_key, val, source="interview")
            return True
        except ValueError:
            return False
    elif field_type == "select":
        if options:
            # Case-insensitive match
            for opt in options:
                if opt.lower() == answer.strip().lower():
                    store.set(dotted_key, opt, source="interview")
                    return True
            return False
        store.set(dotted_key, answer.strip(), source="interview")
        return True
    elif field_type == "multi_select":
        if options:
            selected = [a.strip() for a in answer.split(",")]
            matched = []
            for s in selected:
                for opt in options:
                    if opt.lower() == s.lower():
                        matched.append(opt)
                        break
                else:
                    matched.append(s)  # allow custom values
            store.set(dotted_key, matched, source="interview")
            return True
        store.set(dotted_key, [a.strip() for a in answer.split(",")], source="interview")
        return True
    else:
        store.set(dotted_key, answer.strip(), source="interview")
        return True
