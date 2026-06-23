"""Open-ended answer generator — LLM-powered text responses using profile context."""

from __future__ import annotations

from typing import Any

from ..profile.store import ProfileStore


class OpenEndedGenerator:
    """Generate natural-sounding open-ended answers using the user profile as context."""

    def __init__(self, profile_store: ProfileStore, style_guide: dict[str, Any] | None = None):
        self._profile = profile_store
        self._style = style_guide or {
            "tone": "casual-professional",
            "length": "medium",  # short | medium | long
            "vocabulary": "everyday",
            "avoid": ["AI-sounding phrases", "overly formal language", "lists"],
        }

    def build_prompt(self, question: str) -> str:
        """Build an LLM prompt for generating an open-ended answer."""
        profile_context = self._profile.to_context_string()
        style = self._style

        length_guide = {
            "short": "1-2 sentences",
            "medium": "2-4 sentences",
            "long": "4-6 sentences",
        }.get(style.get("length", "medium"), "2-4 sentences")

        prompt = f"""You are answering a survey question as if you were the user. Use the profile information to answer accurately and consistently.

{profile_context}

Style guide:
- Tone: {style.get("tone", "casual-professional")}
- Length: {length_guide}
- Vocabulary: {style.get("vocabulary", "everyday")}
- Avoid: {", ".join(style.get("avoid", []))}

Survey question: {question}

Answer naturally as this person would, using details from their profile. Do not mention that you are an AI or that you are filling out a survey. Just answer the question directly."""

        return prompt

    def generate(self, question: str, llm_fn: Any = None) -> str:
        """Generate an answer for an open-ended question.

        Args:
            question: The survey question text.
            llm_fn: A callable that takes a prompt string and returns a response string.
                    If None, returns the prompt itself (for external LLM processing).

        Returns:
            The generated answer, or the prompt if no LLM function is provided.
        """
        prompt = self.build_prompt(question)
        if llm_fn:
            return llm_fn(prompt)
        return prompt

    def update_style(self, **kwargs) -> None:
        """Update the style guide."""
        self._style.update(kwargs)
