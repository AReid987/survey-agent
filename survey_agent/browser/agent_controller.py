"""Agent-driven browser control — uses LLM to dynamically interact with pages."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from browser_use import Agent, Browser, Controller

from ..llm_provider import get_llm
from .backend import AgentResult, SurveyAgentBackend

logger = logging.getLogger(__name__)

SESSION_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser_session"


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
        self._llm_chain: list[dict[str, str | None]] = []
        self._llm_index: int = 0

    async def start(self) -> None:
        """Start the browser."""
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._browser = Browser(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            downloads_path=str(self._session_dir),
        )
        self._controller = Controller()

        # If no explicit API key was provided, resolve from project-local env
        if not self._api_key:
            try:
                from ..browser.harness_backend import _read_zshrc_api_keys  # noqa: PLC0415

                env_keys = _read_zshrc_api_keys()
                self._api_key = env_keys.get(f"{self._provider.upper()}_API_KEY")
            except Exception:
                pass

        # Create primary LLM instance and build fallback chain
        self._llm = get_llm(
            provider=self._provider,
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
        )
        self._llm_chain = self._build_fallback_chain()
        logger.info(f"Browser agent started (provider={self._provider}, headless={self._headless})")

    async def stop(self) -> None:
        """Stop the browser."""
        if self._browser:
            await self._browser.close()
            self._browser = None

    def _build_fallback_chain(self) -> list[dict[str, str | None]]:
        """Build a list of LLM configs to try on failure."""
        chain: list[dict[str, str | None]] = []
        seen_providers: set[str] = set()

        def _add(provider: str, model: str | None, api_key: str | None = None, base_url: str | None = None) -> None:
            if provider and provider not in seen_providers:
                chain.append({"provider": provider, "model": model, "api_key": api_key, "base_url": base_url})
                seen_providers.add(provider)

        # Primary
        _add(self._provider, self._model, self._api_key, self._base_url)

        # Config fallback
        try:
            from ..config import load_config  # noqa: PLC0415

            cfg = load_config()
            if cfg.llm_provider and cfg.llm_provider != self._provider:
                _add(cfg.llm_provider, cfg.llm_model or None, cfg.llm_api_key or None, cfg.llm_base_url or None)
        except Exception:
            pass

        # Project-local keys + zsh_secrets + zshrc
        try:
            from ..browser.harness_backend import _read_zshrc_api_keys  # noqa: PLC0415

            keys = _read_zshrc_api_keys()
            for provider, model in [
                ("openrouter", "minimax/minimax-m3:free"),
                ("opencode-go", "qwen3.8-flash"),
                ("opencode", "hy3-free"),
                ("kilocode", "kilo-auto/free"),
                ("mistral", "mistral-large-latest"),
                ("cerebras", "gpt-oss-120b"),
                ("groq", "qwen/qwen3.6-27b"),
            ]:
                key = keys.get(f"{provider.upper()}_API_KEY", "")
                if key:
                    _add(provider, model, key)
        except Exception:
            pass

        # Local LM Studio fallback: no API key required from secret stores
        _add("lmstudio", "prism-ml/bonsai-27b", None)

        return chain

    def _next_llm(self) -> Any:
        """Return the next LLM to try from the fallback chain, skipping the primary."""
        if not self._llm_chain or len(self._llm_chain) <= 1:
            return self._llm
        # Skip index 0, which is the primary LLM already in use.
        idx = (self._llm_index + 1) % len(self._llm_chain)
        self._llm_index += 1
        cfg = self._llm_chain[idx]
        logger.warning("Rotating fallback LLM to %s/%s", cfg["provider"], cfg.get("model"))
        return get_llm(
            provider=cfg["provider"],
            model=cfg.get("model"),
            api_key=cfg.get("api_key"),
            base_url=cfg.get("base_url"),
        )

    async def execute(
        self,
        task: str,
        max_steps: int = 50,
    ) -> AgentResult:
        """Execute a task described in natural language.

        Creates a fresh browser for each task to avoid browser-use state
        handler corruption between tasks.
        """
        if not self._llm:
            await self.start()

        # Always create a fresh browser to avoid stale state handlers
        if self._browser:
            with contextlib.suppress(Exception):
                await self._browser.close()

        self._browser = Browser(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
            downloads_path=str(self._session_dir),
        )
        self._controller = Controller()

        try:
            agent = Agent(
                task=task,
                llm=self._llm,
                fallback_llm=self._next_llm() if len(self._llm_chain) > 1 else None,
                browser=self._browser,
                controller=self._controller,
                max_actions_per_step=5,
                use_vision=False,
                vision_detail_level="low",
            )

            logger.info(f"Agent starting: {task[:120]}...")
            result = await agent.run(max_steps=max_steps)

            final_url = ""
            with contextlib.suppress(Exception):
                final_url = await self._browser.get_current_page_url()

            return AgentResult(
                success=result.is_done() if result else False,
                output=result.final_result() if result else "",
                url=final_url,
            )
        except Exception as e:
            logger.error(f"Agent error: {e}", exc_info=True)
            # Let execute_with_retry handle LLM rotation; just reset browser state
            self._browser = None
            return AgentResult(success=False, error=str(e))

    async def execute_with_retry(
        self,
        task: str,
        max_steps: int = 50,
        max_retries: int = 3,
    ) -> AgentResult:
        """Execute with automatic retry on failure, rotating LLM each time."""
        result = await self.execute(task, max_steps=max_steps)
        if result.success:
            return result

        for attempt in range(1, max_retries):
            logger.info(f"Retry {attempt}/{max_retries}...")
            # Rotate to a fresh LLM so retries don't keep hitting the same rate limit
            self._llm = self._next_llm()
            retry_task = (
                f"{task}\n\n"
                f"IMPORTANT: Previous attempt failed: {result.error or 'unknown error'}\n"
                f"Try a completely different approach. Look at the page carefully."
            )
            result = await self.execute(retry_task, max_steps=max_steps)
            if result.success:
                return result

        return result


class BrowserUseBackend(SurveyAgentBackend):
    """Browser backend powered by browser-use (LLM agent + Playwright)."""

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
        login_email: str = "",
        login_password: str = "",
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
        self._login_email = login_email
        self._login_password = login_password

    async def start(self) -> None:
        await self._browser_agent.start()

    async def stop(self) -> None:
        await self._browser_agent.stop()

    async def login(self, site_url: str, _site_name: str) -> AgentResult:
        """Log into a survey site using email/password credentials."""
        email = self._login_email or self._google_email
        password = self._login_password

        if not email or not password:
            return AgentResult(success=False, error="No login credentials provided. Run: survey-agent credentials")

        task = f"""Navigate to {site_url} and log in using email and password.

Steps:
1. Go to {site_url}/users/sign_in (or find the sign-in page)
2. If already logged in (you see a dashboard with surveys), report "already logged in"
3. IGNORE any "Sign in with Google" or OAuth buttons — do NOT click them
4. Find the email/password login form
5. Enter email: {email}
6. Enter password: {password}
7. Click the login/submit button
8. If there's a CAPTCHA, report "captcha_detected"
9. Verify you're on a dashboard page with surveys

IMPORTANT: Use the standard email + password form. Do NOT use Google OAuth — it is blocked in automated browsers.
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

    async def run_full_session(
        self,
        site_url: str,
        site_name: str,
        dashboard_url: str,
        profile_context: str,
        max_surveys: int = 10,
    ) -> AgentResult:
        """Run an entire survey session in a single agent task.

        Combines login + survey discovery + completion into one agent run
        to avoid browser-use state handler corruption between tasks.
        """
        email = self._login_email or self._google_email
        password = self._login_password

        if not email or not password:
            return AgentResult(success=False, error="No login credentials provided")

        task = f"""You are running a complete survey session on {site_name}.

STEP 1: LOG IN
Navigate to {site_url} and log in using:
- Email: {email}
- Password: {password}
- Do NOT use Google OAuth — use the standard email/password form
- If you see a cookie banner, dismiss it first
- After login, verify you reach the dashboard

STEP 2: COMPLETE SURVEYS
After logging in, complete up to {max_surveys} surveys:
- Find available surveys on the dashboard
- Click on the first available survey
- Answer all questions based on this user profile:

{profile_context}

ANSWERING RULES:
- For multiple choice: pick the option that best matches the profile
- For checkboxes: select all that apply
- For scales/ratings: choose based on profile info
- For open-ended: write natural 2-3 sentence responses as the user would
- If it's an attention check (e.g. "select X for this row"), follow it EXACTLY
- If you hit a CAPTCHA: describe it and move to the next survey
- If disqualified: move to the next survey
- After completing a survey, go back to the dashboard and start the next one

STEP 3: REPORT RESULTS
After completing surveys (or running out), report:
- How many surveys were completed
- How many points were earned
- Any errors or CAPTCHAs encountered

Be resilient. If something unexpected happens, look at the page and adapt.
"""
        return await self._browser_agent.execute_with_retry(task, max_steps=200, max_retries=4)

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        """Withdraw earnings."""
        task = f"""Go to {redeem_url} and withdraw earnings via {method}.

Find the {method} option, select the maximum amount, and confirm.
Report the result.
"""
        return await self._browser_agent.execute_with_retry(task, max_steps=25)


# Backward-compatible alias
SurveyAgent = BrowserUseBackend
