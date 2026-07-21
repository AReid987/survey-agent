"""YouGov adapter — web-based survey platform."""

from __future__ import annotations

import logging
import re

from .base import SiteAdapter, SiteBalance, Survey
from .registry import register

logger = logging.getLogger(__name__)


@register("yougov")
class YouGovAdapter(SiteAdapter):
    """Adapter for yougov.com."""

    @property
    def site_slug(self) -> str:
        return "yougov"

    @property
    def base_url(self) -> str:
        return "https://account.yougov.com"

    @property
    def display_name(self) -> str:
        return "YouGov"

    async def login(self, page) -> bool:
        await page.goto(f"{self.base_url}/us-en/account")
        await page.wait_for_load()
        url = await page.url()
        if "login" in url.lower() or "sign" in url.lower():
            return False
        return True

    async def is_logged_in(self, page) -> bool:
        url = await page.url()
        return "login" not in url.lower() and "sign" not in url.lower()

    async def get_balance(self, page) -> SiteBalance:
        await page.goto(f"{self.base_url}/us-en/account")
        content = await page.content()
        match = re.search(r"(\d+(?:,\d{3})*)\s*(?:points|pts)", content, re.I)
        if match:
            return SiteBalance(available=float(match.group(1).replace(",", "")), pending=0.0, currency="points")
        return SiteBalance(available=0.0, pending=0.0)

    async def get_surveys(self, page) -> list[Survey]:
        await page.goto(f"{self.base_url}/us-en/surveys")
        surveys = []
        links = await page.query_selector_all("a[href*='survey'], a[href*='offer']")
        for link in links:
            try:
                href = await link.evaluate("el => el.href || ''")
                text = await link.evaluate("el => el.textContent?.trim() || ''")
                if href and text and len(text) > 5:
                    surveys.append(Survey(id=href.split("/")[-1], title=text[:100], url=href))
            except Exception:
                continue
        return surveys

    async def start_survey(self, page, survey: Survey) -> bool:
        if not survey.url:
            return False
        await page.goto(survey.url)
        return True

    async def get_current_question(self, page):
        return None

    async def answer_question(self, page, question, answer) -> bool:
        return False

    async def submit_survey(self, page) -> bool:
        return False

    async def withdraw(self, page, amount: float, method: str = "paypal"):
        from .base import WithdrawalResult
        return WithdrawalResult(success=False, amount=amount, method=method, message="YouGov withdrawal must be done manually")