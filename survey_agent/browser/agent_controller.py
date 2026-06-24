"""Agent-driven browser control — uses LLM to dynamically interact with pages."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from browser_use import Agent, Browser, BrowserConfig, Controller

from ..llm_provider import get_llm

logger = logging.getLogger(__name__)

SESSION_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser_session"


@dataclass
class AgentResult:
    """Result from the browser agent."""

    success: bool
    output: str = ""
    error: str = ""
    url: str = ""


class BrowserAgent:
    """LLM-driven browser agent that dynamically interacts with any page.

    Instead of hardcoded selectors, describes tasks in natural language
    and the agent figures out how to accomplish them.
    """

    def __init__(  # noqa: PLR0913
        self,
        llm_provider: str = "gemini",
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        headless: bool = False,
        session_dir: str | Path | None = None,
    ):
        self._provider = llm_provider
        self._model = llm_model
        self._api_key = llm_api_key
        self._base_url = llm_base_url
        self._headless = headless
        self._session_dir = Path(session_dir) if session_dir else SESSION_DIR
        self._browser: Browser | None = None
        self._controller: Controller | None = None
        self._llm = None

    async def start(self) -> None:
        """Start the browser."""
        self._session_dir.mkdir(parents=True, exist_ok=True)

        config = BrowserConfig(
            headless=self._headless,
            disable_security=True,
            extra_chromium_args=[
                "--disable-blink-features=AutomationControlled",
            ],
            user_data_dir=str(self._session_dir),
        )
        self._browser = Browser(config=config)
        self._controller = Controller()

        # Create LLM instance
        self._llm = get_llm(
            provider=self._provider,
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
        )
        logger.info(f"Browser agent started (provider={self._provider}, headless={self._headless})")

    async def stop(self) -> None:
        """Stop the browser."""
        if self._browser:
            await self._browser.close()
            self._browser = None

    async def execute(
        self,
        task: str,
        max_steps: int = 50,
    ) -> AgentResult:
        """Execute a task described in natural language."""
        if not self._browser or not self._llm:
            await self.start()

        try:
            agent = Agent(
                task=task,
                llm=self._llm,
                browser=self._browser,
                controller=self._controller,
                max_actions_per_step=5,
            )

            logger.info(f"Agent starting: {task[:120]}...")
            result = await agent.run(max_steps=max_steps)

            final_url = ""
            try:
                state = await self._browser.get_state()
                final_url = state.url if state else ""
            except Exception:
                pass

            return AgentResult(
                success=result.is_done() if result else False,
                output=result.final_result() if result else "",
                url=final_url,
            )

        except Exception as e:
            logger.error(f"Agent error: {e}", exc_info=True)
            return AgentResult(success=False, error=str(e))

    async def execute_with_retry(
        self,
        task: str,
        max_steps: int = 50,
        max_retries: int = 3,
    ) -> AgentResult:
        """Execute with automatic retry on failure."""
        result = await self.execute(task, max_steps=max_steps)
        if result.success:
            return result

        for attempt in range(1, max_retries):
            logger.info(f"Retry {attempt}/{max_retries}...")
            retry_task = (
                f"{task}\n\n"
                f"IMPORTANT: Previous attempt failed: {result.error or 'unknown error'}\n"
                f"Try a completely different approach. Look at the page carefully."
            )
            result = await self.execute(retry_task, max_steps=max_steps)
            if result.success:
                return result

        return result


class SurveyAgent:
    """High-level survey agent using dynamic browser control."""

    def __init__(
        self,
        llm_provider: str = "gemini",
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        headless: bool = False,
        session_dir: str | Path | None = None,
        google_email: str = "",
        paypal_email: str = "",
    ):
        self._browser_agent = BrowserAgent(
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            headless=headless,
            session_dir=session_dir,
        )
        self._google_email = google_email
        self._paypal_email = paypal_email

    async def start(self) -> None:
        await self._browser_agent.start()

    async def stop(self) -> None:
        await self._browser_agent.stop()

    async def login(self, site_url: str, _site_name: str) -> AgentResult:
        """Log into a survey site. Agent figures out the flow dynamically."""
        task = f"""Navigate to {site_url} and log in.

Steps:
1. Go to {site_url}
2. If already logged in (you see a dashboard with surveys), report "already logged in"
3. Look for a login button or link. Click it.
4. Look for a "Sign in with Google" button. Click it.
5. On the Google page:
   - If you see an account for {self._google_email}, click it
   - If you need to type email, enter: {self._google_email}
   - If Google asks for a password, STOP and report "manual_password_needed"
   - If there's a consent screen, click Allow/Continue
6. Verify you're on a dashboard page with surveys

Be resilient. If the page looks different than expected, look at what's actually there and find the right path.
"""
        return await self._browser_agent.execute_with_retry(task, max_steps=30)

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        """Find available surveys."""
        task = f"""Go to {dashboard_url} and find all available surveys.

Look for survey links, cards, or buttons. For each survey, note:
- Title or description
- Estimated time
- Points/rewards
- How to start it (button/link text)

If you see a list of surveys, describe each one.
If there are no surveys, say "no surveys available".
If the page looks different than expected, describe what you actually see.
"""
        return await self._browser_agent.execute(task, max_steps=20)

    async def complete_survey(
        self,
        survey_info: str,
        profile_context: str,
    ) -> AgentResult:
        """Complete a survey by answering questions dynamically."""
        task = f"""You are completing a survey. Answer as the user based on their profile.

USER PROFILE:
{profile_context}

SURVEY:
{survey_info}

INSTRUCTIONS:
1. Start the survey (click the link/button for it)
2. For each question you encounter:
   - Read the question text carefully
   - Find the matching answer from the user profile above
   - If it's an attention check (e.g. "select X for this row"), follow the instruction EXACTLY
   - For multiple choice: pick the option that best matches the profile
   - For checkboxes: select all that apply from the profile
   - For scales/ratings: choose based on profile info
   - For open-ended: write a natural 2-3 sentence response as the user would
   - If no profile match: make a reasonable, consistent choice
3. After answering, click Next/Continue/Submit to move to the next question
4. If you hit a CAPTCHA: report "captcha_detected" with a description
5. If you hit an error page: try refreshing, going back, or finding an alternative path
6. When the survey ends (thank you page, redirect to dashboard), report success

CRITICAL: Be resilient. If something unexpected happens, LOOK at the page and figure out what to do.
Don't give up on the first error. Try alternative approaches.
"""
        return await self._browser_agent.execute_with_retry(task, max_steps=100)

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        """Check account balance."""
        task = f"""Go to {dashboard_url} and find the account balance.

Look for points, cash, earnings, or available balance.
Report the exact numbers you see.
"""
        return await self._browser_agent.execute(task, max_steps=15)

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        """Withdraw earnings."""
        task = f"""Go to {redeem_url} and withdraw earnings via {method}.

Find the {method} option, select the maximum amount, and confirm.
Report the result.
"""
        return await self._browser_agent.execute_with_retry(task, max_steps=25)
