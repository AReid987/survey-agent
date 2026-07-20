"""Abstract browser backend interface for survey automation.

All browser backends (browser-use, browser-harness, future ones) implement
this interface. The orchestrator only talks to this ABC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AgentResult:
    """Result from a browser backend operation."""

    success: bool
    output: str = ""
    error: str = ""
    url: str = ""


class SurveyAgentBackend(ABC):
    """Abstract survey agent backend.

    Each backend controls a browser differently:
    - BrowserUseBackend: LLM agent generates JSON actions, browser-use executes
    - BrowserHarnessBackend: LLM generates Python code, runs in real Chrome via CDP
    """

    @abstractmethod
    async def start(self) -> None:
        """Initialize the browser and any required services."""

    @abstractmethod
    async def stop(self) -> None:
        """Shut down the browser and clean up resources."""

    @abstractmethod
    async def login(self, site_url: str, site_name: str) -> AgentResult:
        """Log into a survey site.

        Args:
            site_url: The site's base URL or login page URL.
            site_name: Human-readable site name for logging.

        Returns:
            AgentResult with success=True if logged in, or error details.
        """

    @abstractmethod
    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        """Find available surveys on the dashboard.

        Returns:
            AgentResult with output containing survey descriptions, or "no surveys".
        """

    @abstractmethod
    async def complete_survey(
        self,
        survey_info: str,
        profile_context: str,
    ) -> AgentResult:
        """Complete a survey by answering questions.

        Args:
            survey_info: Description of the survey to complete.
            profile_context: User profile data for answering questions.

        Returns:
            AgentResult indicating success/failure.
        """

    @abstractmethod
    async def check_balance(self, dashboard_url: str) -> AgentResult:
        """Check account balance on the dashboard.

        Returns:
            AgentResult with output containing balance info.
        """

    @abstractmethod
    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        """Withdraw earnings.

        Args:
            redeem_url: URL for the redemption/withdrawal page.
            method: Payment method (e.g. "paypal").

        Returns:
            AgentResult indicating success/failure.
        """

    async def get_page(self) -> Any:
        """Get the current browser page for direct manipulation.

        Override in backends that expose direct page access (e.g. CamoufoxBackend).
        Default raises NotImplementedError.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support direct page access")

    async def take_screenshot(self, path: str | Path) -> bytes:
        """Take a screenshot of the current page. Override in supporting backends."""
        raise NotImplementedError(f"{type(self).__name__} does not support screenshots")
