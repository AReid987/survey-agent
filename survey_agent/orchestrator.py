"""Session orchestrator — agent-driven survey automation with recovery."""

from __future__ import annotations

import asyncio
import logging

from .browser import SurveyAgentBackend, create_backend
from .config import DATA_DIR, AppConfig, load_config
from .credentials import CredentialStore
from .profile.store import ProfileStore

try:
    from . import brain
except ImportError:
    brain = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# Site definitions
SITES = {
    "branded-surveys": {
        "name": "Branded Surveys",
        "url": "https://surveys.gobranded.com",
        "dashboard": "https://surveys.gobranded.com/users/dashboard",
    },
    "prime-opinion": {
        "name": "Prime Opinion",
        "url": "https://app.primeopinion.com",
        "dashboard": "https://app.primeopinion.com/",
    },
    "surveypop": {
        "name": "SurveyPop",
        "url": "https://surveypop.com",
        "dashboard": "https://surveypop.com/dashboard",
    },
    "qmee": {
        "name": "Qmee",
        "url": "https://www.qmee.com/en-us",
        "dashboard": "https://www.qmee.com/en-us/surveys",
    },
    "eurekasurveys": {
        "name": "EurekaSurveys",
        "url": "https://eurekasurveys.com",
        "dashboard": "https://eurekasurveys.com/surveys",
    },
    "surveyspin": {
        "name": "SurveySpin",
        "url": "https://surveyspin.com",
        "dashboard": "https://surveyspin.com/surveys",
    },
    "yougov": {
        "name": "YouGov",
        "url": "https://account.yougov.com",
        "dashboard": "https://account.yougov.com/us-en/account",
    },
    "attapoll": {
        "name": "Attapoll",
        "url": "https://earn.attapoll.app",
        "dashboard": "https://earn.attapoll.app/#projects",
    },
}


class SurveySession:
    """Agent-driven survey session with resilience."""

    def __init__(  # noqa: PLR0913
        self,
        agent: SurveyAgentBackend,
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

    async def run(self, max_surveys: int = 10) -> dict:  # noqa: PLR0915
        """Run the survey session with full resilience."""
        logger.info(f"[Session {self._session_id}] Starting on {self._site['name']}")

        try:
            # Check if backend supports run_full_session (browser-use)
            if hasattr(self._agent, 'run_full_session'):
                profile_ctx = self._profile.to_context_string()
                result = await self._agent.run_full_session(
                    site_url=self._site["url"],
                    site_name=self._site["name"],
                    dashboard_url=self._site["dashboard"],
                    profile_context=profile_ctx,
                    max_surveys=max_surveys,
                )
                if result.success:
                    self._stats["surveys_completed"] = max_surveys
                    logger.info(f"[Session {self._session_id}] Full session completed: {result.output[:300]}")
                else:
                    self._stats["errors"] += 1
                    logger.warning(f"[Session {self._session_id}] Session failed: {result.error or result.output}")
                return {"stats": self._stats}

            # Login (for backends without run_full_session)
            login_result = await self._agent.login(self._site["url"], self._site["name"])
            if not login_result.success:
                if brain:
                    try:
                        brain.record(
                            site=self._site_slug,
                            event_type="login_failed",
                            context={"error": login_result.error or "unknown"},
                            session_id=str(self._session_id),
                        )
                    except Exception:
                        logger.exception("brain.record(login_failed) failed")
                return {"error": f"Login failed: {login_result.error}", "stats": self._stats}

            logger.info(f"[Session {self._session_id}] Logged in")

            # Get initial balance for verification
            initial_balance = await self._agent.check_balance(self._site["dashboard"])
            initial_pts = self._extract_points(initial_balance.output or "")
            logger.info(f"[Session {self._session_id}] Initial balance: {initial_pts}")

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

                if result is None:
                    self._stats["errors"] += 1
                    logger.warning(f"[Session {self._session_id}] Survey {i + 1}: complete_survey returned None")
                elif result.success:
                    # Verify points were actually earned
                    post_balance = await self._agent.check_balance(self._site["dashboard"])
                    post_pts = self._extract_points(post_balance.output or "")
                    pts_earned = 0
                    if post_pts and initial_pts and post_pts != initial_pts:
                        pts_earned = post_pts - initial_pts
                        self._stats["surveys_completed"] += 1
                        self._stats["points_earned"] = self._stats.get("points_earned", 0) + pts_earned
                        initial_pts = post_pts  # Update for next survey
                        logger.info(
                            f"[Session {self._session_id}] Survey {i + 1} COMPLETED — earned {pts_earned} points (total: {post_pts})"
                        )
                    else:
                        self._stats["surveys_completed"] += 1
                        logger.info(
                            f"[Session {self._session_id}] Survey {i + 1} completed (no balance change detected)"
                        )
                    if brain:
                        try:
                            brain.record(
                                site=self._site_slug,
                                event_type="survey_completed",
                                context={"survey_index": i + 1},
                                outcome={"points": pts_earned},
                                session_id=str(self._session_id),
                            )
                        except Exception:
                            logger.exception("brain.record(survey_completed) failed")
                    logger.info(f"[Session {self._session_id}] Survey output: {result.output[:300]}")
                elif "captcha" in (result.output or "").lower() or "captcha" in (result.error or "").lower():
                    self._stats["captchas_solved"] += 1
                    logger.info(f"[Session {self._session_id}] Survey {i + 1} skipped: CAPTCHA")
                    if brain:
                        try:
                            brain.record(
                                site=self._site_slug,
                                event_type="captcha_failed",
                                context={"raw": (result.error or result.output or "")[:500]},
                                session_id=str(self._session_id),
                            )
                        except Exception:
                            logger.exception("brain.record(captcha_failed) failed")
                elif "disqualified" in (result.error or "").lower() or "not a match" in (result.error or "").lower():
                    self._stats.setdefault("disqualified", 0)
                    self._stats["disqualified"] += 1
                    logger.info(f"[Session {self._session_id}] Survey {i + 1} disqualified: not a match")
                    if brain:
                        try:
                            brain.record(
                                site=self._site_slug,
                                event_type="disqualified",
                                context={"raw": (result.error or "")[:500]},
                                session_id=str(self._session_id),
                            )
                        except Exception:
                            logger.exception("brain.record(disqualified) failed")
                else:
                    self._stats["errors"] += 1
                    logger.warning(
                        f"[Session {self._session_id}] Survey {i + 1} failed: {(result.error or result.output)[:200]}"
                    )
                    if brain:
                        try:
                            brain.record(
                                site=self._site_slug,
                                event_type="survey_failed",
                                context={"raw": (result.error or result.output or "")[:500]},
                                session_id=str(self._session_id),
                            )
                        except Exception:
                            logger.exception("brain.record(survey_failed) failed")

                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"[Session {self._session_id}] Error: {e}", exc_info=True)
            return {"error": str(e), "stats": self._stats}

        return {"stats": self._stats}

    @staticmethod
    def _extract_points(balance_output: str) -> int | None:
        """Extract numeric points value from balance check output."""
        import re as _re  # noqa: PLC0415

        # Look for patterns like "1,234", "$12.34", "1234 points", "Balance: 1234"
        match = _re.search(r"(?:BALANCE:|balance[:\s]*)\s*[\$]?(\d[\d,]*(?:\.\d+)?)", balance_output, _re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", "").split(".")[0])
        # Try any number in the output
        match = _re.search(r"(\d{2,})", balance_output)
        if match:
            return int(match.group(1))
        return None


class SessionOrchestrator:
    """Manages agent-driven survey sessions."""

    def __init__(self, max_sessions: int = 1, config: AppConfig | None = None, headless: bool = False):
        self._config = config or load_config()
        self._max_sessions = max_sessions
        self._headless = headless
        self._credentials = CredentialStore()
        self._profile = ProfileStore(DATA_DIR / "profile.json")

    def set_profile(self, profile_path: str) -> None:
        self._profile = ProfileStore(profile_path)

    async def run(self, sites: list[str] | None = None, max_surveys: int = 10) -> list[dict]:
        """Run survey sessions."""
        # Autonomy hook — kick off digest if due, non-blocking (spec §4.3)
        if brain:
            try:
                import threading  # noqa: PLC0415

                from .brain.autonomy import trigger_if_due  # noqa: PLC0415
                from .brain.store import FailureStore  # noqa: PLC0415

                def _bg_digest() -> None:
                    try:
                        store = FailureStore(DATA_DIR / "brain")
                        trigger_if_due(store, self._config.brain, DATA_DIR)
                    except Exception:
                        logger.exception("Brain autonomy trigger failed (non-blocking)")

                threading.Thread(target=_bg_digest, daemon=True).start()
            except Exception:
                logger.exception("Failed to start brain autonomy thread")
        if sites is None:
            sites = list(SITES.keys())

        results = []
        for site_slug in sites:
            if site_slug not in SITES:
                results.append({"error": f"Unknown site: {site_slug}", "stats": {}})
                continue

            site_info = SITES[site_slug]
            logger.info(f"Starting agent for {site_info['name']}...")

            # Look up stored credentials for this site
            creds = self._credentials.retrieve(site_slug)
            login_email = creds.username if creds else ""
            login_password = creds.password if creds else ""

            if not login_email:
                logger.warning(f"No stored credentials for {site_slug}. Run: survey-agent account")

            # Use harness-specific LLM settings if available
            if self._config.browser_backend == "browser-harness":
                llm = self._config.harness_llm_settings()
            else:
                llm = {
                    "llm_provider": self._config.llm_provider,
                    "llm_model": self._config.llm_model or None,
                    "llm_api_key": self._config.llm_api_key or None,
                    "llm_base_url": self._config.llm_base_url or None,
                }

            agent = create_backend(
                backend=self._config.browser_backend,
                headless=self._headless,
                google_email=self._config.account.google_email,
                paypal_email=self._config.account.paypal_email,
                login_email=login_email,
                login_password=login_password,
                **llm,
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

    async def run_daemon(
        self,
        sites: list[str] | None = None,
        max_surveys: int = 10,
        cycle_min: int = 8,
        cycle_max: int = 12,
        pause_min_minutes: int = 15,
        pause_max_minutes: int = 45,
    ) -> None:
        """Run survey sessions indefinitely with human-like pauses between cycles.

        Args:
            sites: Site slugs to run on.
            max_surveys: Max surveys per cycle.
            cycle_min: Minimum surveys per cycle.
            cycle_max: Maximum surveys per cycle.
            pause_min_minutes: Minimum pause between cycles.
            pause_max_minutes: Maximum pause between cycles.
        """
        import random  # noqa: PLC0415
        import signal  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        running = True

        def _handle_signal(signum, frame):  # noqa: ARG001
            nonlocal running
            logger.info("Received signal %d — shutting down gracefully after current cycle", signum)
            running = False

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        # Prevent macOS sleep during daemon operation
        try:
            caffeine_proc = subprocess.Popen(
                ["caffeinate", "-dimsu", "-t", "86400"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("caffeinate started to prevent macOS sleep")
        except Exception:
            caffeine_proc = None

        logger.info(
            "Daemon mode: sites=%s, %d-%d surveys/cycle, %d-%d min pause",
            sites or list(SITES.keys()),
            cycle_min,
            cycle_max,
            pause_min_minutes,
            pause_max_minutes,
        )

        total_earned = 0
        cycle = 0

        while running:
            cycle += 1
            surveys_this_cycle = random.randint(cycle_min, cycle_max)
            actual_surveys = min(surveys_this_cycle, max_surveys)
            logger.info("=== Cycle %d: %d surveys ===", cycle, actual_surveys)

            # Run one cycle of surveys
            try:
                results = await self.run(sites=sites, max_surveys=actual_surveys)
                for r in results:
                    stats = r.get("stats", {})
                    if stats.get("surveys_completed", 0) > 0:
                        earned = stats.get("points_earned", 0)
                        total_earned += earned
                        logger.info(
                            "Cycle %d completed: %d surveys, %d points earned (total: %d)",
                            cycle,
                            stats["surveys_completed"],
                            earned,
                            total_earned,
                        )
                    if stats.get("errors", 0) > 0:
                        logger.warning("Cycle %d had %d errors", cycle, stats["errors"])
                    if stats.get("captchas_solved", 0) > 0:
                        logger.info("Cycle %d solved %d CAPTCHAs", cycle, stats["captchas_solved"])
            except Exception as e:
                logger.error("Cycle %d failed: %s", cycle, e)

            if not running:
                break

            # Pause between cycles (human-like: 15-45 minutes)
            pause_minutes = random.randint(pause_min_minutes, pause_max_minutes)
            logger.info(
                "=== Cycle %d done. Pausing %d minutes (%d/%d surveys) ===",
                cycle,
                pause_minutes,
                actual_surveys,
                actual_surveys,
            )
            logger.info("Total points earned so far: %d", total_earned)

            # Sleep in short intervals so SIGINT is responsive
            for _ in range(pause_minutes):
                if not running:
                    break
                await asyncio.sleep(60)

        logger.info("Daemon stopped. Total points earned: %d", total_earned)

        # Clean up caffeinate
        if caffeine_proc:
            try:
                caffeine_proc.kill()
            except Exception:
                pass
