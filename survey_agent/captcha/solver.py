"""CAPTCHA solver — tiered pipeline for solving survey CAPTCHAs."""

from __future__ import annotations

import json
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
    DRAG_DROP = "drag_drop"
    NUMBER_SELECT = "number_select"
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


class DragDropSolver(CaptchaTier):
    """Solve drag-and-drop CAPTCHAs by simulating mouse drag events via CDP."""

    def __init__(self, js_fn: Any = None):
        self._js_fn = js_fn  # callable(js_code) -> result

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:
        return challenge.captcha_type == CaptchaType.DRAG_DROP and self._js_fn is not None

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        if not self._js_fn:
            return CaptchaSolution(solved=False, error="No JS function configured")
        try:
            # Find draggable and drop target elements
            result = self._js_fn("""
                (() => {
                    const draggables = document.querySelectorAll('[draggable=true], [class*=drag], [class*=source]');
                    const targets = document.querySelectorAll('[class*=drop], [class*=target], [class*=zone]');
                    if (draggables.length === 0 || targets.length === 0) return null;
                    const drag = draggables[0];
                    const drop = targets[0];
                    const dragRect = drag.getBoundingClientRect();
                    const dropRect = drop.getBoundingClientRect();
                    return JSON.stringify({
                        dragX: dragRect.x + dragRect.width / 2,
                        dragY: dragRect.y + dragRect.height / 2,
                        dropX: dropRect.x + dropRect.width / 2,
                        dropY: dropRect.y + dropRect.height / 2
                    });
                })()
            """)
            if not result:
                return CaptchaSolution(solved=False, error="Could not find drag/drop elements")

            coords = json.loads(result)
            # The actual drag must be done via CDP mouse events at the browser level
            # Return coordinates for the caller to execute
            return CaptchaSolution(
                solved=True,
                answer=json.dumps(coords),
                method="drag_drop",
                metadata=coords,
            )
        except Exception as e:
            return CaptchaSolution(solved=False, error=str(e))


class NumberSelectSolver(CaptchaTier):
    """Solve number-select CAPTCHAs (pick the right number box from options)."""

    def __init__(self, llm_fn: Any = None, js_fn: Any = None):
        self._llm_fn = llm_fn
        self._js_fn = js_fn

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:
        return challenge.captcha_type == CaptchaType.NUMBER_SELECT and self._llm_fn is not None

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        if not self._llm_fn:
            return CaptchaSolution(solved=False, error="No LLM function configured")
        try:
            prompt = (
                "This is a number-select CAPTCHA. An image shows a number inside a box. "
                "Below are multiple numbered options. You must select the option that matches "
                "the number shown in the box.\n\n"
                f"Challenge description: {challenge.description}\n\n"
                "Reply with ONLY the number to select."
            )
            result = self._llm_fn(prompt, challenge.screenshot_bytes)
            return CaptchaSolution(solved=True, answer=result.strip(), method="number_select")
        except Exception as e:
            return CaptchaSolution(solved=False, error=str(e))


class CaptchaDetector:
    """Detect CAPTCHA elements from page DOM and text content."""

    @staticmethod
    def detect_from_page(page_text: str, forms_json: str) -> CaptchaChallenge | None:  # noqa: ARG004
        """Analyze page content and form elements to detect CAPTCHAs."""
        text_lower = page_text.lower()

        # Check for arithmetic CAPTCHAs
        math_pattern = re.search(r"(\d+)\s*([+\-×*÷/])\s*(\d+)\s*[=＝]", page_text)
        if math_pattern:
            return CaptchaChallenge(
                captcha_type=CaptchaType.ARITHMETIC,
                description=math_pattern.group(0),
            )

        # Check for "type the code" / "enter the text" patterns (text image CAPTCHA)
        # Only trigger when there's a clear CAPTCHA instruction, not just the word "code"
        text_captcha_patterns = [
            r"type\s+(?:the\s+)?(?:following\s+)?code\s+(?:in|into|below|shown)",
            r"enter\s+(?:the\s+)?(?:text|code|characters)\s+(?:from|in|shown|below|you see)",
            r"what\s+(?:does|is)\s+(?:the\s+)?(?:image|text|picture)\s+say",
            r"please\s+enter\s+the\s+(?:text|code)\s+(?:shown|in the image|below)",
            r"type\s+(?:the\s+)?(?:text|characters?)\s+(?:you\s+)?(?:see|shown in)",
            r"please\s+type\s+(?:the\s+)?(?:following|text)\s+(?:code|characters)",
            r"enter\s+(?:the\s+)?(?:characters?|text)\s+(?:from|in)\s+(?:the\s+)?(?:image|picture)",
            r"verification\s+code\s+(?:shown|in|below|from)",
            r"security\s+code\s+(?:shown|in|below)",
            r"type\s+(?:the\s+)?code\s+you\s+see",
        ]
        for pat in text_captcha_patterns:
            if re.search(pat, text_lower):
                return CaptchaChallenge(
                    captcha_type=CaptchaType.TEXT_CHALLENGE,
                    description="Text image CAPTCHA — needs OCR/vision to read",
                )

        # Check for drag-and-drop CAPTCHAs (must have explicit drag instruction AND draggable elements)
        drag_instructions = [
            r"drag\s+(?:the\s+)?(?:number|image|item|icon|box)\s+(?:to|into)",
            r"drop\s+(?:the\s+)?(?:number|image|item|icon|box)\s+(?:to|into|here)",
            r"drag\s+and\s+drop\s+(?:the\s+)?(?:number|image|item)",
            r"move\s+(?:the\s+)?(?:number|image|item)\s+(?:to|into)\s+(?:the\s+)?(?:box|zone|area|target)",
        ]
        for pat in drag_instructions:
            if re.search(pat, text_lower):
                return CaptchaChallenge(
                    captcha_type=CaptchaType.DRAG_DROP,
                    description="Drag-and-drop CAPTCHA",
                )

        # Check for number select
        if "select the" in text_lower and ("number" in text_lower or "box" in text_lower):
            return CaptchaChallenge(
                captcha_type=CaptchaType.NUMBER_SELECT,
                description="Number-select CAPTCHA — pick the matching number",
            )

        # Check for reCAPTCHA / hCaptcha — must have actual widget, not just text mentions
        has_recaptcha_widget = bool(re.search(r"(?:recaptcha|hcaptcha)(?:\.js|\.net|api)", text_lower))
        has_recaptcha_checkbox = 'class="g-recaptcha"' in page_text or "data-sitekey" in page_text
        # "I'm not a robot" only if there's an actual checkbox/iframe, not just survey text
        has_not_robot_widget = "i'm not a robot" in text_lower and (
            "iframe" in text_lower or "checkbox" in text_lower or "g-recaptcha" in page_text or "hcaptcha" in page_text
        )
        if has_recaptcha_widget or has_recaptcha_checkbox or has_not_robot_widget:
            return CaptchaChallenge(
                captcha_type=CaptchaType.RECAPTCHA,
                description="reCAPTCHA/hCaptcha — needs external solver",
            )

        # Check for image selection CAPTCHAs
        if "select all" in text_lower and ("image" in text_lower or "picture" in text_lower):
            return CaptchaChallenge(
                captcha_type=CaptchaType.IMAGE_SELECT,
                description="Image selection CAPTCHA",
            )

        return None


class LLMVisionSolver(CaptchaTier):
    """Solve CAPTCHAs using LLM vision (screenshot analysis)."""

    def __init__(self, llm_fn: Any = None):
        self._llm_fn = llm_fn  # callable(prompt, image_bytes) -> str

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:
        return (
            challenge.captcha_type
            in (
                CaptchaType.ARITHMETIC,
                CaptchaType.TEXT_CHALLENGE,
                CaptchaType.IMAGE_SELECT,
                CaptchaType.UNKNOWN,
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
    2. Drag-drop solver (coordinate-based)
    3. Number-select solver (vision-based)
    4. LLM vision (for image/text challenges)
    5. API-based solving (optional, requires external service)
    6. Manual fallback (alert user)
    """

    def __init__(self, llm_fn: Any = None, js_fn: Any = None, api_key: str = ""):
        self._tiers: list[CaptchaTier] = [
            ArithmeticSolver(),
            DragDropSolver(js_fn=js_fn),
            NumberSelectSolver(llm_fn=llm_fn, js_fn=js_fn),
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

    async def can_solve(self, challenge: CaptchaChallenge) -> bool:  # noqa: ARG002
        return challenge.captcha_type in (CaptchaType.HCAPTCHA, CaptchaType.RECAPTCHA)

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        # Placeholder for external CAPTCHA API integration
        return CaptchaSolution(
            solved=False,
            error=f"CAPTCHA API ({self._provider}) integration not yet implemented",
            method="api",
        )


def create_vision_llm_fn(provider: str = "groq", model: str = "", api_key: str = "", base_url: str = "") -> Any:
    """Create a vision LLM function for CAPTCHA solving.

    Returns a callable(prompt, image_bytes) -> str that sends images
    to a vision-capable LLM and returns the text response.

    Supports any OpenAI-compatible API with vision (Groq, OpenAI, etc.)
    """
    import base64  # noqa: PLC0415
    import os  # noqa: PLC0415

    api_key = api_key or os.environ.get("GROQ_AGENT_ZERO_API_KEY", "")

    if not api_key:
        return None

    # Model mapping for vision-capable models
    vision_models = {
        "groq": "llama-4-scout-17b-16e-instruct",  # Llama 4 Scout with vision
        "openai": "gpt-4o",
        "freemodel": "claude-haiku-4-5-20251001",
    }

    model = model or vision_models.get(provider, "llama-3.3-70b-versatile")

    # API endpoints
    endpoints = {
        "groq": "https://api.groq.com/openai/v1/chat/completions",
        "openai": "https://api.openai.com/v1/chat/completions",
        "freemodel": "https://api.freemodel.dev/v1/chat/completions",
    }
    endpoint = base_url or endpoints.get(provider, endpoints["groq"])

    def _vision_fn(prompt: str, image_bytes: bytes | None = None) -> str:
        import httpx  # noqa: PLC0415

        messages: list[dict] = []

        if image_bytes:
            # Encode image as base64
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            })
        else:
            messages.append({"role": "user", "content": prompt})

        resp = httpx.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 200,
                "temperature": 0.0,
            },
            timeout=30,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Vision LLM error ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    return _vision_fn
