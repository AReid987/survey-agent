"""Browser engine module."""

from .engine import BrowserContext, BrowserEngine, BrowserPage
from .fingerprint import Fingerprint, FingerprintGenerator
from .human_behavior import HumanBehavior


# Lazy import to avoid requiring playwright at import time
def get_playwright_engine():
    from .playwright_engine import PlaywrightEngine  # noqa: PLC0415

    return PlaywrightEngine


__all__ = [
    "BrowserEngine",
    "BrowserContext",
    "BrowserPage",
    "get_playwright_engine",
    "Fingerprint",
    "FingerprintGenerator",
    "HumanBehavior",
]
