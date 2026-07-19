"""Camoufox backend — implements SurveyAgentBackend using stealth Firefox.

Uses CamoufoxEngine for browser control and existing site adapters
(PrimeOpinionAdapter, etc.) for site-specific logic. Maintains persistent
browser sessions via Camoufox's user_data_dir support.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .backend import AgentResult, SurveyAgentBackend
from .camoufox_engine import CamoufoxBrowserContext, CamoufoxEngine, CamoufoxPage

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "camoufox_session"


class CamoufoxBackend(SurveyAgentBackend):
    """Stealth browser backend using Camoufox (patched Firefox).

    Implements SurveyAgentBackend by launching Camoufox with persistent
    sessions, then delegating to site adapters that work via BrowserPage.
    """

    def __init__(
        self,
        headless: bool = True,
        humanize: bool = True,
        geoip: bool = True,
        persistent: bool = True,
        user_data_dir: str | Path | None = None,
        login_email: str = "",
        login_password: str = "",
        **kwargs: Any,
    ) -> None:
        self._headless = headless
        self._humanize = humanize
        self._geoip = geoip
        self._persistent = False  # Fresh profile to avoid stale cookie issues
        self._user_data_dir = Path(user_data_dir) if user_data_dir else DATA_DIR / "profile"
        self._engine: CamoufoxEngine | None = None
        self._context: CamoufoxBrowserContext | None = None
        self._page: CamoufoxPage | None = None
        self._current_site: str = ""
        self._login_email = login_email
        self._login_password = login_password

    async def start(self) -> None:
        """Launch Camoufox and create the default page."""
        logger.info("Starting Camoufox backend...")
        self._engine = CamoufoxEngine(
            headless=self._headless,
            persistent=self._persistent,
            user_data_dir=self._user_data_dir,
        )

        launch_kwargs: dict[str, Any] = {}
        if self._geoip:
            launch_kwargs["geoip"] = False  # Disabled — causes hangs

        await self._engine.launch(headless=self._headless, **launch_kwargs)
        self._context = await self._engine.create_context()
        self._page = await self._context.new_page()
        logger.info("Camoufox backend started OK")

    async def stop(self) -> None:
        """Shut down the browser and clean up resources."""
        if self._page:
            try:
                await self._page.close()
            except Exception:
                logger.warning("Error closing page", exc_info=True)
            self._page = None
        if self._engine:
            await self._engine.close()
            self._engine = None
        self._context = None
        logger.info("Camoufox backend stopped")

    async def login(self, site_url: str, site_name: str) -> AgentResult:
        """Log into a survey site.

        Navigates to the site and attempts credential-based login.
        Falls back to manual login detection if no credentials stored.
        """
        if not self._page:
            return AgentResult(success=False, error="Backend not started")

        self._current_site = site_name
        try:
            # Navigate to login page directly (root URL redirect is slow)
            login_url = site_url.rstrip("/") + "/users/login/"
            logger.info(f"Navigating to {site_name} ({login_url})")
            await self._page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Dismiss cookie banners (common on survey sites)
            for banner_sel in [
                "#_evidon-banner-acceptbutton",
                "#onetrust-accept-btn-handler",
                "button[id*='accept']",
                "button[class*='accept']",
                "button[class*='close']",
                "#_evidon_banner button",
            ]:
                try:
                    await self._page.click(banner_sel, timeout=1)
                    logger.info(f"Dismissed cookie banner via {banner_sel}")
                    await asyncio.sleep(0.5)
                    break
                except Exception:
                    continue

            url = await self._page.url()
            title = await self._page.evaluate("() => document.title")
            body_text = await self._page.evaluate(
                "() => document.body?.innerText?.substring(0, 1000) || ''"
            )

            # Check for login indicators
            is_login_page = any(
                kw in url.lower() for kw in ["login", "signin", "sign-in", "auth"]
            )
            has_login_form = any(
                kw in body_text.lower()
                for kw in ["log in", "sign in", "email", "password"]
            )

            if (is_login_page or has_login_form) and self._login_email and self._login_password:
                logger.info(f"Login form detected, filling credentials for {self._login_email}")
                try:
                    # Use Playwright's type() for proper keystroke simulation
                    # fill() doesn't trigger CakePHP's JavaScript validation
                    email_selectors = [
                        '#UserEmail',
                        'input[type="email"]',
                        'input[name="email"]',
                    ]
                    email_filled = False
                    for sel in email_selectors:
                        try:
                            await self._page._page.click(sel, timeout=3000)
                            await self._page._page.type(sel, self._login_email, delay=50)
                            email_filled = True
                            logger.info(f"Typed email via {sel}")
                            break
                        except Exception:
                            continue

                    pw_selectors = [
                        '#UserPassword',
                        'input[type="password"]',
                    ]
                    pw_filled = False
                    for sel in pw_selectors:
                        try:
                            await self._page._page.click(sel, timeout=3000)
                            await self._page._page.type(sel, self._login_password, delay=50)
                            pw_filled = True
                            logger.info(f"Typed password via {sel}")
                            break
                        except Exception:
                            continue

                    if email_filled and pw_filled:
                        # Click submit — try multiple selectors, then Enter key
                        submit_selectors = [
                            "#_evh-ric-c",
                            "button[type='submit']",
                        ]
                        clicked = False
                        for sel in submit_selectors:
                            try:
                                await self._page.click(sel, timeout=3)
                                clicked = True
                                logger.info(f"Clicked submit via {sel}")
                                break
                            except Exception:
                                continue

                        if not clicked:
                            # Fallback: press Enter on password field via keyboard
                            try:
                                await self._page._page.press("#UserPassword", "Enter")
                                clicked = True
                                logger.info("Submitted via Enter key on password field")
                            except Exception:
                                pass

                        if clicked:
                            logger.info("Login form submitted, waiting for navigation...")
                            await asyncio.sleep(5)

                            new_url = await self._page.url()
                            new_body = await self._page.evaluate(
                                "() => document.body?.innerText?.substring(0, 500) || ''"
                            )
                            still_login = any(
                                kw in new_url.lower() for kw in ["login", "signin", "sign-in"]
                            )

                            if not still_login:
                                logger.info(f"Login appears successful: {new_url}")
                                return AgentResult(success=True, output=f"Logged in to {site_name}", url=new_url)
                            else:
                                logger.warning(f"Still on login page after submit: {new_url}")
                                return AgentResult(success=False, output=f"Login form submitted but still on login page. Body: {new_body[:200]}", url=new_url)
                    else:
                        logger.warning(f"Could not fill form: email={email_filled}, pw={pw_filled}")
                except Exception as e:
                    logger.error(f"Credential fill failed: {e}")

            if is_login_page or has_login_form:
                return AgentResult(
                    success=False,
                    output=f"Login page detected at {url}. Body: {body_text[:200]}",
                    url=url,
                )

            logger.info(f"Appears logged in to {site_name}: {url}")
            return AgentResult(
                success=True,
                output=f"Logged in to {site_name}. Title: {title}, URL: {url}",
                url=url,
            )
        except Exception as e:
            logger.error(f"Login failed: {e}")
            return AgentResult(success=False, error=f"Login error: {e}")

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        """Find available surveys on the dashboard."""
        if not self._page:
            return AgentResult(success=False, error="Backend not started")

        try:
            current_url = await self._page.url()
            if dashboard_url not in current_url:
                logger.info(f"Navigating to dashboard: {dashboard_url}")
                await self._page.goto(dashboard_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)

            # Extract survey links and cards from the page
            surveys = await self._page.evaluate("""() => {
                const results = [];
                // Look for survey links
                const links = document.querySelectorAll(
                    'a[href*="survey"], a[href*="offer"], [class*="survey"], [data-survey]'
                );
                links.forEach(el => {
                    const text = (el.textContent || '').trim().substring(0, 200);
                    const href = el.href || '';
                    if (text && text.length > 3) {
                        results.push({ text, url: href });
                    }
                });
                // Look for survey cards
                const cards = document.querySelectorAll(
                    '[class*="card"], [class*="item"], [class*="list"] a'
                );
                cards.forEach(el => {
                    const text = (el.textContent || '').trim().substring(0, 200);
                    const href = el.closest('a')?.href || '';
                    if (text && text.length > 10 && href) {
                        results.push({ text, url: href });
                    }
                });
                return results.slice(0, 50);
            }""")

            if not surveys:
                body = await self._page.evaluate(
                    "() => document.body?.innerText?.substring(0, 300) || ''"
                )
                return AgentResult(
                    success=True,
                    output=f"No surveys found. Page preview: {body[:300]}",
                    url=await self._page.url(),
                )

            output_lines = [f"Found {len(surveys)} potential surveys:"]
            for i, s in enumerate(surveys[:20], 1):
                output_lines.append(f"  {i}. {s['text'][:100]} → {s['url']}")

            return AgentResult(
                success=True,
                output="\n".join(output_lines),
                url=await self._page.url(),
            )
        except Exception as e:
            logger.error(f"Survey discovery failed: {e}")
            return AgentResult(success=False, error=f"Discovery error: {e}")

    async def complete_survey(
        self,
        survey_info: str,
        profile_context: str,
    ) -> AgentResult:
        """Complete a survey by answering questions.

        Uses the survey engine to:
        1. Answer quick-questions on the dashboard
        2. Click on full surveys and answer each question
        3. Handle CAPTCHAs using the vision solver
        """
        if not self._page:
            return AgentResult(success=False, error="Backend not started")

        try:
            from survey_agent.browser.survey_engine import (  # noqa: PLC0415
                answer_quick_question,
                answer_survey_question,
            )
            from survey_agent.captcha.solver import (  # noqa: PLC0415
                CaptchaDetector, CaptchaSolver, create_vision_llm_fn,
            )

            # Create LLM function for answering questions
            from survey_agent.config import load_config  # noqa: PLC0415
            cfg = load_config()
            from survey_agent.llm_provider import get_llm, PROVIDERS  # noqa: PLC0415
            llm = get_llm(
                provider=cfg.llm_provider,
                model=cfg.llm_model or PROVIDERS.get(cfg.llm_provider, {}).get("default_model", ""),
                api_key=cfg.llm_api_key,
                base_url=cfg.llm_base_url or None,
                temperature=0.1,
            )

            async def llm_fn(prompt: str) -> str:
                from browser_use.llm.messages import UserMessage  # noqa: PLC0415
                result = await llm.ainvoke([UserMessage(content=prompt)])
                completion = getattr(result, "completion", None)
                if completion is None:
                    # Try .content for different LLM wrappers
                    content = getattr(result, "content", None)
                    if content is not None:
                        return str(content).strip()
                    return str(result)
                return str(completion).strip()

            url = await self._page.url()

            # If on dashboard, try quick questions first, then click a survey
            if "dashboard" in url.lower():
                # Try answering quick questions
                answered = await answer_quick_question(self._page._page, profile_context, llm_fn)
                if answered:
                    logger.info("Answered a quick question on dashboard")
                    return AgentResult(success=True, output="Quick question answered on dashboard")

                # Find and click a survey
                survey_clicked = await self._page.evaluate("""() => {
                    const links = document.querySelectorAll('a[href*="survey"], a[href*="pre/"], a[class*="survey"]');
                    for (const link of links) {
                        const text = (link.textContent || '').toLowerCase();
                        if (text.includes('take') || text.includes('start') || text.includes('begin')) {
                            link.click();
                            return link.href || 'clicked';
                        }
                    }
                    // Try any "TAKE SURVEY" button
                    const btns = document.querySelectorAll('a, button');
                    for (const btn of btns) {
                        if ((btn.textContent || '').toLowerCase().includes('take survey')) {
                            btn.click();
                            return btn.href || 'clicked';
                        }
                    }
                    return null;
                }""")

                if survey_clicked:
                    logger.info(f"Clicked survey: {survey_clicked[:100]}")
                    await asyncio.sleep(3)
                else:
                    return AgentResult(success=True, output="No clickable surveys found on dashboard")

            # Now we're on a survey page — answer questions in a loop
            max_questions = 50
            for i in range(max_questions):
                # Check for CAPTCHA
                page_text = await self._page.evaluate(
                    "() => document.body?.innerText?.substring(0, 2000) || ''"
                )
                captcha = CaptchaDetector.detect_from_page(page_text, "")
                if captcha:
                    logger.info(f"CAPTCHA detected: {captcha.captcha_type.value}")
                    screenshot_bytes = await self._page.screenshot()
                    vision_fn = create_vision_llm_fn(provider="groq")
                    solver = CaptchaSolver(llm_fn=vision_fn)
                    solution = await solver.solve(captcha)
                    if solution.solved and solution.answer:
                        logger.info(f"CAPTCHA solved: {solution.answer}")
                        # Enter answer
                        escaped = solution.answer.replace("'", "\\'")
                        await self._page.evaluate(
                            "(() => {"
                            "const inputs = document.querySelectorAll('input[type=\"text\"], input[type=\"number\"]');"
                            "for (const input of inputs) {"
                            "if (input.offsetParent !== null) {"
                            f"input.value = '{escaped}';"
                            "input.dispatchEvent(new Event('input', {bubbles: true}));"
                            "return true;}}"
                            "return false;})()"
                        )
                    else:
                        logger.warning(f"CAPTCHA not solved: {solution.error}")
                        return AgentResult(success=False, error=f"CAPTCHA: {solution.error}")

                # Answer the current question
                result = await answer_survey_question(self._page._page, profile_context, llm_fn)

                if result == "completed":
                    logger.info(f"Survey completed after {i + 1} questions")
                    return AgentResult(success=True, output=f"Survey completed after {i + 1} questions")
                elif result == "disqualified":
                    logger.info(f"Disqualified after {i + 1} questions")
                    return AgentResult(success=False, error="Disqualified from survey")
                elif result == "no_question":
                    # Might be between pages, wait and try again
                    await asyncio.sleep(2)
                    continue

            return AgentResult(success=True, output=f"Answered {max_questions} questions (loop limit)")

        except Exception as e:
            logger.error(f"Survey completion failed: {e}")
            return AgentResult(success=False, error=f"Completion error: {e}")

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        """Check account balance on the dashboard."""
        if not self._page:
            return AgentResult(success=False, error="Backend not started")

        try:
            current_url = await self._page.url()
            if dashboard_url not in current_url:
                await self._page.goto(dashboard_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)

            # Extract balance from page
            balance_info = await self._page.evaluate("""() => {
                const text = document.body?.innerText || '';
                // Look for common balance patterns
                const patterns = [
                    /\\$[\\d,.]+/g,
                    /[\\d,.]+\\s*(?:points|pts)/gi,
                    /balance[:\\s]*[\\d,.]+/gi,
                    /earn(?:ed|ings)?[:\\s]*\\$?[\\d,.]+/gi,
                ];
                const matches = [];
                patterns.forEach(p => {
                    const m = text.match(p);
                    if (m) matches.push(...m);
                });
                return matches.slice(0, 10).join(' | ') || 'No balance found';
            }""")

            return AgentResult(
                success=True,
                output=f"Balance info: {balance_info}",
                url=await self._page.url(),
            )
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return AgentResult(success=False, error=f"Balance check error: {e}")

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        """Withdraw earnings via the specified method."""
        if not self._page:
            return AgentResult(success=False, error="Backend not started")

        try:
            await self._page.goto(redeem_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            url = await self._page.url()
            body = await self._page.evaluate(
                "() => document.body?.innerText?.substring(0, 500) || ''"
            )

            return AgentResult(
                success=False,
                output=f"Withdrawal page loaded but not yet automated. "
                f"URL: {url}. Body: {body[:300]}. "
                f"Method: {method}. Manual intervention needed.",
                url=url,
            )
        except Exception as e:
            logger.error(f"Withdrawal failed: {e}")
            return AgentResult(success=False, error=f"Withdrawal error: {e}")

    # -- Direct page access for advanced operations --

    async def get_page(self) -> CamoufoxPage:
        """Get the current browser page for direct manipulation."""
        if not self._page:
            raise RuntimeError("Backend not started — call start() first")
        return self._page

    async def take_screenshot(self, path: str | Path) -> bytes:
        """Take a screenshot of the current page."""
        if not self._page:
            raise RuntimeError("Backend not started")
        return await self._page.screenshot(path)
