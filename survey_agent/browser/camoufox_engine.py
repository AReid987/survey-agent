"""Camoufox browser engine — wraps AsyncCamoufox with BrowserEngine/BrowserContext/BrowserPage ABCs.

Uses Camoufox's patched Firefox for stealth. All Playwright Page methods
are thin pass-throughs so site adapters work unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from camoufox.async_api import AsyncCamoufox

from .engine import BrowserContext, BrowserEngine, BrowserPage

logger = logging.getLogger(__name__)

SESSION_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "camoufox_session"


class CamoufoxPage(BrowserPage):
    """Wraps a Playwright Page returned by Camoufox."""

    def __init__(self, page: Any) -> None:
        self._page = page

    async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int | None = None) -> None:
        await self._page.goto(url, wait_until=wait_until, timeout=timeout if timeout is not None else 30_000)

    async def click(self, selector: str, timeout: float = 10) -> None:
        await self._page.click(selector, timeout=int(timeout * 1000))

    async def fill(self, selector: str, text: str, timeout: float = 10) -> None:
        await self._page.fill(selector, text, timeout=int(timeout * 1000))

    async def select_option(self, selector: str, value: str, timeout: float = 10) -> None:
        await self._page.select_option(selector, value=value, timeout=int(timeout * 1000))

    async def check(self, selector: str, timeout: float = 10) -> None:
        await self._page.check(selector, timeout=int(timeout * 1000))

    async def uncheck(self, selector: str, timeout: float = 10) -> None:
        await self._page.uncheck(selector, timeout=int(timeout * 1000))

    async def screenshot(self, path: str | Path | None = None, **kwargs) -> bytes:
        """Take a screenshot. Accepts any Playwright screenshot kwargs."""
        kwargs.setdefault("full_page", True)
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
        await self._page.wait_for_selector(selector, timeout=int(timeout * 1000))

    async def url(self) -> str:
        return self._page.url

    async def content(self) -> str:
        return await self._page.content()

    async def wait_for_load(self, state: str = "domcontentloaded", timeout: float = 30_000) -> None:
        """Wait for page load. Used by site adapters."""
        await self._page.wait_for_load_state(state, timeout=int(timeout))

    async def close(self) -> None:
        await self._page.close()


class CamoufoxBrowserContext(BrowserContext):
    """Wraps a Camoufox browser instance (which acts as both browser and context)."""

    def __init__(self, browser_instance: Any) -> None:
        self._browser = browser_instance

    async def new_page(self) -> CamoufoxPage:
        page = await self._browser.new_page()
        return CamoufoxPage(page)

    async def close(self) -> None:
        # Browser lifecycle managed by CamoufoxEngine
        pass


class CamoufoxEngine(BrowserEngine):
    """Browser engine backed by Camoufox (stealth Firefox via Playwright).

    Uses AsyncCamoufox as a manual async context manager (launch/close pattern)
    so the engine can live across multiple async operations.
    """

    def __init__(self, headless: bool = True, persistent: bool = False, user_data_dir: str | Path | None = None) -> None:
        self._headless = headless
        self._persistent = persistent
        self._user_data_dir = str(user_data_dir) if user_data_dir else None
        self._cm: AsyncCamoufox | None = None
        self._browser: Any = None
        self._context: CamoufoxBrowserContext | None = None

    async def launch(self, headless: bool = True, **kwargs) -> None:
        self._headless = headless
        logger.info(f"Launching Camoufox (headless={headless}, persistent={self._persistent})")
        launch_kwargs: dict[str, Any] = {"headless": self._headless, **kwargs}
        if self._persistent and self._user_data_dir:
            Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
            launch_kwargs["persistent_context"] = True
            launch_kwargs["user_data_dir"] = self._user_data_dir
            logger.info(f"Persistent profile: {self._user_data_dir}")
        self._cm = AsyncCamoufox(**launch_kwargs)
        self._browser = await self._cm.__aenter__()
        self._context = CamoufoxBrowserContext(self._browser)
        logger.info("Camoufox launched OK")

    async def create_context(self, **kwargs) -> CamoufoxBrowserContext:
        if not self._context:
            raise RuntimeError("CamoufoxEngine not launched — call launch() first")
        return self._context

    async def close(self) -> None:
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                logger.warning("Error closing Camoufox", exc_info=True)
            finally:
                self._cm = None
                self._browser = None
                self._context = None

    def is_running(self) -> bool:
        return self._browser is not None
