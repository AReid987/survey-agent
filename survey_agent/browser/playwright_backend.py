"""Playwright backend for survey automation.

Uses plain Playwright (like the Hermes browser tool) which bypasses
bot detection that blocks Camoufox and browser-use.

Key findings from testing:
- Must click LABEL not RADIO for radio buttons
- Must use page.select_option() or JS for dropdown selects
- Surveys can be FULL or NOT A MATCH — normal outcomes
- Points earned even for partial completion
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from .backend import AgentResult, SurveyAgentBackend

logger = logging.getLogger(__name__)


class PlaywrightBackend(SurveyAgentBackend):
    """Browser backend using plain Playwright (no anti-detection wrappers).

    Proven to bypass bot detection on Branded Surveys.
    """

    def __init__(
        self,
        headless: bool = False,
        login_email: str = "",
        login_password: str = "",
        **kwargs: Any,
    ) -> None:
        self._headless = headless
        self._login_email = login_email
        self._login_password = login_password
        self._browser = None
        self._context = None
        self._page = None
        self._llm_fn = None

    async def start(self) -> None:
        """Launch Playwright browser."""
        from playwright.async_api import async_playwright  # noqa: PLC0415

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        self._page = await self._context.new_page()

        # Create LLM function for answering questions
        from ..llm_provider import get_llm, PROVIDERS  # noqa: PLC0415
        from ..config import load_config  # noqa: PLC0415

        cfg = load_config()
        llm = get_llm(
            provider=cfg.llm_provider,
            model=cfg.llm_model or PROVIDERS.get(cfg.llm_provider, {}).get("default_model", ""),
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url or None,
            temperature=0.1,
        )

        async def _llm_fn(prompt: str) -> str:
            from browser_use.llm.messages import UserMessage  # noqa: PLC0415
            result = await llm.ainvoke([UserMessage(content=prompt)])
            return (getattr(result, "completion", None) or str(result)).strip()

        self._llm_fn = _llm_fn
        logger.info("Playwright backend started")

    async def stop(self) -> None:
        """Close browser."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Playwright backend stopped")

    async def login(self, site_url: str, site_name: str) -> AgentResult:
        """Log into a survey site."""
        login_url = site_url.rstrip("/") + "/users/login/"
        await self._page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Dismiss cookie banner
        for sel in ["#_evidon_banner button", "button[id*='close']"]:
            try:
                await self._page.click(sel, timeout=1000)
                break
            except Exception:
                continue

        # Fill credentials
        if self._login_email and self._login_password:
            try:
                await self._page.click("#UserEmail", timeout=3000)
                await self._page.type("#UserEmail", self._login_email, delay=50)
                await self._page.click("#UserPassword", timeout=3000)
                await self._page.type("#UserPassword", self._login_password, delay=50)
                await self._page.press("#UserPassword", "Enter")
                await asyncio.sleep(8)

                url = self._page.url
                if "dashboard" in url.lower():
                    logger.info(f"Login successful: {url}")
                    return AgentResult(success=True, output=f"Logged in to {site_name}")
                else:
                    return AgentResult(success=False, error=f"Login failed, at: {url}")
            except Exception as e:
                return AgentResult(success=False, error=f"Login error: {e}")

        return AgentResult(success=False, error="No credentials provided")

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        """Find available surveys on the dashboard."""
        if dashboard_url not in self._page.url:
            await self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        # Find survey links
        surveys = await self._page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="/surveys/pre/"]');
            return Array.from(links).map(l => ({
                href: l.href,
                text: l.textContent.trim().substring(0, 50),
            }));
        }""")

        if not surveys:
            return AgentResult(success=True, output="no surveys available")

        lines = [f"Found {len(surveys)} surveys:"]
        for i, s in enumerate(surveys[:10], 1):
            lines.append(f"  {i}. {s['text']} → {s['href'][:80]}")
        return AgentResult(success=True, output="\n".join(lines))

    async def complete_survey(self, survey_info: str, profile_context: str) -> AgentResult:
        """Complete a survey by answering questions.

        Flow: Dashboard → Pre-page → TAKE SURVEY → BEGIN SURVEY → Questions
        """
        # Step 1: Find and navigate to a survey
        survey_url = await self._page.evaluate("""() => {
            const link = document.querySelector('a[href*="/surveys/pre/"]');
            return link ? link.href : '';
        }""")

        if not survey_url:
            return AgentResult(success=True, output="No surveys available")

        logger.info(f"Survey URL: {survey_url[:80]}")
        await self._page.goto(survey_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Step 2: Click TAKE SURVEY
        take_clicked = await self._page.evaluate("""() => {
            const els = document.querySelectorAll('a, button, input[type="submit"]');
            for (const el of els) {
                const text = (el.textContent || el.value || '').trim().toUpperCase();
                if (text === 'TAKE SURVEY') { el.click(); return true; }
            }
            return false;
        }""")
        if not take_clicked:
            return AgentResult(success=False, error="TAKE SURVEY button not found")
        await asyncio.sleep(8)

        # Step 3: Click BEGIN SURVEY
        begin_clicked = await self._page.evaluate("""() => {
            const els = document.querySelectorAll('a, button, input[type="submit"]');
            for (const el of els) {
                const text = (el.textContent || el.value || '').trim().toUpperCase();
                if (text === 'BEGIN SURVEY' || text === 'START SURVEY') { el.click(); return true; }
            }
            return false;
        }""")
        await asyncio.sleep(8)

        # Step 4: Handle consent page
        await self._handle_consent()

        # Step 5: Answer questions in a loop
        max_questions = 50
        last_question = ""
        same_count = 0

        for i in range(max_questions):
            # Check for completion/disqualification
            url = self._page.url
            body = await self._page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
            body_lower = body.lower()

            if any(kw in body_lower for kw in ["thank you", "survey complete", "successfully completed"]):
                return AgentResult(success=True, output=f"Survey completed after {i} questions")
            if any(kw in body_lower for kw in ["not a match", "disqualified", "screened out"]):
                return AgentResult(success=False, error="Disqualified from survey")
            if "survey full" in body_lower:
                return AgentResult(success=False, error="Survey full")
            if "dashboard" in url.lower() and "survey" not in url.lower():
                return AgentResult(success=False, error="Redirected to dashboard (likely disqualified or full)")

            # Get question and options
            question_data = await self._page.evaluate("""() => {
                // Find question text
                let question = '';
                const textNodes = document.body.innerText.split('\\n');
                for (const line of textNodes) {
                    const t = line.trim();
                    if (t.length > 15 && (t.includes('?') || t.toLowerCase().includes('select') || t.toLowerCase().includes('how'))) {
                        question = t.substring(0, 300);
                        break;
                    }
                    if (t.length > 20 && !question) question = t.substring(0, 300);
                }

                // Find radio/checkbox options
                const options = [];
                const radios = document.querySelectorAll('input[type="radio"], input[type="checkbox"]');
                radios.forEach(r => {
                    const label = r.closest('label')?.textContent?.trim() ||
                                  document.querySelector(`label[for="${r.id}"]`)?.textContent?.trim() || '';
                    if (label && label.length > 1) {
                        options.push({type: r.type, name: r.name, id: r.id, label: label.substring(0, 80)});
                    }
                });

                // Find select dropdowns
                const selects = document.querySelectorAll('select');
                selects.forEach(s => {
                    const opts = [];
                    Array.from(s.options).forEach(o => {
                        if (o.value && o.text.trim()) opts.push({value: o.value, label: o.text.trim()});
                    });
                    if (opts.length > 0) options.push({type: 'select', name: s.name, id: s.id, options: opts});
                });

                // Find text inputs
                const textInputs = document.querySelectorAll('input[type="text"], input[type="number"], textarea');
                textInputs.forEach(t => {
                    if (t.offsetParent !== null) options.push({type: 'text', name: t.name, id: t.id, placeholder: t.placeholder});
                });

                return {question, options};
            }""")

            question_text = question_data.get("question", "")
            options = question_data.get("options", [])

            # Stuck detection
            if question_text == last_question:
                same_count += 1
                if same_count >= 3:
                    await self._page.evaluate("() => window.scrollBy(0, 500)")
                    await asyncio.sleep(1)
                    same_count = 0
            else:
                same_count = 0
            last_question = question_text

            if not options:
                await asyncio.sleep(2)
                continue

            # Use LLM to pick the best answer
            prompt = f"""Answer this survey question as the user described below.

USER PROFILE:
{profile_context}

QUESTION: {question_text}

OPTIONS:
{json.dumps(options[:20], indent=2)}

Reply with ONLY the label text of the best option.
For text inputs, reply with "TEXT: your answer here"."""

            try:
                answer = await self._llm_fn(prompt)
            except Exception as e:
                logger.warning(f"LLM call failed: {e}")
                answer = ""

            if not answer or answer == "None":
                await asyncio.sleep(1)
                continue

            logger.info(f"Q{i+1}: {question_text[:50]} → {answer[:50]}")

            # Apply the answer
            if answer.startswith("TEXT:"):
                text_val = answer[5:].strip().replace("'", "\\'")
                await self._page.evaluate(
                    "(() => {"
                    "const inputs = document.querySelectorAll('input[type=\"text\"], input[type=\"number\"], textarea');"
                    "for (const input of inputs) {"
                    "if (input.offsetParent !== null) {"
                    f"input.value = '{text_val}';"
                    "input.dispatchEvent(new Event('input', {bubbles: true}));"
                    "input.dispatchEvent(new Event('change', {bubbles: true}));"
                    "return true;}}"
                    "return false;})()"
                )
            else:
                # Click matching option — try label click first (works for radio/checkbox)
                answer_clean = answer.strip().lower()[:40].replace("'", "\\'")
                clicked = await self._page.evaluate(
                    "(() => {"
                    "const els = document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]');"
                    "for (const el of els) {"
                    "const label = el.closest('label')?.textContent?.trim() || '';"
                    f"if (label.toLowerCase().includes('{answer_clean}')) {{"
                    "const labelEl = el.closest('label') || el;"
                    "labelEl.click();"
                    "return 'clicked: ' + label;}}"
                    "const selects = document.querySelectorAll('select');"
                    "for (const sel of selects) {"
                    "for (const opt of sel.options) {"
                    f"if (opt.text.toLowerCase().includes('{answer_clean}')) {{"
                    "sel.value = opt.value;"
                    "sel.dispatchEvent(new Event('change', {bubbles: true}));"
                    "return 'selected: ' + opt.text;}}}"
                    "return 'no match';})()"
                )
                if clicked.startswith("no match"):
                    logger.warning(f"No match for answer: {answer_clean[:50]}")

            await asyncio.sleep(1)

            # Click Next/Submit
            await self._page.evaluate("""() => {
                const btns = document.querySelectorAll('button, input[type="submit"], a.btn');
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim().toUpperCase();
                    if (text === 'NEXT' || text === 'SUBMIT' || text === 'CONTINUE' || text === 'NEXT >' || text === '>>') {
                        btn.click();
                        return 'clicked: ' + text;
                    }
                }
                return 'no next button';
            }""")
            await asyncio.sleep(3)

        return AgentResult(success=True, output=f"Answered {max_questions} questions (loop limit)")

    async def _handle_consent(self) -> None:
        """Handle consent/agreement pages."""
        try:
            # Check consent checkbox
            await self._page.evaluate("""() => {
                const cb = document.getElementById('confirmationCheckbox');
                if (cb && !cb.checked) cb.click();
                const cbs = document.querySelectorAll('input[type="checkbox"]');
                for (const c of cbs) {
                    if (!c.checked && c.offsetParent !== null) { c.click(); break; }
                }
            }""")
            await asyncio.sleep(1)

            # Click Accept
            await self._page.evaluate("""() => {
                const els = document.querySelectorAll('a, button, input[type="submit"]');
                for (const el of els) {
                    const text = (el.textContent || el.value || '').trim().toUpperCase();
                    if (text === 'ACCEPT' || text === 'AGREE' || text === 'I AGREE') {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            await asyncio.sleep(5)
        except Exception:
            pass

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        """Check account balance."""
        if dashboard_url not in self._page.url:
            await self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        balance = await self._page.evaluate("""() => {
            const text = document.body?.innerText || '';
            const match = text.match(/My Points\\s*(\\d+)/i);
            return match ? match[1] : 'unknown';
        }""")
        return AgentResult(success=True, output=f"BALANCE: {balance}")

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        """Withdraw earnings."""
        return AgentResult(success=False, error="Withdraw not implemented yet")
