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
        self._persistent = persistent
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

        launch_kwargs: dict[str, Any] = {"humanize": self._humanize}
        if self._geoip:
            launch_kwargs["geoip"] = True

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
            logger.info(f"Navigating to {site_name} ({site_url})")
            await self._page.goto(site_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

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
                    # Use Playwright's fill() — use specific IDs from Branded Surveys
                    email_selectors = [
                        '#UserEmail',
                        'input[type="email"]',
                        'input[name="email"]',
                    ]
                    email_filled = False
                    for sel in email_selectors:
                        try:
                            await self._page.fill(sel, self._login_email, timeout=3)
                            email_filled = True
                            logger.info(f"Filled email via {sel}")
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
                            await self._page.fill(sel, self._login_password, timeout=3)
                            pw_filled = True
                            logger.info(f"Filled password via {sel}")
                            break
                        except Exception:
                            continue

                    if email_filled and pw_filled:
                        # Click submit — try multiple selectors
                        submit_selectors = [
                            '#_evh-ric-c',
                            'button[type="submit"]',
                        ]
                        clicked = False
                        for sel in submit_selectors:
                            try:
                                el = await self._page.query_selector(sel)
                                if el and await el.is_visible():
                                    await self._page.click(sel)
                                    clicked = True
                                    logger.info(f"Clicked submit via {sel}")
                                    break
                            except Exception:
                                continue

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

        Reads questions from the page, generates answers via profile context,
        and handles CAPTCHAs using the vision solver.
        """
        if not self._page:
            return AgentResult(success=False, error="Backend not started")

        try:
            # Check for CAPTCHA first
            page_text = await self._page.evaluate(
                "() => document.body?.innerText?.substring(0, 2000) || ''"
            )

            from survey_agent.captcha.solver import (  # noqa: PLC0415
                CaptchaDetector, CaptchaSolver, create_vision_llm_fn,
            )

            captcha = CaptchaDetector.detect_from_page(page_text, "")
            if captcha:
                logger.info(f"CAPTCHA detected: {captcha.captcha_type.value} — {captcha.description}")

                # Take screenshot for vision solver
                screenshot_bytes = None
                try:
                    screenshot_bytes = await self._page.screenshot()
                except Exception:
                    logger.warning("Could not take screenshot for CAPTCHA")

                # Create vision solver with Groq
                vision_fn = create_vision_llm_fn(provider="groq")
                solver = CaptchaSolver(llm_fn=vision_fn)

                solution = await solver.solve(captcha)
                if solution.solved and solution.answer:
                    logger.info(f"CAPTCHA solved: {solution.answer} (method: {solution.method})")

                    # Try to enter the answer into the CAPTCHA input field
                    answer_escaped = solution.answer.replace("'", "\\'")
                    entered = await self._page.evaluate(
                        f"(() => {{"
                        f"const inputs = document.querySelectorAll('input[type=\"text\"], input[type=\"number\"], input:not([type])');"
                        f"for (const input of inputs) {{"
                        f"if (input.offsetParent !== null && (input.placeholder || '').toLowerCase().includes('code')) {{"
                        f"input.value = '{answer_escaped}';"
                        f"input.dispatchEvent(new Event('input', {{bubbles: true}}));"
                        f"input.dispatchEvent(new Event('change', {{bubbles: true}}));"
                        f"return true;}}}}"
                        f"return false;}})()"
                    )

                    if entered:
                        logger.info("CAPTCHA answer entered into field")
                    else:
                        logger.warning("Could not find CAPTCHA input field")
                else:
                    logger.warning(f"CAPTCHA not solved: {solution.error}")
                    from survey_agent import brain  # noqa: PLC0415
                    brain.record(
                        site="branded_surveys",
                        event_type="captcha_failed",
                        context={
                            "challenge_type": captcha.captcha_type.value,
                            "description": captcha.description,
                            "solver_error": solution.error or "unknown",
                            "method": solution.method,
                        },
                    )
                    return AgentResult(
                        success=False,
                        error=f"CAPTCHA not solved: {solution.error}",
                    )

            # Capture current page state for answering
            page_state = await self._page.evaluate("""() => {
                const state = {
                    url: location.href,
                    title: document.title,
                    questions: [],
                    buttons: [],
                };

                // Find question containers
                const questionEls = document.querySelectorAll(
                    '[class*="question"], [class*="survey-question"], ' +
                    '[id*="question"], fieldset, .q-text'
                );
                questionEls.forEach(el => {
                    const text = (el.textContent || '').trim().substring(0, 500);
                    if (text) state.questions.push(text);
                });

                // Find form elements
                const inputs = document.querySelectorAll(
                    'input[type="radio"], input[type="checkbox"], select, textarea, input[type="text"]'
                );
                inputs.forEach(el => {
                    const name = el.name || el.id || '';
                    const type = el.type || el.tagName.toLowerCase();
                    const label = el.closest('label')?.textContent?.trim() ||
                                  document.querySelector(`label[for="${el.id}"]`)?.textContent?.trim() || '';
                    const opts = [];
                    if (el.tagName === 'SELECT') {
                        Array.from(el.options).forEach(o => opts.push({v: o.value, t: o.text.trim()}));
                    }
                    state.questions.push({name, type, label: label.substring(0, 200), opts});
                });

                // Find next/submit buttons
                const btns = document.querySelectorAll(
                    'button, input[type="submit"], [class*="next"], [class*="submit"]'
                );
                btns.forEach(el => {
                    const text = (el.textContent || el.value || '').trim();
                    if (text) state.buttons.push(text.substring(0, 100));
                });

                return JSON.stringify(state);
            }""")

            import json

            parsed = json.loads(page_state)

            return AgentResult(
                success=True,
                output=f"Survey page captured. Questions: {len(parsed.get('questions', []))}, "
                f"Buttons: {parsed.get('buttons', [])}. "
                f"URL: {parsed.get('url', 'unknown')}. "
                f"Use this data for LLM-driven answering.",
                url=parsed.get("url", ""),
            )
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
