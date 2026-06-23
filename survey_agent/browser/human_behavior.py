"""Human behavior simulation — makes browser interactions look human."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import BrowserPage


class HumanBehavior:
    """Simulate human-like browser interactions."""

    def __init__(self, min_delay_ms: int = 300, max_delay_ms: int = 2000, seed: int | None = None):
        self._rng = random.Random(seed)
        self._min_delay = min_delay_ms / 1000
        self._max_delay = max_delay_ms / 1000
        self._fatigue_factor = 1.0  # increases over time
        self._actions_count = 0

    async def random_delay(self, multiplier: float = 1.0) -> None:
        """Wait a random human-like delay."""
        base = self._rng.uniform(self._min_delay, self._max_delay)
        delay = base * multiplier * self._fatigue_factor
        # Add some jitter
        jitter = self._rng.gauss(0, delay * 0.1)
        await asyncio.sleep(max(0.05, delay + jitter))

    async def human_type(self, page: BrowserPage, selector: str, text: str) -> None:
        """Type text with human-like speed variation and occasional pauses."""
        await page.click(selector)
        await self.random_delay(0.3)

        for i, char in enumerate(text):
            # Base typing speed: 50-150ms per character
            base_delay = self._rng.uniform(0.05, 0.15)

            # Occasional longer pause (thinking)
            if self._rng.random() < 0.05:
                base_delay += self._rng.uniform(0.3, 0.8)

            # Slight fatigue increase
            self._actions_count += 1
            if self._actions_count % 50 == 0:
                self._fatigue_factor = min(2.0, self._fatigue_factor + 0.05)

            await asyncio.sleep(base_delay)
            # Use keyboard to type individual characters
            await page.evaluate(f"document.querySelector('{selector}').focus()")
            await page.evaluate(f"""
                const el = document.querySelector('{selector}');
                const event = new InputEvent('input', {{data: '{char}', inputType: 'insertText'}});
                el.value += '{char}';
                el.dispatchEvent(event);
            """)

    async def human_click(self, page: BrowserPage, selector: str) -> None:
        """Click with a human-like mouse movement pattern."""
        # Small pre-click delay
        await self.random_delay(0.2)
        await page.click(selector)
        # Post-click delay
        await self.random_delay(0.3)

    async def human_select(self, page: BrowserPage, selector: str, value: str) -> None:
        """Select a dropdown option with human-like delay."""
        await self.random_delay(0.5)
        await page.select_option(selector, value)
        await self.random_delay(0.3)

    async def human_check(self, page: BrowserPage, selector: str) -> None:
        """Check a checkbox with human-like delay."""
        await self.random_delay(0.3)
        await page.check(selector)
        await self.random_delay(0.2)

    async def human_uncheck(self, page: BrowserPage, selector: str) -> None:
        """Uncheck a checkbox with human-like delay."""
        await self.random_delay(0.3)
        await page.uncheck(selector)
        await self.random_delay(0.2)

    async def scroll_naturally(self, page: BrowserPage, direction: str = "down", amount: int = 3) -> None:
        """Scroll with natural speed variation."""
        for _ in range(amount):
            scroll_px = self._rng.randint(100, 400)
            if direction == "up":
                scroll_px = -scroll_px
            await page.evaluate(f"window.scrollBy(0, {scroll_px})")
            await asyncio.sleep(self._rng.uniform(0.1, 0.4))

    async def read_pause(self, text_length: int = 100) -> None:
        """Pause for reading time based on text length (words per minute)."""
        words = text_length / 5  # avg word length
        wpm = self._rng.randint(200, 350)  # reading speed
        read_time = (words / wpm) * 60  # seconds
        await asyncio.sleep(min(read_time, 5.0))  # cap at 5 seconds

    def reset_fatigue(self) -> None:
        """Reset fatigue factor (e.g., for a new session)."""
        self._fatigue_factor = 1.0
        self._actions_count = 0
