"""Fingerprint generation — consistent browser fingerprints for anti-detection."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

# Realistic viewport distributions (weighted by real-world usage)
VIEWPORTS = [
    (1920, 1080),
    (1920, 1080),
    (1920, 1080),  # most common
    (1366, 768),
    (1366, 768),
    (1536, 864),
    (1440, 900),
    (1280, 720),
    (2560, 1440),
    (1680, 1050),
    (3840, 2160),
    (1280, 1024),
    (1600, 900),
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
]

LOCALES = ["en-US", "en-US", "en-US", "en-US", "en-GB", "en-CA", "en-AU"]

TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Anchorage",
    "Europe/London",
    "Europe/Berlin",
]

LANGUAGES = [
    ("en-US", "en"),
    ("en-US", "en"),
    ("en-GB", "en"),
    ("en-CA", "en"),
    ("en-AU", "en"),
]

# WebGL renderer strings that match common GPUs
WEBGL_RENDERERS = {
    "windows": [
        "ANGLE (NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0)",
        "ANGLE (NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
        "ANGLE (Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0)",
        "ANGLE (AMD Radeon RX 7800 XT Direct3D11 vs_5_0 ps_5_0)",
        "ANGLE (Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)",
    ],
    "mac": [
        "Apple GPU",
        "Apple M1 GPU",
        "Apple M2 GPU",
        "Apple M3 GPU",
        "ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics 645, OpenGL 4.1)",
    ],
    "linux": [
        "ANGLE (NVIDIA GeForce GTX 1660 SUPER, OpenGL 4.5.0)",
        "Mesa Intel(R) UHD Graphics 630 (CFL GT2)",
        "AMD Radeon RX 580 Series (polaris10, LLVM 15.0.7, DRM 3.49, 6.1.0)",
    ],
}

WEBGL_VENDORS = ["Google Inc. (NVIDIA)", "Google Inc. (Intel)", "Google Inc. (AMD)", "Apple", "Google Inc. (Apple)"]

HARDWARE_CONCURRENCY = [2, 4, 4, 8, 8, 8, 12, 16]
DEVICE_MEMORY = [2, 4, 4, 8, 8, 8, 16, 32]


@dataclass
class Fingerprint:
    """A consistent browser fingerprint."""

    viewport: tuple[int, int]
    user_agent: str
    locale: str
    timezone: str
    languages: tuple[str, str]
    webgl_renderer: str
    webgl_vendor: str
    hardware_concurrency: int
    device_memory: int
    color_scheme: str = "light"
    reduced_motion: str = "no-preference"
    platform: str = ""

    def is_consistent(self) -> bool:
        """Check if the fingerprint is internally consistent."""
        # Windows UA should have Windows platform
        if "Windows" in self.user_agent and self.platform and self.platform != "Win32":
            return False
        # Mac UA should have Mac platform
        if "Macintosh" in self.user_agent and self.platform and self.platform != "MacIntel":
            return False
        # Linux UA should have Linux platform
        return not (
            "Linux" in self.user_agent
            and "Android" not in self.user_agent
            and self.platform
            and self.platform != "Linux x86_64"
        )

    def to_context_kwargs(self) -> dict[str, Any]:
        """Convert to Playwright context kwargs."""
        return {
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "color_scheme": self.color_scheme,
            "reduced_motion": self.reduced_motion,
        }


def _detect_os(ua: str) -> str:
    if "Windows" in ua:
        return "windows"
    elif "Macintosh" in ua or "Mac OS" in ua:
        return "mac"
    return "linux"


def _detect_platform(ua: str) -> str:
    if "Windows" in ua:
        return "Win32"
    elif "Macintosh" in ua or "Mac OS" in ua:
        return "MacIntel"
    return "Linux x86_64"


class FingerprintGenerator:
    """Generate consistent, realistic browser fingerprints."""

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def generate(self) -> Fingerprint:
        """Generate a new random fingerprint."""
        ua = self._rng.choice(USER_AGENTS)
        os_type = _detect_os(ua)
        platform = _detect_platform(ua)
        viewport = self._rng.choice(VIEWPORTS)
        locale = self._rng.choice(LOCALES)
        timezone = self._rng.choice(TIMEZONES)
        languages = self._rng.choice(LANGUAGES)
        renderer = self._rng.choice(WEBGL_RENDERERS[os_type])
        vendor = self._rng.choice(WEBGL_VENDORS)
        concurrency = self._rng.choice(HARDWARE_CONCURRENCY)
        memory = self._rng.choice(DEVICE_MEMORY)

        return Fingerprint(
            viewport=viewport,
            user_agent=ua,
            locale=locale,
            timezone=timezone,
            languages=languages,
            webgl_renderer=renderer,
            webgl_vendor=vendor,
            hardware_concurrency=concurrency,
            device_memory=memory,
            platform=platform,
        )
