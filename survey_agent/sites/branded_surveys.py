"""Branded Surveys adapter — Google OAuth login flow."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ..browser.engine import BrowserPage
from ..browser.human_behavior import HumanBehavior
from ..browser.google_oauth import GoogleOAuthHandler
from ..credentials import CredentialStore
from ..profile.store import ProfileStore
from .base import SiteAdapter, Survey, Question, Answer, SiteBalance, WithdrawalResult
from .registry import register

logger = logging.getLogger(__name__)


@register("branded-surveys")
class BrandedSurveysAdapter(SiteAdapter):
    """Adapter for surveys.gobranded.com — uses Google OAuth for login."""

    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        profile_store: ProfileStore | None = None,
        google_email: str = "",
    ):
        super().__init__(credential_store, profile_store)
        self._behavior = HumanBehavior()
        self._google_email = google_email
        self._oauth = GoogleOAuthHandler(google_email, self._behavior) if google_email else None
        self._paypal_email = "musicmusiq@live.com"

    @property
    def site_slug(self) -> str:
        return "branded-surveys"

    @property
    def base_url(self) -> str:
        return "https://surveys.gobranded.com"

    @property
    def display_name(self) -> str:
        return "Branded Surveys"

    async def login(self, page: BrowserPage) -> bool:
        """Log into Branded Surveys via Google OAuth."""
        logger.info(f"Navigating to {self.base_url}/users/sign_in")
        await page.goto(f"{self.base_url}/users/sign_in")
        await self._behavior.random_delay(2.0)

        url = await page.url()
        logger.info(f"Sign-in page URL: {url}")

        # Check if already logged in (persistent session from previous run)
        if await self.is_logged_in(page):
            logger.info("Already logged in (session cookie valid)")
            return True

        # Try Google OAuth
        if self._oauth:
            logger.info("Attempting Google OAuth login...")
            try:
                success = await self._oauth.login_via_google(page)
                if success:
                    await self._behavior.random_delay(2.0)
                    logged_in = await self.is_logged_in(page)
                    logger.info(f"Google OAuth result: {logged_in}, URL: {await page.url()}")
                    return logged_in
                else:
                    logger.warning("Google OAuth returned False — button not found or flow failed")
                    logger.info(f"Current URL: {await page.url()}")
            except Exception as e:
                logger.error(f"Google OAuth exception: {e}", exc_info=True)

        # Maybe there's a direct login form as fallback
        logger.info("Trying direct login form as fallback...")
        return await self._try_direct_login(page)

    async def _try_direct_login(self, page: BrowserPage) -> bool:
        """Try direct email/password login as fallback."""
        email_input = await page.query_selector(
            "input[type='email'], input[name*='email'], #user_email, "
            "input[name='session[email]'], input[placeholder*='email']"
        )
        if not email_input:
            logger.warning("No email input found on login page")
            # Dump page content for debugging
            content = await page.content()
            logger.debug(f"Login page HTML (first 2000 chars): {content[:2000]}")
            return False

        creds = self.credentials.retrieve(self.site_slug)
        if not creds:
            logger.warning("No stored credentials for direct login")
            return False

        logger.info(f"Logging in with email: {creds.username}")
        selector = await email_input.evaluate(
            "el => { el.id = el.id || 'sa-email-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
        )
        await self._behavior.human_type(page, selector, creds.username)
        await self._behavior.random_delay(0.5)

        pw_input = await page.query_selector("input[type='password'], #user_password")
        if pw_input:
            pw_selector = await pw_input.evaluate(
                "el => { el.id = el.id || 'sa-pw-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            await self._behavior.human_type(page, pw_selector, creds.password)
            await self._behavior.random_delay(0.5)

        login_btn = await page.query_selector(
            "input[type='submit'], button[type='submit'], .login-btn, "
            "button:has-text('Log in'), button:has-text('Sign in'), button:has-text('Login')"
        )
        if login_btn:
            await login_btn.click()
            await self._behavior.random_delay(3.0)

        return await self.is_logged_in(page)

    async def is_logged_in(self, page: BrowserPage) -> bool:
        url = await page.url()
        logged_in = "/dashboard" in url or "/users/dashboard" in url
        logger.debug(f"is_logged_in check: url={url}, result={logged_in}")
        return logged_in

    async def get_surveys(self, page: BrowserPage) -> list[Survey]:
        surveys = []
        await page.goto(f"{self.base_url}/users/dashboard")
        await self._behavior.random_delay(2.0)

        survey_elements = await page.query_selector_all(
            ".survey-card, .survey-item, [class*='survey'], "
            "a[href*='/surveys/'], a[href*='survey_id'], "
            ".offer-card, .offer-item, [class*='offer']"
        )
        logger.info(f"Found {len(survey_elements)} survey elements on dashboard")

        for elem in survey_elements:
            try:
                title = await elem.evaluate("el => el.textContent?.trim() || ''")
                href = await elem.evaluate(
                    "el => el.getAttribute('href') || el.querySelector('a')?.getAttribute('href') || ''"
                )
                points = await elem.evaluate(
                    "el => { const m = el.textContent?.match(/(\\d+)\\s*(?:pts|points|credits)/i); return m ? m[1] : '0'; }"
                )
                minutes = await elem.evaluate(
                    "el => { const m = el.textContent?.match(/(\\d+)\\s*(?:min|minutes)/i); return m ? m[1] : '0'; }"
                )

                if title and len(title) > 5:
                    survey = Survey(
                        id=href.split("/")[-1] if href and "/" in href else str(hash(title) % 100000),
                        title=title[:100],
                        url=href if href and href.startswith("http") else f"{self.base_url}{href}" if href else "",
                        estimated_minutes=int(minutes) if minutes else 0,
                        reward=float(points) / 100 if points else 0,
                    )
                    if survey.url:
                        surveys.append(survey)
            except Exception:
                continue

        logger.info(f"Discovered {len(surveys)} surveys")
        return surveys

    async def start_survey(self, page: BrowserPage, survey: Survey) -> bool:
        if not survey.url:
            return False
        await page.goto(survey.url)
        await self._behavior.random_delay(2.0)

        start_btn = await page.query_selector(
            "button:has-text('Start'), button:has-text('Begin'), "
            "a:has-text('Start'), a:has-text('Begin'), "
            "input[value*='Start'], input[value*='Begin'], "
            ".start-survey, .begin-survey, [class*='start'], [class*='begin']"
        )
        if start_btn:
            await start_btn.click()
            await self._behavior.random_delay(2.0)
            return True

        return await self._has_questions(page)

    async def _has_questions(self, page: BrowserPage) -> bool:
        content = await page.content()
        return bool(re.search(r'(?:question|survey|select|choose|rate|agree)', content, re.I))

    async def get_current_question(self, page: BrowserPage) -> Question | None:
        content = await page.content()
        if re.search(r'(?:thank\s+you|complete|finished|congratulations|survey\s+complete)', content, re.I):
            return None

        question_el = await page.query_selector(
            ".question-text, .question_text, .survey-question, "
            "[class*='question-text'], [class*='questionText'], "
            "legend, h2.question, h3.question, .prompt, "
            ".survey-prompt, [class*='prompt']"
        )
        if not question_el:
            question_el = await page.query_selector("h2, h3, .question, [class*='question']")
        if not question_el:
            return None

        text = await question_el.evaluate("el => el.textContent?.trim() || ''")
        if not text or len(text) < 3:
            return None

        html_context = await page.content()
        q_type = self._classify_from_context(html_context)
        options = await self._extract_options(page)

        return Question(
            id=abs(hash(text)) % 1000000,
            text=text,
            question_type=q_type,
            options=options,
        )

    async def answer_question(self, page: BrowserPage, question: Question, answer: Answer) -> bool:
        try:
            qtype = question.question_type
            value = answer.value

            if qtype == "radio":
                return await self._answer_radio(page, value, question.options)
            elif qtype == "checkbox":
                return await self._answer_checkbox(page, value, question.options)
            elif qtype in ("text", "textarea", "open_ended"):
                return await self._answer_text(page, value)
            elif qtype == "select":
                return await self._answer_select(page, value)
            elif qtype == "scale":
                return await self._answer_scale(page, value)
            elif qtype == "slider":
                return await self._answer_slider(page, value)
            elif qtype == "date":
                return await self._answer_text(page, str(value))
            else:
                return await self._answer_text(page, str(value))
        except Exception as e:
            logger.error(f"Answer error: {e}")
            return False

    async def submit_survey(self, page: BrowserPage) -> bool:
        for selector in [
            "button:has-text('Submit')", "button:has-text('Finish')",
            "button:has-text('Complete')", "button:has-text('Next')",
            "button:has-text('Continue')", "button:has-text('Done')",
            "input[type='submit']", "button[type='submit']",
            ".submit-btn", ".finish-btn", ".next-btn", ".continue-btn",
        ]:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await self._behavior.random_delay(2.0)
                return True
        return False

    async def get_balance(self, page: BrowserPage) -> SiteBalance:
        await page.goto(f"{self.base_url}/users/dashboard")
        await self._behavior.random_delay(1.5)

        balance_el = await page.query_selector(
            ".balance, .points, [class*='balance'], [class*='points'], "
            ".available-balance, .earned, [class*='earn'], [class*='credit']"
        )
        if balance_el:
            text = await balance_el.evaluate("el => el.textContent || ''")
            match = re.search(r'[\d,]+\.?\d*', text.replace(",", ""))
            if match:
                return SiteBalance(available=float(match.group()), pending=0.0)

        return SiteBalance(available=0.0, pending=0.0)

    async def withdraw(self, page: BrowserPage, amount: float, method: str = "paypal") -> WithdrawalResult:
        await page.goto(f"{self.base_url}/users/redeem")
        await self._behavior.random_delay(1.5)

        paypal_btn = await page.query_selector(
            "a:has-text('PayPal'), button:has-text('PayPal'), "
            "[class*='paypal'], .redeem-paypal, "
            "a:has-text('Cash Out'), button:has-text('Cash Out'), "
            "a:has-text('Redeem'), button:has-text('Redeem')"
        )
        if paypal_btn:
            await paypal_btn.click()
            await self._behavior.random_delay(1.0)

            confirm_btn = await page.query_selector(
                "button:has-text('Confirm'), button:has-text('Submit'), "
                "button:has-text('Yes'), input[type='submit']"
            )
            if confirm_btn:
                await confirm_btn.click()
                await self._behavior.random_delay(2.0)

            return WithdrawalResult(
                success=True, amount=amount, method="paypal",
                message=f"Withdrawal to {self._paypal_email}",
            )

        return WithdrawalResult(
            success=False, amount=amount, method=method,
            message="Could not find withdrawal option",
        )

    async def detect_captcha(self, page: BrowserPage) -> bool:
        content = await page.content()
        indicators = [
            "captcha", "recaptcha", "hcaptcha", "g-recaptcha",
            "verify you are human", "prove you are not a robot",
            "security check", "unusual traffic",
        ]
        return any(ind in content.lower() for ind in indicators)

    def _classify_from_context(self, html: str) -> str:
        from ..answering.question_classifier import classify
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
                "li, .option, .answer-option, [class*='option'], [class*='answer']"
            )
            for item in items:
                text = await item.evaluate("el => el.textContent?.trim() || ''")
                if text and 2 < len(text) < 200:
                    options.append(text)

        return options if options else None

    async def _answer_radio(self, page: BrowserPage, value: Any, options: list[str] | None) -> bool:
        val_str = str(value).lower()
        labels = await page.query_selector_all("label")
        for label in labels:
            text = await label.evaluate("el => el.textContent?.trim() || ''")
            if text and (text.lower() == val_str or val_str in text.lower()):
                await label.click()
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

    async def _answer_text(self, page: BrowserPage, value: Any) -> bool:
        input_el = await page.query_selector(
            "input[type='text'], textarea, "
            "input:not([type='hidden']):not([type='submit']):not([type='checkbox']):not([type='radio']):not([type='password'])"
        )
        if input_el:
            selector = await input_el.evaluate(
                "el => { el.id = el.id || 'sa-input-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            await self._behavior.human_type(page, selector, str(value))
            return True
        return False

    async def _answer_select(self, page: BrowserPage, value: Any) -> bool:
        select_el = await page.query_selector("select")
        if select_el:
            selector = await select_el.evaluate(
                "el => { el.id = el.id || 'sa-select-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            options_data = await page.evaluate(f"""
                Array.from(document.querySelector('{selector}').options).map(o => ({{value: o.value, text: o.text}}))
            """)
            for opt in options_data:
                if str(value).lower() in opt.get("text", "").lower():
                    await self._behavior.human_select(page, selector, opt["value"])
                    return True
            await self._behavior.human_select(page, selector, str(value))
            return True
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
                "el => { el.id = el.id || 'sa-slider-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            await page.evaluate(f"""
                const el = document.querySelector('{selector}');
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, '{value}');
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            """)
            return True
        return False
