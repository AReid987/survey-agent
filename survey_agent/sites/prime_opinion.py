"""Prime Opinion adapter — full survey platform integration.

Prime Opinion (app.primeopinion.com) uses Google OAuth for login.
The agent Chrome inherits Google session cookies, so login should work
without credential entry.

Survey flow:
1. Dashboard shows available surveys with reward amounts
2. Click "Start Survey" to begin
3. Questions appear as standard HTML (radio, checkbox, select, text)
4. Survey routes through third-party providers (SameSite, etc.)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..answering import llm_answers as llm_ans
from ..browser.engine import BrowserPage
from ..browser.human_behavior import HumanBehavior
from ..captcha.solver import CaptchaDetector
from ..profile.store import ProfileStore
from .base import Answer, Question, SiteAdapter, SiteBalance, Survey, WithdrawalResult
from .registry import register

logger = logging.getLogger(__name__)

# Profile keyword map for deterministic answering
_PROFILE = {
    "gender": "male",
    "age": "39",
    "marital": "domestic partnership",
    "income": "50000-74999",
    "education": "trade/vocational",
    "employment": "self-employed",
    "race": "black",
    "household": "5",
    "children": "yes",
    "homeowner": "rent",
    "smoker": "former",
    "alcohol": "never",
    "hispanic": "no",
    "exercise": "yes",
    "health": "very good",
}


@register("prime-opinion")
class PrimeOpinionAdapter(SiteAdapter):
    """Adapter for app.primeopinion.com.

    Uses Google OAuth for login. The harness backend provides the
    browser and generic survey navigation; this adapter handles
    site-specific question parsing and answer generation.
    """

    def __init__(
        self,
        credential_store=None,
        profile_store: ProfileStore | None = None,
        google_email: str = "",
    ):
        super().__init__(credential_store, profile_store)
        self._behavior = HumanBehavior()
        self._google_email = google_email

    @property
    def site_slug(self) -> str:
        return "prime-opinion"

    @property
    def base_url(self) -> str:
        return "https://app.primeopinion.com"

    @property
    def display_name(self) -> str:
        return "Prime Opinion"

    async def login(self, page: BrowserPage) -> bool:
        """Log into Prime Opinion. Assumes Google session cookies are active.

        The agent Chrome profile should have Google cookies synced from
        the user's real Chrome (handled by cookie_sync module).
        """
        await page.goto(f"{self.base_url}/login")
        await self._behavior.random_delay(2.0)

        if await self.is_logged_in(page):
            logger.info("Already logged in (session cookie valid)")
            return True

        # Click "Sign in with Google" button
        logger.info("Attempting Google OAuth login...")
        google_btn = await page.query_selector(
            "a:has-text('Google'), button:has-text('Google'), "
            "[class*='google'], [data-provider='google'], "
            "button:has-text('Sign in with Google'), "
            "a:has-text('Sign in with Google')"
        )
        if google_btn:
            await google_btn.click()
            await self._behavior.random_delay(5.0)

            # After clicking, Google should auto-redirect because cookies are present
            # Wait for redirect back to Prime Opinion
            for _ in range(10):
                url = await page.url()
                if "primeopinion.com" in url and "login" not in url.lower():
                    break
                await self._behavior.random_delay(1.0)

        return await self.is_logged_in(page)

    async def is_logged_in(self, page: BrowserPage) -> bool:
        url = await page.url()
        content = await page.content()

        # Check for dashboard indicators
        logged_in = (
            "/surveys" in url
            or "/dashboard" in url
            or "/account" in url
            or "sign in" not in content.lower()
            and "login" not in url.lower()
        )
        return logged_in

    async def get_surveys(self, page: BrowserPage) -> list[Survey]:
        await page.goto(f"{self.base_url}/surveys")
        await self._behavior.random_delay(2.0)

        surveys = []

        # Prime Opinion shows survey cards with reward amounts
        survey_cards = await page.query_selector_all(
            "[class*='survey'], [class*='offer'], [class*='card'], "
            ".survey-item, .offer-item, a[href*='survey']"
        )

        for card in survey_cards:
            try:
                text = await card.evaluate("el => el.textContent?.trim() || ''")
                href = await card.evaluate(
                    "el => el.getAttribute('href') || "
                    "el.querySelector('a')?.getAttribute('href') || ''"
                )

                if not text or len(text) < 5:
                    continue

                # Extract reward amount ($X.XX)
                reward_match = re.search(r"\$?\s*(\d+\.?\d*)", text)
                reward = float(reward_match.group(1)) if reward_match else 0.0

                # Extract estimated minutes
                minutes_match = re.search(r"(\d+)\s*(?:min|minutes|mins)", text, re.I)
                minutes = int(minutes_match.group(1)) if minutes_match else 0

                survey_url = href if href.startswith("http") else f"{self.base_url}{href}" if href else ""

                surveys.append(
                    Survey(
                        id=str(abs(hash(text)) % 1000000),
                        title=text[:100],
                        url=survey_url,
                        estimated_minutes=minutes,
                        reward=reward,
                    )
                )
            except Exception:
                continue

        logger.info(f"Found {len(surveys)} surveys on Prime Opinion")
        return surveys

    async def start_survey(self, page: BrowserPage, survey: Survey) -> bool:
        if not survey.url:
            # Try clicking a survey card on the current page
            start_btn = await page.query_selector(
                "button:has-text('Start'), button:has-text('Begin'), "
                "a:has-text('Start'), [class*='start']"
            )
            if start_btn:
                await start_btn.click()
                await self._behavior.random_delay(3.0)
                return True
            return False

        await page.goto(survey.url)
        await self._behavior.random_delay(3.0)
        return await self._has_questions(page)

    async def _has_questions(self, page: BrowserPage) -> bool:
        content = await page.content()
        return bool(
            re.search(
                r"(?:question|survey|select|choose|rate|agree|disagree|"
                r"input|radio|checkbox|select|textarea)",
                content,
                re.I,
            )
        )

    async def get_current_question(self, page: BrowserPage) -> Question | None:
        content = await page.content()

        # Completion check
        if re.search(
            r"(?:thank\s+you|complete|finished|congratulations|survey\s+complete)",
            content,
            re.I,
        ):
            return None

        # Disqualification check
        if re.search(
            r"(?:not a match|disqualified|do not qualify|not eligible|screened out)",
            content,
            re.I,
        ):
            logger.info("Disqualified from Prime Opinion survey")
            return None

        # Find question text
        question_el = await page.query_selector(
            ".question-text, .question_text, .survey-question, "
            "[class*='question-text'], [class*='questionText'], "
            "legend, h2, h3, .prompt, [class*='prompt'], "
            ".title, .heading, [class*='title']"
        )
        text = ""
        if question_el:
            text = await question_el.evaluate("el => el.textContent?.trim() || ''")

        if not text or len(text) < 3:
            # Try page title as fallback
            text = await page.evaluate("document.title")
            if len(text) < 3:
                return None

        html_context = content
        q_type = self._classify_from_context(html_context)
        options = await self._extract_options(page)

        return Question(
            id=str(abs(hash(text)) % 1000000),
            text=text[:200],
            question_type=q_type,
            options=options,
        )

    async def answer_question(self, page: BrowserPage, question: Question, answer: Answer) -> bool:
        try:
            qtype = question.question_type
            value = answer.value

            if qtype == "radio":
                return await self._answer_radio(page, str(value), question.options)
            elif qtype == "checkbox":
                return await self._answer_checkbox(page, value, question.options)
            elif qtype in ("text", "textarea", "open_ended"):
                return await self._answer_text(page, str(value))
            elif qtype == "select":
                return await self._answer_select(page, str(value))
            elif qtype == "scale":
                return await self._answer_scale(page, value)
            elif qtype == "slider":
                return await self._answer_slider(page, value)
            else:
                return await self._answer_text(page, str(value))
        except Exception as e:
            logger.error(f"Prime Opinion answer error: {e}")
            return False

    async def submit_survey(self, page: BrowserPage) -> bool:
        for selector in [
            "button:has-text('Submit')",
            "button:has-text('Finish')",
            "button:has-text('Complete')",
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Done')",
            "input[type='submit']",
            "button[type='submit']",
            ".submit-btn",
            ".finish-btn",
            ".next-btn",
            ".continue-btn",
        ]:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await self._behavior.random_delay(2.0)
                return True
        return False

    async def get_balance(self, page: BrowserPage) -> SiteBalance:
        await page.goto(f"{self.base_url}/surveys")
        await self._behavior.random_delay(1.5)

        content = await page.content()
        # Look for "$X.XX" or "$ X.XX USD"
        match = re.search(r"\$?\s*(\d+\.?\d*)\s*(?:USD)?", content)
        if match:
            return SiteBalance(available=float(match.group(1)), pending=0.0)
        return SiteBalance(available=0.0, pending=0.0)

    async def withdraw(self, page: BrowserPage, amount: float, method: str = "paypal") -> WithdrawalResult:
        await page.goto(f"{self.base_url}/account/redeem")
        await self._behavior.random_delay(1.5)

        # Look for withdrawal/redeem button
        redeem_btn = await page.query_selector(
            "button:has-text('Redeem'), button:has-text('Withdraw'), "
            "a:has-text('Redeem'), a:has-text('Cash Out'), "
            "[class*='redeem'], [class*='withdraw']"
        )
        if redeem_btn:
            await redeem_btn.click()
            await self._behavior.random_delay(1.0)

            confirm_btn = await page.query_selector(
                "button:has-text('Confirm'), button:has-text('Submit'), "
                "input[type='submit']"
            )
            if confirm_btn:
                await confirm_btn.click()
                await self._behavior.random_delay(2.0)

            return WithdrawalResult(
                success=True,
                amount=amount,
                method="paypal",
                message="Withdrawal initiated",
            )

        return WithdrawalResult(
            success=False,
            amount=amount,
            method=method,
            message="Could not find withdrawal option",
        )

    async def detect_captcha(self, page: BrowserPage) -> bool:
        content = (await page.content()).lower()
        indicators = ["captcha", "recaptcha", "hcaptcha", "g-recaptcha", "verify you are human"]
        return any(ind in content for ind in indicators)

    def _classify_from_context(self, html: str) -> str:
        from ..answering.question_classifier import classify  # noqa: PLC0415

        qtype = classify("", html)
        return qtype.value

    async def _extract_options(self, page: BrowserPage) -> list[str] | None:
        options = []
        labels = await page.query_selector_all("label")
        for label in labels:
            text = await label.evaluate("el => el.textContent?.trim() || ''")
            if text and 2 < len(text) < 200:
                options.append(text)

        if not options:
            items = await page.query_selector_all(
                "li, .option, .answer-option, [class*='option'], [class*='answer'], "
                "[role='radio'], [role='option']"
            )
            for item in items:
                text = await item.evaluate("el => el.textContent?.trim() || ''")
                if text and 2 < len(text) < 200:
                    options.append(text)

        return options if options else None

    async def _answer_radio(self, page: BrowserPage, value: str, options: list[str] | None) -> bool:
        val_str = value.lower()
        labels = await page.query_selector_all("label")
        for label in labels:
            text = await label.evaluate("el => el.textContent?.trim() || ''")
            if text and (text.lower() == val_str or val_str in text.lower()):
                await label.click()
                await self._behavior.random_delay(0.3)
                return True

        # Try role-based elements
        radios = await page.query_selector_all("[role='radio'], [role='option'], input[type='radio']")
        for radio in radios:
            radio_val = await radio.evaluate("el => el.getAttribute('value') || el.textContent?.trim() || ''")
            if val_str in radio_val.lower():
                await radio.click()
                await self._behavior.random_delay(0.3)
                return True

        return False

    async def _answer_checkbox(self, page: BrowserPage, value: Any, options: list[str] | None) -> bool:
        values = value if isinstance(value, list) else [value]
        for val in values:
            val_str = str(val).lower()
            labels = await page.query_selector_all("label")
            for label in labels:
                text = await label.evaluate("el => el.textContent?.trim() || ''")
                if text and (text.lower() == val_str or val_str in text.lower()):
                    checkbox = await label.query_selector("input[type='checkbox']")
                    if checkbox:
                        await checkbox.click()
                    else:
                        await label.click()
                    await self._behavior.random_delay(0.2)
        return True

    async def _answer_text(self, page: BrowserPage, value: str) -> bool:
        input_el = await page.query_selector(
            "input[type='text'], textarea, "
            "input:not([type='hidden']):not([type='submit']):not([type='checkbox']):"
            "not([type='radio']):not([type='password'])"
        )
        if input_el:
            # Give the input a temporary ID for targeting
            selector = await input_el.evaluate(
                "el => { el.id = el.id || 'po-input-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            await self._behavior.human_type(page, selector, value)
            return True
        return False

    async def _answer_select(self, page: BrowserPage, value: str) -> bool:
        select_el = await page.query_selector("select")
        if select_el:
            selector = await select_el.evaluate(
                "el => { el.id = el.id || 'po-select-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            try:
                await self._behavior.human_select(page, selector, value)
                return True
            except Exception:
                pass
        return False

    async def _answer_scale(self, page: BrowserPage, value: Any) -> bool:
        val = int(value) if str(value).isdigit() else 3
        labels = await page.query_selector_all("label")
        for label in labels:
            text = await label.evaluate("el => el.textContent?.trim() || ''")
            if text and text.strip() == str(val):
                await label.click()
                await self._behavior.random_delay(0.2)
                return True
        radios = await page.query_selector_all("input[type='radio']")
        if len(radios) >= val > 0:
            await radios[val - 1].click()
            await self._behavior.random_delay(0.2)
            return True
        return False

    async def _answer_slider(self, page: BrowserPage, value: Any) -> bool:
        slider = await page.query_selector("input[type='range']")
        if slider:
            selector = await slider.evaluate(
                "el => { el.id = el.id || 'po-slider-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            await page.evaluate(
                f"""
                const el = document.querySelector('{selector}');
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, '{value}');
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            """
            )
            return True
        return False
