"""CAPTCHA solver — tiered pipeline for solving survey CAPTCHAs."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class CaptchaType(StrEnum):
    ARITHMETIC = "arithmetic"
    IMAGE_SLIDER = "image_slider"
    IMAGE_SELECT = "image_select"
    TEXT_CHALLENGE = "text_challenge"
    HCAPTCHA = "hcaptcha"
    RECAPTCHA = "recaptcha"
    UNKNOWN = "unknown"


@dataclass
class CaptchaChallenge:
    """A detected CAPTCHA challenge."""

    captcha_type: CaptchaType
    description: str
    screenshot_bytes: bytes | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class CaptchaSolution:
    """A solution to a CAPTCHA challenge."""

    solved: bool
    answer: str | None = None
    method: str = ""  # llm_vision | api | manual
    error: str | None = None


class CaptchaTier(ABC):
    """Abstract CAPTCHA solving tier."""

    @abstractmethod
    async def can_solve(self, challenge: CaptchaChallenge) -> bool:
        """Check if this tier can handle the challenge type."""
        ...

    @abstractmethod
    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        """Attempt to solve the challenge."""
        ...


class ArithmeticSolver(CaptchaTier):
    """Solve arithmetic CAPTCHAs (e.g. '10 - 10 = __')."""

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:
        return challenge.captcha_type == CaptchaType.ARITHMETIC

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        """Parse and solve arithmetic expressions."""
        text = challenge.description
        # Extract the expression
        expr_match = re.search(r"(\d+)\s*([\+\-\*\/×÷])\s*(\d+)", text)
        if not expr_match:
            return CaptchaSolution(solved=False, error="Could not parse arithmetic expression")

        a = int(expr_match.group(1))
        op = expr_match.group(2)
        b = int(expr_match.group(3))

        ops = {"+": a + b, "-": a - b, "*": a * b, "×": a * b, "/": a // b if b else 0, "÷": a // b if b else 0}
        result = ops.get(op)
        if result is None:
            return CaptchaSolution(solved=False, error=f"Unknown operator: {op}")

        return CaptchaSolution(solved=True, answer=str(int(result)), method="arithmetic")


class LLMVisionSolver(CaptchaTier):
    """Solve CAPTCHAs using LLM vision (screenshot analysis)."""

    def __init__(self, llm_fn: Any = None):
        self._llm_fn = llm_fn  # callable(prompt, image_bytes) -> str

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:  # noqa: ARG002 -> bool:
        return (
            challenge.captcha_type
            in (
                CaptchaType.ARITHMETIC,
                CaptchaType.TEXT_CHALLENGE,
                CaptchaType.IMAGE_SELECT,
            )
            and self._llm_fn is not None
        )

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        if not self._llm_fn:
            return CaptchaSolution(solved=False, error="No LLM function configured")

        prompt = f"""You are solving a CAPTCHA challenge. Look at the image and provide ONLY the answer, nothing else.

Challenge type: {challenge.captcha_type.value}
Description: {challenge.description}

What is the answer? Reply with just the answer text."""

        try:
            result = self._llm_fn(prompt, challenge.screenshot_bytes)
            return CaptchaSolution(solved=True, answer=result.strip(), method="llm_vision")
        except Exception as e:
            return CaptchaSolution(solved=False, error=str(e))


class ManualVNCFallback(CaptchaTier):
    """Manual CAPTCHA solving via VNC — alert user and wait for manual intervention."""

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:  # noqa: ARG002 -> bool:
        return True  # always available as last resort

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        # In a real implementation, this would:
        # 1. Send a text/alert to the user
        # 2. Provide a VNC link
        # 3. Wait for manual resolution
        # 4. Record the session for future training
        return CaptchaSolution(
            solved=False,
            error="Manual intervention required. Implement VNC alert integration.",
            method="manual",
        )


class CaptchaSolver:
    """Tiered CAPTCHA solving pipeline.

    Tries each tier in order until one succeeds:
    1. Arithmetic solver (for simple math challenges)
    2. LLM vision (for image/text challenges)
    3. API-based solving (optional, requires external service)
    4. Manual VNC fallback (alert user)
    """

    def __init__(self, llm_fn: Any = None, api_key: str = ""):
        self._tiers: list[CaptchaTier] = [
            ArithmeticSolver(),
            LLMVisionSolver(llm_fn=llm_fn),
        ]
        if api_key:
            self._tiers.append(APICaptchaSolver(api_key=api_key))
        self._tiers.append(ManualVNCFallback())

    @property
    def tiers(self) -> list[str]:
        """List of active solving tiers."""
        names = []
        for t in self._tiers:
            names.append(t.__class__.__name__.replace("Solver", "").replace("Fallback", "").lower())
        return names

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        """Attempt to solve a CAPTCHA challenge using the tiered pipeline."""
        for tier in self._tiers:
            if await tier.can_solve(challenge):
                result = await tier.solve(challenge)
                if result.solved:
                    return result
        return CaptchaSolution(solved=False, error="No tier could solve the challenge")


class APICaptchaSolver(CaptchaTier):
    """Solve CAPTCHAs using an external API (2captcha, CapSolver, etc.)."""

    def __init__(self, api_key: str, provider: str = "2captcha"):
        self._api_key = api_key
        self._provider = provider

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:  # noqa: ARG002 -> bool:
        return challenge.captcha_type in (CaptchaType.HCAPTCHA, CaptchaType.RECAPTCHA)

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        # Placeholder for external CAPTCHA API integration
        return CaptchaSolution(
            solved=False,
            error=f"CAPTCHA API ({self._provider}) integration not yet implemented",
            method="api",
        )
