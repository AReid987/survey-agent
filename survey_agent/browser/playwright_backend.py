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

            # Vision fallback for challenges (CAPTCHA, drag-and-drop, slider, etc.)
            if any(kw in body_lower for kw in [
                "type the following code", "enter the code", "captcha", "verification code",
                "prove you are human", "drag and drop", "drag the", "drop the", "move the",
                "slide the", "slider"
            ]):
                logger.info(f"Q{i+1}: Visual challenge detected, using vision fallback")
                await asyncio.sleep(8)
                action_taken, action_desc = await self._vision_handle_page(body)
                logger.info(f"Q{i+1}: Vision result: {action_desc}")
                if action_taken:
                    await asyncio.sleep(5)
                    no_option_count = 0
                    continue
                else:
                    return AgentResult(success=False, error=f"Vision challenge failed: {action_desc}")

            # Timer questions
            import re as _re
            timer_match = _re.search(r"(?:click|press).*?(?:next|continue).*?after\s+(\d+)\s*sec", body_lower)
            if timer_match:
                wait_secs = int(timer_match.group(1))
                logger.info(f"Q{i+1}: Timer question, waiting {wait_secs}s")
                await asyncio.sleep(wait_secs + 2)
                await self._click_next_button()
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
                    # Check if page actually changed
                    new_body = await self._page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
                    if new_body == body:
                        # Page didn't change — button click didn't work
                        no_option_count += 1
                        if no_option_count >= 3:
                            logger.info(f"Q{i+1}: Button clicks not changing page after {no_option_count} attempts")
                            return AgentResult(success=False, error="Page not advancing after button clicks")
                    else:
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

If there are label/radio/select options above, reply with the EXACT text of the best option (e.g. "Male" or "Texas").
ONLY use "TEXT: your answer" if there are NO label/radio/select options and a text input is needed."""

            try:
                answer = await self._llm_fn(prompt)
            except Exception as e:
                logger.warning(f"LLM call failed: {e}")
                answer = ""

            if not answer or answer == "None":
                await asyncio.sleep(1)
                continue

            # If LLM returned "TEXT:" but we have label options, strip the prefix
            if answer.startswith("TEXT:") and any(o.get("type") == "label" for o in options):
                answer = answer[5:].strip()
                logger.info(f"Stripped TEXT prefix, using label: {answer[:40]}")

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
                    "const answer = '" + answer_clean + "';"
                    "const labels = document.querySelectorAll('label');"
                    "for (const l of labels) {"
                    "if (l.offsetParent === null) continue;"
                    "if (l.textContent.trim().toLowerCase().includes(answer)) {"
                    "const forId = l.getAttribute('for');"
                    "const radio = forId ? document.getElementById(forId) : l.querySelector('input[type=\"radio\"], input[type=\"checkbox\"]');"
                    "if (radio) {"
                    "radio.checked = true;"
                    "radio.dispatchEvent(new Event('change', {bubbles: true}));"
                    "radio.dispatchEvent(new Event('input', {bubbles: true}));"
                    "radio.dispatchEvent(new Event('click', {bubbles: true}));"
                    "}"
                    "l.click();"
                    "return 'clicked label+radio: ' + l.textContent.trim();}}"
                    "const els = document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]');"
                    "for (const el of els) {"
                    "const label = el.closest('label')?.textContent?.trim() || "
                    "document.querySelector('label[for=\"' + el.id + '\"]')?.textContent?.trim() || '';"
                    "if (label.toLowerCase().includes(answer)) {"
                    "el.checked = true;"
                    "el.dispatchEvent(new Event('change', {bubbles: true}));"
                    "el.dispatchEvent(new Event('input', {bubbles: true}));"
                    "el.click();"
                    "return 'clicked radio: ' + label;}}"
                    "const selects = document.querySelectorAll('select');"
                    "for (const sel of selects) {"
                    "for (const opt of sel.options) {"
                    "if (opt.text.toLowerCase().includes(answer)) {"
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

    async def _solve_drag_drop(self) -> bool:
        """Solve drag-and-drop challenges using vision + Playwright drag."""
        try:
            from ..captcha.solver import create_vision_llm_fn  # noqa: PLC0415

            # Screenshot the page
            screenshot = await self._page.screenshot(type="jpeg", quality=50)
            if not screenshot:
                return False

            vision_fn = create_vision_llm_fn(provider="cerebras")
            if not vision_fn:
                return False

            # Ask vision model to find the drag source and drop target coordinates
            prompt = """This image shows a drag-and-drop challenge. Find:
1. The source element (the number/item to drag)
2. The target area (the box/zone to drop it into)

Reply in this exact format:
SOURCE: x,y
TARGET: x,y
where x,y are pixel coordinates."""
            answer = await asyncio.to_thread(vision_fn, prompt, screenshot)
            if not answer:
                return False

            logger.info(f"Drag-drop vision: {answer[:200]}")

            # Parse coordinates
            import re as _re
            source_match = _re.search(r'SOURCE:\s*(\d+)\s*,\s*(\d+)', answer)
            target_match = _re.search(r'TARGET:\s*(\d+)\s*,\s*(\d+)', answer)
            if not source_match or not target_match:
                logger.warning("Drag-drop: Could not parse coordinates from vision response")
                return False

            sx, sy = int(source_match.group(1)), int(source_match.group(2))
            tx, ty = int(target_match.group(1)), int(target_match.group(2))
            logger.info(f"Drag-drop: ({sx},{sy}) -> ({tx},{ty})")

            # Try HTML5 drag-and-drop via JavaScript (more reliable than mouse events)
            drag_result = await self._page.evaluate(f"""() => {{
                // Find element at source coordinates
                const srcEl = document.elementFromPoint({sx}, {sy});
                if (!srcEl) return 'no source element';
                
                // Find element at target coordinates
                const tgtEl = document.elementFromPoint({tx}, {ty});
                if (!tgtEl) return 'no target element';
                
                // Dispatch HTML5 drag events
                const dt = new DataTransfer();
                
                const dragStart = new DragEvent('dragstart', {{ bubbles: true, cancelable: true, dataTransfer: dt }});
                srcEl.dispatchEvent(dragStart);
                
                const drag = new DragEvent('drag', {{ bubbles: true, cancelable: true, dataTransfer: dt }});
                srcEl.dispatchEvent(drag);
                
                const dragOver = new DragEvent('dragover', {{ bubbles: true, cancelable: true, dataTransfer: dt }});
                tgtEl.dispatchEvent(dragOver);
                
                const drop = new DragEvent('drop', {{ bubbles: true, cancelable: true, dataTransfer: dt }});
                tgtEl.dispatchEvent(drop);
                
                const dragEnd = new DragEvent('dragend', {{ bubbles: true, cancelable: true, dataTransfer: dt }});
                srcEl.dispatchEvent(dragEnd);
                
                return 'html5 drag done: ' + srcEl.tagName + ' -> ' + tgtEl.tagName;
            }}""")
            logger.info(f"Drag-drop HTML5: {drag_result}")

            # Also try mouse-based drag as fallback
            await self._page.mouse.move(sx, sy)
            await self._page.mouse.down()
            await asyncio.sleep(0.5)
            steps = 10
            for s in range(steps + 1):
                cx = sx + (tx - sx) * s // steps
                cy = sy + (ty - sy) * s // steps
                await self._page.mouse.move(cx, cy)
                await asyncio.sleep(0.05)
            await self._page.mouse.up()
            await asyncio.sleep(2)

            logger.info("Drag-drop: Completed drag action")
            return True

        except Exception as e:
            logger.warning(f"Drag-drop solve error: {e}")
            return False

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
            vision_fn = create_vision_llm_fn(provider="cerebras")
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


    async def _vision_handle_page(self, body_text: str) -> tuple[bool, str]:
        """Unified vision fallback — screenshot the page and let the vision model decide what to do.
        
        Returns (action_taken, action_description).
        """
        try:
            from ..captcha.solver import create_vision_llm_fn  # noqa: PLC0415

            vision_fn = create_vision_llm_fn(provider="cerebras")
            if not vision_fn:
                return False, "no vision function"

            screenshot = await self._page.screenshot(type="jpeg", quality=50)
            if not screenshot:
                return False, "no screenshot"

            prompt = f"""You are a survey automation assistant. Look at this screenshot of a survey page and determine what action to take.

Page text (first 300 chars):
{body_text[:300]}

Respond with ONE of these commands:
- CAPTCHA: <code> — if there's a verification code to read
- DRAG: x1,y1 TO x2,y2 — if there's a drag-and-drop challenge
- SLIDE: x1,y1 TO x2,y2 — if there's a slider to move
- TYPE: <text> — if there's a text input that needs a specific answer
- CLICK: <text> — if there's a button or option to click
- NEXT — if the page has a Next/Continue button to click
- WAIT: <seconds> — if the page says to wait before proceeding
- DONE — if the survey is complete
- SKIP — if you can't determine what to do

Reply with ONLY the command, nothing else."""

            answer = await asyncio.to_thread(vision_fn, prompt, screenshot)
            if not answer:
                return False, "empty response"

            import re as _re
            answer = _re.sub(r'<think>.*?</think>', '', answer, flags=_re.DOTALL).strip()
            answer = _re.sub(r'<think>.*', '', answer, flags=_re.DOTALL).strip()
            answer = answer.strip().strip('"').strip("'")

            for line in answer.split('\n'):
                line = line.strip()
                upper = line.upper()
                if any(upper.startswith(cmd) for cmd in ['CAPTCHA:', 'CAPTCHA ', 'DRAG:', 'DRAG ', 'SLIDE:', 'SLIDE ', 'TYPE:', 'TYPE ', 'CLICK:', 'CLICK ', 'NEXT', 'WAIT:', 'WAIT ', 'DONE', 'SKIP']):
                    answer = line
                    break

            logger.info(f"Vision command: {answer[:100]}")

            if answer.upper().startswith('CAPTCHA:') or answer.upper().startswith('CAPTCHA '):
                code = answer.split(':', 1)[-1].strip() if ':' in answer else answer.split(' ', 1)[-1].strip()
                code_match = _re.search(r'[A-Za-z0-9]{4,8}', code)
                if code_match:
                    code = code_match.group(0)
                else:
                    return False, "no code in CAPTCHA response"
                safe_code = code.replace("'", "\\'")
                filled = await self._page.evaluate(f"""() => {{
                    const inputs = document.querySelectorAll('input[type="text"], textarea');
                    for (const inp of inputs) {{
                        if (inp.offsetParent !== null && !inp.disabled) {{
                            inp.value = '{safe_code}';
                            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                if filled:
                    await self._click_next_button()
                    await asyncio.sleep(10)
                    return True, f"filled CAPTCHA: {code}"
                return False, "no text input for CAPTCHA"

            elif answer.upper().startswith('DRAG:') or answer.upper().startswith('DRAG ') or answer.upper().startswith('SLIDE:') or answer.upper().startswith('SLIDE '):
                coords = _re.findall(r'(\d+)\s*,\s*(\d+)', answer)
                if len(coords) >= 2:
                    sx, sy = int(coords[0][0]), int(coords[0][1])
                    tx, ty = int(coords[1][0]), int(coords[1][1])
                    logger.info(f"Drag/Slide: ({sx},{sy}) -> ({tx},{ty})")
                    await self._page.evaluate(f"""() => {{
                        const srcEl = document.elementFromPoint({sx}, {sy});
                        const tgtEl = document.elementFromPoint({tx}, {ty});
                        if (!srcEl || !tgtEl) return;
                        const dt = new DataTransfer();
                        srcEl.dispatchEvent(new DragEvent('dragstart', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                        tgtEl.dispatchEvent(new DragEvent('dragover', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                        tgtEl.dispatchEvent(new DragEvent('drop', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                        srcEl.dispatchEvent(new DragEvent('dragend', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                    }}""")
                    await self._page.mouse.move(sx, sy)
                    await self._page.mouse.down()
                    await asyncio.sleep(0.3)
                    steps = 15
                    for s in range(steps + 1):
                        cx = sx + (tx - sx) * s // steps
                        cy = sy + (ty - sy) * s // steps
                        await self._page.mouse.move(cx, cy)
                        await asyncio.sleep(0.03)
                    await self._page.mouse.up()
                    await asyncio.sleep(2)
                    return True, f"dragged ({sx},{sy})->({tx},{ty})"
                return False, "no coordinates in DRAG response"

            elif answer.upper().startswith('TYPE:') or answer.upper().startswith('TYPE '):
                text = answer.split(':', 1)[-1].strip() if ':' in answer else answer.split(' ', 1)[-1].strip()
                filled = await self._page.evaluate(f"""() => {{
                    const inputs = document.querySelectorAll('input[type="text"], textarea');
                    for (const inp of inputs) {{
                        if (inp.offsetParent !== null && !inp.disabled) {{
                            inp.value = '{safe_text}';
                            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                if filled:
                    await self._click_next_button()
                    await asyncio.sleep(3)
                    return True, f"typed: {text[:30]}"
                return False, "no text input found"

            elif answer.upper().startswith('CLICK:') or answer.upper().startswith('CLICK '):
                text = answer.split(':', 1)[-1].strip() if ':' in answer else answer.split(' ', 1)[-1].strip()
                clicked = await self._page.evaluate(f"""() => {{
                    const els = document.querySelectorAll('label, button, a, input[type="radio"], input[type="checkbox"]');
                    for (const el of els) {{
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || el.value || '').trim();
                        if (t.toLowerCase().includes('{text.lower()}')) {{
                            const radio = el.querySelector ? el.querySelector('input[type="radio"]') : null;
                            if (radio) {{ radio.checked = true; radio.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                            el.click();
                            return 'clicked: ' + t;
                        }}
                    }}
                    return 'not found';
                }}""")
                if 'clicked' in clicked:
                    await asyncio.sleep(2)
                    return True, clicked
                return False, f"element not found: {text}"

            elif answer.upper() == 'NEXT' or answer.upper().startswith('NEXT'):
                await self._click_next_button()
                await asyncio.sleep(3)
                return True, "clicked next"

            elif answer.upper().startswith('WAIT:') or answer.upper().startswith('WAIT '):
                secs = int(_re.search(r'\d+', answer).group(0)) if _re.search(r'\d+', answer) else 5
                await asyncio.sleep(min(secs, 30))
                await self._click_next_button()
                await asyncio.sleep(3)
                return True, f"waited {secs}s + clicked next"

            elif answer.upper() == 'DONE':
                return True, "survey done"

            elif answer.upper() == 'SKIP':
                return False, "vision says skip"

            return False, f"unrecognized command: {answer[:50]}"

        except Exception as e:
            logger.warning(f"Vision handle error: {e}")
            return False, str(e)

    async def _click_next_button(self) -> None:
        """Click Next/Continue/Submit button."""
        await self._page.evaluate("""() => {
            const btns = document.querySelectorAll('button, input[type="submit"]');
            for (const b of btns) {
                const t = (b.textContent || b.value || '').trim().toUpperCase();
                if (t.includes('NEXT') || t.includes('SUBMIT') || t.includes('CONTINUE')) {
                    b.click(); return;
                }
            }
        }""")


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
                // Also try by class (PureSpectrum, CookieYes, etc.)
                const classBtns = document.querySelectorAll('.cky-btn-accept, .cky-btn.cky-btn-accept, [class*="cookie"] [class*="accept"], [id*="cookie"] [class*="accept"]');
                for (const btn of classBtns) {
                    if (btn.offsetParent !== null) { btn.click(); return; }
                }
            }""")
            await asyncio.sleep(2)
            # Second pass — sometimes cookie banner needs two clicks
            await self._page.evaluate("""() => {
                const btns = document.querySelectorAll('.cky-btn-accept, button');
                for (const btn of btns) {
                    const text = (btn.textContent || '').trim().toUpperCase();
                    if ((text === 'ACCEPT ALL' || btn.classList.contains('cky-btn-accept')) && btn.offsetParent !== null) {
                        btn.click(); return;
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
