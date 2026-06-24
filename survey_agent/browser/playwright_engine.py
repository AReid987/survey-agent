"""Playwright browser engine — primary browser automation implementation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Playwright, async_playwright
from playwright.async_api import BrowserContext as PwContext
from playwright.async_api import Page as PwPage
from playwright_stealth import Stealth

from .engine import BrowserContext, BrowserEngine, BrowserPage
from .fingerprint import Fingerprint, FingerprintGenerator
from .human_behavior import HumanBehavior

logger = logging.getLogger(__name__)

# Default directory for persistent browser sessions
SESSION_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser_session"


class PlaywrightPage(BrowserPage):
    """Playwright page wrapper."""

    def __init__(self, page: PwPage, behavior: HumanBehavior | None = None):
        self._page = page
        self._behavior = behavior or HumanBehavior()

    async def goto(self, url: str, wait_until: str = "networkidle") -> None:
        await self._page.goto(url, wait_until=wait_until)

    async def click(self, selector: str, timeout: float = 10) -> None:
        await self._behavior.human_click(self, selector)

    async def click_direct(self, selector: str, timeout: float = 10) -> None:
        await self._page.click(selector, timeout=timeout * 1000)

    async def fill(self, selector: str, text: str, timeout: float = 10) -> None:
        await self._behavior.human_type(self, selector, text)

    async def fill_direct(self, selector: str, text: str, timeout: float = 10) -> None:
        await self._page.fill(selector, text, timeout=timeout * 1000)

    async def select_option(self, selector: str, value: str, timeout: float = 10) -> None:
        await self._behavior.human_select(self, selector, value)

    async def check(self, selector: str, timeout: float = 10) -> None:
        await self._behavior.human_check(self, selector)

    async def uncheck(self, selector: str, timeout: float = 10) -> None:
        await self._behavior.human_uncheck(self, selector)

    async def screenshot(self, path: str | Path | None = None) -> bytes:
        kwargs = {}
        if path:
            kwargs["path"] = str(path)
        return await self._page.screenshot(**kwargs)

    async def query_selector(self, selector: str) -> Any | None:
        return await self._page.query_selector(selector)

    async def query_selector_all(self, selector: str) -> list[Any]:
        return await self._page.query_selector_all(selector)

    async def evaluate(self, expression: str) -> Any:
        return await self._page.evaluate(expression)

    async def wait_for_selector(self, selector: str, timeout: float = 10) -> None:
        await self._page.wait_for_selector(selector, timeout=timeout * 1000)

    async def url(self) -> str:
        return self._page.url

    async def content(self) -> str:
        return await self._page.content()

    async def close(self) -> None:
        await self._page.close()

    @property
    def raw_page(self) -> PwPage:
        return self._page


class PlaywrightContext(BrowserContext):
    """Playwright browser context wrapper."""

    def __init__(self, context: PwContext, fingerprint: Fingerprint, behavior: HumanBehavior | None = None):
        self._context = context
        self._fingerprint = fingerprint
        self._behavior = behavior or HumanBehavior()

    async def new_page(self) -> PlaywrightPage:
        page = await self._context.new_page()
        return PlaywrightPage(page, self._behavior)

    async def close(self) -> None:
        await self._context.close()

    @property
    def fingerprint(self) -> Fingerprint:
        return self._fingerprint

    @property
    def raw_context(self) -> PwContext:
        return self._context


class PlaywrightEngine(BrowserEngine):
    """Playwright browser engine with stealth, fingerprint rotation, and persistent sessions.

    When `persistent=True`, uses a persistent browser context that saves cookies,
    localStorage, and session data. This allows Google OAuth sessions to persist
    across runs — the user only needs to sign in once.
    """

    def __init__(self, persistent: bool = True, session_dir: str | Path | None = None):
        self._pw_cm = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._persistent_context: PwContext | None = None
        self._fingerprint_gen = FingerprintGenerator()
        self._persistent = persistent
        self._session_dir = Path(session_dir) if session_dir else SESSION_DIR

    async def launch(self, headless: bool = True, **kwargs) -> None:
        self._pw_cm = async_playwright()
        self._playwright = await self._pw_cm.__aenter__()

        if self._persistent:
            # Persistent context: saves cookies/localStorage to disk
            self._session_dir.mkdir(parents=True, exist_ok=True)
            fingerprint = self._fingerprint_gen.generate()
            ctx_kwargs = fingerprint.to_context_kwargs()
            ctx_kwargs.update(kwargs)

            self._persistent_context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._session_dir),
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
                **ctx_kwargs,
            )

            # Apply stealth to the persistent context
            stealth = Stealth()
            await stealth.apply_stealth_async(self._persistent_context)
            logger.info(f"Persistent browser session at: {self._session_dir}")
        else:
            # Standard ephemeral browser
            stealth = Stealth()
            self._browser = await self._playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
                **kwargs,
            )

    async def create_context(self, fingerprint: Fingerprint | None = None, **kwargs) -> PlaywrightContext:
        if self._persistent and self._persistent_context:
            # Persistent mode: return the persistent context
            behavior = HumanBehavior()
            fp = fingerprint or self._fingerprint_gen.generate()
            return PlaywrightContext(self._persistent_context, fp, behavior)

        if not self._browser:
            raise RuntimeError("Browser not launched. Call launch() first.")

        if fingerprint is None:
            fingerprint = self._fingerprint_gen.generate()

        context_kwargs = fingerprint.to_context_kwargs()
        context_kwargs.update(kwargs)

        context = await self._browser.new_context(**context_kwargs)

        stealth = Stealth()
        await stealth.apply_stealth_async(context)

        behavior = HumanBehavior()
        return PlaywrightContext(context, fingerprint, behavior)

    async def close(self) -> None:
        if self._persistent_context:
            await self._persistent_context.close()
            self._persistent_context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw_cm:
            await self._pw_cm.__aexit__(None, None, None)
            self._pw_cm = None
            self._playwright = None

    def is_running(self) -> bool:
        if self._persistent:
            return self._persistent_context is not None
        return self._browser is not None and self._browser.is_connected()
