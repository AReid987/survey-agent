"""Session orchestrator — agent-driven survey automation with recovery."""

from __future__ import annotations

import asyncio
import logging

from .browser.agent_controller import SurveyAgent
from .config import DATA_DIR, AppConfig, load_config
from .profile.store import ProfileStore

logger = logging.getLogger(__name__)


# Site definitions
SITES = {
    "branded-surveys": {
        "name": "Branded Surveys",
        "url": "https://surveys.gobranded.com",
        "dashboard": "https://surveys.gobranded.com/users/dashboard",
    },
    "surveypop": {
        "name": "SurveyPop",
        "url": "https://surveypop.com",
        "dashboard": "https://surveypop.com/surveys",
    },
    "qmee": {
        "name": "Qmee",
        "url": "https://www.qmee.com/en-us",
        "dashboard": "https://www.qmee.com/en-us/surveys",
    },
}


class SurveySession:
    """Agent-driven survey session with resilience."""

    def __init__(  # noqa: PLR0913
        self,
        agent: SurveyAgent,
        site_slug: str,
        site_info: dict,
        profile: ProfileStore,
        config: AppConfig,
        session_id: int = 0,
    ):
        self._agent = agent
        self._site_slug = site_slug
        self._site = site_info
        self._profile = profile
        self._config = config
        self._session_id = session_id
        self._stats = {"surveys_completed": 0, "questions_answered": 0, "captchas_solved": 0, "errors": 0}

    async def run(self, max_surveys: int = 10) -> dict:
        """Run the survey session with full resilience."""
        logger.info(f"[Session {self._session_id}] Starting on {self._site['name']}")

        try:
            # Login
            login_result = await self._agent.login(self._site["url"], self._site["name"])
            if not login_result.success:
                return {"error": f"Login failed: {login_result.error}", "stats": self._stats}

            logger.info(f"[Session {self._session_id}] Logged in")

            # Survey loop
            for i in range(max_surveys):
                # Discover surveys
                discover = await self._agent.discover_surveys(self._site["dashboard"])
                if not discover.success or "no surveys" in (discover.output or "").lower():
                    logger.info(f"[Session {self._session_id}] No more surveys")
                    break

                # Complete survey
                profile_ctx = self._profile.to_context_string()
                result = await self._agent.complete_survey(discover.output, profile_ctx)

                if result.success:
                    self._stats["surveys_completed"] += 1
                    logger.info(f"[Session {self._session_id}] Survey {i + 1} completed")
                elif "captcha" in (result.output or "").lower():
                    self._stats["captchas_solved"] += 1
                else:
                    self._stats["errors"] += 1
                    logger.warning(f"[Session {self._session_id}] Survey failed: {result.output[:200]}")

                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"[Session {self._session_id}] Error: {e}", exc_info=True)
            return {"error": str(e), "stats": self._stats}

        return {"stats": self._stats}


class SessionOrchestrator:
    """Manages agent-driven survey sessions."""

    def __init__(self, max_sessions: int = 1, config: AppConfig | None = None, headless: bool = False):
        self._config = config or load_config()
        self._max_sessions = max_sessions
        self._headless = headless
        self._profile = ProfileStore(DATA_DIR / "profile.json")

    def set_profile(self, profile_path: str) -> None:
        self._profile = ProfileStore(profile_path)

    async def run(self, sites: list[str] | None = None, max_surveys: int = 10) -> list[dict]:
        """Run survey sessions."""
        if sites is None:
            sites = list(SITES.keys())

        results = []
        for site_slug in sites:
            if site_slug not in SITES:
                results.append({"error": f"Unknown site: {site_slug}", "stats": {}})
                continue

            site_info = SITES[site_slug]
            logger.info(f"Starting agent for {site_info['name']}...")

            agent = SurveyAgent(
                llm_provider=self._config.llm_provider,
                llm_model=self._config.llm_model or None,
                llm_api_key=self._config.llm_api_key or None,
                llm_base_url=self._config.llm_base_url or None,
                headless=self._headless,
                google_email=self._config.account.google_email,
                paypal_email=self._config.account.paypal_email,
            )

            try:
                await agent.start()
                session = SurveySession(
                    agent=agent,
                    site_slug=site_slug,
                    site_info=site_info,
                    profile=self._profile,
                    config=self._config,
                )
                results.append(await session.run(max_surveys=max_surveys))
            except Exception as e:
                logger.error(f"Error for {site_slug}: {e}", exc_info=True)
                results.append({"error": str(e), "stats": {}})
            finally:
                await agent.stop()

        return results
