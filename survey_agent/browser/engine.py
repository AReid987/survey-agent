"""Browser engine — abstract interface for browser automation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BrowserEngine(ABC):
    """Abstract browser engine interface.

    Implementations wrap a specific browser automation library (Playwright, Camoufox, etc.)
    and provide a consistent API for site adapters.
    """

    @abstractmethod
    async def launch(self, headless: bool = True, **kwargs) -> None:
        """Launch the browser."""
        ...

    @abstractmethod
    async def create_context(self, **kwargs) -> BrowserContext:
        """Create a new browser context with its own fingerprint/session."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the browser and all contexts."""
        ...

    @abstractmethod
    def is_running(self) -> bool:
        """Check if the browser is currently running."""
        ...


class BrowserContext(ABC):
    """Abstract browser context (isolated session)."""

    @abstractmethod
    async def new_page(self) -> BrowserPage:
        """Create a new page/tab in this context."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close this context."""
        ...


class BrowserPage(ABC):
    """Abstract browser page with common operations."""

    @abstractmethod
    async def goto(self, url: str, wait_until: str = "networkidle") -> None:
        """Navigate to a URL."""
        ...

    @abstractmethod
    async def click(self, selector: str, timeout: float = 10) -> None:
        """Click an element."""
        ...

    @abstractmethod
    async def fill(self, selector: str, text: str, timeout: float = 10) -> None:
        """Fill a text input."""
        ...

    @abstractmethod
    async def select_option(self, selector: str, value: str, timeout: float = 10) -> None:
        """Select an option from a dropdown."""
        ...

    @abstractmethod
    async def check(self, selector: str, timeout: float = 10) -> None:
        """Check a checkbox."""
        ...

    @abstractmethod
    async def uncheck(self, selector: str, timeout: float = 10) -> None:
        """Uncheck a checkbox."""
        ...

    @abstractmethod
    async def screenshot(self, path: str | Path | None = None) -> bytes:
        """Take a screenshot. Returns PNG bytes."""
        ...

    @abstractmethod
    async def query_selector(self, selector: str) -> Any | None:
        """Find an element by selector."""
        ...

    @abstractmethod
    async def query_selector_all(self, selector: str) -> list[Any]:
        """Find all elements matching a selector."""
        ...

    @abstractmethod
    async def evaluate(self, expression: str) -> Any:
        """Evaluate JavaScript in the page."""
        ...

    @abstractmethod
    async def wait_for_selector(self, selector: str, timeout: float = 10) -> None:
        """Wait for an element to appear."""
        ...

    @abstractmethod
    async def url(self) -> str:
        """Get the current page URL."""
        ...

    @abstractmethod
    async def content(self) -> str:
        """Get the page HTML content."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close this page."""
        ...
