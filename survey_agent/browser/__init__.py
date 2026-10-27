"""Browser engine module — pluggable backend system.

Four backends available:
- browser-use: LLM agent + Playwright (default, works headless)
- browser-harness: Real Chrome via CDP (zero bot detection, needs Chrome open)
- camoufox: Stealth Firefox via Camoufox (best anti-detection, headless)
- cloakbrowser: Stealth Chromium via Playwright API (drop-in Playwright replacement,
              passes 30/30 bot detection tests, bypasses Cloudflare/Turnstile)
"""

from .backend import AgentResult, SurveyAgentBackend
from .engine import BrowserContext, BrowserEngine, BrowserPage
from .fingerprint import Fingerprint, FingerprintGenerator
from .human_behavior import HumanBehavior

BACKENDS = {
    "browser-use",
    "browser-harness",
    "camoufox",
    "cloakbrowser",
    "ego-lite",
    "playwright",
    "cua",
    "agent-browser",
}


def create_backend(
    backend: str = "browser-use",
    **kwargs,
) -> SurveyAgentBackend:
    """Create a browser backend by name.

    Args:
        backend: One of "browser-use", "browser-harness", "camoufox", "cloakbrowser", "cua", "agent-browser".
        **kwargs: Passed to the backend constructor (llm_provider, headless, etc.)

    Returns:
        A SurveyAgentBackend instance.

    Raises:
        ValueError: If backend name is unknown.
    """
    backend = backend.lower().strip()

    if backend == "browser-use":
        from .agent_controller import BrowserUseBackend  # noqa: PLC0415

        return BrowserUseBackend(**kwargs)

    if backend == "browser-harness":
        from .harness_backend import BrowserHarnessBackend  # noqa: PLC0415

        return BrowserHarnessBackend(**kwargs)

    if backend == "camoufox":
        from .camoufox_backend import CamoufoxBackend  # noqa: PLC0415

        return CamoufoxBackend(**kwargs)

    if backend == "cloakbrowser":
        from .cloakbrowser_backend import CloakBrowserBackend  # noqa: PLC0415

        return CloakBrowserBackend(**kwargs)

    if backend == "ego-lite":
        from .ego_backend import EgoLiteBackend  # noqa: PLC0415

        return EgoLiteBackend(**kwargs)

    if backend == "playwright":
        from .playwright_backend import PlaywrightBackend  # noqa: PLC0415

        return PlaywrightBackend(**kwargs)

    if backend in ("cua", "cua-driver"):
        from .cua_backend import CuaBackend  # noqa: PLC0415

        return CuaBackend(**kwargs)

    if backend == "agent-browser":
        from .agent_browser_backend import AgentBrowserBackend  # noqa: PLC0415

        return AgentBrowserBackend(**kwargs)

    raise ValueError(f"Unknown browser backend: {backend!r}. Available: {BACKENDS}")


__all__ = [
    "AgentResult",
    "SurveyAgentBackend",
    "BrowserContext",
    "BrowserEngine",
    "BrowserPage",
    "Fingerprint",
    "FingerprintGenerator",
    "HumanBehavior",
    "BACKENDS",
    "create_backend",
]
