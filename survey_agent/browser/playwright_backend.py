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
            for attempt in range(3):
                try:
                    result = await llm.ainvoke([UserMessage(content=prompt)])
                    return (getattr(result, "completion", None) or str(result)).strip()
                except Exception as e:
                    if "429" in str(e) or "rate" in str(e).lower():
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    raise
            return ""

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
        """Log into a survey site with retry for bot detection."""
        login_url = site_url.rstrip("/") + "/users/login/"

        for attempt in range(3):
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
                    await asyncio.sleep(12)

                    url = self._page.url
                    if "dashboard" in url.lower():
                        logger.info(f"Login successful: {url}")

                        # Wait for dynamic content to load
                        await asyncio.sleep(5)
                        try:
                            await self._page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass

                        return AgentResult(success=True, output=f"Logged in to {site_name}")
                    elif "system.gobranded" in url:
                        logger.warning(f"Bot detection (attempt {attempt+1}): {url}")
                        await asyncio.sleep(5)
                        continue
                    else:
                        return AgentResult(success=False, error=f"Login failed, at: {url}")
                except Exception as e:
                    logger.warning(f"Login error (attempt {attempt+1}): {e}")
                    await asyncio.sleep(5)
                    continue

        return AgentResult(success=False, error="Login failed after 3 attempts (bot detection)")

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        """Find available surveys on the dashboard."""
        if dashboard_url not in self._page.url:
            await self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        # Find survey links
        surveys = await self._page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="/surveys/pre/"], a[href*="/router/go"], a[href*="/qualifications/"]');
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
        # Step 1: Find survey links on dashboard
        # Wait for dynamic content
        await asyncio.sleep(3)
        try:
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # Find pre-survey links (real surveys) — navigate directly, don't click
        survey_url = await self._page.evaluate("""() => {
            const pre = document.querySelector('a[href*="/surveys/pre/"]');
            if (pre) return pre.href;
            // Fallback to qualifications
            const qual = document.querySelector('a[href*="/qualifications/"]');
            if (qual) return qual.href;
            // Fallback to router (refreshes survey list)
            const router = document.querySelector('a[href*="/router/go"]');
            if (router) return router.href;
            return '';
        }""")

        if not survey_url:
            return AgentResult(success=True, output="No surveys available")

        logger.info(f"Survey URL: {survey_url[:80]}")

        # Navigate directly to the survey URL (don't click — clicking redirects differently)
        await self._page.goto(survey_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        # Check if redirected back to dashboard
        current_url = self._page.url
        if "/users/dashboard" in current_url.lower():
            # Survey was full — try next one or qualifications
            qual_url = await self._page.evaluate("""() => {
                const l = document.querySelector('a[href*="/qualifications/"]');
                return l ? l.href : '';
            }""")
            if qual_url:
                logger.info("Survey full, trying qualifications")
                await self._page.goto(qual_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(5)
            else:
                return AgentResult(success=True, output="No surveys available (all redirected to dashboard)")

        # Step 2: Click TAKE SURVEY (only if on gobranded pre-page)
        url_after_nav = self._page.url
        if "survey" in url_after_nav.lower() or "gobranded" in url_after_nav.lower():
            try:
                await self._page.evaluate("""() => {
                    const els = document.querySelectorAll('a, button, input[type="submit"]');
                    for (const el of els) {
                        const text = (el.textContent || el.value || '').trim().toUpperCase();
                        if (text === 'TAKE SURVEY') { el.click(); return true; }
                    }
                    return false;
                }""")
            except Exception:
                pass  # Navigation destroys context — that's expected
            await asyncio.sleep(8)

        # Step 3: Click BEGIN SURVEY (on external provider page)
        try:
            await self._page.evaluate("""() => {
                const els = document.querySelectorAll('a, button, input[type="submit"]');
                for (const el of els) {
                    const text = (el.textContent || el.value || '').trim().toUpperCase();
                    if (text === 'BEGIN SURVEY' || text === 'START SURVEY') { el.click(); return true; }
                }
                return false;
            }""")
        except Exception:
            pass  # Navigation destroys context — that's expected
        await asyncio.sleep(8)

        # Step 4: Handle consent page
        await self._handle_consent()

        # Step 5: Answer questions in a loop
        max_questions = 50
        last_question = ""
        same_count = 0
        no_option_count = 0

        for i in range(max_questions):
            # Check for completion/disqualification
            try:
                url = self._page.url
                body = await self._page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
            except Exception as nav_err:
                logger.warning(f"Q{i+1} page error (navigation?): {nav_err}")
                await asyncio.sleep(3)
                continue
            body_lower = body.lower()

            if any(kw in body_lower for kw in ["thank you", "survey complete", "successfully completed"]):
                return AgentResult(success=True, output=f"Survey completed after {i} questions")
            if any(kw in body_lower for kw in ["not a match", "disqualified", "screened out"]):
                return AgentResult(success=False, error="Disqualified from survey")
            if "survey full" in body_lower:
                return AgentResult(success=False, error="Survey full")
            if "/members/oq" in url or ("/members/" in url and "/surveys/" not in url):
                return AgentResult(success=False, error="Redirected to members page (survey rejected)")
            if any(kw in body_lower for kw in ["recommend branded surveys", "my top surveys", "daily poll", "branded elite", "points until payout"]):
                return AgentResult(success=False, error="Back on Branded Surveys dashboard/feedback page")
            if "dashboard" in url.lower() and "survey" not in url.lower():
                return AgentResult(success=False, error="Redirected to dashboard (likely disqualified or full)")

            # Detect CAPTCHA/verification codes — solve via Groq vision
            if any(kw in body_lower for kw in ["type the following code", "enter the code", "captcha", "verification code", "prove you are human"]):
                logger.info(f"Q{i+1}: CAPTCHA detected, attempting vision solve")
                await asyncio.sleep(10)  # Rate limit: wait between CAPTCHA attempts
                solved = await self._solve_captcha_vision()
                if solved:
                    # Click Next/Submit after solving
                    await self._page.evaluate("""() => {
                        const btns = document.querySelectorAll('button, input[type="submit"]');
                        for (const b of btns) {
                            const t = (b.textContent || b.value || '').trim().toUpperCase();
                            if (t.includes('NEXT') || t.includes('SUBMIT') || t.includes('CONTINUE')) { b.click(); return; }
                        }
                    }""")
                    await asyncio.sleep(15)  # Wait for page to load after CAPTCHA
                    # Wait for page content to stabilize (up to 30s total)
                    for wait_i in range(10):
                        body_check = await self._page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
                        if body_check and len(body_check.strip()) > 30:
                            # Check if page has actual content (not just loading percentage)
                            import re as _re2
                            if not _re2.match(r'^\d+%$', body_check.strip()) and 'loading' not in body_check.lower():
                                break
                        await asyncio.sleep(3)
                    no_option_count = 0  # Reset counter after CAPTCHA solve
                    continue
                else:
                    logger.info(f"Q{i+1}: CAPTCHA solve failed, skipping survey")
                    return AgentResult(success=False, error="CAPTCHA/verification code — vision solve failed")

            # Detect timer-based questions ("click NEXT after N seconds")
            import re as _re
            timer_match = _re.search(r"(?:click|press).*?(?:next|continue).*?after\s+(\d+)\s*sec", body_lower)
            if timer_match:
                wait_secs = int(timer_match.group(1))
                logger.info(f"Q{i+1}: Timer question detected, waiting {wait_secs} seconds")
                await asyncio.sleep(wait_secs + 2)
                # Then click Next
                await self._page.evaluate("""() => {
                    const btns = document.querySelectorAll('button, input[type="submit"]');
                    for (const b of btns) {
                        const t = (b.textContent || b.value || '').trim().toUpperCase();
                        if (t.includes('NEXT') || t.includes('CONTINUE')) { b.click(); return; }
                    }
                }""")
                await asyncio.sleep(3)
                continue

            # Get question and options (including standalone labels)
            question_data = await self._page.evaluate("""() => {
                // Find question text
                let question = '';
                const textNodes = document.body.innerText.split('\\n');
                for (const line of textNodes) {
                    const t = line.trim();
                    if (!t || t.length < 10) continue;
                    // Skip navigation/progress/widget elements
                    if (t.toLowerCase().includes('technical problem') || t.toLowerCase().includes('click here') || t.toLowerCase().includes('select language') || t.toLowerCase().includes('translate') || t.toLowerCase().includes('chinese') || t.toLowerCase().includes('traditional') || t.toLowerCase().includes('simplified')) continue;
                    // Look for question indicators
                    if (t.includes('?') || t.toLowerCase().includes('select') || t.toLowerCase().includes('how') || t.toLowerCase().includes('what') || t.toLowerCase().includes('which') || t.toLowerCase().includes('have you')) {
                        question = t.substring(0, 300);
                        break;
                    }
                    if (t.length > 20 && !question) question = t.substring(0, 300);
                }

                const options = [];

                // Get standalone labels (common in survey providers) — skip Google Translate
                const labels = document.querySelectorAll('label');
                labels.forEach(l => {
                    if (l.offsetParent === null) return;
                    if (l.closest('.goog-te-combo') || l.closest('[class*="translate"]') || l.closest('.skiptranslate') || l.closest('#google_translate_element')) return;
                    const text = l.textContent.trim().substring(0, 80);
                    if (text.length > 1) {
                        options.push({type: 'label', name: '', id: l.id || '', label: text});
                    }
                });

                // Get select dropdowns — skip Google Translate
                const selects = document.querySelectorAll('select');
                selects.forEach(s => {
                    if (s.offsetParent === null) return;
                    if (s.closest('.goog-te-combo') || s.closest('[class*="translate"]') || s.closest('.skiptranslate') || s.closest('#google_translate_element')) return;
                    // Skip if this select has language options (Google Translate)
                    const firstOpt = s.options[0]?.text || '';
                    if (firstOpt.match(/^(Select|Afrikaans|Chinese|Spanish|French|German|Japanese|Korean)/i)) return;
                    Array.from(s.options).forEach(o => {
                        if (o.value && o.text.trim() !== 'Select one') {
                            options.push({type: 'select', name: s.name, id: s.id, label: o.text.trim().substring(0, 80)});
                        }
                    });
                });

                // Get text inputs
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
                # Try clicking Continue/Next buttons (handles consent pages, intro pages)
                clicked = await self._page.evaluate("""() => {
                    const btns = document.querySelectorAll('button, input[type="submit"], a');
                    for (const b of btns) {
                        const t = (b.textContent || b.value || '').trim().toUpperCase();
                        if (t.includes('CONTINUE') || t === 'NEXT' || t === 'START' || t === 'BEGIN' || t.includes('I AGREE') || t.includes('ACCEPT')) {
                            if (b.offsetParent !== null) { b.click(); return true; }
                        }
                    }
                    return false;
                }""")
                if clicked:
                    logger.info(f"Q{i+1}: No labels/text but clicked button")
                    await asyncio.sleep(3)
                    no_option_count = 0
                    continue
                no_option_count += 1
                if no_option_count >= 5:
                    logger.info(f"Q{i+1}: No options found after {no_option_count} iterations, page body: {body[:200]}")
                    return AgentResult(success=False, error=f"No survey options found after {no_option_count} iterations (page may have redirected)")
                await asyncio.sleep(2)
                continue
            else:
                no_option_count = 0

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
                # Click matching option — try labels first, then radio, then select
                answer_clean = answer.strip().lower()[:40].replace("'", "\\'")
                js_code = (
                    "(() => {"
                    "const labels = document.querySelectorAll('label');"
                    "for (const l of labels) {"
                    "if (l.offsetParent === null) continue;"
                    "if (l.textContent.trim().toLowerCase().includes('" + answer_clean + "')) {"
                    "l.click();"
                    "return 'clicked label: ' + l.textContent.trim();}}"
                    "const els = document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]');"
                    "for (const el of els) {"
                    "const label = el.closest('label')?.textContent?.trim() || '';"
                    "if (label.toLowerCase().includes('" + answer_clean + "')) {"
                    "el.click();"
                    "return 'clicked radio: ' + label;}}"
                    "const selects = document.querySelectorAll('select');"
                    "for (const sel of selects) {"
                    "for (const opt of sel.options) {"
                    "if (opt.text.toLowerCase().includes('" + answer_clean + "')) {"
                    "sel.value = opt.value;"
                    "sel.dispatchEvent(new Event('change', {bubbles: true}));"
                    "return 'selected: ' + opt.text;}}}"
                    "return 'no match';})()"
                )
                clicked = await self._page.evaluate(js_code)
                if clicked.startswith("no match"):
                    logger.warning(f"No match for answer: {answer_clean[:50]}")

            await asyncio.sleep(1)

            # Click Next/Submit — try XX_NXT first, then standard buttons
            await self._page.evaluate("""() => {
                // Try XX_NXT button first (common in survey providers)
                const nxt = document.querySelector('input[type="submit"][name="XX_NXT"]');
                if (nxt && nxt.offsetParent !== null) { nxt.click(); return; }
                // Try standard buttons
                const btns = document.querySelectorAll('button, input[type="submit"], a.btn');
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim().toUpperCase();
                    if (text === 'NEXT' || text === 'SUBMIT' || text === 'CONTINUE' || text === '>>' || text === 'NEXT >' || text === 'START >>') {
                        btn.click();
                        return;
                    }
                }
            }""")
            await asyncio.sleep(3)

        return AgentResult(success=True, output=f"Answered {max_questions} questions (loop limit)")

    async def _solve_captcha_vision(self) -> bool:
        """Screenshot CAPTCHA area and solve via Groq vision model."""
        try:
            from ..captcha.solver import create_vision_llm_fn  # noqa: PLC0415

            # Screenshot the page — use JPEG to reduce token usage
            screenshot = await self._page.screenshot(type="jpeg", quality=50)
            if not screenshot:
                logger.warning("CAPTCHA: Could not get screenshot")
                return False

            # Send to Groq vision
            vision_fn = create_vision_llm_fn(provider="groq")
            if not vision_fn:
                logger.warning("CAPTCHA: No vision function available (missing API key?)")
                return False

            prompt = "Look at this image. Find the CAPTCHA verification code (distorted text/characters). Reply with ONLY the code characters, no explanation, no reasoning, no thinking. Just the code."
            answer = await asyncio.to_thread(vision_fn, prompt, screenshot)

            if not answer:
                logger.warning("CAPTCHA: Vision model returned empty response")
                return False

            # Clean up the answer — extract from <think> tags if present
            answer = answer.strip().strip('"').strip("'").strip()
            # Remove <think>...</think> blocks (Qwen chain-of-thought)
            import re as _re
            answer = _re.sub(r'<think>.*?</think>', '', answer, flags=_re.DOTALL).strip()
            # Also remove unclosed <think> blocks (partial responses)
            answer = _re.sub(r'<think>.*', '', answer, flags=_re.DOTALL).strip()
            # Extract just alphanumeric code (CAPTCHAs are usually 4-8 chars with mixed case/numbers)
            code_match = _re.search(r'[A-Za-z0-9]{4,8}', answer)
            if code_match:
                candidate = code_match.group(0)
                # Skip common words that aren't CAPTCHA codes
                if candidate.lower() not in ('think', 'think', 'image', 'shows', 'code', 'text', 'answer', 'captcha', 'verify', 'enter'):
                    answer = candidate
            logger.info(f"CAPTCHA: Vision model answered: '{answer}'")

            # Fill the answer into the text input
            safe_answer = answer.replace("'", "\\'")
            filled = await self._page.evaluate(f"""() => {{
                const inputs = document.querySelectorAll('input[type="text"], textarea');
                for (const inp of inputs) {{
                    if (inp.offsetParent !== null && !inp.disabled) {{
                        inp.value = '{safe_answer}';
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                }}
                return false;
            }}""")

            if filled:
                logger.info(f"CAPTCHA: Filled '{answer}' into input")
                return True
            else:
                logger.warning("CAPTCHA: Could not find text input to fill")
                return False

        except Exception as e:
            logger.warning(f"CAPTCHA vision solve error: {e}")
            return False

    async def _handle_consent(self) -> None:
        """Handle consent/agreement pages and cookie banners."""
        try:
            # Dismiss cookie banners first (common on survey provider pages)
            await self._page.evaluate("""() => {
                // Cookie consent buttons
                const cookieTexts = ['ACCEPT ALL', 'ACCEPT COOKIES', 'I AGREE', 'AGREE', 'OK', 'GOT IT'];
                const btns = document.querySelectorAll('button, a, input[type="submit"]');
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim().toUpperCase();
                    for (const ct of cookieTexts) {
                        if (text === ct && btn.offsetParent !== null) {
                            btn.click();
                            return;
                        }
                    }
                }
            }""")
            await asyncio.sleep(2)

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

            # Click Accept/START
            await self._page.evaluate("""() => {
                const els = document.querySelectorAll('a, button, input[type="submit"]');
                for (const el of els) {
                    const text = (el.textContent || el.value || '').trim().toUpperCase();
                    if (text.includes('ACCEPT') || text.includes('AGREE') || text === 'I AGREE' || text === 'START >>' || text === 'BEGIN SURVEY') {
                        el.click();
                        return;
                    }
                }
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
